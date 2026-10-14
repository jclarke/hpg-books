"""Rule, seed, and transfer classification.

Order for a non-manual row: first matching rule (priority, then id), then the
prior-analysis seed, then needs_review. match_transfers() runs after that and
may retag card payments from the configured transfer pairs. Manual rows are
never overwritten.
"""

from __future__ import annotations

import csv
import os
import re
from datetime import datetime

from hpgbooks.config import get_config
from hpgbooks.db import (
    BUSINESS_TAGS,
    CATEGORIES,
    HpgbooksError,
    audit,
    now_iso,
)
from hpgbooks.scope import scope_clause

# Optional prior-analysis seed: a CSV of txn_id, status, category, vendor rows
# from an earlier spreadsheet. Configured by [seed] prior_csv; HPGBOOKS_SEED_CSV overrides.
UNMATCHED_TAG, UNMATCHED_CATEGORY, UNMATCHED_CONFIDENCE = "needs_review", "Owner Draw", 0.4


def seed_csv_path() -> str | None:
    env = os.environ.get("HPGBOOKS_SEED_CSV")
    if env is None:
        env = os.environ.get("HPBOOKS_SEED_CSV")  # compat: pre-rename name
    if env is not None:
        return env
    return get_config().prior_csv


def seed_category_map() -> dict[str, str]:
    return get_config().prior_category_map


def pair_specs() -> list[dict]:
    """Transfer pairs from the config, with account roles resolved. Pairs whose accounts are missing are skipped."""
    cfg = get_config()
    specs = []
    for pair in cfg.transfer_pairs:
        left, right = cfg.resolve_account(pair.left_account), cfg.resolve_account(pair.right_account)
        if not left or not right:
            continue
        specs.append(
            {
                "kind": pair.kind,
                "left_account": left,
                "left_re": re.compile(pair.left_pattern, re.I),
                "right_account": right,
                "right_re": re.compile(pair.right_pattern, re.I),
                "window": pair.window,
                "classify_left": pair.classify_left,
                "matched_note": pair.matched_note,
                "unmatched_note": pair.unmatched_note,
            }
        )
    return specs


_seed_cache: dict | None = None


def reset_seed_cache() -> None:
    global _seed_cache
    _seed_cache = None


def as_dict(row) -> dict | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    return {key: row[key] for key in row.keys()}


def normalize_assignment(tag: str, category: str, confidence: float | None):
    if tag not in BUSINESS_TAGS:
        raise HpgbooksError(f"unknown business tag {tag!r}")
    if category not in CATEGORIES:
        raise HpgbooksError(f"unknown category {category!r}")
    if tag == "owner_draw":
        category = "Owner Draw"
    elif tag == "transfer":
        category = "Transfer"
    if confidence is None:
        confidence = 0.2 if tag == "needs_review" else 1.0
    confidence = float(confidence)
    if tag == "needs_review":
        confidence = min(confidence, 0.5)
    if confidence < 0 or confidence > 1:
        raise HpgbooksError("confidence must be between 0 and 1")
    return tag, category, confidence


def load_seed_index() -> dict[str, dict]:
    global _seed_cache
    if _seed_cache is not None:
        return _seed_cache
    index: dict[str, dict] = {}
    path = seed_csv_path()
    if path and os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                txn_id = row.get("txn_id") or ""
                if txn_id:
                    index[txn_id] = row
    _seed_cache = index
    return index


def load_rules(conn) -> list[tuple[dict, re.Pattern]]:
    rows = conn.execute(
        "SELECT * FROM rules WHERE active = 1 ORDER BY priority ASC, id ASC"
    ).fetchall()
    compiled = []
    for row in rows:
        item = as_dict(row)
        try:
            regex = re.compile(item["pattern"], re.IGNORECASE)
        except re.error as exc:
            raise HpgbooksError(f"rule {item['id']} has an invalid pattern") from exc
        compiled.append((item, regex))
    return compiled


def rule_matches(rule: dict, regex: re.Pattern, txn: dict) -> bool:
    if rule.get("account_id") and rule["account_id"] != txn["account_id"]:
        return False
    cents = int(txn["amount_cents"])
    sign = rule.get("amount_sign")
    if sign == "in" and cents <= 0:
        return False
    if sign == "out" and cents >= 0:
        return False
    abs_cents = abs(cents)
    if rule.get("min_amount") is not None and abs_cents < int(round(float(rule["min_amount"]) * 100)):
        return False
    if rule.get("max_amount") is not None and abs_cents > int(round(float(rule["max_amount"]) * 100)):
        return False
    field = rule.get("field") or "any"
    if field == "name":
        blobs = [txn.get("name") or ""]
    elif field == "merchant":
        blobs = [txn.get("merchant_name") or ""]
    else:
        blobs = [
            txn.get("name") or "",
            txn.get("merchant_name") or "",
            txn.get("description") or "",
        ]
    return any(regex.search(blob or "") for blob in blobs)


def _seed_decision(txn_id: str) -> dict | None:
    business = get_config().prior_business
    if not business or business not in BUSINESS_TAGS:
        return None
    row = load_seed_index().get(txn_id)
    if not row or row.get("status") != get_config().prior_status:
        return None
    category = seed_category_map().get(row.get("category") or "")
    if not category:
        return None
    vendor = (row.get("vendor") or "").strip()
    note = "seed from prior analysis"
    if vendor:
        note = f"{note}: {vendor}"
    return {
        "business_tag": business,
        "category": category,
        "source": "seed",
        "rule_id": None,
        "confidence": 0.8,
        "note": note,
    }


def decide(txn: dict, rules: list[tuple[dict, re.Pattern]]) -> dict:
    for rule, regex in rules:
        if rule_matches(rule, regex, txn):
            return {
                "business_tag": rule["business_tag"],
                "category": rule["category"],
                "source": "rule",
                "rule_id": rule["id"],
                "confidence": rule["confidence"],
                "note": rule.get("note") or "",
            }
    seeded = _seed_decision(txn["id"])
    if seeded:
        return seeded
    return {
        "business_tag": "needs_review",
        "category": "Uncategorized",
        "source": "agent",
        "rule_id": None,
        "confidence": 0.2,
        "note": "no matching rule",
    }


def _same(existing: dict, tag, category, source, rule_id, confidence, note) -> bool:
    return (
        existing["business_tag"] == tag
        and existing["category"] == category
        and existing["source"] == source
        and existing["rule_id"] == rule_id
        and abs(float(existing["confidence"]) - float(confidence)) < 1e-9
        and (existing["note"] or "") == (note or "")
    )


def write_classification(
    conn,
    txn_id: str,
    tag: str,
    category: str,
    source: str,
    confidence: float | None,
    note: str | None,
    rule_id: int | None,
    *,
    overwrite_manual: bool,
    actor: str,
    audit_write: bool,
) -> str:
    """Insert or update a classification.

    Returns 'manual' (kept), 'same', or 'wrote'.
    """
    tag, category, confidence = normalize_assignment(tag, category, confidence)
    note = note or ""
    existing = as_dict(
        conn.execute("SELECT * FROM classifications WHERE txn_id = ?", (txn_id,)).fetchone()
    )
    if existing and existing["source"] == "manual" and not overwrite_manual:
        return "manual"
    if existing and _same(existing, tag, category, source, rule_id, confidence, note):
        if audit_write:
            audit(
                conn,
                "classify",
                txn_id=txn_id,
                rule_id=rule_id,
                field="classification",
                old_value=_snap(existing),
                new_value=_snap_values(tag, category, source, note),
                actor=actor,
                note=note or None,
            )
        return "same"
    if existing:
        conn.execute(
            """
            UPDATE classifications
            SET business_tag=?, category=?, source=?, rule_id=?, confidence=?, note=?, updated_at=?
            WHERE txn_id=?
            """,
            (tag, category, source, rule_id, confidence, note, now_iso(), txn_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO classifications (
              txn_id, business_tag, category, source, rule_id, confidence, note, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (txn_id, tag, category, source, rule_id, confidence, note, now_iso()),
        )
    if audit_write:
        audit(
            conn,
            "classify",
            txn_id=txn_id,
            rule_id=rule_id,
            field="classification",
            old_value=_snap(existing) if existing else "",
            new_value=_snap_values(tag, category, source, note),
            actor=actor,
            note=note or None,
        )
    return "wrote"


def _snap(row: dict | None) -> str:
    if not row:
        return ""
    return _snap_values(row["business_tag"], row["category"], row["source"], row.get("note") or "")


def _snap_values(tag, category, source, note) -> str:
    return f"{tag}|{category}|{source}|{note or ''}"


def classify_ids(conn, txn_ids: list[str], *, overwrite_non_manual: bool) -> int:
    """Classify specific transactions. Manual rows are skipped."""
    if not txn_ids:
        return 0
    rules = load_rules(conn)
    wrote = 0
    for txn_id in txn_ids:
        existing = as_dict(
            conn.execute(
                "SELECT source FROM classifications WHERE txn_id = ?", (txn_id,)
            ).fetchone()
        )
        if existing and existing["source"] == "manual":
            continue
        if existing and not overwrite_non_manual:
            continue
        scope_sql, scope_params = scope_clause(conn, "business", "account_id")
        txn = as_dict(
            conn.execute(
                f"SELECT * FROM transactions WHERE id = ? AND {scope_sql}", (txn_id, *scope_params)
            ).fetchone()
        )
        if not txn:
            continue
        decision = decide(txn, rules)
        result = write_classification(
            conn,
            txn_id,
            decision["business_tag"],
            decision["category"],
            decision["source"],
            decision["confidence"],
            decision["note"],
            decision["rule_id"],
            overwrite_manual=False,
            actor="classify",
            audit_write=False,
        )
        if result == "wrote":
            wrote += 1
    return wrote


def classify_new(conn, txn_ids: list[str]) -> int:
    return classify_ids(conn, txn_ids, overwrite_non_manual=False)


def reclassify(conn, *, txn_ids: list[str] | None = None) -> int:
    """Re-run rules, seed, fallback, and transfer matching on non-manual rows."""
    if txn_ids is None:
        scope_sql, scope_params = scope_clause(conn, "business")
        rows = conn.execute(
            f"""
            SELECT t.id
            FROM transactions t
            LEFT JOIN classifications c ON c.txn_id = t.id
            WHERE (c.source IS NULL OR c.source != 'manual') AND {scope_sql}
            """,
            scope_params,
        ).fetchall()
        txn_ids = [row["id"] for row in rows]
    else:
        kept = []
        for txn_id in txn_ids:
            existing = as_dict(
                conn.execute(
                    "SELECT source FROM classifications WHERE txn_id = ?", (txn_id,)
                ).fetchone()
            )
            if existing and existing["source"] == "manual":
                continue
            kept.append(txn_id)
        txn_ids = kept
    wrote = classify_ids(conn, txn_ids, overwrite_non_manual=True)
    wrote += match_transfers(conn)
    return wrote


def _parse_date(value: str):
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def _pair(lefts: list[dict], rights: list[dict], window: str):
    edges = []
    for left in lefts:
        left_date = _parse_date(left["date"])
        for right in rights:
            if abs(int(left["amount_cents"])) != abs(int(right["amount_cents"])):
                continue
            delta = (_parse_date(right["date"]) - left_date).days
            if window == "abs7":
                if abs(delta) > 7:
                    continue
                rank = abs(delta)
            elif window == "after0_7":
                if delta < 0 or delta > 7:
                    continue
                rank = delta
            else:
                raise HpgbooksError(f"unknown transfer window {window}")
            edges.append((rank, left["date"], left["id"], right["date"], right["id"], left, right))
    edges.sort()
    used_left: set[str] = set()
    used_right: set[str] = set()
    pairs = []
    for _rank, _ld, _lid, _rd, _rid, left, right in edges:
        if left["id"] in used_left or right["id"] in used_right:
            continue
        used_left.add(left["id"])
        used_right.add(right["id"])
        pairs.append((left, right))
    unmatched_left = [row for row in lefts if row["id"] not in used_left]
    unmatched_right = [row for row in rights if row["id"] not in used_right]
    return pairs, unmatched_left, unmatched_right


def build_transfer_pairs(txns: list[dict]) -> list[dict]:
    """Pair inter-account payments. txns must be active."""
    results = []
    for spec in pair_specs():
        lefts = [
            txn
            for txn in txns
            if txn["account_id"] == spec["left_account"] and spec["left_re"].search(txn.get("name") or "")
        ]
        rights = [
            txn
            for txn in txns
            if txn["account_id"] == spec["right_account"] and spec["right_re"].search(txn.get("name") or "")
        ]
        pairs, unmatched_left, unmatched_right = _pair(lefts, rights, spec["window"])
        for left, right in pairs:
            results.append({"kind": spec["kind"], "status": "matched", "left": left, "right": right})
        for left in unmatched_left:
            results.append({"kind": spec["kind"], "status": "unmatched_left", "left": left, "right": None})
        for right in unmatched_right:
            results.append(
                {"kind": spec["kind"], "status": "unmatched_right", "left": None, "right": right}
            )
    return results


def match_transfers(conn) -> int:
    """Classify the paying side of pairs marked classify_left. Does not touch manual rows.

    A matched payment becomes a transfer. An unmatched one goes to review: the
    payment may be to a card that is not in the books (a personal card).
    """
    scope_sql, scope_params = scope_clause(conn, "business", "account_id")
    txns = [
        as_dict(row)
        for row in conn.execute(
            f"SELECT * FROM transactions WHERE status = 'active' AND {scope_sql}", scope_params
        ).fetchall()
    ]
    wrote = 0
    specs = {spec["kind"]: spec for spec in pair_specs() if spec["classify_left"]}
    if not specs:
        return 0
    for item in build_transfer_pairs(txns):
        spec = specs.get(item["kind"])
        if spec is None or item["left"] is None:
            continue
        left = item["left"]
        if item["status"] == "matched":
            tag, category, confidence = "transfer", "Transfer", 0.95
            note = spec["matched_note"].format(txn_id=item["right"]["id"])
        else:
            tag, category, confidence = UNMATCHED_TAG, UNMATCHED_CATEGORY, UNMATCHED_CONFIDENCE
            note = spec["unmatched_note"]
        result = write_classification(
            conn,
            left["id"],
            tag,
            category,
            "agent",
            confidence,
            note,
            None,
            overwrite_manual=False,
            actor="classify",
            audit_write=False,
        )
        if result == "wrote":
            wrote += 1
    return wrote


def resolve_txn(conn, ref: str, mode: str = "business") -> dict:
    """A transaction by id or unique prefix, among accounts in this mode only."""
    ref = ref.strip()
    if not ref:
        raise HpgbooksError("transaction id is required")
    scope_sql, scope_params = scope_clause(conn, mode, "account_id")
    exact = as_dict(
        conn.execute(
            f"SELECT * FROM transactions WHERE id = ? AND {scope_sql}", (ref, *scope_params)
        ).fetchone()
    )
    if exact:
        return exact
    rows = conn.execute(
        f"SELECT * FROM transactions WHERE id LIKE ? AND {scope_sql} ORDER BY id LIMIT 5",
        (ref + "%", *scope_params),
    ).fetchall()
    if len(rows) == 1:
        return as_dict(rows[0])
    if not rows:
        raise HpgbooksError(f"no transaction matches {ref}")
    raise HpgbooksError(f"{ref} matches more than one transaction; use a longer prefix")


def default_pattern(txn: dict) -> str:
    """Escaped merchant/name prefix with digits stripped.

    Short single tokens are anchored so a merchant like "Pro" does not match
    every description that contains those letters.
    """
    merchant = (txn.get("merchant_name") or "").strip()
    raw = merchant if len(merchant) >= 3 else (txn.get("name") or "").strip()
    raw = re.sub(r"\d+", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = re.split(r"\b(?:DES|INDN|CO ID)\b", raw, maxsplit=1)[0]
    raw = raw.strip(" *.,:-")
    if len(raw) > 42:
        cut = raw[:42]
        raw = cut.rsplit(" ", 1)[0] if " " in cut else cut
        raw = raw.strip(" *.,:-")
    if not raw:
        raise HpgbooksError("cannot derive a rule pattern from this transaction")
    escaped = re.escape(raw)
    if " " not in raw and len(raw) < 8:
        return f"^{escaped}$"
    return escaped


def add_rule(
    conn,
    *,
    pattern: str,
    tag: str,
    category: str,
    confidence: float,
    note: str = "",
    field: str = "any",
    account_id: str | None = None,
    amount_sign: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    priority: int = 12,
    created_by: str = "manual",
    actor: str = "cli",
) -> int:
    if field not in ("name", "merchant", "any"):
        raise HpgbooksError("field must be name, merchant, or any")
    if amount_sign not in (None, "", "in", "out"):
        raise HpgbooksError("amount sign must be in, out, or omitted")
    amount_sign = amount_sign or None
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise HpgbooksError(f"invalid regex: {exc}") from exc
    tag, category, confidence = normalize_assignment(tag, category, confidence)
    cur = conn.execute(
        """
        INSERT INTO rules (
          priority, pattern, field, account_id, amount_sign, min_amount, max_amount,
          business_tag, category, confidence, note, active, created_at, created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            int(priority),
            pattern,
            field,
            account_id,
            amount_sign,
            min_amount,
            max_amount,
            tag,
            category,
            confidence,
            note or "",
            now_iso(),
            created_by,
        ),
    )
    rule_id = int(cur.lastrowid)
    audit(
        conn,
        "rule_create",
        rule_id=rule_id,
        field="pattern",
        new_value=pattern,
        actor=actor,
        note=f"{tag} / {category}",
    )
    return rule_id


def disable_rule(conn, rule_id: int, actor: str = "cli") -> None:
    row = as_dict(conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone())
    if not row:
        raise HpgbooksError(f"no rule {rule_id}")
    if row["active"] == 0:
        return
    conn.execute("UPDATE rules SET active = 0 WHERE id = ?", (rule_id,))
    audit(
        conn,
        "rule_disable",
        rule_id=rule_id,
        field="active",
        old_value="1",
        new_value="0",
        actor=actor,
        note=row["pattern"],
    )


def apply_rule(conn, rule_id: int, *, skip_txn_id: str | None = None, actor: str = "classify") -> int:
    """Stamp one rule onto every other active, non-manual row it matches.

    Superseded rows stay as they were. Each row whose classification actually
    changes gets an audit_log entry. Manual classifications are left alone.
    """
    rule = as_dict(conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone())
    if not rule:
        raise HpgbooksError(f"no rule {rule_id}")
    if not rule["active"]:
        raise HpgbooksError(f"rule {rule_id} is disabled")
    tag, category, confidence = normalize_assignment(
        rule["business_tag"], rule["category"], rule["confidence"]
    )
    note = rule.get("note") or ""
    regex = re.compile(rule["pattern"], re.IGNORECASE)
    applied = 0
    scope_sql, scope_params = scope_clause(conn, "business", "account_id")
    rows = conn.execute(
        f"SELECT * FROM transactions WHERE status = 'active' AND {scope_sql}", scope_params
    ).fetchall()
    for txn in rows:
        txn = as_dict(txn)
        if skip_txn_id and txn["id"] == skip_txn_id:
            continue
        if not rule_matches(rule, regex, txn):
            continue
        existing = current_classification(conn, txn["id"])
        result = write_classification(
            conn,
            txn["id"],
            tag,
            category,
            "rule",
            confidence,
            note,
            rule_id,
            overwrite_manual=False,
            actor=actor,
            audit_write=False,
        )
        if result == "wrote":
            audit(
                conn,
                "classify",
                txn_id=txn["id"],
                rule_id=rule_id,
                field="classification",
                old_value=_snap(existing),
                new_value=_snap_values(tag, category, "rule", note),
                actor=actor,
                note="rule applied",
            )
        if result in ("wrote", "same"):
            applied += 1
    return applied


def classify_manual(
    conn,
    ref: str,
    tag: str,
    category: str,
    note: str | None,
    *,
    make_rule: bool = False,
    pattern: str | None = None,
    actor: str = "cli",
) -> tuple[dict, int]:
    txn = resolve_txn(conn, ref)
    write_classification(
        conn,
        txn["id"],
        tag,
        category,
        "manual",
        1.0,
        note or "",
        None,
        overwrite_manual=True,
        actor=actor,
        audit_write=True,
    )
    applied = 0
    if make_rule:
        pattern = pattern.strip() if pattern else default_pattern(txn)
        rule_id = add_rule(
            conn,
            pattern=pattern,
            tag=tag,
            category=category,
            confidence=0.4 if tag == "needs_review" else 0.9,
            note=note or "",
            field="any",
            priority=12,
            created_by="manual",
            actor=actor,
        )
        applied = apply_rule(conn, rule_id, skip_txn_id=txn["id"], actor=actor)
    return txn, applied


def seed_disagreements(conn) -> tuple[int, list[dict]]:
    """Rule-vs-seed disagreements for prior-analysis business rows that are in the database."""
    business = get_config().prior_business
    rules = load_rules(conn)
    disagreements = []
    checked = 0
    for txn_id, seed in load_seed_index().items():
        if seed.get("status") != get_config().prior_status:
            continue
        txn = as_dict(conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone())
        if not txn:
            continue
        expected = seed_category_map().get(seed.get("category") or "")
        if not expected:
            continue
        checked += 1
        hit = None
        for rule, regex in rules:
            if rule_matches(rule, regex, txn):
                hit = rule
                break
        if hit is None:
            continue
        if hit["business_tag"] != business or hit["category"] != expected:
            disagreements.append(
                {
                    "id": txn_id,
                    "date": txn["date"],
                    "name": txn.get("name") or "",
                    "seed_category": expected,
                    "seed_source_category": seed.get("category") or "",
                    "rule_id": hit["id"],
                    "rule_tag": hit["business_tag"],
                    "rule_category": hit["category"],
                    "pattern": hit["pattern"],
                }
            )
    disagreements.sort(key=lambda row: (row["date"], row["id"]))
    return checked, disagreements


def enable_rule(conn, rule_id: int, actor: str = "cli") -> None:
    row = as_dict(conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone())
    if not row:
        raise HpgbooksError(f"no rule {rule_id}")
    if row["active"] == 1:
        return
    try:
        re.compile(row["pattern"], re.IGNORECASE)
    except re.error as exc:
        raise HpgbooksError(f"rule {rule_id} has an invalid pattern") from exc
    conn.execute("UPDATE rules SET active = 1 WHERE id = ?", (rule_id,))
    audit(
        conn,
        "rule_enable",
        rule_id=rule_id,
        field="active",
        old_value="0",
        new_value="1",
        actor=actor,
        note=row["pattern"],
    )


def preview_rule(conn, pattern: str, *, field: str = "any", limit: int = 8) -> dict:
    """How many active transactions a pattern would match, with a few examples."""
    if field not in ("name", "merchant", "any"):
        raise HpgbooksError("field must be name, merchant, or any")
    if not pattern or len(pattern) > 200:
        raise HpgbooksError("pattern must be 1 to 200 characters")
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise HpgbooksError(f"invalid regex: {exc}") from exc
    rule = {"field": field, "account_id": None, "amount_sign": None, "min_amount": None, "max_amount": None}
    scope_sql, scope_params = scope_clause(conn, "business")
    rows = conn.execute(
        f"""
        SELECT t.id, t.date, t.amount_cents, t.name, t.merchant_name, t.description, t.account_id,
               ifnull(c.business_tag, 'needs_review') AS business_tag, c.category, c.source
        FROM transactions t
        LEFT JOIN classifications c ON c.txn_id = t.id
        WHERE t.status = 'active' AND {scope_sql}
        ORDER BY t.date DESC, t.id DESC
        """,
        scope_params,
    ).fetchall()
    matched = []
    manual = 0
    for row in rows:
        txn = as_dict(row)
        if rule_matches(rule, regex, txn):
            matched.append(txn)
            if txn.get("source") == "manual":
                manual += 1
    return {
        "count": len(matched),
        "manual": manual,
        "sample": [
            {
                "id": txn["id"],
                "date": txn["date"],
                "amount_cents": int(txn["amount_cents"]),
                "name": txn.get("name") or txn.get("merchant_name") or "",
                "business_tag": txn["business_tag"],
                "category": txn.get("category") or "",
            }
            for txn in matched[:limit]
        ],
    }


RESTORE_SOURCES = ("rule", "manual", "agent", "seed")


def current_classification(conn, txn_id: str) -> dict | None:
    row = as_dict(
        conn.execute(
            "SELECT business_tag, category, source, rule_id, confidence, note FROM classifications WHERE txn_id = ?",
            (txn_id,),
        ).fetchone()
    )
    if row is not None:
        row["note"] = row.get("note") or ""
    return row


def restore_classification(conn, txn_id: str, previous: dict | None, *, actor: str = "web") -> None:
    """Put back a classification captured before an edit (the web undo).

    `previous` is what current_classification returned, or None when the row had none.
    """
    txn = resolve_txn(conn, txn_id)
    existing = current_classification(conn, txn["id"])
    if previous is None:
        conn.execute("DELETE FROM classifications WHERE txn_id = ?", (txn["id"],))
        new_snap = ""
    else:
        source = previous.get("source")
        if source not in RESTORE_SOURCES:
            raise HpgbooksError("unknown classification source")
        rule_id = previous.get("rule_id")
        if rule_id is not None:
            if not isinstance(rule_id, int) or conn.execute(
                "SELECT 1 FROM rules WHERE id = ?", (rule_id,)
            ).fetchone() is None:
                raise HpgbooksError("unknown rule")
        note = previous.get("note") or ""
        if not isinstance(note, str) or len(note) > 500:
            raise HpgbooksError("note is too long")
        tag, category, confidence = normalize_assignment(
            previous.get("business_tag"), previous.get("category"), previous.get("confidence")
        )
        conn.execute(
            """
            INSERT INTO classifications (txn_id, business_tag, category, source, rule_id, confidence, note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(txn_id) DO UPDATE SET
              business_tag = excluded.business_tag,
              category = excluded.category,
              source = excluded.source,
              rule_id = excluded.rule_id,
              confidence = excluded.confidence,
              note = excluded.note,
              updated_at = excluded.updated_at
            """,
            (txn["id"], tag, category, source, rule_id, confidence, note, now_iso()),
        )
        new_snap = _snap_values(tag, category, source, note)
    audit(
        conn,
        "classify_undo",
        txn_id=txn["id"],
        field="classification",
        old_value=_snap(existing),
        new_value=new_snap,
        actor=actor,
    )
