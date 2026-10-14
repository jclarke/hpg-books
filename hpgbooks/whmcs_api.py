"""JSON API for the WHMCS pages. Mounted under /api/whmcs. Read-only.

The app-wide guard in web.py already requires a signed-in session for every
/api route on a non-loopback Host. The customer routes, which return names and
emails, check it again here so they can never be served without sign-in when
sign-in applies.
"""

from __future__ import annotations

from flask import Blueprint, abort, jsonify, request

from hpgbooks import whmcs_reports as wr
from hpgbooks.access import request_requires_login, session_is_authenticated
from hpgbooks.db import HpgbooksError, connect
from hpgbooks.webargs import _int_arg, _optional_date
from hpgbooks.whmcs import brand_names, sync_status, whmcs_ready

whmcs_api = Blueprint("whmcs_api", __name__, url_prefix="/api/whmcs")


@whmcs_api.errorhandler(HpgbooksError)
def _hpgbooks_error(err: HpgbooksError):
    return jsonify({"ok": False, "error": str(err)}), 400


def _brand() -> str | None:
    raw = (request.args.get("brand") or "all").strip()
    if len(raw) > 40:
        abort(400)
    return wr.check_brand(raw)


def _dates() -> tuple[str | None, str | None]:
    start = _optional_date("start")
    end = _optional_date("end")
    if (start is None) != (end is None):
        raise HpgbooksError("start and end go together")
    if start and end and start > end:
        raise HpgbooksError("start must be on or before end")
    return start, end


def _by() -> str:
    by = request.args.get("by") or "month"
    if by not in ("month", "quarter", "year"):
        raise HpgbooksError("by must be month, quarter, or year")
    return by


def _not_synced():
    return jsonify({"ok": True, "ready": False, "brands": list(brand_names())})


def _require_signed_in() -> None:
    if request_requires_login() and not session_is_authenticated():
        abort(401)


def build(name: str, conn) -> dict:
    """Run one report from the query string. Shared by the API and the CSV exports."""
    if name == "revenue":
        start, end = _dates()
        return wr.revenue(conn, start=start, end=end, brand=_brand(), by=_by())
    if name == "mrr":
        return wr.mrr(conn, brand=_brand(), months=_int_arg("months", 36, 0, 600))
    if name == "churn":
        start, end = _dates()
        return wr.churn(conn, start=start, end=end, brand=_brand())
    if name == "refunds":
        start, end = _dates()
        return wr.refunds(conn, start=start, end=end, brand=_brand(), limit=_int_arg("limit", 25, 1, 500))
    if name == "dunning":
        start, end = _dates()
        return wr.dunning(conn, start=start, end=end, brand=_brand(), limit=_int_arg("limit", 1000, 1, 5000))
    if name == "reconcile":
        start, end = _dates()
        return wr.reconcile_paypal(conn, start=start, end=end, window=_int_arg("window", 3, 0, 15))
    abort(404)


@whmcs_api.get("/status")
def status():
    with connect(readonly=True) as conn:
        data = sync_status(conn, log_limit=_int_arg("limit", 30, 1, 200))
    return jsonify({"ok": True, **data})


@whmcs_api.get("/summary")
def summary():
    with connect(readonly=True) as conn:
        data = wr.summary(conn)
    return jsonify({"ok": True, **data})


@whmcs_api.get("/<any(revenue, mrr, churn, refunds, dunning, reconcile):name>")
def report(name):
    with connect(readonly=True) as conn:
        if not whmcs_ready(conn):
            return _not_synced()
        data = build(name, conn)
    return jsonify({"ok": True, "ready": True, **data})


@whmcs_api.post("/customers/search")
def customers():
    """POST so the search text (often a name or email) never lands in a request log line.
    It reads only; the CSRF and Origin checks in web.py still apply."""
    _require_signed_in()
    body = request.get_json(silent=True)
    if not request.is_json or not isinstance(body, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 415
    query = body.get("q") if isinstance(body.get("q"), str) else ""
    query = query.strip()
    if len(query) > 100:
        raise HpgbooksError("search is too long")
    brand = body.get("brand") if isinstance(body.get("brand"), str) else "all"
    limit = body.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 200:
        raise HpgbooksError("limit must be 1 to 200")
    with connect(readonly=True) as conn:
        if not whmcs_ready(conn):
            return _not_synced()
        rows = wr.customer_search(conn, query, brand=wr.check_brand(brand[:40]), limit=limit)
    return jsonify({"ok": True, "ready": True, "rows": rows})


@whmcs_api.get("/customers/<brand>/<int:client_id>")
def customer(brand, client_id):
    _require_signed_in()
    if brand not in brand_names():
        abort(404)
    with connect(readonly=True) as conn:
        if not whmcs_ready(conn):
            return _not_synced()
        data = wr.customer_detail(conn, brand, client_id)
    if data is None:
        abort(404)
    return jsonify({"ok": True, "ready": True, "customer": data})


@whmcs_api.route("/<path:_rest>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def not_found(_rest):
    abort(404)


# CSV exports: /export/whmcs/<table>.csv. Same query string as the report. No names or emails.
EXPORT_TABLES = {
    "revenue": "revenue",
    "revenue-plans": "revenue",
    "mrr": "mrr",
    "mrr-trend": "mrr",
    "churn": "churn",
    "churn-plans": "churn",
    "refunds": "refunds",
    "refunds-largest": "refunds",
    "dunning": "dunning",
    "dunning-aging": "dunning",
    "reconcile": "reconcile",
    "reconcile-rows": "reconcile",
    "gateways": "reconcile",
}


def export_csv(table: str) -> tuple[str, str]:
    if table not in EXPORT_TABLES:
        abort(404)
    try:
        with connect(readonly=True) as conn:
            if not whmcs_ready(conn):
                abort(404)
            data = build(EXPORT_TABLES[table], conn)
    except HpgbooksError:
        abort(400)
    return wr.table_csv(table, data), f"whmcs-{table}.csv"
