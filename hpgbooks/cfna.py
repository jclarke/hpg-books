"""Credit First N.A. (CFNA) statement parser and importer.

CFNA store cards (for example the Firestone Complete Auto Care Mastercard) have
no Finance connection, so the monthly statement PDF is the source of truth.
`pdftotext -layout` turns a statement into text; `parse_statement_text` reads
the ACCOUNT SUMMARY and every row of the TRANSACTIONS section from that text.

Sign conventions
  * On the statement, purchases, fees, and interest are positive (they raise
    the balance) and payments and credits are negative.
  * In the ledger a card purchase is negative and a payment received is
    positive (money into that account), so the ledger amount is the statement
    amount with its sign flipped.

Dedupe: a row's id is `cfna:<reference>:<date>:<amount_cents>`, so a second
import of the same statement (or of an overlapping one) inserts nothing.
Fee and interest rows carry no reference on the statement; they get a stable
synthetic one (`FEE-20260101`, `INT-20260305`).

Nothing here reads a real database unless the caller hands it a connection.
"""

from __future__ import annotations

import csv
import io
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from hpgbooks.db import HpgbooksError, audit, now_iso, to_cents

DEFAULT_INSTITUTION = "Credit First N.A. (CFNA)"
SOURCE = "cfna_statement"
MANUAL_NOTE = "manual:cfna statements (no Finance connection; imported from PDF)"

_MONEY = r"[+\-]?\s*\$?\s*[+\-]?\s*[\d,]+\.\d{2}"
_REF = r"(?=[A-Z0-9]*\d)[A-Z0-9]{12,24}"
_TXN_RE = re.compile(
    rf"^\s*(?P<date>\d{{2}}/\d{{2}}/\d{{4}})\s+(?:(?P<ref>{_REF})\s+)?(?P<desc>\S.*?)\s+(?P<amount>{_MONEY})\s*$"
)
_SECTION_NAMES = {
    "PAYMENTS & CREDITS": "payments_credits",
    "PAYMENTS AND CREDITS": "payments_credits",
    "PURCHASES & DEBITS": "purchases",
    "PURCHASES AND DEBITS": "purchases",
    "FEES": "fees",
    "FEES CHARGED": "fees",
    "INTEREST CHARGED": "interest",
    "CASH ADVANCES": "purchases",
    "BALANCE TRANSFERS": "purchases",
}
_PAYMENT_WORDS = re.compile(r"\bPMT\b|PAYMENT|THANK YOU", re.IGNORECASE)
_TX_HEADER = re.compile(r"^\s*TRANSACTIONS(?:\s*-\s*continued)?\s*$")
_HEADER_LINE = re.compile(r"^\s*([A-Z][A-Z &/]+?)\s*$")
_TOTAL_LINE = re.compile(r"^\s*(TRANSACTION TOTAL|TOTAL FEES FOR THIS PERIOD|TOTAL INTEREST FOR THIS PERIOD)\s+(" + _MONEY + r")\s*$")
_END_MARKER = re.compile(r"TOTAL INTEREST FOR THIS PERIOD")

# label (spaces optional, because pdftotext sometimes splits words), summary key
_SUMMARY_FIELDS = (
    ("previous_balance", "PreviousBalance"),
    ("payments", "Payments"),
    ("other_credits", "OtherCredits"),
    ("purchases", "Purchases/Debits"),
    ("fees", "FeesCharged"),
    ("interest", "InterestCharged"),
    ("new_balance", "NewBalance"),
)


class StatementError(HpgbooksError):
    pass


def parse_money(text: str) -> int:
    """'-$1,234.50' / '+$0.00' / '$41.00' -> signed cents."""
    cleaned = re.sub(r"[\s$,+]", "", text or "")
    negative = cleaned.startswith("-") or cleaned.endswith("-") or ("(" in text and ")" in text)
    cleaned = cleaned.strip("-()")
    if not re.fullmatch(r"\d+\.\d{2}", cleaned):
        raise StatementError(f"not a money amount: {text!r}")
    cents = to_cents(cleaned)
    return -cents if negative else cents


def _flex(label: str) -> str:
    """Regex for a label whose letters may be split by stray spaces."""
    return r"\s*".join(re.escape(ch) for ch in label if ch != " ")


@dataclass
class Txn:
    date: str  # ISO
    reference: str  # statement reference, or synthetic for fee/interest rows
    description: str
    statement_amount_cents: int  # statement sign: charges positive, payments/credits negative
    section: str
    kind: str  # purchase | refund | payment | credit | fee | interest
    statement: str  # YYYY-MM of the closing date
    has_reference: bool = True
    occurrence: int = 0  # 0 for the first row with this key in a statement

    @property
    def amount_cents(self) -> int:
        """Ledger sign: purchases negative, payments received positive."""
        return -self.statement_amount_cents

    @property
    def txn_id(self) -> str:
        suffix = f"#{self.occurrence + 1}" if self.occurrence else ""
        return f"cfna:{self.reference}:{self.date}:{self.amount_cents}{suffix}"


@dataclass
class Statement:
    closing_date: str
    period_start: str | None
    period_end: str | None
    last4: str | None
    summary: dict
    txns: list[Txn] = field(default_factory=list)
    section_totals: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    source_name: str = ""
    payment: dict = field(default_factory=dict)  # min due, due date, APR (see parse_payment_info)

    @property
    def label(self) -> str:
        return self.closing_date[:7]


def _to_iso(mdy: str) -> str:
    return datetime.strptime(mdy, "%m/%d/%Y").date().isoformat()


def _cycle(text: str) -> tuple[str | None, str | None]:
    match = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})\s*-\s*([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    if not match:
        return None, None
    try:
        return tuple(datetime.strptime(g, "%B %d, %Y").date().isoformat() for g in match.groups())  # type: ignore[return-value]
    except ValueError:
        return None, None


def parse_summary(text: str) -> dict:
    """Pull the ACCOUNT SUMMARY block (first page) into cents, statement sign."""
    head = text
    cut = re.search(r"^\s*(PROMOTIONAL CREDIT PLAN|TRANSACTIONS)\b", text, re.MULTILINE)
    if cut:
        head = text[: cut.start()]
    summary: dict = {}
    closing = re.search(_flex("Statement Closing Date") + r"\s+(\d{2}/\d{2}/\d{4})", head)
    if not closing:
        raise StatementError("no Statement Closing Date found; not a CFNA statement?")
    summary["closing_date"] = _to_iso(closing.group(1))
    for key, label in _SUMMARY_FIELDS:
        match = re.search(rf"^\s*{_flex(label)}\s+({_MONEY})", head, re.MULTILINE | re.IGNORECASE)
        if match:
            summary[key] = parse_money(match.group(1))
    return summary


def parse_payment_info(text: str, closing_date: str | None = None) -> dict:
    """Minimum payment, due date, and APR from the first page and the interest table.

    Returns {} when the statement has no payment box. `as_of` is the closing date.
    """
    info: dict = {}
    due = re.search(_flex("PAYMENT DUE DATE") + r"\s+(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    minimum = re.search(_flex("MINIMUM PAYMENT DUE") + r"\s+(" + _MONEY + r")", text, re.IGNORECASE)
    if due:
        info["due_date"] = _to_iso(due.group(1))
    if minimum:
        info["min_payment_cents"] = parse_money(minimum.group(1))
    balance = re.search(_flex("NEW BALANCE") + r"\s+(" + _MONEY + r")", text, re.IGNORECASE)
    if balance:
        info["statement_balance_cents"] = parse_money(balance.group(1))
    rates = []
    for line in text.splitlines():
        match = re.match(r"^\s*(R\s?evolving|PROTECTED BALANCE|Promotional[A-Za-z ]*|Purchases?)\s+(\d+\.\d+)\s?%", line, re.IGNORECASE)
        if match:
            raw = re.sub(r"\s+", "", match.group(1)).lower()
            name = "revolving" if raw.startswith("rev") else "protected" if raw.startswith("prot") else "promo" if raw.startswith("promo") else "purchases"
            rate = f"{float(match.group(2)):g}% {name}"
            if rate not in rates:
                rates.append(rate)
    if rates:
        info["apr"] = "; ".join(rates)
    if closing_date and info:
        info["as_of"] = closing_date
    return info if "due_date" in info else {}


def _last4(text: str) -> str | None:
    for pattern in (r"ACCOUNT ENDING\s+(\d{4})\b", r"#(\d{4})\b", r"ACCOUNT ENDING[^\n]*\n[^\n]*?\b(\d{4})\b"):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def _kind(section: str, description: str, statement_amount: int) -> str:
    if section == "purchases":
        return "refund" if statement_amount < 0 else "purchase"
    if section == "payments_credits":
        if statement_amount < 0 and _PAYMENT_WORDS.search(description):
            return "payment"
        return "credit" if statement_amount < 0 else "debit"
    if section == "fees":
        return "fee"
    if section == "interest":
        return "interest"
    return "other"


def parse_statement_text(text: str, source_name: str = "") -> Statement:
    """Parse the text of one CFNA statement (pdftotext -layout output)."""
    summary = parse_summary(text)
    start, end = _cycle(text)
    stmt = Statement(
        payment=parse_payment_info(text, summary["closing_date"]),
        closing_date=summary["closing_date"],
        period_start=start,
        period_end=end,
        last4=_last4(text),
        summary=summary,
        source_name=source_name,
    )
    lines = text.splitlines()
    in_tx = False
    section: str | None = None
    for index, line in enumerate(lines):
        if not in_tx:
            if _TX_HEADER.match(line):
                in_tx = True
            continue
        if _TX_HEADER.match(line):
            continue  # "TRANSACTIONS - continued" keeps the current section
        total = _TOTAL_LINE.match(line)
        if total:
            stmt.section_totals[total.group(1)] = parse_money(total.group(2))
            if _END_MARKER.search(line):
                break
            continue
        stripped = line.strip()
        header = _HEADER_LINE.match(line)
        if header and stripped.upper() == stripped:
            name = re.sub(r"\s+", " ", header.group(1).strip())
            upcoming = [nxt.strip() for nxt in lines[index + 1 : index + 4]]
            if name in _SECTION_NAMES:
                section = _SECTION_NAMES[name]
                continue
            if any(nxt.startswith("Date") for nxt in upcoming):
                section = "other"
                stmt.warnings.append(f"unknown section {name!r}; rows kept with kind 'other'")
                continue
        match = _TXN_RE.match(line)
        if not match:
            continue
        if section is None:
            stmt.warnings.append(f"row before any section header: {stripped[:40]}")
            section = "other"
        description = re.sub(r"\s+", " ", match.group("desc")).strip()
        amount = parse_money(match.group("amount"))
        day = _to_iso(match.group("date"))
        ref = match.group("ref")
        kind = _kind(section, description, amount)
        if not ref:
            ref = f"{'FEE' if section == 'fees' else 'INT' if section == 'interest' else 'ROW'}-{day.replace('-', '')}"
        stmt.txns.append(
            Txn(
                date=day,
                reference=ref,
                description=description,
                statement_amount_cents=amount,
                section=section,
                kind=kind,
                statement=stmt.label,
                has_reference=bool(match.group("ref")),
            )
        )
    if not in_tx:
        stmt.warnings.append("no TRANSACTIONS section found")
    seen: dict[tuple, int] = {}
    for txn in stmt.txns:
        key = (txn.reference, txn.date, txn.amount_cents)
        txn.occurrence = seen.get(key, 0)
        seen[key] = txn.occurrence + 1
    return stmt


def reconcile(stmt: Statement) -> dict:
    """Check the parsed rows against the ACCOUNT SUMMARY.

    previous + purchases + fees + interest - payments - credits = new balance,
    plus each summary line against the sum of its parsed rows, plus the printed
    section totals. Returns {"ok": bool, "checks": [...], "problems": [...]}.
    """
    s = stmt.summary
    by_kind: dict[str, int] = {}
    for txn in stmt.txns:
        by_kind[txn.kind] = by_kind.get(txn.kind, 0) + txn.statement_amount_cents
    computed = {
        "payments": -by_kind.get("payment", 0),
        "other_credits": -by_kind.get("credit", 0),
        "purchases": by_kind.get("purchase", 0) + by_kind.get("refund", 0) + by_kind.get("debit", 0),
        "fees": by_kind.get("fee", 0),
        "interest": by_kind.get("interest", 0),
    }
    problems: list[str] = []
    checks: list[dict] = []
    # The summary prints payments and credits negative; compare magnitudes.
    printed = {
        "payments": abs(s["payments"]) if "payments" in s else None,
        "other_credits": abs(s["other_credits"]) if "other_credits" in s else None,
        "purchases": s.get("purchases"),
        "fees": s.get("fees"),
        "interest": s.get("interest"),
    }
    for key, value in computed.items():
        want = printed[key]
        ok = want is not None and want == value
        checks.append({"name": key, "summary": want, "rows": value, "ok": ok})
        if not ok:
            problems.append(f"{key}: summary {fmt(want)} vs rows {fmt(value)}")
    needed = ("previous_balance", "new_balance")
    if all(k in s for k in needed):
        total = s["previous_balance"] + computed["purchases"] + computed["fees"] + computed["interest"] - computed["payments"] - computed["other_credits"]
        ok = total == s["new_balance"]
        checks.append({"name": "balance", "summary": s["new_balance"], "rows": total, "ok": ok})
        if not ok:
            problems.append(f"balance: previous + rows = {fmt(total)}, statement new balance {fmt(s['new_balance'])}")
        if "payments" in s and "purchases" in s:
            # The same equation using only the printed summary lines.
            printed_total = (
                s["previous_balance"] + s.get("purchases", 0) + s.get("fees", 0) + s.get("interest", 0)
                - abs(s.get("payments", 0)) - abs(s.get("other_credits", 0))
            )
            if printed_total != s["new_balance"]:
                problems.append(f"summary lines do not add to new balance ({fmt(printed_total)} vs {fmt(s['new_balance'])})")
    else:
        problems.append("summary is missing previous or new balance")
    totals = stmt.section_totals
    for label, kinds in (
        ("TRANSACTION TOTAL", ("purchase", "refund", "debit")),
        ("TOTAL FEES FOR THIS PERIOD", ("fee",)),
        ("TOTAL INTEREST FOR THIS PERIOD", ("interest",)),
    ):
        if label in totals:
            rows = sum(by_kind.get(kind, 0) for kind in kinds)
            if rows != totals[label]:
                problems.append(f"{label.lower()}: printed {fmt(totals[label])} vs rows {fmt(rows)}")
    return {"ok": not problems, "checks": checks, "problems": problems}


def fmt(cents) -> str:
    if cents is None:
        return "n/a"
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


# --- PDF reading --------------------------------------------------------------


def pdf_text(path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise StatementError("pdftotext is not installed (poppler-utils)") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise StatementError(f"cannot read {path.name} with pdftotext") from exc
    return result.stdout


def looks_like_cfna(text: str) -> bool:
    return bool(re.search(r"CFNA|CREDIT FIRST", text, re.IGNORECASE)) and bool(re.search(r"ACCOUNT\s+SUMMARY", text)) and bool(
        re.search(_flex("Statement Closing Date"), text)
    )


def find_statement_pdfs(folder: Path, pattern: str = "statement-*.pdf") -> list[Path]:
    if folder.is_file():
        return [folder]
    return sorted(path for path in folder.glob(pattern) if path.is_file())


def account_id(last4: str) -> str:
    """Id of the manual account the importer creates for the card ending `last4`."""
    return f"manual-cfna-{last4}"


def load_statements(paths: list[Path], last4: str) -> tuple[list[Statement], list[str]]:
    """Parse every CFNA statement for `last4`. Returns (statements by closing date, skipped notes)."""
    statements: list[Statement] = []
    skipped: list[str] = []
    for path in paths:
        try:
            text = pdf_text(path)
        except StatementError as exc:
            skipped.append(f"{path.name}: {exc}")
            continue
        if not looks_like_cfna(text):
            skipped.append(f"{path.name}: not a CFNA statement")
            continue
        try:
            stmt = parse_statement_text(text, path.name)
        except StatementError as exc:
            skipped.append(f"{path.name}: {exc}")
            continue
        if stmt.last4 and stmt.last4 != last4:
            skipped.append(f"{path.name}: account ending {stmt.last4}, expected {last4}")
            continue
        statements.append(stmt)
    statements.sort(key=lambda s: s.closing_date)
    return statements, skipped


def unique_rows(statements: list[Statement]) -> list[Txn]:
    """All rows across statements, an overlapping row kept once (key + occurrence)."""
    seen: set[str] = set()
    rows: list[Txn] = []
    for stmt in statements:
        for txn in stmt.txns:
            if txn.txn_id in seen:
                continue
            seen.add(txn.txn_id)
            rows.append(txn)
    return rows


def ledger_description(txn: Txn) -> str:
    return txn.description


def to_csv(rows: list[Txn]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["date", "reference", "description", "amount", "kind", "statement"])
    for txn in rows:
        writer.writerow([txn.date, txn.reference, txn.description, f"{txn.amount_cents / 100:.2f}", txn.kind, txn.statement])
    return out.getvalue()


# --- database -----------------------------------------------------------------


def find_account(conn, last4: str) -> dict | None:
    """The CFNA account: the manual one by id, else a card with this last4 at Credit First."""
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id(last4),)).fetchone()
    if row:
        return dict(row)
    for row in conn.execute("SELECT * FROM accounts WHERE last4 = ?", (last4,)).fetchall():
        blob = f"{row['name']} {row['institution'] or ''} {row['notes'] or ''}".lower()
        if "credit first" in blob or "cfna" in blob:
            return dict(row)
    return None


def ensure_account(conn, last4: str, *, actor: str = "import_cfna", dry_run: bool = False) -> tuple[str, bool]:
    """Return (account_id, created). Creates the personal manual account through `accounts discover`."""
    from hpgbooks.accounts_admin import discover, update_settings

    acct_id = account_id(last4)
    name = f"Credit First (CFNA) {last4}"
    existing = find_account(conn, last4)
    if existing:
        return existing["id"], False
    if dry_run:
        return acct_id, True
    payload = json.dumps(
        [
            {
                "id": acct_id,
                "name": name,
                "official_name": name,
                "mask": last4,
                "institution": DEFAULT_INSTITUTION,
                "type": "credit",
                "subtype": "credit card",
            }
        ]
    )
    results = discover(conn, payload, scope="personal", actor=actor)
    if not results or results[0]["status"] != "new":
        raise HpgbooksError("could not register the CFNA account")
    conn.execute("UPDATE accounts SET notes = ? WHERE id = ?", (MANUAL_NOTE, acct_id))
    # Statement-fed: the daily Finance pull must not list it.
    update_settings(conn, acct_id, {"sync_enabled": False}, actor=actor)
    return acct_id, True


def credit_category(conn, txn: dict):
    """For a refund/credit: the category the same merchant gets as a purchase, else Refunds."""
    from hpgbooks.personal.classify import decide_personal, load_aliases, load_personal_rules, uncategorized_id
    from hpgbooks.personal.classify import category_id as pcat

    rules = load_personal_rules(conn)
    mirrored = dict(txn)
    mirrored["amount_cents"] = -abs(int(txn["amount_cents"]))
    mirrored["direction"] = "out"
    decision = decide_personal(mirrored, rules, load_aliases(conn), uncategorized_id(conn))
    if decision["source"] == "rule" and decision.get("category_id"):
        return int(decision["category_id"]), "refund mirrors the purchase category"
    fallback = pcat(conn, "Income", "Refunds & reimbursements")
    return fallback, "refund; no purchase rule matched"


def import_rows(conn, account_id: str, rows: list[Txn], *, dry_run: bool = False, file_label: str = "cfna statements") -> dict:
    """Insert rows that are not in the ledger yet. Idempotent on the txn id."""
    from hpgbooks.personal.classify import (
        after_personal_import,
        classify_new_personal,
        current,
        write_personal,
    )

    stats = {"rows_in": len(rows), "inserted": 0, "unchanged": 0, "new_ids": [], "by_statement": {}}
    credit_ids: list[str] = []
    ts = now_iso()
    for txn in rows:
        per = stats["by_statement"].setdefault(txn.statement, {"rows": 0, "inserted": 0})
        per["rows"] += 1
        if conn.execute("SELECT 1 FROM transactions WHERE id = ?", (txn.txn_id,)).fetchone():
            stats["unchanged"] += 1
            if not dry_run:
                conn.execute("UPDATE transactions SET last_seen_at = ? WHERE id = ?", (ts, txn.txn_id))
            continue
        stats["inserted"] += 1
        per["inserted"] += 1
        stats["new_ids"].append(txn.txn_id)
        if txn.kind in ("refund", "credit"):
            credit_ids.append(txn.txn_id)
        if dry_run:
            continue
        raw = json.dumps(
            {"reference": txn.reference, "statement": txn.statement, "section": txn.section, "kind": txn.kind, "has_reference": txn.has_reference},
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            """
            INSERT INTO transactions (
              id, account_id, date, amount_cents, direction, currency, name, merchant_name,
              description, pending, provider_category, raw_json, status, superseded_by,
              first_seen_at, last_seen_at, updated_at, source
            ) VALUES (?, ?, ?, ?, ?, 'USD', ?, '', ?, 0, ?, ?, 'active', NULL, ?, ?, ?, ?)
            """,
            (
                txn.txn_id,
                account_id,
                txn.date,
                txn.amount_cents,
                "in" if txn.amount_cents > 0 else "out",
                ledger_description(txn),
                f"CFNA statement {txn.statement}; ref {txn.reference}",
                txn.kind,
                raw,
                ts,
                ts,
                ts,
                SOURCE,
            ),
        )
    if dry_run or not stats["new_ids"]:
        return stats
    classify_new_personal(conn, stats["new_ids"])
    # A credit has the opposite sign of the purchase, so sign-specific rules miss
    # it. Put it in the purchase's category (it nets against that spending). It is
    # written as a manual choice so a later reclassify cannot turn it into income.
    for txn_id in credit_ids:
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        category_id, note = credit_category(conn, dict(row))
        if category_id is not None:
            write_personal(conn, txn_id, category_id, "manual", 1.0, note, overwrite_manual=True, actor="import_cfna", audit_write=True)
    after_personal_import(conn)
    stats["unpaired"] = unpair_non_payments(conn, account_id)
    stats["ambiguous"] = ambiguous_payment_pairs(conn, account_id)
    conn.execute(
        """
        INSERT INTO import_log (ts, file, account_id, date_from, date_to, rows_in, inserted, updated, unchanged, superseded, skipped)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 0, 0)
        """,
        (
            now_iso(),
            file_label,
            account_id,
            min(t.date for t in rows),
            max(t.date for t in rows),
            stats["rows_in"],
            stats["inserted"],
            stats["unchanged"],
        ),
    )
    audit(conn, "cfna_import", field=account_id, new_value=f"inserted={stats['inserted']} unchanged={stats['unchanged']}", actor="import_cfna")
    return stats


def unpair_non_payments(conn, account_id: str, source: str = SOURCE) -> list[str]:
    """Undo a transfer pairing that grabbed a purchase, fee, or interest row.

    Pairing matches equal opposite amounts within 3 days when one side looks like
    a transfer, so a card purchase can pair with an unrelated deposit of the same
    size. Only CFNA payments are transfers; the pair is rejected (remembered) and
    both legs are re-categorized by the rules. Returns the rejected pair keys.
    """
    from hpgbooks.personal.classify import reclassify_personal, set_transfer_decision

    rows = conn.execute(
        """
        SELECT t.id, t.amount_cents, p.transfer_pair FROM transactions t
        JOIN p_classifications p ON p.txn_id = t.id
        WHERE t.account_id = ? AND t.source = ? AND p.source = 'transfer'
          AND p.transfer_pair IS NOT NULL AND p.transfer_pair NOT LIKE 'biz:%'
          AND json_extract(t.raw_json, '$.kind') IN ('purchase', 'fee', 'interest')
        """,
        (account_id, source),
    ).fetchall()
    keys: list[str] = []
    for row in rows:
        # out leg (negative amount) first, matching find_transfer_pairs
        key = f"{row['id']}|{row['transfer_pair']}" if int(row["amount_cents"]) < 0 else f"{row['transfer_pair']}|{row['id']}"
        set_transfer_decision(conn, key, "rejected", actor="import_cfna")
        keys.append(key)
    if keys:
        reclassify_personal(conn)
    return keys


def ambiguous_payment_pairs(conn, account_id: str, source: str = SOURCE) -> list[str]:
    """Pair keys that need `hpgbooks personal transfers confirm` and involve this account."""
    from hpgbooks.personal.classify import detect_transfers

    ids = {row["id"] for row in conn.execute("SELECT id FROM transactions WHERE account_id = ? AND source = ?", (account_id, source))}
    return [item["key"] for item in detect_transfers(conn)["ambiguous"] if item["out"]["id"] in ids or item["in"]["id"] in ids]


def ensure_anchors_from(conn, account_id: str, owed_cents: int, as_of: str, note: str, *, dry_run: bool = False) -> list[str]:
    """Record an opening anchor when the account has none yet."""
    from hpgbooks.balances import set_anchor

    if conn.execute("SELECT 1 FROM balance_anchors WHERE account_id = ? LIMIT 1", (account_id,)).fetchone():
        return []
    if not dry_run:
        set_anchor(conn, account_id, owed_cents, as_of, "statement", note, actor="import_cfna")
    return [f"opening anchor {fmt(owed_cents)} owed as of {as_of}"]


def ensure_anchors(conn, account_id: str, statements: list[Statement], *, dry_run: bool = False) -> list[str]:
    """Opening anchor from the earliest statement when the account has none."""
    from hpgbooks.balances import set_anchor

    notes: list[str] = []
    if not statements:
        return notes
    has_any = conn.execute("SELECT 1 FROM balance_anchors WHERE account_id = ? LIMIT 1", (account_id,)).fetchone()
    if has_any:
        return notes
    first = statements[0]
    prev = first.summary.get("previous_balance")
    if prev is None:
        return notes
    if first.period_start:
        as_of = (date.fromisoformat(first.period_start) - timedelta(days=1)).isoformat()
    else:
        as_of = (date.fromisoformat(first.closing_date) - timedelta(days=31)).isoformat()
    notes.append(f"opening anchor {fmt(prev)} owed as of {as_of} (previous balance on the {first.label} statement)")
    if not dry_run:
        set_anchor(conn, account_id, prev, as_of, "statement", f"CFNA {first.label} statement previous balance", actor="import_cfna")
    return notes


def set_manual_balance(conn, account_id: str, owed_cents: int, as_of: str, note: str, *, dry_run: bool = False) -> str:
    """Record the amount owed as of a date (positive = owed). Skips an identical latest anchor."""
    from hpgbooks.balances import set_anchor

    latest = conn.execute(
        "SELECT as_of_date, balance_cents FROM balance_anchors WHERE account_id = ? ORDER BY as_of_date DESC, id DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    if latest and latest["as_of_date"] == as_of and int(latest["balance_cents"]) == owed_cents:
        return f"balance {fmt(owed_cents)} owed as of {as_of} already recorded"
    if not dry_run:
        set_anchor(conn, account_id, owed_cents, as_of, "statement", note, actor="import_cfna")
    return f"balance set: {fmt(owed_cents)} owed as of {as_of}"
