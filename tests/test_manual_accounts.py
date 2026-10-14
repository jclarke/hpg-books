"""Balance-only manual accounts and scripts/set_manual_balance.py. Synthetic data only."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from personal_helpers import add_account, add_txn, make_db

from hpgbooks import manual_accounts
from hpgbooks.accounts_admin import discover
from hpgbooks.db import HpgbooksError, connect


@pytest.fixture()
def db(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    return tmp_path


def _anchor(conn, account_id):
    return [tuple(r) for r in conn.execute("SELECT as_of_date, balance_cents, source, note FROM balance_anchors WHERE account_id = ?", (account_id,))]


def test_create_liability_and_asset(db):
    with connect() as conn:
        manual_accounts.set_balance(conn, "Fake Mortgage 1234", "250,000.50", "2099-01-02", create_class="loan", last4="1234", institution="Fake Lender", subtype="mortgage")
        manual_accounts.set_balance(conn, "12 Fake St (home)", "400000", "2099-01-02", create_class="other")
        loan = dict(conn.execute("SELECT * FROM accounts WHERE name = 'Fake Mortgage 1234'").fetchone())
        home = dict(conn.execute("SELECT * FROM accounts WHERE name = '12 Fake St (home)'").fetchone())
        assert (loan["scope"], loan["class"], loan["type"], loan["last4"], loan["sync_enabled"]) == ("personal", "loan", "liability", "1234", 0)
        assert (home["scope"], home["class"], home["type"], home["sync_enabled"]) == ("personal", "other", "cash", 0)
        assert loan["notes"] == "manual: balance from Monarch 2099-01-02, update manually"
        assert _anchor(conn, loan["id"])[0][:2] == ("2099-01-02", 25000050)
        assert _anchor(conn, home["id"])[0][1] == 40000000
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_refresh_is_idempotent_and_appends_new_dates(db):
    with connect() as conn:
        manual_accounts.set_balance(conn, "Fake 401k", "100.00", "2099-01-02", create_class="investment")
        assert "already recorded" in manual_accounts.set_balance(conn, "fake 401k", "100.00", "2099-01-02")
        manual_accounts.set_balance(conn, "401k", "120.00", "2099-02-01", label="Retirement Plan Provider")
        account_id = conn.execute("SELECT id FROM accounts WHERE name = 'Fake 401k'").fetchone()[0]
        assert [a[:2] for a in _anchor(conn, account_id)] == [("2099-01-02", 10000), ("2099-02-01", 12000)]
        assert conn.execute("SELECT notes FROM accounts WHERE id = ?", (account_id,)).fetchone()[0] == "manual: balance from Retirement Plan Provider 2099-02-01, update manually"
        assert conn.execute("SELECT COUNT(*) FROM accounts WHERE name LIKE '%401k%'").fetchone()[0] == 1


def test_refuses_unknown_synced_and_ambiguous_accounts(db):
    with connect() as conn:
        add_account(conn, "synced1", "cash", last4="9999", name="Fake Synced")
        with pytest.raises(HpgbooksError, match="no manual account"):
            manual_accounts.set_balance(conn, "Fake Synced", "1.00", "2099-01-02")  # never anchors a synced account
        with pytest.raises(HpgbooksError, match="already exists"):
            manual_accounts.create_account(conn, "Fake Synced", "cash", last4="9999")
        manual_accounts.set_balance(conn, "Fake Roth A", "1", "2099-01-02", create_class="investment")
        manual_accounts.set_balance(conn, "Fake Roth B", "2", "2099-01-02", create_class="investment")
        with pytest.raises(HpgbooksError, match="more than one"):
            manual_accounts.set_balance(conn, "Roth", "3", "2099-01-02")
        with pytest.raises(HpgbooksError):
            manual_accounts.set_balance(conn, "Fake Roth A", "abc", "2099-01-02")


def test_net_worth_signs_and_no_effect_on_spending(db):
    from hpgbooks.personal.analytics import cash_flow, net_worth, spending

    with connect() as conn:
        add_account(conn, "bank1", "cash", last4="1111", name="Fake Checking")
        add_txn(conn, "t1", "bank1", "2099-01-01", -1234, "FAKE SHOP")
        before_txn = [tuple(r) for r in conn.execute("SELECT id, account_id, amount_cents FROM transactions")]
        base = net_worth(conn)["net_cents"]
        spend_before = (spending(conn, "2099-01-01", "2099-01-31"), cash_flow(conn, "2099-01", 3))
        manual_accounts.set_balance(conn, "Fake Loan", "1000.00", "2099-01-02", create_class="loan")
        manual_accounts.set_balance(conn, "Fake House", "5000.00", "2099-01-02", create_class="other")
        assert net_worth(conn)["net_cents"] == base - 100000 + 500000
        assert [tuple(r) for r in conn.execute("SELECT id, account_id, amount_cents FROM transactions")] == before_txn
        assert (spending(conn, "2099-01-01", "2099-01-31"), cash_flow(conn, "2099-01", 3)) == spend_before


def test_discover_does_not_duplicate_manual_accounts(db):
    with connect() as conn:
        manual_accounts.set_balance(conn, "Fake Mortgage 9999", "10", "2099-01-02", create_class="loan", last4="9999")
        manual_accounts.set_balance(conn, "Fake Coin Exchange", "0", "2099-01-02", create_class="investment")
        manual_accounts.set_balance(conn, "Fake Holder Roth IRA (Fakeco)", "5", "2099-01-02", create_class="investment", institution="Fakeco")
        manual_accounts.set_balance(conn, "Other Person Roth IRA (Fakeco)", "6", "2099-01-02", create_class="investment", institution="Fakeco")
        count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        payload = json.dumps(
            [
                {"id": "f1", "name": "Fake Mortgage", "mask": "9999", "type": "loan", "subtype": "mortgage", "current_balance": "9"},
                {"id": "f2", "name": "Fake Coin Exchange", "type": "investment"},
                {"id": "f3", "name": "Brand New Bank", "mask": "4444", "type": "depository"},
            ]
        )
        statuses = {r["status"] for r in discover(conn, payload, scope="personal", as_of="2099-01-03")}
        assert statuses == {"manual-match", "new"}
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == count + 1
        # an institution alone (Fakeco) must not make a different new account look like a manual one
        other = json.dumps([{"id": "f4", "name": "Fakeco Brokerage", "institution": "Fakeco", "type": "investment"}])
        assert discover(conn, other, scope="personal")[0]["status"] == "new"
        assert conn.execute("SELECT COUNT(*) FROM balance_anchors WHERE as_of_date = '2099-01-03'").fetchone()[0] == 0  # f1's balance never lands on the manual account


def _script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "set_manual_balance.py"
    spec = importlib.util.spec_from_file_location("set_manual_balance_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_single_and_batch(db, tmp_path, capsys):
    script = _script()
    assert script.main(["Fake Card 4321", "12.34", "--as-of", "2099-03-01", "--create", "--class", "liability", "--last4", "4321"]) == 0
    assert "created Fake Card 4321" in capsys.readouterr().out
    assert script.main(["Fake Card", "20", "--as-of", "2099-03-02"]) == 0
    assert script.main(["Nothing Here", "1"]) == 1
    assert "no manual account" in capsys.readouterr().err
    batch = tmp_path / "b.json"
    batch.write_text(json.dumps([{"name": "Fake Car", "balance": "9000", "class": "other"}, {"name": "Fake Card 4321", "balance": "5"}]))
    assert script.main(["--batch", str(batch), "--as-of", "2099-03-03", "--dry-run"]) == 0
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts WHERE name = 'Fake Car'").fetchone()[0] == 0
        assert [a[:2] for a in _anchor(conn, "manual-fake-card-4321")] == [("2099-03-01", 1234), ("2099-03-02", 2000)]
    assert script.main(["--batch", str(batch), "--as-of", "2099-03-03"]) == 0
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM balance_anchors").fetchone()[0] == 4
    assert script.main(["--create", "X", "1"]) == 1
