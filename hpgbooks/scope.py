"""Business / personal scope of accounts, and the SQL that keeps the two apart.

Every account row has scope 'business', 'personal', or 'excluded'. A mode is
'business' or 'personal'; an excluded account belongs to neither and is shown
only on the accounts settings screen. Transactions carry no scope of their own:
a row's scope is its account's scope, read through the join each time, so
moving an account between modes re-scopes reports without rewriting the ledger.

All filters are parameterized. A database that has not been migrated yet (no
scope column, opened read-only) is treated as all-business, which is what it is.
"""

from __future__ import annotations

from hpgbooks.db import ACCOUNT_CLASSES, ACCOUNT_SCOPES, ACCOUNTS, HpgbooksError

MODES = ("business", "personal")
DEFAULT_MODE = "business"
SEED_ORDER = {acct["id"]: index for index, acct in enumerate(ACCOUNTS)}
# Finance tool per account class for the daily pull.
CASH_TOOL = "finance_query_account_transactions"
LIABILITY_TOOL = "finance_query_liability_transactions"


def parse_mode(value: str | None) -> str:
    """'business' (also for a missing value) or 'personal'. Anything else is an error."""
    if value is None or value == "":
        return DEFAULT_MODE
    if value not in MODES:
        raise HpgbooksError("mode must be business or personal")
    return value


def has_scope(conn) -> bool:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    return "scope" in cols


def scope_clause(conn, mode: str, column: str = "t.account_id") -> tuple[str, list]:
    """SQL fragment limiting `column` (an account id) to accounts in this mode."""
    mode = parse_mode(mode)
    if not has_scope(conn):
        return ("1 = 1", []) if mode == "business" else ("0 = 1", [])
    return f"{column} IN (SELECT id FROM accounts WHERE scope = ?)", [mode]


def _row(row) -> dict:
    return {key: row[key] for key in row.keys()}


def _normalize(item: dict) -> dict:
    item.setdefault("scope", "business")
    if not item.get("class"):
        item["class"] = "liability" if item.get("type") == "liability" else "cash"
    for key, default in (("include_in_net_worth", 1), ("sync_enabled", 1), ("closed", 0)):
        if item.get(key) is None:
            item[key] = default
    item.setdefault("display_name", None)
    item.setdefault("nickname", None)
    return item


def all_accounts(conn) -> list[dict]:
    """Every account row, the five business accounts first in their usual order."""
    rows = [_normalize(_row(row)) for row in conn.execute("SELECT * FROM accounts")]
    rows.sort(key=lambda item: (SEED_ORDER.get(item["id"], 99), (item.get("name") or "").lower(), item["id"]))
    return rows


def accounts_in(conn, mode: str) -> list[dict]:
    mode = parse_mode(mode)
    return [item for item in all_accounts(conn) if item["scope"] == mode]


def account_ids(conn, mode: str) -> set[str]:
    return {item["id"] for item in accounts_in(conn, mode)}


def known_account_ids(conn) -> set[str]:
    """Ids of every registered account, any scope. The importer accepts these."""
    return {row["id"] for row in conn.execute("SELECT id FROM accounts")}


def account_scope(conn, account_id: str) -> str | None:
    for item in all_accounts(conn):
        if item["id"] == account_id:
            return item["scope"]
    return None


def get_account(conn, account_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return _normalize(_row(row)) if row is not None else None


def txn_scope(conn, txn_id: str) -> str | None:
    if not has_scope(conn):
        row = conn.execute("SELECT 1 FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        return "business" if row else None
    row = conn.execute(
        """
        SELECT a.scope FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE t.id = ?
        """,
        (txn_id,),
    ).fetchone()
    return row["scope"] if row else None


def label(acct: dict) -> str:
    """Short label for lists: display name, else name. Last 4 only, never a full number."""
    return acct.get("display_name") or acct.get("nickname") or acct.get("name") or acct["id"][:8]


def finance_tool(acct: dict) -> str:
    return LIABILITY_TOOL if acct.get("type") == "liability" or acct.get("class") in ("liability", "loan") else CASH_TOOL


def inbox_prefix(acct: dict) -> str:
    """File prefix for the daily pull: last 4, else a slug of the label."""
    import re

    if acct.get("last4"):
        return str(acct["last4"])
    source = acct.get("institution") or label(acct)
    slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
    return slug[:24] or acct["id"][:8]


def resolve_account(conn, text: str, *, scopes: tuple[str, ...] = ACCOUNT_SCOPES) -> dict:
    """Account by id, last 4, or a unique name fragment. Ambiguous text is refused."""
    text = (text or "").strip()
    if not text:
        raise HpgbooksError("account is required")
    rows = [item for item in all_accounts(conn) if item["scope"] in scopes]
    exact = [item for item in rows if item["id"] == text]
    if exact:
        return exact[0]
    last4 = [item for item in rows if item.get("last4") and item["last4"] == text]
    if len(last4) == 1:
        return last4[0]
    if len(last4) > 1:
        raise HpgbooksError(f"{text} matches more than one account")
    folded = text.lower()
    matches = [
        item
        for item in rows
        if item["id"].startswith(text)
        or folded in (item.get("name") or "").lower()
        or folded in (item.get("display_name") or "").lower()
        or folded in (item.get("nickname") or "").lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise HpgbooksError(f"no account matches {text}")
    raise HpgbooksError(f"{text} matches more than one account")


def validate_scope(value: str) -> str:
    if value not in ACCOUNT_SCOPES:
        raise HpgbooksError("scope must be business, personal, or excluded")
    return value


def validate_class(value: str) -> str:
    if value not in ACCOUNT_CLASSES:
        raise HpgbooksError("class must be cash, liability, investment, loan, or other")
    return value


def type_for_class(account_class: str) -> str:
    """Balance sign convention for a class: loans and cards are amounts owed."""
    return "liability" if account_class in ("liability", "loan") else "cash"


def count_transactions(conn, account_id: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE account_id = ? AND status = 'active'",
            (account_id,),
        ).fetchone()[0]
    )
