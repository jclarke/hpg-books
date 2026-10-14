"""JSON API for the server margins page. Mounted under /api/margins.

The report carries customer names, server labels, and mapping values (domains,
service ids), so every route checks sign-in here as well as in the app-wide
guard in web.py, like the WHMCS customer routes. Edits change only the margin_*
tables, each with an audit_log row; WHMCS data is never written. CSRF, Origin,
and the JSON body checks in web.py run before any POST reaches these handlers.
"""

from __future__ import annotations

from flask import Blueprint, abort, jsonify, request

from hpgbooks import margins as mg
from hpgbooks.access import request_requires_login, session_is_authenticated
from hpgbooks.db import HpgbooksError, connect
from hpgbooks.whmcs import brand_names

margins_api = Blueprint("margins_api", __name__, url_prefix="/api/margins")


@margins_api.errorhandler(HpgbooksError)
def _hpgbooks_error(err: HpgbooksError):
    message = str(err)
    return jsonify({"ok": False, "error": message}), 404 if message.startswith("no such") else 400


@margins_api.before_request
def _require_signed_in():
    if request_requires_login() and not session_is_authenticated():
        abort(401)


def _body(allowed: set[str]) -> dict:
    body = request.get_json(silent=True)
    if not request.is_json or not isinstance(body, dict):
        return abort(415)
    unknown = set(body) - allowed - {"csrf_token"}
    if unknown:
        raise HpgbooksError(f"unknown field {sorted(unknown)[0]}")
    return body


def _report(conn) -> dict:
    data = mg.build(conn)
    if not data["ready"]:
        return {"ok": True, "ready": False, "brands": list(brand_names())}
    return {"ok": True, **data}


@margins_api.get("")
def report():
    with connect(readonly=True) as conn:
        return jsonify(_report(conn))


@margins_api.post("/servers/<int:server_id>")
def server_update(server_id):
    body = _body({"status", "notes"})
    with connect() as conn:
        server = mg.update_server(conn, server_id, status=body.get("status"), notes=body.get("notes"), actor="web")
    return jsonify({"ok": True, "server": server})


@margins_api.post("/servers/<int:server_id>/costs")
def cost_add(server_id):
    body = _body({"component", "monthly_cost", "effective"})
    with connect() as conn:
        cost = mg.add_cost(conn, server_id, body.get("component"), body.get("monthly_cost"), body.get("effective"), actor="web")
    return jsonify({"ok": True, "cost": cost})


@margins_api.post("/costs/<int:cost_id>")
def cost_update(cost_id):
    body = _body({"component", "monthly_cost", "effective"})
    with connect() as conn:
        cost = mg.update_cost(conn, cost_id, component=body.get("component"), monthly_cost=body.get("monthly_cost"), effective=body.get("effective"), actor="web")
    return jsonify({"ok": True, "cost": cost})


@margins_api.post("/costs/<int:cost_id>/delete")
def cost_delete(cost_id):
    _body(set())
    with connect() as conn:
        mg.delete_cost(conn, cost_id, actor="web")
    return jsonify({"ok": True})


@margins_api.post("/mappings")
def mapping_add():
    body = _body({"server_id", "rule_type", "rule_value", "allocation", "brand_scope", "note"})
    server_id = body.get("server_id")
    if isinstance(server_id, bool) or not isinstance(server_id, int):
        raise HpgbooksError("server_id must be a number")
    with connect() as conn:
        mapping = mg.add_mapping(
            conn,
            server_id,
            body.get("rule_type"),
            body.get("rule_value"),
            allocation=body.get("allocation", "direct"),
            brand_scope=body.get("brand_scope"),
            note=body.get("note"),
            actor="web",
        )
    return jsonify({"ok": True, "mapping": mapping})


@margins_api.post("/mappings/<int:mapping_id>/delete")
def mapping_delete(mapping_id):
    _body(set())
    with connect() as conn:
        mg.delete_mapping(conn, mapping_id, actor="web")
    return jsonify({"ok": True})


@margins_api.post("/overhead")
def overhead_add():
    body = _body({"label", "vendor", "monthly_cost", "kind", "note"})
    with connect() as conn:
        line = mg.add_overhead(conn, body.get("label"), body.get("monthly_cost"), vendor=body.get("vendor"), kind=body.get("kind", "shared"), note=body.get("note"), actor="web")
    return jsonify({"ok": True, "line": line})


@margins_api.post("/overhead/<int:line_id>")
def overhead_update(line_id):
    body = _body({"monthly_cost", "kind", "note"})
    with connect() as conn:
        line = mg.update_overhead(conn, line_id, monthly_cost=body.get("monthly_cost"), kind=body.get("kind"), note=body.get("note"), actor="web")
    return jsonify({"ok": True, "line": line})


@margins_api.route("/<path:_rest>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def not_found(_rest):
    abort(404)
