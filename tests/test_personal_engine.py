"""Personal engine: schema, rules, transfers, recurring, budgets, net worth, cash flow,
spending, goals, monthly summary, owner draws, and the importer. Fake data only."""

from __future__ import annotations

from datetime import date

import pytest

import hpgbooks.db as hpdb
from hpgbooks.db import HpgbooksError, _apply_migrations, connect
from fake_accounts import BANK
from hpgbooks.personal import actions
from hpgbooks.personal import analytics as pa
from hpgbooks.personal import recurring as rec
from hpgbooks.personal.classify import load_personal, needs_review, reclassify_personal
from hpgbooks.personal.merchants import merchant_key
from hpgbooks.personal.seed import seed_personal
from personal_helpers import add_account, add_txn, anchor, business_class, cat_id, classify_all, make_db

CHK, SAV, CARD, LOAN, BROKER = "fake-chk", "fake-sav", "fake-card", "fake-loan", "fake-broker"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    with connect() as conn:
        add_account(conn, CHK, "cash", last4="1001")
        add_account(conn, SAV, "cash", last4="1002")
        add_account(conn, CARD, "liability", last4="1003")
        add_account(conn, LOAN, "loan", last4="1004")
        add_account(conn, BROKER, "investment", last4="1005")
    return tmp_path


def _row(conn, txn_id):
    return next(row for row in load_personal(conn) if row["id"] == txn_id)


# --- schema --------------------------------------------------------------------


def test_personal_tables_on_fresh_and_existing_db(db):
    with connect() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"p_categories", "p_rules", "p_classifications", "p_splits", "p_merchants", "p_tags", "p_txn_tags",
                "p_budgets", "p_goals", "p_recurring", "p_transfer_reviews"} <= tables
        cats = conn.execute("SELECT COUNT(*) FROM p_categories").fetchone()[0]
        rules = conn.execute("SELECT COUNT(*) FROM p_rules").fetchone()[0]
        assert cats > 50 and rules > 30
        assert seed_personal(conn) == (0, rules)  # idempotent
        assert conn.execute("SELECT COUNT(*) FROM p_categories").fetchone()[0] == cats
        conn.execute("DELETE FROM schema_version WHERE version = 9")
        _apply_migrations(conn)  # IF NOT EXISTS: a rerun changes nothing
        assert conn.execute("SELECT COUNT(*) FROM p_rules").fetchone()[0] == rules
        system = {row["name"] for row in conn.execute("SELECT name FROM p_categories WHERE is_system = 1")}
        assert system == {"Owner draws", "Uncategorized"}
        assert conn.execute("SELECT kind FROM p_categories WHERE name = 'Owner draws'").fetchone()[0] == "funding"


# --- classification ---------------------------------------------------------------


def test_starter_rules_and_manual_wins(db):
    with connect() as conn:
        add_txn(conn, "t-netflix", CARD, "2026-09-08", -1799, "NETFLIX.COM", merchant="Netflix")
        add_txn(conn, "t-kroger", CARD, "2026-09-09", -8800, "KROGER #0451")
        add_txn(conn, "t-pay", CHK, "2026-09-05", 300000, "ACME DES:PAYROLL DIRECT DEP")
        add_txn(conn, "t-shell", CARD, "2026-09-10", -4000, "SHELL OIL 5744")
        add_txn(conn, "t-odd", CARD, "2026-09-11", -1234, "ZZQ LOCAL SHOP")
        add_txn(conn, "t-venmo", CHK, "2026-09-12", -5000, "VENMO PAYMENT 1234")
        classify_all(conn)
        assert _row(conn, "t-netflix")["group"] == "Subscriptions"
        assert _row(conn, "t-kroger")["category"] == "Groceries"
        assert _row(conn, "t-pay")["category"] == "Paycheck"
        assert _row(conn, "t-shell")["category"] == "Gas & fuel"
        assert needs_review(_row(conn, "t-odd")) and needs_review(_row(conn, "t-venmo"))
        dining = cat_id(conn, "Food", "Dining out")
        actions.categorize(conn, "t-kroger", dining, "lunch", actor="test")
        reclassify_personal(conn)
        assert _row(conn, "t-kroger")["category"] == "Dining out"
        assert _row(conn, "t-kroger")["note"] == "lunch"
        audit = conn.execute("SELECT action FROM audit_log WHERE txn_id = 't-kroger'").fetchall()
        assert [row[0] for row in audit] == ["personal_classify"]
        with pytest.raises(HpgbooksError):
            actions.categorize(conn, "biz:anything", dining, actor="test")


def test_business_rows_cannot_be_categorized_from_personal(db):
    with connect() as conn:
        add_txn(conn, "b-1", BANK, "2026-09-01", -1000, "BUSINESS THING")
        with pytest.raises(HpgbooksError):
            actions.categorize(conn, "b-1", cat_id(conn, "Food", "Groceries"), actor="test")
        with pytest.raises(HpgbooksError):
            actions.set_note(conn, "b-1", "x", actor="test")


def test_merchant_keys():
    assert merchant_key("", "AMZN Mktp US*2K4AB12C3") == "AMZN MKTP US"
    assert merchant_key("", "SQ *BLUE BOTTLE COFFEE") == "BLUE BOTTLE COFFEE"
    assert merchant_key("", "KROGER #0451 CHARLOTTE NC") == "KROGER"
    assert merchant_key("", "FAKE AUTO FINANCE DES:AUTO PMT ID:99") == "FAKE AUTO FINANCE"
    assert merchant_key("Netflix", "NETFLIX.COM 866") == "NETFLIX"


def test_splits_must_sum_to_the_amount(db):
    with connect() as conn:
        add_txn(conn, "t-costco", CARD, "2026-09-03", -15000, "COSTCO WHSE #0123")
        classify_all(conn)
        groceries, home = cat_id(conn, "Food", "Groceries"), cat_id(conn, "Shopping", "Home goods")
        with pytest.raises(HpgbooksError):
            actions.set_splits(conn, "t-costco", [{"category_id": groceries, "amount_cents": -10000}, {"category_id": home, "amount_cents": -4000}], actor="test")
        with pytest.raises(HpgbooksError):
            actions.set_splits(conn, "t-costco", [{"category_id": groceries, "amount_cents": -15000}], actor="test")
        actions.set_splits(conn, "t-costco", [{"category_id": groceries, "amount_cents": -10000}, {"category_id": home, "amount_cents": -5000}], actor="test")
        t = pa.totals([_row(conn, "t-costco")], pa.categories(conn))
        by = {item["category"]: item["cents"] for item in t["by_category"]}
        assert by == {"Groceries": 10000, "Home goods": 5000}
        actions.set_splits(conn, "t-costco", [], actor="test")
        assert _row(conn, "t-costco")["splits"] == []


def test_tags_and_notes_are_searchable(db):
    from hpgbooks.personal.queries import personal_search, query

    with connect() as conn:
        add_txn(conn, "t-gift", CARD, "2026-09-03", -4500, "TARGET 00012345")
        classify_all(conn)
        actions.set_tags(conn, "t-gift", ["birthday", "kids"], actor="test")
        actions.set_note(conn, "t-gift", "present for Sam", actor="test")
        assert query(conn, tag="birthday")["total"] == 1
        assert query(conn, search="present for")["total"] == 1
        assert query(conn, search="45.00")["total"] == 1
        assert personal_search(conn, "birthday")["transactions"][0]["id"] == "t-gift"
        with pytest.raises(HpgbooksError):
            actions.set_tags(conn, "t-gift", ["<script>"], actor="test")


# --- transfers ------------------------------------------------------------------


def test_card_payment_is_a_transfer_not_spending(db):
    with connect() as conn:
        add_txn(conn, "c-buy", CARD, "2026-09-02", -12000, "KROGER #1")
        add_txn(conn, "c-pay-out", CHK, "2026-09-20", -12000, "FAKE CARD CO DES:CARD PAYMENT")
        add_txn(conn, "c-pay-in", CARD, "2026-09-21", 12000, "PAYMENT THANK YOU")
        add_txn(conn, "s-out", CHK, "2026-09-05", -50000, "ONLINE TRANSFER 77")
        add_txn(conn, "s-in", SAV, "2026-09-06", 50000, "ONLINE TRANSFER 77")
        classify_all(conn)
        assert _row(conn, "c-pay-out")["kind"] == "transfer" and _row(conn, "c-pay-in")["kind"] == "transfer"
        assert _row(conn, "c-pay-out")["category"] == "Credit card payment"
        assert _row(conn, "s-out")["category"] == "Internal transfer" and _row(conn, "s-in")["transfer_pair"] == "s-out"
        t = pa.totals(load_personal(conn, "2026-09-01", "2026-09-30"), pa.categories(conn))
        assert t["spending_cents"] == 12000 and t["income_cents"] == 0 and t["transfers_net_cents"] == 0


def test_ambiguous_pairs_wait_for_confirmation(db):
    from hpgbooks.personal.classify import detect_transfers, set_transfer_decision

    with connect() as conn:
        add_txn(conn, "a-out", CHK, "2026-09-10", -20000, "ONLINE TRANSFER")
        add_txn(conn, "a-in1", SAV, "2026-09-11", 20000, "ONLINE TRANSFER")
        add_txn(conn, "a-in2", BROKER, "2026-09-11", 20000, "ONLINE TRANSFER")
        classify_all(conn)
        found = detect_transfers(conn)
        assert found["pairs"] == [] and len(found["ambiguous"]) == 1
        assert _row(conn, "a-out")["kind"] != "transfer"
        key = found["ambiguous"][0]["key"]
        set_transfer_decision(conn, key, "confirmed", actor="test")
        assert _row(conn, "a-out")["kind"] == "transfer"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'personal_transfer_review'").fetchone()[0] == 1


def test_coincidental_equal_amounts_do_not_pair(db):
    from hpgbooks.personal.classify import detect_transfers

    with connect() as conn:
        add_txn(conn, "x-out", CARD, "2026-09-10", -1799, "NETFLIX.COM")
        add_txn(conn, "x-in", CHK, "2026-09-11", 1799, "REFUND FROM SHOP")
        classify_all(conn)
        assert detect_transfers(conn)["pairs"] == []


# --- owner draws (section 6) ------------------------------------------------------


def _business_draw(conn, txn_id, day, cents, name):
    add_txn(conn, txn_id, BANK, day, cents, name)
    business_class(conn, txn_id, "owner_draw", "Owner Draw")


def test_draw_paired_with_personal_deposit_is_counted_once(db):
    with connect() as conn:
        _business_draw(conn, "d-biz", "2026-09-10", -250000, "Online Banking transfer to CHK 1001")
        add_txn(conn, "d-dep", CHK, "2026-09-11", 250000, "Online Banking transfer from CHK 0101")
        classify_all(conn)
        rows = load_personal(conn, "2026-09-01", "2026-09-30")
        assert [row["id"] for row in rows] == ["d-dep"]
        assert rows[0]["category"] == "Owner draws" and rows[0]["transfer_pair"] == "biz:d-biz"
        t = pa.totals(rows, pa.categories(conn))
        assert t["owner_draws_cents"] == 250000 and t["earned_cents"] == 0


def test_draw_without_personal_leg_is_synthesized_once(db):
    with connect() as conn:
        _business_draw(conn, "d-biz", "2026-09-10", -250000, "Online Banking transfer to CHK 9999")
        classify_all(conn)
        rows = load_personal(conn, "2026-09-01", "2026-09-30")
        assert [(row["id"], row["kind"], row["from_business"], row["editable"]) for row in rows] == [
            ("biz:d-biz", "funding", True, False)
        ]
        assert rows[0]["note"] == "paid from business account"


def test_mortgage_paid_directly_from_business(db):
    with connect() as conn:
        _business_draw(conn, "m-biz", "2026-09-01", -210000, "FAKE MORTGAGE SERVICER DES:MTG PYMT")
        classify_all(conn)
        rows = load_personal(conn, "2026-09-01", "2026-09-30")
        kinds = {(row["kind"], row["category"], row["amount_cents"]) for row in rows}
        assert kinds == {("funding", "Owner draws", 210000), ("expense", "Mortgage", -210000)}
        t = pa.totals(rows, pa.categories(conn))
        assert t["owner_draws_cents"] == 210000 and t["spending_cents"] == 210000 and t["net_cents"] == 0
        with pytest.raises(HpgbooksError):
            actions.categorize(conn, "biz:m-biz", cat_id(conn, "Food", "Groceries"), actor="test")


def test_mortgage_from_personal_after_draw(db):
    with connect() as conn:
        _business_draw(conn, "d-biz", "2026-09-01", -700000, "Online Banking transfer to CHK 1001")
        add_txn(conn, "d-dep", CHK, "2026-09-01", 700000, "TRANSFER FROM BUSINESS")
        add_txn(conn, "m-pay", CHK, "2026-09-02", -700000, "FAKE HOME LOANS DES:MORTGAGE PMT")
        add_txn(conn, "m-loan", LOAN, "2026-09-03", 700000, "PAYMENT RECEIVED - THANK YOU")
        classify_all(conn)
        rows = load_personal(conn, "2026-09-01", "2026-09-30")
        by_id = {row["id"]: row for row in rows}
        assert by_id["d-dep"]["category"] == "Owner draws"
        assert by_id["m-pay"]["category"] == "Mortgage" and by_id["m-pay"]["kind"] == "expense"
        assert by_id["m-loan"]["kind"] == "transfer"
        assert not any(row["from_business"] for row in rows)
        t = pa.totals(rows, pa.categories(conn))
        assert t["owner_draws_cents"] == 700000 and t["spending_cents"] == 700000 and t["earned_cents"] == 0


def test_business_pnl_identical_with_and_without_personal(db):
    from hpgbooks.reports import build_pnl, render_pnl

    with connect() as conn:
        _business_draw(conn, "d-biz", "2026-09-10", -250000, "Online Banking transfer to CHK 1001")
        add_txn(conn, "rev", BANK, "2026-09-03", 500000, "EXAMPLE PAYMENTS DES:FUNDS DISB")
        business_class(conn, "rev", "branda", "Revenue - Hosting")
        before = render_pnl(build_pnl(conn, 2026, by="month"))
        add_txn(conn, "d-dep", CHK, "2026-09-11", 250000, "TRANSFER FROM BUSINESS")
        add_txn(conn, "p-buy", CARD, "2026-09-12", -9999, "KROGER")
        add_txn(conn, "p-pay", CHK, "2026-09-12", -250000, "Online Banking transfer to CHK")
        classify_all(conn)
        assert render_pnl(build_pnl(conn, 2026, by="month")) == before


# --- recurring detector (pure) -----------------------------------------------------


def _series(key, dates_amounts, group="Subscriptions", direction="out"):
    sign = -1 if direction == "out" else 1
    return [
        {"id": f"{key}-{i}", "date": day, "amount_cents": sign * cents, "merchant_key": key, "merchant": key.title(),
         "kind": "expense" if direction == "out" else "income", "group": group, "category": group, "category_id": 1,
         "account_id": "a", "account_label": "A", "pending": False}
        for i, (day, cents) in enumerate(dates_amounts)
    ]


TODAY = date(2026, 9, 30)


def test_detector_monthly_with_drift_and_price_change():
    rows = _series("NETFLIX", [("2026-05-08", 1549), ("2026-06-10", 1549), ("2026-07-07", 1549), ("2026-08-09", 1549), ("2026-09-08", 1799)])
    [item] = rec.detect(rows, TODAY)
    assert item["cadence"] == "monthly" and item["kind"] == "subscription"
    assert item["next_expected"] == "2026-10-08"
    assert item["price_change"] == {"from_cents": 1549, "to_cents": 1799, "change_cents": 250}
    assert item["monthly_cents"] == 1549 and item["annual_cents"] == 1549 * 12
    assert not item["may_be_cancelled"]


def test_detector_needs_three_monthly_and_two_annual():
    assert rec.detect(_series("GYM", [("2026-08-01", 3000), ("2026-09-01", 3000)]), TODAY) == []
    [annual] = rec.detect(_series("CLOUD", [("2025-09-03", 9999), ("2026-09-03", 9999)]), TODAY)
    assert annual["cadence"] == "annual" and annual["next_expected"] == "2027-09-03" and annual["monthly_cents"] == 833


def test_detector_weekly_biweekly_and_income():
    weekly = _series("COFFEE", [(f"2026-09-{d:02d}", 500) for d in (1, 8, 15, 22, 29)], group="Food")
    [item] = rec.detect(weekly, TODAY)
    assert item["cadence"] == "weekly" and item["kind"] == "bill" and item["monthly_cents"] == round(500 * 52 / 12)
    pay = _series("ACME", [("2026-08-07", 400000), ("2026-08-21", 400000), ("2026-09-04", 400000), ("2026-09-18", 400000)], group="Income", direction="in")
    [item] = rec.detect(pay, TODAY)
    assert item["cadence"] == "biweekly" and item["kind"] == "income" and item["next_expected"] == "2026-10-02"


def test_detector_cancelled_hint_and_new_and_duplicates():
    stopped = _series("HULU", [("2026-01-19", 799), ("2026-02-19", 799), ("2026-03-19", 799)])
    [item] = rec.detect(stopped, TODAY)
    assert item["may_be_cancelled"]
    fresh = _series("NEWAPP", [("2026-08-01", 999), ("2026-08-31", 999), ("2026-09-30", 999)])
    [item] = rec.detect(fresh, TODAY)
    assert item["new"]
    two_plans = _series("SPOTIFY", [("2026-07-01", 1199), ("2026-08-01", 1199), ("2026-09-01", 1199)]) + _series(
        "SPOTIFY", [("2026-07-15", 2999), ("2026-08-15", 2999), ("2026-09-15", 2999)]
    )
    for i, row in enumerate(two_plans):
        row["id"] = f"s{i}"
    items = rec.detect(two_plans, TODAY)
    assert len(items) == 2 and all(item["possible_duplicate"] for item in items)


def test_detector_ignores_irregular_and_transfers():
    irregular = _series("RANDOM", [("2026-01-02", 500), ("2026-01-09", 500), ("2026-04-20", 500), ("2026-09-01", 500)])
    assert rec.detect(irregular, TODAY) == []
    transfers = _series("SAVINGS", [("2026-07-05", 500), ("2026-08-05", 500), ("2026-09-05", 500)])
    for row in transfers:
        row["kind"] = "transfer"
    assert rec.detect(transfers, TODAY) == []


def test_recurring_status_and_cadence_overrides(db):
    with connect() as conn:
        for i, day in enumerate(("2026-06-14", "2026-07-14", "2026-08-14", "2026-09-14")):
            add_txn(conn, f"sp{i}", CARD, day, -1199, "SPOTIFY USA", merchant="Spotify")
        classify_all(conn)
        data = pa.recurring(conn)
        [item] = [item for item in data["items"] if item["merchant_key"] == "SPOTIFY"]
        assert data["subscription_monthly_cents"] == 1199
        pa.refresh_recurring(conn)
        actions.update_recurring(conn, item["series_key"], {"status": "cancelled"}, actor="test")
        assert pa.recurring(conn)["subscription_monthly_cents"] == 0
        actions.update_recurring(conn, item["series_key"], {"status": "confirmed", "cadence": "quarterly"}, actor="test")
        [item] = [item for item in pa.recurring(conn)["items"] if item["merchant_key"] == "SPOTIFY"]
        assert item["cadence"] == "quarterly" and item["user_confirmed"] and item["next_expected"] == "2026-12-14"
        with pytest.raises(HpgbooksError):
            actions.update_recurring(conn, item["series_key"], {"status": "gone"}, actor="test")
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'personal_recurring'").fetchone()[0] == 2


# --- budgets ------------------------------------------------------------------


def test_budget_math_alerts_rollover_copy_and_average(db):
    with connect() as conn:
        groceries = cat_id(conn, "Food", "Groceries")
        coffee = cat_id(conn, "Food", "Coffee")
        add_txn(conn, "g1", CARD, "2026-09-03", -40000, "KROGER #1")
        add_txn(conn, "g2", CARD, "2026-09-04", 5000, "KROGER #1 RETURN")
        add_txn(conn, "c1", CARD, "2026-09-05", -900, "STARBUCKS")
        for month, cents in (("06", 30000), ("07", 36000), ("08", 33000)):
            add_txn(conn, f"h{month}", CARD, f"2026-{month}-10", -cents, "KROGER #1")
        classify_all(conn)
        actions.categorize(conn, "g2", groceries, actor="test")  # a refund nets against Groceries
        actions.set_budget(conn, {"category_id": groceries, "amount_cents": 40000}, actor="test")
        actions.set_budget(conn, {"category_id": coffee, "month": "2026-09", "amount_cents": 800, "rollover": True}, actor="test")
        data = pa.budgets(conn, "2026-09")
        rows = {row["name"]: row for row in data["rows"]}
        assert rows["Groceries"]["spent_cents"] == 35000 and rows["Groceries"]["status"] == "warning"
        assert rows["Groceries"]["pct_used"] == 87.5
        assert rows["Coffee"]["status"] == "over" and rows["Coffee"]["carry_cents"] == 0
        assert {alert["name"] for alert in data["alerts"]} == {"Groceries", "Coffee"}
        assert data["budgeted_cents"] == 40800 and data["spent_cents"] == 35900
        # The default budget applies to August: 33,000 spent of 40,000 is 82.5%, a warning.
        assert {row["name"]: (row["status"], row["pct_used"]) for row in pa.budgets(conn, "2026-08")["rows"]} == {
            "Groceries": ("warning", 82.5)
        }
        # Rollover: October coffee budget carries September's -100.
        actions.set_budget(conn, {"category_id": coffee, "amount_cents": 800, "rollover": True}, actor="test")
        october = {row["name"]: row for row in pa.budgets(conn, "2026-10")["rows"]}
        assert october["Coffee"]["carry_cents"] == -100 and october["Coffee"]["available_cents"] == 700
        assert actions.copy_budgets(conn, "2026-10", actor="test") == 2  # both September lines gain October rows
        assert actions.copy_budgets(conn, "2026-10", actor="test") == 0
        suggestions = {item["category"]: item["average_cents"] for item in pa.average_suggestions(conn, "2026-09")}
        assert suggestions["Groceries"] == 33000
        with pytest.raises(HpgbooksError):
            actions.set_budget(conn, {"category_id": cat_id(conn, "Income", "Paycheck"), "amount_cents": 1}, actor="test")
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'personal_budget'").fetchone()[0] >= 4


def test_budget_projected_pace(db):
    with connect() as conn:
        groceries = cat_id(conn, "Food", "Groceries")
        add_txn(conn, "g1", CARD, "2026-09-03", -30000, "KROGER #1")
        classify_all(conn)
        actions.set_budget(conn, {"category_id": groceries, "amount_cents": 50000}, actor="test")
        [row] = pa.budgets(conn, "2026-09")["rows"]
        assert row["projected_cents"] == 30000  # the month is over on 2026-09-30


# --- net worth, accounts, cash flow, spending -------------------------------------------


def test_net_worth_from_anchors_and_history(db):
    with connect() as conn:
        add_txn(conn, "n1", CHK, "2026-09-10", -10000, "KROGER")
        add_txn(conn, "n2", CHK, "2026-09-25", 50000, "ACME DES:PAYROLL")
        anchor(conn, CHK, "2026-09-20", 100000)
        anchor(conn, CARD, "2026-09-29", 30000)
        add_txn(conn, "n3", CARD, "2026-09-30", -2000, "SHELL", pending=True)
        anchor(conn, LOAN, "2026-08-01", 500000)
        conn.execute("UPDATE accounts SET include_in_net_worth = 0 WHERE id = ?", (BROKER,))
        anchor(conn, BROKER, "2026-09-29", 999999)
        classify_all(conn)
        worth = pa.net_worth(conn, "3M")
        rows = {row["id"]: row for row in worth["accounts"]}
        assert rows[CHK]["balance_cents"] == 150000  # anchor + later payroll
        assert rows[CARD]["balance_cents"] == 32000  # owed: anchor + pending charge after it
        assert rows[LOAN]["stale"] and not rows[CHK]["stale"]
        assert not rows[BROKER]["included"]
        assert worth["net_cents"] == 150000 - 32000 - 500000
        history = {point["date"]: point["net_cents"] for point in worth["points"]}
        assert history["2026-09-30"] == worth["net_cents"]
        # Before the checking anchor: walk back the posted payroll-free activity (the -100 on 9/10).
        from hpgbooks.personal.balances import load_series

        series = load_series(conn, {CHK: {"type": "cash"}})[CHK]
        assert series.balance_at("2026-09-05") == 110000
        with pytest.raises(HpgbooksError):
            pa.net_worth(conn, "5Y")
        overview = pa.accounts_overview(conn)
        assert [group["label"] for group in overview["groups"]] == ["Cash", "Credit cards", "Investments", "Loans & mortgages"]
        assert len(overview["groups"][0]["accounts"][0]["spark"]) == 30


def test_cash_flow_and_savings_rate_with_and_without_draws(db):
    with connect() as conn:
        add_txn(conn, "pay", CHK, "2026-09-05", 400000, "ACME DES:PAYROLL")
        add_txn(conn, "int", SAV, "2026-09-28", 1000, "INTEREST PAYMENT")
        _business_draw(conn, "d-biz", "2026-09-10", -100000, "Online Banking transfer to CHK 1001")
        add_txn(conn, "d-dep", CHK, "2026-09-10", 100000, "TRANSFER FROM BUSINESS")
        add_txn(conn, "spend", CARD, "2026-09-12", -201000, "KROGER")
        classify_all(conn)
        flow = pa.cash_flow(conn, "2026-09", 3)
        sept = flow["months"][-1]
        assert sept["sources"] == {"Paycheck": 400000, "Interest & dividends": 1000, "Owner draws": 100000, "Other": 0}
        assert sept["income_cents"] == 501000 and sept["spending_cents"] == 201000
        assert sept["savings_rate"] == round(300000 / 501000 * 100, 1)
        assert sept["savings_rate_without_draws"] == round(200000 / 401000 * 100, 1)
        assert [item["source"] for item in flow["sources"]] == ["Paycheck", "Interest & dividends", "Owner draws", "Other"]


def test_spending_comparisons(db):
    with connect() as conn:
        add_txn(conn, "a", CARD, "2026-09-03", -30000, "KROGER")
        add_txn(conn, "b", CARD, "2026-08-03", -10000, "KROGER")
        add_txn(conn, "c", CARD, "2025-09-03", -20000, "KROGER")
        add_txn(conn, "d", CARD, "2026-09-04", -5000, "SHELL OIL")
        add_txn(conn, "e", CARD, "2026-09-05", 1000, "SHELL OIL REFUND")
        classify_all(conn)
        actions.categorize(conn, "e", cat_id(conn, "Transportation", "Gas & fuel"), actor="test")
        data = pa.spending(conn, "2026-09-01", "2026-09-30")
        assert data["total"]["cents"] == 34000 and data["total"]["prior_cents"] == 10000
        assert data["same_period_last_year_cents"] == 20000
        groups = {group["group"]: group for group in data["groups"]}
        assert groups["Food"]["cents"] == 30000 and groups["Food"]["prior_cents"] == 10000 and groups["Food"]["last_year_cents"] == 20000
        assert groups["Transportation"]["cents"] == 4000  # refund nets
        assert data["merchants"][0]["merchant_key"] == "KROGER" and data["merchants"][0]["average_cents"] == 30000
        assert data["ytd"]["cents"] == 44000 and data["ytd"]["prior_cents"] == 20000
        assert len(data["trend"]) == 12


def test_goals_progress(db):
    with connect() as conn:
        anchor(conn, SAV, "2026-09-29", 500000)
        add_txn(conn, "s1", SAV, "2026-07-01", 100000, "TRANSFER FROM CHECKING")
        g1 = actions.save_goal(conn, {"name": "Emergency fund", "target_cents": 1000000, "target_date": "2027-09-30", "account_id": SAV}, actor="test")
        g2 = actions.save_goal(conn, {"name": "Trip", "target_cents": 300000, "target_date": "2026-12-31", "manual_current_cents": 100000, "monthly_contribution_cents": 100000}, actor="test")
        actions.save_goal(conn, {"name": "Done", "target_cents": 1000, "manual_current_cents": 2000}, actor="test")
        goals = {goal["name"]: goal for goal in pa.goals(conn)}
        assert goals["Emergency fund"]["current_cents"] == 500000 and goals["Emergency fund"]["progress_pct"] == 50.0
        assert goals["Emergency fund"]["months_left"] == 12 and goals["Emergency fund"]["required_monthly_cents"] == 41667
        assert goals["Emergency fund"]["status"] == "behind"
        assert goals["Trip"]["required_monthly_cents"] == 66667 and goals["Trip"]["status"] == "on_track"
        assert goals["Done"]["status"] == "done"
        actions.save_goal(conn, {"archived": True}, g2["id"], actor="test")
        assert "Trip" not in {goal["name"] for goal in pa.goals(conn)}
        with pytest.raises(HpgbooksError):
            actions.save_goal(conn, {"name": "x", "target_cents": 0}, actor="test")
        assert g1["id"]


def test_monthly_summary_and_reconcile(db):
    with connect() as conn:
        for month in ("07", "08", "09"):
            add_txn(conn, f"pay{month}", CHK, f"2026-{month}-05", 400000, "ACME DES:PAYROLL")
            add_txn(conn, f"nf{month}", CARD, f"2026-{month}-08", -1549 if month != "09" else -1799, "NETFLIX.COM", merchant="Netflix")
            add_txn(conn, f"gr{month}", CARD, f"2026-{month}-09", -30000, "KROGER")
        add_txn(conn, "big", CARD, "2026-09-15", -90000, "BEST BUY 123")
        add_txn(conn, "cp-out", CHK, "2026-09-20", -50000, "FAKE CARD DES:CARD PAYMENT")
        add_txn(conn, "cp-in", CARD, "2026-09-21", 50000, "PAYMENT THANK YOU")
        add_txn(conn, "pend", CHK, "2026-09-29", -1500, "STARBUCKS", pending=True)
        _business_draw(conn, "m-biz", "2026-09-01", -210000, "FAKE MORTGAGE SERVICER DES:MTG PYMT")
        classify_all(conn)
        summary = pa.monthly_summary(conn, "2026-09")
        assert summary["spending_cents"] == 1799 + 30000 + 90000 + 1500 + 210000
        assert summary["owner_draws_cents"] == 210000
        assert summary["biggest"][0]["id"] == "biz:m-biz:spend"
        assert summary["changed_subscriptions"] == [{"merchant": "Netflix", "from_cents": 1549, "to_cents": 1799}]
        assert any("owner draws" in line for line in summary["summary"])
        assert summary["category_changes"][0]["category"] == "Mortgage"
        recon = pa.reconcile(conn, "2026-09")
        assert recon["ties"] and recon["difference_cents"] == 0
        assert recon["pending_cents"] == -1500
        assert recon["paid_by_business"] == {"funding_cents": 210000, "spending_cents": 210000, "count": 2}
        assert recon["expected_change_cents"] == 400000 - (1799 + 30000 + 90000 + 1500)


def test_importer_classifies_per_scope(tmp_path, monkeypatch):
    from golden_fixture import run_cli
    from personal_fake import CARD_A, CHECKING, write_inbox

    make_db(tmp_path, monkeypatch)
    day = write_inbox(tmp_path / "inbox")
    code, _out, err = run_cli(["import", str(day)])
    assert code == 0 and "unregistered account" in err  # not registered yet: reported, not created
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    run_cli(["accounts", "discover", str(tmp_path / "inbox" / "finance_list_accounts_2026-09-30.json")])
    code, out, err = run_cli(["import", str(day)])
    assert code == 0 and "rows by scope: personal=" in out
    with connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM classifications c JOIN transactions t ON t.id = c.txn_id WHERE t.account_id = ?", (CARD_A,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM p_classifications c JOIN transactions t ON t.id = c.txn_id WHERE t.account_id = ?", (CHECKING,)
        ).fetchone()[0] > 0
    code, out, _ = run_cli(["import", str(day)])
    assert "inserted=0" in out  # idempotent
