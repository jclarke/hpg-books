"""Account registration and settings: scope, class, names, flags, discovery, sync list.

Changing an account's scope never rewrites ledger rows; every report reads the
scope through the account join. Each change writes an audit row. Output never
shows a full account number: only the stored last 4.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date

from hpgbooks.db import HpgbooksError, audit, now_iso, to_cents
from hpgbooks.scope import (
    all_accounts,
    count_transactions,
    finance_tool,
    get_account,
    inbox_prefix,
    label,
    validate_class,
    validate_scope,
    type_for_class,
)

MAX_NAME = 120
_LONG_DIGITS = re.compile(r"\d{5,}")


def scrub_name(text: str | None) -> str:
    """Account names from a provider can carry a full number; keep only its last 4."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    return _LONG_DIGITS.sub(lambda match: "…" + match.group(0)[-4:], text)[:MAX_NAME]


def _clean_text(value, name: str, *, max_len: int = MAX_NAME) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HpgbooksError(f"{name} must be text")
    value = re.sub(r"\s+", " ", value.strip())
    if len(value) > max_len:
        raise HpgbooksError(f"{name} is too long")
    return scrub_name(value) or None


def public_account(acct: dict, *, txn_count: int | None = None) -> dict:
    out = {
        "id": acct["id"],
        "name": acct.get("name") or "",
        "display_name": acct.get("display_name") or "",
        "nickname": acct.get("nickname") or "",
        "label": label(acct),
        "last4": acct.get("last4") or "",
        "institution": acct.get("institution") or "",
        "type": acct.get("type") or "cash",
        "class": acct.get("class") or "cash",
        "subtype": acct.get("subtype") or "",
        "scope": acct.get("scope") or "business",
        "include_in_net_worth": bool(acct.get("include_in_net_worth", 1)),
        "sync_enabled": bool(acct.get("sync_enabled", 1)),
        "closed": bool(acct.get("closed", 0)),
    }
    if txn_count is not None:
        out["transaction_count"] = txn_count
    return out


def list_accounts(conn, scope: str | None = None) -> list[dict]:
    rows = all_accounts(conn)
    if scope:
        validate_scope(scope)
        rows = [row for row in rows if row["scope"] == scope]
    return [public_account(row, txn_count=count_transactions(conn, row["id"])) for row in rows]


def set_scope(conn, account_id: str, scope: str, *, actor: str) -> dict:
    """Move an account to business, personal, or excluded. Returns the change summary."""
    validate_scope(scope)
    acct = get_account(conn, account_id)
    if acct is None:
        raise HpgbooksError("no such account")
    old = acct["scope"]
    moved = count_transactions(conn, account_id)
    if old != scope:
        conn.execute("UPDATE accounts SET scope = ? WHERE id = ?", (scope, account_id))
        audit(
            conn,
            "account_scope",
            field=account_id,
            old_value=old,
            new_value=scope,
            actor=actor,
            note=f"{label(acct)}: {moved} transactions move from {old} to {scope}",
        )
    return {"id": account_id, "old_scope": old, "scope": scope, "transactions": moved, "changed": old != scope}


def set_class(conn, account_id: str, account_class: str, *, actor: str) -> dict:
    validate_class(account_class)
    acct = get_account(conn, account_id)
    if acct is None:
        raise HpgbooksError("no such account")
    old = acct["class"]
    if old == account_class:
        return public_account(acct)
    if acct["scope"] == "business" and type_for_class(account_class) != acct["type"]:
        # The business balance math reads `type`; do not flip a business account's sign.
        raise HpgbooksError("a business account keeps its cash/liability type; move it to personal first")
    conn.execute(
        "UPDATE accounts SET class = ?, type = ? WHERE id = ?",
        (account_class, type_for_class(account_class), account_id),
    )
    audit(conn, "account_class", field=account_id, old_value=old, new_value=account_class, actor=actor)
    return public_account(get_account(conn, account_id))


def rename(conn, account_id: str, display_name: str | None, *, actor: str) -> dict:
    acct = get_account(conn, account_id)
    if acct is None:
        raise HpgbooksError("no such account")
    new = _clean_text(display_name, "display name")
    old = acct.get("display_name")
    if (old or None) != new:
        conn.execute("UPDATE accounts SET display_name = ? WHERE id = ?", (new, account_id))
        audit(conn, "account_rename", field=account_id, old_value=old, new_value=new, actor=actor)
    return public_account(get_account(conn, account_id))


SETTINGS_FIELDS = ("scope", "class", "include_in_net_worth", "sync_enabled", "display_name", "closed")


def update_settings(conn, account_id: str, changes: dict, *, actor: str) -> dict:
    """Apply a PATCH from the settings screen. Unknown fields are rejected."""
    unknown = set(changes) - set(SETTINGS_FIELDS)
    if unknown:
        raise HpgbooksError("unknown account setting")
    acct = get_account(conn, account_id)
    if acct is None:
        raise HpgbooksError("no such account")
    result: dict = {}
    if "scope" in changes:
        if not isinstance(changes["scope"], str):
            raise HpgbooksError("scope must be business, personal, or excluded")
        result["scope_change"] = set_scope(conn, account_id, changes["scope"], actor=actor)
    if "class" in changes:
        if not isinstance(changes["class"], str):
            raise HpgbooksError("class must be cash, liability, investment, loan, or other")
        set_class(conn, account_id, changes["class"], actor=actor)
    if "display_name" in changes:
        rename(conn, account_id, changes["display_name"] or None, actor=actor)
    for flag in ("include_in_net_worth", "sync_enabled", "closed"):
        if flag not in changes:
            continue
        value = changes[flag]
        if not isinstance(value, bool):
            raise HpgbooksError(f"{flag} must be true or false")
        current = get_account(conn, account_id)
        if bool(current.get(flag)) != value:
            conn.execute(f"UPDATE accounts SET {flag} = ? WHERE id = ?", (1 if value else 0, account_id))
            audit(
                conn,
                "account_settings",
                field=account_id,
                old_value=str(int(bool(current.get(flag)))),
                new_value=str(int(value)),
                actor=actor,
                note=flag,
            )
    result["account"] = public_account(get_account(conn, account_id), txn_count=count_transactions(conn, account_id))
    return result


# --- discovery from finance_list_accounts -----------------------------------


def _parse_payload(text: str) -> list[dict]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HpgbooksError("accounts input is not valid JSON") from exc
    items = payload if isinstance(payload, list) else [payload]
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            raise HpgbooksError("accounts input must be objects")
        if "csv" in item and isinstance(item.get("csv"), str):
            out.extend(dict(row) for row in csv.DictReader(io.StringIO(item["csv"])))
        elif isinstance(item.get("accounts"), list):
            out.extend(row for row in item["accounts"] if isinstance(row, dict))
        elif isinstance(item.get("content"), list):
            # MCP tool-result wrapper: [{"type": "text", "text": "<json>"}]
            for part in item["content"]:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    out.extend(_parse_payload(part["text"]))
        else:
            out.append(item)
    return out


def _pick(row: dict, *names):
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _nested_balance(row: dict, key: str):
    balances = row.get("balances")
    if isinstance(balances, dict):
        return balances.get(key)
    return None


def classify_provider(row: dict) -> str:
    """Account class from the provider's class/type/subtype words."""
    words = " ".join(
        str(row.get(name) or "").lower() for name in ("class", "type", "subtype", "account_type", "account_subtype")
    )
    if any(word in words for word in ("mortgage", "loan", "auto", "student", "heloc", "line of credit")):
        return "loan"
    if any(word in words for word in ("credit", "liability", "card")):
        return "liability"
    if any(word in words for word in ("investment", "brokerage", "ira", "401k", "retirement", "hsa", "529")):
        return "investment"
    if any(word in words for word in ("depository", "checking", "savings", "cash", "money market", "cd", "prepaid")):
        return "cash"
    return "other"


def normalize_discovered(row: dict) -> dict | None:
    account_id = _pick(row, "id", "account_id", "liability_id")
    if not account_id:
        return None
    mask = str(_pick(row, "mask", "last4", "last_four") or "")
    digits = re.sub(r"\D", "", mask)
    current = _pick(row, "current_balance") if _pick(row, "current_balance") is not None else _nested_balance(row, "current")
    return {
        "id": str(account_id).strip(),
        "name": scrub_name(str(_pick(row, "official_name", "name", "account_name") or "")) or None,
        "short_name": scrub_name(str(_pick(row, "name", "account_name") or "")) or None,
        "last4": digits[-4:] if digits else None,
        "institution": scrub_name(str(_pick(row, "institution", "institution_name") or "")) or None,
        "class": classify_provider(row),
        "subtype": scrub_name(str(_pick(row, "subtype", "account_subtype") or "")) or None,
        "current_balance": current,
    }


MANUAL_PREFIX = "manual:"


def manual_match(conn, item: dict) -> dict | None:
    """A manual (statement-fed) account for the same card as a discovered one.

    Manual accounts carry notes starting with "manual:" (for example the CFNA
    store card, which has no Finance connection). A discovered account with the
    same last 4 and the same cash/liability type is that card, so discovery
    must not register a second account or add anchors to the manual one.
    """
    last4 = item.get("last4")
    discovered_type = type_for_class(item["class"])
    names = {_norm_name(item.get(key)) for key in ("name", "short_name")} - {""}  # not the institution: one institution can hold several accounts
    for acct in all_accounts(conn):
        if not (acct.get("notes") or "").startswith(MANUAL_PREFIX) or acct.get("type") != discovered_type:
            continue
        if last4 and acct.get("last4") == last4:
            return acct
        if not acct.get("last4"):
            # A card with no number on record (Apple Card): match on the name.
            mine = _norm_name(acct.get("name"))
            if mine and any(mine in other or other in mine for other in names if len(other) >= 5):
                return acct
    return None


def _norm_name(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def discover(conn, text: str, *, scope: str = "personal", dry_run: bool = False, as_of: str | None = None, actor: str = "cli") -> list[dict]:
    """Register unknown accounts from a finance_list_accounts result. Idempotent.

    Existing accounts keep their scope; a blank name or an 'other' class is filled in.
    current_balance becomes a finance balance anchor dated as_of (default today),
    skipped when the latest anchor already has that date and amount.
    """
    from hpgbooks.balances import MAX_BALANCE_CENTS
    from hpgbooks.reports import require_date

    validate_scope(scope)
    as_of = require_date(as_of) if as_of else date.today().isoformat()
    results: list[dict] = []
    seen: set[str] = set()
    for raw in _parse_payload(text):
        item = normalize_discovered(raw)
        if item is None or item["id"] in seen:
            continue
        seen.add(item["id"])
        existing = get_account(conn, item["id"])
        status = "existing"
        if existing is None:
            manual = manual_match(conn, item)
            if manual is not None:
                # A statement-fed (manual) account already stands for this card: do not
                # register a twin or touch its balance anchors.
                results.append(
                    {
                        "id": manual["id"],
                        "last4": manual.get("last4") or "",
                        "name": manual.get("display_name") or manual.get("name") or "",
                        "institution": manual.get("institution") or "",
                        "class": manual["class"],
                        "scope": manual["scope"],
                        "status": "manual-match",
                        "balance_cents": None,
                        "anchored": False,
                    }
                )
                continue
            status = "new"
            account_class = item["class"]
            if not dry_run:
                conn.execute(
                    """
                    INSERT INTO accounts (id, name, type, last4, institution, notes, scope, class, subtype, created_at)
                    VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
                    """,
                    (
                        item["id"],
                        item["name"] or item["short_name"] or f"Account {item['last4'] or item['id'][:6]}",
                        type_for_class(account_class),
                        item["last4"],
                        item["institution"],
                        scope,
                        account_class,
                        item["subtype"],
                        now_iso(),
                    ),
                )
                audit(
                    conn,
                    "account_register",
                    field=item["id"],
                    new_value=scope,
                    actor=actor,
                    note=f"{item['institution'] or ''} {item['last4'] or ''} {account_class}".strip(),
                )
            acct_scope = scope
        else:
            acct_scope = existing["scope"]
            account_class = existing["class"]
            if not dry_run:
                if not (existing.get("name") or "").strip() and (item["name"] or item["short_name"]):
                    conn.execute("UPDATE accounts SET name = ? WHERE id = ?", (item["name"] or item["short_name"], item["id"]))
                if existing.get("class") == "other" and item["class"] != "other" and existing["scope"] != "business":
                    conn.execute(
                        "UPDATE accounts SET class = ?, type = ? WHERE id = ?",
                        (item["class"], type_for_class(item["class"]), item["id"]),
                    )
                    account_class = item["class"]
                if not existing.get("last4") and item["last4"]:
                    conn.execute("UPDATE accounts SET last4 = ? WHERE id = ?", (item["last4"], item["id"]))
        balance_cents = None
        anchored = False
        if item["current_balance"] is not None:
            try:
                balance_cents = to_cents(item["current_balance"])
            except HpgbooksError:
                balance_cents = None
            if balance_cents is not None and abs(balance_cents) <= MAX_BALANCE_CENTS and acct_scope != "excluded":
                if type_for_class(account_class) == "liability":
                    # Finance reports what is owed; anchors store a liability as the amount owed.
                    balance_cents = abs(balance_cents)
                latest = conn.execute(
                    """
                    SELECT as_of_date, balance_cents FROM balance_anchors WHERE account_id = ?
                    ORDER BY as_of_date DESC, created_at DESC, id DESC LIMIT 1
                    """,
                    (item["id"],),
                ).fetchone() if existing is not None or not dry_run else None
                duplicate = latest is not None and latest["as_of_date"] == as_of and int(latest["balance_cents"]) == balance_cents
                if not duplicate:
                    anchored = True
                    if not dry_run:
                        conn.execute(
                            """
                            INSERT INTO balance_anchors (account_id, as_of_date, balance_cents, source, note, created_at)
                            VALUES (?, ?, ?, 'finance', 'accounts discover', ?)
                            """,
                            (item["id"], as_of, balance_cents, now_iso()),
                        )
                        audit(
                            conn,
                            "balance_set",
                            field=item["id"],
                            new_value=json.dumps(
                                {"as_of_date": as_of, "balance_cents": balance_cents, "source": "finance", "note": "accounts discover"},
                                sort_keys=True,
                            ),
                            actor=actor,
                        )
        results.append(
            {
                "id": item["id"],
                "last4": item["last4"] or (existing or {}).get("last4") or "",
                "name": (existing or {}).get("display_name") or (existing or {}).get("name") or item["name"] or item["short_name"] or "",
                "institution": item["institution"] or (existing or {}).get("institution") or "",
                "class": account_class,
                "scope": acct_scope,
                "status": status,
                "balance_cents": balance_cents,
                "anchored": anchored,
            }
        )
    return results


def sync_list(conn, scope: str | None = None) -> list[dict]:
    """Accounts the daily pull should fetch, with the Finance tool and inbox file prefix."""
    rows = []
    for acct in all_accounts(conn):
        if acct["scope"] == "excluded" or not acct.get("sync_enabled", 1) or acct.get("closed"):
            continue
        if scope and acct["scope"] != scope:
            continue
        prefix = inbox_prefix(acct)
        if acct["scope"] == "personal":
            prefix = f"personal/{prefix}"
        rows.append(
            {
                "id": acct["id"],
                "scope": acct["scope"],
                "label": label(acct),
                "last4": acct.get("last4") or "",
                "class": acct["class"],
                "tool": finance_tool(acct),
                "file_prefix": prefix,
            }
        )
    return rows
