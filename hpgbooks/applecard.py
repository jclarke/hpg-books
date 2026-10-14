"""Apple Card (Goldman Sachs) CSV importer and statement tie-out.

The Apple Card has no Finance connection. Wallet exports a transactions CSV
(`Transaction Date, Clearing Date, Description, Merchant, Category, Type,
Amount (USD), Purchased By`) and monthly statement PDFs.

Sign conventions
  * CSV and statement: a charge is positive and a payment or credit is negative
    (both raise or lower the amount owed).
  * Ledger (like every card in hpgbooks): a purchase or interest charge is
    negative and a payment received is positive, so the ledger amount is the
    CSV amount with its sign flipped. A Payment row that is positive, or an
    Interest row that is negative, is reported as a sign problem.

Dedupe: a row's id is `apple:<hash>` of transaction date + clearing date +
description + amount + occurrence index (n-th identical row in the file), so
re-importing the same or an overlapping export inserts nothing. A row that the
account already holds from another source (a linked Finance account) is
skipped, not duplicated.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from hpgbooks.cfna import (
    ambiguous_payment_pairs,
    credit_category,
    ensure_anchors_from,
    fmt as _fmt,
    pdf_text,
    set_manual_balance,
    unpair_non_payments,
)
from hpgbooks.config import get_config
from hpgbooks.db import HpgbooksError, audit, now_iso, to_cents

DEFAULT_ACCOUNT_NAME = "Apple Card"
DEFAULT_INSTITUTION = "Apple Card (Goldman Sachs)"
SOURCE = "applecard_csv"
MANUAL_NOTE = "manual:applecard CSV export + statements (no Finance connection)"

REQUIRED = ("Transaction Date", "Clearing Date", "Description", "Amount (USD)", "Type")


class AppleError(HpgbooksError):
    pass


@dataclass
class Row:
    txn_date: str
    clearing_date: str
    description: str
    merchant: str
    category: str
    type: str
    csv_cents: int  # CSV sign: charge positive
    occurrence: int = 0
    kind: str = "purchase"
    problem: str = ""

    @property
    def amount_cents(self) -> int:
        return -self.csv_cents

    @property
    def txn_id(self) -> str:
        payload = "|".join([self.txn_date, self.clearing_date, self.description, str(self.csv_cents), str(self.occurrence)])
        return "apple:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    @property
    def statement(self) -> str:
        """Statements run by calendar month of the clearing date."""
        return self.clearing_date[:7]


def _iso(text: str) -> str:
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError as exc:
        raise AppleError(f"unrecognized date {text!r}") from exc


def _kind(type_: str, csv_cents: int) -> tuple[str, str]:
    t = type_.strip().lower()
    if t == "payment":
        return ("payment", "") if csv_cents < 0 else ("payment", "payment row is positive; check the sign")
    if t == "interest":
        return ("interest", "") if csv_cents > 0 else ("interest", "interest row is negative; check the sign")
    if t in ("purchase", "debit", "installment"):
        return ("refund", "") if csv_cents < 0 else ("purchase", "")
    if t in ("credit", "refund", "return"):
        return ("credit", "") if csv_cents < 0 else ("credit", "credit row is positive; check the sign")
    return ("other", f"unknown type {type_!r}")


def read_csv(path: Path) -> list[Row]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise AppleError(f"cannot read {path.name}") from exc
    return parse_csv_text(text)


def parse_csv_text(text: str) -> list[Row]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not set(REQUIRED).issubset(set(reader.fieldnames)):
        raise AppleError("not an Apple Card transactions export (missing columns)")
    rows: list[Row] = []
    seen: Counter = Counter()
    for raw in reader:
        if not any((v or "").strip() for v in raw.values()):
            continue
        cents = to_cents(re.sub(r"[$,\s]", "", raw["Amount (USD)"]))
        kind, problem = _kind(raw["Type"], cents)
        row = Row(
            txn_date=_iso(raw["Transaction Date"]),
            clearing_date=_iso(raw["Clearing Date"] or raw["Transaction Date"]),
            description=re.sub(r"\s+", " ", (raw["Description"] or "").strip()),
            merchant=re.sub(r"\s+", " ", (raw.get("Merchant") or "").strip()),
            category=(raw.get("Category") or "").strip(),
            type=raw["Type"].strip(),
            csv_cents=cents,
            kind=kind,
            problem=problem,
        )
        key = (row.txn_date, row.clearing_date, row.description, row.csv_cents)
        row.occurrence = seen[key]
        seen[key] += 1
        rows.append(row)
    rows.sort(key=lambda r: (r.txn_date, r.clearing_date, r.description, r.occurrence))
    return rows



def to_csv(rows: list[Row]) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["date", "clearing_date", "description", "amount", "kind"])
    for r in rows:
        w.writerow([r.txn_date, r.clearing_date, r.description, f"{r.amount_cents / 100:.2f}", r.kind])
    return out.getvalue()


# --- statements ---------------------------------------------------------------

_MONEY = r"-?\$[\d,]+\.\d{2}"


def _m(text: str) -> int:
    neg = text.strip().startswith("-")
    cents = to_cents(re.sub(r"[-$,\s]", "", text))
    return -cents if neg else cents


@dataclass
class Statement:
    label: str  # YYYY-MM
    previous_balance: int | None
    total_balance: int | None
    payments: int  # statement sign: negative
    charges: int  # "Total charges, credits and returns"
    interest: int
    rows: list[tuple[str, str, int]] = field(default_factory=list)  # (txn date iso, description, cents)
    source_name: str = ""
    payment: dict = field(default_factory=dict)  # min due, due date, APR (see parse_payment_info)


_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_ROW = re.compile(rf"^\s*(\d{{2}}/\d{{2}}/\d{{4}})\s+(.+?)\s+(?:\d+%\s+\$[\d,]+\.\d{{2}}\s+)?({_MONEY})\s*$")


def parse_payment_info(text: str) -> dict:
    """Minimum payment, due date, and APR from the first page. {} when not found.

    The statement lists "Minimum Payment Due $40.00" and "Payment Due By Oct 31, 2026"
    side by side, with the dollar figures on the line under the labels.
    """
    info: dict = {}
    minimum = re.search(rf"({_MONEY})\s+([A-Z][a-z]{{2}} \d{{1,2}}, \d{{4}})\s*$", text, re.MULTILINE)
    # first-page layout: "$1,234.56     $40.00     Oct 31, 2026"
    row = re.search(rf"^\s*({_MONEY})\s+({_MONEY})\s+([A-Z][a-z]{{2}} \d{{1,2}}, \d{{4}})\s*$", text, re.MULTILINE)
    if row:
        info["statement_balance_cents"] = _m(row.group(1))
        info["min_payment_cents"] = _m(row.group(2))
        info["due_date"] = datetime.strptime(row.group(3), "%b %d, %Y").date().isoformat()
    elif minimum:
        info["due_date"] = datetime.strptime(minimum.group(2), "%b %d, %Y").date().isoformat()
        info["min_payment_cents"] = _m(minimum.group(1))
    apr = re.search(r"Annual Percentage Rate \(APR\)\s+(\d+(?:\.\d+)?)\s?%", text)
    if apr:
        info["apr"] = f"{float(apr.group(1)):g}%"
    closing = re.search(r"as of ([A-Z][a-z]{2} \d{1,2}, \d{4})", text)
    if closing and info:
        info["as_of"] = datetime.strptime(closing.group(1), "%b %d, %Y").date().isoformat()
    return info if "due_date" in info else {}


def parse_statement_text(text: str, source_name: str = "") -> Statement:
    period = re.search(r"([A-Z][a-z]{2}) \d{1,2}\s*[—-]\s*[A-Z][a-z]{2} \d{1,2}, (\d{4})", text)
    closing = re.search(r"as of ([A-Z][a-z]{2}) \d{1,2}, (\d{4})", text)
    if "Apple Card" not in text or not (period or closing):
        raise AppleError("not an Apple Card statement")
    mon, year = (period.group(1), period.group(2)) if period else (closing.group(1), closing.group(2))
    label = f"{year}-{_MONTHS[mon]:02d}"

    def one(pattern: str) -> int | None:
        match = re.search(pattern + rf"\s+({_MONEY})", text)
        return _m(match.group(1)) if match else None

    stmt = Statement(
        label=label,
        previous_balance=one(r"Previous Total Balance"),
        total_balance=one(r"(?<!Previous )Total Balance"),
        payments=one(r"Total payments for this period") or 0,
        charges=one(r"Total charges, credits and returns") or 0,
        interest=one(r"Total interest for this month") or 0,
        source_name=source_name,
        payment=parse_payment_info(text),
    )
    section = None
    for line in text.splitlines():
        head = line.strip()
        if head in ("Payments", "Transactions"):
            section = head
            continue
        if head.startswith(("Total payments for this period", "Total Daily Cash", "Total charges")):
            section = None
            continue
        if section:
            match = _ROW.match(line)
            if match:
                stmt.rows.append((_iso(match.group(1)), match.group(2), _m(match.group(3))))
    return stmt


def load_statements(paths: list[Path]) -> tuple[list[Statement], list[str]]:
    out: list[Statement] = []
    notes: list[str] = []
    for path in paths:
        try:
            out.append(parse_statement_text(pdf_text(path), path.name))
        except HpgbooksError as exc:
            notes.append(f"{path.name}: {exc}")
    out.sort(key=lambda s: s.label)
    return out, notes


def tie_out(rows: list[Row], statements: list[Statement]) -> list[dict]:
    """Compare the CSV with each statement (grouped by clearing month).

    previous + charges + interest + payments(negative) = total balance on the
    statement, and the CSV's purchases, payments, and interest for that month
    must equal the statement totals; the statement's own rows must match the
    CSV's (transaction date, amount) pairs.
    """
    reports = []
    prev_total = None
    for stmt in statements:
        month = [r for r in rows if r.statement == stmt.label]
        csv_charges = sum(r.csv_cents for r in month if r.kind in ("purchase", "refund", "credit", "other"))
        csv_payments = sum(r.csv_cents for r in month if r.kind == "payment")
        csv_interest = sum(r.csv_cents for r in month if r.kind == "interest")
        problems: list[str] = []
        if None in (stmt.previous_balance, stmt.total_balance):
            problems.append("statement balance not found")
        else:
            if stmt.previous_balance + stmt.charges + stmt.interest + stmt.payments != stmt.total_balance:
                problems.append("statement does not add up (previous + charges + interest - payments != total)")
            if prev_total is not None and prev_total != stmt.previous_balance:
                problems.append(f"previous balance {_fmt(stmt.previous_balance)} != prior statement total {_fmt(prev_total)}")
            prev_total = stmt.total_balance
        for name, want, got in (
            ("charges", stmt.charges, csv_charges),
            ("payments", stmt.payments, csv_payments),
            ("interest", stmt.interest, csv_interest),
        ):
            if want != got:
                problems.append(f"{name}: statement {_fmt(want)} vs CSV {_fmt(got)}")
        # row-level: the statement lists charges and payments by transaction date
        stmt_pairs = Counter((d, c) for d, _desc, c in stmt.rows)
        csv_pairs = Counter((r.txn_date, r.csv_cents) for r in month if r.kind != "interest")
        if stmt.rows and stmt_pairs != csv_pairs:
            only_stmt = sum((stmt_pairs - csv_pairs).values())
            only_csv = sum((csv_pairs - stmt_pairs).values())
            problems.append(f"{only_stmt} statement rows not in the CSV, {only_csv} CSV rows not on the statement")
        reports.append(
            {
                "label": stmt.label,
                "ok": not problems,
                "problems": problems,
                "previous": stmt.previous_balance,
                "total": stmt.total_balance,
                "csv_rows": len(month),
                "statement_rows": len(stmt.rows),
            }
        )
    return reports


# --- database -----------------------------------------------------------------


def account_id() -> str:
    """Id of the manual Apple Card account (config importers.applecard_account_id)."""
    return get_config().importers.applecard_account_id


def find_account(conn) -> dict | None:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id(),)).fetchone()
    if row:
        return dict(row)
    for row in conn.execute("SELECT * FROM accounts").fetchall():
        blob = f"{row['name']} {row['display_name'] or ''} {row['institution'] or ''}".lower()
        if "apple card" in blob:
            return dict(row)
    return None


def ensure_account(conn, *, actor: str = "import_applecard", dry_run: bool = False) -> tuple[str, bool]:
    """(account_id, created). Uses an existing Apple Card account (for example a linked one)."""
    from hpgbooks.accounts_admin import discover, update_settings

    acct_id = account_id()
    existing = find_account(conn)
    if existing:
        return existing["id"], False
    if dry_run:
        return acct_id, True
    payload = json.dumps(
        [{"id": acct_id, "name": DEFAULT_ACCOUNT_NAME, "official_name": DEFAULT_ACCOUNT_NAME, "institution": DEFAULT_INSTITUTION, "type": "credit", "subtype": "credit card"}]
    )
    results = discover(conn, payload, scope="personal", actor=actor)
    if not results or results[0]["status"] != "new":
        raise HpgbooksError("could not register the Apple Card account")
    conn.execute("UPDATE accounts SET notes = ? WHERE id = ?", (MANUAL_NOTE, acct_id))
    update_settings(conn, acct_id, {"sync_enabled": False}, actor=actor)
    return acct_id, True


def _near(a: str, b: str, days: int = 2) -> bool:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days) <= days


def split_existing(conn, account_id: str, rows: list[Row]) -> tuple[list[Row], list[tuple[Row, str]]]:
    """Rows the account already holds from another source (a linked Finance feed) are skipped."""
    from hpgbooks.capitalone import descriptions_match

    others = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM transactions WHERE account_id = ? AND status = 'active' AND COALESCE(source, 'finance-mcp') != ?",
            (account_id, SOURCE),
        )
    ]
    used: set[str] = set()
    kept: list[Row] = []
    skipped: list[tuple[Row, str]] = []
    for row in rows:
        hit = None
        for other in others:
            if other["id"] in used or int(other["amount_cents"]) != row.amount_cents:
                continue
            if not (_near(other["date"], row.txn_date) or _near(other["date"], row.clearing_date)):
                continue
            if not (descriptions_match(row.description, other.get("name") or "", other.get("merchant_name") or "")
                    or descriptions_match(row.merchant, other.get("name") or "", other.get("merchant_name") or "")):
                continue
            hit = other
            break
        if hit:
            used.add(hit["id"])
            skipped.append((row, hit["id"]))
        else:
            kept.append(row)
    return kept, skipped


def import_rows(conn, account_id: str, rows: list[Row], *, dry_run: bool = False, file_label: str = "applecard csv") -> dict:
    from hpgbooks.personal.classify import after_personal_import, classify_new_personal, write_personal

    present = {r["id"] for r in conn.execute("SELECT id FROM transactions WHERE account_id = ?", (account_id,))}
    fresh = [r for r in rows if r.txn_id not in present]
    kept, overlaps = split_existing(conn, account_id, fresh)
    stats = {
        "rows_in": len(rows),
        "inserted": len(kept),
        "unchanged": len(rows) - len(fresh),
        "overlaps": len(overlaps),
        "new_ids": [r.txn_id for r in kept],
        "by_month": {},
        "unpaired": [],
        "ambiguous": [],
    }
    for r in kept:
        stats["by_month"][r.statement] = stats["by_month"].get(r.statement, 0) + 1
    if dry_run:
        return stats
    ts = now_iso()
    credit_ids: list[str] = []
    for row in rows:
        if row.txn_id in present:
            conn.execute("UPDATE transactions SET last_seen_at = ? WHERE id = ?", (ts, row.txn_id))
    for row in kept:
        raw = json.dumps(
            {"kind": row.kind, "type": row.type, "category": row.category, "clearing_date": row.clearing_date, "statement": row.statement, "occurrence": row.occurrence},
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            """
            INSERT INTO transactions (
              id, account_id, date, amount_cents, direction, currency, name, merchant_name,
              description, pending, provider_category, raw_json, status, superseded_by,
              first_seen_at, last_seen_at, updated_at, source
            ) VALUES (?, ?, ?, ?, ?, 'USD', ?, ?, ?, 0, ?, ?, 'active', NULL, ?, ?, ?, ?)
            """,
            (
                row.txn_id,
                account_id,
                row.txn_date,
                row.amount_cents,
                "in" if row.amount_cents > 0 else "out",
                row.description,
                row.merchant,
                f"cleared {row.clearing_date}",
                row.category,
                raw,
                ts,
                ts,
                ts,
                SOURCE,
            ),
        )
        if row.kind in ("refund", "credit"):
            credit_ids.append(row.txn_id)
    if not kept:
        return stats
    classify_new_personal(conn, stats["new_ids"])
    for txn_id in credit_ids:
        txn = dict(conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone())
        category_id, note = credit_category(conn, txn)
        if category_id is not None:
            write_personal(conn, txn_id, category_id, "manual", 1.0, note, overwrite_manual=True, actor="import_applecard", audit_write=True)
    after_personal_import(conn)
    stats["unpaired"] = unpair_non_payments(conn, account_id, SOURCE)
    stats["ambiguous"] = ambiguous_payment_pairs(conn, account_id, SOURCE)
    conn.execute(
        """
        INSERT INTO import_log (ts, file, account_id, date_from, date_to, rows_in, inserted, updated, unchanged, superseded, skipped)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 0, ?)
        """,
        (now_iso(), file_label, account_id, min(r.txn_date for r in rows), max(r.txn_date for r in rows),
         stats["rows_in"], stats["inserted"], stats["unchanged"], stats["overlaps"]),
    )
    audit(conn, "applecard_import", field=account_id, new_value=f"inserted={stats['inserted']} unchanged={stats['unchanged']}", actor="import_applecard")
    return stats


def ensure_opening_anchor(conn, account_id: str, rows: list[Row], statements: list[Statement], *, dry_run: bool = False) -> list[str]:
    """Opening anchor: the first statement's previous balance, as of the day before the first row."""
    if not rows or not statements or statements[0].previous_balance is None:
        return []
    as_of = (date.fromisoformat(min(r.txn_date for r in rows)) - timedelta(days=1)).isoformat()
    return ensure_anchors_from(conn, account_id, statements[0].previous_balance, as_of, f"Apple Card {statements[0].label} statement previous balance", dry_run=dry_run)
