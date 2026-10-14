"""Account scope migration, the scope helper, and the account CLI. Fake data only."""

from __future__ import annotations

import json

import pytest

import hpgbooks.db as hpdb
from hpgbooks.db import ACCOUNTS, HpgbooksError, _apply_migrations, connect, init_db, seed_accounts
from fake_accounts import BANK, REWARDS, PAYPAL
from hpgbooks.scope import account_ids, parse_mode, resolve_account, scope_clause, txn_scope
from golden_fixture import run_cli
from personal_fake import CARD_A, CHECKING, MORTGAGE, accounts_payload

KEY = "ef" * 32


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HPBOOKS_KEY", KEY)
    monkeypatch.setenv("HPBOOKS_DB", str(tmp_path / "books.db"))
    monkeypatch.delenv("HPBOOKS_KEY_FILE", raising=False)
    monkeypatch.setenv("HPGBOOKS_SEED_CSV", str(tmp_path / "no-seed.csv"))
    return tmp_path


def _cols(conn) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}


def test_fresh_database_has_scope_and_five_business_accounts(env):
    init_db()
    with connect(readonly=True) as conn:
        assert {"scope", "class", "include_in_net_worth", "sync_enabled", "display_name", "nickname", "closed"} <= _cols(conn)
        rows = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM accounts")}
    assert set(rows) == {acct["id"] for acct in ACCOUNTS}
    assert all(row["scope"] == "business" for row in rows.values())
    assert rows[BANK]["class"] == "cash" and rows[REWARDS]["class"] == "liability"


def _v7_database(monkeypatch) -> None:
    """A database as master left it: migrations 1..7, the five accounts, one transaction."""
    monkeypatch.setattr(hpdb, "MIGRATIONS", hpdb.MIGRATIONS[:7])
    with connect() as conn:
        for acct in ACCOUNTS:
            conn.execute(
                "INSERT INTO accounts (id, name, type, last4, institution, notes) VALUES (?, ?, ?, ?, ?, ?)",
                (acct["id"], acct["name"], acct["type"], acct["last4"], acct["institution"], acct["notes"]),
            )
        conn.execute(
            """
            INSERT INTO transactions (id, account_id, date, amount_cents, name, first_seen_at, last_seen_at, updated_at)
            VALUES ('t1', ?, '2026-01-02', -1234, 'ZZZ', 'x', 'x', 'x')
            """,
            (BANK,),
        )
        assert "scope" not in _cols(conn)
    monkeypatch.undo()


def test_migration_on_an_existing_database_is_idempotent_and_keeps_ledger(env, monkeypatch):
    _v7_database(monkeypatch)
    monkeypatch.setenv("HPBOOKS_KEY", KEY)
    monkeypatch.setenv("HPBOOKS_DB", str(env / "books.db"))
    with connect() as conn:  # connect() migrates
        before = dict(conn.execute("SELECT * FROM transactions WHERE id = 't1'").fetchone())
        rows = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM accounts")}
        assert all(row["scope"] == "business" for row in rows.values())
        assert rows[BANK]["class"] == "cash" and rows[REWARDS]["class"] == "liability"
        _apply_migrations(conn)
        conn.execute("DELETE FROM schema_version WHERE version = 8")
        _apply_migrations(conn)  # columns already there: no error, same shape
        assert conn.execute("SELECT COUNT(*) FROM schema_version WHERE version = 8").fetchone()[0] == 1
        after = dict(conn.execute("SELECT * FROM transactions WHERE id = 't1'").fetchone())
    assert before == after


def test_seed_accounts_never_resets_scope_or_user_fields(env):
    init_db()
    with connect() as conn:
        conn.execute("UPDATE accounts SET scope = 'excluded', display_name = 'Mine', include_in_net_worth = 0 WHERE id = ?", (REWARDS,))
        seed_accounts(conn)
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (REWARDS,)).fetchone()
    assert (row["scope"], row["display_name"], row["include_in_net_worth"]) == ("excluded", "Mine", 0)
    init_db()
    with connect(readonly=True) as conn:
        assert conn.execute("SELECT scope FROM accounts WHERE id = ?", (REWARDS,)).fetchone()[0] == "excluded"


def test_scope_helper(env):
    init_db()
    assert parse_mode(None) == "business" and parse_mode("") == "business" and parse_mode("personal") == "personal"
    with pytest.raises(HpgbooksError):
        parse_mode("all")
    with connect() as conn:
        sql, params = scope_clause(conn, "personal")
        assert "?" in sql and params == ["personal"]
        assert account_ids(conn, "personal") == set()
        assert BANK in account_ids(conn, "business")
        assert txn_scope(conn, "missing") is None


def test_discover_dry_run_real_and_idempotent(env):
    init_db()
    payload = env / "accounts.json"
    payload.write_text(accounts_payload(), encoding="utf-8")
    code, out, err = run_cli(["accounts", "discover", str(payload), "--dry-run", "--as-of", "2026-09-30"])
    assert code == 0, err
    assert "would register 7 new" in out
    with connect(readonly=True) as conn:
        assert account_ids(conn, "personal") == set()
    code, out, _ = run_cli(["accounts", "discover", str(payload), "--as-of", "2026-09-30"])
    assert code == 0 and "registered 7 new, 0 already known" in out
    assert "1111" in out and "fake-p-checking" not in out  # last 4 only in the table
    with connect(readonly=True) as conn:
        assert CHECKING in account_ids(conn, "personal")
        classes = {row["id"]: row["class"] for row in conn.execute("SELECT id, class FROM accounts")}
        anchors = conn.execute("SELECT COUNT(*) FROM balance_anchors").fetchone()[0]
        audits = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'account_register'").fetchone()[0]
    assert classes[MORTGAGE] == "loan" and classes[CARD_A] == "liability" and anchors == 7 and audits == 7
    code, out, _ = run_cli(["accounts", "discover", str(payload), "--as-of", "2026-09-30", "--scope", "business"])
    assert "registered 0 new, 7 already known" in out
    with connect(readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM balance_anchors").fetchone()[0] == 7
        assert conn.execute("SELECT scope FROM accounts WHERE id = ?", (CHECKING,)).fetchone()[0] == "personal"
    # A plain JSON list works too.
    plain = env / "plain.json"
    plain.write_text(accounts_payload(wrapper=False), encoding="utf-8")
    code, out, _ = run_cli(["accounts", "discover", str(plain), "--as-of", "2026-09-30"])
    assert code == 0 and "0 new, 7 already known" in out


def test_discover_scrubs_full_account_numbers(env):
    init_db()
    payload = env / "a.json"
    payload.write_text(json.dumps([{"id": "fake-x", "name": "Checking 123456789012", "mask": "9876543210", "type": "depository"}]))
    code, out, _ = run_cli(["accounts", "discover", str(payload)])
    assert code == 0 and "123456789012" not in out and "9876543210" not in out
    with connect(readonly=True) as conn:
        row = conn.execute("SELECT name, last4 FROM accounts WHERE id = 'fake-x'").fetchone()
    assert row["last4"] == "3210" and "123456789012" not in row["name"]


def test_set_scope_ambiguity_and_audit(env):
    init_db()
    payload = env / "accounts.json"
    payload.write_text(accounts_payload(), encoding="utf-8")
    run_cli(["accounts", "discover", str(payload)])
    code, _out, err = run_cli(["accounts", "set-scope", "Fake Card Co", "excluded"])
    assert code == 1 and "more than one account" in err
    code, out, err = run_cli(["accounts", "set-scope", "3333", "excluded"])
    assert code == 0, err
    with connect(readonly=True) as conn:
        assert conn.execute("SELECT scope FROM accounts WHERE id = ?", (CARD_A,)).fetchone()[0] == "excluded"
        audit = conn.execute("SELECT * FROM audit_log WHERE action = 'account_scope'").fetchone()
    assert audit["old_value"] == "personal" and audit["new_value"] == "excluded"
    code, out, _ = run_cli(["accounts", "set-scope", "3333", "excluded"])
    assert "already excluded" in out
    code, out, _ = run_cli(["accounts", "rename", "1111", "Joint checking"])
    assert code == 0
    code, out, _ = run_cli(["accounts", "list", "--scope", "personal", "--json"])
    rows = json.loads(out)
    assert {row["display_name"] for row in rows if row["id"] == CHECKING} == {"Joint checking"}
    assert all(row["scope"] == "personal" for row in rows)
    code, _out, err = run_cli(["accounts", "set-class", "0101", "loan"])
    assert code == 1 and "business account keeps" in err
    code, out, _ = run_cli(["accounts", "set-class", "7777", "other"])
    assert code == 0


def test_sync_list_reads_accounts_from_the_database(env):
    init_db()
    payload = env / "accounts.json"
    payload.write_text(accounts_payload(), encoding="utf-8")
    run_cli(["accounts", "discover", str(payload)])
    code, out, _ = run_cli(["accounts", "sync-list", "--json"])
    rows = json.loads(out)
    by_id = {row["id"]: row for row in rows}
    assert by_id[BANK]["file_prefix"] == "0101" and by_id[BANK]["tool"] == "finance_query_account_transactions"
    assert by_id[PAYPAL]["file_prefix"] == "paypal"
    assert by_id[REWARDS]["tool"] == "finance_query_liability_transactions"
    assert by_id[CHECKING]["file_prefix"] == "personal/1111"
    assert by_id[MORTGAGE]["tool"] == "finance_query_liability_transactions"
    run_cli(["accounts", "set-scope", "2222", "excluded"])
    code, out, _ = run_cli(["accounts", "sync-list", "--scope", "personal"])
    assert "personal/2222" not in out and "personal/1111" in out and "0101" not in out


def test_resolve_account_by_id_last4_and_name(env):
    init_db()
    with connect(readonly=True) as conn:
        assert resolve_account(conn, "0101")["id"] == BANK
        assert resolve_account(conn, BANK)["id"] == BANK
        assert resolve_account(conn, "paypal")["id"] == PAYPAL
        with pytest.raises(HpgbooksError):
            resolve_account(conn, "nothing-like-this")


def test_guard_refuses_the_real_database(env, monkeypatch):
    monkeypatch.setenv("HPBOOKS_DB", hpdb.DEFAULT_DB)
    with pytest.raises(pytest.fail.Exception):
        with connect(readonly=True):
            pass
    monkeypatch.delenv("HPBOOKS_DB")
    assert hpdb.db_path() == hpdb.DEFAULT_DB
    with pytest.raises(pytest.fail.Exception):
        with connect(readonly=True):
            pass
