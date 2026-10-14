"""Statement balance anchors and the real balance derived from them.

Sign
----
An anchor's balance_cents is positive when cash is money in the account, and
positive when a credit card or other liability is the amount owed. Enter
liability anchors as the amount owed (a credit balance is negative).

Ledger rows keep their own sign: positive amount_cents is money in, negative
is money out. A card charge is negative, and that increases the amount owed,
so a liability's displayed balance moves by the opposite of amount_cents.

Posted vs pending
-----------------
An anchor is a posted balance at the end of as_of_date. Posted activity on
that day is already inside it.

Counted toward the real balance and the register running balance:

- active rows dated after as_of_date, pending or posted
- active posted rows from 2026-01-01 through as_of_date (these are already in
  the anchor; they are used to walk the register back to an opening balance)

Left out, so they are not double counted:

- pending rows dated on or before as_of_date (that day is already closed in
  the posted anchor)
- superseded rows
- rows dated before 2026-01-01

Opening balance
---------------
The books open on 2026-01-01, before that day's activity:

    opening = anchor − posted activity from 2026-01-01 through as_of_date

The register starts at that opening figure and adds every counted row, so the
balance after the last counted row equals the real balance:

    real = anchor + activity dated after as_of_date

Latest anchor
-------------
An account may have many anchors. The active one is the latest by as_of_date,
then created_at, then id. When a newer anchor disagrees with the balance the
previous anchor predicts on the newer as_of_date (posted activity strictly
after the previous date, through the newer date), that gap is the
reconciliation difference. A zero gap reconciles to the newer anchor.

With no anchor, display_cents is the imported activity sum for cash and the
negation of that sum for a liability (amount owed). balance_cents on account
lists stays the raw activity sum either way.
"""

from __future__ import annotations

import json
import re

from hpgbooks.db import (
    HpgbooksError,
    _table_exists,
    audit,
    now_iso,
    to_cents,
)

OPENING_DATE = "2026-01-01"
ANCHOR_SOURCES = ("finance", "statement")
# One hundred million dollars. Larger figures are rejected as typos.
MAX_BALANCE_CENTS = 10_000_000_000
_DOLLAR_RE = re.compile(r"-?\d+(\.\d{1,2})?")

PUBLIC_KEYS = (
    "anchored",
    "display_cents",
    "real_balance_cents",
    "opening_cents",
    "as_of_date",
    "anchor_cents",
    "source",
    "anchor_note",
    "drift_cents",
    "computed_cents",
    "reconcile_cents",
    "activity_cents",
)


def balance_effect(account_type: str, amount_cents: int) -> int:
    """How one ledger row moves the displayed balance."""
    amount = int(amount_cents)
    if account_type == "liability":
        return -amount
    return amount


def counts_for_balance(txn: dict, as_of: str) -> bool:
    """True when this active row is part of the anchored balance.

    Pending rows on or before as_of are excluded. See the module docstring.
    """
    if txn["date"] < OPENING_DATE:
        return False
    if int(txn["pending"] or 0) != 0 and txn["date"] <= as_of:
        return False
    return True


def parse_dollars(value, *, required: bool = True) -> int:
    """Dollars (string or number) to cents. At most two decimal places."""
    if isinstance(value, bool) or value is None:
        if required:
            raise HpgbooksError("balance is required")
        raise HpgbooksError("balance must be a dollar amount")
    if isinstance(value, (int, float)):
        text = str(value) if isinstance(value, int) else f"{value:.2f}"
    elif isinstance(value, str):
        text = value.strip().replace(",", "").replace("$", "")
    else:
        raise HpgbooksError("balance must be a dollar amount")
    if text == "":
        raise HpgbooksError("balance is required")
    if not _DOLLAR_RE.fullmatch(text):
        raise HpgbooksError("balance must be a dollar amount")
    cents = to_cents(text)
    if abs(cents) > MAX_BALANCE_CENTS:
        raise HpgbooksError("balance is out of range")
    return cents


def clean_note(note) -> str:
    if note is None:
        return ""
    if not isinstance(note, str):
        raise HpgbooksError("note must be text")
    note = note.strip()
    if len(note) > 500:
        raise HpgbooksError("note is too long")
    return note


def _account(conn, account_id: str) -> dict:
    """Any registered account, business or personal. Excluded accounts are refused."""
    from hpgbooks.scope import get_account

    acct = get_account(conn, account_id)
    if acct is None or acct["scope"] == "excluded":
        raise HpgbooksError("no such account")
    return acct


def _load_anchors(conn) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    if not _table_exists(conn, "balance_anchors"):
        return grouped
    rows = conn.execute(
        """
        SELECT id, account_id, as_of_date, balance_cents, source, note, created_at
        FROM balance_anchors
        ORDER BY as_of_date, created_at, id
        """
    ).fetchall()
    for row in rows:
        grouped.setdefault(row["account_id"], []).append({key: row[key] for key in row.keys()})
    return grouped


def _active_by_account(conn, account_id: str | None = None) -> dict[str, list[dict]]:
    if account_id is None:
        grouped: dict[str, list[dict]] = {row["id"]: [] for row in conn.execute("SELECT id FROM accounts")}
        where, params = "", ()
    else:
        grouped = {account_id: []}
        where, params = "AND account_id = ?", (account_id,)
    rows = conn.execute(
        f"""
        SELECT id, account_id, date, amount_cents, pending
        FROM transactions
        WHERE status = 'active' {where}
        ORDER BY date, id
        """,
        params,
    ).fetchall()
    for row in rows:
        bucket = grouped.get(row["account_id"])
        if bucket is None:
            continue
        bucket.append({key: row[key] for key in row.keys()})
    return grouped


def _drift(account_type: str, ordered: list[dict], txns: list[dict]) -> tuple[int | None, int | None]:
    """Newer anchor minus the balance the previous anchor predicts on that date."""
    if len(ordered) < 2:
        return None, None
    latest = ordered[-1]
    prev = ordered[-2]
    computed = int(prev["balance_cents"])
    for txn in txns:
        if int(txn["pending"] or 0) != 0:
            continue
        if txn["date"] < OPENING_DATE:
            continue
        if prev["as_of_date"] < txn["date"] <= latest["as_of_date"]:
            computed += balance_effect(account_type, txn["amount_cents"])
    return int(latest["balance_cents"]) - computed, computed


def _snapshot(acct: dict, anchor_rows: list[dict], txns: list[dict]) -> dict:
    activity = sum(int(txn["amount_cents"]) for txn in txns)
    ordered = sorted(anchor_rows, key=lambda row: (row["as_of_date"], row["created_at"], row["id"]))
    snap = {
        "id": acct["id"],
        "name": acct["name"],
        "type": acct["type"],
        "anchored": False,
        "activity_cents": activity,
        "real_balance_cents": None,
        "opening_cents": None,
        "as_of_date": None,
        "anchor_cents": None,
        "source": None,
        "anchor_note": None,
        "drift_cents": None,
        "computed_cents": None,
        "reconcile_cents": None,
        "anchor_id": None,
        "display_cents": -activity if acct["type"] == "liability" else activity,
    }
    if not ordered:
        return snap
    latest = ordered[-1]
    as_of = latest["as_of_date"]
    through = 0
    after = 0
    for txn in txns:
        if not counts_for_balance(txn, as_of):
            continue
        effect = balance_effect(acct["type"], txn["amount_cents"])
        if txn["date"] <= as_of:
            through += effect
        else:
            after += effect
    anchor_cents = int(latest["balance_cents"])
    drift, computed = _drift(acct["type"], ordered, txns)
    snap.update(
        {
            "anchored": True,
            "real_balance_cents": anchor_cents + after,
            "display_cents": anchor_cents + after,
            "opening_cents": anchor_cents - through,
            "as_of_date": as_of,
            "anchor_cents": anchor_cents,
            "source": latest["source"],
            "anchor_note": latest["note"] or "",
            "drift_cents": drift,
            "computed_cents": computed,
            "reconcile_cents": anchor_cents,
            "anchor_id": latest["id"],
        }
    )
    return snap


def account_snapshots(conn, mode: str = "business") -> list[dict]:
    """One snapshot per account in the mode, in the standard account order."""
    from hpgbooks.scope import accounts_in

    anchors = _load_anchors(conn)
    activity = _active_by_account(conn)
    return [
        _snapshot(acct, anchors.get(acct["id"], []), activity.get(acct["id"], []))
        for acct in accounts_in(conn, mode)
    ]


def snapshot_for(conn, account_id: str) -> dict:
    acct = _account(conn, account_id)
    anchors = _load_anchors(conn).get(account_id, [])
    txns = _active_by_account(conn, account_id).get(account_id, [])
    return _snapshot(acct, anchors, txns)


def attach_balances(conn, rows: list[dict], mode: str = "business") -> list[dict]:
    """Copy the public balance fields onto account rows. Raw balance_cents stays."""
    snaps = {snap["id"]: snap for snap in account_snapshots(conn, mode)}
    for row in rows:
        snap = snaps.get(row.get("id"))
        if snap is None:
            continue
        for key in PUBLIC_KEYS:
            row[key] = snap[key]
    return rows


def running_balances(snap: dict, txns: list[dict]) -> dict[str, int]:
    """Running displayed balance after each counted row, in date/id order.

    With no anchor this is the cumulative ledger sum (historical register).
    With an anchor it starts at the implied opening balance on 2026-01-01.
    """
    ordered = sorted(txns, key=lambda txn: (txn["date"], txn["id"]))
    running: dict[str, int] = {}
    if not snap["anchored"]:
        balance = 0
        for txn in ordered:
            balance += int(txn["amount_cents"])
            running[txn["id"]] = balance
        return running
    balance = int(snap["opening_cents"])
    as_of = snap["as_of_date"]
    for txn in ordered:
        if not counts_for_balance(txn, as_of):
            continue
        balance += balance_effect(snap["type"], txn["amount_cents"])
        running[txn["id"]] = balance
    return running


def anchor_history(conn, account_id: str) -> list[dict]:
    _account(conn, account_id)
    rows = _load_anchors(conn).get(account_id, [])
    return list(reversed(rows))


def set_anchor(
    conn,
    account_id: str,
    balance_cents: int,
    as_of: str,
    source: str,
    note: str,
    *,
    actor: str,
) -> dict:
    """Record an anchor. The ledger is not changed. Returns the new snapshot."""
    from hpgbooks.reports import require_date

    _account(conn, account_id)
    as_of = require_date(as_of)
    if source not in ANCHOR_SOURCES:
        raise HpgbooksError("source must be finance or statement")
    note = clean_note(note)
    if isinstance(balance_cents, bool) or not isinstance(balance_cents, int):
        raise HpgbooksError("balance must be a dollar amount")
    if abs(balance_cents) > MAX_BALANCE_CENTS:
        raise HpgbooksError("balance is out of range")
    previous = _load_anchors(conn).get(account_id, [])
    prev = previous[-1] if previous else None
    old = None
    if prev is not None:
        old = json.dumps(
            {
                "as_of_date": prev["as_of_date"],
                "balance_cents": int(prev["balance_cents"]),
                "source": prev["source"],
                "note": prev["note"] or "",
            },
            sort_keys=True,
        )
    conn.execute(
        """
        INSERT INTO balance_anchors (account_id, as_of_date, balance_cents, source, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (account_id, as_of, int(balance_cents), source, note or None, now_iso()),
    )
    new = json.dumps(
        {"as_of_date": as_of, "balance_cents": int(balance_cents), "source": source, "note": note},
        sort_keys=True,
    )
    audit(
        conn,
        "balance_set",
        field=account_id,
        old_value=old,
        new_value=new,
        actor=actor,
        note=note or None,
    )
    return snapshot_for(conn, account_id)
