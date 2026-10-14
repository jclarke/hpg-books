"""Monarch Money transaction export: fill gaps in the personal ledger and hint categories.

Monarch's CSV (Date, Merchant, Category, Account, Original Statement, Notes, Amount,
Tags, Owner, Business Entity, Reviewed, Id; money out is negative, like the ledger)
is a second opinion on the same accounts the Finance feed covers. This module:

* maps each Monarch account to a personal account (last 4 in the name, or an
  alias such as "Savings" -> Apple Savings),
* finds the 2026 rows our books lack (same amount within a few days, a Monarch
  split of one charge, or a sign-flipped card payment all count as present),
* inserts only those, as source "monarch_csv" with id "monarch:<Monarch Id>", and
* turns Monarch's category + merchant into rules for rows still uncategorized.

Accounts fed by an authoritative source (Apple Card CSV, CFNA statements) and
business/excluded accounts are compared, never imported.
"""

from __future__ import annotations

import csv
import io
import itertools
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from hpgbooks.config import get_config
from hpgbooks.db import HpgbooksError, audit, now_iso

SOURCE = "monarch_csv"
SINCE = "2026-01-01"
EXPECTED_HEADER = ["Date", "Merchant", "Category", "Account", "Original Statement", "Notes", "Amount", "Tags", "Owner", "Business Entity", "Reviewed", "Id"]
TOLERANCES = (0, 1, 4, 12)
AUTHORITATIVE_SOURCES = ("applecard_csv", "cfna_statement")
PAYMENT_CATEGORIES = {"credit card payment", "loan repayment", "loan repayments", "transfer"}
NOT_A_PAYMENT = re.compile(r"cash ?back|reward|bonus|statement credit|refund|return", re.I)
SIGN_FLIP_CATEGORIES = {"credit card payment", "loan repayment", "loan repayments", "transfer"}

# Monarch category -> (group, name) in our personal categories. Left out on purpose:
# Transfer / payments (pairing handles them), Miscellaneous, Uncategorized, Insurance,
# Other Income, Check, business and allowance categories.
CATEGORY_MAP = {
    "restaurants & bars": ("Food", "Dining out"),
    "groceries": ("Food", "Groceries"),
    "coffee shops": ("Food", "Coffee"),
    "gas": ("Transportation", "Gas & fuel"),
    "shopping": ("Shopping", "General"),
    "clothing": ("Shopping", "Clothing"),
    "electronics": ("Shopping", "Electronics"),
    "furniture & housewares": ("Shopping", "Home goods"),
    "home improvement": ("Housing", "Home maintenance"),
    "home maintenance": ("Housing", "Home maintenance"),
    "entertainment & recreation": ("Entertainment", "Entertainment"),
    "software": ("Subscriptions", "Subscriptions"),
    "education": ("Education", "Education"),
    "financial fees": ("Fees & interest", "Fees & interest"),
    "internet & cable": ("Utilities", "Internet & cable"),
    "gas & electric": ("Utilities", "Electric"),
    "water": ("Utilities", "Gas & water"),
    "garbage": ("Utilities", "Trash"),
    "phone": ("Utilities", "Phone"),
    "medical": ("Health", "Doctor"),
    "dentist": ("Health", "Dental"),
    "fitness": ("Health", "Fitness"),
    "personal": ("Personal care", "Personal care"),
    "cash & atm": ("Cash & ATM", "Cash & ATM"),
    "charity": ("Gifts & donations", "Charity"),
    "gifts": ("Gifts & donations", "Gifts & donations"),
    "parking & tolls": ("Transportation", "Parking & tolls"),
    "auto maintenance": ("Transportation", "Auto maintenance"),
    "auto payment": ("Transportation", "Auto payment"),
    "taxi & ride shares": ("Transportation", "Rideshare"),
    "child activities": ("Kids", "Activities"),
    "pets": ("Pets", "Pets"),
    "storage": ("Housing", "Storage"),
    "taxes": ("Taxes", "Taxes"),
    "travel & vacation": ("Travel", "Travel"),
    "mortgage": ("Housing", "Mortgage"),
    "rent": ("Housing", "Rent"),
    "paychecks": ("Income", "Paycheck"),
    "payroll": ("Income", "Paycheck"),
    "dividends & capital gains": ("Income", "Interest & dividends"),
}


def _cents(text: str) -> int:
    return int((__import__("decimal").Decimal(text.strip().replace(",", "")) * 100).to_integral_value())


def norm(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


@dataclass
class MRow:
    date: str
    merchant: str
    category: str
    account: str
    statement: str
    amount_cents: int
    owner: str
    mid: str
    notes: str = ""
    entity: str = ""

    @property
    def txn_id(self) -> str:
        return f"monarch:{self.mid}"


def parse_csv_text(text: str) -> list[MRow]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if reader.fieldnames is None or [h.strip() for h in reader.fieldnames][:7] != EXPECTED_HEADER[:7] or "Id" not in reader.fieldnames:
        raise HpgbooksError("not a Monarch transactions export (expected Date, Merchant, Category, Account, Original Statement, Notes, Amount, ... Id)")
    rows: list[MRow] = []
    for line, raw in enumerate(reader, start=2):
        try:
            day = date.fromisoformat((raw["Date"] or "").strip()).isoformat()
            cents = _cents(raw["Amount"])
        except Exception as exc:  # noqa: BLE001
            raise HpgbooksError(f"line {line}: bad date or amount") from exc
        mid = (raw.get("Id") or "").strip()
        if not mid:
            raise HpgbooksError(f"line {line}: missing Id")
        rows.append(
            MRow(day, (raw["Merchant"] or "").strip(), (raw["Category"] or "").strip(), (raw["Account"] or "").strip(),
                 re.sub(r"\s+", " ", raw["Original Statement"] or "").strip(), cents, (raw.get("Owner") or "").strip(), mid, (raw.get("Notes") or "").strip(), (raw.get("Business Entity") or "").strip())
        )
    return rows


def read_csv(path) -> list[MRow]:
    return parse_csv_text(Path(path).expanduser().read_text(encoding="utf-8"))


# --- accounts -------------------------------------------------------------------


def resolve_accounts(conn, monarch_names: list[str], overrides: dict[str, str] | None = None) -> dict[str, dict | None]:
    """Monarch account name -> our account row (any scope), or None when nothing matches."""
    from hpgbooks.scope import all_accounts

    accounts = all_accounts(conn)
    aliases = {norm(k): norm(v) for k, v in {**get_config().importers.monarch_aliases, **(overrides or {})}.items()}
    out: dict[str, dict | None] = {}
    for name in monarch_names:
        found = None
        key = norm(name)
        by_id = [a for a in accounts if a["id"] == name]
        match = re.search(r"\.\.\.(\d{4})\)", name) or re.search(r"(\d{4})\)?$", name)
        last4 = match.group(1) if match else None
        if by_id:
            found = by_id[0]
        elif key in aliases:
            want = aliases[key]
            hits = [a for a in accounts if want in (norm(a.get("name")), norm(a.get("display_name")))]
            if not hits:
                hits = [a for a in accounts if want in norm(a.get("display_name") or a.get("name"))]
            if len(hits) > 1:
                hits = [a for a in hits if a["scope"] == "personal"]
            found = hits[0] if len(hits) == 1 else None
        elif last4:
            hits = [a for a in accounts if a.get("last4") == last4]
            if len(hits) > 1:  # same last 4 in different scopes: prefer the one whose name appears in Monarch's
                hits = [a for a in hits if a["scope"] == "personal"] or hits
            found = hits[0] if len(hits) == 1 else None
        if found is None and key not in aliases:
            hits = [a for a in accounts if norm(a.get("name")) == key or norm(a.get("display_name")) == key]
            found = hits[0] if len(hits) == 1 else None
        out[name] = found
    return out


def authoritative(conn, account_id: str) -> bool:
    row = conn.execute(
        f"SELECT COUNT(*) FROM transactions WHERE account_id = ? AND source IN ({','.join('?' * len(AUTHORITATIVE_SOURCES))})",
        (account_id, *AUTHORITATIVE_SOURCES),
    ).fetchone()
    return bool(row[0])


# --- matching -------------------------------------------------------------------


def _days(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def match_rows(existing: list[dict], rows: list[MRow]) -> dict:
    """Pair Monarch rows with rows we already hold.

    Passes: same amount within 0/1/4/12 days (closest date and statement text first);
    a payment-type row whose sign Monarch flipped; a Monarch split (two or three rows
    that add up to one of ours on the same day). Returns {"pairs": [(MRow, existing, how)],
    "missing": [MRow], "left_over": [existing rows nobody claimed]}.
    """
    pool = list(existing)
    rest = sorted(rows, key=lambda r: (r.date, r.mid))
    pairs: list[tuple[MRow, dict, str]] = []

    def take(row: MRow, cents: int, tol: int, how: str) -> bool:
        best = None
        for held in pool:
            if held["amount_cents"] != cents:
                continue
            gap = _days(held["date"], row.date)
            if gap > tol:
                continue
            same_text = norm(row.statement)[:6] == norm(held.get("name"))[:6]
            score = (gap, 0 if same_text else 1)
            if best is None or score < best[0]:
                best = (score, held)
        if best is None:
            return False
        pool.remove(best[1])
        pairs.append((row, best[1], how))
        return True

    by_id = {h["id"]: h for h in pool}
    leftovers = []
    for row in rest:
        held = by_id.get(row.txn_id)
        if held is not None and held in pool:
            pool.remove(held)
            pairs.append((row, held, "imported earlier"))
        else:
            leftovers.append(row)
    rest = leftovers
    for tol in TOLERANCES:
        rest = [r for r in rest if not take(r, r.amount_cents, tol, f"amount within {tol}d")]
    rest = [r for r in rest if not (r.category.lower() in SIGN_FLIP_CATEGORIES and take(r, -r.amount_cents, 12, "sign flipped by Monarch"))]
    # Monarch splits
    by_day: dict[str, list[MRow]] = defaultdict(list)
    for r in rest:
        by_day[r.date].append(r)
    consumed: set[str] = set()
    for day, group in sorted(by_day.items()):
        for size in (2, 3):
            for combo in itertools.combinations([g for g in group if g.mid not in consumed], size):
                total = sum(c.amount_cents for c in combo)
                held = next((h for h in pool if h["amount_cents"] == total and _days(h["date"], day) <= 4), None)
                if held is not None:
                    pool.remove(held)
                    consumed.update(c.mid for c in combo)
                    pairs.extend((c, held, "split of one charge") for c in combo)
    missing = [r for r in rest if r.mid not in consumed]
    return {"pairs": pairs, "missing": missing, "left_over": pool}


def load_existing(conn, account_id: str, since: str = SINCE) -> list[dict]:
    start = (date.fromisoformat(since) - timedelta(days=15)).isoformat()
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, date, amount_cents, name, source FROM transactions WHERE account_id = ? AND status = 'active' AND date >= ?",
            (account_id, start),
        )
    ]


def _key(row: MRow) -> tuple:
    return (row.date, row.amount_cents, norm(row.statement))


def collapse_monarch_duplicates(all_rows: list[MRow], pairs: list, missing: list[MRow]) -> tuple[list[MRow], list[MRow]]:
    """Monarch sometimes holds an account twice (two connections): identical rows twice where
    the Finance feed has one. When an account shows that (two or more identical groups only
    partly matched), identical missing rows are imported once. Returns (missing, dropped)."""
    totals = Counter(_key(r) for r in all_rows)
    held = Counter(_key(r) for r, _h, _how in pairs)
    evidence = sum(1 for key, n in totals.items() if n > 1 and 0 < held.get(key, 0) < n)
    if evidence < 2:
        return missing, []
    seen: set[tuple] = set()
    kept, dropped = [], []
    for row in missing:
        key = _key(row)
        if held.get(key, 0) > 0 or key in seen:
            dropped.append(row)
        else:
            seen.add(key)
            kept.append(row)
    return kept, dropped


# --- kinds and import -------------------------------------------------------------


def row_kind(acct: dict, row: MRow) -> str:
    liability = acct.get("type") == "liability"
    text = f"{row.statement} {row.merchant}"
    if not liability:
        return "deposit" if row.amount_cents > 0 else "withdrawal"
    if row.amount_cents > 0:
        if NOT_A_PAYMENT.search(text):
            return "credit"
        if row.category.lower() in PAYMENT_CATEGORIES or re.search(r"payment|pymt|pmt", text, re.I):
            return "payment"
        return "credit"
    if re.search(r"interest", text, re.I) or row.category.lower() == "interest":
        return "interest"
    if row.category.lower() == "financial fees":
        return "fee"
    return "purchase"


def payment_category(acct: dict) -> tuple[str, str]:
    return ("Transfers", "Loan payment") if acct.get("class") == "loan" else ("Transfers", "Credit card payment")


def ensure_payment_rules(conn, acct: dict, rows: list[MRow]) -> int:
    """An account-specific rule for each payment text the rule set does not already read as a transfer."""
    from hpgbooks.personal.actions import add_rule
    from hpgbooks.personal.classify import category_id, decide_personal, load_aliases, load_personal_rules, uncategorized_id

    group, name = payment_category(acct)
    wanted = category_id(conn, group, name)
    made = 0
    seen: set[str] = set()
    for row in rows:
        if row_kind(acct, row) != "payment" or row.statement in seen:
            continue
        seen.add(row.statement)
        rules = load_personal_rules(conn)
        txn = {"account_id": acct["id"], "amount_cents": row.amount_cents, "name": row.statement, "merchant_name": row.merchant, "description": ""}
        decision = decide_personal(txn, rules, load_aliases(conn), uncategorized_id(conn))
        kind = conn.execute("SELECT kind FROM p_categories WHERE id = ?", (decision["category_id"],)).fetchone() if decision.get("category_id") else None
        if kind and kind["kind"] == "transfer":
            continue
        add_rule(
            conn,
            {"pattern": re.escape(row.statement)[:190], "field": "name", "account_id": acct["id"], "amount_sign": "in", "category_id": wanted,
             "priority": 14, "confidence": 0.85, "note": "Monarch import: payment"},
            actor="import_monarch",
            apply=False,
        )
        made += 1
    return made


def unpair_unlikely_transfers(conn, txn_ids: list[str]) -> list[str]:
    """Reject a transfer pairing that grabbed a new row Monarch does not call a transfer or payment
    (a Daily Cash deposit pairing with a card purchase of the same size, say). Repeats, because
    rejecting one pair frees the row to pair with the next look-alike."""
    from hpgbooks.personal.classify import reclassify_personal, set_transfer_decision

    keys: list[str] = []
    for _round in range(8):
        found = _unlikely_pairs(conn, txn_ids)
        if not found:
            break
        for key in found:
            set_transfer_decision(conn, key, "rejected", actor="import_monarch")
        keys.extend(found)
        reclassify_personal(conn)
    return keys


def _unlikely_pairs(conn, txn_ids: list[str]) -> list[str]:
    from hpgbooks.personal.classify import TRANSFER_WORDS

    keys: list[str] = []
    for txn_id in txn_ids:
        row = conn.execute(
            """
            SELECT t.id, t.amount_cents, t.raw_json, p.transfer_pair, o.name AS other_name FROM transactions t
            JOIN p_classifications p ON p.txn_id = t.id AND p.source = 'transfer' AND p.transfer_pair IS NOT NULL AND p.transfer_pair NOT LIKE 'biz:%'
            LEFT JOIN transactions o ON o.id = p.transfer_pair WHERE t.id = ?
            """,
            (txn_id,),
        ).fetchone()
        if row is None:
            continue
        category = (json.loads(row["raw_json"] or "{}").get("category") or "").lower()
        if category in PAYMENT_CATEGORIES or TRANSFER_WORDS.search(row["other_name"] or ""):
            continue
        keys.append(f"{row['id']}|{row['transfer_pair']}" if int(row["amount_cents"]) < 0 else f"{row['transfer_pair']}|{row['id']}")
    return keys


# Account-specific rules for rows that carry no merchant text: (account name contains, pattern, sign, category, note)
ACCOUNT_RULES = [
    ("apple savings", r"^Deposit$", "in", ("Income", "Refunds & reimbursements"), "Apple Daily Cash deposit (cash back, nets against card spending)"),
    ("robinhood", r"^(buy|sell)\s", None, ("Transfers", "Internal transfer"), "brokerage trade: money stays inside the account, not spending"),
]


def ensure_account_rules(conn, acct: dict) -> int:
    from hpgbooks.personal.actions import add_rule
    from hpgbooks.personal.classify import category_id

    made = 0
    for needle, pattern, sign, (group, name), note in ACCOUNT_RULES:
        if needle.replace(" ", "") not in norm(acct.get("name")):
            continue
        if conn.execute("SELECT 1 FROM p_rules WHERE active = 1 AND pattern = ? AND account_id = ?", (pattern, acct["id"])).fetchone():
            continue
        cat = category_id(conn, group, name)
        if cat is None:
            continue
        add_rule(conn, {"pattern": pattern, "field": "name", "account_id": acct["id"], "amount_sign": sign, "category_id": cat, "priority": 20, "confidence": 0.85, "note": note},
                 actor="import_monarch", apply=False)
        made += 1
    return made


def import_missing(conn, acct: dict, rows: list[MRow], *, dry_run: bool = False) -> dict:
    """Insert Monarch rows we lack into one account and classify/pair them."""
    from hpgbooks.cfna import ambiguous_payment_pairs, credit_category, unpair_non_payments
    from hpgbooks.personal.classify import (
        after_personal_import,
        classify_new_personal,
        current,
        uncategorized_id,
        write_personal,
    )

    stats = {"inserted": 0, "unchanged": 0, "new_ids": [], "kinds": Counter(), "unpaired": [], "ambiguous": [], "rules": 0}
    ts = now_iso()
    fresh: list[MRow] = []
    for row in rows:
        if conn.execute("SELECT 1 FROM transactions WHERE id = ?", (row.txn_id,)).fetchone():
            stats["unchanged"] += 1
        else:
            fresh.append(row)
    if dry_run or not fresh:
        stats["inserted"] = len(fresh)
        stats["kinds"] = Counter(row_kind(acct, r) for r in fresh)
        return stats
    stats["rules"] = ensure_payment_rules(conn, acct, fresh) + ensure_account_rules(conn, acct)
    credit_ids: list[str] = []
    for row in fresh:
        kind = row_kind(acct, row)
        stats["kinds"][kind] += 1
        if kind == "credit":
            credit_ids.append(row.txn_id)
        raw = json.dumps({"monarch_id": row.mid, "category": row.category, "merchant": row.merchant, "owner": row.owner, "kind": kind}, sort_keys=True, separators=(",", ":"))
        conn.execute(
            """
            INSERT INTO transactions (
              id, account_id, date, amount_cents, direction, currency, name, merchant_name,
              description, pending, provider_category, raw_json, status, superseded_by,
              first_seen_at, last_seen_at, updated_at, source
            ) VALUES (?, ?, ?, ?, ?, 'USD', ?, ?, ?, 0, ?, ?, 'active', NULL, ?, ?, ?, ?)
            """,
            (row.txn_id, acct["id"], row.date, row.amount_cents, "in" if row.amount_cents > 0 else "out",
             row.statement if re.search(r"[A-Za-z0-9]", row.statement) else row.merchant, row.merchant, "Monarch export", row.category, raw, ts, ts, ts, SOURCE),
        )
        stats["inserted"] += 1
        stats["new_ids"].append(row.txn_id)
    classify_new_personal(conn, stats["new_ids"])
    fallback = uncategorized_id(conn)
    for txn_id in credit_ids:  # a refund goes where its purchase would
        held = current(conn, txn_id)
        if held and held["category_id"] == fallback:
            category_id, note = credit_category(conn, dict(conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()))
            if category_id is not None:
                write_personal(conn, txn_id, category_id, "manual", 1.0, note, overwrite_manual=True, actor="import_monarch", audit_write=True)
    after_personal_import(conn)
    stats["unpaired"] = unpair_non_payments(conn, acct["id"], SOURCE) + unpair_unlikely_transfers(conn, stats["new_ids"])
    stats["ambiguous"] = ambiguous_payment_pairs(conn, acct["id"], SOURCE)
    conn.execute(
        """
        INSERT INTO import_log (ts, file, account_id, date_from, date_to, rows_in, inserted, updated, unchanged, superseded, skipped)
        VALUES (?, 'monarch export', ?, ?, ?, ?, ?, 0, ?, 0, 0)
        """,
        (now_iso(), acct["id"], min(r.date for r in rows), max(r.date for r in rows), len(rows), stats["inserted"], stats["unchanged"]),
    )
    audit(conn, "monarch_import", field=acct["id"], new_value=f"inserted={stats['inserted']}", actor="import_monarch")
    return stats


# --- reconciliation -----------------------------------------------------------


def reconcile(conn, rows: list[MRow], *, since: str = SINCE, recent_days: int = 3, today: str | None = None, overrides: dict[str, str] | None = None) -> list[dict]:
    """Per Monarch account: what we hold, what is missing, and what to do with it."""
    today = today or date.today().isoformat()
    cutoff = (date.fromisoformat(today) - timedelta(days=recent_days)).isoformat()
    rows = [r for r in rows if r.date >= since]
    names = sorted({r.account for r in rows}, key=lambda n: -sum(1 for r in rows if r.account == n))
    accounts = resolve_accounts(conn, names, overrides)
    report = []
    for name in names:
        mine = [r for r in rows if r.account == name]
        acct = accounts[name]
        item = {
            "monarch": name, "account": acct, "action": "import", "monarch_n": len(mine),
            "monarch_cents": sum(r.amount_cents for r in mine), "ours_n": 0, "ours_cents": 0,
            "missing": [], "pairs": [], "left_over": [], "skipped_recent": [], "monarch_dups": [],
        }
        if acct is None:
            item["action"] = "no matching account"
        elif acct["scope"] != "personal":
            item["action"] = f"out of scope ({acct['scope']})"
        else:
            existing = [e for e in load_existing(conn, acct["id"], since) if e["date"] >= since]
            item["ours_n"], item["ours_cents"] = len(existing), sum(e["amount_cents"] for e in existing)
            result = match_rows(load_existing(conn, acct["id"], since), mine)
            item.update({"pairs": result["pairs"], "left_over": [h for h in result["left_over"] if h["date"] >= since]})
            missing, item["monarch_dups"] = collapse_monarch_duplicates(mine, result["pairs"], result["missing"])
            if authoritative(conn, acct["id"]):
                item["action"] = "compare only (authoritative source)"
            elif acct.get("sync_enabled"):
                item["skipped_recent"] = [r for r in missing if r.date > cutoff]
                missing = [r for r in missing if r.date <= cutoff]
            item["missing"] = missing
        report.append(item)
    return report


def apply_report(conn, report: list[dict], *, dry_run: bool = False) -> dict[str, dict]:
    out = {}
    for item in report:
        if item["action"] != "import" or not item["missing"]:
            continue
        out[item["monarch"]] = import_missing(conn, item["account"], item["missing"], dry_run=dry_run)
    return out


# --- category hints -------------------------------------------------------------

GENERIC_KEYS = {"PAYMENT", "TRANSFER", "DEPOSIT", "WITHDRAWAL", "CHECK", "ZELLE", "(UNKNOWN)", "INTEREST", "FEE", "POINT OF SALE", "PURCHASE", "DEBIT"}


def hint_category(row: MRow, acct: dict | None) -> tuple[str, str] | None:
    """Our category for a Monarch row, or None when Monarch's label is not a clear spending/income category."""
    cat = row.category.strip().lower()
    if cat == "interest":
        return ("Fees & interest", "Fees & interest") if acct and acct.get("type") == "liability" and row.amount_cents < 0 else ("Income", "Interest & dividends")
    if row.amount_cents > 0 and cat not in ("paychecks", "payroll", "dividends & capital gains"):
        return None  # credits, refunds and deposits are too ambiguous to guess
    return CATEGORY_MAP.get(cat)


def suggest_rules(conn, rows: list[MRow], *, min_share: float = 0.85, min_rows: int = 2) -> dict:
    """Rules for review-queue merchants whose Monarch category is clear.

    A merchant qualifies when the review rows' Monarch matches all agree on one of our
    categories, and the same merchant's wider Monarch history (personal rows, any year)
    agrees at least min_share of the time over at least min_rows rows. Returns
    {"rules": [...], "skipped": [(merchant_key, reason)]}.
    """
    from hpgbooks.personal.classify import category_id, load_aliases
    from hpgbooks.personal.merchants import merchant_key
    from hpgbooks.personal.queries import review_queue

    queue = review_queue(conn)
    accounts = resolve_accounts(conn, sorted({r.account for r in rows}))
    ours_to_monarch: dict[str, list[str]] = defaultdict(list)
    for name, acct in accounts.items():
        if acct is not None and acct["scope"] == "personal":
            ours_to_monarch[acct["id"]].append(name)
    # history: merchant key -> Counter(our category)
    history: dict[str, Counter] = defaultdict(Counter)
    by_merchant: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r.entity:
            continue
        acct = accounts.get(r.account)
        if acct is None or acct["scope"] != "personal":
            continue
        hint = hint_category(r, acct)
        if hint:
            history[merchant_key(r.merchant, r.statement)][hint] += 1
            history[merchant_key(None, r.statement)][hint] += 1
            if r.merchant:
                by_merchant[r.merchant.lower()][hint] += 1
    # match each review row to its Monarch row
    hits: dict[str, MRow] = {}
    for acct_id, names in ours_to_monarch.items():
        wanted = [q for q in queue if q["account_id"] == acct_id]
        if not wanted:
            continue
        mine = [r for r in rows if r.account in names and r.date >= SINCE]
        result = match_rows(load_existing(conn, acct_id), mine)
        for row, held, _how in result["pairs"]:
            hits[held["id"]] = row
    aliases = load_aliases(conn)
    groups: dict[str, list[tuple[dict, MRow]]] = defaultdict(list)
    for item in queue:
        row = hits.get(item["id"])
        if row is not None and item["source"] != "manual" and not item.get("transfer_pair"):
            key = item["merchant_key"]
            head = key.split('"')[0].strip(" -")  # 'PRO TOUR "MAY BONUS"' -> 'PRO TOUR'
            groups[head if '"' in key and len(head) >= 6 else key].append((item, row))
    existing = {r["pattern"].lower() for r in conn.execute("SELECT pattern FROM p_rules WHERE active = 1")}
    rules, skipped = [], []
    accepted: list[re.Pattern] = []
    for key, members in sorted(groups.items(), key=lambda kv: (len(kv[0]), kv[0])):
        if key in GENERIC_KEYS or len(key) < 4:
            skipped.append((key, "generic merchant text"))
            continue
        acct_of = {m[0]["account_id"]: accounts.get(m[1].account) for m in members}
        hints = {hint_category(row, acct_of[item["account_id"]]) for item, row in members}
        if len(hints) != 1 or None in hints:
            skipped.append((key, "Monarch categories disagree or are not clear"))
            continue
        hint = next(iter(hints))
        evidence = Counter(history.get(key, Counter()))
        for monarch_name in {row.merchant.lower() for _, row in members if row.merchant}:
            if sum(by_merchant[monarch_name].values()) > sum(evidence.values()):
                evidence = Counter(by_merchant[monarch_name])
        total = sum(evidence.values())
        top = evidence.get(hint, 0)
        if " " not in key and not (len(key) >= 8 or (len(key) >= 7 and top == total and total >= 5)):
            skipped.append((key, "single short word"))
            continue
        if total < min_rows or top / total < min_share:
            skipped.append((key, f"history too thin or mixed ({top} of {total})"))
            continue
        if aliases.get(key, {}).get("default_category_id"):
            skipped.append((key, "merchant already has a default category"))
            continue
        cat_id = category_id(conn, *hint)
        if cat_id is None:
            skipped.append((key, f"no category {hint}"))
            continue
        pattern = r"\s+".join(re.escape(part) for part in key.split())
        pattern = (r"\b" if key[0].isalnum() else "") + pattern + (r"\b" if key[-1].isalnum() else "")

        if pattern.lower() in existing:
            skipped.append((key, "rule already exists"))
            continue
        if not all(re.search(pattern, f"{item['name']} {item['merchant']}", re.I) for item, _ in members):
            skipped.append((key, "key not found in the raw text"))
            continue
        if any(rx.search(key) for rx in accepted):
            skipped.append((key, "covered by a shorter rule"))
            continue
        accepted.append(re.compile(pattern, re.I))
        signs = {item["amount_cents"] > 0 for item, _ in members}
        rules.append(
            {
                "pattern": pattern, "category": hint, "category_id": cat_id, "amount_sign": ("in" if signs == {True} else "out" if signs == {False} else None),
                "rows": len(members), "cents": sum(abs(item["amount_cents"]) for item, _ in members),
                "monarch": sorted({row.category for _, row in members}), "history": f"{top} of {total}", "key": key,
            }
        )
    return {"rules": rules, "skipped": skipped, "queue": len(queue)}


def apply_rules(conn, suggestions: list[dict]) -> dict:
    """Create the rules (after every starter and manual rule) and reclassify. Returns what moved."""
    from hpgbooks.personal.actions import add_rule
    from hpgbooks.personal.classify import reclassify_personal

    before = {r["txn_id"]: r["category_id"] for r in conn.execute("SELECT txn_id, category_id FROM p_classifications")}
    uncategorized = conn.execute("SELECT id FROM p_categories WHERE group_name = 'Uncategorized'").fetchone()[0]
    for item in suggestions:
        add_rule(
            conn,
            {"pattern": item["pattern"], "field": "any", "amount_sign": item["amount_sign"], "category_id": item["category_id"], "priority": 60,
             "confidence": 0.8, "note": f"Monarch hint: {', '.join(item['monarch'])}"},
            actor="import_monarch",
            apply=False,
        )
    changed = reclassify_personal(conn)
    after = {r["txn_id"]: r["category_id"] for r in conn.execute("SELECT txn_id, category_id FROM p_classifications")}
    moved = [t for t, c in after.items() if t in before and before[t] != c]
    unexpected = [t for t in moved if before[t] != uncategorized]
    return {"reclassified": changed, "moved": len(moved), "unexpected": unexpected}
