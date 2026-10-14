"""Business routes refuse personal mode and personal transaction ids. Fake data only."""

from __future__ import annotations

import pytest

from golden_fixture import build_business_ledger, freeze_today, run_cli, set_env
from personal_fake import CHECKING, accounts_payload


@pytest.fixture()
def client(tmp_path, monkeypatch):
    set_env(monkeypatch, tmp_path)
    freeze_today(monkeypatch)
    build_business_ledger(tmp_path)
    payload = tmp_path / "accounts.json"
    payload.write_text(accounts_payload(), encoding="utf-8")
    assert run_cli(["accounts", "discover", str(payload), "--as-of", "2026-09-30"])[0] == 0
    from hpgbooks.db import connect

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO transactions (id, account_id, date, amount_cents, name, first_seen_at, last_seen_at, updated_at)
            VALUES ('fake-personal-1', ?, '2026-09-02', -5000, 'PERSONAL THING', 'x', 'x', 'x')
            """,
            (CHECKING,),
        )
    from hpgbooks.web import create_app

    return create_app().test_client()


def _token(client, mode="business"):
    return client.get(f"/api/session?mode={mode}").get_json()["csrf_token"]


@pytest.mark.parametrize(
    "url",
    ["/api/dashboard", "/api/pnl", "/api/transactions", "/api/vendors", "/api/accounts", "/api/calendar",
     "/api/review", "/api/rules", "/api/balances", "/api/reports/cash-flow", "/export/transactions.csv",
     "/export/pnl.csv", pytest.param("/api/margins", marks=pytest.mark.whmcs_on),
     pytest.param("/api/whmcs/summary", marks=pytest.mark.whmcs_on)],
)
def test_business_routes_refuse_personal_mode(client, url):
    sep = "&" if "?" in url else "?"
    assert client.get(f"{url}{sep}mode=personal").status_code == 404
    assert client.get(f"{url}{sep}mode=bogus").status_code == 400


def test_personal_ids_are_404_on_business_detail_and_classify(client):
    assert client.get("/api/transactions/fake-personal-1").status_code == 404
    assert client.get("/api/transactions/g-bank-client-3").status_code == 200
    token = _token(client)
    response = client.post(
        "/api/classify",
        json={"txn_id": "fake-personal-1", "tag": "general", "category": "Office/Other"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code in (400, 404)
    response = client.post(
        "/api/classify/bulk",
        json={"txn_ids": ["fake-personal-1"], "tag": "general", "category": "Office/Other"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code in (400, 404)
    assert client.get(f"/api/accounts/{CHECKING}/register").status_code == 404
    assert client.get(f"/api/balances?account={CHECKING}").status_code == 404
    body = client.get("/api/transactions?search=PERSONAL").get_json()
    assert body["total"] == 0
    assert client.get(f"/api/transactions?account={CHECKING}").status_code == 400
    assert run_cli(["classify", "fake-personal-1", "--tag", "general", "--category", "Office/Other"])[0] == 1


def test_session_lists_mode_accounts_and_scope_change_moves_numbers(client):
    business = client.get("/api/session").get_json()
    assert CHECKING not in {acct["id"] for acct in business["accounts"]}
    rows = client.get("/api/accounts/settings").get_json()["rows"]
    assert {row["id"] for row in rows} >= {CHECKING}
    before = client.get("/api/transactions?search=PERSONAL").get_json()["total"]
    token = _token(client)
    response = client.patch(f"/api/accounts/{CHECKING}/settings", json={"scope": "business"}, headers={"X-CSRF-Token": token})
    assert response.status_code == 200
    assert response.get_json()["scope_change"]["transactions"] == 1
    after = client.get("/api/transactions?search=PERSONAL").get_json()["total"]
    assert (before, after) == (0, 1)
    response = client.patch(f"/api/accounts/{CHECKING}/settings", json={"scope": "nope"}, headers={"X-CSRF-Token": token})
    assert response.status_code == 400
    response = client.patch(f"/api/accounts/{CHECKING}/settings", json={"scope": "personal"})
    assert response.status_code == 403  # CSRF
    response = client.patch("/api/accounts/fake-missing/settings", json={"scope": "personal"}, headers={"X-CSRF-Token": token})
    assert response.status_code == 404
    audit = client.get("/api/audit").get_json()["rows"]
    assert any(row["action"] == "account_scope" for row in audit)


def test_audit_is_scoped(client):
    token = _token(client)
    client.patch(f"/api/accounts/{CHECKING}/settings", json={"include_in_net_worth": False}, headers={"X-CSRF-Token": token})
    business = client.get("/api/audit").get_json()["rows"]
    personal = client.get("/api/audit?mode=personal").get_json()["rows"]
    assert not any(row["action"] == "account_settings" for row in business)
    assert any(row["action"] == "account_settings" for row in personal)
    assert not any(row["action"] == "classify" for row in personal)
