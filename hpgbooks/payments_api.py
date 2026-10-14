"""JSON API for minimum payments and due dates. Mounted under /api/payments.

GET  /api/payments?mode=business|personal   the Accounts page table and summary
PATCH /api/payments/<account_id>            edit one account (source 'manual'), or mark it paid

Edits write only account_payment_terms (plus an audit row). CSRF, Origin, and the
JSON body checks in web.py run before any PATCH reaches these handlers.
"""

from __future__ import annotations

from flask import Blueprint, abort, jsonify, request

from hpgbooks import payments as pay
from hpgbooks.access import request_requires_login, session_is_authenticated
from hpgbooks.db import HpgbooksError, connect
from hpgbooks.scope import get_account, parse_mode

payments_api = Blueprint("payments_api", __name__, url_prefix="/api/payments")

ALLOWED = {"min_payment", "due_date", "apr", "autopay", "notes", "paid", "paid_on", "unverified"}


@payments_api.errorhandler(HpgbooksError)
def _hpgbooks_error(err: HpgbooksError):
    message = str(err)
    return jsonify({"ok": False, "error": message}), 404 if message.startswith("no such") else 400


@payments_api.before_request
def _require_signed_in():
    if request_requires_login() and not session_is_authenticated():
        abort(401)


@payments_api.get("")
def listing():
    mode = parse_mode(request.args.get("mode"))
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, **pay.build(conn, mode)})


@payments_api.patch("/<account_id>")
def update(account_id):
    body = request.get_json(silent=True)
    if not request.is_json or not isinstance(body, dict):
        return abort(415)
    unknown = set(body) - ALLOWED - {"csrf_token"}
    if unknown:
        raise HpgbooksError(f"unknown field {sorted(unknown)[0]}")
    mode = parse_mode(request.args.get("mode"))
    if len(account_id) > 80:
        raise HpgbooksError("no such account")
    with connect() as conn:
        acct = get_account(conn, account_id)
        if acct is None or acct["scope"] != mode:
            raise HpgbooksError("no such account")
        if body.get("paid"):
            if pay.get_terms(conn, account_id) is None:
                raise HpgbooksError("enter the due date and minimum first")
            pay.mark_paid(conn, account_id, body.get("paid_on"), actor="web")
        fields: dict = {}
        if "min_payment" in body:
            fields["min_payment_cents"] = pay.cents_arg(body["min_payment"], "minimum payment")
        for key in ("due_date", "apr", "autopay", "notes"):
            if key in body:
                fields[key] = body[key]
        if "unverified" in body:
            fields["unverified"] = bool(body["unverified"])
        if fields:
            pay.set_manual(conn, account_id, fields, actor="web")
        result = pay.build(conn, mode)
    row = next((r for r in result["rows"] if r["id"] == account_id), None)
    return jsonify({"ok": True, "row": row, "summary": result["summary"]})
