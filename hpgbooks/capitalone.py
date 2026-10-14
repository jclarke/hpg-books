"""Capital One Spark CSV import and statement verification.

Connector rows win overlaps. A later connector import supersedes the CSV row
it matches. Statement text is parsed from `pdftotext -layout` output.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

from hpgbooks.classify import classify_new, match_transfers, write_classification
from hpgbooks.config import get_config
from hpgbooks.db import HpgbooksError, audit, now_iso, to_cents


def default_account() -> str:
    """The account with role capitalone_default in the config."""
    account = get_config().account_for_role("capitalone_default")
    if not account:
        raise HpgbooksError("give --account (no account has role capitalone_default in the config)")
    return account

COV_START = date(2026, 1, 1)
COV_END = date(2026, 7, 2)
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def normalize_desc(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def descriptions_match(csv_description: str, name: str, merchant: str) -> bool:
    """True when a CSV description and a connector name/merchant are the same charge."""
    left = normalize_desc(csv_description)
    if not left:
        return False
    for raw in (name, merchant):
        right = normalize_desc(raw)
        if not right:
            continue
        if left in right or right in left:
            return True
        if len(left) >= 8 and len(right) >= 8 and left[:8] == right[:8]:
            return True
    return False


def _parse_date(value: str) -> str:
    text = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise HpgbooksError(f"unrecognized date {text!r}")


def _money_cents(value: str) -> int:
    text = (value or "").strip().replace("$", "").replace(",", "")
    if not text:
        return 0
    return to_cents(text)


def _days_apart(left: str, right: str) -> int | None:
    if not left or not right:
        return None
    return abs((date.fromisoformat(left[:10]) - date.fromisoformat(right[:10])).days)


def within_one_day(connector_date: str, trans_date: str, posted_date: str) -> bool:
    for other in (trans_date, posted_date):
        delta = _days_apart(connector_date, other)
        if delta is not None and delta <= 1:
            return True
    return False


def synthetic_id(account_id: str, trans_date: str, posted_date: str, description: str, amount_cents: int, n: int) -> str:
    payload = f"{account_id}|{trans_date}|{posted_date}|{description}|{amount_cents}|{n}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"capone:{digest}"


def account_last4(conn, account_id: str) -> str:
    row = conn.execute("SELECT last4 FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not row:
        raise HpgbooksError(f"unknown account {account_id}")
    return (row["last4"] or "").strip()


def read_capitalone_csv(path: Path, account_id: str, last4: str) -> tuple[list[dict], list[str]]:
    """Return normalized rows and warning lines for card numbers that are not last4."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise HpgbooksError(f"cannot read {path}") from exc
    reader = csv.DictReader(text.splitlines())
    required = {"Transaction Date", "Posted Date", "Card No.", "Description", "Category", "Debit", "Credit"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise HpgbooksError(f"{path} is not a Capital One transaction download")
    warnings: list[str] = []
    prepared: list[dict] = []
    seen: dict[tuple, int] = {}
    for raw in reader:
        card = (raw.get("Card No.") or "").strip()
        if card != last4:
            warnings.append(f"skip card {card or '(blank)'} != {last4}")
            continue
        trans_date = _parse_date(raw.get("Transaction Date") or "")
        posted_date = _parse_date(raw.get("Posted Date") or "")
        description = (raw.get("Description") or "").strip()
        amount_cents = _money_cents(raw.get("Credit") or "") - _money_cents(raw.get("Debit") or "")
        key = (account_id, trans_date, posted_date, description, amount_cents)
        n = seen.get(key, 0)
        seen[key] = n + 1
        prepared.append(
            {
                "id": synthetic_id(*key, n),
                "account_id": account_id,
                "date": trans_date,
                "posted_date": posted_date,
                "amount_cents": amount_cents,
                "direction": "in" if amount_cents > 0 else "out",
                "currency": "USD",
                "name": description,
                "merchant_name": "",
                "description": f"posted {posted_date}",
                "pending": 0,
                "provider_category": (raw.get("Category") or "").strip(),
                "raw_json": json.dumps(dict(raw), separators=(",", ":"), sort_keys=True),
                "source": "capitalone_csv",
                "_occurrence": n,
            }
        )
    return prepared, warnings


def posted_date_of(row: dict) -> str:
    if row.get("posted_date"):
        return row["posted_date"]
    raw = row.get("raw_json") or ""
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        posted = payload.get("Posted Date") or payload.get("posted_date") or ""
        if posted:
            try:
                return _parse_date(str(posted))
            except HpgbooksError:
                return row.get("date") or ""
    return row.get("date") or ""


def _active_connectors(conn, account_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM transactions
        WHERE account_id = ? AND status = 'active'
          AND COALESCE(source, 'finance-mcp') != 'capitalone_csv'
        """,
        (account_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def match_overlaps(csv_rows: list[dict], connectors: list[dict]) -> tuple[list[dict], list[tuple[dict, dict]]]:
    """One connector row can explain at most one CSV row."""
    used: set[str] = set()
    kept: list[dict] = []
    skipped: list[tuple[dict, dict]] = []
    for csv_row in csv_rows:
        best: tuple[int, dict] | None = None
        for connector in connectors:
            if connector["id"] in used:
                continue
            if int(connector["amount_cents"]) != int(csv_row["amount_cents"]):
                continue
            if not within_one_day(connector["date"], csv_row["date"], csv_row.get("posted_date") or ""):
                continue
            if not descriptions_match(csv_row["name"], connector.get("name") or "", connector.get("merchant_name") or ""):
                continue
            distances = [
                d
                for d in (
                    _days_apart(connector["date"], csv_row["date"]),
                    _days_apart(connector["date"], csv_row.get("posted_date") or ""),
                )
                if d is not None
            ]
            dist = min(distances) if distances else 9
            if best is None or dist < best[0]:
                best = (dist, connector)
        if best:
            used.add(best[1]["id"])
            skipped.append((csv_row, best[1]))
        else:
            kept.append(csv_row)
    return kept, skipped


def overlap_line(csv_row: dict, connector: dict) -> str:
    return (
        f"skip overlap {csv_row['date']} {csv_row['amount_cents'] / 100:.2f} "
        f"{csv_row['name']} -> {connector['id']} {(connector.get('name') or '')[:48]}"
    )


def _mark_superseded(conn, txn_id: str, superseded_by: str, note: str) -> None:
    conn.execute(
        """
        UPDATE transactions
        SET status = 'superseded', superseded_by = ?, updated_at = ?
        WHERE id = ?
        """,
        (superseded_by, now_iso(), txn_id),
    )
    audit(
        conn,
        "supersede",
        txn_id=txn_id,
        field="status",
        old_value="active",
        new_value="superseded",
        actor="import",
        note=note,
    )


def _carry_manual(conn, old_id: str, new_id: str) -> None:
    old = conn.execute("SELECT * FROM classifications WHERE txn_id = ?", (old_id,)).fetchone()
    if not old or old["source"] != "manual":
        return
    write_classification(
        conn,
        new_id,
        old["business_tag"],
        old["category"],
        "manual",
        old["confidence"],
        old["note"] or "",
        None,
        overwrite_manual=False,
        actor="import",
        audit_write=True,
    )


def supersede_matching_csv(conn, connector_rows: list[dict]) -> int:
    """Supersede active CSV rows that a newly inserted connector transaction explains."""
    if not connector_rows:
        return 0
    count = 0
    used: set[str] = set()
    for connector in connector_rows:
        candidates = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM transactions
                WHERE account_id = ? AND status = 'active' AND source = 'capitalone_csv'
                  AND amount_cents = ?
                """,
                (connector["account_id"], int(connector["amount_cents"])),
            ).fetchall()
            if row["id"] not in used
        ]
        best: tuple[int, dict] | None = None
        for csv_row in candidates:
            posted = posted_date_of(csv_row)
            if not within_one_day(connector["date"], csv_row["date"], posted):
                continue
            if not descriptions_match(csv_row.get("name") or "", connector.get("name") or "", connector.get("merchant_name") or ""):
                continue
            distances = [
                d
                for d in (
                    _days_apart(connector["date"], csv_row["date"]),
                    _days_apart(connector["date"], posted),
                )
                if d is not None
            ]
            dist = min(distances) if distances else 9
            if best is None or dist < best[0]:
                best = (dist, csv_row)
        if not best:
            continue
        csv_row = best[1]
        used.add(csv_row["id"])
        _mark_superseded(
            conn,
            csv_row["id"],
            connector["id"],
            f"superseded_by={connector['id']} capitalone_csv overlap",
        )
        _carry_manual(conn, csv_row["id"], connector["id"])
        count += 1
    return count


def import_capitalone_csv(conn, path: Path, account_id: str | None = None, dry_run: bool = False) -> dict:
    account_id = account_id or default_account()
    from hpgbooks.scope import known_account_ids

    if account_id not in known_account_ids(conn):
        raise HpgbooksError(f"unknown account {account_id}")
    last4 = account_last4(conn, account_id)
    if not last4:
        raise HpgbooksError(f"account {account_id} has no last4; cannot verify Card No.")
    rows, warnings = read_capitalone_csv(path, account_id, last4)
    connectors = _active_connectors(conn, account_id)
    kept, overlaps = match_overlaps(rows, connectors)
    stats = {
        "file": str(path),
        "rows_in": len(rows) + len(warnings),
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "superseded": 0,
        "skipped": len(warnings) + len(overlaps),
        "overlaps": overlaps,
        "warnings": warnings,
        "dry_run": dry_run,
        "date_from": min((row["date"] for row in rows), default=None),
        "date_to": max((row["date"] for row in rows), default=None),
    }
    inserted_ids: list[str] = []
    for row in kept:
        existing = conn.execute("SELECT id, status FROM transactions WHERE id = ?", (row["id"],)).fetchone()
        if existing:
            stats["unchanged"] += 1
            if not dry_run:
                conn.execute(
                    "UPDATE transactions SET last_seen_at = ? WHERE id = ?",
                    (now_iso(), row["id"]),
                )
            continue
        stats["inserted"] += 1
        if dry_run:
            continue
        ts = now_iso()
        conn.execute(
            """
            INSERT INTO transactions (
              id, account_id, date, amount_cents, direction, currency, name, merchant_name,
              description, pending, provider_category, raw_json, status, superseded_by,
              first_seen_at, last_seen_at, updated_at, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'active', NULL, ?, ?, ?, 'capitalone_csv')
            """,
            (
                row["id"],
                row["account_id"],
                row["date"],
                row["amount_cents"],
                row["direction"],
                row["currency"],
                row["name"],
                row["merchant_name"],
                row["description"],
                row["provider_category"],
                row["raw_json"],
                ts,
                ts,
                ts,
            ),
        )
        inserted_ids.append(row["id"])
    if not dry_run:
        classify_new(conn, inserted_ids)
        match_transfers(conn)
        conn.execute(
            """
            INSERT INTO import_log (
              ts, file, account_id, date_from, date_to, rows_in, inserted, updated, unchanged, superseded, skipped
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),
                str(path),
                account_id,
                stats["date_from"],
                stats["date_to"],
                stats["rows_in"],
                stats["inserted"],
                stats["updated"],
                stats["unchanged"],
                stats["superseded"],
                stats["skipped"],
            ),
        )
    return stats


def format_import_report(stats: dict) -> str:
    lines = [overlap_line(csv_row, connector) for csv_row, connector in stats["overlaps"]]
    for warning in stats["warnings"]:
        lines.append(f"warning: {warning}")
    prefix = "dry-run " if stats.get("dry_run") else ""
    lines.append(
        f"{prefix}{stats['file']}: inserted={stats['inserted']} updated={stats['updated']} "
        f"unchanged={stats['unchanged']} superseded={stats['superseded']} skipped={stats['skipped']}"
    )
    return "\n".join(lines)


# --- statements ---------------------------------------------------------------

_MONTH = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_CYCLE_RE = re.compile(rf"({_MONTH}\s+\d{{1,2}},\s+\d{{4}})\s*-\s*({_MONTH}\s+\d{{1,2}},\s+\d{{4}})")
_LINE_RE = re.compile(rf"^\s*{_MONTH}\s+(\d{{1,2}})\s+{_MONTH}\s+(\d{{1,2}})\s+(.*\S)\s*$")
_AMOUNT_RE = re.compile(r"(-)?\s*\$\s*([\d,]+\.\d{2})\s*$")
_SUMMARY_AMOUNT_RE = re.compile(r"([+\-=])?\s*\$\s*([\d,]+\.\d{2})")


def _mdy(text: str) -> date:
    return datetime.strptime(text.strip(), "%b %d, %Y").date()


def _infer_date(month_name: str, day: int, start: date, end: date) -> date:
    month = MONTHS[month_name.lower()[:3]]
    candidates = []
    for year in {start.year - 1, start.year, end.year, end.year + 1}:
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    window_lo = start - timedelta(days=10)
    window_hi = end + timedelta(days=5)
    inside = [item for item in candidates if window_lo <= item <= window_hi]
    pool = inside or candidates
    return min(pool, key=lambda item: abs((item - start).days))


def _summary_amount(summary: str, label: str) -> int:
    """Read the first dollar amount that follows the label on the same line.

    Account-summary rows share the line with the payment coupon, so the first
    dollar sign on the line is often a different field.
    """
    for line in summary.splitlines():
        index = line.find(label)
        if index < 0:
            continue
        if label == "Previous Balance" and "Earned" in line:
            continue
        if label == "New Balance" and "=" not in line:
            continue
        match = _SUMMARY_AMOUNT_RE.search(line, index + len(label))
        if not match:
            continue
        sign, number = match.group(1), match.group(2)
        cents = to_cents(number.replace(",", ""))
        if sign == "-":
            return -abs(cents)
        return abs(cents)
    raise HpgbooksError(f"statement summary is missing {label}")


def parse_statement_text(text: str, name: str = "statement") -> dict:
    cycle = _CYCLE_RE.search(text)
    if not cycle:
        raise HpgbooksError(f"{name}: no billing cycle")
    start = _mdy(cycle.group(1))
    end = _mdy(cycle.group(3))
    summary, _, _rest = text.partition("Trans Date")
    previous = abs(_summary_amount(summary, "Previous Balance"))
    payments = abs(_summary_amount(summary, "Payments"))
    other_credits = abs(_summary_amount(summary, "Other Credits"))
    transactions = abs(_summary_amount(summary, "Transactions"))
    fees = abs(_summary_amount(summary, "Fees Charged"))
    interest = abs(_summary_amount(summary, "Interest Charged"))
    cash = abs(_summary_amount(summary, "Cash Advances"))
    new_balance = abs(_summary_amount(summary, "New Balance"))
    balance_ok = previous + transactions + fees + interest + cash - payments - other_credits == new_balance

    section = None
    items: list[dict] = []
    for line in text.splitlines():
        if "Payments, Credits and Adjustments" in line:
            section = "payments"
            continue
        if "Transactions (Continued)" in line or re.search(r": Transactions\b", line):
            section = "transactions"
            continue
        if line.strip() == "Interest Charged":
            section = "interest"
            continue
        if line.strip().startswith("Fees") and "$" not in line:
            section = "fees"
            continue
        if section and "Total Transactions" in line:
            section = None
            continue
        if section not in {"payments", "transactions", "interest", "fees"}:
            continue
        parsed = _LINE_RE.match(line)
        if not parsed:
            continue
        amount_match = _AMOUNT_RE.search(parsed.group(5))
        if not amount_match:
            continue
        desc = parsed.group(5)[: amount_match.start()].strip()
        negative = bool(amount_match.group(1))
        cents = to_cents(amount_match.group(2).replace(",", ""))
        signed = -cents if negative or section == "payments" else cents
        trans = _infer_date(parsed.group(1), int(parsed.group(2)), start, end)
        posted = _infer_date(parsed.group(3), int(parsed.group(4)), start, end)
        items.append(
            {
                "section": section,
                "trans_date": trans.isoformat(),
                "posted_date": posted.isoformat(),
                "description": desc,
                "amount_cents": signed,
            }
        )
    return {
        "name": name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "previous": previous,
        "payments": payments,
        "other_credits": other_credits,
        "transactions": transactions,
        "fees": fees,
        "interest": interest,
        "cash": cash,
        "new_balance": new_balance,
        "balance_ok": balance_ok,
        "items": items,
    }


def pdftotext(path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HpgbooksError(f"pdftotext failed for {path}") from exc
    return result.stdout


def _in_coverage(iso: str) -> bool:
    day = date.fromisoformat(iso[:10])
    return COV_START <= day <= COV_END


def _prefix_match(csv_desc: str, statement_desc: str) -> bool:
    left = normalize_desc(csv_desc)
    right = normalize_desc(statement_desc)
    if not left or not right:
        return False
    return right.startswith(left) or left.startswith(right)


def _pair_lines(statement_items: list[dict], csv_rows: list[dict], section: str) -> tuple[list, list, list]:
    """Match statement lines to CSV rows. Payments are credits; transactions are debits."""
    want_positive = section == "payments"
    pool = []
    for row in csv_rows:
        amount = int(row["amount_cents"])
        if want_positive and amount <= 0:
            continue
        if not want_positive and amount >= 0:
            continue
        pool.append(row)
    used: set[int] = set()
    matched = []
    missing = []
    for item in statement_items:
        target = abs(int(item["amount_cents"]))
        found = None
        for index, row in enumerate(pool):
            if index in used:
                continue
            if abs(int(row["amount_cents"])) != target:
                continue
            if not _prefix_match(row["name"], item["description"]):
                continue
            same_trans = row["date"] == item["trans_date"]
            same_post = row.get("posted_date") == item["posted_date"]
            if not same_trans and not same_post:
                continue
            found = index
            break
        if found is None:
            missing.append(item)
        else:
            used.add(found)
            matched.append((item, pool[found]))
    extra = [row for index, row in enumerate(pool) if index not in used]
    return matched, missing, extra


def verify_statements(csv_rows: list[dict], statements: list[dict]) -> list[dict]:
    reports = []
    for statement in statements:
        start = date.fromisoformat(statement["start"])
        end = date.fromisoformat(statement["end"])
        win_start = max(start, COV_START)
        win_end = min(end, COV_END)
        if start >= COV_START and end <= COV_END:
            coverage = "full"
        elif win_start <= win_end:
            coverage = "partial"
        else:
            coverage = "none"
        def in_window(iso: str) -> bool:
            if coverage == "none":
                return False
            day = date.fromisoformat(iso[:10])
            return win_start <= day <= win_end

        stmt_payments = [
            item for item in statement["items"]
            if item["section"] == "payments" and in_window(item["posted_date"]) and _in_coverage(item["trans_date"])
        ]
        stmt_txns = [
            item for item in statement["items"]
            if item["section"] == "transactions" and in_window(item["posted_date"]) and _in_coverage(item["trans_date"])
        ]
        csv_window = [row for row in csv_rows if in_window(row["posted_date"]) and _in_coverage(row["date"])]
        pay_match, pay_missing, pay_extra = _pair_lines(stmt_payments, csv_window, "payments")
        txn_match, txn_missing, txn_extra = _pair_lines(stmt_txns, csv_window, "transactions")
        stmt_pay_total = sum(abs(item["amount_cents"]) for item in stmt_payments)
        csv_pay_total = sum(int(row["amount_cents"]) for row in csv_window if int(row["amount_cents"]) > 0)
        stmt_txn_total = sum(abs(item["amount_cents"]) for item in stmt_txns)
        csv_debit_total = sum(-int(row["amount_cents"]) for row in csv_window if int(row["amount_cents"]) < 0)
        problems = []
        if not statement["balance_ok"]:
            problems.append("summary previous+charges-payments != new balance")
        if stmt_pay_total != csv_pay_total:
            problems.append("payment total")
        if stmt_txn_total != csv_debit_total:
            problems.append("transaction total")
        if pay_missing or txn_missing or pay_extra or txn_extra:
            problems.append("line diff")
        reports.append(
            {
                "name": statement["name"],
                "start": statement["start"],
                "end": statement["end"],
                "coverage": coverage,
                "stmt_payments": stmt_pay_total,
                "csv_payments": csv_pay_total,
                "stmt_transactions": stmt_txn_total,
                "csv_debits": csv_debit_total,
                "fees": statement["fees"],
                "interest": statement["interest"],
                "balance_ok": statement["balance_ok"],
                "new_balance": statement["new_balance"],
                "missing_statement": pay_missing + txn_missing,
                "missing_csv": pay_extra + txn_extra,
                "status": "OK" if not problems else "MISMATCH",
                "problems": problems,
            }
        )
    return reports


def _dollars(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    dollars, rem = divmod(cents, 100)
    return f"{sign}{dollars:,}.{rem:02d}"


def format_verification(reports: list[dict]) -> str:
    headers = [
        "cycle", "coverage", "stmt pay", "csv pay", "stmt txn", "csv debit",
        "fees", "interest", "balance", "status",
    ]
    rows = []
    for report in reports:
        rows.append(
            [
                f"{report['start']}..{report['end']}",
                report["coverage"],
                _dollars(report["stmt_payments"]),
                _dollars(report["csv_payments"]),
                _dollars(report["stmt_transactions"]),
                _dollars(report["csv_debits"]),
                _dollars(report["fees"]),
                _dollars(report["interest"]),
                "ok" if report["balance_ok"] else "BAD",
                report["status"],
            ]
        )
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def fmt(row: list[str]) -> str:
        parts = []
        for index, cell in enumerate(row):
            parts.append(cell.ljust(widths[index]) if index < 2 else cell.rjust(widths[index]))
        return "  ".join(parts)

    lines = [fmt(headers), "  ".join("-" * width for width in widths)]
    lines.extend(fmt(row) for row in rows)
    for report in reports:
        if report["status"] == "OK":
            continue
        lines.append(
            f"{report['start']}..{report['end']} {report['status']}: {', '.join(report['problems'])} "
            f"({len(report['missing_statement'])} statement-only, {len(report['missing_csv'])} csv-only)"
        )
        for item in report["missing_statement"][:8]:
            lines.append(
                f"  statement only {item['trans_date']} {_dollars(item['amount_cents'])} {item['description'][:48]}"
            )
        for row in report["missing_csv"][:8]:
            lines.append(f"  csv only {row['date']} {_dollars(row['amount_cents'])} {row['name'][:48]}")
    return "\n".join(lines)


def load_statement(path: Path) -> dict:
    return parse_statement_text(pdftotext(path), path.name)


def verify_files(csv_path: Path, statement_paths: list[Path], account_id: str | None = None) -> tuple[list[dict], str]:
    account_id = account_id or default_account()
    from hpgbooks.db import connect

    with connect() as conn:
        last4 = account_last4(conn, account_id)
    rows, warnings = read_capitalone_csv(csv_path, account_id, last4)
    if warnings:
        raise HpgbooksError(f"{len(warnings)} CSV rows have a card number other than {last4}")
    statements = [load_statement(path) for path in statement_paths]
    reports = verify_statements(rows, statements)
    reports.sort(key=lambda report: report["start"])
    return reports, format_verification(reports)
