"""Personal transaction register, search, review queue, and transfer listing."""

from __future__ import annotations

import csv
import io

from hpgbooks.db import HpgbooksError
from hpgbooks.personal.analytics import _public_row
from hpgbooks.personal.classify import (
    categories,
    detect_transfers,
    load_personal,
    needs_review,
    owner_draw_pairs,
    personal_accounts,
)
from hpgbooks.scope import label

SORTS = {
    "date": lambda row: (row["date"], row["id"]),
    "amount": lambda row: (row["amount_cents"], row["id"]),
    "merchant": lambda row: (row["merchant"].lower(), row["date"]),
    "category": lambda row: (row["category"].lower(), row["date"]),
    "account": lambda row: (row["account_label"].lower(), row["date"]),
}


def _text_hit(row: dict, needle: str) -> bool:
    blob = " ".join(
        [row["name"], row["merchant"], row["merchant_key"], row["note"], row["category"], " ".join(row["tags"]),
         f"{abs(row['amount_cents']) / 100:.2f}"]
    ).lower()
    return needle in blob


def query(
    conn,
    *,
    start: str | None = None,
    end: str | None = None,
    search: str = "",
    account: str = "",
    category_id: int | None = None,
    group: str = "",
    tag: str = "",
    merchant_key: str = "",
    min_cents: int | None = None,
    max_cents: int | None = None,
    uncategorized: bool = False,
    pending: str = "",
    transfers: str = "show",
    review: bool = False,
    kind: str = "",
    sort: str = "date",
    direction: str = "desc",
    limit: int = 100,
    offset: int = 0,
    include_business: bool = True,
) -> dict:
    if sort not in SORTS:
        raise HpgbooksError("unknown sort")
    if direction not in ("asc", "desc"):
        raise HpgbooksError("direction must be asc or desc")
    if transfers not in ("show", "hide", "only"):
        raise HpgbooksError("transfers must be show, hide, or only")
    rows = load_personal(conn, start, end, include_synthesized=include_business)
    needle = search.strip().lower()
    out = []
    for row in rows:
        if account and row["account_id"] != account:
            continue
        if category_id is not None and row["category_id"] != category_id and not any(
            split["category_id"] == category_id for split in row["splits"]
        ):
            continue
        if group and row["group"] != group:
            continue
        if kind and row["kind"] != kind:
            continue
        if tag and tag.lower() not in [item.lower() for item in row["tags"]]:
            continue
        if merchant_key and row["merchant_key"] != merchant_key:
            continue
        cents = abs(row["amount_cents"])
        if min_cents is not None and cents < min_cents:
            continue
        if max_cents is not None and cents > max_cents:
            continue
        if uncategorized and row["category"] != "Uncategorized":
            continue
        if pending == "1" and not row["pending"]:
            continue
        if pending == "0" and row["pending"]:
            continue
        if transfers == "hide" and row["kind"] == "transfer":
            continue
        if transfers == "only" and row["kind"] != "transfer":
            continue
        if review and not needs_review(row):
            continue
        if needle and not _text_hit(row, needle):
            continue
        out.append(row)
    out.sort(key=SORTS[sort], reverse=direction == "desc")
    page = out[offset: offset + limit]
    return {
        "rows": [_public_row(row) for row in page],
        "total": len(out),
        "in_cents": sum(row["amount_cents"] for row in out if row["amount_cents"] > 0),
        "out_cents": -sum(row["amount_cents"] for row in out if row["amount_cents"] < 0),
    }


def register(conn, account_id: str, **filters) -> dict:
    """One personal account's rows with a running balance (anchored when an anchor exists)."""
    from hpgbooks.personal.balances import load_series

    accounts = personal_accounts(conn)
    if account_id not in accounts:
        raise HpgbooksError("no such account")
    acct = accounts[account_id]
    series = load_series(conn, {account_id: acct})[account_id]
    data = query(conn, account=account_id, include_business=False, limit=100_000, **filters)
    rows = data["rows"]
    balances: dict[str, int] = {}
    for row in sorted(rows, key=lambda item: (item["date"], item["id"])):
        balances.setdefault(row["date"], series.balance_at(row["date"]))
    for row in rows:
        row["day_end_balance_cents"] = balances.get(row["date"])
    return {
        "account": {"id": account_id, "label": label(acct), "class": acct.get("class"), "last4": acct.get("last4") or "", "institution": acct.get("institution") or ""},
        **data,
    }


def personal_search(conn, query_text: str, limit: int = 8) -> dict:
    """Command-palette search over personal rows, merchants, and accounts only."""
    needle = (query_text or "").strip().lower()
    if not needle:
        return {"transactions": [], "vendors": [], "accounts": []}
    rows = [row for row in load_personal(conn) if _text_hit(row, needle)]
    rows.sort(key=lambda row: (row["date"], row["id"]), reverse=True)
    merchants: dict[str, dict] = {}
    for row in rows:
        item = merchants.setdefault(row["merchant_key"], {"name": row["merchant"], "count": 0, "spend_cents": 0, "last_date": row["date"]})
        item["count"] += 1
        if row["amount_cents"] < 0:
            item["spend_cents"] -= row["amount_cents"]
    accounts = [
        {"id": acct["id"], "name": label(acct), "short_name": label(acct)}
        for acct in personal_accounts(conn).values()
        if needle in label(acct).lower() or needle in (acct.get("institution") or "").lower() or needle == (acct.get("last4") or "")
    ]
    return {
        "transactions": [
            {
                "id": row["id"], "date": row["date"], "amount_cents": row["amount_cents"], "name": row["merchant"],
                "account_id": row["account_id"], "account_name": row["account_label"], "business_tag": "",
                "category": row["category"],
            }
            for row in rows[:limit]
        ],
        "vendors": sorted(merchants.values(), key=lambda item: (-item["count"], item["name"]))[:limit],
        "accounts": accounts,
    }


def review_queue(conn) -> list[dict]:
    """Uncategorized and low-confidence rows, largest first, with a suggestion from history."""
    rows = load_personal(conn, include_synthesized=False)
    cats = categories(conn)
    history: dict[str, dict[int, int]] = {}
    for row in rows:
        if row["source"] in ("manual", "rule") and row["category"] != "Uncategorized" and not needs_review(row):
            bucket = history.setdefault(row["merchant_key"], {})
            bucket[row["category_id"]] = bucket.get(row["category_id"], 0) + 1
    out = []
    for row in rows:
        if not needs_review(row):
            continue
        item = _public_row(row)
        bucket = history.get(row["merchant_key"])
        if bucket:
            cat_id, count = sorted(bucket.items(), key=lambda pair: (-pair[1], pair[0]))[0]
            total = sum(bucket.values())
            item["suggestion"] = {
                "category_id": cat_id,
                "category": cats.get(cat_id, {}).get("name", ""),
                "reason": f"{count} of {total} earlier {row['merchant']} rows",
                "confidence": round(count / total, 2),
            }
        elif row["category"] != "Uncategorized":
            item["suggestion"] = {"category_id": row["category_id"], "category": row["category"], "reason": "suggested by a rule", "confidence": row["confidence"]}
        else:
            item["suggestion"] = None
        item["similar"] = sum(1 for other in rows if other["merchant_key"] == row["merchant_key"] and other["source"] != "manual")
        out.append(item)
    out.sort(key=lambda item: (-abs(item["amount_cents"]), item["date"], item["id"]))
    return out


def transfers_view(conn) -> dict:
    found = detect_transfers(conn)
    accounts = personal_accounts(conn)

    def leg(row: dict) -> dict:
        return {"id": row["id"], "date": row["date"], "amount_cents": int(row["amount_cents"]), "name": row.get("name") or "",
                "account_label": label(accounts[row["account_id"]]) if row["account_id"] in accounts else ""}

    draws = owner_draw_pairs(conn)
    personal_rows = {row["id"]: row for row in load_personal(conn, include_synthesized=False)}
    return {
        "pairs": [{"key": item["key"], "days": item["days"], "out": leg(item["out"]), "in": leg(item["in"])} for item in found["pairs"]],
        "ambiguous": [{"key": item["key"], "days": item["days"], "out": leg(item["out"]), "in": leg(item["in"])} for item in found["ambiguous"]],
        "owner_draws": [
            {
                "business": {"id": biz["id"], "date": biz["date"], "amount_cents": int(biz["amount_cents"]), "name": biz.get("name") or ""},
                "personal": {"id": txn_id, "date": personal_rows.get(txn_id, {}).get("date"), "amount_cents": personal_rows.get(txn_id, {}).get("amount_cents"),
                             "account_label": personal_rows.get(txn_id, {}).get("account_label", "")},
            }
            for txn_id, biz in draws["pairs"].items()
        ],
        "owner_draws_unpaired": [
            {"id": biz["id"], "date": biz["date"], "amount_cents": int(biz["amount_cents"]), "name": biz.get("name") or ""}
            for biz in draws["unpaired"]
        ],
    }


def _csv_text(value) -> str:
    value = "" if value is None else str(value)
    if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def rows_csv(columns: list[tuple[str, str]], rows: list[dict]) -> str:
    """CSV with money columns (keys ending _cents) in dollars and formula-safe text."""
    from hpgbooks.db import cents_to_dollars

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([title for _key, title in columns])
    for row in rows:
        out = []
        for key, _title in columns:
            value = row.get(key)
            if key.endswith("_cents") and isinstance(value, int):
                out.append(cents_to_dollars(value))
            elif isinstance(value, list):
                out.append(_csv_text(", ".join(str(item) for item in value)))
            elif isinstance(value, float):
                out.append(f"{value:.1f}")
            elif isinstance(value, bool):
                out.append("yes" if value else "no")
            else:
                out.append(_csv_text(value))
        writer.writerow(out)
    return buffer.getvalue()
