"""WHMCS JSON API, CSV exports, and the sign-in rule for the customer lookup."""

from __future__ import annotations

import pytest

from hpgbooks.access import clear_passphrase_cache, reset_lockout
from hpgbooks.cli import main
from hpgbooks.db import connect, init_db
from hpgbooks.whmcs import sync
from whmcs_fake import FakeSource, factory_for

KEY = "b2" * 32
SECRET = "correct-horse-battery"
HOST = "books.example:8765"
PII = ("Alice", "Fakename", "alice@example.test", "Sample Widgets", "bob@example.test")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HPBOOKS_KEY", KEY)
    monkeypatch.setenv("HPBOOKS_DB", str(tmp_path / "books.db"))
    monkeypatch.delenv("HPBOOKS_KEY_FILE", raising=False)
    monkeypatch.delenv("HPGBOOKS_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("HPGBOOKS_NEW_PASSPHRASE", raising=False)
    init_db()
    reset_lockout()
    clear_passphrase_cache()
    yield tmp_path
    reset_lockout()
    clear_passphrase_cache()


@pytest.fixture()
def synced(env):
    with connect() as conn:
        sync(conn, ["BrandA"], source_factory=factory_for({"BrandA": FakeSource()}))
    return env


def _client(extra_hosts=None):
    from hpgbooks.web import create_app

    app = create_app()
    if extra_hosts is not None:
        app.config["ALLOWED_HOSTS"] = tuple(extra_hosts)
    return app.test_client()


def test_reports_before_sync_say_not_ready(env):
    client = _client()
    body = client.get("/api/whmcs/revenue").get_json()
    assert body == {"ok": True, "ready": False, "brands": ["BrandA", "BrandB", "BrandC"]}
    assert client.get("/api/whmcs/summary").get_json()["ready"] is False
    assert client.get("/api/whmcs/status").get_json()["ready"] is False
    assert client.get("/export/whmcs/revenue.csv").status_code == 404


def test_report_endpoints(synced):
    client = _client()
    revenue = client.get("/api/whmcs/revenue?start=2020-01-01&end=2026-12-31&by=year").get_json()
    assert revenue["ready"] and revenue["totals"]["gross_cents"] == 33200
    for path in ("mrr?months=12", "churn?start=2026-01-01&end=2026-09-30", "refunds", "dunning", "reconcile", "status", "summary"):
        response = client.get(f"/api/whmcs/{path}")
        assert response.status_code == 200, path
        assert response.headers["Cache-Control"] == "no-store"
        text = response.get_data(as_text=True)
        for secret in PII:
            assert secret not in text, (path, secret)
    assert client.get("/api/whmcs/summary").get_json()["mrr_cents"] > 0


def test_bad_parameters_are_400(synced):
    client = _client()
    assert client.get("/api/whmcs/revenue?brand=Nope").status_code == 400
    assert client.get("/api/whmcs/revenue?by=week").status_code == 400
    assert client.get("/api/whmcs/revenue?start=2026-13-01&end=2026-12-31").status_code == 400
    assert client.get("/api/whmcs/revenue?start=2026-01-01").status_code == 400
    assert client.get("/api/whmcs/reconcile?window=99").status_code == 400
    token = client.get("/api/session").get_json()["csrf_token"]
    assert client.post("/api/whmcs/customers/search", json={"q": "x" * 101}, headers={"X-CSRF-Token": token}).status_code == 400
    assert client.post("/api/whmcs/customers/search", json={"q": "alice", "brand": "Nope"}, headers={"X-CSRF-Token": token}).status_code == 400
    assert client.post("/api/whmcs/customers/search", data="q=alice", headers={"X-CSRF-Token": token, "Content-Type": "application/x-www-form-urlencoded"}).status_code in (400, 415)
    assert client.get("/api/whmcs/customers?q=alice").status_code == 404
    assert client.get("/api/whmcs/nope").status_code == 404
    assert client.get("/api/whmcs/customers/NotABrand/1").status_code == 404
    assert client.get("/api/whmcs/customers/BrandA/999").status_code == 404


def test_reports_are_read_only(synced):
    client = _client()
    token = client.get("/api/session").get_json()["csrf_token"]
    response = client.post("/api/whmcs/revenue", json={}, headers={"X-CSRF-Token": token})
    assert response.status_code in (404, 405)


def _search(client, q, headers=None):
    headers = dict(headers or {})
    token = client.get("/api/session", headers=headers).get_json()["csrf_token"]
    return client.post("/api/whmcs/customers/search", json={"q": q}, headers={**headers, "X-CSRF-Token": token})


def test_customer_lookup_on_loopback(synced):
    client = _client()
    rows = _search(client, "alice").get_json()["rows"]
    assert rows[0]["name"] == "Alice Fakename" and rows[0]["email"] == "alice@example.test"
    detail = client.get("/api/whmcs/customers/BrandA/1").get_json()["customer"]
    assert detail["totals"]["paid_cents"] == 1200 and len(detail["services"]) == 3


def test_customer_lookup_needs_sign_in_on_the_tailnet(synced, monkeypatch):
    monkeypatch.setenv("HPGBOOKS_NEW_PASSPHRASE", SECRET)
    assert main(["web-passphrase", "set"]) == 0
    clear_passphrase_cache()
    client = _client([HOST])
    headers = {"Host": HOST}
    response = _search(client, "alice", headers)
    assert response.status_code == 401 and b"Alice" not in response.data
    for path in (
        "/api/whmcs/customers/BrandA/1",
        "/api/whmcs/revenue",
        "/api/whmcs/summary",
        "/export/whmcs/revenue.csv",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 401, path
        assert b"Alice" not in response.data
    token = client.get("/api/session", headers=headers).get_json()["csrf_token"]
    login = client.post("/api/login", json={"passphrase": SECRET}, headers={**headers, "X-CSRF-Token": token})
    assert login.status_code == 200
    assert _search(client, "alice", headers).get_json()["rows"][0]["client_id"] == 1
    assert client.get("/api/whmcs/customers/BrandA/1", headers=headers).status_code == 200


def test_customer_handler_checks_sign_in_itself(synced, monkeypatch):
    """Even if the app-wide guard were bypassed, the handler refuses an unsigned tailnet request."""
    import hpgbooks.whmcs_api as module

    monkeypatch.setattr(module, "request_requires_login", lambda: True)
    monkeypatch.setattr(module, "session_is_authenticated", lambda: False)
    client = _client()
    assert _search(client, "alice").status_code == 401
    assert client.get("/api/whmcs/customers/BrandA/1").status_code == 401


def test_csv_exports(synced):
    client = _client()
    for table in ("revenue", "revenue-plans", "mrr", "mrr-trend", "churn", "churn-plans", "refunds", "refunds-largest", "dunning", "dunning-aging", "reconcile", "reconcile-rows", "gateways"):
        response = client.get(f"/export/whmcs/{table}.csv?start=2020-01-01&end=2026-12-31")
        assert response.status_code == 200, table
        assert response.mimetype == "text/csv"
        assert response.headers["Cache-Control"] == "no-store"
        assert f"whmcs-{table}.csv" in response.headers["Content-Disposition"]
        for secret in PII:
            assert secret not in response.get_data(as_text=True)
    assert client.get("/export/whmcs/customers.csv").status_code == 404
    assert client.get("/export/whmcs/revenue.csv?brand=Nope").status_code == 400


def test_whmcs_pages_load_the_app(synced):
    client = _client()
    for path in ("/whmcs", "/whmcs/revenue", "/whmcs/customers/BrandA/1"):
        response = client.get(path)
        assert response.status_code == 200, path
