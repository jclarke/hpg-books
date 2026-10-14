"""Personal writes: categorize, split, notes, tags, rules, categories, budgets,
goals, recurring status. Each one writes an audit row (action personal_*).

Every transaction write checks that the row belongs to a personal account; a
business id is "no such transaction". Synthesized owner-draw rows (biz:...)
are read-only here, so business classifications can never change from
personal mode.
"""

from __future__ import annotations

import json
import re

from hpgbooks.classify import as_dict, rule_matches
from hpgbooks.db import HpgbooksError, audit, now_iso
from hpgbooks.personal.classify import (
    apply_transfers,
    categories,
    current,
    load_personal,
    load_personal_rules,
    personal_accounts,
    reclassify_personal,
    write_personal,
)
from hpgbooks.personal.merchants import merchant_key, rename_merchant
from hpgbooks.reports import parse_month, prev_month, require_date

MAX_NOTE = 500
MAX_TAGS = 20
TAG_RE = re.compile(r"^[\w &+\-.'/]{1,40}$")
CADENCES = ("weekly", "biweekly", "monthly", "quarterly", "annual")
RECURRING_STATUSES = ("active", "cancelled", "ignored", "confirmed")


def personal_txn(conn, txn_id: str) -> dict:
    if not isinstance(txn_id, str) or txn_id.startswith("biz:"):
        raise HpgbooksError("no such transaction")
    row = conn.execute(
        """
        SELECT t.* FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE t.id = ? AND a.scope = 'personal'
        """,
        (txn_id,),
    ).fetchone()
    if row is None:
        raise HpgbooksError("no such transaction")
    return as_dict(row)


def _category(conn, category_id) -> dict:
    if isinstance(category_id, bool) or not isinstance(category_id, int):
        raise HpgbooksError("category is required")
    cat = categories(conn).get(category_id)
    if cat is None:
        raise HpgbooksError("no such category")
    return cat


def _note(note) -> str | None:
    if note is None:
        return None
    if not isinstance(note, str):
        raise HpgbooksError("note must be text")
    if len(note) > MAX_NOTE:
        raise HpgbooksError("note is too long")
    return note.strip()


def categorize(conn, txn_id: str, category_id: int, note=None, *, actor: str) -> dict:
    txn = personal_txn(conn, txn_id)
    _category(conn, category_id)
    note = _note(note)
    existing = current(conn, txn["id"]) or {}
    write_personal(
        conn,
        txn["id"],
        category_id,
        "manual",
        1.0,
        note if note is not None else existing.get("note"),
        merchant_display=existing.get("merchant_display"),
        transfer_pair=existing.get("transfer_pair"),
        overwrite_manual=True,
        actor=actor,
        audit_write=True,
    )
    return current(conn, txn["id"])


def set_note(conn, txn_id: str, note: str, *, actor: str) -> dict:
    txn = personal_txn(conn, txn_id)
    note = _note(note) or ""
    existing = current(conn, txn["id"])
    old = (existing or {}).get("note") or ""
    if existing is None:
        from hpgbooks.personal.classify import uncategorized_id

        conn.execute(
            "INSERT INTO p_classifications (txn_id, category_id, source, confidence, note, updated_at) VALUES (?, ?, 'agent', 0.2, ?, ?)",
            (txn["id"], uncategorized_id(conn), note, now_iso()),
        )
    else:
        conn.execute("UPDATE p_classifications SET note = ?, updated_at = ? WHERE txn_id = ?", (note, now_iso(), txn["id"]))
    if old != note:
        audit(conn, "personal_note", txn_id=txn["id"], field="note", old_value=old, new_value=note, actor=actor)
    return current(conn, txn["id"])


def set_tags(conn, txn_id: str, tags, *, actor: str) -> list[str]:
    txn = personal_txn(conn, txn_id)
    if not isinstance(tags, list) or len(tags) > MAX_TAGS:
        raise HpgbooksError(f"tags must be a list of up to {MAX_TAGS}")
    clean = []
    for tag in tags:
        if not isinstance(tag, str) or not TAG_RE.fullmatch(tag.strip()):
            raise HpgbooksError("tags are 1 to 40 letters, digits, or spaces")
        if tag.strip().lower() not in [item.lower() for item in clean]:
            clean.append(tag.strip())
    old = [row["name"] for row in conn.execute(
        "SELECT g.name FROM p_txn_tags t JOIN p_tags g ON g.id = t.tag_id WHERE t.txn_id = ? ORDER BY g.name", (txn["id"],)
    )]
    conn.execute("DELETE FROM p_txn_tags WHERE txn_id = ?", (txn["id"],))
    for tag in clean:
        conn.execute("INSERT OR IGNORE INTO p_tags (name) VALUES (?)", (tag,))
        tag_id = conn.execute("SELECT id FROM p_tags WHERE name = ?", (tag,)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO p_txn_tags (txn_id, tag_id) VALUES (?, ?)", (txn["id"], tag_id))
    new = sorted(clean, key=str.lower)
    if sorted(old, key=str.lower) != new:
        audit(conn, "personal_tags", txn_id=txn["id"], field="tags", old_value=", ".join(old), new_value=", ".join(new), actor=actor)
    return new


def set_splits(conn, txn_id: str, parts, *, actor: str) -> list[dict]:
    """Replace a row's splits. Parts must sum exactly to the row amount; an empty list clears them."""
    txn = personal_txn(conn, txn_id)
    if not isinstance(parts, list) or len(parts) > 20:
        raise HpgbooksError("splits must be a list of up to 20 parts")
    clean = []
    for part in parts:
        if not isinstance(part, dict):
            raise HpgbooksError("invalid split")
        cents = part.get("amount_cents")
        if isinstance(cents, bool) or not isinstance(cents, int) or cents == 0:
            raise HpgbooksError("each split needs a non-zero amount in cents")
        _category(conn, part.get("category_id"))
        clean.append({"category_id": part["category_id"], "amount_cents": cents, "note": _note(part.get("note")) or ""})
    if clean:
        if len(clean) < 2:
            raise HpgbooksError("a split needs at least two parts")
        if sum(part["amount_cents"] for part in clean) != int(txn["amount_cents"]):
            raise HpgbooksError("split amounts must add up to the transaction amount")
    old = [dict(row) for row in conn.execute("SELECT category_id, amount_cents, note FROM p_splits WHERE txn_id = ? ORDER BY id", (txn["id"],))]
    conn.execute("DELETE FROM p_splits WHERE txn_id = ?", (txn["id"],))
    for part in clean:
        conn.execute(
            "INSERT INTO p_splits (txn_id, category_id, amount_cents, note) VALUES (?, ?, ?, ?)",
            (txn["id"], part["category_id"], part["amount_cents"], part["note"]),
        )
    audit(
        conn,
        "personal_split",
        txn_id=txn["id"],
        field="splits",
        old_value=json.dumps(old, sort_keys=True),
        new_value=json.dumps(clean, sort_keys=True),
        actor=actor,
    )
    return clean


def mark_transfer(conn, txn_id: str, *, actor: str, name: str = "Internal transfer") -> dict:
    from hpgbooks.personal.classify import transfer_category_id

    category = transfer_category_id(conn, name)
    if category is None:
        raise HpgbooksError("no such transfer category")
    return categorize(conn, txn_id, category, actor=actor)


def rename(conn, txn_id: str, display_name: str, *, actor: str) -> dict:
    txn = personal_txn(conn, txn_id)
    key = merchant_key(txn.get("merchant_name"), txn.get("name"))
    result = rename_merchant(conn, key, display_name, actor=actor)
    matching = sum(
        1 for row in load_personal(conn, include_synthesized=False) if row["merchant_key"] == key
    )
    return {**result, "matching": matching}


# --- rules --------------------------------------------------------------------


def _rule_fields(conn, body: dict) -> dict:
    pattern = body.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > 200:
        raise HpgbooksError("pattern must be 1 to 200 characters")
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise HpgbooksError(f"invalid regex: {exc}") from exc
    field = body.get("field") or "any"
    if field not in ("name", "merchant", "any"):
        raise HpgbooksError("field must be name, merchant, or any")
    sign = body.get("amount_sign") or None
    if sign not in (None, "in", "out"):
        raise HpgbooksError("amount sign must be in, out, or empty")
    account_id = body.get("account_id") or None
    if account_id is not None and account_id not in personal_accounts(conn):
        raise HpgbooksError("no such personal account")
    out = {"pattern": pattern.strip(), "field": field, "amount_sign": sign, "account_id": account_id}
    for key in ("min_amount", "max_amount"):
        value = body.get(key)
        if value in (None, ""):
            out[key] = None
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise HpgbooksError(f"{key} must be a positive number")
        else:
            out[key] = float(value)
    return out


def preview_rule(conn, body: dict, limit: int = 8) -> dict:
    """How many personal rows a rule would match, and how many would change category."""
    fields = _rule_fields(conn, body)
    category_id = body.get("category_id")
    regex = re.compile(fields["pattern"], re.IGNORECASE)
    matched, changes, manual = [], 0, 0
    for row in load_personal(conn, include_synthesized=False):
        txn = {"account_id": row["account_id"], "amount_cents": row["amount_cents"], "name": row["name"], "merchant_name": row["merchant"], "description": ""}
        if not rule_matches(fields, regex, txn):
            continue
        matched.append(row)
        if row["source"] == "manual":
            manual += 1
        elif category_id is not None and row["category_id"] != category_id:
            changes += 1
    return {
        "count": len(matched),
        "would_change": changes,
        "manual": manual,
        "sample": [
            {"id": row["id"], "date": row["date"], "amount_cents": row["amount_cents"], "name": row["name"], "category": row["category"]}
            for row in sorted(matched, key=lambda item: item["date"], reverse=True)[:limit]
        ],
    }


def add_rule(conn, body: dict, *, actor: str, apply: bool = True) -> dict:
    fields = _rule_fields(conn, body)
    cat = _category(conn, body.get("category_id"))
    priority = body.get("priority", 12)
    if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 1000:
        raise HpgbooksError("priority must be 0 to 1000")
    confidence = body.get("confidence", 0.9)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise HpgbooksError("confidence must be between 0 and 1")
    rename_to = body.get("merchant_rename")
    if rename_to is not None and (not isinstance(rename_to, str) or len(rename_to) > 120):
        raise HpgbooksError("merchant rename is too long")
    note = _note(body.get("note")) or ""
    cur = conn.execute(
        """
        INSERT INTO p_rules (priority, pattern, field, account_id, amount_sign, min_amount, max_amount, category_id,
                             merchant_rename, confidence, active, note, created_at, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'manual')
        """,
        (
            priority, fields["pattern"], fields["field"], fields["account_id"], fields["amount_sign"],
            fields["min_amount"], fields["max_amount"], cat["id"], (rename_to or "").strip() or None,
            float(confidence), note, now_iso(),
        ),
    )
    rule_id = int(cur.lastrowid)
    audit(conn, "personal_rule_create", rule_id=rule_id, field="pattern", new_value=fields["pattern"], actor=actor, note=f"{cat['group_name']} / {cat['name']}")
    changed = reclassify_personal(conn) if apply else 0
    return {"id": rule_id, "changed": changed}


def set_rule_active(conn, rule_id: int, active: bool, *, actor: str) -> None:
    row = conn.execute("SELECT active, pattern FROM p_rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        raise HpgbooksError("no such rule")
    if bool(row["active"]) == active:
        return
    conn.execute("UPDATE p_rules SET active = ? WHERE id = ?", (1 if active else 0, rule_id))
    audit(
        conn,
        "personal_rule_enable" if active else "personal_rule_disable",
        rule_id=rule_id,
        field="active",
        old_value=str(int(row["active"])),
        new_value=str(int(active)),
        actor=actor,
        note=row["pattern"],
    )
    reclassify_personal(conn)


def list_rules(conn) -> list[dict]:
    cats = categories(conn)
    hits = {
        row["rule_id"]: int(row["n"])
        for row in conn.execute("SELECT rule_id, COUNT(*) AS n FROM p_classifications WHERE rule_id IS NOT NULL GROUP BY rule_id")
    }
    out = []
    for row in conn.execute("SELECT * FROM p_rules ORDER BY priority, id"):
        item = as_dict(row)
        cat = cats.get(item["category_id"]) or {}
        item["category"] = cat.get("name") or ""
        item["group"] = cat.get("group_name") or ""
        item["active"] = bool(item["active"])
        item["hits"] = hits.get(item["id"], 0)
        out.append(item)
    return out


# --- categories -------------------------------------------------------------------


def _name(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 60:
        raise HpgbooksError(f"{label} must be 1 to 60 characters")
    return re.sub(r"\s+", " ", value.strip())


def add_category(conn, body: dict, *, actor: str) -> dict:
    name = _name(body.get("name"), "name")
    group = _name(body.get("group_name") or name, "group")
    kind = body.get("kind") or "expense"
    if kind not in ("expense", "income", "transfer"):
        raise HpgbooksError("kind must be expense, income, or transfer")
    color = body.get("color") or ""
    if not isinstance(color, str) or not re.fullmatch(r"(#[0-9a-fA-F]{6})?", color):
        raise HpgbooksError("color must be #RRGGBB")
    if conn.execute("SELECT 1 FROM p_categories WHERE group_name = ? AND name = ?", (group, name)).fetchone():
        raise HpgbooksError("that category already exists")
    sort = int(conn.execute("SELECT ifnull(MAX(sort), 0) + 10 FROM p_categories").fetchone()[0])
    cur = conn.execute(
        "INSERT INTO p_categories (name, group_name, kind, color, sort, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (name, group, kind, color, sort, now_iso()),
    )
    audit(conn, "personal_category", field="create", new_value=f"{group} / {name}", actor=actor)
    return categories(conn)[int(cur.lastrowid)]


def update_category(conn, category_id: int, body: dict, *, actor: str) -> dict:
    cat = _category(conn, category_id)
    changes = {}
    if "name" in body:
        changes["name"] = _name(body["name"], "name")
    if "group_name" in body:
        changes["group_name"] = _name(body["group_name"], "group")
    if "hidden" in body:
        if not isinstance(body["hidden"], bool):
            raise HpgbooksError("hidden must be true or false")
        changes["hidden"] = 1 if body["hidden"] else 0
    if "color" in body:
        if not isinstance(body["color"], str) or not re.fullmatch(r"(#[0-9a-fA-F]{6})?", body["color"]):
            raise HpgbooksError("color must be #RRGGBB")
        changes["color"] = body["color"]
    unknown = set(body) - {"name", "group_name", "hidden", "color"}
    if unknown:
        raise HpgbooksError("unknown category field")
    if cat["is_system"] and ({"name", "group_name", "hidden"} & set(changes)):
        raise HpgbooksError("system categories cannot be renamed or hidden")
    if not changes:
        return cat
    sets = ", ".join(f"{key} = ?" for key in changes)
    try:
        conn.execute(f"UPDATE p_categories SET {sets} WHERE id = ?", (*changes.values(), category_id))
    except Exception as exc:
        raise HpgbooksError("that category already exists") from exc
    audit(
        conn,
        "personal_category",
        field=str(category_id),
        old_value=json.dumps({key: cat[key] for key in changes}, sort_keys=True),
        new_value=json.dumps(changes, sort_keys=True),
        actor=actor,
    )
    return categories(conn)[category_id]


# --- budgets ------------------------------------------------------------------


def set_budget(conn, body: dict, *, actor: str) -> dict:
    category_id = body.get("category_id")
    group = body.get("group_name")
    if (category_id is None) == (group is None):
        raise HpgbooksError("give a category or a group, not both")
    if category_id is not None:
        cat = _category(conn, category_id)
        if cat["kind"] != "expense":
            raise HpgbooksError("budgets are for expense categories")
    else:
        group = _name(group, "group")
        if not conn.execute("SELECT 1 FROM p_categories WHERE group_name = ? AND kind = 'expense'", (group,)).fetchone():
            raise HpgbooksError("no such expense group")
    month = body.get("month")
    month = parse_month(month) if month else None
    amount = body.get("amount_cents")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0 or amount > 10_000_000_00:
        raise HpgbooksError("amount_cents must be a whole number of cents from 0 to 1,000,000,000")
    rollover = body.get("rollover", False)
    if not isinstance(rollover, bool):
        raise HpgbooksError("rollover must be true or false")
    existing = conn.execute(
        "SELECT * FROM p_budgets WHERE ifnull(category_id, -1) = ? AND ifnull(group_name, '') = ? AND ifnull(month, '') = ?",
        (category_id if category_id is not None else -1, group or "", month or ""),
    ).fetchone()
    old = json.dumps({"amount_cents": existing["amount_cents"], "rollover": bool(existing["rollover"])}) if existing else None
    if existing:
        conn.execute(
            "UPDATE p_budgets SET amount_cents = ?, rollover = ?, updated_at = ? WHERE id = ?",
            (amount, 1 if rollover else 0, now_iso(), existing["id"]),
        )
        budget_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO p_budgets (category_id, group_name, month, amount_cents, rollover, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (category_id, group, month, amount, 1 if rollover else 0, now_iso()),
        )
        budget_id = int(cur.lastrowid)
    audit(
        conn,
        "personal_budget",
        field=f"{'category ' + str(category_id) if category_id is not None else 'group ' + group} {month or 'default'}",
        old_value=old,
        new_value=json.dumps({"amount_cents": amount, "rollover": rollover}),
        actor=actor,
    )
    return {"id": budget_id}


def delete_budget(conn, budget_id: int, *, actor: str) -> None:
    row = conn.execute("SELECT * FROM p_budgets WHERE id = ?", (budget_id,)).fetchone()
    if row is None:
        raise HpgbooksError("no such budget")
    conn.execute("DELETE FROM p_budgets WHERE id = ?", (budget_id,))
    audit(conn, "personal_budget", field=f"delete {budget_id}", old_value=json.dumps({"amount_cents": row["amount_cents"]}), actor=actor)


def copy_budgets(conn, month: str, *, actor: str) -> int:
    """Give `month` its own copy of every budget in effect for the month before."""
    from hpgbooks.personal.analytics import _budget_rows, _effective

    month = parse_month(month)
    source = _effective(_budget_rows(conn), prev_month(month))
    target = {key for key, row in _effective(_budget_rows(conn), month).items() if row["month"] == month}
    copied = 0
    for key, row in source.items():
        if key in target:
            continue
        body = {"amount_cents": int(row["amount_cents"]), "rollover": bool(row["rollover"]), "month": month}
        body["category_id" if key[0] == "category" else "group_name"] = key[1]
        set_budget(conn, body, actor=actor)
        copied += 1
    return copied


def apply_average(conn, month: str, category_ids: list[int] | None, *, actor: str) -> int:
    from hpgbooks.personal.analytics import average_suggestions

    count = 0
    for item in average_suggestions(conn, month):
        if category_ids is not None and item["category_id"] not in category_ids:
            continue
        if categories(conn).get(item["category_id"], {}).get("kind") != "expense":
            continue
        set_budget(conn, {"category_id": item["category_id"], "month": parse_month(month), "amount_cents": item["average_cents"]}, actor=actor)
        count += 1
    return count


# --- goals --------------------------------------------------------------------


def _cents(value, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > 10_000_000_000:
        raise HpgbooksError(f"{label} must be a whole number of cents")
    return value


def save_goal(conn, body: dict, goal_id: int | None = None, *, actor: str) -> dict:
    existing = None
    if goal_id is not None:
        existing = conn.execute("SELECT * FROM p_goals WHERE id = ?", (goal_id,)).fetchone()
        if existing is None:
            raise HpgbooksError("no such goal")
    merged = {**({key: existing[key] for key in existing.keys()} if existing else {}), **body}
    name = _name(merged.get("name"), "name")
    target = _cents(merged.get("target_cents"), "target", minimum=1)
    target_date = merged.get("target_date") or None
    if target_date:
        target_date = require_date(target_date)
    account_id = merged.get("account_id") or None
    if account_id is not None and account_id not in personal_accounts(conn):
        raise HpgbooksError("no such personal account")
    manual = _cents(merged.get("manual_current_cents") or 0, "current amount")
    contribution = _cents(merged.get("monthly_contribution_cents") or 0, "monthly contribution")
    archived = merged.get("archived", 0)
    archived = 1 if archived in (True, 1) else 0
    values = (name, target, target_date, account_id, manual, contribution, archived)
    if existing:
        conn.execute(
            """
            UPDATE p_goals SET name = ?, target_cents = ?, target_date = ?, account_id = ?, manual_current_cents = ?,
                   monthly_contribution_cents = ?, archived = ?, updated_at = ? WHERE id = ?
            """,
            (*values, now_iso(), goal_id),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO p_goals (name, target_cents, target_date, account_id, manual_current_cents,
                                 monthly_contribution_cents, archived, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*values, now_iso(), now_iso()),
        )
        goal_id = int(cur.lastrowid)
    audit(conn, "personal_goal", field=str(goal_id), new_value=json.dumps(dict(zip(
        ("name", "target_cents", "target_date", "account_id", "manual_current_cents", "monthly_contribution_cents", "archived"), values
    )), sort_keys=True), actor=actor)
    return {"id": goal_id}


# --- recurring ----------------------------------------------------------------


def update_recurring(conn, series_key: str, body: dict, *, actor: str) -> dict:
    from hpgbooks.personal.analytics import refresh_recurring

    if not isinstance(series_key, str) or not series_key or len(series_key) > 300:
        raise HpgbooksError("no such recurring item")
    row = conn.execute("SELECT * FROM p_recurring WHERE series_key = ?", (series_key,)).fetchone()
    if row is None:
        refresh_recurring(conn)
        row = conn.execute("SELECT * FROM p_recurring WHERE series_key = ?", (series_key,)).fetchone()
    if row is None:
        raise HpgbooksError("no such recurring item")
    unknown = set(body) - {"status", "cadence", "notes"}
    if unknown:
        raise HpgbooksError("unknown recurring field")
    changes = {}
    if "status" in body:
        if body["status"] not in RECURRING_STATUSES:
            raise HpgbooksError("status must be active, cancelled, ignored, or confirmed")
        changes["status"] = body["status"]
        changes["user_confirmed"] = 1 if body["status"] == "confirmed" else row["user_confirmed"]
    if "cadence" in body:
        if body["cadence"] not in CADENCES:
            raise HpgbooksError("cadence must be weekly, biweekly, monthly, quarterly, or annual")
        changes["cadence"] = body["cadence"]
        changes["cadence_locked"] = 1
    if "notes" in body:
        changes["notes"] = _note(body["notes"]) or ""
    if not changes:
        raise HpgbooksError("nothing to change")
    sets = ", ".join(f"{key} = ?" for key in changes)
    conn.execute(f"UPDATE p_recurring SET {sets}, updated_at = ? WHERE series_key = ?", (*changes.values(), now_iso(), series_key))
    audit(
        conn,
        "personal_recurring",
        field=series_key,
        old_value=json.dumps({key: row[key] for key in changes}, sort_keys=True),
        new_value=json.dumps(changes, sort_keys=True),
        actor=actor,
    )
    return {"series_key": series_key, **changes}


def bulk_categorize(conn, txn_ids: list[str], category_id: int, *, actor: str) -> int:
    for txn_id in txn_ids:
        categorize(conn, txn_id, category_id, actor=actor)
    apply_transfers(conn)
    return len(txn_ids)


def apply_to_similar(conn, txn_id: str, category_id: int, *, actor: str) -> int:
    """Categorize every non-manual row with the same merchant key (review queue helper)."""
    txn = personal_txn(conn, txn_id)
    key = merchant_key(txn.get("merchant_name"), txn.get("name"))
    count = 0
    for row in load_personal(conn, include_synthesized=False):
        if row["merchant_key"] != key or (row["source"] == "manual" and row["id"] != txn["id"]):
            continue
        categorize(conn, row["id"], category_id, actor=actor)
        count += 1
    return count


def rules_for_matching(conn):
    return load_personal_rules(conn)
