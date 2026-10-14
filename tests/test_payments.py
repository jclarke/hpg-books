"""Minimum payments, due dates, APR: trust rules, rolling, inference, CLI, API, sheet sync. Fake data only."""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest
from golden_fixture import run_cli
from personal_helpers import add_account, add_txn, anchor, classify_all, make_db

from hpgbooks import applecard, cfna, payments as pay
from hpgbooks.db import MIGRATIONS, HpgbooksError, _apply_migrations, connect

TODAY = "2026-10-04"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    with connect() as conn:
        add_account(conn, "card1", "liability", last4="4444", name="Fake Card")
        add_account(conn, "loan1", "loan", last4="5555", name="Fake Auto Loan")
        add_account(conn, "bank1", "cash", last4="1111", name="Fake Checking")
        add_account(conn, "biz1", "liability", scope="business", last4="9999", name="Fake Biz Card")
    return tmp_path


def terms(conn, account_id="card1", **kw):
    base = {"min_payment_cents": 10000, "due_date": "2026-10-15", "estimated": 0}
    base.update(kw)
    source = base.pop("source", "statement")
    as_of = base.pop("as_of", "2026-09-20")
    return pay.merge_terms(conn, account_id, base, source=source, as_of=as_of)


def paid(conn, txn_id, day, cents, name="ONLINE PAYMENT THANK YOU", account="card1"):
    add_txn(conn, txn_id, account, day, cents, name)


# --- schema --------------------------------------------------------------------------------


def test_migration_adds_the_table_once_and_keeps_data(db):
    with connect() as conn:
        assert [v for v, _ in MIGRATIONS][-1] >= 10
        conn.execute("DROP TABLE account_payment_terms")
        conn.execute("DELETE FROM schema_version WHERE version = 10")
    with connect(readonly=True) as conn:
        assert pay.build(conn, "personal")["rows"]  # a read-only open before migration still works
        assert pay.get_terms(conn, "card1") is None
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 9  # five seeded business accounts plus ours
        terms(conn)
        _apply_migrations(conn)
        assert conn.execute("SELECT COUNT(*) FROM schema_version WHERE version = 10").fetchone()[0] == 1
        with pytest.raises(Exception):
            conn.execute("UPDATE account_payment_terms SET autopay = 'maybe'")
        with pytest.raises(Exception):
            conn.execute("UPDATE account_payment_terms SET due_date = 'soon'")


def test_add_months_clamps_to_month_end():
    assert pay.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert pay.add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert pay.add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)


# --- trust rules -----------------------------------------------------------------------------


def test_older_sheet_never_replaces_a_current_statement(db):
    with connect() as conn:
        terms(conn, min_payment_cents=42700, due_date="2026-10-31", apr="19.49%", as_of="2026-09-30")
        res = pay.merge_terms(conn, "card1", {"min_payment_cents": 50000, "due_date": "2026-11-01", "apr": "19.49%", "autopay": "yes"},
                              source="sheet", as_of="2026-10-02")
        assert res["action"] == "updated" and res["filled"] == ["autopay"]  # only the blank was filled
        assert len(res["conflicts"]) == 2 and "kept 2026-10-31" in res["conflicts"][1]
        row = pay.get_terms(conn, "card1")
        assert (row["min_payment_cents"], row["due_date"], row["source"], row["autopay"]) == (42700, "2026-10-31", "statement", "yes")


def test_sheet_wins_once_the_statement_cycle_is_over_and_equal_apr_text_is_kept(db):
    with connect() as conn:
        terms(conn, min_payment_cents=42700, due_date="2026-10-01", apr="33.24% revolving; 25.49% protected", as_of="2026-09-05")
        same = pay.merge_terms(conn, "card1", {"min_payment_cents": 42700, "due_date": "2026-10-01", "apr": "33.24%"}, source="sheet", as_of="2026-10-02")
        assert same["action"] == "unchanged" and pay.get_terms(conn, "card1")["source"] == "statement"
        newer = pay.merge_terms(conn, "card1", {"min_payment_cents": 45000, "due_date": "2026-11-01"}, source="sheet", as_of="2026-10-02")
        assert newer["action"] == "updated" and "min_payment_cents" in newer["changed"]
        row = pay.get_terms(conn, "card1")
        assert (row["source"], row["min_payment_cents"], row["apr"]) == ("sheet", 45000, "33.24% revolving; 25.49% protected")


def test_manual_entries_are_only_replaced_by_manual(db):
    with connect() as conn:
        pay.set_manual(conn, "card1", {"min_payment_cents": 9900, "due_date": "2026-10-20"}, as_of="2026-09-01")
        res = pay.merge_terms(conn, "card1", {"min_payment_cents": 1, "due_date": "2026-12-01"}, source="statement", as_of="2026-12-01")
        assert res["action"] in ("kept", "unchanged") or not res["changed"]
        assert pay.get_terms(conn, "card1")["due_date"] == "2026-10-20"
        pay.set_manual(conn, "card1", {"due_date": "2026-10-22"})
        assert pay.get_terms(conn, "card1")["due_date"] == "2026-10-22"


def test_inferred_data_is_replaced_by_the_sheet_and_validation(db):
    with connect() as conn:
        terms(conn, source=pay.INFERRED_SOURCE, as_of="2026-09-01", estimated=1)
        pay.merge_terms(conn, "card1", {"min_payment_cents": 12300, "due_date": "2026-10-16", "estimated": 0}, source="sheet", as_of="2026-09-02")
        row = pay.get_terms(conn, "card1")
        assert (row["source"], row["min_payment_cents"], row["estimated"]) == ("sheet", 12300, 0)
        with pytest.raises(HpgbooksError):
            pay.merge_terms(conn, "card1", {"due_date": "10/16/2026"}, source="sheet")
        with pytest.raises(HpgbooksError):
            pay.merge_terms(conn, "card1", {"autopay": "maybe"}, source="sheet")
        with pytest.raises(HpgbooksError):
            pay.set_manual(conn, "bank1", {"min_payment_cents": 1})  # not a card or loan


# --- rolling and status -----------------------------------------------------------------------


def test_statuses_overdue_due_soon_upcoming_none(db):
    with connect() as conn:
        terms(conn, due_date="2026-10-01")
        assert pay.evaluate(conn, "card1", pay.get_terms(conn, "card1"), TODAY)["status"] == "overdue"
        terms(conn, due_date="2026-10-10", as_of="2026-10-02")
        state = pay.evaluate(conn, "card1", pay.get_terms(conn, "card1"), TODAY)
        assert (state["status"], state["days_until"]) == ("due_soon", 6)
        terms(conn, due_date="2026-10-12", as_of="2026-10-03")
        assert pay.evaluate(conn, "card1", pay.get_terms(conn, "card1"), TODAY)["status"] == "upcoming"
        zero = pay.merge_terms(conn, "card1", {"min_payment_cents": 0, "due_date": "2026-10-12"}, source="statement", as_of="2026-10-04")
        assert zero["action"] == "updated"
        assert pay.evaluate(conn, "card1", pay.get_terms(conn, "card1"), TODAY)["status"] == "none"
        assert pay.evaluate(conn, "loan1", None, TODAY)["status"] == "unknown"


def test_a_payment_after_the_due_date_rolls_to_next_month_estimated(db):
    with connect() as conn:
        terms(conn, due_date="2026-09-28", min_payment_cents=10000, as_of="2026-09-01")
        paid(conn, "p0", "2026-08-30", 10000)  # belongs to the previous cycle: before as_of
        state = pay.evaluate(conn, "card1", pay.get_terms(conn, "card1"), TODAY)
        assert state["status"] == "overdue" and not state["rolled"]
        classify_all(conn)
        paid(conn, "p1", "2026-09-27", 10000)
        state = pay.evaluate(conn, "card1", pay.get_terms(conn, "card1"), TODAY)
        assert (state["status"], state["effective_due_date"], state["rolled"], state["estimated"], state["paid_date"]) == ("upcoming", "2026-10-28", True, True, "2026-09-27")


def test_small_payment_does_not_satisfy_the_minimum_and_payments_are_not_reused(db):
    with connect() as conn:
        terms(conn, due_date="2026-08-10", min_payment_cents=10000, as_of="2026-07-01")
        paid(conn, "p1", "2026-08-09", 4000)
        assert pay.evaluate(conn, "card1", pay.get_terms(conn, "card1"), TODAY)["status"] == "overdue"
        paid(conn, "p2", "2026-08-09", 6000, "AUTOPAY PMT")
        paid(conn, "p3", "2026-09-10", 10000)
        state = pay.evaluate(conn, "card1", pay.get_terms(conn, "card1"), TODAY)
        assert (state["effective_due_date"], state["status"]) == ("2026-10-10", "due_soon")  # Aug and Sep both paid, Oct not due yet
        add_txn(conn, "p4", "card1", "2026-09-12", 10000, "refund of something")  # not a payment
        assert pay.evaluate(conn, "card1", pay.get_terms(conn, "card1"), TODAY)["effective_due_date"] == "2026-10-10"


def test_missed_cycle_after_a_paid_one_is_overdue(db):
    with connect() as conn:
        terms(conn, due_date="2026-08-10", min_payment_cents=10000, as_of="2026-07-01")
        paid(conn, "p1", "2026-08-09", 10000)
        state = pay.evaluate(conn, "card1", pay.get_terms(conn, "card1"), TODAY)
        assert state["status"] == "overdue" and state["effective_due_date"] == "2026-09-10" and state["rolled"]


def test_payment_before_the_due_date_marks_it_paid(db):
    with connect() as conn:
        terms(conn, due_date="2026-10-20", as_of="2026-09-25")
        paid(conn, "early", "2026-09-30", 10000)
        state = pay.evaluate(conn, "card1", pay.get_terms(conn, "card1"), TODAY)
        assert (state["status"], state["paid_date"], state["effective_due_date"]) == ("paid", "2026-09-30", "2026-10-20")
        paid(conn, "old", "2026-09-20", 99999)  # before the figures were taken: ignored
        terms(conn, due_date="2026-10-21", as_of="2026-09-25")
        assert pay.evaluate(conn, "card1", pay.get_terms(conn, "card1"), TODAY)["status"] == "paid"


def test_balance_drop_counts_as_paid_for_statement_cards(db):
    with connect() as conn:
        terms(conn, due_date="2026-10-01", min_payment_cents=42700, statement_balance_cents=969619, as_of="2026-09-05")
        row = pay.get_terms(conn, "card1")
        assert pay.evaluate(conn, "card1", row, TODAY, owed_cents=969619)["status"] == "overdue"
        state = pay.evaluate(conn, "card1", row, TODAY, owed_cents=926919)
        assert (state["status"], state["paid_by"], state["effective_due_date"]) == ("upcoming", "balance", "2026-11-01")


def test_mark_paid_and_a_new_due_date_clears_it(db):
    with connect() as conn:
        terms(conn, due_date="2026-10-01", as_of="2026-09-05")
        pay.mark_paid(conn, "card1", "2026-10-02")
        state = pay.evaluate(conn, "card1", pay.get_terms(conn, "card1"), TODAY)
        assert state["rolled"] and state["effective_due_date"] == "2026-11-01"
        terms(conn, due_date="2026-11-02", as_of="2026-10-03")
        assert pay.get_terms(conn, "card1")["paid_on"] is None
        with pytest.raises(HpgbooksError):
            pay.mark_paid(conn, "loan1", "2026-10-02")


def test_estimated_dates_get_a_short_grace_period(db):
    with connect() as conn:
        pay.merge_terms(conn, "loan1", {"min_payment_cents": 50000, "due_date": "2026-10-02", "estimated": 1}, source=pay.INFERRED_SOURCE, as_of="2026-09-02")
        assert pay.evaluate(conn, "loan1", pay.get_terms(conn, "loan1"), TODAY)["status"] == "due_soon"
        assert pay.evaluate(conn, "loan1", pay.get_terms(conn, "loan1"), "2026-10-06")["status"] == "overdue"


def test_loan_paid_from_a_bank_account_is_found_by_pattern(db):
    with connect() as conn:
        pay.merge_terms(conn, "loan1", {"min_payment_cents": 50000, "due_date": "2026-09-15", "payer_pattern": r"ACME AUTO.*PMT"}, source="manual", as_of="2026-09-01")
        add_txn(conn, "b1", "bank1", "2026-09-14", -50000, "ACME AUTO FIN DES:PMT ID:1")
        add_txn(conn, "b2", "bank1", "2026-09-14", -777, "OTHER STORE")
        state = pay.evaluate(conn, "loan1", pay.get_terms(conn, "loan1"), TODAY)
        assert (state["status"], state["effective_due_date"]) == ("upcoming", "2026-10-15")
        with pytest.raises(HpgbooksError):
            pay.merge_terms(conn, "loan1", {"payer_pattern": "(unclosed"}, source="manual")


def test_paired_payment_is_not_counted_twice(db):
    with connect() as conn:
        pay.merge_terms(conn, "loan1", {"min_payment_cents": 50000, "due_date": "2026-09-15", "payer_pattern": r"ACME"}, source="manual", as_of="2026-09-01")
        add_txn(conn, "l1", "loan1", "2026-09-14", 50000, "Payment")
        add_txn(conn, "b1", "bank1", "2026-09-14", -50000, "ACME AUTO PMT")
        conn.execute("INSERT INTO p_classifications (txn_id, category_id, source, confidence, transfer_pair, updated_at) VALUES ('b1', NULL, 'transfer', 0.95, 'l1', 'x')")
        found = pay.find_payments(conn, "loan1", pay.get_terms(conn, "loan1"), "2026-09-01", TODAY)
        assert [p["id"] for p in found] == ["l1"]
        # the bank leg dated after the window start must not count when its pair is out of the window
        later = pay.find_payments(conn, "loan1", pay.get_terms(conn, "loan1"), "2026-09-15", TODAY)
        assert later == []


# --- inference ---------------------------------------------------------------------------------


def test_infer_schedule_uses_median_amount_and_day():
    pays = [{"date": d, "cents": c, "name": "x"} for d, c in
            [("2026-06-05", 100000), ("2026-07-06", 104125), ("2026-08-05", 104125), ("2026-09-08", 104125)]]
    out = pay.infer_schedule(pays, today=TODAY)
    assert (out["min_payment_cents"], out["due_date"], out["last_paid"]) == (104125, "2026-10-05", "2026-09-08")
    assert pay.infer_schedule(pays[:2], today=TODAY) is None
    late = pay.infer_schedule([{"date": d, "cents": 7, "name": "x"} for d in ("2026-07-31", "2026-08-31", "2026-09-30")], today=TODAY)
    assert late["due_date"] == "2026-10-31"


def test_infer_schedule_reads_the_servicers_applied_to_label():
    pays = [{"date": f"2026-0{m}-28", "cents": 209910, "name": f"Payment Applied to {mon}-01-26"} for m, mon in ((6, "JUL"), (7, "AUG"), (8, "SEP"), (9, "OCT"))]
    assert pay.infer_schedule(pays, today=TODAY)["due_date"] == "2026-11-01"


def _payer_hints(*pairs):
    from hpgbooks import config as hpconfig

    hpconfig.override(payer_hints=tuple(hpconfig.PayerHint(match=m, pattern=p) for m, p in pairs))


def test_infer_loans_from_bank_debits_and_never_overwrites_better_data(db):
    _payer_hints(("auto loan", "WIDGET AUTO FIN|WIDGET AUTO PMT"))
    with connect() as conn:
        for i, d in enumerate(("2026-06-12", "2026-07-11", "2026-08-12", "2026-09-11")):
            add_txn(conn, f"b{i}", "bank1", d, -94008, "WIDGET AUTO FIN DES:PAYMENT")
        rows = pay.infer_loans(conn, today=TODAY)
        assert rows[0]["schedule"]["due_date"] == "2026-10-11" and rows[0]["action"] == "created"
        row = pay.get_terms(conn, "loan1")
        assert (row["source"], row["estimated"], row["min_payment_cents"], row["payer_pattern"]) == (pay.INFERRED_SOURCE, 1, 94008, "WIDGET AUTO FIN|WIDGET AUTO PMT")
        assert pay.infer_loans(conn, today=TODAY)[0]["action"] == "unchanged"
        pay.set_manual(conn, "loan1", {"min_payment_cents": 95000, "due_date": "2026-10-13"})
        pay.infer_loans(conn, today=TODAY)
        assert pay.get_terms(conn, "loan1")["min_payment_cents"] == 95000


def test_payer_hints_come_from_the_config(db):
    from hpgbooks import config as hpconfig

    assert hpconfig.build({}).payer_hints == ()
    cfg = hpconfig.build({"payments": {"payer_hints": [{"match": "5555", "pattern": "SAMPLE LENDER"}]}})
    assert cfg.payer_hints == (hpconfig.PayerHint(match="5555", pattern="SAMPLE LENDER"),)
    for bad in ({"payer_hints": {"match": "x"}}, {"payer_hints": [{"match": "5555"}]}):
        with pytest.raises(hpconfig.ConfigError):
            hpconfig.build({"payments": bad})
    with connect() as conn:
        for i, d in enumerate(("2026-07-03", "2026-08-03", "2026-09-03")):
            add_txn(conn, f"s{i}", "bank1", d, -30000, "SAMPLE LENDER DES:LOAN PMT")
        # No hint configured: the bank debits are not tied to the loan.
        assert pay.infer_loans(conn, today=TODAY)[0]["action"] == "no payment history"
        # A hint matched by the loan's last 4.
        hpconfig.override(payer_hints=cfg.payer_hints)
        rows = pay.infer_loans(conn, today=TODAY)
        assert rows[0]["action"] == "created" and rows[0]["schedule"]["min_payment_cents"] == 30000
        assert pay.get_terms(conn, "loan1")["payer_pattern"] == "SAMPLE LENDER"


# --- statements and feed --------------------------------------------------------------------------


def test_cfna_statement_payment_info():
    text = """
 MINIMUM PAYMENT DUE
 NEW BALANCE   $9,696.19
 PAYMENT DUE DATE     10/01/2026
 MINIMUM PAYMENT DUE   $427.00
 INTEREST CHARGE CALCULATION
   R evolving     33.240% (v)(d) (ADB)   $6,015.24    $169.81
   PROTECTED BALANCE   25.490% (v)(m) (ADB)  $3,611.02   $76.69
"""
    info = cfna.parse_payment_info(text, "2026-09-05")
    assert info == {"due_date": "2026-10-01", "min_payment_cents": 42700, "statement_balance_cents": 969619,
                    "apr": "33.24% revolving; 25.49% protected", "as_of": "2026-09-05"}
    assert cfna.parse_payment_info("nothing here") == {}


def test_applecard_statement_payment_info():
    text = """
Your September Balance      Minimum      Payment
as of Sep 30, 2026          Payment Due  Due By

$16,479.33                  $427.00      Oct 31, 2026

Annual Percentage Rate (APR) 19.49 % (variable)
"""
    assert applecard.parse_payment_info(text) == {"statement_balance_cents": 1647933, "min_payment_cents": 42700, "due_date": "2026-10-31",
                                                  "apr": "19.49%", "as_of": "2026-09-30"}


def test_ingest_latest_statement_picks_the_newest(db):
    class S:
        def __init__(self, payment):
            self.payment = payment

    older = S({"due_date": "2026-09-01", "min_payment_cents": 1, "as_of": "2026-08-05"})
    newer = S({"due_date": "2026-10-01", "min_payment_cents": 42700, "as_of": "2026-09-05", "apr": "33.24%"})
    with connect() as conn:
        assert "created" in pay.ingest_latest_statement(conn, "card1", [newer, older])
        row = pay.get_terms(conn, "card1")
        assert (row["due_date"], row["source"], row["as_of"], row["estimated"]) == ("2026-10-01", "statement", "2026-09-05", 0)
        assert "no statement" in pay.ingest_latest_statement(conn, "card1", [S({})])


def test_finance_feed_fields_are_stored_when_present(db):
    items = [{"id": "card1", "next_payment_due_date": "2026-10-20", "minimum_payment_amount": "35.00", "interest_rate_percentage": "24.99"},
             {"id": "card1x", "minimum_payment_amount": "5"}, {"id": "loan1"}]
    with connect() as conn:
        done = pay.ingest_finance_items(conn, items, as_of=TODAY)
        assert [d["id"] for d in done] == ["card1"]
        row = pay.get_terms(conn, "card1")
        assert (row["min_payment_cents"], row["due_date"], row["apr"], row["source"]) == (3500, "2026-10-20", "24.99%", "finance")


# --- page data --------------------------------------------------------------------------------------


def test_build_lists_cards_and_loans_by_mode_with_summary(db):
    with connect() as conn:
        anchor(conn, "card1", "2026-10-01", 123400)
        terms(conn, due_date="2026-10-10", min_payment_cents=2500, as_of="2026-10-01")
        pay.merge_terms(conn, "loan1", {"min_payment_cents": 50000, "due_date": "2026-09-30"}, source="manual", as_of="2026-09-01")
        terms(conn, "biz1", due_date="2026-10-12", min_payment_cents=1500, as_of="2026-10-01")
        personal = pay.build(conn, "personal", TODAY)
        assert [r["id"] for r in personal["rows"]] == ["loan1", "card1"]  # soonest first, overdue loan on top
        assert personal["rows"][0]["status"] == "overdue"
        s = personal["summary"]
        assert (s["next30_cents"], s["next30_count"], s["overdue_cents"], s["overdue_count"]) == (2500, 1, 50000, 1)
        assert s["by_date"] == [{"date": "2026-10-10", "cents": 2500, "count": 1}]
        business = pay.build(conn, "business", TODAY)
        assert "biz1" in [r["id"] for r in business["rows"]] and "card1" not in [r["id"] for r in business["rows"]] and business["summary"]["next30_cents"] == 1500
        with pytest.raises(HpgbooksError):
            pay.build(conn, "excluded", TODAY)


def test_missing_and_unverified_counts(db):
    with connect() as conn:
        anchor(conn, "card1", "2026-10-01", 5000)
        pay.merge_terms(conn, "loan1", {"due_date": "2026-10-20", "unverified": True}, source="sheet", as_of=TODAY)
        s = pay.build(conn, "personal", TODAY)["summary"]
        assert s["unverified_count"] == 1 and s["missing_count"] == 1 and s["next30_cents"] == 0


# --- CLI ------------------------------------------------------------------------------------------------


def test_cli_set_payment_and_listing(db):
    code, out, err = run_cli(["accounts", "set-payment", "4444", "--min", "224", "--due", "2026-10-01", "--apr", "29.64", "--autopay", "yes"])
    assert code == 0, err
    assert "created" in out
    code, out, err = run_cli(["accounts", "payments", "--mode", "personal"])
    assert code == 0 and "Fake Card" in out and "224.00" in out and "29.64%" in out and "overdue" in out or "due_soon" in out or "upcoming" in out
    code, out, _ = run_cli(["accounts", "payments", "--json"])
    data = json.loads(out)
    assert data["rows"][0]["min_payment_cents"] == 22400 and data["rows"][0]["autopay"] == "yes"
    assert run_cli(["accounts", "set-payment", "4444", "--paid", "2026-10-02"])[0] == 0
    assert run_cli(["accounts", "set-payment", "4444"])[0] != 0
    assert run_cli(["accounts", "set-payment", "1111", "--min", "5"])[0] != 0  # a checking account
    assert run_cli(["accounts", "set-payment", "4444", "--due", "tomorrow"])[0] != 0


def test_cli_infer_payments_dry_run_writes_nothing(db):
    code, out, err = run_cli(["accounts", "infer-payments", "--dry-run"])
    assert code == 0, err
    with connect(readonly=True) as conn:
        assert pay.get_terms(conn, "loan1") is None


# --- API ------------------------------------------------------------------------------------------------------


@pytest.fixture()
def client(db):
    from hpgbooks.web import create_app

    return create_app().test_client()


def _token(client):
    return client.get("/api/session").get_json()["csrf_token"]


def test_api_lists_and_edits_in_either_mode(client):
    assert client.get("/api/payments?mode=bogus").status_code == 400
    body = client.get("/api/payments?mode=personal").get_json()
    assert {r["id"] for r in body["rows"]} == {"card1", "loan1"}
    assert "biz1" in [r["id"] for r in client.get("/api/payments?mode=business").get_json()["rows"]]
    token = _token(client)
    headers = {"X-CSRF-Token": token}
    assert client.patch("/api/payments/card1?mode=personal", json={"min_payment": "50"}).status_code == 403  # no CSRF token
    res = client.patch("/api/payments/card1?mode=personal", json={"min_payment": "224.50", "due_date": "2026-10-20", "apr": "29.64", "autopay": "no", "notes": "n"}, headers=headers)
    assert res.status_code == 200, res.get_json()
    row = res.get_json()["row"]
    assert (row["min_payment_cents"], row["due_date"], row["apr"], row["autopay"], row["source"]) == (22450, "2026-10-20", "29.64%", "no", "manual")
    assert "summary" in res.get_json()
    wrong_mode = client.patch("/api/payments/card1?mode=business", json={"min_payment": "1"}, headers=headers)
    assert wrong_mode.status_code == 404
    assert client.patch("/api/payments/biz1?mode=business", json={"min_payment": "15", "due_date": "2026-10-12"}, headers=headers).status_code == 200
    assert client.patch("/api/payments/bank1?mode=personal", json={"min_payment": "15"}, headers=headers).status_code == 400
    assert client.patch("/api/payments/card1?mode=personal", json={"min_payment": "-5"}, headers=headers).status_code == 400
    assert client.patch("/api/payments/card1?mode=personal", json={"bogus": 1}, headers=headers).status_code == 400
    assert client.patch("/api/payments/loan1?mode=personal", json={"paid": True}, headers=headers).status_code == 400  # no terms yet
    done = client.patch("/api/payments/card1?mode=personal", json={"paid": True, "paid_on": "2026-09-28"}, headers=headers)
    assert done.status_code == 200 and done.get_json()["row"]["status"] == "paid"


# --- sheet sync script ---------------------------------------------------------------------------------------------


def load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "sync_payment_sheet.py"
    spec = importlib.util.spec_from_file_location("sync_payment_sheet", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sheet_sync_matches_by_last4_and_name_and_logs_conflicts(db, tmp_path):
    script = load_script()
    rows = [
        {"account": "Fake Card", "last4": "4444", "balance": "100.00", "min": "35.00", "due": "2026-10-20", "apr": "24.99%", "autopay": "yes"},
        {"account": "Auto Loan", "min": "500", "due": "2026-10-25"},
        {"account": "Fake Biz Card", "last4": "9999", "min": "", "due": "2026-10-20", "unverified": "yes"},
        {"account": "Someone Else's Card", "last4": "3197", "min": "9"},
        {"account": "Skipped", "last4": "4444", "skip": "yes"},
    ]
    with connect() as conn:
        anchor(conn, "card1", "2026-10-01", 20000)
        pay.merge_terms(conn, "loan1", {"min_payment_cents": 45000, "due_date": "2026-10-26"}, source="statement", as_of="2026-10-03")
        report = script.sync(conn, rows, as_of="2026-10-02")
        assert report["unmatched"] == ["Someone Else's Card 3197"] and report["skipped"] == ["Skipped 4444"]
        card = pay.get_terms(conn, "card1")
        assert (card["source"], card["as_of"], card["min_payment_cents"], card["autopay"], card["apr"]) == ("sheet", "2026-10-02", 3500, "yes", "24.99%")
        loan = pay.get_terms(conn, "loan1")
        assert (loan["source"], loan["min_payment_cents"], loan["due_date"]) == ("statement", 45000, "2026-10-26")  # newer statement kept
        assert any("kept 2026-10-26" in line for line in report["conflicts"])
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'payment_sync_conflict'").fetchone()[0] == len(report["conflicts"])
        assert report["balance_diffs"] == ["Fake Card: sheet 100.00 vs books 200.00 (sheet - books -100.00)"]
        biz = pay.get_terms(conn, "biz1")
        assert biz["min_payment_cents"] is None and biz["unverified"] == 1 and biz["due_date"] == "2026-10-20"
        again = script.sync(conn, rows, as_of="2026-10-02")
        assert not again["updated"]


def test_sheet_sync_script_reads_json_and_csv(db, tmp_path, capsys):
    script = load_script()
    js = tmp_path / "rows.json"
    js.write_text(json.dumps([{"account": "Fake Card", "last4": "4444", "min": "10", "due": "2026-10-20"}]))
    assert script.main([str(js), "--as-of", "2026-10-02", "--dry-run"]) == 0
    with connect(readonly=True) as conn:
        assert pay.get_terms(conn, "card1") is None
    cs = tmp_path / "rows.csv"
    cs.write_text("Account,Last4,Min,Due,APR\nFake Card,4444,10,2026-10-20,19%\n")
    assert script.main([str(cs), "--as-of", "2026-10-02"]) == 0
    with connect(readonly=True) as conn:
        assert pay.get_terms(conn, "card1")["apr"] == "19%"
    assert script.main([str(tmp_path / "missing.json")]) == 1
    capsys.readouterr()
