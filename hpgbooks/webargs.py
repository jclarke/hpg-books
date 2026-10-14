"""Request parsing and CSRF helpers shared by the page routes and the JSON API."""

from __future__ import annotations

import re
import secrets
from datetime import date
from urllib.parse import urlparse

from flask import abort, request, session

from hpgbooks.db import BUSINESS_TAGS, CATEGORIES, HpgbooksError, connect, to_cents
from hpgbooks.reports import BUSINESS_FILTERS, parse_month, require_date

BUSINESSES = BUSINESS_FILTERS
TXN_RE = re.compile(r"^[A-Za-z0-9:_\-.]{1,200}$")


def _ensure_csrf() -> str:
    token = session.get("csrf_token")
    if not isinstance(token, str) or len(token) < 16:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _host_ok() -> bool:
    """Loopback, plus exact extra hosts. A rebinding name that is not listed cannot get in."""
    from hpgbooks.access import host_header_allowed

    return host_header_allowed()


def _origin_ok() -> bool:
    origin = request.headers.get("Origin")
    if origin is None or origin == "":
        return True
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https"):
        return False
    return (parsed.netloc or "").lower() == (request.host or "").lower()


def _csrf_ok() -> bool:
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    if not sent:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            sent = body.get("csrf_token") or ""
    expected = session.get("csrf_token")
    return isinstance(sent, str) and isinstance(expected, str) and secrets.compare_digest(sent, expected)


def _business() -> str:
    business = request.args.get("business") or "all"
    if business not in BUSINESSES:
        abort(400)
    return business


def _optional_date(name: str) -> str | None:
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return None
    try:
        return require_date(raw)
    except HpgbooksError:
        abort(400)


def _range() -> tuple[str, str]:
    start = _optional_date("start")
    end = _optional_date("end")
    if start is None and end is None:
        today = date.today()
        return f"{today.year:04d}-01-01", today.isoformat()
    if start is None or end is None or start > end:
        abort(400)
    return start, end


def _year_arg() -> int:
    raw = request.args.get("year") or "2026"
    if not raw.isdigit():
        abort(400)
    year = int(raw)
    if year < 1900 or year > 2200:
        abort(400)
    return year


def _int_arg(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    if not re.fullmatch(r"\d+", raw):
        abort(400)
    value = int(raw)
    if value < minimum or value > maximum:
        abort(400)
    return value


def _amount_arg(name: str) -> int | None:
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return None
    try:
        return to_cents(raw)
    except HpgbooksError:
        abort(400)


def _business_account_ids() -> set[str]:
    from hpgbooks.scope import account_ids

    with connect(readonly=True) as conn:
        return account_ids(conn, "business")


def _transaction_filters() -> dict:
    month = request.args.get("month") if "month" in request.args else None
    if month:
        try:
            month = parse_month(month)
        except HpgbooksError:
            abort(400)
    account = (request.args.get("account") or "").strip()
    if account and account not in _business_account_ids():
        abort(400)
    tag = (request.args.get("tag") or "").strip()
    if tag and tag not in BUSINESS_TAGS:
        abort(400)
    category = (request.args.get("category") or "").strip()
    if category and category not in CATEGORIES:
        abort(400)
    status = request.args.get("status") or "active"
    if status not in ("active", "superseded", "all"):
        abort(400)
    sort = request.args.get("sort") or "date"
    direction = request.args.get("dir") or "desc"
    if sort not in ("date", "account", "amount", "name", "tag", "category", "status"):
        abort(400)
    if direction not in ("asc", "desc"):
        abort(400)
    search = request.args.get("search") or ""
    if len(search) > 200:
        abort(400)
    vendor = (request.args.get("vendor") or "").strip()
    if len(vendor) > 200:
        abort(400)
    min_cents = _amount_arg("min_amount")
    max_cents = _amount_arg("max_amount")
    if min_cents is not None and max_cents is not None and min_cents > max_cents:
        abort(400)
    business = _business()
    return {
        "month": month or "",
        "account": account,
        "tag": tag,
        "category": category,
        "search": search,
        "vendor": vendor,
        "needs_review": request.args.get("needs_review") == "1",
        "status": status,
        "sort": sort,
        "dir": direction,
        "min_amount": request.args.get("min_amount") or "",
        "max_amount": request.args.get("max_amount") or "",
        "min_cents": min_cents,
        "max_cents": max_cents,
        "business": business,
        "offset": _int_arg("offset", 0, 0, 1_000_000),
        "start": _optional_date("start") or "",
        "end": _optional_date("end") or "",
    }


def _txn_query(filters: dict) -> dict:
    return {
        "month": filters["month"] or None,
        "account_id": filters["account"] or None,
        "tag": filters["tag"] or None,
        "category": filters["category"] or None,
        "needs_review": filters["needs_review"],
        "search": filters["search"] or None,
        "vendor": filters["vendor"] or None,
        "status": filters["status"],
        "min_cents": filters["min_cents"],
        "max_cents": filters["max_cents"],
        "sort": filters["sort"],
        "direction": filters["dir"],
        "date_from": filters["start"] or None,
        "date_to": filters["end"] or None,
        "business": filters["business"],
    }
