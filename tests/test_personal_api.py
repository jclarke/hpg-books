"""Personal JSON API, exports, auth, validation, audit, and the cross-scope leak walk."""

from __future__ import annotations

import json
import re
import shutil

import pytest

from golden_fixture import freeze_today, run_cli, set_env
from hpgbooks.access import clear_passphrase_cache, reset_lockout
from hpgbooks.db import connect
from fake_accounts import BANK
from personal_fake import CARD_A, CHECKING, MORTGAGE
from personal_helpers import build_full_ledger

SECRET = "correct-horse-battery-staple"
HOST = "books.example:8765"
P = "mode=personal"


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    path = tmp_path_factory.mktemp("personal-template")
    with pytest.MonkeyPatch.context() as mp:
        set_env(mp, path)
        freeze_today(mp)
        build_full_ledger(path)
    return path / "books.db"


@pytest.fixture()
def client(template, tmp_path, monkeypatch):
    shutil.copy(template, tmp_path / "books.db")
    set_env(monkeypatch, tmp_path)
    freeze_today(monkeypatch)
    clear_passphrase_cache()
    from hpgbooks.web import create_app

    yield create_app().test_client()
    reset_lockout()
    clear_passphrase_cache()


def _token(client):
    return client.get(f"/api/session?{P}").get_json()["csrf_token"]


def _post(client, url, payload, token=None):
    sep = "&" if "?" in url else "?"
    return client.post(f"{url}{sep}{P}", json=payload, headers={"X-CSRF-Token": token or _token(client)})


def _get(client, url):
    sep = "&" if "?" in url else "?"
    return client.get(f"{url}{sep}{P}")


def _cat(client, name):
    cats = client.get(f"/api/session?{P}").get_json()["personal"]["categories"]
    return next(cat["id"] for cat in cats if cat["name"] == name)


READ_ROUTES = [
    "/api/personal/status",
    "/api/personal/dashboard",
    "/api/personal/net-worth?range=1Y",
    "/api/personal/net-worth?range=3M&interval=week",
    "/api/personal/accounts",
    f"/api/personal/accounts/{CHECKING}/register",
    "/api/personal/transactions",
    "/api/personal/transactions?search=kroger&transfers=hide&sort=amount&dir=asc",
    "/api/personal/transactions?review=1",
    "/api/personal/spending?start=2026-09-01&end=2026-09-30",
    "/api/personal/cash-flow?month=2026-09&months=12",
    "/api/personal/budgets?month=2026-09",
    "/api/personal/budgets/suggestions?month=2026-09",
    "/api/personal/recurring",
    "/api/personal/bills?days=45",
    "/api/personal/debt-payoff",
    "/api/personal/debt-offers",
    "/api/personal/goals",
    "/api/personal/summary?month=2026-09",
    "/api/personal/reconcile?month=2026-09",
    "/api/personal/review",
    "/api/personal/categories",
    "/api/personal/rules",
    "/api/personal/merchants",
    "/api/personal/transfers",
]
EXPORTS = ["transactions", "review", "spending", "merchants", "cash-flow", "budgets", "recurring", "bills", "goals",
           "net-worth", "net-worth-history", "accounts", "summary", "categories", "rules"]


@pytest.mark.parametrize("url", READ_ROUTES)
def test_personal_reads_need_personal_mode(client, url):
    response = _get(client, url)
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    assert response.get_json()["ok"] is True
    assert client.get(url).status_code == 404  # absent mode means business
    sep = "&" if "?" in url else "?"
    assert client.get(f"{url}{sep}mode=business").status_code == 404


@pytest.mark.parametrize("name", EXPORTS)
def test_personal_exports(client, name):
    response = _get(client, f"/export/personal/{name}.csv")
    assert response.status_code == 200 and response.mimetype == "text/csv"
    assert response.headers["Content-Disposition"] == f"attachment; filename=personal-{name}.csv"
    assert client.get(f"/export/personal/{name}.csv").status_code == 404


def test_export_escapes_formulas_and_unknown_export_is_404(client):
    token = _token(client)
    row = _get(client, "/api/personal/transactions?search=kroger").get_json()["rows"][0]
    assert _post(client, f"/api/personal/transactions/{row['id']}/note", {"note": "=HYPERLINK(1)"}, token).status_code == 200
    text = _get(client, "/export/personal/transactions.csv?search=kroger").get_data(as_text=True)
    assert "'=HYPERLINK(1)" in text and "\n=HYPERLINK" not in text
    assert _get(client, "/export/personal/nope.csv").status_code == 404


def test_validation(client):
    assert _get(client, "/api/personal/budgets?month=2026-13").status_code == 400
    assert _get(client, "/api/personal/summary?month=bad").status_code == 400
    assert _get(client, "/api/personal/net-worth?range=10Y").status_code == 400
    assert _get(client, "/api/personal/spending?start=2026-09-01").status_code == 400
    assert _get(client, "/api/personal/transactions?min_amount=abc").status_code == 400
    assert _get(client, "/api/personal/transactions?sort=evil").status_code == 400
    assert _get(client, "/api/personal/transactions?limit=100000").status_code == 400
    assert _get(client, "/api/personal/accounts/fake-nope/register").status_code == 404
    assert _get(client, f"/api/personal/accounts/{BANK}/register").status_code == 404
    token = _token(client)
    assert _post(client, "/api/personal/categorize", {"txn_id": "x y", "category_id": 1}, token).status_code == 400
    assert _post(client, "/api/personal/categorize", {"txn_id": "fake-ptx-00001"}, token).status_code == 400
    assert _post(client, "/api/personal/budgets", {"category_id": _cat(client, "Groceries"), "amount_cents": -5}, token).status_code == 400
    response = client.post(f"/api/personal/categorize?{P}", data="x", headers={"X-CSRF-Token": token})
    assert response.status_code == 415


def test_cross_scope_ids_are_404(client):
    token = _token(client)
    groceries = _cat(client, "Groceries")
    assert _post(client, "/api/personal/categorize", {"txn_id": "g-bank-client-3", "category_id": groceries}, token).status_code == 404
    assert _get(client, "/api/personal/transactions/g-bank-client-3").status_code == 404
    assert _post(client, "/api/personal/transactions/g-bank-client-3/note", {"note": "x"}, token).status_code == 404
    assert _post(client, "/api/personal/categorize", {"txn_id": "biz:g-bank-mortgage-3", "category_id": groceries}, token).status_code == 404
    assert client.get("/api/transactions/fake-ptx-00001").status_code == 404
    detail = _get(client, "/api/personal/transactions/biz:g-bank-mortgage-3").get_json()["transaction"]
    assert detail["from_business"] is True and detail["editable"] is False


def test_writes_are_audited_and_need_csrf(client):
    token = _token(client)
    groceries, dining = _cat(client, "Groceries"), _cat(client, "Dining out")
    row = _get(client, "/api/personal/review").get_json()["rows"][0]
    assert client.post(f"/api/personal/categorize?{P}", json={"txn_id": row["id"], "category_id": dining}).status_code == 403
    body = _post(client, "/api/personal/categorize", {"txn_id": row["id"], "category_id": dining, "note": "dinner"}, token).get_json()
    assert body["ok"] and body["result"]["source"] == "manual"
    assert _post(client, "/api/personal/categorize/similar", {"txn_id": row["id"], "category_id": dining}, token).status_code == 200
    kroger = _get(client, "/api/personal/transactions?search=kroger&limit=2").get_json()["rows"]
    assert _post(client, "/api/personal/categorize/bulk", {"txn_ids": [r["id"] for r in kroger], "category_id": groceries}, token).status_code == 200
    k = kroger[0]
    assert _post(client, f"/api/personal/transactions/{k['id']}/tags", {"tags": ["weekly"]}, token).status_code == 200
    assert _post(client, f"/api/personal/transactions/{k['id']}/note", {"note": "big shop"}, token).status_code == 200
    bad = _post(client, f"/api/personal/transactions/{k['id']}/splits", {"splits": [{"category_id": groceries, "amount_cents": -1}, {"category_id": dining, "amount_cents": -1}]}, token)
    assert bad.status_code == 400 and "add up" in bad.get_json()["error"]
    half = k["amount_cents"] // 2
    good = _post(client, f"/api/personal/transactions/{k['id']}/splits", {"splits": [{"category_id": groceries, "amount_cents": half}, {"category_id": dining, "amount_cents": k["amount_cents"] - half}]}, token)
    assert good.status_code == 200
    renamed = _post(client, f"/api/personal/transactions/{k['id']}/rename", {"display_name": "Kroger Market", "create_rule": True, "category_id": groceries}, token).get_json()
    assert renamed["result"]["matching"] > 10 and renamed["result"]["rule"]["id"]
    preview = _post(client, "/api/personal/rules/preview", {"pattern": "STARBUCKS", "category_id": dining}, token).get_json()
    assert preview["count"] > 10 and preview["would_change"] == preview["count"] - preview["manual"]
    rule = _post(client, "/api/personal/rules", {"pattern": "ZZQ LOCAL", "category_id": _cat(client, "General")}, token).get_json()
    assert _post(client, f"/api/personal/rules/{rule['result']['id']}/active", {"active": False}, token).status_code == 200
    cat = _post(client, "/api/personal/categories", {"name": "Boat", "group_name": "Hobbies"}, token).get_json()["result"]
    assert _post(client, f"/api/personal/categories/{cat['id']}", {"hidden": True}, token).status_code == 200
    system = _cat(client, "Uncategorized")
    assert _post(client, f"/api/personal/categories/{system}", {"name": "x"}, token).status_code == 400
    budget = _post(client, "/api/personal/budgets", {"category_id": groceries, "amount_cents": 60000}, token).get_json()["result"]
    assert _post(client, "/api/personal/budgets/copy", {"month": "2026-10"}, token).status_code == 200
    assert _post(client, "/api/personal/budgets/average", {"month": "2026-10", "category_ids": [groceries]}, token).status_code == 200
    assert _post(client, f"/api/personal/budgets/{budget['id']}/delete", {}, token).status_code == 200
    goal = _post(client, "/api/personal/goals", {"name": "Fund", "target_cents": 100000, "account_id": CHECKING}, token).get_json()["result"]
    assert _post(client, f"/api/personal/goals/{goal['id']}", {"archived": True}, token).status_code == 200
    item = _get(client, "/api/personal/recurring").get_json()["items"][0]
    assert _post(client, "/api/personal/recurring/update", {"series_key": item["series_key"], "status": "ignored"}, token).status_code == 200
    assert _post(client, "/api/personal/recurring/update", {"series_key": "nope|out|0", "status": "ignored"}, token).status_code == 404
    assert _post(client, "/api/personal/reclassify", {}, token).status_code == 200
    assert _post(client, f"/api/personal/transactions/{k['id']}/transfer", {"category": "Internal transfer"}, token).status_code == 200
    audit = client.get(f"/api/audit?{P}").get_json()["rows"]
    actions = {row["action"] for row in audit}
    assert {"personal_classify", "personal_tags", "personal_split", "personal_merchant_rename", "personal_rule_create",
            "personal_rule_disable", "personal_category", "personal_budget", "personal_goal", "personal_recurring",
            "personal_note"} <= actions
    business_audit = {row["action"] for row in client.get("/api/audit").get_json()["rows"]}
    assert not any(action.startswith("personal_") for action in business_audit)


def test_sign_in_required_on_tailnet(client, monkeypatch):
    from hpgbooks.cli import main
    from hpgbooks.web import create_app

    monkeypatch.setenv("HPGBOOKS_NEW_PASSPHRASE", SECRET)
    assert main(["web-passphrase", "set"]) == 0
    clear_passphrase_cache()
    app = create_app()
    app.config["ALLOWED_HOSTS"] = (HOST,)
    remote = app.test_client()
    headers = {"Host": HOST}
    for url in READ_ROUTES[:5] + ["/export/personal/transactions.csv", "/api/accounts/settings"]:
        sep = "&" if "?" in url else "?"
        response = remote.get(f"{url}{sep}{P}", headers=headers)
        assert response.status_code == 401 and b"Kroger" not in response.data
    session = remote.get(f"/api/session?{P}", headers=headers).get_json()
    assert "personal" not in session and "accounts" not in session
    token = session["csrf_token"]
    assert remote.post(f"/api/personal/categorize?{P}", json={"txn_id": "x", "category_id": 1}, headers={**headers, "X-CSRF-Token": token}).status_code == 401
    assert remote.post("/api/login", json={"passphrase": SECRET}, headers={**headers, "X-CSRF-Token": token}).status_code == 200
    assert remote.get(f"/api/personal/dashboard?{P}", headers=headers).status_code == 200


def test_session_and_search_in_personal_mode(client):
    session = client.get(f"/api/session?{P}").get_json()
    ids = {acct["id"] for acct in session["accounts"]}
    assert CHECKING in ids and BANK not in ids
    assert session["personal"]["has_accounts"] and session["review_count"] > 0
    assert session["months"][0] == "2025-08"
    business = client.get("/api/session").get_json()
    assert "personal" not in business and business["months"][0] == "2026-01"
    found = client.get(f"/api/search?q=kroger&{P}").get_json()
    assert found["transactions"] and all(row["id"].startswith(("fake-", "biz:")) for row in found["transactions"])
    assert all(not row["id"].startswith("fake-") for row in client.get("/api/search?q=kroger").get_json()["transactions"])


def test_dashboard_numbers(client):
    data = _get(client, "/api/personal/dashboard").get_json()
    assert data["has_accounts"] and data["month"] == "2026-09"
    totals = data["month_totals"]
    assert totals["owner_draws_cents"] > 0 and totals["income_cents"] == totals["earned_cents"] + totals["owner_draws_cents"]
    assert data["subscription_monthly_cents"] > 0 and data["review_count"] > 0
    assert data["net_worth"]["net_cents"] < 0  # the fake mortgage is larger than the fake assets


def test_scope_change_moves_numbers_between_modes(client):
    token = _token(client)
    before = _get(client, "/api/personal/transactions?account=" + CARD_A).get_json()["total"]
    assert before > 0
    response = client.patch(f"/api/accounts/{CARD_A}/settings?{P}", json={"scope": "excluded"}, headers={"X-CSRF-Token": token})
    assert response.status_code == 200 and response.get_json()["scope_change"]["transactions"] == before
    assert _get(client, "/api/personal/transactions?account=" + CARD_A).get_json()["total"] == 0
    assert client.get(f"/api/transactions?account={CARD_A}").status_code == 400  # not a business account either
    rows = client.get("/api/accounts/settings").get_json()["rows"]
    assert {row["id"]: row["scope"] for row in rows}[CARD_A] == "excluded"


# --- leak walk ----------------------------------------------------------------------


def _ids(conn, scope):
    return {row[0] for row in conn.execute(
        "SELECT t.id FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE a.scope = ?", (scope,)
    )}


def _fill(rule: str, mode: str) -> str | None:
    values = {
        "account_id": CHECKING if mode == "personal" else BANK,
        "txn_id": "fake-ptx-00010" if mode == "personal" else "g-bank-client-3",
        "name": "transactions" if mode == "personal" else "cash-flow",
        "slug": "cash-flow.csv",
        "table": "revenue",
    }
    out = rule
    for key, value in values.items():
        out = re.sub(rf"<(?:[a-z]+:)?{key}>", value, out)
    return None if "<" in out else out


def test_leak_walk_every_get_route(client):
    from hpgbooks.web import create_app

    with connect(readonly=True) as conn:
        personal_ids = _ids(conn, "personal")
        business_ids = _ids(conn, "business")
        allowed_business = {row[0] for row in conn.execute(
            "SELECT txn_id FROM classifications WHERE business_tag IN ('owner_draw', 'transfer')"
        )}
        personal_accounts = {row[0] for row in conn.execute("SELECT id FROM accounts WHERE scope = 'personal'")}
    app = create_app()
    checked = 0
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.rule.startswith(("/app", "/favicon")) or "<path:_rest>" in rule.rule:
            continue
        if rule.rule in ("/", "/<path:route>", "/healthz"):
            continue
        personal_route = rule.rule.startswith(("/api/personal", "/export/personal"))
        shared = rule.rule in ("/api/session", "/api/search", "/api/audit", "/api/review-count", "/api/settings", "/api/accounts/settings")
        modes = ["business", "personal"] if shared else (["personal"] if personal_route else ["business"])
        for mode in modes:
            url = _fill(rule.rule, mode)
            if url is None:
                continue
            query = "q=a" if "search" in url else "start=2026-01-01&end=2026-09-30"
            if "/api/personal/" in url and not url.endswith("/register"):
                query = ""
            response = client.get(f"{url}?mode={mode}&{query}")
            if response.status_code in (401, 503):
                continue
            assert response.status_code in (200, 400, 404), (url, mode, response.status_code)
            text = response.get_data().decode("utf-8", "replace")
            checked += 1
            if url == "/api/accounts/settings":
                continue  # lists every account by design
            if mode == "business":
                leaked = [txn_id for txn_id in personal_ids if f'"{txn_id}"' in text or f",{txn_id}" in text]
                assert not leaked, (url, leaked[:3])
                assert not any(acct in text for acct in personal_accounts), url
                assert "ACME CORP" not in text and "FAKE HOME LOANS" not in text, url
            else:
                for txn_id in re.findall(r'"(?:biz:)?(g-[a-z0-9\-]+)', text):
                    assert txn_id in allowed_business, (url, txn_id)
                assert "EXAMPLE PAYMENTS" not in text and "SAMPLE CLIENT" not in text and "ACME HOSTING" not in text, url
    assert checked > 50


def test_leak_walk_sums(client):
    """Personal totals are personal rows plus synthesized draws; business totals ignore personal rows."""
    with connect(readonly=True) as conn:
        personal_sum = conn.execute(
            "SELECT SUM(t.amount_cents) FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE a.scope = 'personal' AND t.status = 'active'"
        ).fetchone()[0]
        unpaired_draws = conn.execute(
            """
            SELECT COUNT(*) FROM transactions t JOIN classifications c ON c.txn_id = t.id
            WHERE c.business_tag = 'owner_draw' AND t.status = 'active'
            """
        ).fetchone()[0]
    data = _get(client, "/api/personal/transactions?limit=500").get_json()
    own = sum(row["amount_cents"] for row in _all_rows(client) if not row["from_business"])
    assert own == personal_sum
    synthesized = [row for row in _all_rows(client) if row["from_business"]]
    assert 0 < len({row["id"].split(":")[1] for row in synthesized}) <= unpaired_draws
    assert data["total"] > 500
    business = client.get("/api/transactions?start=2025-01-01&end=2026-12-31&limit=500").get_json()
    assert all(not row["id"].startswith("fake-") for row in business["rows"])


def _all_rows(client):
    rows, offset = [], 0
    while True:
        page = _get(client, f"/api/personal/transactions?limit=500&offset={offset}").get_json()
        rows.extend(page["rows"])
        offset += 500
        if offset >= page["total"]:
            return rows


def test_personal_cli_reports_do_not_leak(client):
    for argv in (["personal", "dashboard"], ["personal", "summary", "--month", "2026-09"], ["personal", "transfers"],
                 ["personal", "budgets", "--alerts"], ["personal", "recurring", "detect"], ["personal", "reconcile", "--month", "2026-09"],
                 ["personal", "net-worth"], ["personal", "cash-flow"], ["personal", "spending", "--month", "2026-09"],
                 ["personal", "summary", "--month", "2026-09", "--format", "json"]):
        code, out, err = run_cli(argv)
        assert code == 0, (argv, err)
        assert "EXAMPLE PAYMENTS" not in out and "SAMPLE CLIENT" not in out and "g-bank-client" not in out, argv
