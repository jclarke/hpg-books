"""Saved personal loan offers for the Debt payoff page (table debt_offers).

Offers are quotes to compare, not accounts: nothing here reads or writes the
ledger, balances, or payment terms. Every write leaves an audit row. A database
opened read-only before migration 11 has no table yet; reads then return [].
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from hpgbooks.db import HpgbooksError, audit, now_iso

SOURCES = ("manual", "grok", "import")
LENDER_MAX = 80
NOTES_MAX = 1000
MAX_CENTS = 10**11
FIELDS = (
    "lender", "amount_cents", "apr", "fee_pct", "fee_from_proceeds", "term_months",
    "monthly_payment_cents", "source", "notes", "expires_on",
)
_DOLLARS = re.compile(r"\d+(\.\d{1,2})?")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def has_table(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'debt_offers'").fetchone() is not None


def parse_money(value, what: str = "amount") -> int:
    """Dollars to cents: 187500, "187,500", "$187,500.00". Two decimals at most."""
    if isinstance(value, bool) or value is None:
        raise HpgbooksError(f"{what} is required")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        text = f"{value:.2f}"
    elif isinstance(value, str):
        text = value.strip().replace(",", "").replace("$", "").strip()
    else:
        raise HpgbooksError(f"{what} must be a dollar amount")
    if not text:
        raise HpgbooksError(f"{what} is required")
    if not _DOLLARS.fullmatch(text):
        raise HpgbooksError(f"{what} must be a dollar amount")
    cents = int(Decimal(text) * 100)
    if cents > MAX_CENTS:
        raise HpgbooksError(f"{what} is too large")
    return cents


def _number(value, what: str, low: float, high: float) -> float:
    if isinstance(value, bool) or value is None or (isinstance(value, str) and not value.strip()):
        raise HpgbooksError(f"{what} is required")
    try:
        number = float(Decimal(str(value).strip().rstrip("%").strip()))
    except (InvalidOperation, ValueError) as exc:
        raise HpgbooksError(f"{what} must be a number") from exc
    if not (low <= number <= high):
        raise HpgbooksError(f"{what} must be between {low:g} and {high:g}")
    return round(number, 4)


def _term(value) -> int:
    if isinstance(value, bool):
        raise HpgbooksError("term must be 1 to 360 months")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise HpgbooksError("term must be 1 to 360 months") from exc
    if not 1 <= number <= 360:
        raise HpgbooksError("term must be 1 to 360 months")
    return number


def _date(value) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = str(value).strip()
    try:
        if not _DATE.fullmatch(text):
            raise ValueError
        date.fromisoformat(text)
    except ValueError as exc:
        raise HpgbooksError("expires must be a date (YYYY-MM-DD)") from exc
    return text


def _flag(value) -> int:
    if isinstance(value, bool):
        return int(value)
    if value in (0, 1):
        return int(value)
    if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on"):
        return 1
    if isinstance(value, str) and value.strip().lower() in ("0", "false", "no", "off"):
        return 0
    raise HpgbooksError("fee_from_proceeds must be true or false")


def _text(value, what: str, limit: int, *, required: bool) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip() if what != "notes" else str(value or "").strip()
    if not text:
        if required:
            raise HpgbooksError(f"{what} is required")
        return None
    if len(text) > limit:
        raise HpgbooksError(f"{what} is too long (at most {limit} characters)")
    return text


def _cents_field(body: dict, key: str, what: str) -> int | None:
    """`<key>_cents` (integer cents) or `<key>` (dollars). None when neither is given or blank."""
    if f"{key}_cents" in body and body[f"{key}_cents"] is not None:
        value = body[f"{key}_cents"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise HpgbooksError(f"{what} must be whole cents")
        return value
    if key in body and body[key] not in (None, ""):
        return parse_money(body[key], what)
    return None


def normalize(body: dict, existing: dict | None = None) -> dict:
    """Validated column values. `existing` fills in what an update leaves out."""
    if not isinstance(body, dict):
        raise HpgbooksError("expected an object")
    known = set(FIELDS) | {"amount", "monthly_payment", "csrf_token"}
    unknown = set(body) - known
    if unknown:
        raise HpgbooksError(f"unknown field {sorted(unknown)[0]}")
    base = dict(existing or {})
    out: dict = {}
    out["lender"] = _text(body.get("lender", base.get("lender")), "lender", LENDER_MAX, required=True)
    amount = _cents_field(body, "amount", "amount")
    out["amount_cents"] = amount if amount is not None else base.get("amount_cents")
    if out["amount_cents"] is None or not 0 < out["amount_cents"] <= MAX_CENTS:
        raise HpgbooksError("amount must be more than 0")
    out["apr"] = _number(body.get("apr", base.get("apr")), "apr", 0, 100)
    out["fee_pct"] = _number(body.get("fee_pct", base.get("fee_pct", 0)), "fee", 0, 10)
    out["fee_from_proceeds"] = _flag(body.get("fee_from_proceeds", base.get("fee_from_proceeds", 1)))
    out["term_months"] = _term(body.get("term_months", base.get("term_months")))
    if "monthly_payment_cents" in body or "monthly_payment" in body:
        payment = _cents_field(body, "monthly_payment", "monthly payment")
    else:
        payment = base.get("monthly_payment_cents")
    if payment is not None and not 0 < payment <= MAX_CENTS:
        raise HpgbooksError("monthly payment must be more than 0")
    out["monthly_payment_cents"] = payment
    source = body.get("source", base.get("source") or "manual")
    if source not in SOURCES:
        raise HpgbooksError("source must be manual, grok, or import")
    out["source"] = source
    out["notes"] = _text(body.get("notes", base.get("notes")), "notes", NOTES_MAX, required=False)
    out["expires_on"] = _date(body.get("expires_on", base.get("expires_on")))
    return out


def _today() -> str:
    from hpgbooks.personal.analytics import today

    return today().isoformat()


def shape(row, today: str | None = None) -> dict:
    """One offer for the API and CLI: money as cents and as dollars."""
    item = {key: row[key] for key in row.keys()}
    today = today or _today()
    payment = item["monthly_payment_cents"]
    return {
        "id": item["id"],
        "lender": item["lender"],
        "amount_cents": item["amount_cents"],
        "amount": item["amount_cents"] / 100,
        "apr": item["apr"],
        "fee_pct": item["fee_pct"],
        "fee_from_proceeds": bool(item["fee_from_proceeds"]),
        "term_months": item["term_months"],
        "monthly_payment_cents": payment,
        "monthly_payment": None if payment is None else payment / 100,
        "source": item["source"],
        "notes": item["notes"] or "",
        "expires_on": item["expires_on"],
        "expired": bool(item["expires_on"] and item["expires_on"] < today),
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


def list_offers(conn) -> list[dict]:
    if not has_table(conn):
        return []
    today = _today()
    return [shape(row, today) for row in conn.execute("SELECT * FROM debt_offers ORDER BY created_at, id")]


def get_offer(conn, offer_id: int) -> dict:
    row = conn.execute("SELECT * FROM debt_offers WHERE id = ?", (int(offer_id),)).fetchone() if has_table(conn) else None
    if row is None:
        raise HpgbooksError("no such offer")
    return shape(row)


def _raw(conn, offer_id: int) -> dict:
    row = conn.execute("SELECT * FROM debt_offers WHERE id = ?", (int(offer_id),)).fetchone() if has_table(conn) else None
    if row is None:
        raise HpgbooksError("no such offer")
    return {key: row[key] for key in row.keys()}


def _audit(conn, offer_id: int, old: dict | None, new: dict | None, actor: str) -> None:
    def dump(values):
        return None if values is None else json.dumps({key: values.get(key) for key in FIELDS}, sort_keys=True)

    audit(conn, "debt_offer", field=str(offer_id), old_value=dump(old), new_value=dump(new), actor=actor)


def create_offer(conn, body: dict, *, actor: str) -> dict:
    values = normalize(body)
    stamp = now_iso()
    cur = conn.execute(
        f"""
        INSERT INTO debt_offers ({", ".join(FIELDS)}, created_at, updated_at)
        VALUES ({", ".join("?" for _ in FIELDS)}, ?, ?)
        """,
        (*(values[key] for key in FIELDS), stamp, stamp),
    )
    offer_id = int(cur.lastrowid)
    _audit(conn, offer_id, None, values, actor)
    return get_offer(conn, offer_id)


def update_offer(conn, offer_id: int, body: dict, *, actor: str) -> dict:
    old = _raw(conn, offer_id)
    values = normalize(body, old)
    conn.execute(
        f"UPDATE debt_offers SET {', '.join(f'{key} = ?' for key in FIELDS)}, updated_at = ? WHERE id = ?",
        (*(values[key] for key in FIELDS), now_iso(), int(offer_id)),
    )
    _audit(conn, offer_id, old, values, actor)
    return get_offer(conn, offer_id)


def delete_offer(conn, offer_id: int, *, actor: str) -> dict:
    old = _raw(conn, offer_id)
    conn.execute("DELETE FROM debt_offers WHERE id = ?", (int(offer_id),))
    _audit(conn, offer_id, old, None, actor)
    return {"id": int(offer_id), "deleted": True}
