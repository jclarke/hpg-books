"""Personal classification, transfer pairing, owner draws, and the row loader.

Order for a personal row: manual > transfer pair > first matching personal rule
(priority, then id) > merchant default category > Uncategorized (review).
A manual classification is never replaced by rules, transfer detection, or import.

Sign convention (same as the ledger): amount_cents > 0 is money into the
account. On a card or loan an inflow is a payment that lowers what is owed.
Spending is outflow in expense categories, shown positive; a refund in an
expense category nets against it. Transfers and owner-draw funding are never
spending, and transfers are never income.

Owner draws: a business-account row tagged owner_draw (or transfer) in the
business books and a personal-account deposit of the same amount within 3 days
are one transfer. The deposit becomes "Owner draws" funding on the personal side
and the business row is not counted again. A business draw with no personal leg
is synthesized as an Owner draws funding row (read-only, from_business). If it
was paid straight to a merchant (a mortgage servicer, not a transfer), a matching
expense row is synthesized too, categorized by the personal rules. None of this
touches business classifications, P&L, or totals.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from hpgbooks.classify import as_dict, rule_matches
from hpgbooks.db import HpgbooksError, _table_exists, audit, now_iso
from hpgbooks.personal.merchants import display_for, load_aliases, merchant_key
from hpgbooks.personal.seed import OWNER_DRAWS, UNCATEGORIZED, ensure_seeded
from hpgbooks.scope import has_scope, label

REVIEW_THRESHOLD = 0.6
TRANSFER_WINDOW_DAYS = 3
TRANSFER_WORDS = re.compile(
    r"TRANSFER|XFER|PAYMENT|PYMT|PMT|DEPOSIT|WITHDRAW|CONTRIBUTION|AUTOPAY|ONLINE BANKING|ZELLE|VENMO", re.I
)
# A business draw that names a transfer went to a bank account; anything else was paid to a payee.
DRAW_TRANSFER_LIKE = re.compile(r"TRANSFER|XFER|TO CHK|TO SAV|ZELLE|ONLINE BANKING", re.I)
SOURCES = ("rule", "manual", "agent", "transfer")


# --- categories ---------------------------------------------------------------


def categories(conn) -> dict[int, dict]:
    if not _readonly(conn):
        ensure_seeded(conn)
    return {
        int(row["id"]): {key: row[key] for key in row.keys()}
        for row in conn.execute("SELECT * FROM p_categories ORDER BY sort, id")
    }


def _readonly(conn) -> bool:
    try:
        return bool(conn.execute("PRAGMA query_only").fetchone()[0])
    except Exception:
        return False


def category_id(conn, group: str, name: str) -> int | None:
    row = conn.execute("SELECT id FROM p_categories WHERE group_name = ? AND name = ?", (group, name)).fetchone()
    return int(row["id"]) if row else None


def uncategorized_id(conn) -> int | None:
    return category_id(conn, *UNCATEGORIZED)


def owner_draws_id(conn) -> int | None:
    return category_id(conn, *OWNER_DRAWS)


def transfer_category_id(conn, name: str) -> int | None:
    return category_id(conn, "Transfers", name)


# --- personal accounts --------------------------------------------------------


def personal_accounts(conn) -> dict[str, dict]:
    from hpgbooks.scope import accounts_in

    return {acct["id"]: acct for acct in accounts_in(conn, "personal")}


def personal_ready(conn) -> bool:
    return has_scope(conn) and _table_exists(conn, "p_classifications")


# --- rules --------------------------------------------------------------------


def load_personal_rules(conn) -> list[tuple[dict, re.Pattern]]:
    rows = conn.execute("SELECT * FROM p_rules WHERE active = 1 ORDER BY priority ASC, id ASC").fetchall()
    compiled = []
    for row in rows:
        item = as_dict(row)
        try:
            compiled.append((item, re.compile(item["pattern"], re.IGNORECASE)))
        except re.error:
            continue
    return compiled


def decide_personal(txn: dict, rules, aliases: dict, fallback_id: int | None) -> dict:
    """Category for a non-manual personal row (no transfer pairing here)."""
    for rule, regex in rules:
        if rule_matches(rule, regex, txn):
            return {
                "category_id": int(rule["category_id"]),
                "source": "rule",
                "rule_id": int(rule["id"]),
                "confidence": float(rule["confidence"]),
                "merchant_display": rule.get("merchant_rename") or None,
                "note": rule.get("note") or "",
            }
    key = merchant_key(txn.get("merchant_name"), txn.get("name"))
    alias = aliases.get(key)
    if alias and alias.get("default_category_id"):
        return {
            "category_id": int(alias["default_category_id"]),
            "source": "agent",
            "rule_id": None,
            "confidence": 0.8,
            "merchant_display": None,
            "note": "merchant default category",
        }
    return {
        "category_id": fallback_id,
        "source": "agent",
        "rule_id": None,
        "confidence": 0.2,
        "merchant_display": None,
        "note": "no matching rule",
    }


def current(conn, txn_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM p_classifications WHERE txn_id = ?", (txn_id,)).fetchone()
    return as_dict(row)


def _snap(row: dict | None) -> str:
    if not row:
        return ""
    return f"{row.get('category_id') or ''}|{row.get('source') or ''}|{row.get('note') or ''}"


def write_personal(
    conn,
    txn_id: str,
    category_id: int | None,
    source: str,
    confidence: float,
    note: str | None = None,
    *,
    rule_id: int | None = None,
    merchant_display: str | None = None,
    transfer_pair: str | None = None,
    overwrite_manual: bool = False,
    actor: str = "classify",
    audit_write: bool = False,
    keep_note: bool = True,
) -> str:
    """Insert or update a personal classification. Returns 'manual', 'same', or 'wrote'."""
    if source not in SOURCES:
        raise HpgbooksError("unknown classification source")
    existing = current(conn, txn_id)
    if existing and existing["source"] == "manual" and not overwrite_manual:
        return "manual"
    if keep_note and existing and note is None:
        note = existing.get("note")
    if keep_note and existing and merchant_display is None:
        merchant_display = existing.get("merchant_display") if existing.get("source") == "manual" else merchant_display
    values = (category_id, source, rule_id, merchant_display, float(confidence), note or "", transfer_pair)
    if existing and (
        existing["category_id"],
        existing["source"],
        existing["rule_id"],
        existing["merchant_display"],
        float(existing["confidence"]),
        existing["note"] or "",
        existing["transfer_pair"],
    ) == values:
        return "same"
    conn.execute(
        """
        INSERT INTO p_classifications (txn_id, category_id, source, rule_id, merchant_display, confidence, note, transfer_pair, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(txn_id) DO UPDATE SET
          category_id = excluded.category_id, source = excluded.source, rule_id = excluded.rule_id,
          merchant_display = excluded.merchant_display, confidence = excluded.confidence,
          note = excluded.note, transfer_pair = excluded.transfer_pair, updated_at = excluded.updated_at
        """,
        (txn_id, *values, now_iso()),
    )
    if audit_write:
        audit(
            conn,
            "personal_classify",
            txn_id=txn_id,
            rule_id=rule_id,
            field="category",
            old_value=_snap(existing),
            new_value=_snap({"category_id": category_id, "source": source, "note": note}),
            actor=actor,
            note=note or None,
        )
    return "wrote"


def _personal_txns(conn, ids: list[str] | None = None, *, active_only: bool = True) -> list[dict]:
    clauses = ["a.scope = 'personal'"]
    params: list = []
    if active_only:
        clauses.append("t.status = 'active'")
    if ids is not None:
        if not ids:
            return []
        clauses.append(f"t.id IN ({', '.join('?' for _ in ids)})")
        params.extend(ids)
    rows = conn.execute(
        f"""
        SELECT t.* FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE {' AND '.join(clauses)}
        ORDER BY t.date, t.id
        """,
        params,
    ).fetchall()
    return [as_dict(row) for row in rows]


def classify_new_personal(conn, txn_ids: list[str]) -> int:
    """Rules for newly imported personal rows. Rows already classified are left alone."""
    ensure_seeded(conn)
    rules = load_personal_rules(conn)
    aliases = load_aliases(conn)
    fallback = uncategorized_id(conn)
    wrote = 0
    for txn in _personal_txns(conn, txn_ids, active_only=False):
        if current(conn, txn["id"]):
            continue
        decision = decide_personal(txn, rules, aliases, fallback)
        result = write_personal(
            conn,
            txn["id"],
            decision["category_id"],
            decision["source"],
            decision["confidence"],
            decision["note"],
            rule_id=decision["rule_id"],
            merchant_display=decision["merchant_display"],
        )
        wrote += result == "wrote"
    return wrote


def reclassify_personal(conn) -> int:
    """Re-run personal rules on every non-manual personal row, then transfer pairing."""
    ensure_seeded(conn)
    rules = load_personal_rules(conn)
    aliases = load_aliases(conn)
    fallback = uncategorized_id(conn)
    wrote = 0
    for txn in _personal_txns(conn):
        existing = current(conn, txn["id"])
        if existing and existing["source"] == "manual":
            continue
        decision = decide_personal(txn, rules, aliases, fallback)
        result = write_personal(
            conn,
            txn["id"],
            decision["category_id"],
            decision["source"],
            decision["confidence"],
            existing.get("note") if existing else decision["note"],
            rule_id=decision["rule_id"],
            merchant_display=decision["merchant_display"],
        )
        wrote += result == "wrote"
    wrote += apply_transfers(conn)
    return wrote


def carry_manual_personal(conn, old_id: str, new_id: str) -> None:
    """A pending row that posts under a new id keeps its manual category, note, and tags."""
    old = current(conn, old_id)
    if old and old["source"] == "manual":
        write_personal(
            conn, new_id, old["category_id"], "manual", 1.0, old.get("note") or "",
            merchant_display=old.get("merchant_display"), overwrite_manual=False,
            actor="import", audit_write=True,
        )
    conn.execute(
        "INSERT OR IGNORE INTO p_txn_tags (txn_id, tag_id) SELECT ?, tag_id FROM p_txn_tags WHERE txn_id = ?",
        (new_id, old_id),
    )


def after_personal_import(conn) -> None:
    ensure_seeded(conn)
    apply_transfers(conn)


# --- transfer pairing ---------------------------------------------------------


def _days(left: str, right: str) -> int:
    return (date.fromisoformat(right) - date.fromisoformat(left)).days


def _pair_key(out_id: str, in_id: str) -> str:
    return f"{out_id}|{in_id}"


def find_transfer_pairs(txns: list[dict], accounts: dict[str, dict], kinds: dict[str, str], decisions: dict[str, str] | None = None) -> dict:
    """Pure pairing of personal transfer legs.

    txns: active, posted personal rows with 'category_kind' (current category kind)
    and 'manual' flags. Two rows pair when they are in different personal accounts,
    have opposite signs and equal size, are within 3 days, and at least one looks
    like a transfer (transfer-kind category or transfer wording). A leg with more
    than one candidate makes its best pair ambiguous: it is listed for
    confirmation and not applied unless confirmed. Returns
    {"pairs": [...], "ambiguous": [...]}. Rejected pairs are skipped.
    """
    decisions = decisions or {}
    outs = [txn for txn in txns if int(txn["amount_cents"]) < 0]
    ins = [txn for txn in txns if int(txn["amount_cents"]) > 0]
    by_amount: dict[int, list[dict]] = {}
    for txn in ins:
        by_amount.setdefault(int(txn["amount_cents"]), []).append(txn)

    def looks_like(txn: dict) -> bool:
        return kinds.get(txn["id"]) == "transfer" or bool(TRANSFER_WORDS.search(txn.get("name") or ""))

    edges = []
    for out in outs:
        for inc in by_amount.get(-int(out["amount_cents"]), []):
            if inc["account_id"] == out["account_id"]:
                continue
            gap = abs(_days(out["date"], inc["date"]))
            if gap > TRANSFER_WINDOW_DAYS:
                continue
            if not (looks_like(out) or looks_like(inc)):
                continue
            if decisions.get(_pair_key(out["id"], inc["id"])) == "rejected":
                continue
            edges.append((gap, out["date"], out["id"], inc["id"], out, inc))
    out_count: dict[str, int] = {}
    in_count: dict[str, int] = {}
    for _gap, _d, out_id, in_id, _o, _i in edges:
        out_count[out_id] = out_count.get(out_id, 0) + 1
        in_count[in_id] = in_count.get(in_id, 0) + 1
    edges.sort(key=lambda edge: edge[:4])
    used: set[str] = set()
    pairs, ambiguous = [], []
    for gap, _d, out_id, in_id, out, inc in edges:
        if out_id in used or in_id in used:
            continue
        used.add(out_id)
        used.add(in_id)
        item = {"key": _pair_key(out_id, in_id), "out": out, "in": inc, "days": gap}
        unclear = out_count[out_id] > 1 or in_count[in_id] > 1
        if unclear and decisions.get(item["key"]) != "confirmed":
            ambiguous.append(item)
        else:
            pairs.append(item)
    return {"pairs": pairs, "ambiguous": ambiguous}


def _leg_category(conn, txn: dict, other_acct: dict, own_acct: dict, kinds: dict[str, str], cat_ids: dict[str, int]) -> int | None:
    existing = current(conn, txn["id"]) or {}
    if kinds.get(txn["id"]) == "transfer" and existing.get("category_id"):
        return int(existing["category_id"])
    for acct in (own_acct, other_acct):
        if acct.get("class") == "loan":
            return cat_ids["Loan payment"]
    for acct in (own_acct, other_acct):
        if acct.get("class") == "liability" or acct.get("type") == "liability":
            return cat_ids["Credit card payment"]
    return cat_ids["Internal transfer"]


def transfer_decisions(conn) -> dict[str, str]:
    return {row["pair_key"]: row["decision"] for row in conn.execute("SELECT pair_key, decision FROM p_transfer_reviews")}


def _kinds(conn, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    out: dict[str, str] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for row in conn.execute(
            f"""
            SELECT c.txn_id, k.kind, c.source FROM p_classifications c
            LEFT JOIN p_categories k ON k.id = c.category_id
            WHERE c.txn_id IN ({', '.join('?' for _ in chunk)})
            """,
            chunk,
        ):
            out[row["txn_id"]] = row["kind"] or ""
    return out


def transfer_candidates(conn) -> tuple[list[dict], dict[str, str], dict[str, dict]]:
    accounts = personal_accounts(conn)
    txns = [txn for txn in _personal_txns(conn) if not int(txn["pending"] or 0)]
    kinds = _kinds(conn, [txn["id"] for txn in txns])
    manual = {
        row["txn_id"]
        for row in conn.execute("SELECT txn_id FROM p_classifications WHERE source = 'manual'")
    }
    paired_to_business = {
        row["txn_id"]
        for row in conn.execute(
            "SELECT txn_id FROM p_classifications WHERE transfer_pair LIKE 'biz:%'"
        )
    }
    draws = owner_draw_pairs(conn)
    paired_to_business |= set(draws["pairs"])
    usable = [
        txn
        for txn in txns
        if (txn["id"] not in manual or kinds.get(txn["id"]) == "transfer") and txn["id"] not in paired_to_business
    ]
    return usable, kinds, accounts


def detect_transfers(conn) -> dict:
    usable, kinds, accounts = transfer_candidates(conn)
    return find_transfer_pairs(usable, accounts, kinds, transfer_decisions(conn))


def apply_transfers(conn) -> int:
    """Mark matched personal transfer legs and owner-draw deposits. Manual rows are kept."""
    ensure_seeded(conn)
    cat_ids = {name: transfer_category_id(conn, name) for name in ("Credit card payment", "Savings transfer", "Loan payment", "Internal transfer")}
    wrote = 0
    draws_id = owner_draws_id(conn)
    for txn_id, biz in owner_draw_pairs(conn)["pairs"].items():
        result = write_personal(
            conn, txn_id, draws_id, "transfer", 0.95, None,
            transfer_pair=f"biz:{biz['id']}", merchant_display="Owner draw from business",
        )
        wrote += result == "wrote"
    found = detect_transfers(conn)
    usable, kinds, accounts = transfer_candidates(conn)
    for item in found["pairs"]:
        out, inc = item["out"], item["in"]
        out_acct, in_acct = accounts.get(out["account_id"], {}), accounts.get(inc["account_id"], {})
        for leg, other, own, other_acct in ((out, inc, out_acct, in_acct), (inc, out, in_acct, out_acct)):
            existing = current(conn, leg["id"]) or {}
            if existing.get("source") == "manual":
                continue
            # A payment to a loan is spending on the paying side (Mortgage, Auto payment);
            # only the loan's own credit is a transfer, so nothing is counted twice.
            if leg is out and in_acct.get("class") == "loan" and kinds.get(leg["id"]) == "expense":
                continue
            category = _leg_category(conn, leg, other_acct, own, kinds, cat_ids)
            result = write_personal(conn, leg["id"], category, "transfer", 0.95, None, transfer_pair=other["id"])
            wrote += result == "wrote"
    return wrote


def set_transfer_decision(conn, pair_key: str, decision: str, *, actor: str) -> None:
    if decision not in ("confirmed", "rejected"):
        raise HpgbooksError("decision must be confirmed or rejected")
    if not re.fullmatch(r"[A-Za-z0-9:_\-.]{1,200}\|[A-Za-z0-9:_\-.]{1,200}", pair_key or ""):
        raise HpgbooksError("invalid pair")
    out_id, in_id = pair_key.split("|")
    for txn_id in (out_id, in_id):
        if not _personal_txns(conn, [txn_id]):
            raise HpgbooksError("no such transaction")
    conn.execute(
        """
        INSERT INTO p_transfer_reviews (pair_key, decision, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(pair_key) DO UPDATE SET decision = excluded.decision, updated_at = excluded.updated_at
        """,
        (pair_key, decision, now_iso()),
    )
    audit(conn, "personal_transfer_review", field=pair_key, new_value=decision, actor=actor)
    apply_transfers(conn)


# --- owner draws ----------------------------------------------------------------


def business_draws(conn, start: str | None = None, end: str | None = None) -> list[dict]:
    """Business-account outflows the business books treat as owner draws or transfers."""
    if not has_scope(conn):
        return []
    clauses = ["a.scope = 'business'", "t.status = 'active'", "t.amount_cents < 0",
               "c.business_tag IN ('owner_draw', 'transfer')"]
    params: list = []
    if start:
        clauses.append("t.date >= ?")
        params.append(start)
    if end:
        clauses.append("t.date <= ?")
        params.append(end)
    rows = conn.execute(
        f"""
        SELECT t.id, t.account_id, t.date, t.amount_cents, t.name, t.merchant_name, t.description, t.pending,
               a.name AS account_name, a.display_name AS account_display, c.business_tag
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        JOIN classifications c ON c.txn_id = t.id
        WHERE {' AND '.join(clauses)}
        ORDER BY t.date, t.id
        """,
        params,
    ).fetchall()
    return [as_dict(row) for row in rows]


def owner_draw_pairs(conn, start: str | None = None, end: str | None = None) -> dict:
    """Pair business draws with personal deposits (equal amount, within 3 days).

    Returns {"pairs": {personal_txn_id: business_row}, "unpaired": [business owner_draw rows]}.
    Business 'transfer' rows only pair; they are never synthesized, since an
    unpaired one moved money between business accounts.
    """
    lo = (date.fromisoformat(start) - timedelta(days=TRANSFER_WINDOW_DAYS)).isoformat() if start else None
    hi = (date.fromisoformat(end) + timedelta(days=TRANSFER_WINDOW_DAYS)).isoformat() if end else None
    draws = business_draws(conn, lo, hi)
    if not draws:
        return {"pairs": {}, "unpaired": []}
    manual_other = {
        row["txn_id"]
        for row in conn.execute(
            """
            SELECT c.txn_id FROM p_classifications c JOIN p_categories k ON k.id = c.category_id
            WHERE c.source = 'manual' AND NOT (k.group_name = ? AND k.name = ?)
            """,
            OWNER_DRAWS,
        )
    } if _table_exists(conn, "p_classifications") else set()
    deposits: dict[int, list[dict]] = {}
    for txn in _personal_txns(conn):
        cents = int(txn["amount_cents"])
        if cents <= 0 or txn["id"] in manual_other:
            continue
        deposits.setdefault(cents, []).append(txn)
    edges = []
    for draw in draws:
        for dep in deposits.get(-int(draw["amount_cents"]), []):
            gap = abs(_days(draw["date"], dep["date"]))
            if gap <= TRANSFER_WINDOW_DAYS:
                edges.append((gap, draw["date"], draw["id"], dep["id"], draw, dep))
    edges.sort(key=lambda edge: edge[:4])
    used: set[str] = set()
    pairs: dict[str, dict] = {}
    for _gap, _d, draw_id, dep_id, draw, dep in edges:
        if draw_id in used or dep_id in used:
            continue
        used.add(draw_id)
        used.add(dep_id)
        pairs[dep_id] = draw
    unpaired = [draw for draw in draws if draw["id"] not in used and draw["business_tag"] == "owner_draw"]
    return {"pairs": pairs, "unpaired": unpaired}


# --- loader -------------------------------------------------------------------


def _business_label(row: dict) -> str:
    from hpgbooks.db import short_account

    return f"{row.get('account_display') or short_account(row.get('account_name') or '')} (business)"


def load_personal(
    conn,
    start: str | None = None,
    end: str | None = None,
    *,
    include_synthesized: bool = True,
    include_pending: bool = True,
    status: str = "active",
) -> list[dict]:
    """Personal rows with category info, plus synthesized owner-draw rows.

    Each row: id, account_id, account_label, account_class, date, amount_cents,
    name, merchant_key, merchant, pending, category_id, category, group, kind,
    source, confidence, note, transfer_pair, splits, tags, from_business, editable.
    """
    if not personal_ready(conn):
        return []
    cats = categories(conn)
    aliases = load_aliases(conn)
    accounts = personal_accounts(conn)
    clauses = ["a.scope = 'personal'"]
    params: list = []
    if status != "all":
        clauses.append("t.status = ?")
        params.append(status)
    if start:
        clauses.append("t.date >= ?")
        params.append(start)
    if end:
        clauses.append("t.date <= ?")
        params.append(end)
    if not include_pending:
        clauses.append("t.pending = 0")
    rows = conn.execute(
        f"""
        SELECT t.id, t.account_id, t.date, t.amount_cents, t.name, t.merchant_name, t.description,
               t.pending, t.status, c.category_id, c.source, c.confidence, c.note, c.merchant_display,
               c.transfer_pair, c.rule_id
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN p_classifications c ON c.txn_id = t.id
        WHERE {' AND '.join(clauses)}
        ORDER BY t.date, t.id
        """,
        params,
    ).fetchall()
    splits: dict[str, list[dict]] = {}
    for row in conn.execute("SELECT txn_id, category_id, amount_cents, note FROM p_splits ORDER BY id"):
        splits.setdefault(row["txn_id"], []).append(
            {"category_id": int(row["category_id"]), "amount_cents": int(row["amount_cents"]), "note": row["note"] or ""}
        )
    tags: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT tt.txn_id, tg.name FROM p_txn_tags tt JOIN p_tags tg ON tg.id = tt.tag_id ORDER BY tg.name"
    ):
        tags.setdefault(row["txn_id"], []).append(row["name"])
    draws = owner_draw_pairs(conn, start, end)
    draws_id = owner_draws_id(conn)
    fallback = uncategorized_id(conn)
    out: list[dict] = []
    for row in rows:
        item = as_dict(row)
        source = item.get("source") or ""
        category = item.get("category_id")
        transfer_pair = item.get("transfer_pair")
        if item["id"] in draws["pairs"] and source != "manual":
            category = draws_id
            source = "transfer"
            transfer_pair = f"biz:{draws['pairs'][item['id']]['id']}"
        if category is None:
            category = fallback
        cat = cats.get(int(category)) if category is not None else None
        key = merchant_key(item.get("merchant_name"), item.get("name"))
        acct = accounts.get(item["account_id"], {})
        out.append(
            {
                "id": item["id"],
                "account_id": item["account_id"],
                "account_label": label(acct) if acct else item["account_id"][:8],
                "account_class": acct.get("class") or "cash",
                "date": item["date"],
                "amount_cents": int(item["amount_cents"]),
                "name": item.get("name") or item.get("merchant_name") or item.get("description") or "",
                "merchant_key": key,
                "merchant": item.get("merchant_display") or display_for(key, aliases),
                "pending": bool(item.get("pending")),
                "status": item.get("status") or "active",
                "category_id": int(category) if category is not None else None,
                "category": cat["name"] if cat else "Uncategorized",
                "group": cat["group_name"] if cat else "Uncategorized",
                "kind": cat["kind"] if cat else "expense",
                "source": source,
                "confidence": float(item["confidence"]) if item.get("confidence") is not None else 0.0,
                "note": item.get("note") or "",
                "transfer_pair": transfer_pair,
                "rule_id": item.get("rule_id"),
                "splits": splits.get(item["id"], []),
                "tags": tags.get(item["id"], []),
                "from_business": False,
                "editable": True,
            }
        )
    if include_synthesized:
        out.extend(synthesized_rows(conn, draws["unpaired"], start, end, cats, aliases))
    out.sort(key=lambda row: (row["date"], row["id"]))
    return out


def synthesized_rows(conn, unpaired: list[dict], start, end, cats, aliases) -> list[dict]:
    rules = load_personal_rules(conn)
    draws_id = owner_draws_id(conn)
    fallback = uncategorized_id(conn)
    rows = []
    for draw in unpaired:
        if (start and draw["date"] < start) or (end and draw["date"] > end):
            continue
        cents = -int(draw["amount_cents"])
        key = merchant_key(draw.get("merchant_name"), draw.get("name"))
        base = {
            "account_id": draw["account_id"],
            "account_label": _business_label(draw),
            "account_class": "business",
            "date": draw["date"],
            "name": draw.get("name") or draw.get("merchant_name") or "",
            "merchant_key": key,
            "merchant": display_for(key, aliases),
            "pending": bool(draw.get("pending")),
            "status": "active",
            "source": "business",
            "confidence": 1.0,
            "note": "paid from business account",
            "rule_id": None,
            "splits": [],
            "tags": [],
            "from_business": True,
            "editable": False,
        }
        cat = cats.get(draws_id) if draws_id else None
        rows.append(
            {
                **base,
                "id": f"biz:{draw['id']}",
                "amount_cents": cents,
                "category_id": draws_id,
                "category": cat["name"] if cat else "Owner draws",
                "group": cat["group_name"] if cat else "Income",
                "kind": "funding",
                "transfer_pair": draw["id"],
            }
        )
        if DRAW_TRANSFER_LIKE.search(draw.get("name") or ""):
            continue
        decision = decide_personal({**draw, "account_id": draw["account_id"]}, rules, aliases, fallback)
        spend_cat = cats.get(decision["category_id"]) if decision["category_id"] else None
        if spend_cat and spend_cat["kind"] != "expense":
            spend_cat = cats.get(fallback)
        rows.append(
            {
                **base,
                "id": f"biz:{draw['id']}:spend",
                "amount_cents": -cents,
                "category_id": spend_cat["id"] if spend_cat else fallback,
                "category": spend_cat["name"] if spend_cat else "Uncategorized",
                "group": spend_cat["group_name"] if spend_cat else "Uncategorized",
                "kind": "expense",
                "transfer_pair": draw["id"],
            }
        )
    return rows


def allocations(row: dict, cats: dict[int, dict]) -> list[dict]:
    """(category, kind, group, amount) pieces of a row: its splits, or the row itself."""
    if row.get("splits"):
        pieces = []
        for split in row["splits"]:
            cat = cats.get(split["category_id"]) or {}
            pieces.append(
                {
                    "category_id": split["category_id"],
                    "category": cat.get("name") or "Uncategorized",
                    "group": cat.get("group_name") or "Uncategorized",
                    "kind": cat.get("kind") or "expense",
                    "amount_cents": int(split["amount_cents"]),
                }
            )
        return pieces
    return [
        {
            "category_id": row["category_id"],
            "category": row["category"],
            "group": row["group"],
            "kind": row["kind"],
            "amount_cents": int(row["amount_cents"]),
        }
    ]


def needs_review(row: dict) -> bool:
    if row.get("from_business"):
        return False
    if row.get("source") in ("manual", "transfer"):
        return False
    return row.get("category") == "Uncategorized" or float(row.get("confidence") or 0) < REVIEW_THRESHOLD


def personal_review_count(conn) -> int:
    if not personal_ready(conn):
        return 0
    return sum(1 for row in load_personal(conn, include_synthesized=False) if needs_review(row))
