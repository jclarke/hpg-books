"""Local Flask server: the JSON API, file exports, and the React app. Binds to 127.0.0.1 only."""

from __future__ import annotations

import io
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from hpgbooks.access import load_allowed_hosts, passphrase_configured, request_requires_login, session_is_authenticated

from hpgbooks.analytics import (
    cash_flow_report,
    compare_window,
    owner_draws_report,
    pnl_by_business,
    schedule_c_report,
    table_csv,
    transactions_csv,
    vendor_report,
    write_table_pdf,
    write_table_xlsx,
)
from hpgbooks.api import api
from hpgbooks.config import get_config
from hpgbooks.margins_api import margins_api
from hpgbooks.payments_api import payments_api
from hpgbooks.personal.api import personal_api
from hpgbooks.whmcs_api import export_csv as whmcs_export_csv, whmcs_api
from hpgbooks.db import HpgbooksError, connect
from hpgbooks.reports import (
    attach_prior,
    build_pnl,
    distinct_months,
    pnl_csv,
    query_transactions,
    with_prior_period,
    write_pdf,
    write_xlsx,
)
from hpgbooks.webargs import (
    _business,
    _csrf_ok,
    _host_ok,
    _int_arg,
    _optional_date,
    _origin_ok,
    _range,
    _transaction_filters,
    _txn_query,
    _year_arg,
)

HOST = "127.0.0.1"
EXPORT_LIMIT = 20000
# Client-side routes of the React app. Anything else that is not an API,
# export, or asset path gets the app with a 404 status.
SPA_ROUTES = (
    "",
    "transactions",
    "accounts",
    "reports",
    "pnl",
    "vendors",
    "review",
    "calendar",
    "rules",
    "audit",
    "settings",
    "whmcs",
    "personal",
)

# Routes that serve either mode and read ?mode= themselves. Every other /api or
# /export route belongs to one mode: /api/personal and /export/personal need
# mode=personal; the rest are business routes and refuse mode=personal.
_SHARED_MODE_ROUTES = frozenset(
    {
        "/api/session",
        "/api/config",
        "/api/login",
        "/api/logout",
        "/api/search",
        "/api/audit",
        "/api/review-count",
        "/api/settings",
    }
)


def _is_account_settings(path: str) -> bool:
    """The accounts settings screen lists every scope, so it serves both modes."""
    return path == "/api/accounts/settings" or (path.startswith("/api/accounts/") and path.endswith("/settings"))


def _mode_mismatch() -> str | None:
    """'bad' for an unknown mode, 'wrong' when the mode does not own the route."""
    path = request.path
    if not (path.startswith("/api/") or path.startswith("/export/")):
        return None
    mode = request.args.get("mode") or "business"
    if mode not in ("business", "personal"):
        return "bad"
    if path in _SHARED_MODE_ROUTES or _is_account_settings(path) or path == "/api/payments" or path.startswith("/api/payments/"):
        return None
    personal_route = path.startswith("/api/personal/") or path.startswith("/export/personal/")
    if personal_route != (mode == "personal"):
        return "wrong"
    return None


# Anonymous routes a non-loopback client may call before it has a session.
# The static app is also public so the sign-in screen can load; ledger routes are not.
_OPEN_API = frozenset(
    {
        ("GET", "/api/session"),
        ("GET", "/api/config"),
        ("POST", "/api/login"),
        ("POST", "/api/logout"),
    }
)


def _disabled_feature_route() -> str | None:
    """The error for a WHMCS, margins, or personal route when that feature is off in the config."""
    path = request.path
    cfg = get_config()
    if not cfg.whmcs_enabled and (
        path.startswith(("/api/whmcs/", "/api/margins/", "/export/whmcs/")) or path in ("/api/whmcs", "/api/margins")
    ):
        return "WHMCS integration is disabled"
    if not cfg.margins_enabled and (path == "/api/margins" or path.startswith("/api/margins/")):
        return "Server margins are disabled"
    if not cfg.personal_enabled and (path == "/api/personal" or path.startswith(("/api/personal/", "/export/personal/"))):
        return "Personal mode is disabled"
    return None


def _remote_auth_required() -> bool:
    if (request.method, request.path) in _OPEN_API:
        return False
    return request.path.startswith("/api/") or request.path.startswith("/export/")


def _sign_in_not_configured():
    message = "Sign-in is not configured"
    if request.path.startswith("/api") or request.path.startswith("/export"):
        response = jsonify({"ok": False, "error": message})
        response.status_code = 503
        return response
    return Response(message + "\n", status=503, mimetype="text/plain")


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False
    app.config["SECRET_KEY"] = secrets.token_hex(32)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    # The browser talks HTTP to the tailnet TCP proxy. WireGuard already encrypts
    # that path, and a Secure cookie would not be stored on http://.
    app.config["SESSION_COOKIE_SECURE"] = False
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
    app.config["SESSION_REFRESH_EACH_REQUEST"] = True
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    app.config["ALLOWED_HOSTS"] = load_allowed_hosts()
    app.register_blueprint(api)
    app.register_blueprint(whmcs_api)
    app.register_blueprint(margins_api)
    app.register_blueprint(payments_api)
    app.register_blueprint(personal_api)

    @app.before_request
    def guard_requests():
        if not _host_ok():
            abort(403)
        if request_requires_login():
            if not passphrase_configured():
                return _sign_in_not_configured()
            if _remote_auth_required() and not session_is_authenticated():
                abort(401)
        disabled = _disabled_feature_route()
        if disabled:
            response = jsonify({"ok": False, "error": disabled})
            response.status_code = 404
            return response
        mismatch = _mode_mismatch()
        if mismatch == "bad":
            abort(400)
        if mismatch == "wrong":
            response = jsonify({"ok": False, "error": "not available in this mode"})
            response.status_code = 404
            return response
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return None
        if not _origin_ok():
            abort(403)
        if not _csrf_ok():
            abort(403)
        return None

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        if request.path.startswith("/api/") or request.path.startswith("/export/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(400)
    def bad_request(_err):
        if request.path.startswith("/api"):
            return jsonify({"ok": False, "error": "bad request"}), 400
        return Response("bad request\n", status=400, mimetype="text/plain")

    @app.errorhandler(401)
    def unauthorized(_err):
        if request.path.startswith("/api") or request.path.startswith("/export"):
            return jsonify({"ok": False, "error": "sign in required"}), 401
        return Response("sign in required\n", status=401, mimetype="text/plain")

    @app.errorhandler(403)
    def forbidden(_err):
        if request.path.startswith("/api"):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        return Response("forbidden\n", status=403, mimetype="text/plain")

    @app.errorhandler(404)
    def not_found(_err):
        if request.path.startswith("/api"):
            return jsonify({"ok": False, "error": "not found"}), 404
        return Response("not found\n", status=404, mimetype="text/plain")

    @app.errorhandler(405)
    def not_allowed(_err):
        if request.path.startswith("/api"):
            return jsonify({"ok": False, "error": "method not allowed"}), 405
        return Response("method not allowed\n", status=405, mimetype="text/plain")

    @app.errorhandler(413)
    def too_large(_err):
        return jsonify({"ok": False, "error": "request is too large"}), 413

    @app.get("/healthz")
    def healthz():
        try:
            with connect(readonly=True) as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception:
            return jsonify({"ok": False}), 500
        return jsonify({"ok": True})

    @app.get("/app")
    @app.get("/app/")
    @app.get("/app/<path:asset>")
    def spa_assets(asset: str = ""):
        """The built React app. Real files are served as-is; other paths get index.html."""
        return _spa_response(asset)

    @app.get("/favicon.ico")
    @app.get("/favicon.svg")
    def favicon():
        return _spa_response("favicon.svg")

    @app.get("/")
    @app.get("/<path:route>")
    def spa_routes(route: str = ""):
        """Client-side routes (/, /transactions, /reports/pnl, …) all load the app."""
        first = route.split("/", 1)[0]
        if first in ("api", "export", "app", "static"):
            abort(404)
        response = _spa_response("")
        if first not in SPA_ROUTES and response.status_code == 200:
            response.status_code = 404
        return response

    @app.get("/export/transactions.csv")
    def export_transactions():
        filters = _transaction_filters()
        with connect(readonly=True) as conn:
            if "month" not in request.args and not (filters["start"] or filters["end"]):
                months = distinct_months(conn)
                filters["month"] = months[-1] if months else ""
            rows, _total = query_transactions(
                conn, limit=EXPORT_LIMIT, offset=0, **_txn_query(filters)
            )
        return Response(
            transactions_csv(rows),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=transactions.csv"},
        )

    @app.get("/export/pnl.csv")
    def export_csv():
        report = _report_from_request()
        filename = f"pnl-{report.year}-{report.business}.csv"
        return Response(
            pnl_csv(report),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.get("/export/pnl.xlsx")
    def export_xlsx():
        report = _report_from_request()
        return _download(report, "xlsx", write_xlsx)

    @app.get("/export/pnl.pdf")
    def export_pdf():
        report = _report_from_request()
        return _download(report, "pdf", write_pdf)

    @app.get("/export/whmcs/<table>.csv")
    def export_whmcs(table):
        text, filename = whmcs_export_csv(table)
        return Response(
            text,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.get("/export/<slug>")
    def export_slug(slug):
        if slug.count(".") != 1:
            abort(404)
        name, ext = slug.rsplit(".", 1)
        if ext not in ("csv", "xlsx", "pdf") or name not in _REPORTS:
            abort(404)
        report = _table_from_request(name)
        filename = f"{name}.{ext}"
        if ext == "csv":
            return Response(
                table_csv(report),
                mimetype="text/csv",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )
        buffer = io.BytesIO()
        (write_table_xlsx if ext == "xlsx" else write_table_pdf)(report, buffer)
        mimetype = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if ext == "xlsx"
            else "application/pdf"
        )
        return Response(
            buffer.getvalue(),
            mimetype=mimetype,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    return app


_REPORTS = (
    "pnl-by-business",
    "expenses-by-vendor",
    "cash-flow",
    "owner-draws",
    "schedule-c",
)


def _build_table(conn, name: str, start: str, end: str, business: str, limit: int):
    if name == "pnl-by-business":
        return pnl_by_business(conn, start, end)
    if name == "expenses-by-vendor":
        _rows, report = vendor_report(conn, start, end, business, limit)
        return report
    if name == "cash-flow":
        return cash_flow_report(conn, start, end, business)
    if name == "owner-draws":
        return owner_draws_report(conn, start, end)
    if name == "schedule-c":
        return schedule_c_report(conn, start, end, business)
    abort(404)


def _table_from_request(name: str):
    start, end = _range()
    business = _business()
    limit = _int_arg("limit", 25, 1, 100)
    try:
        with connect(readonly=True) as conn:
            return _build_table(conn, name, start, end, business, limit)
    except HpgbooksError:
        abort(400)


def _report_from_request():
    year = _year_arg()
    by = request.args.get("by") or "month"
    business = _business()
    ytd = request.args.get("ytd") == "1"
    if by not in ("month", "year"):
        abort(400)
    start = _optional_date("start")
    end = _optional_date("end")
    if (start is None) ^ (end is None):
        abort(400)
    try:
        with connect(readonly=True) as conn:
            if start and end:
                # Same comparison window as the P&L screen.
                if start > end:
                    abort(400)
                report = build_pnl(conn, year, by=by, business=business, start=start, end=end)
                prior_start, prior_end = compare_window(start, end)
                prior = build_pnl(conn, int(prior_start[:4]), by="year", business=business, start=prior_start, end=prior_end)
                return attach_prior(report, prior, prior_start, prior_end)
            return with_prior_period(conn, year, by=by, business=business, ytd=ytd)
    except HpgbooksError:
        abort(400)


def _download(report, ext: str, writer) -> Response:
    """Build the export in memory so a temp file is never left world-readable."""
    filename = f"pnl-{report.year}-{report.business}.{ext}"
    buffer = io.BytesIO()
    writer(report, buffer)
    data = buffer.getvalue()
    mimetype = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if ext == "xlsx"
        else "application/pdf"
    )
    return Response(
        data,
        mimetype=mimetype,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


SPA_DIR = Path(__file__).parent / "static" / "app"
# No inline script or style is allowed. React sets styles through the CSSOM, which CSP permits.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def _spa_response(asset: str) -> Response:
    index = SPA_DIR / "index.html"
    if not index.is_file():
        return Response(
            "The web UI is not built. Run bin/build-ui.\n", status=503, mimetype="text/plain"
        )
    if asset:
        target = (SPA_DIR / asset).resolve()
        if SPA_DIR.resolve() in target.parents and target.is_file():
            response = send_from_directory(SPA_DIR, asset)
            if asset.startswith("assets/"):
                # File names carry a content hash.
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
            return response
        if asset.startswith("assets/") or "." in asset.rsplit("/", 1)[-1]:
            abort(404)
    response = send_from_directory(SPA_DIR, "index.html")
    response.headers["Cache-Control"] = "no-cache"
    return response


def main() -> None:
    create_app().run(host=HOST, port=8765, debug=False, use_reloader=False)
