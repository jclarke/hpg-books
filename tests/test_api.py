"""JSON API used by the React app. Temporary database and key only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpgbooks.cli import main
from hpgbooks.db import connect, init_db
from fake_accounts import BANK, REWARDS
from hpgbooks.reports import build_pnl

KEY = "cd" * 32


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HPBOOKS_KEY", KEY)
    monkeypatch.setenv("HPBOOKS_DB", str(tmp_path / "books.db"))
    monkeypatch.delenv("HPBOOKS_KEY_FILE", raising=False)
    init_db()
    return tmp_path


def _txn(txn_id, account, day, amount, name):
    return {
        "id": txn_id,
        "account_id": account,
        "date": day,
        "amount": amount,
        "direction": "out" if str(amount).startswith("-") else "in",
        "currency": "USD",
        "name": name,
        "merchant_name": name,
        "description": "",
        "pending": "false",
        "category": "",
    }


def _feed(path: Path, account: str, txns: list[dict]) -> Path:
    path.write_text(
        json.dumps(
            {
                "source": "finance-mcp",
                "account_id": account,
                "date_from": "2026-01-01",
                "date_to": "2026-12-31",
                "fetched_at": "2026-09-30T00:00:00Z",
                "transactions": txns,
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def books(env):
    feed = _feed(
        env / "bank.json",
        BANK,
        [
            _txn("rev-aug", BANK, "2026-08-04", "50.00", "ZZZ HOSTING"),
            _txn("rev-sep", BANK, "2026-09-04", "100.00", "ZZZ HOSTING"),
            _txn("soft-aug", BANK, "2026-08-05", "-30.00", "ZZZ SOFTWARE"),
            _txn("soft-sep", BANK, "2026-09-05", "-40.00", "ZZZ SOFTWARE"),
            _txn("draw-sep", BANK, "2026-09-06", "-10.00", "ZZZ DRAW"),
            _txn("q1", BANK, "2026-09-11", "-4.00", "ZZZ QUEUE ONE"),
            _txn("q2", BANK, "2026-09-12", "-6.00", "ZZZ QUEUE TWO"),
            _txn("rent-1", BANK, "2026-07-01", "-100.00", "ZZZ RENT"),
            _txn("rent-2", BANK, "2026-08-01", "-100.00", "ZZZ RENT"),
            _txn("rent-3", BANK, "2026-09-01", "-100.00", "ZZZ RENT"),
        ],
    )
    rewards = _feed(env / "rewards.json", REWARDS, [_txn("evil", REWARDS, "2026-09-07", "-3.00", "=1+1<script>")])
    assert main(["import", str(feed)]) == 0
    assert main(["import", str(rewards)]) == 0
    for txn_id, tag, category in (
        ("rev-aug", "branda", "Revenue - Hosting"),
        ("rev-sep", "branda", "Revenue - Hosting"),
        ("soft-aug", "general", "Software & Licenses"),
        ("soft-sep", "general", "Software & Licenses"),
        ("draw-sep", "owner_draw", "Owner Draw"),
        ("rent-1", "general", "Office/Other"),
        ("rent-2", "general", "Office/Other"),
        ("rent-3", "general", "Office/Other"),
        ("evil", "general", "Office/Other"),
    ):
        assert main(["classify", txn_id, "--tag", tag, "--category", category]) == 0
    return env


@pytest.fixture()
def client(books):
    from hpgbooks.web import create_app

    return create_app().test_client()


def _token(client) -> str:
    body = client.get("/api/session").get_json()
    return body["csrf_token"]


def _post(client, url, payload, token=None):
    headers = {"X-CSRF-Token": token if token is not None else _token(client)}
    return client.post(url, json=payload, headers=headers)


def test_session_reference_data(client):
    body = client.get("/api/session").get_json()
    assert body["ok"] is True
    assert body["product"] == "Example Books"
    assert body["requires_login"] is False
    assert body["authenticated"] is False
    assert len(body["csrf_token"]) >= 16
    assert body["latest_month"] == "2026-09"
    assert body["review_count"] == 2
    assert "branda" in body["businesses"]
    assert "Revenue - Hosting" in body["categories"]
    assert len(body["accounts"]) == 5
    assert all("mortgage" not in json.dumps(acct).lower() for acct in body["accounts"])


def test_dashboard_range_kpis_and_widgets(client):
    body = client.get("/api/dashboard?start=2026-09-01&end=2026-09-30&business=all").get_json()
    assert body["prior_start"] == "2026-08-01"
    assert body["prior_end"] == "2026-08-31"
    kpis = {item["key"]: item for item in body["kpis"]}
    assert set(kpis) == {"net_revenue", "total_expenses", "net_income"}
    assert kpis["net_revenue"]["cents"] == 10000
    assert kpis["net_revenue"]["prior_cents"] == 5000
    assert kpis["net_revenue"]["pct"] == 100.0
    assert kpis["total_expenses"]["cents"] == 4000 + 10000 + 300
    assert len(kpis["net_income"]["spark"]) == len(body["points"])
    assert body["points"][-1]["month"] == "2026-09"
    assert body["review_count"] == 2
    assert body["plug"] == -1000
    assert {row["category"] for row in body["expenses"]} == {"Software & Licenses", "Office/Other"}
    assert any(row["merchant"] == "ZZZ RENT" for row in body["top_vendors"])
    assert len(body["cash"]) == 5
    # Owner draws are not a dashboard KPI.
    assert "owner" not in json.dumps(body["kpis"]).lower()
    default = client.get("/api/dashboard").get_json()
    assert default["start"] == "2026-09-01" and default["end"] == "2026-09-30"
    assert client.get("/api/dashboard?business=nope").status_code == 400
    assert client.get("/api/dashboard?start=2026-09-30&end=2026-09-01").status_code == 400
    assert client.get("/api/dashboard?start=2026-09-01").status_code == 400


def test_transactions_filters_and_detail(client):
    body = client.get("/api/transactions?start=2026-09-01&end=2026-09-30&sort=amount&dir=asc&limit=3").get_json()
    assert body["total"] == 7
    assert len(body["rows"]) == 3
    assert body["rows"][0]["amount_cents"] == -10000
    review = client.get("/api/transactions?needs_review=1").get_json()
    assert {row["id"] for row in review["rows"]} == {"q1", "q2"}
    assert client.get("/api/transactions?sort=nope").status_code == 400
    assert client.get("/api/transactions?limit=100000").status_code == 400
    detail = client.get("/api/transactions/rent-1").get_json()["transaction"]
    assert detail["category"] == "Office/Other"
    assert detail["suggested_pattern"]
    assert detail["history"]
    assert client.get("/api/transactions/does-not-exist").status_code == 404


def test_classify_undo_and_audit(client):
    token = _token(client)
    saved = _post(client, "/api/classify", {"txn_id": "q1", "tag": "general", "category": "Office/Other", "note": "x"}, token)
    body = saved.get_json()
    assert saved.status_code == 200, body
    assert body["previous"] is None or body["previous"]["business_tag"] == "needs_review"
    assert body["row"]["source"] == "manual"
    assert body["review_count"] == 1
    undone = _post(client, "/api/classify/undo", {"items": [{"txn_id": "q1", "previous": body["previous"]}]}, token)
    assert undone.status_code == 200, undone.get_json()
    assert undone.get_json()["review_count"] == 2
    audit = client.get("/api/audit").get_json()["rows"]
    actions = [row["action"] for row in audit]
    assert "classify_undo" in actions
    assert "classify" in actions
    bad = _post(client, "/api/classify/undo", {"items": [{"txn_id": "q1", "previous": {"business_tag": "general", "category": "Office/Other", "source": "hacker", "confidence": 1}}]}, token)
    assert bad.status_code == 400


def test_bulk_classify(client):
    token = _token(client)
    res = _post(client, "/api/classify/bulk", {"txn_ids": ["q1", "q2"], "tag": "general", "category": "Bank & Card Fees", "note": "bulk"}, token)
    body = res.get_json()
    assert res.status_code == 200, body
    assert body["count"] == 2
    assert body["review_count"] == 0
    assert len(body["previous"]) == 2
    undo = _post(client, "/api/classify/undo", {"items": [{"txn_id": p["txn_id"], "previous": p["previous"]} for p in body["previous"]]}, token)
    assert undo.get_json()["review_count"] == 2
    assert _post(client, "/api/classify/bulk", {"txn_ids": [], "tag": "general", "category": "Office/Other"}, token).status_code == 400
    assert _post(client, "/api/classify/bulk", {"txn_ids": ["q1", "q1"], "tag": "general", "category": "Office/Other"}, token).status_code == 400
    assert _post(client, "/api/classify/bulk", {"txn_ids": ["q1"], "tag": "bogus", "category": "Office/Other"}, token).status_code == 400


def test_mutations_need_csrf_origin_and_json(client):
    token = _token(client)
    payload = {"txn_id": "q1", "tag": "general", "category": "Office/Other"}
    assert client.post("/api/classify", json=payload).status_code == 403
    assert _post(client, "/api/classify", payload, "wrong-token-wrong-token").status_code == 403
    evil = client.post("/api/classify", json=payload, headers={"X-CSRF-Token": token, "Origin": "https://evil.example"})
    assert evil.status_code == 403
    rebind = client.post("/api/classify", json=payload, headers={"X-CSRF-Token": token, "Host": "evil.example"})
    assert rebind.status_code == 403
    assert client.get("/api/session", headers={"Host": "evil.example:8765"}).status_code == 403
    form = client.post("/api/settings", data={"reserve_cents": "5"}, headers={"X-CSRF-Token": token})
    assert form.status_code == 415
    with connect() as conn:
        tag = conn.execute("SELECT ifnull(c.business_tag,'needs_review') FROM transactions t LEFT JOIN classifications c ON c.txn_id=t.id WHERE t.id='q1'").fetchone()[0]
    assert tag == "needs_review"


def test_accounts_and_register(client):
    body = client.get("/api/accounts?start=2026-09-01&end=2026-09-30").get_json()
    bank = next(row for row in body["rows"] if row["id"] == BANK)
    assert bank["in_cents"] == 10000
    assert bank["out_cents"] == 4000 + 1000 + 400 + 600 + 10000
    reg = client.get(f"/api/accounts/{BANK}/register?limit=2").get_json()
    assert reg["total"] == 10
    assert reg["rows"][0]["running_cents"] == reg["balance_cents"]
    assert client.get("/api/accounts/nope/register").status_code == 404


def test_pnl_keeps_empty_months_and_matches_transfer_memo_prior(client, env, tmp_path):
    """A requested month with no activity still gets a column, and the transfer
    memo's prior figure follows the line even when the row count in the label changes.
    """
    body = client.get("/api/pnl?start=2026-09-01&end=2026-11-30&by=month&compare=0").get_json()
    assert body["column_keys"] == ["2026-09", "2026-10", "2026-11", "Total"]
    net = next(row for row in body["rows"] if row["label"] == "Net Income")
    assert net["values"][1] == 0
    assert net["values"][2] == 0
    assert net["values"][-1] == net["values"][0]
    assert net["values"][-1] != 0
    october = client.get("/api/pnl?start=2026-10-01&end=2026-10-31&by=month&compare=0").get_json()
    assert october["column_keys"] == ["2026-10", "Total"]
    october_net = next(row for row in october["rows"] if row["label"] == "Net Income")
    assert october_net["values"] == [0, 0]
    spanned = client.get("/api/pnl?start=2026-12-15&end=2027-01-02&by=month&compare=0").get_json()
    assert spanned["column_keys"] == ["2026-12", "2027-01", "Total"]
    yearly = client.get("/api/pnl?start=2026-09-01&end=2026-11-30&by=year&compare=0").get_json()
    assert yearly["column_keys"] == ["", "Total"]
    yearly_net = next(row for row in yearly["rows"] if row["label"] == "Net Income")
    assert yearly_net["values"][-1] == net["values"][-1]
    assert client.get("/api/pnl?start=1900-01-01&end=1951-01-01&by=month&compare=0").status_code == 400

    # An unscoped year statement still lists only months that have activity.
    with connect() as conn:
        unscoped = build_pnl(conn, 2026, by="month")
    assert unscoped.columns[0] == "Jul 2026"
    assert "Jan 2026" not in unscoped.columns
    assert "Oct 2026" not in unscoped.columns

    feed = _feed(
        tmp_path / "xfers.json",
        BANK,
        [
            _txn("xfer-feb", BANK, "2026-02-10", "5.00", "QA XFER LEG"),
            _txn("xfer-mar-a", BANK, "2026-03-04", "-2.50", "QA XFER LEG"),
            _txn("xfer-mar-b", BANK, "2026-03-18", "-2.50", "QA XFER LEG"),
        ],
    )
    assert main(["import", str(feed)]) == 0
    for txn_id in ("xfer-feb", "xfer-mar-a", "xfer-mar-b"):
        assert main(["classify", txn_id, "--tag", "transfer", "--category", "Transfer"]) == 0
    from hpgbooks.web import create_app

    fresh = create_app().test_client()
    march = fresh.get("/api/pnl?start=2026-03-01&end=2026-03-31&by=month").get_json()
    assert march["prior_start"] == "2026-02-01"
    assert march["prior_end"] == "2026-02-28"
    memo = next(row for row in march["rows"] if row["label"].startswith("Memo: transfers excluded"))
    assert memo["label"] == "Memo: transfers excluded (2 rows, net)"
    assert memo["values"][-1] == -500
    assert memo["prior"] == 500
    assert memo["pct"] == -200.0


def test_pnl_compare_and_drilldown(client):
    body = client.get("/api/pnl?start=2026-08-01&end=2026-09-30&by=month&business=all").get_json()
    assert body["column_keys"] == ["2026-08", "2026-09", "Total"]
    labels = [row["label"] for row in body["rows"]]
    assert "Net Income" in labels
    owner = next(row for row in body["rows"] if row["label"].startswith("Owner Draws"))
    assert owner["values"][-1] == 1000
    assert labels.index(owner["label"]) > labels.index("Net Income")
    assert body["prior_start"] == "2026-06-01"
    net = next(row for row in body["rows"] if row["label"] == "Net Income")
    lines = client.get("/api/pnl/lines?start=2026-08-01&end=2026-09-30&business=all&label=Net%20Income").get_json()
    assert lines["total_cents"] == net["values"][-1]
    soft = client.get("/api/pnl/lines?start=2026-08-01&end=2026-09-30&label=Software%20%26%20Licenses&month=2026-09").get_json()
    assert [row["id"] for row in soft["rows"]] == ["soft-sep"]
    review = client.get("/api/pnl/lines?start=2026-08-01&end=2026-09-30&label=Uncategorized%20%2F%20needs_review").get_json()
    assert {row["id"] for row in review["rows"]} == {"q1", "q2"}
    draws = client.get("/api/pnl/lines?start=2026-08-01&end=2026-09-30&label=Owner%20Draws%20(below%20the%20line)").get_json()
    assert [row["id"] for row in draws["rows"]] == ["draw-sep"]
    assert client.get("/api/pnl/lines?label=Nope").status_code == 400
    for row in body["rows"]:
        if row["kind"] in ("line", "subtotal", "total") and not row["label"].startswith("Net after"):
            got = client.get("/api/pnl/lines", query_string={"start": "2026-08-01", "end": "2026-09-30", "label": row["label"]}).get_json()
            total = got["total_cents"]
            shown = row["values"][-1]
            assert abs(total) == abs(shown), row["label"]


def test_reports_vendors_review_rules(client):
    for name in ("pnl-by-business", "expenses-by-vendor", "cash-flow", "owner-draws", "schedule-c"):
        body = client.get(f"/api/reports/{name}?start=2026-01-01&end=2026-09-30").get_json()
        assert body["ok"] and body["columns"] and body["rows"], name
    assert client.get("/api/reports/nope").status_code == 404
    vendors = client.get("/api/vendors?start=2026-01-01&end=2026-09-30").get_json()
    assert vendors["rows"][0]["merchant"] == "ZZZ RENT"
    assert any(row["merchant"] == "=1+1<script>" for row in vendors["rows"])
    review = client.get("/api/review").get_json()
    assert review["count"] == 2
    assert all("suggestion" in row for row in review["rows"])
    rules = client.get("/api/rules").get_json()["rows"]
    assert rules
    rule_id = rules[0]["id"]
    token = _token(client)
    off = _post(client, f"/api/rules/{rule_id}/active", {"active": False}, token)
    assert off.status_code == 200
    on = _post(client, f"/api/rules/{rule_id}/active", {"active": True}, token)
    assert on.status_code == 200
    actions = [row["action"] for row in client.get("/api/audit").get_json()["rows"]]
    assert "rule_disable" in actions and "rule_enable" in actions
    preview = client.get("/api/rules/preview?pattern=ZZZ%20RENT").get_json()
    assert preview["count"] == 3
    assert client.get("/api/rules/preview?pattern=(").status_code == 400


def test_rule_from_transaction(client):
    token = _token(client)
    res = _post(
        client,
        "/api/classify",
        {"txn_id": "q1", "tag": "general", "category": "Bank & Card Fees", "save_rule": True, "pattern": "ZZZ QUEUE"},
        token,
    )
    body = res.get_json()
    assert body["rule_created"] is True
    assert body["applied_others"] == 1
    assert body["review_count"] == 0


def test_calendar_settings_and_search(client):
    cal = client.get("/api/calendar").get_json()
    assert cal["outlook"]["estimate"] is True
    assert any(item["merchant"] == "ZZZ RENT" for item in cal["items"])
    token = _token(client)
    saved = _post(client, "/api/settings", {"reserve_cents": 12550}, token)
    assert saved.get_json()["reserve_cents"] == 12550
    assert client.get("/api/settings").get_json()["reserve_cents"] == 12550
    assert _post(client, "/api/settings", {"reserve_cents": -1}, token).status_code == 400
    assert _post(client, "/api/settings", {"reserve_cents": "12"}, token).status_code == 400
    assert _post(client, "/api/settings", {"other": 1}, token).status_code == 400
    audit = client.get("/api/audit").get_json()["rows"]
    assert any(row["action"] == "setting" and row["new_value"] == "12550" for row in audit)
    found = client.get("/api/search?q=rent").get_json()
    assert found["transactions"] and found["vendors"][0]["name"] == "ZZZ RENT"
    accounts = client.get("/api/search?q=rewards").get_json()["accounts"]
    assert accounts and accounts[0]["id"] == REWARDS
    assert client.get("/api/search?q=" + "x" * 200).status_code == 400
    assert client.get("/api/nope").status_code == 404
    assert client.get("/api/nope").get_json()["ok"] is False


def test_spa_is_served_with_strict_csp(client):
    import re

    for path in ("/", "/transactions", "/reports/pnl", "/accounts/whatever", "/settings", "/app/", "/app/review"):
        page = client.get(path)
        assert page.status_code == 200, path
        html = page.get_data(as_text=True)
        assert '<div id="root">' in html
        # Every script is an external same-origin file; nothing inline.
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
        assert "http://" not in html and "https://" not in html
        assert page.headers["Cache-Control"] == "no-cache"
        csp = page.headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp and "style-src 'self'" in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert "frame-ancestors 'none'" in csp
    index = client.get("/").get_data(as_text=True)
    asset = re.search(r'src="(/app/assets/[^"]+\.js)"', index).group(1)
    served = client.get(asset)
    assert served.status_code == 200
    assert "immutable" in served.headers["Cache-Control"]
    assert client.get("/app/favicon.svg").status_code == 200
    assert client.get("/app/assets/nope.js").status_code == 404
    assert client.get("/app/../../hpgbooks/db.py").status_code == 404
    unknown = client.get("/definitely-not-a-page")
    assert unknown.status_code == 404
    assert b'<div id="root">' in unknown.data
    assert client.get("/api/nope").is_json


def test_api_get_does_not_write(client, books):
    """GETs open the database read-only: the audit log does not change."""
    with connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    for url in ("/api/session", "/api/dashboard", "/api/transactions", "/api/review", "/api/calendar", "/api/rules", "/api/pnl", "/api/search?q=zzz"):
        assert client.get(url).status_code == 200, url
    with connect() as conn:
        after = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert before == after
