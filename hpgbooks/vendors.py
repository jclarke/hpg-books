"""Vendor display names. Ledger rows are never rewritten.

A row in vendor_aliases maps one imported spelling (alias_key) to a canonical
display name. Reports, search, and the vendor filter group by that name.
Missing table (a read-only database that has not been migrated) means no aliases.
"""

from __future__ import annotations

import json
import re

from hpgbooks.db import HpgbooksError, _table_exists, audit, now_iso

# Same expression the transaction list uses, so a filter hits the stored text.
VENDOR_SQL = (
    "CASE WHEN trim(ifnull(t.merchant_name,'')) != '' "
    "THEN trim(t.merchant_name) ELSE trim(ifnull(t.name,'')) END"
)

_STOP = frozenset(
    {
        "THE",
        "AND",
        "INC",
        "LLC",
        "CO",
        "COMPANY",
        "PAYMENT",
        "PAYMENTS",
        "ACH",
        "DEBIT",
        "CREDIT",
        "POS",
        "PURCHASE",
        "CHECKCARD",
        "CARD",
        "ONLINE",
        "TRANSFER",
        "WEB",
        "WWW",
    }
)
_MAX_NAME = 200
_MAX_MERGE = 50


def vendor_key(merchant: str | None, name: str | None = None) -> str:
    """Display key for a transaction: merchant, otherwise name, whitespace collapsed."""
    raw = (merchant or "").strip() or (name or "").strip()
    raw = re.sub(r"\s+", " ", raw)
    return raw or "(unknown)"


def normalize_name(value: str) -> str:
    if not isinstance(value, str):
        raise HpgbooksError("vendor name must be text")
    text = re.sub(r"\s+", " ", value.strip())
    if not text or text == "(unknown)":
        raise HpgbooksError("vendor name is required")
    if len(text) > _MAX_NAME:
        raise HpgbooksError("vendor name is too long")
    return text


def load_aliases(conn) -> dict[str, str]:
    """alias_key -> canonical_name. Empty when the table is not there yet."""
    if not _table_exists(conn, "vendor_aliases"):
        return {}
    rows = conn.execute("SELECT alias_key, canonical_name FROM vendor_aliases").fetchall()
    return {row["alias_key"]: row["canonical_name"] for row in rows}


def canonical_name(key: str, aliases: dict[str, str]) -> str:
    return aliases.get(key, key)


def list_aliases(conn) -> list[dict]:
    if not _table_exists(conn, "vendor_aliases"):
        return []
    rows = conn.execute(
        """
        SELECT alias_key, canonical_name, created_at, created_by
        FROM vendor_aliases
        ORDER BY canonical_name, alias_key
        """
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def group_keys(conn, vendor: str) -> list[str]:
    """Spellings that belong to the vendor a filter asked for.

    Matching is case-insensitive. A canonical name returns every spelling
    merged into it, and an alias returns that whole group.
    """
    key = normalize_name(vendor)
    aliases = load_aliases(conn)
    folded = key.casefold()
    canons = set()
    for alias, canon in aliases.items():
        if alias.casefold() == folded or canon.casefold() == folded:
            canons.add(canon)
    keys = {key}
    for alias, canon in aliases.items():
        if canon in canons or canon.casefold() == folded or alias.casefold() == folded:
            keys.add(alias)
            keys.add(canon)
    return sorted(keys)


def vendor_filter_sql(conn, vendor: str) -> tuple[str, list[str]]:
    keys = group_keys(conn, vendor)
    placeholders = ", ".join("?" for _ in keys)
    clause = f"lower({VENDOR_SQL}) IN ({placeholders})"
    return clause, [key.casefold() for key in keys]


def _changed(before: dict[str, str], after: dict[str, str]) -> dict[str, dict]:
    diff = {}
    keys = set(before) | set(after)
    for key in sorted(keys):
        old = before.get(key)
        new = after.get(key)
        if old != new:
            diff[key] = {"old": old, "new": new}
    return diff


def _group_spellings(key: str, aliases: dict[str, str]) -> set[str]:
    """The key, its canonical name, and every alias already in that group."""
    canon = aliases.get(key, key)
    spellings = {key, canon}
    for alias, name in aliases.items():
        if name == canon or alias == key or name == key:
            spellings.add(alias)
            spellings.add(name)
    return spellings


def merge_vendors(conn, names: list[str], into: str, *, actor: str) -> dict:
    """Point each spelling at one canonical name. No transaction row is updated.

    Names may be raw spellings or canonical display names. A canonical name
    brings its aliases along. Merging the same group into the same name again
    does not write a second audit row.
    """
    if not isinstance(names, list):
        raise HpgbooksError("merge needs at least two vendor names")
    if len(names) > _MAX_MERGE:
        raise HpgbooksError("too many vendor names")
    keys = [normalize_name(name) for name in names]
    if len(set(keys)) < 2:
        raise HpgbooksError("merge needs at least two vendor names")
    into_name = normalize_name(into)
    before = load_aliases(conn)
    spellings: set[str] = set()
    for key in keys:
        spellings.update(_group_spellings(key, before))
    to_delete = [key for key in spellings if key == into_name and key in before]
    to_upsert = [key for key in spellings if key != into_name and before.get(key) != into_name]
    if not to_delete and not to_upsert:
        return {"canonical": into_name, "changed": 0, "aliases": list_aliases(conn)}
    for key in to_delete:
        conn.execute("DELETE FROM vendor_aliases WHERE alias_key = ?", (key,))
    created = now_iso()
    for key in to_upsert:
        conn.execute(
            """
            INSERT INTO vendor_aliases (alias_key, canonical_name, created_at, created_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(alias_key) DO UPDATE SET
              canonical_name = excluded.canonical_name,
              created_at = excluded.created_at,
              created_by = excluded.created_by
            """,
            (key, into_name, created, actor),
        )
    after = load_aliases(conn)
    diff = _changed(before, after)
    if diff:
        audit(
            conn,
            "vendor_merge",
            field="canonical_name",
            old_value=json.dumps({key: item["old"] for key, item in diff.items()}, sort_keys=True),
            new_value=json.dumps({"into": into_name, "aliases": sorted(diff)}, sort_keys=True),
            actor=actor,
            note=into_name,
        )
    return {"canonical": into_name, "changed": len(diff), "aliases": list_aliases(conn)}


def unmerge_vendor(conn, alias: str, *, actor: str) -> dict:
    key = normalize_name(alias)
    before = load_aliases(conn)
    if key not in before:
        raise HpgbooksError(f"no alias {key}")
    conn.execute("DELETE FROM vendor_aliases WHERE alias_key = ?", (key,))
    audit(
        conn,
        "vendor_unmerge",
        field=key,
        old_value=before[key],
        new_value=None,
        actor=actor,
        note=before[key],
    )
    return {"alias": key, "canonical": before[key]}


def rename_canonical(conn, old: str, new: str, *, actor: str) -> dict:
    """Rename a canonical display name. Spellings that used it follow the new name.

    The natural spelling (rows whose merchant already equals the old name, with
    no alias row) gets an alias so those rows stay in the group.
    """
    old_name = normalize_name(old)
    new_name = normalize_name(new)
    if old_name == new_name:
        return {"canonical": new_name, "changed": 0}
    before = load_aliases(conn)
    if old_name in before and before[old_name] != old_name:
        raise HpgbooksError("rename the canonical name; unmerge an alias")
    if any(alias.casefold() == new_name.casefold() and canon != old_name for alias, canon in before.items()):
        raise HpgbooksError(f"{new_name} is already an alias of another vendor")
    targets = [alias for alias, canon in before.items() if canon == old_name]
    if not targets and old_name not in before:
        # Still record the alias from the old display name, so the rename sticks
        # even when every spelling was the name itself.
        targets = []
    for alias in targets:
        if alias == new_name:
            conn.execute("DELETE FROM vendor_aliases WHERE alias_key = ?", (alias,))
            continue
        conn.execute(
            """
            UPDATE vendor_aliases
            SET canonical_name = ?, created_at = ?, created_by = ?
            WHERE alias_key = ?
            """,
            (new_name, now_iso(), actor, alias),
        )
    if old_name != new_name:
        conn.execute(
            """
            INSERT INTO vendor_aliases (alias_key, canonical_name, created_at, created_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(alias_key) DO UPDATE SET
              canonical_name = excluded.canonical_name,
              created_at = excluded.created_at,
              created_by = excluded.created_by
            """,
            (old_name, new_name, now_iso(), actor),
        )
    after = load_aliases(conn)
    diff = _changed(before, after)
    if diff:
        audit(
            conn,
            "vendor_rename",
            field="canonical_name",
            old_value=old_name,
            new_value=new_name,
            actor=actor,
            note=new_name,
        )
    return {"canonical": new_name, "changed": len(diff)}


def _tokens(name: str) -> list[str]:
    cleaned = re.sub(r"[^A-Za-z0-9]+", " ", name).upper()
    return [token for token in cleaned.split() if len(token) >= 3 and token not in _STOP]


def _compact(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def _overlap_reason(left_tokens: list[str], right_tokens: list[str], left_compact: str, right_compact: str) -> str | None:
    shared = {token for token in set(left_tokens) & set(right_tokens) if len(token) >= 5}
    if shared:
        token = sorted(shared, key=len, reverse=True)[0]
        return f"shared {token.lower()}"
    if len(left_compact) >= 8 and len(right_compact) >= 8:
        length = 0
        for left, right in zip(left_compact, right_compact):
            if left != right:
                break
            length += 1
        if length >= 8:
            return f"same prefix {left_compact[:length].lower()}"
    return None


def _canonical_vendor(name: str, aliases: dict[str, str]) -> str | None:
    """Follow aliases to a display name that is not itself an alias.

    A blank name, "(unknown)", or a cycle that never leaves the alias table
    is not a suggestion side.
    """
    if not name or name == "(unknown)":
        return None
    seen: set[str] = set()
    current = name
    while current in aliases and aliases[current] != current:
        if current in seen:
            return None
        seen.add(current)
        nxt = aliases[current]
        if not nxt or nxt == "(unknown)":
            return None
        current = nxt
    return current


def _is_merged_alias(name: str, aliases: dict[str, str]) -> bool:
    """True when this spelling is stored as an alias of a different vendor."""
    target = aliases.get(name)
    return bool(target) and target != name


def suggest_duplicates(spending: dict[str, int], aliases: dict[str, str], limit: int = 15) -> list[dict]:
    """Pairs of canonical names that share a long token or a normalized prefix.

    spending maps a raw vendor key to spend cents. Spellings already merged
    into a canonical name are rolled up first, so a suggestion never names an
    alias and never pairs two spellings of the same vendor. The higher-spend
    canonical name is first so a one-click merge can use it.
    """
    totals: dict[str, int] = {}
    for name, spend in spending.items():
        canon = _canonical_vendor(name, aliases)
        if canon is None or _is_merged_alias(canon, aliases):
            continue
        totals[canon] = totals.get(canon, 0) + int(spend)

    items = [
        {
            "name": name,
            "canon": name,
            "spend": spend,
            "tokens": _tokens(name),
            "compact": _compact(name),
        }
        for name, spend in totals.items()
    ]
    found = []
    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            if left["canon"] == right["canon"]:
                continue
            if _is_merged_alias(left["name"], aliases) or _is_merged_alias(right["name"], aliases):
                continue
            reason = _overlap_reason(left["tokens"], right["tokens"], left["compact"], right["compact"])
            if not reason:
                continue
            ordered = sorted((left, right), key=lambda item: (-item["spend"], item["name"].lower()))
            found.append(
                {
                    "names": [ordered[0]["name"], ordered[1]["name"]],
                    "reason": reason,
                    "spend_cents": left["spend"] + right["spend"],
                }
            )
    found.sort(key=lambda item: (-item["spend_cents"], item["names"][0].lower()))
    # One suggestion per unordered pair of canonical names. Cap the list.
    return found[:limit]


def combine_vendor_hits(rows: list[dict], aliases: dict[str, str]) -> list[dict]:
    """Collapse search hits that share a canonical name.

    Each row needs name, count, spend_cents, last_date.
    """
    grouped: dict[str, dict] = {}
    for row in rows:
        name = row.get("name") or ""
        if not name:
            continue
        canon = canonical_name(vendor_key(name), aliases)
        dest = grouped.setdefault(
            canon, {"name": canon, "count": 0, "spend_cents": 0, "last_date": ""}
        )
        dest["count"] += int(row.get("count") or 0)
        dest["spend_cents"] += int(row.get("spend_cents") or 0)
        last = row.get("last_date") or ""
        if last >= dest["last_date"]:
            dest["last_date"] = last
    return sorted(grouped.values(), key=lambda item: (-item["count"], item["name"].lower()))
