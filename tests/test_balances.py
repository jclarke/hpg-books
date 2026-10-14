"""Statement balance anchors. Temporary database and key only."""

from __future__ import annotations

import pytest

from datetime import date

from hpgbooks.analytics import account_register, accounts_range, build_dashboard_range, cash_outlook
from hpgbooks.balances import (
    account_snapshots,
    anchor_history,
    parse_dollars,
    set_anchor,
    snapshot_for,
)
from hpgbooks.cli import main
from hpgbooks.db import ACCOUNTS, HpgbooksError, connect, init_db
from fake_accounts import BANK, REWARDS

KEY = "ef" * 32


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HPBOOKS_KEY", KEY)
    monkeypatch.setenv("HPBOOKS_DB", str(tmp_path / "books.db"))
    monkeypatch.delenv("HPBOOKS_KEY_FILE", raising=False)
    init_db()
    return tmp_path


def _insert(conn, txn_id, account, day, cents, *, pending=0, status="active"):
    now = "2026-03-01T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO transactions (
          id, account_id, date, amount_cents, direction, currency,
          name, merchant_name, description, pending, status,
          first_seen_at, last_seen_at, updated_at, source
        ) VALUES (?, ?, ?, ?, ?, 'USD', ?, ?, '', ?, ?, ?, ?, ?, 'test')
        """,
        (
            txn_id,
            account,
            day,
            cents,
            "in" if cents >= 0 else "out",
            txn_id,
            txn_id,
            pending,
            status,
            now,
            now,
            now,
        ),
    )


def _cash_book(conn):
    """The documented cash example, plus a pre-opening row that must stay out."""
    _insert(conn, "pre", BANK, "2025-12-31", 99900)
    _insert(conn, "jan5", BANK, "2026-01-05", 10000)
    _insert(conn, "jan10", BANK, "2026-01-10", -3000)
    _insert(conn, "pend", BANK, "2026-01-10", -500, pending=1)
    _insert(conn, "feb1", BANK, "2026-02-01", 5000)
    _insert(conn, "gone", BANK, "2026-02-01", -700, status="superseded")


def _liability_book(conn):
    _insert(conn, "chg1", REWARDS, "2026-03-01", -4000)
    _insert(conn, "pay1", REWARDS, "2026-03-02", 1500)
    _insert(conn, "chg2", REWARDS, "2026-03-05", -1000)
    _insert(conn, "pend-on", REWARDS, "2026-03-05", -200, pending=1)
    _insert(conn, "pend-after", REWARDS, "2026-03-06", -300, pending=1)


def test_parse_dollars_accepts_money_text_and_rejects_junk():
    assert parse_dollars("11786.73") == 1_178_673
    assert parse_dollars("$1,178.73") == 117_873
    assert parse_dollars("-25") == -2500
    assert parse_dollars(80) == 8000
    assert parse_dollars(80.1) == 8010
    with pytest.raises(HpgbooksError, match="balance is required"):
        parse_dollars("")
    with pytest.raises(HpgbooksError, match="balance is required"):
        parse_dollars(True)
    with pytest.raises(HpgbooksError, match="balance must be a dollar amount"):
        parse_dollars("80.123")
    with pytest.raises(HpgbooksError, match="balance must be a dollar amount"):
        parse_dollars("nope")
    with pytest.raises(HpgbooksError, match="balance is out of range"):
        parse_dollars("100000000.01")


def test_cash_anchor_opening_running_and_superseded(env):
    with connect() as conn:
        _cash_book(conn)
        bare = snapshot_for(conn, BANK)
        assert bare["anchored"] is False
        # Raw activity keeps every active row, including pending and pre-2026.
        assert bare["activity_cents"] == 99900 + 10000 - 3000 - 500 + 5000
        assert bare["display_cents"] == bare["activity_cents"]
        assert bare["real_balance_cents"] is None

        snap = set_anchor(conn, BANK, 8000, "2026-01-10", "statement", "January statement", actor="test")
        assert snap["anchored"] is True
        assert snap["opening_cents"] == 1000
        assert snap["real_balance_cents"] == 13000
        assert snap["display_cents"] == 13000
        assert snap["as_of_date"] == "2026-01-10"
        assert snap["source"] == "statement"
        assert snap["anchor_note"] == "January statement"
        assert snap["drift_cents"] is None
        assert snap["activity_cents"] == bare["activity_cents"]

        rows, _total, activity = account_register(conn, BANK, limit=20, status="all")
        assert activity == bare["activity_cents"]
        running = {row["id"]: row["running_cents"] for row in rows}
        assert running["jan5"] == 11000
        assert running["jan10"] == 8000
        assert running["feb1"] == 13000
        assert running["pend"] is None
        assert running["gone"] is None
        assert running["pre"] is None
        included = {row["id"]: row["in_balance"] for row in rows}
        assert included["feb1"] is True
        assert included["pend"] is False
        assert included["gone"] is False

        merchants = {
            row["id"]: row["merchant_name"]
            for row in conn.execute("SELECT id, merchant_name FROM transactions")
        }
        assert merchants["jan5"] == "jan5"


def test_liability_anchor_latest_wins_and_drift(env):
    with connect() as conn:
        _liability_book(conn)
        first = set_anchor(conn, REWARDS, 2500, "2026-03-02", "statement", "", actor="test")
        assert first["opening_cents"] == 0
        # Pending on Mar 5 is after this anchor, so it counts with the later pending.
        assert first["real_balance_cents"] == 2500 + 1000 + 200 + 300
        assert first["display_cents"] == first["real_balance_cents"]
        # Pending on the anchor date is inside the posted figure; the later pending is not.
        assert first["anchor_note"] == ""

        second = set_anchor(conn, REWARDS, 4000, "2026-03-05", "finance", "card", actor="test")
        assert second["anchor_cents"] == 4000
        assert second["computed_cents"] == 3500
        assert second["drift_cents"] == 500
        assert second["reconcile_cents"] == 4000
        assert second["opening_cents"] == 500
        # Posted Mar 5 is inside the newer anchor. Pending after it still counts.
        assert second["real_balance_cents"] == 4300
        assert second["as_of_date"] == "2026-03-05"
        assert second["source"] == "finance"

        # An older statement does not replace the later as-of date.
        backfill = set_anchor(conn, REWARDS, 2500, "2026-03-02", "statement", "backfill", actor="test")
        assert backfill["as_of_date"] == "2026-03-05"
        assert backfill["real_balance_cents"] == 4300

        history = anchor_history(conn, REWARDS)
        assert [row["as_of_date"] for row in history] == ["2026-03-05", "2026-03-02", "2026-03-02"]
        assert history[0]["note"] == "card"
        audits = conn.execute(
            "SELECT action, actor FROM audit_log WHERE action = 'balance_set' ORDER BY id"
        ).fetchall()
        assert len(audits) == 3
        assert {row["actor"] for row in audits} == {"test"}

        rows, _total, activity = account_register(conn, REWARDS, limit=20)
        running = {row["id"]: row["running_cents"] for row in rows}
        # Opening 500, charge +4000 owed, payment -1500, second charge +1000, later pending +300.
        assert running["chg1"] == 4500
        assert running["pay1"] == 3000
        assert running["chg2"] == 4000
        assert running["pend-on"] is None
        assert running["pend-after"] == 4300
        assert activity == -4000 + 1500 - 1000 - 200 - 300


def test_unanchored_liability_display_is_amount_owed(env):
    with connect() as conn:
        _insert(conn, "chg", REWARDS, "2026-04-01", -4000)
        snap = snapshot_for(conn, REWARDS)
        assert snap["anchored"] is False
        assert snap["activity_cents"] == -4000
        assert snap["display_cents"] == 4000
        snaps = account_snapshots(conn)
        assert [row["id"] for row in snaps] == [acct["id"] for acct in ACCOUNTS]
        assert len(snaps) == 5


def test_dashboard_and_outlook_use_the_anchor(env):
    with connect() as conn:
        _cash_book(conn)
        _insert(conn, "chg", REWARDS, "2026-02-02", -4000)
        before = build_dashboard_range(conn, "2026-01-01", "2026-02-28", "all")
        bank = next(row for row in before["cash"] if row["id"] == BANK)
        rewards = next(row for row in before["cash"] if row["id"] == REWARDS)
        assert bank["anchored"] is False
        assert bank["balance_cents"] == bank["display_cents"]
        assert rewards["display_cents"] == 4000
        assert rewards["balance_cents"] == -4000
        outlook = cash_outlook(conn, today=date(2026, 2, 15))
        assert outlook["opening_cents"] == bank["balance_cents"]

        set_anchor(conn, BANK, 8000, "2026-01-10", "statement", "", actor="test")
        set_anchor(conn, REWARDS, 2500, "2026-02-02", "statement", "", actor="test")
        after = build_dashboard_range(conn, "2026-01-01", "2026-02-28", "all")
        bank = next(row for row in after["cash"] if row["id"] == BANK)
        rewards = next(row for row in after["cash"] if row["id"] == REWARDS)
        assert bank["anchored"] is True
        assert bank["display_cents"] == 13000
        assert bank["balance_cents"] != 13000
        assert bank["opening_cents"] == 1000
        assert rewards["display_cents"] == 2500
        assert rewards["type"] == "liability"
        outlook = cash_outlook(conn, today=date(2026, 2, 15))
        assert outlook["opening_cents"] == 13000


def test_cli_and_validation(env, capsys):
    with connect() as conn:
        _cash_book(conn)
    assert main(["balances", "list"]) == 0
    listed = capsys.readouterr().out
    assert "imported activity" in listed
    assert "Bank 0101" in listed
    assert "Rewards 0303" in listed
    assert listed.count("\n") >= 5

    assert main(["balances", "set", "0101", "--balance", "80.00", "--as-of", "2026-01-10", "--note", "stmt"]) == 0
    printed = capsys.readouterr().out
    assert "130.00 as of 2026-01-10" in printed
    assert "(statement)" in printed
    assert "opening 10.00" in printed
    assert "reconciles to 80.00" in printed

    assert main(["balances", "set", "0303", "--balance", "25.00", "--as-of", "2026-03-02", "--source", "finance"]) == 0
    capsys.readouterr()
    with connect() as conn:
        _liability_book(conn)
    assert main(["balances", "set", "0303", "--balance", "40.00", "--as-of", "2026-03-05", "--source", "finance"]) == 0
    drifted = capsys.readouterr().out
    assert "reconciliation difference 5.00" in drifted
    assert "statement 40.00" in drifted
    assert "computed 35.00" in drifted

    assert main(["balances", "history", "0101"]) == 0
    history = capsys.readouterr().out
    assert "2026-01-10" in history
    assert "80.00" in history
    assert "stmt" in history

    assert main(["balances", "set", "0101", "--balance", "1.00", "--as-of", "2026-13-01"]) == 1
    assert "date must be YYYY-MM-DD" in capsys.readouterr().err
    assert main(["balances", "set", "nope", "--balance", "1.00", "--as-of", "2026-01-01"]) == 1
    assert "no account matches nope" in capsys.readouterr().err
    assert main(["balances", "set", "0101", "--balance", "12.345", "--as-of", "2026-01-01"]) == 1
    assert "balance must be a dollar amount" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        main(["balances", "set", "0101", "--balance", "1", "--as-of", "2026-01-01", "--source", "guess"])
    assert exc.value.code == 2

    with connect() as conn:
        note = conn.execute(
            "SELECT note FROM balance_anchors WHERE account_id = ? AND as_of_date = '2026-01-10'",
            (BANK,),
        ).fetchone()["note"]
        assert note == "stmt"
        empty = set_anchor(conn, BANK, 100, "2026-04-01", "statement", "   ", actor="cli")
        assert empty["anchor_note"] == ""
        stored = conn.execute(
            "SELECT note FROM balance_anchors WHERE account_id = ? AND as_of_date = '2026-04-01'",
            (BANK,),
        ).fetchone()["note"]
        assert stored is None
        with pytest.raises(HpgbooksError, match="note is too long"):
            set_anchor(conn, BANK, 100, "2026-04-02", "statement", "x" * 501, actor="cli")
        with pytest.raises(HpgbooksError, match="source must be finance or statement"):
            set_anchor(conn, BANK, 100, "2026-04-02", "guess", "", actor="cli")
        with pytest.raises(HpgbooksError, match="no such account"):
            set_anchor(conn, "not-an-account", 100, "2026-04-02", "statement", "", actor="cli")


def test_api_balances(env):
    from hpgbooks.web import create_app

    with connect() as conn:
        _cash_book(conn)
        _liability_book(conn)
    client = create_app().test_client()
    token = client.get("/api/session").get_json()["csrf_token"]

    def post(payload, csrf=token):
        headers = {"X-CSRF-Token": csrf} if csrf is not None else {}
        return client.post("/api/balances", json=payload, headers=headers)

    denied = post({"account_id": BANK, "balance": "80.00", "as_of": "2026-01-10"}, csrf=None)
    assert denied.status_code == 403

    missing = post({"account_id": BANK, "as_of": "2026-01-10"})
    assert missing.status_code == 400
    assert missing.get_json()["error"] == "balance is required"
    bad = post({"account_id": BANK, "balance": "80.123", "as_of": "2026-01-10"})
    assert bad.status_code == 400
    assert "dollar amount" in bad.get_json()["error"]
    guess = post({"account_id": BANK, "balance": "80.00", "as_of": "2026-01-10", "source": "guess"})
    assert guess.status_code == 400
    assert guess.get_json()["error"] == "source must be finance or statement"
    unknown = post({"account_id": "nope", "balance": "1.00", "as_of": "2026-01-10"})
    assert unknown.status_code == 404

    saved = post({"account_id": BANK, "balance": "80.00", "as_of": "2026-01-10", "note": "web"})
    assert saved.status_code == 200
    body = saved.get_json()
    assert body["balance"]["real_balance_cents"] == 13000
    assert body["balance"]["opening_cents"] == 1000
    assert body["balance"]["source"] == "statement"

    card = post({"account_id": REWARDS, "balance": "25.00", "as_of": "2026-03-02", "source": "finance"})
    assert card.status_code == 200
    again = post({"account_id": REWARDS, "balance": "40.00", "as_of": "2026-03-05", "source": "finance"})
    assert again.get_json()["balance"]["drift_cents"] == 500

    listed = client.get("/api/balances").get_json()
    assert len(listed["rows"]) == 5
    bank = next(row for row in listed["rows"] if row["id"] == BANK)
    assert bank["display_cents"] == 13000
    one = client.get(f"/api/balances?account={REWARDS}").get_json()
    assert one["balance"]["drift_cents"] == 500
    assert len(one["history"]) == 2
    assert client.get("/api/balances?account=nope").status_code == 404

    reg = client.get(f"/api/accounts/{BANK}/register?limit=20").get_json()
    assert reg["anchored"] is True
    assert reg["real_balance_cents"] == 13000
    assert reg["opening_cents"] == 1000
    assert reg["balance_cents"] != 13000
    by_id = {row["id"]: row for row in reg["rows"]}
    assert by_id["jan10"]["running_cents"] == 8000
    assert by_id["pend"]["in_balance"] is False

    accounts = client.get("/api/accounts?start=2026-01-01&end=2026-03-31").get_json()
    bank_row = next(row for row in accounts["rows"] if row["id"] == BANK)
    assert bank_row["balance_cents"] != bank_row["display_cents"]
    assert bank_row["display_cents"] == 13000

    with connect() as conn:
        actions = [row["action"] for row in conn.execute("SELECT action FROM audit_log WHERE action = 'balance_set'")]
        assert actions
        actors = {
            row["actor"]
            for row in conn.execute("SELECT actor FROM audit_log WHERE action = 'balance_set'")
        }
        assert "web" in actors


def test_reads_work_when_the_anchor_table_is_missing(env):
    with connect() as conn:
        _insert(conn, "jan5", BANK, "2026-01-05", 10000)
        conn.execute("DROP TABLE balance_anchors")
    with connect(readonly=True) as conn:
        snap = snapshot_for(conn, BANK)
        assert snap["anchored"] is False
        assert snap["display_cents"] == 10000
        rows = accounts_range(conn, "2026-01-01", "2026-01-31")
        bank = next(row for row in rows if row["id"] == BANK)
        assert bank["anchored"] is False
        assert bank["balance_cents"] == 10000
