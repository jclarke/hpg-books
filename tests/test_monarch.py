"""Monarch export reconciliation, gap import, and category hints. Synthetic data only."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from personal_helpers import add_account, add_txn, cat_id, classify_all, make_db

from hpgbooks import monarch
from hpgbooks.db import HpgbooksError, connect

HEADER = "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner,Business Entity,Reviewed,Id\n"


def line(day, merchant, category, account, statement, amount, mid, entity=""):
    return f'{day},"{merchant}",{category},"{account}","{statement}",,{amount},,Shared,{entity},,{mid}\n'


CARD = "Fake Card (...4444)"
CSV = HEADER + "".join(
    [
        line("2099-02-10", "Fake Bank", "Credit Card Payment", CARD, "ONLINE PAYMENT THANK YOU", "100.00", "1001"),
        line("2099-02-05", "Fake Pizza", "Restaurants & Bars", CARD, "FAKE PIZZA 12", "-20.00", "1002"),
        line("2099-02-04", "Fake Pizza", "Restaurants & Bars", CARD, "FAKE PIZZA 12", "-20.00", "1003"),
        line("2099-01-20", "Fake Mart", "Groceries", CARD, "FAKE MART 77", "-70.00", "1004"),  # before SINCE is set to 2099-02-01 below
        line("2099-02-07", "Fake Gas", "Gas", CARD, "FAKE GAS 9", "-30.00", "1005"),
        line("2099-02-28", "Fake Card", "Financial Fees", CARD, "INTEREST CHARGE", "-5.00", "1006"),
        line("2099-02-20", "Biz Co", "Software", "Biz Account (...9999)", "BIZ CO", "-9.00", "1007", entity="Fake Inc"),
    ]
)
SINCE = "2099-02-01"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    with connect() as conn:
        add_account(conn, "card1", "liability", last4="4444", name="Fake Card")
        add_account(conn, "biz1", "cash", scope="business", last4="9999", name="Fake Biz")
        add_account(conn, "bank1", "cash", last4="1111", name="Fake Checking")
    return tmp_path


def test_parse_csv_signs_and_header():
    rows = monarch.parse_csv_text(CSV)
    assert len(rows) == 7 and rows[0].amount_cents == 10000 and rows[1].amount_cents == -2000 and rows[0].txn_id == "monarch:1001"
    with pytest.raises(HpgbooksError):
        monarch.parse_csv_text("a,b\n1,2\n")


def _alias_savings():
    """Site config: Monarch's "Savings" account is the one named Apple Savings."""
    from hpgbooks import config as hpconfig

    aliases = {**hpconfig.get_config().importers.monarch_aliases, "savings": "apple savings"}
    hpconfig.override(importers=hpconfig.ImportersConfig(monarch_aliases=aliases))


def test_account_resolution(db):
    with connect() as conn:
        add_account(conn, "sv1", "cash", last4="2468", name="Savings")
        add_account(conn, "sv2", "cash", name="Apple Savings (Apple Card, Cash, and Savings)")
        # Without a configured alias the account literally named Savings wins.
        assert monarch.resolve_accounts(conn, ["Savings"])["Savings"]["id"] == "sv1"
        _alias_savings()
        found = monarch.resolve_accounts(conn, [CARD, "Biz Account (...9999)", "Savings", "Nope"])
        assert found[CARD]["id"] == "card1" and found["Biz Account (...9999)"]["scope"] == "business"
        assert found["Savings"]["id"] == "sv2"  # the alias beats the account that is literally named Savings
        assert found["Nope"] is None
        assert monarch.resolve_accounts(conn, ["Savings"], {"Savings": "Fake Checking"})["Savings"]["id"] == "bank1"


def test_match_rows_tolerances_splits_and_sign_flip():
    existing = [
        {"id": "a", "date": "2099-02-06", "amount_cents": -2000, "name": "FAKE PIZZA"},
        {"id": "b", "date": "2099-02-09", "amount_cents": -1066, "name": "TARGET"},
        {"id": "c", "date": "2099-02-10", "amount_cents": 50000, "name": "PAYMENT FROM CHK"},
    ]

    def mk(d, amt, mid, cat="Shopping", st="X"):
        return monarch.MRow(d, "", cat, "acct", st, amt, "", mid)

    rows = [mk("2099-02-05", -2000, "1", st="FAKE PIZZA"), mk("2099-02-09", -805, "2"), mk("2099-02-09", -261, "3"),
            mk("2099-02-10", -50000, "4", cat="Credit Card Payment"), mk("2099-02-11", -777, "5")]
    result = monarch.match_rows(existing, rows)
    how = {r.mid: h for r, _e, h in result["pairs"]}
    assert how["1"] == "amount within 1d" and how["2"] == how["3"] == "split of one charge" and how["4"] == "sign flipped by Monarch"
    assert [r.mid for r in result["missing"]] == ["5"] and result["left_over"] == []


def test_monarch_duplicates_collapse_only_with_evidence():
    def mk(mid, amt, d="2099-02-05"):
        return monarch.MRow(d, "", "Gas", "a", "SAME", amt, "", mid)

    rows = [mk("1", -100), mk("2", -100), mk("3", -200, "2099-02-06"), mk("4", -200, "2099-02-06"), mk("5", -300), mk("6", -300)]
    pairs = [(rows[0], {}, "x"), (rows[2], {}, "x")]  # two identical groups only half held: the account duplicates itself
    kept, dropped = monarch.collapse_monarch_duplicates(rows, pairs, [rows[1], rows[3], rows[4], rows[5]])
    assert [r.mid for r in kept] == ["5"] and {r.mid for r in dropped} == {"2", "4", "6"}
    kept, dropped = monarch.collapse_monarch_duplicates(rows, pairs[:1], [rows[1], rows[2], rows[3], rows[4], rows[5]])
    assert len(kept) == 5 and not dropped  # one half-held group is not enough evidence


def _card_item(report):
    return [i for i in report if i["monarch"] == CARD][0]


def test_reconcile_import_and_idempotency(db):
    rows = monarch.parse_csv_text(CSV)
    with connect() as conn:
        add_txn(conn, "held1", "card1", "2099-02-06", -2000, "FAKE PIZZA 12")  # Monarch dates it the 5th: present
        add_txn(conn, "bankpay", "bank1", "2099-02-11", -10000, "FAKE CARD ONLINE PAYMENT")
        report = monarch.reconcile(conn, rows, since=SINCE, today="2099-03-10")
        assert [i for i in report if i["monarch"].startswith("Biz")][0]["action"].startswith("out of scope")
        item = _card_item(report)
        assert item["action"] == "import" and item["monarch_n"] == 5 and item["ours_n"] == 1
        assert sorted(r.mid for r in item["missing"]) == ["1001", "1003", "1005", "1006"]
        stats = monarch.apply_report(conn, report)[CARD]
        assert stats["inserted"] == 4 and dict(stats["kinds"]) == {"payment": 1, "purchase": 2, "interest": 1}
        cats = {r["txn_id"]: (r["group_name"], r["name"], r["source"]) for r in conn.execute(
            "SELECT p.txn_id, c.group_name, c.name, p.source FROM p_classifications p JOIN p_categories c ON c.id = p.category_id")}
        assert cats["monarch:1001"][1] == "Credit card payment" and cats["bankpay"][1] == "Credit card payment"
        assert cats["monarch:1006"][0] == "Fees & interest"
        row = conn.execute("SELECT source, merchant_name, provider_category FROM transactions WHERE id = 'monarch:1003'").fetchone()
        assert tuple(row) == (monarch.SOURCE, "Fake Pizza", "Restaurants & Bars")
        again = monarch.reconcile(conn, rows, since=SINCE, today="2099-03-10")
        assert not _card_item(again)["missing"]
        assert monarch.apply_report(conn, again) == {}


def test_recent_rows_are_left_to_the_finance_feed(db):
    rows = monarch.parse_csv_text(CSV)
    with connect() as conn:
        conn.execute("UPDATE accounts SET sync_enabled = 1 WHERE id = 'card1'")
        item = _card_item(monarch.reconcile(conn, rows, since=SINCE, today="2099-03-10", recent_days=3))
        assert not item["skipped_recent"]
        item = _card_item(monarch.reconcile(conn, rows, since=SINCE, today="2099-03-01", recent_days=3))
        assert [r.mid for r in item["skipped_recent"]] == ["1006"] and "1006" not in {r.mid for r in item["missing"]}


def test_authoritative_accounts_are_compared_not_imported(db):
    rows = monarch.parse_csv_text(CSV)
    with connect() as conn:
        add_txn(conn, "x1", "card1", "2099-02-05", -2000, "FAKE PIZZA")
        conn.execute("UPDATE transactions SET source = 'applecard_csv' WHERE id = 'x1'")
        item = _card_item(monarch.reconcile(conn, rows, since=SINCE, today="2099-03-10"))
        assert item["action"].startswith("compare only") and item["missing"]
        assert monarch.apply_report(conn, [item]) == {}


def test_apple_savings_deposit_rule(db):
    with connect() as conn:
        add_account(conn, "sv2", "cash", name="Apple Savings (Apple Card, Cash, and Savings)")
        _alias_savings()
        rows = monarch.parse_csv_text(HEADER + line("2099-02-10", "Deposit", "Transfer", "Savings", "Deposit", "1.25", "9001"))
        monarch.apply_report(conn, monarch.reconcile(conn, rows, since=SINCE, today="2099-03-10"))
        got = conn.execute(
            "SELECT c.group_name, c.name FROM p_classifications p JOIN p_categories c ON c.id = p.category_id WHERE p.txn_id = 'monarch:9001'"
        ).fetchone()
        assert tuple(got) == ("Income", "Refunds & reimbursements")


def test_rules_from_monarch_hints_leave_manual_decisions_alone(db):
    dates = ["2099-01-03", "2099-02-04", "2099-03-05", "2099-04-06"]
    text = HEADER
    for i, d in enumerate(dates):
        text += line(d, "Zorblax Grill", "Restaurants & Bars", CARD, "ZORBLAXGRILLCO 12", "-20.00", f"20{i}")
    text += line("2099-03-15", "Mixed", "Shopping", CARD, "MIXEDSTORE", "-9.00", "301")
    text += line("2099-03-16", "Mixed", "Restaurants & Bars", CARD, "MIXEDSTORE", "-9.00", "302")
    rows = monarch.parse_csv_text(text)
    with connect() as conn:
        for i, d in enumerate(dates):
            add_txn(conn, f"p{i}", "card1", d, -2000, "ZORBLAXGRILLCO 12")
        add_txn(conn, "m1", "card1", "2099-03-15", -900, "MIXEDSTORE")
        add_txn(conn, "m2", "card1", "2099-03-16", -900, "MIXEDSTORE")
        classify_all(conn)
        from hpgbooks.personal.classify import write_personal

        groceries = cat_id(conn, "Food", "Groceries")
        write_personal(conn, "p3", groceries, "manual", 1.0, "test-user", overwrite_manual=True, actor="test", audit_write=False)
        out = monarch.suggest_rules(conn, rows)
        patterns = [r["pattern"] for r in out["rules"]]
        assert any("zorblax" in p.lower() for p in patterns) and not any("mixed" in p.lower() for p in patterns)
        result = monarch.apply_rules(conn, out["rules"])
        assert result["unexpected"] == []
        names = {r["txn_id"]: r["name"] for r in conn.execute("SELECT p.txn_id, c.name FROM p_classifications p JOIN p_categories c ON c.id = p.category_id")}
        assert names["p0"] == "Dining out" and names["p3"] == "Groceries" and names["m1"] == "Uncategorized"
        assert monarch.suggest_rules(conn, rows)["rules"] == []


def _load_script():
    spec = importlib.util.spec_from_file_location("import_monarch", Path(__file__).resolve().parents[1] / "scripts" / "import_monarch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_reports_then_applies(db, tmp_path, capsys):
    path = tmp_path / "monarch.csv"
    path.write_text(CSV)
    script = _load_script()
    assert script.main([str(path), "--today", "2099-03-10", "--since", SINCE]) == 0
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0  # report only by default
    assert script.main([str(path), "--today", "2099-03-10", "--since", SINCE, "--apply"]) == 0
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'monarch_csv'").fetchone()[0] == 5
    capsys.readouterr()


def test_default_and_configured_aliases(db):
    from hpgbooks import config as hpconfig

    assert hpconfig.build({}).importers.monarch_aliases == {"apple card": "apple card", "apple cash": "apple cash"}
    with connect() as conn:
        add_account(conn, "ac1", "liability", name="Apple Card")
        add_account(conn, "jt1", "cash", name="Example Joint Checking")
        assert monarch.resolve_accounts(conn, ["Apple Card"])["Apple Card"]["id"] == "ac1"
        assert monarch.resolve_accounts(conn, ["Joint"])["Joint"] is None
        hpconfig.override(importers=hpconfig.build({"importers": {"monarch_aliases": {"Joint": "example joint checking"}}}).importers)
        assert monarch.resolve_accounts(conn, ["Joint"])["Joint"]["id"] == "jt1"
    with pytest.raises(hpconfig.ConfigError):
        hpconfig.build({"importers": {"monarch_aliases": {"Joint": 1}}})
