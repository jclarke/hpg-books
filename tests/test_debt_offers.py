"""Saved loan offers: migration 11, validation, API CRUD and guards, CLI. Fake data only."""

from __future__ import annotations

import json

import pytest
from golden_fixture import run_cli
from personal_helpers import make_db

import hpgbooks.db as hpdb
from hpgbooks.access import clear_passphrase_cache, reset_lockout
from hpgbooks.db import HpgbooksError, connect
from hpgbooks.personal import offers

P = "mode=personal"
SECRET = "correct-horse-battery-staple"
HOST = "books.example:8765"
OFFER = {"lender": "Fake Lender A", "amount": "20,000", "apr": 9.5, "fee_pct": 3, "term_months": 60}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    return tmp_path


def _tables(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


# --- migration ------------------------------------------------------------------------


def test_fresh_database_has_the_table_once(db):
    with connect() as conn:
        assert "debt_offers" in _tables(conn)
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_version ORDER BY version")]
        assert versions == [version for version, _sql in hpdb.MIGRATIONS] and versions[-1] == 11
        hpdb._apply_migrations(conn)
        hpdb._apply_migrations(conn)
        assert conn.execute("SELECT COUNT(*) FROM schema_version WHERE version = 11").fetchone()[0] == 1


def test_migration_applies_to_a_version_10_database_and_keeps_its_data(tmp_path, monkeypatch):
    from golden_fixture import set_env

    set_env(monkeypatch, tmp_path)
    with monkeypatch.context() as mp:
        mp.setattr(hpdb, "MIGRATIONS", hpdb.MIGRATIONS[:10])
        hpdb.init_db()
        with connect() as conn:
            assert "debt_offers" not in _tables(conn)
            assert max(row[0] for row in conn.execute("SELECT version FROM schema_version")) == 10
            conn.execute("INSERT INTO settings (key, value, updated_at) VALUES ('reserve_cents', '123', 'x')")
            before = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in ("accounts", "rules", "transactions", "settings")}
            schema_before = {row[0]: row[1] for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")}
    with connect(readonly=True) as conn:
        assert offers.list_offers(conn) == []  # read-only before the migration: no table, no error
        assert "debt_offers" not in _tables(conn)
    with connect() as conn:  # the normal open applies migration 11
        assert "debt_offers" in _tables(conn)
        assert conn.execute("SELECT COUNT(*) FROM schema_version WHERE version = 11").fetchone()[0] == 1
        after = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in before}
        assert after == before
        schema_after = {row[0]: row[1] for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")}
        assert {k: v for k, v in schema_after.items() if k != "debt_offers"} == schema_before  # additive only
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_version WHERE version = 11").fetchone()[0] == 1


def test_table_checks_back_up_the_validation(db):
    with connect() as conn:
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO debt_offers (lender, amount_cents, apr, term_months, source, created_at, updated_at) VALUES ('x', 100, 5, 12, 'bogus', 'x', 'x')"
            )
        with pytest.raises(Exception):
            conn.execute("INSERT INTO debt_offers (lender, amount_cents, apr, term_months, created_at, updated_at) VALUES ('x', 0, 5, 12, 'x', 'x')")


# --- module ------------------------------------------------------------------------------


@pytest.mark.parametrize("text, cents", [("187500", 18750000), ("187,500", 18750000), ("$187,500.00", 18750000), (" $1,234.5 ", 123450), (2500, 250000), (99.99, 9999)])
def test_parse_money(text, cents):
    assert offers.parse_money(text) == cents


@pytest.mark.parametrize("bad", ["", "abc", "1.234", "-5", None, True, "1e5"])
def test_parse_money_refuses(bad):
    with pytest.raises(HpgbooksError):
        offers.parse_money(bad)


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"amount": "0"}, "amount"),
        ({"amount": None, "amount_cents": -5}, "amount"),
        ({"apr": -1}, "apr"),
        ({"apr": 100.01}, "apr"),
        ({"apr": "abc"}, "apr"),
        ({"fee_pct": 10.5}, "fee"),
        ({"fee_pct": -0.1}, "fee"),
        ({"term_months": 0}, "term"),
        ({"term_months": 361}, "term"),
        ({"term_months": "sixty"}, "term"),
        ({"expires_on": "2026-02-30"}, "expires"),
        ({"expires_on": "12/01/2026"}, "expires"),
        ({"lender": "  "}, "lender"),
        ({"lender": "x" * 81}, "lender"),
        ({"source": "bank"}, "source"),
        ({"monthly_payment": "0"}, "monthly payment"),
        ({"bogus": 1}, "unknown field"),
        ({"fee_from_proceeds": "maybe"}, "fee_from_proceeds"),
    ],
)
def test_validation(db, patch, message):
    body = {**OFFER, **patch}
    if body.get("amount") is None:
        body.pop("amount")
    with connect() as conn, pytest.raises(HpgbooksError, match=message):
        offers.create_offer(conn, body, actor="test")


def test_create_update_delete_with_audit(db):
    with connect() as conn:
        offer = offers.create_offer(conn, {**OFFER, "apr": "9.5%", "monthly_payment": "$420.10", "expires_on": "2026-10-15", "notes": "Fake quote"}, actor="test")
        assert offer["amount_cents"] == 2000000 and offer["amount"] == 20000.0
        assert offer["apr"] == 9.5 and offer["fee_pct"] == 3 and offer["fee_from_proceeds"] is True
        assert offer["monthly_payment_cents"] == 42010 and offer["monthly_payment"] == 420.10
        assert offer["source"] == "manual" and offer["expires_on"] == "2026-10-15" and offer["expired"] is False
        updated = offers.update_offer(conn, offer["id"], {"apr": 8.25, "fee_from_proceeds": False, "expires_on": "2026-09-01"}, actor="test")
        assert updated["apr"] == 8.25 and updated["fee_from_proceeds"] is False
        assert updated["amount_cents"] == 2000000 and updated["notes"] == "Fake quote"  # untouched fields kept
        assert updated["expired"] is True  # frozen today is 2026-09-30
        cleared = offers.update_offer(conn, offer["id"], {"monthly_payment": None, "expires_on": ""}, actor="test")
        assert cleared["monthly_payment_cents"] is None and cleared["expires_on"] is None
        offers.delete_offer(conn, offer["id"], actor="test")
        assert offers.list_offers(conn) == []
        with pytest.raises(HpgbooksError, match="no such offer"):
            offers.update_offer(conn, offer["id"], {"apr": 1}, actor="test")
        actions = [row[0] for row in conn.execute("SELECT action FROM audit_log WHERE action = 'debt_offer' ORDER BY id")]
        assert len(actions) == 4
        last = conn.execute("SELECT old_value, new_value FROM audit_log WHERE action = 'debt_offer' ORDER BY id DESC").fetchone()
        assert json.loads(last[0])["lender"] == "Fake Lender A" and last[1] is None


def test_offers_never_touch_the_ledger(db):
    with connect() as conn:
        tables = ("transactions", "classifications", "balance_anchors", "accounts", "account_payment_terms")
        before = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in tables}
        offer = offers.create_offer(conn, OFFER, actor="test")
        offers.update_offer(conn, offer["id"], {"apr": 7}, actor="test")
        offers.delete_offer(conn, offer["id"], actor="test")
        assert {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in tables} == before


# --- API ----------------------------------------------------------------------------------


@pytest.fixture()
def client(db):
    clear_passphrase_cache()
    from hpgbooks.web import create_app

    yield create_app().test_client()
    reset_lockout()
    clear_passphrase_cache()


def _token(client):
    return client.get(f"/api/session?{P}").get_json()["csrf_token"]


def test_api_crud(client):
    headers = {"X-CSRF-Token": _token(client)}
    assert client.get(f"/api/personal/debt-offers?{P}").get_json() == {"ok": True, "rows": []}
    created = client.post(f"/api/personal/debt-offers?{P}", json={**OFFER, "source": "grok"}, headers=headers)
    assert created.status_code == 201, created.get_data(as_text=True)
    body = created.get_json()
    offer_id = body["offer"]["id"]
    assert body["offer"]["source"] == "grok" and [row["id"] for row in body["rows"]] == [offer_id]
    second = client.post(f"/api/personal/debt-offers?{P}", json={"lender": "Fake Lender B", "amount_cents": 1500000, "apr": "11", "term_months": "36", "fee_from_proceeds": False}, headers=headers)
    assert second.status_code == 201 and second.get_json()["offer"]["fee_pct"] == 0
    put = client.put(f"/api/personal/debt-offers/{offer_id}?{P}", json={"apr": 8.99, "notes": "Fake note"}, headers=headers)
    assert put.status_code == 200 and put.get_json()["offer"]["apr"] == 8.99
    patch = client.patch(f"/api/personal/debt-offers/{offer_id}?{P}", json={"term_months": 48}, headers=headers)
    assert patch.get_json()["offer"]["term_months"] == 48 and patch.get_json()["offer"]["notes"] == "Fake note"
    bad = client.post(f"/api/personal/debt-offers?{P}", json={**OFFER, "apr": 120}, headers=headers)
    assert bad.status_code == 400 and "apr" in bad.get_json()["error"]
    assert client.patch(f"/api/personal/debt-offers/999?{P}", json={"apr": 1}, headers=headers).status_code == 404
    rows = client.get(f"/api/personal/debt-offers?{P}").get_json()["rows"]
    assert [row["lender"] for row in rows] == ["Fake Lender A", "Fake Lender B"]
    deleted = client.delete(f"/api/personal/debt-offers/{offer_id}?{P}", headers=headers)
    assert deleted.status_code == 200 and [row["lender"] for row in deleted.get_json()["rows"]] == ["Fake Lender B"]
    assert client.delete(f"/api/personal/debt-offers/{offer_id}?{P}", headers=headers).status_code == 404


def test_api_mode_gating_and_csrf(client):
    token = _token(client)
    headers = {"X-CSRF-Token": token}
    assert client.get("/api/personal/debt-offers").status_code == 404
    assert client.get("/api/personal/debt-offers?mode=business").status_code == 404
    assert client.post("/api/personal/debt-offers?mode=business", json=OFFER, headers=headers).status_code == 404
    assert client.post(f"/api/personal/debt-offers?{P}", json=OFFER).status_code == 403  # no CSRF token
    assert client.post(f"/api/personal/debt-offers?{P}", json=OFFER, headers={"X-CSRF-Token": "nope"}).status_code == 403
    assert client.post(f"/api/personal/debt-offers?{P}", data="x", headers=headers).status_code == 415
    offer_id = client.post(f"/api/personal/debt-offers?{P}", json=OFFER, headers=headers).get_json()["offer"]["id"]
    assert client.delete(f"/api/personal/debt-offers/{offer_id}?{P}").status_code == 403
    assert client.delete(f"/api/personal/debt-offers/{offer_id}?mode=business", headers=headers).status_code == 404
    assert len(client.get(f"/api/personal/debt-offers?{P}").get_json()["rows"]) == 1


def test_api_needs_sign_in_off_loopback(client, monkeypatch):
    from hpgbooks.cli import main
    from hpgbooks.web import create_app

    monkeypatch.setenv("HPGBOOKS_NEW_PASSPHRASE", SECRET)
    assert main(["web-passphrase", "set"]) == 0
    clear_passphrase_cache()
    app = create_app()
    app.config["ALLOWED_HOSTS"] = (HOST,)
    remote = app.test_client()
    headers = {"Host": HOST}
    token = remote.get(f"/api/session?{P}", headers=headers).get_json()["csrf_token"]
    assert remote.get(f"/api/personal/debt-offers?{P}", headers=headers).status_code == 401
    assert remote.post(f"/api/personal/debt-offers?{P}", json=OFFER, headers={**headers, "X-CSRF-Token": token}).status_code == 401
    assert remote.post("/api/login", json={"passphrase": SECRET}, headers={**headers, "X-CSRF-Token": token}).status_code == 200
    assert remote.get(f"/api/personal/debt-offers?{P}", headers=headers).status_code == 200


# --- CLI ----------------------------------------------------------------------------------


def test_cli_add_list_update_delete(db):
    code, out, err = run_cli(["personal", "offers", "add", "--lender", "Fake Lender A", "--amount", "$187,500.00", "--apr", "9.76",
                              "--fee", "3", "--term", "60", "--payment", "3,962", "--expires", "2026-10-31", "--source", "grok", "--notes", "Fake quote"])
    assert code == 0, err
    assert "saved offer #1 Fake Lender A: 187,500.00 at 9.76% for 60 months, 3% fee from proceeds, quoted 3,962.00/mo" in out
    code, out, err = run_cli(["personal", "offers", "add", "--lender", "Fake Lender B", "--amount", "187500", "--apr", "11.2", "--term", "36", "--fee-added", "--fee", "1"])
    assert code == 0, err
    code, out, _ = run_cli(["personal", "offers", "list"])
    assert code == 0 and "Fake Lender A" in out and "Fake Lender B" in out and "added" in out and "2026-10-31" in out
    code, out, _ = run_cli(["personal", "offers"])
    assert code == 0 and "Fake Lender B" in out
    code, out, _ = run_cli(["personal", "offers", "list", "--json"])
    rows = json.loads(out)
    assert [row["amount_cents"] for row in rows] == [18750000, 18750000]
    assert rows[0]["source"] == "grok" and rows[1]["source"] == "manual" and rows[1]["fee_from_proceeds"] is False
    code, out, err = run_cli(["personal", "offers", "update", "2", "--apr", "10.5", "--fee-from-proceeds"])
    assert code == 0, err
    with connect(readonly=True) as conn:
        b = offers.get_offer(conn, 2)
    assert b["apr"] == 10.5 and b["fee_from_proceeds"] is True and b["term_months"] == 36 and b["fee_pct"] == 1
    code, _out, err = run_cli(["personal", "offers", "update", "2"])
    assert code != 0 and "nothing to change" in err
    with pytest.raises(SystemExit) as exc:  # mutually exclusive flags: argparse refuses
        run_cli(["personal", "offers", "add", "--lender", "X", "--amount", "100", "--apr", "5", "--term", "12", "--fee-added", "--fee-from-proceeds"])
    assert exc.value.code == 2
    code, _out, err = run_cli(["personal", "offers", "add", "--lender", "X", "--amount", "0", "--apr", "5", "--term", "12"])
    assert code != 0 and "amount" in err
    code, out, err = run_cli(["personal", "offers", "delete", "1"])
    assert code == 0 and "deleted offer #1 Fake Lender A" in out
    code, _out, err = run_cli(["personal", "offers", "delete", "1"])
    assert code != 0 and "no such offer" in err
    with connect(readonly=True) as conn:
        assert [row["id"] for row in offers.list_offers(conn)] == [2]
