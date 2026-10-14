"""Merchant cleanup: a stable key per merchant and a display name.

The key strips processor prefixes (SQ *, TST*, PAYPAL *), marketplace order
codes (AMZN Mktp US*2K4AB), store numbers, ACH descriptors (DES: ... ID:),
phone numbers, and a trailing city/state, then upper-cases. User renames are
stored in p_merchants by key; ledger rows are never rewritten.
"""

from __future__ import annotations

import re

from hpgbooks.db import HpgbooksError, _table_exists, audit, now_iso

_PREFIXES = re.compile(r"^(SQ ?\*|TST ?\*|PAYPAL ?\*|PP ?\*|SP ?\*|PY ?\*|GOOGLE ?\*|APPLE\.COM/BILL ?\*?|IC ?\*|DD ?\*|POS |DEBIT CARD PURCHASE |CHECKCARD \d{4} |PURCHASE AUTHORIZED ON \d\d/\d\d )", re.I)
_ACH = re.compile(r"\s+(DES|INDN|CO ID|ID|PPD|WEB|CCD|CONF|CONFIRMATION)\s*[:#].*$", re.I)
_ORDER = re.compile(r"\*[A-Z0-9]{4,}\b.*$", re.I)
_STORE = re.compile(r"(#\s*\d+|\bSTORE\s+\d+|\b\d{3,}\b)", re.I)
_PHONE = re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b")
_STATE = re.compile(
    r"\s+(?:[A-Z][A-Z .'-]{2,20}\s+)?(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)$"
)
KNOWN = {
    "AMZN MKTP US": "Amazon",
    "AMZN MKTP": "Amazon",
    "AMAZON COM": "Amazon",
    "AMAZON.COM": "Amazon",
    "NETFLIX.COM": "Netflix",
    "SPOTIFY USA": "Spotify",
    "COMCAST XFINITY": "Xfinity",
    "VERIZON WIRELESS": "Verizon",
    "WAL-MART": "Walmart",
    "COSTCO WHSE": "Costco",
}


def merchant_key(merchant: str | None, name: str | None = None) -> str:
    raw = (merchant or "").strip() or (name or "").strip()
    text = re.sub(r"\s+", " ", raw.upper())
    text = _PREFIXES.sub("", text)
    text = _ACH.sub("", text)
    text = _PHONE.sub("", text)
    text = _ORDER.sub("", text)
    text = _STORE.sub("", text)
    text = re.sub(r"[*]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -*.,")
    stripped = _STATE.sub("", text).strip(" -*.,")
    if len(stripped) >= 3:
        text = stripped
    return text or "(UNKNOWN)"


def default_display(key: str) -> str:
    if key in KNOWN:
        return KNOWN[key]
    if key == "(UNKNOWN)":
        return "Unknown"
    words = []
    for word in key.split(" "):
        if len(word) <= 3 and word.isalpha() and word not in ("THE", "AND"):
            words.append(word)
        else:
            words.append(word.capitalize())
    return " ".join(words)


def load_aliases(conn) -> dict[str, dict]:
    if not _table_exists(conn, "p_merchants"):
        return {}
    return {
        row["key"]: {"display_name": row["display_name"], "default_category_id": row["default_category_id"]}
        for row in conn.execute("SELECT key, display_name, default_category_id FROM p_merchants")
    }


def display_for(key: str, aliases: dict[str, dict]) -> str:
    alias = aliases.get(key)
    return alias["display_name"] if alias else default_display(key)


def rename_merchant(conn, key: str, display_name: str, *, actor: str) -> dict:
    """Store a display name for every transaction with this merchant key."""
    key = (key or "").strip().upper()
    name = re.sub(r"\s+", " ", (display_name or "").strip())
    if not key or len(key) > 200:
        raise HpgbooksError("merchant key is required")
    if not name or len(name) > 120:
        raise HpgbooksError("display name must be 1 to 120 characters")
    old = conn.execute("SELECT display_name FROM p_merchants WHERE key = ?", (key,)).fetchone()
    conn.execute(
        """
        INSERT INTO p_merchants (key, display_name, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET display_name = excluded.display_name, updated_at = excluded.updated_at
        """,
        (key, name, now_iso()),
    )
    audit(
        conn,
        "personal_merchant_rename",
        field=key,
        old_value=old["display_name"] if old else None,
        new_value=name,
        actor=actor,
    )
    return {"key": key, "display_name": name}


def remove_merchant_alias(conn, key: str, *, actor: str) -> None:
    row = conn.execute("SELECT display_name FROM p_merchants WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise HpgbooksError("no such merchant name")
    conn.execute("DELETE FROM p_merchants WHERE key = ?", (key,))
    audit(conn, "personal_merchant_rename", field=key, old_value=row["display_name"], new_value=None, actor=actor)
