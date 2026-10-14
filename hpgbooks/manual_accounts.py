"""Balance-only manual accounts (no transactions, no Finance sync).

For things that have no feed and no statements to import: a mortgage, a 401(k),
a house, a car. The account is personal-scope with sync off, and its notes start
with "manual:" so `accounts discover` recognizes it (by last 4, or by name when
there is none) and does not register a twin. The balance is a balance anchor:
liabilities (card, loan, mortgage) are stored as the amount owed (positive),
assets as the value (positive). Only the net-worth views read these accounts;
they have no transactions, so spending, cash flow and the business reports never
see them.
"""

from __future__ import annotations

import json
import re

from hpgbooks.balances import parse_dollars, set_anchor
from hpgbooks.db import ACCOUNT_CLASSES, HpgbooksError
from hpgbooks.scope import all_accounts, get_account

MANUAL_PREFIX = "manual:"
DEFAULT_LABEL = "Monarch"
ACTOR = "set_manual_balance"

# Provider words handed to account discovery for each class (it derives class from them).
CLASS_WORDS = {
    "liability": "credit card",
    "loan": "loan",
    "investment": "investment",
    "cash": "savings",
    "other": "other",
}


def account_note(as_of: str, label: str = DEFAULT_LABEL) -> str:
    return f"{MANUAL_PREFIX} balance from {label} {as_of}, update manually"


def _norm(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def slug_id(name: str, last4: str | None = None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48].strip("-")
    return f"manual-{slug}" + (f"-{last4}" if last4 and not slug.endswith(last4) else "")


def is_manual(acct: dict) -> bool:
    return (acct.get("notes") or "").startswith(MANUAL_PREFIX)


def find_manual(conn, name: str) -> dict | None:
    """A manual account by id, exact name, or a unique piece of its name."""
    wanted = _norm(name)
    if not wanted:
        raise HpgbooksError("account name is required")
    manual = [a for a in all_accounts(conn) if is_manual(a)]
    for acct in manual:
        if name == acct["id"] or wanted in (_norm(acct.get("name")), _norm(acct.get("display_name"))):
            return acct
    hits = [a for a in manual if wanted in _norm(a.get("name")) or wanted in _norm(a.get("display_name"))]
    if len(hits) > 1:
        raise HpgbooksError(f"'{name}' matches more than one manual account: " + ", ".join(a["name"] for a in hits))
    return hits[0] if hits else None


def create_account(
    conn,
    name: str,
    account_class: str,
    *,
    last4: str | None = None,
    institution: str | None = None,
    subtype: str | None = None,
    actor: str = ACTOR,
) -> str:
    """Register a personal, sync-off, balance-only account through the normal discovery path."""
    from hpgbooks.accounts_admin import discover, set_class, update_settings

    if account_class not in ACCOUNT_CLASSES:
        raise HpgbooksError("class must be cash, liability, investment, loan, or other")
    taken = [a for a in all_accounts(conn) if _norm(a.get("name")) == _norm(name) and (a.get("last4") or "") == (last4 or "")]
    if taken:
        raise HpgbooksError(f"an account named '{taken[0]['name']}' already exists ({taken[0]['id']}); it is not a manual account")
    account_id = slug_id(name, last4)
    if get_account(conn, account_id):
        raise HpgbooksError(f"account id {account_id} already exists")
    payload = json.dumps(
        [
            {
                "id": account_id,
                "name": name,
                "official_name": name,
                "institution": institution,
                "mask": last4 or "",
                "type": CLASS_WORDS[account_class],
                "subtype": subtype,
            }
        ]
    )
    results = discover(conn, payload, scope="personal", actor=actor)
    if not results or results[0]["status"] != "new":
        match = results[0] if results else {}
        raise HpgbooksError(f"'{name}' looks like an existing account ({match.get('name', '?')}, {match.get('status', '?')}); not creating a twin")
    acct = get_account(conn, account_id)
    if acct["class"] != account_class:
        set_class(conn, account_id, account_class, actor=actor)
    update_settings(conn, account_id, {"sync_enabled": False}, actor=actor)
    return account_id


def set_balance(
    conn,
    name: str,
    balance,
    as_of: str,
    *,
    create_class: str | None = None,
    last4: str | None = None,
    institution: str | None = None,
    subtype: str | None = None,
    label: str = DEFAULT_LABEL,
    dry_run: bool = False,
) -> str:
    """Record a balance anchor on a manual account, creating the account when create_class is given."""
    from hpgbooks.reports import require_date

    as_of = require_date(as_of)
    cents = parse_dollars(balance)
    acct = find_manual(conn, name)
    created = False
    if acct is None:
        if not create_class:
            raise HpgbooksError(f"no manual account matches '{name}'; add --class (and --create) to create it")
        if dry_run:
            return f"would create {name} ({create_class}) with balance {cents / 100:,.2f} as of {as_of}"
        account_id = create_account(conn, name, create_class, last4=last4, institution=institution, subtype=subtype)
        acct = get_account(conn, account_id)
        created = True
    account_id = acct["id"]
    latest = conn.execute(
        "SELECT as_of_date, balance_cents FROM balance_anchors WHERE account_id = ? ORDER BY as_of_date DESC, id DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    note = account_note(as_of, label)
    prefix = "created " if created else ""
    if latest and latest["as_of_date"] == as_of and int(latest["balance_cents"]) == cents:
        return f"{acct['name']}: {cents / 100:,.2f} as of {as_of} already recorded"
    if not dry_run:
        set_anchor(conn, account_id, cents, as_of, "statement", note[len(MANUAL_PREFIX):].strip(), actor=ACTOR)
        conn.execute("UPDATE accounts SET notes = ? WHERE id = ?", (note, account_id))
    verb = "would set" if dry_run else "set"
    return f"{prefix}{acct['name']}: {verb} {cents / 100:,.2f} as of {as_of} ({acct['class']})"
