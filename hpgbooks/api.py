"""JSON API for the single-page app. Mounted under /api.

Reads open the database with query_only. Every write goes through classify/db
helpers that add an audit_log row. CSRF and Origin checks run in web.py before
any mutating request reaches these handlers.
"""

from __future__ import annotations

from datetime import date, datetime

from flask import Blueprint, abort, jsonify, request

from hpgbooks.analytics import (
    accounts_range,
    account_register,
    build_dashboard_range,
    cash_flow_report,
    cash_outlook,
    compare_window,
    global_search,
    owner_draws_report,
    pnl_by_business,
    pnl_line_transactions,
    recurring_items,
    review_count,
    review_suggestions,
    schedule_c_report,
    tabular_json,
    upcoming_bills,
    vendor_report,
    vendor_suggestions,
)
from hpgbooks.balances import anchor_history, parse_dollars, set_anchor, snapshot_for
from hpgbooks.classify import (
    classify_manual,
    current_classification,
    default_pattern,
    disable_rule,
    enable_rule,
    preview_rule,
    resolve_txn,
    restore_classification,
)
from hpgbooks.config import get_config
from hpgbooks.db import (
    BUSINESS_TAGS,
    CATEGORIES,
    COGS_CATEGORIES,
    OPEX_CATEGORIES,
    REVENUE_CATEGORIES,
    HpgbooksError,
    audit,
    connect,
    get_setting,
    set_setting,
    short_account,
)
from hpgbooks.scope import accounts_in, get_account, parse_mode
from hpgbooks.vendors import list_aliases, merge_vendors, rename_canonical, unmerge_vendor
from hpgbooks.reports import (
    attach_prior,
    build_pnl,
    distinct_months,
    month_end,
    query_audit,
    query_review,
    query_rules,
    query_transactions,
)
from hpgbooks.access import (
    clear_login_session,
    establish_session,
    lockout_payload,
    record_login_failure,
    request_requires_login,
    reset_lockout,
    session_is_authenticated,
    verify_passphrase,
)
from hpgbooks.webargs import (
    BUSINESSES,
    TXN_RE,
    _business,
    _ensure_csrf,
    _int_arg,
    _optional_date,
    _transaction_filters,
    _txn_query,
)

api = Blueprint("api", __name__, url_prefix="/api")

TXN_PAGE_MAX = 500
BULK_MAX = 500
RESERVE_MAX_CENTS = 100_000_000_00
REPORTS = {
    "pnl-by-business": "P&L by business",
    "expenses-by-vendor": "Expenses by vendor",
    "cash-flow": "Cash flow",
    "owner-draws": "Owner draws",
    "schedule-c": "Schedule C-style summary",
}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@api.errorhandler(ApiError)
def _api_error(err: ApiError):
    return jsonify({"ok": False, "error": err.message}), err.status


@api.errorhandler(HpgbooksError)
def _hpgbooks_error(err: HpgbooksError):
    return jsonify({"ok": False, "error": str(err)}), 400


def _body() -> dict:
    if not request.is_json:
        raise ApiError("expected a JSON body", 415)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError("expected a JSON object")
    return body


def _str_field(body: dict, name: str, *, max_len: int, required: bool = False) -> str:
    value = body.get(name)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError(f"{name} must be text")
    if required and not value.strip():
        raise ApiError(f"{name} is required")
    if len(value) > max_len:
        raise ApiError(f"{name} is too long")
    return value


def _txn_id(value) -> str:
    if not isinstance(value, str) or not TXN_RE.fullmatch(value.strip()):
        raise ApiError("invalid transaction id")
    return value.strip()


def _assignment(body: dict) -> tuple[str, str, str]:
    tag = _str_field(body, "tag", max_len=40).strip()
    category = _str_field(body, "category", max_len=60).strip()
    note = _str_field(body, "note", max_len=500)
    if tag not in BUSINESS_TAGS or category not in CATEGORIES:
        raise ApiError("txn_id, tag, and category are required")
    return tag, category, note


def _mode() -> str:
    """?mode=business|personal; absent means business (old clients and the CLI)."""
    try:
        return parse_mode(request.args.get("mode"))
    except HpgbooksError:
        raise ApiError("mode must be business or personal")


def _business_account(conn, account_id: str) -> dict:
    acct = get_account(conn, account_id)
    if acct is None or acct["scope"] != "business":
        raise ApiError("no such account", 404)
    return acct


def _latest_month(conn) -> str:
    months = distinct_months(conn)
    return months[-1] if months else date.today().strftime("%Y-%m")


def _range_or(conn, default: str) -> tuple[str, str]:
    """start/end from the query string, or a page default: 'month' (latest month with data) or 'ytd'."""
    start = _optional_date("start")
    end = _optional_date("end")
    if start and end:
        if start > end:
            raise ApiError("start must be on or before end")
        return start, end
    if start or end:
        raise ApiError("start and end go together")
    if default == "month":
        month = _latest_month(conn)
        return month + "-01", month_end(month)
    year = _latest_month(conn)[:4]
    end = f"{year}-12-31"
    if year == str(date.today().year):
        end = date.today().isoformat()
    return f"{year}-01-01", end


def _row_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "date": row["date"],
        "amount_cents": int(row["amount_cents"]),
        "name": row.get("name") or row.get("merchant_name") or row.get("description") or "",
        "merchant_name": row.get("merchant_name") or "",
        "account_id": row.get("account_id") or "",
        "account_name": short_account(row.get("account_name") or ""),
        "status": row.get("status") or "active",
        "pending": bool(row.get("pending")),
        "business_tag": row.get("business_tag") or "needs_review",
        "category": row.get("category") or "",
        "source": row.get("source") or "",
        "confidence": float(row["confidence"]) if row.get("confidence") is not None else None,
        "note": row.get("note") or "",
    }


# --- session and reference data -------------------------------------------


def _session_flags() -> tuple[bool, bool]:
    return request_requires_login(), session_is_authenticated()


@api.get("/session")
def session_info():
    requires, authenticated = _session_flags()
    if requires and not authenticated:
        # No ledger fields until the tailnet client has a session.
        return jsonify(
            {
                "ok": True,
                "product": get_config().product_name,
                "company": get_config().company_name,
                "csrf_token": _ensure_csrf(),
                "requires_login": True,
                "authenticated": False,
            }
        )
    mode = _mode()
    with connect(readonly=True) as conn:
        months = distinct_months(conn, mode)
        count = review_count(conn, mode)
        mode_accounts = accounts_in(conn, mode)
        extra = {}
        if mode == "personal":
            from hpgbooks.personal.api import personal_session_extra

            extra = personal_session_extra(conn)
    return jsonify(
        {
            "ok": True,
            "product": get_config().product_name,
            "company": get_config().company_name,
            "csrf_token": _ensure_csrf(),
            "requires_login": requires,
            "authenticated": authenticated,
            "today": date.today().isoformat(),
            "months": months,
            "latest_month": months[-1] if months else date.today().strftime("%Y-%m"),
            "review_count": count,
            "businesses": list(BUSINESSES),
            "tags": list(BUSINESS_TAGS),
            "categories": list(CATEGORIES),
            "category_groups": {
                "revenue": list(REVENUE_CATEGORIES) + ["Refunds"],
                "cogs": list(COGS_CATEGORIES),
                "opex": list(OPEX_CATEGORIES),
                "other": ["Owner Draw", "Transfer", "Uncategorized"],
            },
            "accounts": [
                {
                    "id": acct["id"],
                    "name": acct["name"],
                    "short_name": short_account(acct["name"]),
                    "type": acct["type"],
                    "last4": acct["last4"],
                    "institution": acct["institution"],
                }
                for acct in mode_accounts
            ],
            **extra,
        }
    )


@api.get("/config")
def site_config():
    """Product name, features, businesses, and WHMCS brands for the web UI.

    Names and feature flags are public (the sign-in screen shows them). The
    business, account, and brand lists need a session when sign-in applies.
    """
    requires, authenticated = _session_flags()
    detail = authenticated or not requires
    return jsonify({"ok": True, **get_config().public_payload(detail=detail)})


@api.post("/login")
def login():
    locked = lockout_payload()
    if locked is not None:
        response = jsonify(locked)
        response.status_code = 429
        response.headers["Retry-After"] = str(locked["retry_after_seconds"])
        return response
    body = _body()
    candidate = body.get("passphrase") if isinstance(body.get("passphrase"), str) else ""
    try:
        with connect() as conn:
            ok = verify_passphrase(conn, candidate)
            # Success, failure, and logout are audited. The passphrase is not.
            audit(conn, "login_success" if ok else "login_failure", actor="web")
    finally:
        body["passphrase"] = ""
        candidate = ""
    if not ok:
        record_login_failure()
        return jsonify({"ok": False, "error": "Sign-in failed"}), 401
    reset_lockout()
    establish_session()
    return jsonify({"ok": True, "authenticated": True})


@api.post("/logout")
def logout():
    was_in = session_is_authenticated()
    clear_login_session()
    if was_in:
        with connect() as conn:
            audit(conn, "logout", actor="web")
    return jsonify({"ok": True, "authenticated": False})


@api.get("/review-count")
def review_count_api():
    mode = _mode()
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, "count": review_count(conn, mode)})


@api.get("/search")
def search():
    query = (request.args.get("q") or "").strip()
    if len(query) > 100:
        raise ApiError("search is too long")
    mode = _mode()
    with connect(readonly=True) as conn:
        if mode == "personal":
            from hpgbooks.personal.queries import personal_search

            return jsonify({"ok": True, **personal_search(conn, query)})
        return jsonify({"ok": True, **global_search(conn, query)})


# --- dashboard ---------------------------------------------------------------


@api.get("/dashboard")
def dashboard():
    business = _business()
    with connect(readonly=True) as conn:
        start, end = _range_or(conn, "month")
        payload = build_dashboard_range(conn, start, end, business)
    return jsonify({"ok": True, **payload})


# --- transactions ------------------------------------------------------------


@api.get("/transactions")
def transactions():
    filters = _transaction_filters()
    limit = _int_arg("limit", 100, 1, TXN_PAGE_MAX)
    with connect(readonly=True) as conn:
        rows, total = query_transactions(conn, limit=limit, offset=filters["offset"], **_txn_query(filters))
    money_in = sum(int(row["amount_cents"]) for row in rows if int(row["amount_cents"]) > 0)
    money_out = sum(-int(row["amount_cents"]) for row in rows if int(row["amount_cents"]) < 0)
    return jsonify(
        {
            "ok": True,
            "rows": [_row_out(row) for row in rows],
            "total": total,
            "offset": filters["offset"],
            "limit": limit,
            "page_in_cents": money_in,
            "page_out_cents": money_out,
        }
    )


@api.get("/transactions/<path:txn_id>")
def transaction_detail(txn_id):
    txn_id = _txn_id(txn_id)
    with connect(readonly=True) as conn:
        try:
            txn = resolve_txn(conn, txn_id)
        except HpgbooksError:
            raise ApiError("no such transaction", 404)
        rows, _total = query_transactions(conn, search=txn["id"], status="all", limit=5)
        row = next((item for item in rows if item["id"] == txn["id"]), None)
        history = conn.execute(
            """
            SELECT id, ts, action, field, old_value, new_value, actor, note
            FROM audit_log WHERE txn_id = ? ORDER BY id DESC LIMIT 20
            """,
            (txn["id"],),
        ).fetchall()
        try:
            pattern = default_pattern(txn)
        except HpgbooksError:
            pattern = ""
    if row is None:
        raise ApiError("no such transaction", 404)
    out = _row_out(row)
    out["description"] = txn.get("description") or ""
    out["provider_category"] = txn.get("provider_category") or ""
    out["suggested_pattern"] = pattern
    out["history"] = [{key: item[key] for key in item.keys()} for item in history]
    return jsonify({"ok": True, "transaction": out})


def _classified_row(conn, txn_id: str) -> dict:
    stored = current_classification(conn, txn_id) or {}
    return {
        "id": txn_id,
        "business_tag": stored.get("business_tag") or "needs_review",
        "category": stored.get("category") or "",
        "source": stored.get("source") or "",
        "confidence": stored.get("confidence"),
        "note": stored.get("note") or "",
    }


@api.post("/classify")
def classify():
    body = _body()
    try:
        txn_id = _txn_id(body.get("txn_id"))
        tag, category, note = _assignment(body)
    except ApiError:
        return jsonify({"ok": False, "error": "txn_id, tag, and category are required"}), 400
    pattern = body.get("pattern") or ""
    if not isinstance(pattern, str) or len(pattern) > 200:
        raise ApiError("note or pattern is too long")
    make_rule = bool(body.get("save_rule"))
    with connect() as conn:
        txn = resolve_txn(conn, txn_id)
        previous = current_classification(conn, txn["id"])
        _txn, applied = classify_manual(
            conn, txn["id"], tag, category, note, make_rule=make_rule, pattern=pattern, actor="web"
        )
        row = _classified_row(conn, txn["id"])
        count = review_count(conn)
    return jsonify(
        {
            "ok": True,
            "applied_others": applied,
            "rule_created": make_rule,
            "previous": previous,
            "row": row,
            "review_count": count,
        }
    )


@api.post("/classify/bulk")
def classify_bulk():
    body = _body()
    ids = body.get("txn_ids")
    if not isinstance(ids, list) or not ids or len(ids) > BULK_MAX:
        raise ApiError(f"txn_ids must list 1 to {BULK_MAX} transactions")
    ids = [_txn_id(item) for item in ids]
    if len(set(ids)) != len(ids):
        raise ApiError("txn_ids has duplicates")
    tag, category, note = _assignment(body)
    previous = {}
    with connect() as conn:
        for txn_id in ids:
            txn = resolve_txn(conn, txn_id)
            previous[txn["id"]] = current_classification(conn, txn["id"])
            classify_manual(conn, txn["id"], tag, category, note, actor="web")
        rows = [_classified_row(conn, txn_id) for txn_id in previous]
        count = review_count(conn)
    return jsonify(
        {
            "ok": True,
            "count": len(rows),
            "rows": rows,
            "previous": [{"txn_id": key, "previous": value} for key, value in previous.items()],
            "review_count": count,
        }
    )


@api.post("/classify/undo")
def classify_undo():
    body = _body()
    items = body.get("items")
    if not isinstance(items, list) or not items or len(items) > BULK_MAX:
        raise ApiError("items must list the rows to restore")
    with connect() as conn:
        for item in items:
            if not isinstance(item, dict):
                raise ApiError("invalid undo item")
            txn_id = _txn_id(item.get("txn_id"))
            previous = item.get("previous")
            if previous is not None and not isinstance(previous, dict):
                raise ApiError("invalid undo item")
            restore_classification(conn, txn_id, previous, actor="web")
        rows = [_classified_row(conn, _txn_id(item.get("txn_id"))) for item in items]
        count = review_count(conn)
    return jsonify({"ok": True, "rows": rows, "review_count": count})


# --- accounts ----------------------------------------------------------------


@api.get("/accounts")
def accounts():
    with connect(readonly=True) as conn:
        start, end = _range_or(conn, "month")
        rows = accounts_range(conn, start, end)
    return jsonify({"ok": True, "start": start, "end": end, "rows": rows})


@api.get("/accounts/<account_id>/register")
def register(account_id):
    with connect(readonly=True) as conn:
        account = _business_account(conn, account_id)
    search = (request.args.get("search") or "").strip()
    if len(search) > 200:
        raise ApiError("search is too long")
    start = _optional_date("start")
    end = _optional_date("end")
    if start and end and start > end:
        raise ApiError("start must be on or before end")
    status = request.args.get("status") or "active"
    if status not in ("active", "superseded", "all"):
        raise ApiError("status must be active, superseded, or all")
    offset = _int_arg("offset", 0, 0, 1_000_000)
    limit = _int_arg("limit", 100, 1, TXN_PAGE_MAX)
    with connect(readonly=True) as conn:
        rows, total, balance = account_register(
            conn,
            account_id,
            search=search or None,
            date_from=start,
            date_to=end,
            status=status,
            limit=limit,
            offset=offset,
        )
        snap = snapshot_for(conn, account_id)
    out = []
    for row in rows:
        item = _row_out({**row, "account_id": account_id})
        item["payment_cents"] = row["payment_cents"]
        item["deposit_cents"] = row["deposit_cents"]
        item["running_cents"] = row["running_cents"]
        item["in_balance"] = bool(row.get("in_balance"))
        out.append(item)
    return jsonify(
        {
            "ok": True,
            "account": {
                "id": account["id"],
                "name": account["name"],
                "short_name": short_account(account["name"]),
                "type": account["type"],
                "last4": account["last4"],
                "institution": account["institution"],
                "notes": account["notes"],
            },
            "balance_cents": balance,
            "display_cents": snap["display_cents"],
            "real_balance_cents": snap["real_balance_cents"],
            "opening_cents": snap["opening_cents"],
            "anchored": snap["anchored"],
            "as_of_date": snap["as_of_date"],
            "source": snap["source"],
            "anchor_cents": snap["anchor_cents"],
            "anchor_note": snap["anchor_note"],
            "drift_cents": snap["drift_cents"],
            "computed_cents": snap["computed_cents"],
            "reconcile_cents": snap["reconcile_cents"],
            "rows": out,
            "total": total,
            "offset": offset,
            "limit": limit,
        }
    )


# --- reports -----------------------------------------------------------------


def _pnl_json(report, compare: bool) -> dict:
    return {
        "title": report.title,
        "generated": report.generated,
        "business": report.business,
        "by": report.by,
        "columns": report.columns,
        "column_keys": _column_keys(report),
        "rows": [
            {
                "label": row.label,
                "kind": row.kind,
                "values": row.values,
                "prior": report.prior_values[index] if compare and report.prior_values else None,
                "pct": report.pct_values[index] if compare and report.pct_values else None,
            }
            for index, row in enumerate(report.rows)
        ],
        "net_income_cents": report.net_income_cents,
        "owner_draw_cents": report.owner_draw_cents,
        "transfer_count": report.transfer_count,
        "transfer_cents": report.transfer_cents,
        "prior_start": report.prior_start,
        "prior_end": report.prior_end,
    }


def _column_keys(report) -> list[str]:
    """Machine keys beside the display columns: YYYY-MM per month, '' for a year column."""
    if report.by == "year":
        return ["", "Total"]
    return [datetime.strptime(label, "%b %Y").strftime("%Y-%m") for label in report.columns[:-1]] + ["Total"]


@api.get("/pnl")
def pnl():
    business = _business()
    by = request.args.get("by") or "month"
    if by not in ("month", "year"):
        raise ApiError("by must be month or year")
    compare = request.args.get("compare", "1") != "0"
    with connect(readonly=True) as conn:
        start, end = _range_or(conn, "ytd")
        report = build_pnl(conn, int(start[:4]), by=by, business=business, start=start, end=end)
        if compare:
            prior_start, prior_end = compare_window(start, end)
            prior = build_pnl(conn, int(prior_start[:4]), by="year", business=business, start=prior_start, end=prior_end)
            attach_prior(report, prior, prior_start, prior_end)
    return jsonify({"ok": True, "start": start, "end": end, **_pnl_json(report, compare)})


@api.get("/pnl/lines")
def pnl_lines():
    business = _business()
    label = (request.args.get("label") or "").strip()
    if not label or len(label) > 120:
        raise ApiError("label is required")
    month = (request.args.get("month") or "").strip() or None
    with connect(readonly=True) as conn:
        start, end = _range_or(conn, "ytd")
        rows = pnl_line_transactions(conn, start, end, business, label, month)
    return jsonify(
        {
            "ok": True,
            "label": label,
            "month": month,
            "rows": rows,
            "total_cents": sum(row["amount_cents"] for row in rows),
        }
    )


@api.get("/reports/<name>")
def report(name):
    if name not in REPORTS:
        raise ApiError("no such report", 404)
    business = _business()
    limit = _int_arg("limit", 25, 1, 100)
    with connect(readonly=True) as conn:
        start, end = _range_or(conn, "ytd")
        if name == "pnl-by-business":
            table = pnl_by_business(conn, start, end)
        elif name == "expenses-by-vendor":
            _rows, table = vendor_report(conn, start, end, business, limit)
        elif name == "cash-flow":
            table = cash_flow_report(conn, start, end, business)
        elif name == "owner-draws":
            table = owner_draws_report(conn, start, end)
        else:
            table = schedule_c_report(conn, start, end, business)
    return jsonify({"ok": True, "name": name, "heading": REPORTS[name], "start": start, "end": end, **tabular_json(table)})


@api.get("/vendors")
def vendors():
    business = _business()
    limit = _int_arg("limit", 100, 1, 500)
    with connect(readonly=True) as conn:
        start, end = _range_or(conn, "ytd")
        rows, _table = vendor_report(conn, start, end, business, limit=10_000)
        suggestions = vendor_suggestions(conn, start, end, business)
    total = sum(row["spend_cents"] for row in rows)
    return jsonify(
        {
            "ok": True,
            "start": start,
            "end": end,
            "rows": rows[:limit],
            "vendor_count": len(rows),
            "total_spend_cents": total,
            "suggestions": suggestions,
        }
    )


def _public_balance(snap: dict) -> dict:
    return {
        "id": snap["id"],
        "anchored": snap["anchored"],
        "display_cents": snap["display_cents"],
        "real_balance_cents": snap["real_balance_cents"],
        "opening_cents": snap["opening_cents"],
        "as_of_date": snap["as_of_date"],
        "anchor_cents": snap["anchor_cents"],
        "source": snap["source"],
        "anchor_note": snap["anchor_note"],
        "drift_cents": snap["drift_cents"],
        "computed_cents": snap["computed_cents"],
        "reconcile_cents": snap["reconcile_cents"],
        "activity_cents": snap["activity_cents"],
    }


@api.get("/balances")
def balances_list():
    account_id = (request.args.get("account") or "").strip()
    with connect(readonly=True) as conn:
        if account_id:
            _business_account(conn, account_id)
            history = anchor_history(conn, account_id)
            snap = snapshot_for(conn, account_id)
            return jsonify({"ok": True, "balance": _public_balance(snap), "history": history})
        from hpgbooks.balances import account_snapshots

        snaps = account_snapshots(conn)
    return jsonify({"ok": True, "rows": [_public_balance(snap) for snap in snaps]})


@api.post("/balances")
def balances_set():
    body = _body()
    account_id = _str_field(body, "account_id", max_len=80, required=True).strip()
    with connect(readonly=True) as conn:
        _business_account(conn, account_id)
    if "balance" not in body:
        raise ApiError("balance is required")
    as_of = _str_field(body, "as_of", max_len=10, required=True).strip()
    source = _str_field(body, "source", max_len=20).strip() or "statement"
    note = _str_field(body, "note", max_len=500)
    try:
        cents = parse_dollars(body.get("balance"))
    except HpgbooksError as exc:
        raise ApiError(str(exc))
    with connect() as conn:
        snap = set_anchor(conn, account_id, cents, as_of, source, note, actor="web")
    return jsonify({"ok": True, "balance": _public_balance(snap)})


@api.post("/vendors/merge")
def vendors_merge():
    body = _body()
    names = body.get("names")
    if not isinstance(names, list) or not names or len(names) > 50:
        raise ApiError("names must list two or more vendors")
    if any(not isinstance(item, str) for item in names):
        raise ApiError("names must be text")
    into = _str_field(body, "into", max_len=200, required=True)
    with connect() as conn:
        result = merge_vendors(conn, names, into, actor="web")
    return jsonify({"ok": True, **result})


@api.post("/vendors/unmerge")
def vendors_unmerge():
    body = _body()
    alias = _str_field(body, "alias", max_len=200, required=True)
    with connect() as conn:
        result = unmerge_vendor(conn, alias, actor="web")
    return jsonify({"ok": True, **result})


@api.post("/vendors/rename")
def vendors_rename():
    body = _body()
    old = _str_field(body, "canonical", max_len=200, required=True)
    new = _str_field(body, "name", max_len=200, required=True)
    with connect() as conn:
        result = rename_canonical(conn, old, new, actor="web")
    return jsonify({"ok": True, **result})


@api.get("/vendors/aliases")
def vendors_aliases():
    with connect(readonly=True) as conn:
        rows = list_aliases(conn)
    return jsonify({"ok": True, "rows": rows})


# --- review ------------------------------------------------------------------


@api.get("/review")
def review():
    year_text = request.args.get("year") or ""
    if year_text and (not year_text.isdigit() or len(year_text) != 4):
        raise ApiError("year must be YYYY")
    with connect(readonly=True) as conn:
        rows = query_review(conn, int(year_text) if year_text else None)
        suggestions = review_suggestions(conn, rows)
    out = []
    for row in rows:
        item = _row_out(row)
        item["suggestion"] = suggestions.get(row["id"])
        out.append(item)
    return jsonify({"ok": True, "rows": out, "count": len(out)})


# --- rules -------------------------------------------------------------------


@api.get("/rules")
def rules():
    with connect(readonly=True) as conn:
        names = {acct["id"]: acct["name"] for acct in accounts_in(conn, "business")}
        rows = query_rules(conn)
        hits = {
            row["rule_id"]: int(row["n"])
            for row in conn.execute(
                "SELECT rule_id, COUNT(*) AS n FROM classifications WHERE rule_id IS NOT NULL GROUP BY rule_id"
            )
        }
    for row in rows:
        row["active"] = bool(row["active"])
        row["hits"] = hits.get(row["id"], 0)
        row["account_name"] = short_account(names.get(row.get("account_id"), "")) if row.get("account_id") else ""
    return jsonify({"ok": True, "rows": rows})


@api.get("/rules/preview")
def rules_preview():
    pattern = request.args.get("pattern") or ""
    field = request.args.get("field") or "any"
    with connect(readonly=True) as conn:
        result = preview_rule(conn, pattern, field=field)
    return jsonify({"ok": True, **result})


@api.post("/rules/<int:rule_id>/active")
def rule_active(rule_id):
    body = _body()
    active = body.get("active")
    if not isinstance(active, bool):
        raise ApiError("active must be true or false")
    with connect() as conn:
        if active:
            enable_rule(conn, rule_id, actor="web")
        else:
            disable_rule(conn, rule_id, actor="web")
    return jsonify({"ok": True, "id": rule_id, "active": active})


# --- calendar and settings ---------------------------------------------------


def _reserve(conn) -> int:
    raw = get_setting(conn, "reserve_cents", "0") or "0"
    try:
        return int(raw)
    except ValueError:
        return 0


@api.get("/calendar")
def calendar():
    business = _business()
    with connect(readonly=True) as conn:
        reserve = _reserve(conn)
        items = recurring_items(conn, business=business)
        outlook = cash_outlook(conn, business=business, reserve_cents=reserve)
        upcoming = upcoming_bills(conn, business=business, days=60)
    return jsonify({"ok": True, "items": items, "outlook": outlook, "upcoming": upcoming, "reserve_cents": reserve})


@api.get("/settings")
def settings():
    with connect(readonly=True) as conn:
        reserve = _reserve(conn)
    return jsonify({"ok": True, "reserve_cents": reserve})


@api.post("/settings")
def settings_update():
    body = _body()
    unknown = set(body) - {"reserve_cents", "csrf_token"}
    if unknown:
        raise ApiError("unknown setting")
    cents = body.get("reserve_cents")
    if isinstance(cents, bool) or not isinstance(cents, int) or cents < 0 or cents > RESERVE_MAX_CENTS:
        raise ApiError("reserve_cents must be a whole number of cents from 0 to 10,000,000,000")
    with connect() as conn:
        set_setting(conn, "reserve_cents", str(int(cents)), actor="web")
        reserve = _reserve(conn)
    return jsonify({"ok": True, "reserve_cents": reserve})


# --- audit -------------------------------------------------------------------


@api.get("/audit")
def audit_log():
    limit = _int_arg("limit", 200, 1, 1000)
    mode = _mode()
    with connect(readonly=True) as conn:
        rows = query_audit(conn, limit, mode)
    return jsonify({"ok": True, "rows": rows})


# --- account settings (every scope; changing scope moves reports between modes) ---


@api.get("/accounts/settings")
def accounts_settings():
    from hpgbooks.accounts_admin import list_accounts

    with connect(readonly=True) as conn:
        rows = list_accounts(conn)
    counts = {scope: sum(1 for row in rows if row["scope"] == scope) for scope in ("business", "personal", "excluded")}
    return jsonify({"ok": True, "rows": rows, "counts": counts})


@api.patch("/accounts/<account_id>/settings")
def account_settings_update(account_id):
    from hpgbooks.accounts_admin import update_settings

    body = _body()
    changes = {key: value for key, value in body.items() if key != "csrf_token"}
    if not changes:
        raise ApiError("nothing to change")
    if len(account_id) > 80:
        raise ApiError("no such account", 404)
    with connect() as conn:
        if get_account(conn, account_id) is None:
            raise ApiError("no such account", 404)
        result = update_settings(conn, account_id, changes, actor="web")
    return jsonify({"ok": True, **result})


@api.route("/<path:_rest>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def not_found(_rest):
    abort(404)
