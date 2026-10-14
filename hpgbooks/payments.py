"""Minimum payment due, due date, and APR for credit cards and loans.

One row per account in `account_payment_terms`. Each row says where it came from
(`source`) and the date it describes (`as_of`). Higher-trust sources are never
replaced by lower-trust ones unless the old billing cycle is over:

    manual > statement > issuer site / finance feed > sheet > inferred

Paying is read from the books: a payment is a credit on the card or loan account
that is classified as a transfer (Credit card payment / Loan payment), or, for
loans paid from a bank account, a debit matching the row's `payer_pattern`.
When a due date has passed and a payment was made, the row rolls to the same day
next month (flagged estimated); with no payment it is overdue. Nothing here
writes to the ledger, balance anchors, or classifications.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from statistics import median_low

from hpgbooks.config import get_config
from hpgbooks.db import HpgbooksError, audit, now_iso, to_cents

SOURCE_RANK = {"manual": 6, "statement": 5, "issuer site": 4, "finance": 4, "sheet": 3, "inferred": 2}
AUTOPAY = ("yes", "no", "unknown")
DUE_SOON_DAYS = 7
ESTIMATED_GRACE_DAYS = 3  # an inferred date is a guess: do not shout "overdue" the day after
INFERRED_SOURCE = "inferred from payment history"
FIELDS = ("min_payment_cents", "due_date", "apr", "autopay", "statement_balance_cents", "payer_pattern", "notes")
TERM_COLUMNS = (
    "account_id", "min_payment_cents", "due_date", "apr", "source", "as_of", "autopay", "estimated",
    "unverified", "statement_balance_cents", "paid_on", "payer_pattern", "notes", "updated_at",
)


# --- small helpers -------------------------------------------------------------


def today_iso() -> str:
    return date.today().isoformat()


def parse_day(value: str | None, what: str = "date") -> str | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError as exc:
        raise HpgbooksError(f"{what} must be YYYY-MM-DD") from exc


def add_months(day: date, months: int) -> date:
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def rank(source: str | None) -> int:
    text = (source or "").lower()
    for key, value in SOURCE_RANK.items():
        if key in text:
            return value
    return 2


def clean_source(source: str | None) -> str:
    text = re.sub(r"\s+", " ", (source or "").strip())
    if not text:
        raise HpgbooksError("source is required")
    return text[:60]


def clean_apr(value) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value).strip())
    if not text:
        return None
    if len(text) > 80:
        raise HpgbooksError("apr is too long")
    if re.fullmatch(r"\d+(\.\d+)?", text):
        text += "%"
    return text


def apr_number(text: str | None) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", text or "")
    return float(match.group(0)) if match else None


def clean_autopay(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value).strip().lower()
    mapping = {"y": "yes", "true": "yes", "1": "yes", "on": "yes", "enrolled": "yes", "n": "no", "false": "no", "0": "no", "off": "no",
               "not enrolled": "no", "": None, "?": "unknown"}
    text = mapping.get(text, text)
    if text is not None and text not in AUTOPAY:
        raise HpgbooksError("autopay must be yes, no, or unknown")
    return text


def cents_arg(value, what: str = "amount") -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    cents = to_cents(str(value).replace("$", "").replace(",", ""))
    if cents < 0 or cents > 10**11:
        raise HpgbooksError(f"{what} must be between 0 and 1,000,000,000")
    return cents


# --- storage ---------------------------------------------------------------------


def has_table(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'account_payment_terms'").fetchone() is not None


def get_terms(conn, account_id: str) -> dict | None:
    if not has_table(conn):
        return None
    row = conn.execute("SELECT * FROM account_payment_terms WHERE account_id = ?", (account_id,)).fetchone()
    return dict(row) if row is not None else None


def _store(conn, account_id: str, values: dict) -> None:
    cols = [c for c in TERM_COLUMNS if c != "account_id"]
    conn.execute(
        f"""
        INSERT INTO account_payment_terms (account_id, {', '.join(cols)})
        VALUES (?, {', '.join('?' for _ in cols)})
        ON CONFLICT(account_id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols)}
        """,
        (account_id, *[values.get(c) for c in cols]),
    )


def normalize(new: dict) -> dict:
    """Validated copy of a terms dict (cents, ISO dates, clean text)."""
    out: dict = {}
    if "min_payment_cents" in new:
        out["min_payment_cents"] = None if new["min_payment_cents"] is None else int(new["min_payment_cents"])
    if "due_date" in new:
        out["due_date"] = parse_day(new["due_date"], "due date")
    if "apr" in new:
        out["apr"] = clean_apr(new["apr"])
    if "autopay" in new:
        out["autopay"] = clean_autopay(new["autopay"])
    if "statement_balance_cents" in new:
        out["statement_balance_cents"] = None if new["statement_balance_cents"] is None else int(new["statement_balance_cents"])
    if "payer_pattern" in new:
        pattern = (new["payer_pattern"] or "").strip() or None
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise HpgbooksError("payer pattern is not a valid regular expression") from exc
        out["payer_pattern"] = pattern
    if "notes" in new:
        out["notes"] = (new["notes"] or "").strip()[:500] or None
    for flag in ("estimated", "unverified"):
        if flag in new:
            out[flag] = 1 if new[flag] else 0
    if "paid_on" in new:
        out["paid_on"] = parse_day(new["paid_on"], "paid date")
    return out


def merge_terms(conn, account_id: str, new: dict, *, source: str, as_of: str | None = None, actor: str = "cli", force: bool = False) -> dict:
    """Apply `new` to the account's row under the trust rules. Returns what happened.

    {"action": "created"|"updated"|"kept"|"unchanged", "changed": [fields], "conflicts": [text], "filled": [fields]}

    A new value wins when its source ranks at least as high and its as_of is not older.
    A lower-ranked source (the sheet over a statement) wins only when it is newer AND the
    old due date had already passed on that date, so a stale sheet never beats a current
    statement. Manual entries are replaced only by manual entries (or force). Fields the
    winner does not mention are kept; fields the loser fills are only blanks.
    """
    source = clean_source(source)
    as_of = parse_day(as_of) or today_iso()
    new = normalize(new)
    old = get_terms(conn, account_id)
    result = {"action": "created", "changed": [], "conflicts": [], "filled": [], "source": source, "as_of": as_of}
    if old is None:
        values = {c: None for c in TERM_COLUMNS}
        values.update({"autopay": "unknown", "estimated": 0, "unverified": 0})
        values.update(new)
        values.update({"source": source, "as_of": as_of, "updated_at": now_iso()})
        _store(conn, account_id, values)
        result["changed"] = sorted(new)
        audit(conn, "payment_terms_set", field=account_id, new_value=_summary(values), actor=actor, note=source)
        return result

    old_rank, new_rank = rank(old["source"]), rank(source)
    old_as_of = old["as_of"] or "0000-00-00"
    if force or (new_rank >= old_rank and as_of >= old_as_of):
        wins = True
    elif new_rank < old_rank and old_rank < SOURCE_RANK["manual"] and as_of > old_as_of and old["due_date"] and old["due_date"] < as_of:
        wins = True
    else:
        wins = False
    values = dict(old)
    for field, value in new.items():
        before = old.get(field)
        if value is None and not wins:
            continue
        if value == before:
            continue
        if field in ("estimated", "unverified", "paid_on"):
            if wins:
                values[field] = value
                result["changed"].append(field)
            continue
        if wins:
            if field == "apr" and before is not None and value is not None and not _differs("apr", before, value):
                continue  # same rate, keep the fuller text
            if value is None and before is not None and field not in ("notes",):
                # winner says "unknown": keep what we had rather than blank a known value
                if field not in ("min_payment_cents", "due_date"):
                    continue
            values[field] = value
            result["changed"].append(field)
            if before is not None and field in ("min_payment_cents", "due_date", "apr") and _differs(field, before, value):
                result["conflicts"].append(f"{field}: {_show(field, before)} ({old['source']}, {old['as_of']}) -> {_show(field, value)} ({source}, {as_of})")
        elif before is None or (field == "autopay" and before == "unknown"):
            values[field] = value
            result["filled"].append(field)
        elif field in ("min_payment_cents", "due_date", "apr") and _differs(field, before, value):
            result["conflicts"].append(f"{field}: kept {_show(field, before)} ({old['source']}, {old['as_of']}); {source} ({as_of}) says {_show(field, value)}")
    if result["changed"]:
        values["source"], values["as_of"] = source, as_of
        # a fresh due date starts a new cycle: a recorded manual 'paid' no longer applies
        if "due_date" in result["changed"] and "paid_on" not in new:
            values["paid_on"] = None
        if wins and not ({"estimated"} & set(new)):
            values["estimated"] = 0
        if wins and "unverified" not in new:
            values["unverified"] = 0
        values["updated_at"] = now_iso()
        _store(conn, account_id, values)
        result["action"] = "updated"
        audit(conn, "payment_terms_set", field=account_id, old_value=_summary(old), new_value=_summary(values), actor=actor, note=source)
    elif result["filled"]:
        values["updated_at"] = now_iso()
        _store(conn, account_id, values)
        result["action"] = "updated"
        audit(conn, "payment_terms_fill", field=account_id, old_value=_summary(old), new_value=_summary(values), actor=actor, note=source)
    else:
        result["action"] = "kept" if result["conflicts"] else "unchanged"
    return result


def _differs(field: str, a, b) -> bool:
    if field == "apr":
        x, y = apr_number(a), apr_number(b)
        return x != y
    return a != b


def _show(field: str, value) -> str:
    if field == "min_payment_cents":
        return f"${int(value) / 100:,.2f}"
    return str(value)


def _summary(values: dict) -> str:
    bits = []
    if values.get("min_payment_cents") is not None:
        bits.append(f"min {values['min_payment_cents'] / 100:.2f}")
    if values.get("due_date"):
        bits.append(f"due {values['due_date']}")
    if values.get("apr"):
        bits.append(f"apr {values['apr']}")
    bits.append(f"src {values.get('source')}")
    return ", ".join(bits)


def set_manual(conn, account_id: str, fields: dict, *, as_of: str | None = None, source: str = "manual", actor: str = "cli") -> dict:
    """The owner's own entry (CLI or the Accounts page). Replaces anything; blank clears."""
    account = conn.execute("SELECT id, class, type, scope FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if account is None:
        raise HpgbooksError("no such account")
    if account["class"] not in ("liability", "loan") and account["type"] != "liability":
        raise HpgbooksError("payment terms are for credit cards and loans")
    if account["scope"] not in ("business", "personal"):
        raise HpgbooksError("payment terms are for business and personal accounts")
    fields = dict(fields)
    if "estimated" not in fields and any(k in fields for k in ("due_date", "min_payment_cents")):
        fields["estimated"] = 0
    return merge_terms(conn, account_id, fields, source=source, as_of=as_of, actor=actor, force=rank(source) >= SOURCE_RANK["manual"])


def mark_paid(conn, account_id: str, paid_on: str | None = None, *, actor: str = "cli") -> dict:
    paid_on = parse_day(paid_on) or today_iso()
    terms = get_terms(conn, account_id)
    if terms is None:
        raise HpgbooksError("no payment terms recorded for this account")
    conn.execute("UPDATE account_payment_terms SET paid_on = ?, updated_at = ? WHERE account_id = ?", (paid_on, now_iso(), account_id))
    audit(conn, "payment_marked_paid", field=account_id, new_value=paid_on, actor=actor)
    return {"paid_on": paid_on}


# --- finding payments --------------------------------------------------------------


def _balance_paid(terms: dict, owed_cents: int | None, need: int) -> bool:
    sb = terms.get("statement_balance_cents")
    return owed_cents is not None and sb is not None and need > 1 and owed_cents <= sb - need


def find_payments(conn, account_id: str, terms: dict | None, start: str, end: str) -> list[dict]:
    """Payments toward the account dated start..end: [{id, date, cents, name}] oldest first."""
    rows: dict[str, dict] = {}
    for r in conn.execute(
        """
        SELECT t.id, t.date, t.amount_cents, t.name,
               pc.transfer_pair AS pair, pcat.group_name AS grp, bc.business_tag AS btag
        FROM transactions t
        LEFT JOIN p_classifications pc ON pc.txn_id = t.id
        LEFT JOIN p_categories pcat ON pcat.id = pc.category_id
        LEFT JOIN classifications bc ON bc.txn_id = t.id
        WHERE t.account_id = ? AND t.status = 'active' AND t.amount_cents > 0 AND t.date >= ? AND t.date <= ?
        """,
        (account_id, start, end),
    ):
        looks_like_payment = bool(re.search(r"payment|pymt|pmt|autopay|applied to", r["name"] or "", re.I))
        if r["grp"] == "Transfers" or r["btag"] == "transfer" or r["pair"] or looks_like_payment:
            rows[r["id"]] = {"id": r["id"], "date": r["date"], "cents": int(r["amount_cents"]), "name": r["name"] or ""}
    pattern = (terms or {}).get("payer_pattern")
    if pattern:
        rx = re.compile(pattern, re.I)
        own = {r[0] for r in conn.execute("SELECT id FROM transactions WHERE account_id = ?", (account_id,))}
        for r in conn.execute(
            """
            SELECT t.id, t.date, t.amount_cents, t.name, pc.transfer_pair AS pair
            FROM transactions t LEFT JOIN p_classifications pc ON pc.txn_id = t.id
            WHERE t.status = 'active' AND t.amount_cents < 0 AND t.date >= ? AND t.date <= ?
            """,
            (start, end),
        ):
            if not rx.search(r["name"] or ""):
                continue
            if r["pair"] and r["pair"] in own:
                continue  # the loan's own credit already counts this payment (even outside this window)
            rows[r["id"]] = {"id": r["id"], "date": r["date"], "cents": -int(r["amount_cents"]), "name": r["name"] or ""}
    return sorted(rows.values(), key=lambda p: (p["date"], p["id"]))


def evaluate(conn, account_id: str, terms: dict | None, today: str | None = None, owed_cents: int | None = None) -> dict:
    """Where this account stands today: status, effective due date, whether it rolled.

    `owed_cents` (what the books say is owed now) adds one more way to see a payment for a
    statement-backed card whose payments only arrive with the next statement: the balance
    has fallen by at least the minimum since the statement balance.
    """
    today_d = date.fromisoformat(today or today_iso())
    out = {
        "status": "unknown", "effective_due_date": None, "days_until": None, "rolled": False, "paid_date": None,
        "estimated": False, "min_payment_cents": None, "note": "", "paid_by": None,
    }
    if terms is None:
        return out
    min_cents = terms["min_payment_cents"]
    out["min_payment_cents"] = min_cents
    out["estimated"] = bool(terms["estimated"])
    if min_cents == 0:
        out.update(status="none", note="No payment due")
        return out
    if not terms["due_date"]:
        out["note"] = "No due date recorded"
        return out
    cursor = date.fromisoformat(terms["due_date"])
    need = min_cents if min_cents else 1
    used: set[str] = set()
    manual_paid = date.fromisoformat(terms["paid_on"]) if terms.get("paid_on") else None
    # Payments up to the date the figures were taken belong to the cycle before, unless the
    # snapshot was already past its own due date (a stale sheet row).
    as_of = date.fromisoformat(terms["as_of"]) if terms.get("as_of") else None
    floor = as_of + timedelta(days=1) if as_of and as_of < cursor else None
    for _ in range(24):
        if cursor >= today_d:
            break
        prev, nxt = add_months(cursor, -1), add_months(cursor, 1)
        start = max(prev + timedelta(days=1), floor) if floor and not out["rolled"] else prev + timedelta(days=1)
        window = [p for p in find_payments(conn, account_id, terms, start.isoformat(), min(today_d, nxt - timedelta(days=1)).isoformat()) if p["id"] not in used]
        total = sum(p["cents"] for p in window)
        balance_paid = _balance_paid(terms, owed_cents, need) and not out["rolled"]
        if manual_paid is not None and prev < manual_paid <= today_d:
            out["paid_date"] = manual_paid.isoformat()
            manual_paid = None
        elif total >= need:
            used.update(p["id"] for p in window)
            out["paid_date"] = window[-1]["date"]
            out["paid_by"] = "payment"
        elif balance_paid:
            out["paid_by"] = "balance"
        else:
            break
        cursor, out["rolled"], out["estimated"] = nxt, True, True
    out["effective_due_date"] = cursor.isoformat()
    out["days_until"] = (cursor - today_d).days
    if cursor < today_d:
        late = (today_d - cursor).days
        guessed = bool(terms["estimated"]) or out["rolled"]
        out["status"] = "due_soon" if guessed and late <= ESTIMATED_GRACE_DAYS else "overdue"
        out["note"] = f"{late} day{'s' if late != 1 else ''} past due"
    else:
        prev = add_months(cursor, -1)
        start = prev + timedelta(days=1)
        if floor and not out["rolled"]:
            start = max(start, floor)
        early = [p for p in find_payments(conn, account_id, terms, start.isoformat(), today_d.isoformat()) if p["id"] not in used]
        if manual_paid is not None and prev < manual_paid <= today_d:
            out["status"], out["paid_date"], out["paid_by"] = "paid", manual_paid.isoformat(), "manual"
        elif early and sum(p["cents"] for p in early) >= need:
            out["status"], out["paid_date"], out["paid_by"] = "paid", early[-1]["date"], "payment"
        elif not out["rolled"] and _balance_paid(terms, owed_cents, need):
            out["status"], out["paid_by"] = "paid", "balance"
        else:
            out["status"] = "due_soon" if out["days_until"] <= DUE_SOON_DAYS else "upcoming"
    return out


# --- the page ------------------------------------------------------------------------


def balances_owed(conn, mode: str) -> dict[str, int]:
    """Amount owed per account (positive), the same figure the Accounts pages show."""
    if mode == "personal":
        from hpgbooks.personal.analytics import today as p_today
        from hpgbooks.personal.balances import load_series
        from hpgbooks.personal.classify import personal_accounts

        accounts = personal_accounts(conn)
        series = load_series(conn, accounts)
        now = p_today().isoformat()
        return {aid: int(series[aid].balance_at(now)) for aid in accounts}
    from hpgbooks.balances import account_snapshots

    out = {}
    for snap in account_snapshots(conn, "business"):
        shown = snap.get("display_cents")
        out[snap["id"]] = int(snap["balance_cents"] if shown is None else shown)
    return out


def build(conn, mode: str, today: str | None = None) -> dict:
    from hpgbooks.scope import accounts_in, label, parse_mode

    mode = parse_mode(mode)
    today = today or today_iso()
    today_d = date.fromisoformat(today)
    owed = balances_owed(conn, mode)
    terms_all = {r["account_id"]: dict(r) for r in conn.execute("SELECT * FROM account_payment_terms")} if has_table(conn) else {}
    rows = []
    for acct in accounts_in(conn, mode):
        if acct["class"] not in ("liability", "loan") and acct["type"] != "liability":
            continue
        terms = terms_all.get(acct["id"])
        state = evaluate(conn, acct["id"], terms, today, owed.get(acct["id"]))
        rows.append(
            {
                "id": acct["id"], "label": label(acct), "last4": acct.get("last4") or "", "institution": acct.get("institution") or "",
                "class": acct["class"], "balance_cents": owed.get(acct["id"], 0), "has_terms": terms is not None,
                "due_date": terms["due_date"] if terms else None, "apr": terms["apr"] if terms else None,
                "autopay": terms["autopay"] if terms else "unknown", "source": terms["source"] if terms else None,
                "as_of": terms["as_of"] if terms else None, "unverified": bool(terms and terms["unverified"]),
                "notes": terms["notes"] if terms else None, "payer_pattern": terms["payer_pattern"] if terms else None,
                "statement_balance_cents": terms["statement_balance_cents"] if terms else None,
                "stored_min_cents": terms["min_payment_cents"] if terms else None,
                **{k: state[k] for k in ("status", "effective_due_date", "days_until", "rolled", "paid_date", "paid_by", "estimated", "min_payment_cents", "note")},
            }
        )
    rows.sort(key=lambda r: (r["effective_due_date"] is None, r["effective_due_date"] or "", r["label"].lower()))
    horizon = (today_d + timedelta(days=30)).isoformat()
    counted = [r for r in rows if r["status"] in ("overdue", "due_soon", "upcoming") and r["min_payment_cents"]]
    soon = [r for r in counted if r["status"] in ("due_soon", "upcoming") and r["effective_due_date"] <= horizon]
    by_date: dict[str, dict] = {}
    for r in soon:
        slot = by_date.setdefault(r["effective_due_date"], {"date": r["effective_due_date"], "cents": 0, "count": 0})
        slot["cents"] += r["min_payment_cents"]
        slot["count"] += 1
    overdue = [r for r in counted if r["status"] == "overdue"]
    summary = {
        "today": today, "horizon": horizon,
        "next30_cents": sum(r["min_payment_cents"] for r in soon), "next30_count": len(soon),
        "overdue_cents": sum(r["min_payment_cents"] for r in overdue), "overdue_count": len(overdue),
        "by_date": [by_date[k] for k in sorted(by_date)],
        "unverified_count": sum(1 for r in rows if r["unverified"]),
        "missing_count": sum(1 for r in rows if not r["has_terms"] and r["balance_cents"]),
    }
    return {"mode": mode, "rows": rows, "summary": summary}


# --- learning from our own books -----------------------------------------------------

APPLIED_TO = re.compile(r"applied to ([A-Za-z]{3})-(\d{2})-(\d{2})", re.I)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def infer_schedule(payments: list[dict], *, today: str, min_payments: int = 3, recent: int = 4) -> dict | None:
    """Typical amount and next expected date from the last few payments, or None.

    Amount is the median of the last `recent` payments; the day is their median day of the
    month. A servicer's "Payment Applied to OCT-01-26" label gives the exact due date.
    """
    today_d = date.fromisoformat(today)
    pays = sorted(payments, key=lambda p: p["date"])
    pays = [p for p in pays if date.fromisoformat(p["date"]) >= today_d - timedelta(days=200)]
    if len(pays) < min_payments:
        return None
    last = pays[-recent:]
    amount = int(median_low(sorted(p["cents"] for p in last)))
    labelled = None
    for p in reversed(pays):
        m = APPLIED_TO.search(p.get("name") or "")
        if m and m.group(1).lower() in _MONTHS:
            labelled = date(2000 + int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
            break
    if labelled is not None:
        nxt = add_months(labelled, 1)
    else:
        day = int(median_low(sorted(date.fromisoformat(p["date"]).day for p in last)))
        end = date.fromisoformat(pays[-1]["date"])
        nxt = date(end.year, end.month, min(day, calendar.monthrange(end.year, end.month)[1]))
        if nxt <= end:
            following = add_months(date(end.year, end.month, 1), 1)
            nxt = date(following.year, following.month, min(day, calendar.monthrange(following.year, following.month)[1]))
    return {"min_payment_cents": amount, "due_date": nxt.isoformat(), "count": len(last), "last_paid": pays[-1]["date"]}


def infer_loans(conn, *, today: str | None = None, dry_run: bool = False, actor: str = "cli") -> list[dict]:
    """Fill loan terms from the payments we can see. Never replaces a higher-trust entry."""
    from hpgbooks.scope import all_accounts, label

    today = today or today_iso()
    out = []
    for acct in all_accounts(conn):
        if acct["scope"] != "personal" or acct["class"] not in ("loan", "liability"):
            continue
        terms = get_terms(conn, acct["id"])
        pattern = (terms or {}).get("payer_pattern")
        if not pattern:
            for key, hint in ((h.match, h.pattern) for h in get_config().payer_hints):
                if key == acct.get("last4") or (not key.isdigit() and key.lower() in (acct.get("name") or "").lower()):
                    pattern = hint
                    break
        if acct["class"] != "loan":  # cards only learn where their bank-side payments are
            if pattern and terms and not terms.get("payer_pattern") and not dry_run:
                merge_terms(conn, acct["id"], {"payer_pattern": pattern}, source=INFERRED_SOURCE, as_of=terms["as_of"], actor=actor)
            continue
        probe = dict(terms or {}, payer_pattern=pattern)
        since = (date.fromisoformat(today) - timedelta(days=210)).isoformat()
        pays = find_payments(conn, acct["id"], probe, since, today)
        schedule = infer_schedule(pays, today=today)
        item = {"id": acct["id"], "label": label(acct), "payments_seen": len(pays), "schedule": schedule, "action": "no payment history"}
        if schedule is not None:
            fields = {"min_payment_cents": schedule["min_payment_cents"], "due_date": schedule["due_date"], "estimated": 1}
            if pattern and not (terms or {}).get("payer_pattern"):
                fields["payer_pattern"] = pattern
            if dry_run:
                item["action"] = "would set"
            else:
                res = merge_terms(conn, acct["id"], fields, source=INFERRED_SOURCE, as_of=schedule["last_paid"], actor=actor)
                item["action"] = res["action"]
                item["conflicts"] = res["conflicts"]
        out.append(item)
    return out


# --- statements and feeds --------------------------------------------------------------


def ingest_statement(conn, account_id: str, info: dict, *, source: str = "statement", actor: str = "import") -> dict | None:
    """Store minimum, due date, and APR from a parsed statement (info from cfna/applecard)."""
    if not info or not info.get("due_date"):
        return None
    fields = {"min_payment_cents": info.get("min_payment_cents"), "due_date": info["due_date"], "estimated": 0}
    if info.get("apr"):
        fields["apr"] = info["apr"]
    if info.get("statement_balance_cents") is not None:
        fields["statement_balance_cents"] = info["statement_balance_cents"]
    return merge_terms(conn, account_id, fields, source=source, as_of=info.get("as_of"), actor=actor)


def ingest_finance_items(conn, items: list[dict], *, as_of: str, actor: str = "accounts discover") -> list[dict]:
    """Take next_payment_due_date / minimum_payment_amount / interest_rate_percentage from
    finance_list_accounts rows when the provider sends them (most are null today)."""
    from hpgbooks.scope import get_account

    done = []
    for raw in items:
        account_id = str(raw.get("id") or "")
        acct = get_account(conn, account_id)
        if acct is None or acct["class"] not in ("liability", "loan"):
            continue
        due = (raw.get("next_payment_due_date") or "").strip() or None
        minimum = raw.get("minimum_payment_amount")
        rate = raw.get("interest_rate_percentage")
        if not due and minimum in (None, "") and rate in (None, ""):
            continue
        fields: dict = {}
        if due:
            fields["due_date"] = due[:10]
        if minimum not in (None, ""):
            fields["min_payment_cents"] = cents_arg(minimum)
        if rate not in (None, ""):
            fields["apr"] = clean_apr(rate)
        fields["estimated"] = 0
        done.append({"id": account_id, **merge_terms(conn, account_id, fields, source="finance", as_of=as_of, actor=actor)})
    return done


def ingest_latest_statement(conn, account_id: str, statements, *, dry_run: bool = False) -> str:
    """Store the newest statement's minimum, due date, and APR. Returns a one-line note."""
    withinfo = [s for s in statements if getattr(s, "payment", None)]
    if not withinfo:
        return "payment terms: no statement with a payment box"
    latest = max(withinfo, key=lambda s: s.payment.get("as_of") or "")
    info = latest.payment
    if dry_run:
        return f"payment terms: would store min {info.get('min_payment_cents', 0) / 100:.2f} due {info['due_date']} from the {info.get('as_of')} statement"
    result = ingest_statement(conn, account_id, info, source="statement", actor="import")
    return f"payment terms: {result['action']} (min {info.get('min_payment_cents', 0) / 100:.2f}, due {info['due_date']}, as of {info.get('as_of')})"
