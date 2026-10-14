"""Idempotent import of Finance-MCP JSON, CSV tool results, and page lists."""

from __future__ import annotations

import csv
import glob
import io
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from hpgbooks.capitalone import supersede_matching_csv
from hpgbooks.classify import classify_new, match_transfers, write_classification
from hpgbooks.db import HpgbooksError, audit, now_iso, to_cents
from hpgbooks.scope import account_scope, has_scope, known_account_ids
from hpgbooks.scope import txn_scope as account_scope_of_txn

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COMPARE_FIELDS = (
    "account_id",
    "date",
    "amount_cents",
    "direction",
    "currency",
    "name",
    "merchant_name",
    "description",
    "pending",
    "provider_category",
)


def expand_inputs(items: list[str]) -> list[Path]:
    found: list[Path] = []
    for item in items:
        path = Path(item)
        if path.is_dir():
            # Personal pulls sit in <day>/personal/ next to the business files.
            matches = sorted(path.glob("*.json")) + sorted(path.glob("personal/*.json"))
            if not matches:
                raise HpgbooksError(f"no json files in {item}")
            found.extend(matches)
        elif path.is_file():
            found.append(path)
        else:
            matches = sorted(glob.glob(item))
            if not matches:
                raise HpgbooksError(f"no such file or pattern: {item}")
            found.extend(Path(match) for match in matches if Path(match).is_file())
    ordered: list[Path] = []
    seen: set[str] = set()
    for path in found:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    if not ordered:
        raise HpgbooksError("nothing to import")
    return ordered


def _parse_pending(value) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    return 1 if str(value).strip().lower() in {"1", "true", "yes", "y"} else 0


def _category_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), default=str)


def _is_csv_batch(batch: dict) -> bool:
    if batch.get("format") == "csv":
        return True
    return "csv" in batch and "transactions" not in batch


def _normalize(raw: dict, fallback_account: str | None, known: set[str]) -> dict:
    txn_id = raw.get("id")
    if not txn_id:
        return {"_skip": "missing id"}
    # Liability feeds name the column liability_id. A missing column is not a
    # blank value: on update we keep whatever was stored last time.
    has_account = "account_id" in raw or "liability_id" in raw
    account_id = raw.get("account_id") or raw.get("liability_id") or fallback_account
    if account_id not in known:
        return {"_skip": "unknown account", "_account": str(account_id or "")}
    date = str(raw.get("date") or "")[:10]
    if not DATE_RE.fullmatch(date):
        raise HpgbooksError(f"transaction {txn_id} has an invalid date")
    try:
        amount_cents = to_cents(raw.get("amount"))
    except HpgbooksError as exc:
        raise HpgbooksError(f"transaction {txn_id} has an invalid amount") from exc
    omit = set()
    if not has_account:
        omit.add("account_id")
    if "category" not in raw:
        omit.add("provider_category")
    for field in ("direction", "currency", "name", "merchant_name", "description", "pending"):
        if field not in raw:
            omit.add(field)
    return {
        "id": str(txn_id),
        "account_id": account_id,
        "date": date,
        "amount_cents": amount_cents,
        "direction": raw.get("direction") or "",
        "currency": raw.get("currency") or "USD",
        "name": raw.get("name") or "",
        "merchant_name": raw.get("merchant_name") or "",
        "description": raw.get("description") or "",
        "pending": _parse_pending(raw.get("pending")) if "pending" in raw else 0,
        "provider_category": _category_text(raw.get("category")) if "category" in raw else "",
        "raw_json": json.dumps(raw, separators=(",", ":"), sort_keys=True, default=str),
        "_omit": omit,
    }


def load_file(path: Path, date_from: str | None = None, date_to: str | None = None, known: set[str] | None = None):
    """Rows from one file. `known` is the set of registered account ids (any scope)."""
    if known is None:
        from hpgbooks.db import ACCOUNT_IDS

        known = set(ACCOUNT_IDS)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HpgbooksError(f"{path} is not valid JSON") from exc
    batches = payload if isinstance(payload, list) else [payload]
    rows: list[dict] = []
    window_from: list[str] = []
    window_to: list[str] = []
    query_accounts: list[str] = []
    saw_query = False
    for batch in batches:
        if not isinstance(batch, dict):
            raise HpgbooksError(f"{path} contains a non-object batch")
        query = batch.get("_query") if isinstance(batch.get("_query"), dict) else None
        if query:
            saw_query = True
            if query.get("date_from"):
                window_from.append(str(query["date_from"])[:10])
            if query.get("date_to"):
                window_to.append(str(query["date_to"])[:10])
            for account_id in query.get("account_ids") or []:
                if account_id not in query_accounts:
                    query_accounts.append(account_id)
        fallback = batch.get("account_id")
        if _is_csv_batch(batch):
            raw_rows = list(csv.DictReader(io.StringIO(batch.get("csv") or "")))
            if not query:
                dates = sorted(str(row.get("date") or "")[:10] for row in raw_rows if row.get("date"))
                dates = [item for item in dates if DATE_RE.fullmatch(item)]
                if dates:
                    window_from.append(dates[0])
                    window_to.append(dates[-1])
        else:
            raw_rows = batch.get("transactions") or []
            if not query:
                if batch.get("date_from"):
                    window_from.append(str(batch["date_from"])[:10])
                if batch.get("date_to"):
                    window_to.append(str(batch["date_to"])[:10])
        if not isinstance(raw_rows, list):
            raise HpgbooksError(f"{path} transactions must be a list")
        for raw in raw_rows:
            if not isinstance(raw, dict):
                rows.append({"_skip": "not an object"})
                continue
            rows.append(_normalize(raw, fallback, known))

    if date_from or date_to:
        kept = []
        for row in rows:
            if row.get("_skip"):
                kept.append(row)
                continue
            if date_from and row["date"] < date_from:
                continue
            if date_to and row["date"] > date_to:
                continue
            kept.append(row)
        rows = kept
        if date_from:
            window_from = [date_from]
        if date_to:
            window_to = [date_to]

    accepted_dates = [row["date"] for row in rows if not row.get("_skip")]
    effective_from = min(window_from) if window_from else (min(accepted_dates) if accepted_dates else None)
    effective_to = max(window_to) if window_to else (max(accepted_dates) if accepted_dates else None)
    if date_from:
        effective_from = date_from
    if date_to:
        effective_to = date_to
    # _query.account_ids is the account list even when a queried account
    # contributed zero rows, so its stale pending transactions still supersede.
    supersede_accounts = query_accounts if saw_query else None
    return rows, effective_from, effective_to, supersede_accounts


def _dates_within(left: str, right: str) -> int | None:
    delta = abs(
        (
            datetime.strptime(left, "%Y-%m-%d") - datetime.strptime(right, "%Y-%m-%d")
        ).days
    )
    return delta if delta <= 7 else None


def _amount_close(pending_cents: int, posted_cents: int) -> bool:
    pending_abs = abs(pending_cents)
    posted_abs = abs(posted_cents)
    if pending_abs == posted_abs:
        return True
    if pending_abs == 0:
        return False
    return abs(posted_abs - pending_abs) / pending_abs <= 0.20


def _supersede_account(conn, account_id: str, present_ids: set[str], posted_rows: list[dict], date_from: str, date_to: str) -> int:
    pending_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM transactions
            WHERE account_id = ? AND pending = 1 AND status = 'active'
              AND COALESCE(source, 'finance-mcp') != 'capitalone_csv'
              AND date >= ? AND date <= ?
            """,
            (account_id, date_from, date_to),
        ).fetchall()
        if row["id"] not in present_ids
    ]
    candidates = [row for row in posted_rows if not row["pending"] and row["id"] not in {p["id"] for p in pending_rows}]
    edges = []
    for pending in pending_rows:
        for posted in candidates:
            if not _amount_close(pending["amount_cents"], posted["amount_cents"]):
                continue
            delta = _dates_within(pending["date"], posted["date"])
            if delta is None:
                continue
            exact = 0 if abs(pending["amount_cents"]) == abs(posted["amount_cents"]) else 1
            amount_delta = abs(abs(pending["amount_cents"]) - abs(posted["amount_cents"]))
            edges.append((exact, amount_delta, delta, pending["id"], posted["id"], pending, posted))
    edges.sort()
    used_pending: set[str] = set()
    used_posted: set[str] = set()
    matched: list[tuple[dict, dict]] = []
    for _exact, _amt, _days, _pid, _qid, pending, posted in edges:
        if pending["id"] in used_pending or posted["id"] in used_posted:
            continue
        used_pending.add(pending["id"])
        used_posted.add(posted["id"])
        matched.append((pending, posted))

    count = 0
    for pending, posted in matched:
        _mark_superseded(conn, pending["id"], posted["id"])
        _carry_manual(conn, pending["id"], posted["id"])
        count += 1
    for pending in pending_rows:
        if pending["id"] in used_pending:
            continue
        _mark_superseded(conn, pending["id"], None)
        count += 1
    return count


def _mark_superseded(conn, txn_id: str, superseded_by: str | None) -> None:
    conn.execute(
        """
        UPDATE transactions
        SET status = 'superseded', superseded_by = ?, updated_at = ?
        WHERE id = ?
        """,
        (superseded_by, now_iso(), txn_id),
    )
    audit(
        conn,
        "supersede",
        txn_id=txn_id,
        field="status",
        old_value="active",
        new_value="superseded",
        actor="import",
        note=f"superseded_by={superseded_by or ''}",
    )


def _carry_manual(conn, old_id: str, new_id: str) -> None:
    if account_scope_of_txn(conn, old_id) == "personal":
        from hpgbooks.personal.classify import carry_manual_personal

        carry_manual_personal(conn, old_id, new_id)
        return
    old = conn.execute("SELECT * FROM classifications WHERE txn_id = ?", (old_id,)).fetchone()
    if not old or old["source"] != "manual":
        return
    write_classification(
        conn,
        new_id,
        old["business_tag"],
        old["category"],
        "manual",
        old["confidence"],
        old["note"] or "",
        None,
        overwrite_manual=False,
        actor="import",
        audit_write=True,
    )


def _incoming(existing, row: dict, field: str):
    if field in row.get("_omit", ()):
        return existing[field]
    return row[field]


def import_file(conn, path: Path, date_from: str | None = None, date_to: str | None = None) -> dict:
    known = known_account_ids(conn)
    rows, effective_from, effective_to, supersede_accounts = load_file(path, date_from, date_to, known)
    stats = {
        "file": str(path),
        "rows_in": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "superseded": 0,
        "skipped": 0,
        "unknown_accounts": 0,
        "unknown_account_ids": [],
        "by_scope": {},
        "date_from": effective_from,
        "date_to": effective_to,
    }
    present: dict[str, set[str]] = defaultdict(set)
    posted_by_account: dict[str, list[dict]] = defaultdict(list)
    inserted_ids: list[str] = []
    inserted_rows: list[dict] = []

    for row in rows:
        stats["rows_in"] += 1
        if row.get("_skip"):
            stats["skipped"] += 1
            if row["_skip"] == "unknown account":
                stats["unknown_accounts"] += 1
                if row.get("_account") and row["_account"] not in stats["unknown_account_ids"]:
                    stats["unknown_account_ids"].append(row["_account"])
            continue
        present[row["account_id"]].add(row["id"])
        posted_by_account[row["account_id"]].append(row)
        existing = conn.execute("SELECT * FROM transactions WHERE id = ?", (row["id"],)).fetchone()
        ts = now_iso()
        if existing is None:
            conn.execute(
                """
                INSERT INTO transactions (
                  id, account_id, date, amount_cents, direction, currency, name, merchant_name,
                  description, pending, provider_category, raw_json, status, superseded_by,
                  first_seen_at, last_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["account_id"],
                    row["date"],
                    row["amount_cents"],
                    row["direction"],
                    row["currency"],
                    row["name"],
                    row["merchant_name"],
                    row["description"],
                    row["pending"],
                    row["provider_category"],
                    row["raw_json"],
                    ts,
                    ts,
                    ts,
                ),
            )
            stats["inserted"] += 1
            inserted_ids.append(row["id"])
            inserted_rows.append(row)
            continue

        changed = any(
            existing[field] != _incoming(existing, row, field) for field in COMPARE_FIELDS
        ) or existing["status"] != "active"
        if not changed:
            conn.execute(
                "UPDATE transactions SET last_seen_at = ? WHERE id = ?",
                (ts, row["id"]),
            )
            stats["unchanged"] += 1
            continue

        was_superseded = existing["status"] == "superseded"
        conn.execute(
            """
            UPDATE transactions SET
              account_id=?, date=?, amount_cents=?, direction=?, currency=?, name=?,
              merchant_name=?, description=?, pending=?, provider_category=?, raw_json=?,
              status='active', superseded_by=NULL, last_seen_at=?, updated_at=?
            WHERE id=?
            """,
            (
                _incoming(existing, row, "account_id"),
                _incoming(existing, row, "date"),
                _incoming(existing, row, "amount_cents"),
                _incoming(existing, row, "direction"),
                _incoming(existing, row, "currency"),
                _incoming(existing, row, "name"),
                _incoming(existing, row, "merchant_name"),
                _incoming(existing, row, "description"),
                _incoming(existing, row, "pending"),
                _incoming(existing, row, "provider_category"),
                row["raw_json"],
                ts,
                ts,
                row["id"],
            ),
        )
        stats["updated"] += 1
        if was_superseded:
            audit(
                conn,
                "reactivate",
                txn_id=row["id"],
                field="status",
                old_value="superseded",
                new_value="active",
                actor="import",
                note="id reappeared in a later import",
            )

    # Each account's scope picks the engine: business rules for business accounts,
    # personal rules for personal ones; excluded accounts are stored but not classified.
    scopes = {account_id: (account_scope(conn, account_id) if has_scope(conn) else "business") for account_id in present}
    for account_id, ids in present.items():
        scope = scopes[account_id]
        stats["by_scope"][scope] = stats["by_scope"].get(scope, 0) + len(ids)
    business_ids = [txn_id for txn_id, row in zip(inserted_ids, inserted_rows) if scopes.get(row["account_id"]) == "business"]
    personal_ids = [txn_id for txn_id, row in zip(inserted_ids, inserted_rows) if scopes.get(row["account_id"]) == "personal"]
    classify_new(conn, business_ids)
    if personal_ids:
        from hpgbooks.personal.classify import classify_new_personal

        classify_new_personal(conn, personal_ids)
    stats["superseded"] += supersede_matching_csv(conn, inserted_rows)
    if effective_from and effective_to:
        if supersede_accounts is None:
            scan_accounts = list(present)
        else:
            scan_accounts = [account_id for account_id in supersede_accounts if account_id in known]
        for account_id in scan_accounts:
            stats["superseded"] += _supersede_account(
                conn,
                account_id,
                present.get(account_id, set()),
                posted_by_account.get(account_id, []),
                effective_from,
                effective_to,
            )
    match_transfers(conn)
    # A business draw can arrive after its personal deposit (or the reverse), so
    # personal pairing reruns after any import once personal accounts exist.
    if has_scope(conn) and conn.execute("SELECT 1 FROM accounts WHERE scope = 'personal' LIMIT 1").fetchone():
        from hpgbooks.personal.classify import after_personal_import

        after_personal_import(conn)

    accounts = list(present)
    conn.execute(
        """
        INSERT INTO import_log (
          ts, file, account_id, date_from, date_to, rows_in, inserted, updated, unchanged, superseded, skipped
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_iso(),
            str(path),
            accounts[0] if len(accounts) == 1 else None,
            effective_from,
            effective_to,
            stats["rows_in"],
            stats["inserted"],
            stats["updated"],
            stats["unchanged"],
            stats["superseded"],
            stats["skipped"],
        ),
    )
    return stats


def import_paths(conn, items: list[str], date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    summaries = []
    for path in expand_inputs(items):
        summaries.append(import_file(conn, path, date_from, date_to))
    return summaries
