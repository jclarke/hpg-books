"""JSON API for personal mode: /api/personal/* and /export/personal/*.csv.

web.py only lets these routes through with ?mode=personal, behind the same
sign-in, host, Origin, and CSRF checks as the business API. Reads open the
database read-only; every write goes through personal/actions.py, which
writes an audit row. Transaction ids from business accounts are 404 here.
"""

from __future__ import annotations

import re

from flask import Blueprint, Response, jsonify, request

from hpgbooks.db import HpgbooksError, connect
from hpgbooks.personal import actions
from hpgbooks.personal import analytics as pa
from hpgbooks.personal import queries as pq
from hpgbooks.webargs import _int_arg, _optional_date

personal_api = Blueprint("personal_api", __name__)

TXN_RE = re.compile(r"^[A-Za-z0-9:_\-.]{1,200}$")
BULK_MAX = 500
SETUP_STEPS = [
    "bin/hpgbooks accounts discover sync/inbox/YYYY-MM-DD/accounts/finance_list_accounts.json   # registers new accounts as personal",
    "bin/hpgbooks accounts list --scope personal",
    "bin/hpgbooks accounts set-scope <last4> personal   # or business / excluded",
    "bin/hpgbooks import sync/inbox/YYYY-MM-DD/   # reads personal/ too",
    "bin/hpgbooks personal seed-rules && bin/hpgbooks personal reclassify",
]


class PersonalError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@personal_api.errorhandler(PersonalError)
def _personal_error(err: PersonalError):
    return jsonify({"ok": False, "error": err.message}), err.status


@personal_api.errorhandler(HpgbooksError)
def _hpgbooks_error(err: HpgbooksError):
    message = str(err)
    status = 404 if message.startswith("no such") else 400
    return jsonify({"ok": False, "error": message}), status


def _body() -> dict:
    if not request.is_json:
        raise PersonalError("expected a JSON body", 415)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise PersonalError("expected a JSON object")
    body.pop("csrf_token", None)
    return body


def _txn_id(value) -> str:
    if not isinstance(value, str) or not TXN_RE.fullmatch(value.strip()):
        raise PersonalError("invalid transaction id")
    return value.strip()


def _txn_ids(body: dict) -> list[str]:
    ids = body.get("txn_ids")
    if not isinstance(ids, list) or not ids or len(ids) > BULK_MAX:
        raise PersonalError(f"txn_ids must list 1 to {BULK_MAX} transactions")
    ids = [_txn_id(item) for item in ids]
    if len(set(ids)) != len(ids):
        raise PersonalError("txn_ids has duplicates")
    return ids


def _int_field(body: dict, name: str) -> int:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PersonalError(f"{name} is required")
    return value


def _range_args(default_days: int | None = None) -> tuple[str | None, str | None]:
    start, end = _optional_date("start"), _optional_date("end")
    if (start is None) != (end is None):
        raise PersonalError("start and end go together")
    if start and end and start > end:
        raise PersonalError("start must be on or before end")
    if start is None and default_days is not None:
        today = pa.today()
        end = today.isoformat()
        start = today.replace(day=1).isoformat()
    return start, end


def _month_arg(name: str = "month") -> str | None:
    value = (request.args.get(name) or "").strip()
    if not value:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise PersonalError("month must be YYYY-MM")
    return value


def _amount_arg(name: str) -> int | None:
    from hpgbooks.db import to_cents

    raw = (request.args.get(name) or "").strip()
    if not raw:
        return None
    if not re.fullmatch(r"\d{1,9}(\.\d{1,2})?", raw):
        raise PersonalError(f"{name} must be a dollar amount")
    return to_cents(raw)


def _txn_filters() -> dict:
    start, end = _range_args()
    search = request.args.get("search") or ""
    if len(search) > 200:
        raise PersonalError("search is too long")
    category = (request.args.get("category_id") or "").strip()
    if category and not category.isdigit():
        raise PersonalError("category_id must be a number")
    sort = request.args.get("sort") or "date"
    direction = request.args.get("dir") or "desc"
    transfers = request.args.get("transfers") or "show"
    pending = request.args.get("pending") or ""
    if pending not in ("", "0", "1"):
        raise PersonalError("pending must be 0 or 1")
    account = (request.args.get("account") or "").strip()
    if len(account) > 80:
        raise PersonalError("no such account", 404)
    for name in ("group", "tag", "merchant", "kind"):
        if len(request.args.get(name) or "") > 120:
            raise PersonalError(f"{name} is too long")
    return {
        "start": start,
        "end": end,
        "search": search,
        "account": account,
        "category_id": int(category) if category else None,
        "group": request.args.get("group") or "",
        "tag": request.args.get("tag") or "",
        "merchant_key": request.args.get("merchant") or "",
        "kind": request.args.get("kind") or "",
        "min_cents": _amount_arg("min_amount"),
        "max_cents": _amount_arg("max_amount"),
        "uncategorized": request.args.get("uncategorized") == "1",
        "pending": pending,
        "transfers": transfers,
        "review": request.args.get("review") == "1",
        "sort": sort,
        "direction": direction,
    }


def personal_session_extra(conn) -> dict:
    """Reference data added to /api/session in personal mode."""
    from hpgbooks.personal.classify import categories, personal_accounts, personal_ready

    if not personal_ready(conn):
        return {"personal": {"has_accounts": False, "categories": [], "setup_steps": SETUP_STEPS}}
    accounts = personal_accounts(conn)
    return {
        "personal": {
            "has_accounts": bool(accounts),
            "categories": [
                {"id": cat["id"], "name": cat["name"], "group": cat["group_name"], "kind": cat["kind"],
                 "color": cat["color"], "hidden": bool(cat["hidden"]), "is_system": bool(cat["is_system"])}
                for cat in categories(conn).values()
            ],
            "setup_steps": SETUP_STEPS,
        }
    }


# --- reads --------------------------------------------------------------------


@personal_api.get("/api/personal/status")
def status():
    from hpgbooks.personal.classify import personal_accounts, personal_ready

    with connect(readonly=True) as conn:
        ready = personal_ready(conn)
        accounts = personal_accounts(conn) if ready else {}
        count = 0
        if accounts:
            marks = ", ".join("?" for _ in accounts)
            count = int(conn.execute(
                f"SELECT COUNT(*) FROM transactions WHERE status = 'active' AND account_id IN ({marks})", list(accounts)
            ).fetchone()[0])
    return jsonify({"ok": True, "ready": ready, "account_count": len(accounts), "transaction_count": count, "setup_steps": SETUP_STEPS})


@personal_api.get("/api/personal/dashboard")
def dashboard():
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, **pa.dashboard(conn)})


@personal_api.get("/api/personal/net-worth")
def net_worth():
    span = (request.args.get("range") or "1Y").upper()
    interval = request.args.get("interval") or None
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, **pa.net_worth(conn, span, interval)})


@personal_api.get("/api/personal/accounts")
def accounts():
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, **pa.accounts_overview(conn)})


@personal_api.get("/api/personal/accounts/<account_id>/register")
def register(account_id):
    filters = _txn_filters()
    filters.pop("account")
    limit = _int_arg("limit", 100, 1, 500)
    offset = _int_arg("offset", 0, 0, 1_000_000)
    with connect(readonly=True) as conn:
        data = pq.register(conn, account_id, **filters)
    rows = data["rows"]
    return jsonify({"ok": True, **data, "rows": rows[offset: offset + limit], "offset": offset, "limit": limit})


@personal_api.get("/api/personal/transactions")
def transactions():
    filters = _txn_filters()
    limit = _int_arg("limit", 100, 1, 500)
    offset = _int_arg("offset", 0, 0, 1_000_000)
    with connect(readonly=True) as conn:
        data = pq.query(conn, limit=limit, offset=offset, **filters)
    return jsonify({"ok": True, **data, "offset": offset, "limit": limit})


@personal_api.get("/api/personal/transactions/<path:txn_id>")
def transaction_detail(txn_id):
    txn_id = _txn_id(txn_id)
    with connect(readonly=True) as conn:
        if txn_id.startswith("biz:"):
            rows = [row for row in pq.query(conn, limit=100_000)["rows"] if row["id"] == txn_id]
            if not rows:
                raise PersonalError("no such transaction", 404)
            return jsonify({"ok": True, "transaction": {**rows[0], "history": [], "description": ""}})
        txn = actions.personal_txn(conn, txn_id)
        rows = [row for row in pq.query(conn, include_business=False, limit=100_000)["rows"] if row["id"] == txn_id]
        if not rows:
            rows = [row for row in pa.load_personal(conn, status="all", include_synthesized=False) if row["id"] == txn_id]
        history = conn.execute(
            "SELECT id, ts, action, field, old_value, new_value, actor, note FROM audit_log WHERE txn_id = ? ORDER BY id DESC LIMIT 20",
            (txn_id,),
        ).fetchall()
    out = dict(rows[0]) if rows else {}
    if isinstance(out.get("splits"), list):
        out["splits"] = list(out["splits"])
    out["description"] = txn.get("description") or ""
    out["raw_name"] = txn.get("name") or ""
    out["history"] = [{key: item[key] for key in item.keys()} for item in history]
    return jsonify({"ok": True, "transaction": out})


@personal_api.get("/api/personal/spending")
def spending():
    start, end = _range_args(default_days=30)
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, **pa.spending(conn, start, end)})


@personal_api.get("/api/personal/cash-flow")
def cash_flow():
    months = _int_arg("months", 12, 1, 60)
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, **pa.cash_flow(conn, _month_arg(), months)})


@personal_api.get("/api/personal/budgets")
def budgets():
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, **pa.budgets(conn, _month_arg())})


@personal_api.get("/api/personal/budgets/suggestions")
def budget_suggestions():
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, "rows": pa.average_suggestions(conn, _month_arg())})


@personal_api.get("/api/personal/recurring")
def recurring():
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, **pa.recurring(conn)})


@personal_api.get("/api/personal/recurring/review")
def recurring_review():
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, **pa.recurring_review(conn)})


@personal_api.get("/api/personal/bills")
def bills():
    days = _int_arg("days", 45, 1, 120)
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, **pa.bills(conn, days)})


@personal_api.get("/api/personal/debt-payoff")
def debt_payoff():
    from hpgbooks.personal import debt

    with connect(readonly=True) as conn:
        return jsonify({"ok": True, **debt.build(conn)})


@personal_api.get("/api/personal/debt-offers")
def debt_offers():
    from hpgbooks.personal import offers

    with connect(readonly=True) as conn:
        return jsonify({"ok": True, "rows": offers.list_offers(conn)})


@personal_api.get("/api/personal/goals")
def goals():
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, "rows": pa.goals(conn, request.args.get("archived") == "1")})


@personal_api.get("/api/personal/summary")
def summary():
    month = _month_arg() or pa.month_of(pa.today())
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, **pa.monthly_summary(conn, month)})


@personal_api.get("/api/personal/reconcile")
def reconcile():
    month = _month_arg() or pa.month_of(pa.today())
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, **pa.reconcile(conn, month)})


@personal_api.get("/api/personal/review")
def review():
    with connect(readonly=True) as conn:
        rows = pq.review_queue(conn)
    return jsonify({"ok": True, "rows": rows, "count": len(rows)})


@personal_api.get("/api/personal/categories")
def categories_list():
    from hpgbooks.personal.classify import categories

    with connect(readonly=True) as conn:
        cats = categories(conn)
        counts = {
            row["category_id"]: int(row["n"])
            for row in conn.execute("SELECT category_id, COUNT(*) AS n FROM p_classifications GROUP BY category_id")
        }
    rows = [
        {"id": cat["id"], "name": cat["name"], "group": cat["group_name"], "kind": cat["kind"], "color": cat["color"],
         "hidden": bool(cat["hidden"]), "is_system": bool(cat["is_system"]), "transaction_count": counts.get(cat["id"], 0)}
        for cat in cats.values()
    ]
    return jsonify({"ok": True, "rows": rows})


@personal_api.get("/api/personal/rules")
def rules_list():
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, "rows": actions.list_rules(conn)})


@personal_api.get("/api/personal/merchants")
def merchants():
    from hpgbooks.personal.merchants import load_aliases

    with connect(readonly=True) as conn:
        aliases = load_aliases(conn)
        rows = pa.load_personal(conn, include_synthesized=False)
    found: dict[str, dict] = {}
    for row in rows:
        item = found.setdefault(row["merchant_key"], {"key": row["merchant_key"], "display_name": row["merchant"], "renamed": row["merchant_key"] in aliases, "count": 0, "spellings": set()})
        item["count"] += 1
        if len(item["spellings"]) < 6:
            item["spellings"].add(row["name"])
    out = sorted(found.values(), key=lambda item: (-item["count"], item["display_name"]))
    for item in out:
        item["spellings"] = sorted(item["spellings"])
    return jsonify({"ok": True, "rows": out})


@personal_api.get("/api/personal/transfers")
def transfers():
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, **pq.transfers_view(conn)})


# --- writes -------------------------------------------------------------------


def _write(fn):
    with connect() as conn:
        result = fn(conn)
        from hpgbooks.analytics import review_count

        count = review_count(conn, "personal")
    return jsonify({"ok": True, "result": result, "review_count": count})


@personal_api.post("/api/personal/categorize")
def categorize():
    body = _body()
    txn_id = _txn_id(body.get("txn_id"))
    category_id = _int_field(body, "category_id")
    note = body.get("note")
    return _write(lambda conn: actions.categorize(conn, txn_id, category_id, note, actor="web"))


@personal_api.post("/api/personal/categorize/bulk")
def categorize_bulk():
    body = _body()
    ids = _txn_ids(body)
    category_id = _int_field(body, "category_id")
    return _write(lambda conn: actions.bulk_categorize(conn, ids, category_id, actor="web"))


@personal_api.post("/api/personal/categorize/similar")
def categorize_similar():
    body = _body()
    txn_id = _txn_id(body.get("txn_id"))
    category_id = _int_field(body, "category_id")
    return _write(lambda conn: actions.apply_to_similar(conn, txn_id, category_id, actor="web"))


@personal_api.post("/api/personal/transactions/<path:txn_id>/note")
def note(txn_id):
    txn_id = _txn_id(txn_id)
    body = _body()
    return _write(lambda conn: actions.set_note(conn, txn_id, body.get("note") or "", actor="web"))


@personal_api.post("/api/personal/transactions/<path:txn_id>/tags")
def tags(txn_id):
    txn_id = _txn_id(txn_id)
    body = _body()
    return _write(lambda conn: actions.set_tags(conn, txn_id, body.get("tags"), actor="web"))


@personal_api.post("/api/personal/transactions/<path:txn_id>/splits")
def splits(txn_id):
    txn_id = _txn_id(txn_id)
    body = _body()
    return _write(lambda conn: actions.set_splits(conn, txn_id, body.get("splits"), actor="web"))


@personal_api.post("/api/personal/transactions/<path:txn_id>/transfer")
def mark_transfer(txn_id):
    txn_id = _txn_id(txn_id)
    body = _body()
    name = body.get("category") or "Internal transfer"
    if name not in ("Credit card payment", "Savings transfer", "Loan payment", "Internal transfer"):
        raise PersonalError("unknown transfer category")
    return _write(lambda conn: actions.mark_transfer(conn, txn_id, actor="web", name=name))


@personal_api.post("/api/personal/transactions/<path:txn_id>/rename")
def rename(txn_id):
    txn_id = _txn_id(txn_id)
    body = _body()
    display = body.get("display_name")
    if not isinstance(display, str):
        raise PersonalError("display_name is required")

    def run(conn):
        result = actions.rename(conn, txn_id, display, actor="web")
        if body.get("create_rule"):
            category_id = _int_field(body, "category_id")
            pattern = re.escape(result["key"])
            result["rule"] = actions.add_rule(
                conn, {"pattern": pattern, "category_id": category_id, "merchant_rename": display, "field": "any"}, actor="web"
            )
        return result

    return _write(run)


@personal_api.post("/api/personal/merchants/rename")
def merchants_rename():
    from hpgbooks.personal.merchants import rename_merchant

    body = _body()
    key, display = body.get("key"), body.get("display_name")
    if not isinstance(key, str) or not isinstance(display, str):
        raise PersonalError("key and display_name are required")
    return _write(lambda conn: rename_merchant(conn, key, display, actor="web"))


@personal_api.post("/api/personal/rules")
def rules_create():
    body = _body()
    return _write(lambda conn: actions.add_rule(conn, body, actor="web"))


@personal_api.post("/api/personal/rules/preview")
def rules_preview():
    body = _body()
    with connect(readonly=True) as conn:
        return jsonify({"ok": True, **actions.preview_rule(conn, body)})


@personal_api.post("/api/personal/rules/<int:rule_id>/active")
def rules_active(rule_id):
    body = _body()
    active = body.get("active")
    if not isinstance(active, bool):
        raise PersonalError("active must be true or false")
    return _write(lambda conn: actions.set_rule_active(conn, rule_id, active, actor="web"))


@personal_api.post("/api/personal/categories")
def categories_create():
    body = _body()
    return _write(lambda conn: actions.add_category(conn, body, actor="web"))


@personal_api.post("/api/personal/categories/<int:category_id>")
def categories_update(category_id):
    body = _body()
    return _write(lambda conn: actions.update_category(conn, category_id, body, actor="web"))


@personal_api.post("/api/personal/budgets")
def budgets_set():
    body = _body()
    return _write(lambda conn: actions.set_budget(conn, body, actor="web"))


@personal_api.post("/api/personal/budgets/<int:budget_id>/delete")
def budgets_delete(budget_id):
    _body()
    return _write(lambda conn: actions.delete_budget(conn, budget_id, actor="web"))


@personal_api.post("/api/personal/budgets/copy")
def budgets_copy():
    body = _body()
    month = body.get("month")
    if not isinstance(month, str):
        raise PersonalError("month is required")
    return _write(lambda conn: actions.copy_budgets(conn, month, actor="web"))


@personal_api.post("/api/personal/budgets/average")
def budgets_average():
    body = _body()
    month = body.get("month")
    ids = body.get("category_ids")
    if not isinstance(month, str):
        raise PersonalError("month is required")
    if ids is not None and (not isinstance(ids, list) or any(isinstance(item, bool) or not isinstance(item, int) for item in ids)):
        raise PersonalError("category_ids must be a list of ids")
    return _write(lambda conn: actions.apply_average(conn, month, ids, actor="web"))


def _offer_write(fn, status: int = 200):
    from hpgbooks.personal import offers

    with connect() as conn:
        result = fn(conn, offers)
        rows = offers.list_offers(conn)
    return jsonify({"ok": True, "offer": result, "rows": rows}), status


@personal_api.post("/api/personal/debt-offers")
def debt_offers_create():
    body = _body()
    return _offer_write(lambda conn, offers: offers.create_offer(conn, body, actor="web"), 201)


@personal_api.route("/api/personal/debt-offers/<int:offer_id>", methods=["PUT", "PATCH"])
def debt_offers_update(offer_id):
    body = _body()
    return _offer_write(lambda conn, offers: offers.update_offer(conn, offer_id, body, actor="web"))


@personal_api.delete("/api/personal/debt-offers/<int:offer_id>")
def debt_offers_delete(offer_id):
    return _offer_write(lambda conn, offers: offers.delete_offer(conn, offer_id, actor="web"))


@personal_api.post("/api/personal/goals")
def goals_create():
    body = _body()
    return _write(lambda conn: actions.save_goal(conn, body, actor="web"))


@personal_api.post("/api/personal/goals/<int:goal_id>")
def goals_update(goal_id):
    body = _body()
    return _write(lambda conn: actions.save_goal(conn, body, goal_id, actor="web"))


@personal_api.post("/api/personal/recurring/update")
def recurring_update():
    body = _body()
    key = body.pop("series_key", None)
    if not isinstance(key, str):
        raise PersonalError("series_key is required")
    return _write(lambda conn: actions.update_recurring(conn, key, body, actor="web"))


@personal_api.post("/api/personal/recurring/<any(confirm, ignore):verb>")
def recurring_decide(verb):
    body = _body()
    status = "confirmed" if verb == "confirm" else "ignored"
    return _write(lambda conn: actions.set_recurring_decision(conn, body.get("merchant_key"), status, actor="web"))


@personal_api.post("/api/personal/recurring/undo")
def recurring_undo():
    body = _body()
    return _write(lambda conn: actions.clear_recurring_decision(conn, body.get("merchant_key"), actor="web"))


@personal_api.post("/api/personal/recurring/refresh")
def recurring_refresh():
    _body()
    return _write(lambda conn: pa.refresh_recurring(conn))


@personal_api.post("/api/personal/transfers/review")
def transfers_review():
    from hpgbooks.personal.classify import set_transfer_decision

    body = _body()
    key, decision = body.get("pair_key"), body.get("decision")
    if not isinstance(key, str) or not isinstance(decision, str):
        raise PersonalError("pair_key and decision are required")
    return _write(lambda conn: set_transfer_decision(conn, key, decision, actor="web"))


@personal_api.post("/api/personal/reclassify")
def reclassify():
    from hpgbooks.personal.classify import reclassify_personal

    _body()
    return _write(lambda conn: reclassify_personal(conn))


# --- exports (built on request, never stored) --------------------------------------------

TXN_COLUMNS = [
    ("date", "Date"), ("account_label", "Account"), ("merchant", "Merchant"), ("name", "Description"),
    ("amount_cents", "Amount"), ("group", "Group"), ("category", "Category"), ("tags", "Tags"), ("note", "Note"),
    ("pending", "Pending"), ("from_business", "Paid from business"), ("id", "Id"),
]


def _export_rows(name: str, conn) -> tuple[list[tuple[str, str]], list[dict]]:
    if name == "transactions":
        filters = _txn_filters()
        return TXN_COLUMNS, pq.query(conn, limit=20000, **filters)["rows"]
    if name == "review":
        return TXN_COLUMNS, pq.review_queue(conn)
    if name == "spending":
        start, end = _range_args(default_days=30)
        data = pa.spending(conn, start, end)
        return [("group", "Group"), ("category", "Category"), ("cents_cents", "Spent"), ("prior_cents", "Prior period"),
                ("last_year_cents", "Same period last year"), ("count", "Count")], [
            {**item, "cents_cents": item["cents"]} for item in data["categories"]
        ]
    if name == "merchants":
        start, end = _range_args(default_days=30)
        data = pa.spending(conn, start, end)
        return [("merchant", "Merchant"), ("cents_cents", "Spent"), ("count", "Count"), ("average_cents", "Average"), ("last_date", "Last")], [
            {**item, "cents_cents": item["cents"]} for item in data["merchants"]
        ]
    if name == "cash-flow":
        data = pa.cash_flow(conn, _month_arg(), _int_arg("months", 12, 1, 60))
        rows = [
            {**item, "paycheck_cents": item["sources"]["Paycheck"], "interest_cents": item["sources"]["Interest & dividends"],
             "other_cents": item["sources"]["Other"]}
            for item in data["months"]
        ]
        return [("month", "Month"), ("paycheck_cents", "Paycheck"), ("interest_cents", "Interest & dividends"),
                ("owner_draws_cents", "Owner draws"), ("other_cents", "Other income"), ("income_cents", "Income"),
                ("spending_cents", "Spending"), ("net_cents", "Net"), ("savings_rate", "Savings rate %"),
                ("savings_rate_without_draws", "Savings rate without draws %")], rows
    if name == "budgets":
        data = pa.budgets(conn, _month_arg())
        return [("group", "Group"), ("name", "Budget"), ("budget_cents", "Budget"), ("carry_cents", "Rollover"),
                ("available_cents", "Available"), ("spent_cents", "Spent"), ("remaining_cents", "Remaining"),
                ("pct_used", "% used"), ("projected_cents", "Projected"), ("status", "Status")], data["rows"]
    if name == "recurring":
        data = pa.recurring(conn)
        return [("merchant", "Merchant"), ("kind", "Kind"), ("cadence", "Cadence"), ("typical_cents", "Typical"),
                ("last_cents", "Last"), ("last_date", "Last date"), ("next_expected", "Next expected"),
                ("monthly_cents", "Monthly"), ("annual_cents", "Annual"), ("status", "Status")], data["items"]
    if name == "bills":
        data = pa.bills(conn, _int_arg("days", 45, 1, 120))
        return [("date", "Date"), ("merchant", "Merchant"), ("kind", "Kind"), ("amount_cents", "Amount"), ("status", "Status"),
                ("account_label", "Account")], data["events"]
    if name == "goals":
        return [("name", "Goal"), ("target_cents", "Target"), ("current_cents", "Current"), ("progress_pct", "% done"),
                ("target_date", "Target date"), ("required_monthly_cents", "Needed per month"), ("status", "Status")], pa.goals(conn)
    if name == "net-worth":
        data = pa.net_worth(conn, (request.args.get("range") or "1Y").upper())
        return [("label", "Account"), ("class", "Class"), ("balance_cents", "Balance"), ("contribution_cents", "Net worth effect"),
                ("last_updated", "Last updated"), ("stale", "Stale"), ("included", "Included")], data["accounts"]
    if name == "net-worth-history":
        data = pa.net_worth(conn, (request.args.get("range") or "1Y").upper())
        return [("date", "Date"), ("assets_cents", "Assets"), ("liabilities_cents", "Liabilities"), ("net_cents", "Net worth")], data["points"]
    if name == "accounts":
        data = pa.accounts_overview(conn)
        rows = [{**acct, "group": group["label"]} for group in data["groups"] for acct in group["accounts"]]
        return [("group", "Group"), ("label", "Account"), ("last4", "Last 4"), ("balance_cents", "Balance"),
                ("last_updated", "Last updated"), ("stale", "Stale")], rows
    if name == "summary":
        data = pa.monthly_summary(conn, _month_arg() or pa.month_of(pa.today()))
        return [("line", "Summary")], [{"line": line} for line in data["summary"]]
    if name == "categories":
        from hpgbooks.personal.classify import categories

        return [("group_name", "Group"), ("name", "Category"), ("kind", "Kind"), ("hidden", "Hidden")], list(categories(conn).values())
    if name == "rules":
        return [("priority", "Priority"), ("pattern", "Pattern"), ("group", "Group"), ("category", "Category"),
                ("amount_sign", "Sign"), ("active", "Active"), ("hits", "Hits")], actions.list_rules(conn)
    raise PersonalError("no such export", 404)


@personal_api.get("/export/personal/<name>.csv")
def export_csv(name):
    if not re.fullmatch(r"[a-z\-]{1,40}", name):
        raise PersonalError("no such export", 404)
    with connect(readonly=True) as conn:
        columns, rows = _export_rows(name, conn)
    return Response(
        pq.rows_csv(columns, rows),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=personal-{name}.csv"},
    )
