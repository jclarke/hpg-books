"""Bill detection tiers and the confirm / not-recurring review. Invented merchants and amounts only."""

from __future__ import annotations

from datetime import date

import pytest

from hpgbooks.db import HpgbooksError, connect
from hpgbooks.personal import actions
from hpgbooks.personal import analytics as pa
from hpgbooks.personal import recurring as rec
from hpgbooks.personal.merchants import default_display
from personal_helpers import add_account, add_txn, cat_id, classify_all, make_db

TODAY = date(2026, 9, 30)
MONTHS = ("2026-05", "2026-06", "2026-07", "2026-08", "2026-09")


def _series(key, dates_amounts, group="Housing", category="Rent", direction="out", name=""):
    sign = -1 if direction == "out" else 1
    return [
        {"id": f"{key}-{day}-{i}", "date": day, "amount_cents": sign * cents, "merchant_key": key, "merchant": key.title(),
         "name": name or key, "kind": "expense" if direction == "out" else "income", "group": group, "category": category,
         "category_id": 1, "account_id": "a", "account_label": "A", "pending": False}
        for i, (day, cents) in enumerate(dates_amounts)
    ]


def _monthly(key, cents, day=12, **kw):
    return _series(key, [(f"{month}-{day:02d}", cents) for month in MONTHS], **kw)


def _one(rows, decisions=None):
    items = rec.detect(rows, TODAY, decisions=decisions)
    assert len(items) <= 1, items
    return items[0] if items else None


# --- detector tiers (pure) ----------------------------------------------------------


def test_steady_monthly_bill_is_a_bill():
    item = _one(_monthly("ZEBRA PROPERTY GROUP", 123400))
    assert item["tier"] == "bill" and item["kind"] == "bill" and item["review_reasons"] == []
    assert item["cadence"] == "monthly" and item["typical_cents"] == 123400


@pytest.mark.parametrize(
    "rows",
    [
        _monthly("INTEREST CHARGE ON PURCHASES", 4321, group="Fees & interest", category="Fees & interest"),
        _monthly("PLAN FEE - WIDGET PLAN", 250, group="Shopping", category="General"),
        _monthly("FAKEBANK", 999, group="Health", category="Doctor", name="FAKEBANK LATE FEE"),
        _monthly("CASH DIVIDEND OF $0.03", 3, group="Income", category="Interest & dividends", direction="in"),
        _monthly("MONTHLY INTEREST PAID", 2500, group="Income", category="Interest & dividends", direction="in"),
        _monthly("QUUX STORE", 5000, group="Income", category="Refunds & reimbursements", direction="in"),
        _monthly("TINY APP", 49, group="Subscriptions", category="Subscriptions"),
        _monthly("ONLINE TRANSFER TO SAV", 10000, group="Transfers", category="Savings transfer"),
    ],
    ids=["interest", "plan-fee", "late-fee-name", "dividend", "interest-income", "refund", "tiny", "transfer"],
)
def test_interest_fees_dividends_refunds_tiny_and_transfers_are_never_bills(rows):
    assert rec.detect(rows, TODAY) == []


def test_one_off_and_two_monthly_charges_are_not_recurring():
    assert rec.detect(_series("FILM PALACE", [("2026-09-03", 2450)], group="Entertainment", category="Entertainment"), TODAY) == []
    assert rec.detect(_series("NEWGYM", [("2026-08-01", 3000), ("2026-09-01", 3000)]), TODAY) == []


def test_two_charges_count_only_on_a_strong_quarterly_or_annual_cadence():
    assert _one(_series("CLOUDBOX", [("2025-09-03", 9999), ("2026-09-03", 9999)]))["cadence"] == "annual"
    # 343 days sits in the loose annual band but not the strict one.
    assert rec.detect(_series("CLOUDBOX", [("2025-10-22", 9999), ("2026-09-30", 9999)]), TODAY) == []


def test_everyday_spending_with_uneven_amounts_is_excluded():
    rows = _series("TACO PLANET", [("2026-09-01", 1200), ("2026-09-08", 2300), ("2026-09-15", 900), ("2026-09-22", 3100), ("2026-09-29", 1500)],
                   group="Food", category="Dining out")
    assert rec.detect(rows, TODAY) == []


def test_steady_spending_category_and_generic_names_need_review():
    item = _one(_monthly("MEAL KIT BOX", 6000, group="Food", category="Groceries"))
    assert item["tier"] == "review" and "Food is usually not a bill" in item["review_reasons"]
    item = _one(_monthly("DIGITAL", 1500, group="Subscriptions", category="Subscriptions"))
    assert item["tier"] == "review" and item["review_reasons"] == ["generic merchant name"]


def test_irregular_timing_needs_review_and_a_single_price_change_does_not():
    # Every gap is in the loose monthly band, none in the strict one.
    gaps = _series("ACME LAWN", [(day, 5000) for day in ("2026-03-01", "2026-03-26", "2026-05-01", "2026-05-26", "2026-07-01", "2026-07-26", "2026-08-31")])
    item = _one(gaps)
    assert item["tier"] == "review" and "timing is irregular" in item["review_reasons"]
    raised = _series("STREAMFAKE", [(f"{month}-08", cents) for month, cents in zip(MONTHS, (1000, 1000, 1000, 1300, 1300))],
                     group="Subscriptions", category="Subscriptions")
    item = _one(raised)
    assert item["tier"] == "bill" and item["kind"] == "subscription" and item["price_change"] is None


def test_confirm_forces_a_bill_and_ignore_hides_it():
    fee = _monthly("FAKEBANK SAFE BOX", 1500, group="Fees & interest", category="Fees & interest")
    assert rec.detect(fee, TODAY) == []
    item = _one(fee, {"FAKEBANK SAFE BOX": "confirmed"})
    assert item["tier"] == "bill" and item["decision"] == "confirmed" and item["user_confirmed"]
    kit = _monthly("MEAL KIT BOX", 6000, group="Food", category="Groceries")
    assert rec.detect(kit, TODAY, decisions={"MEAL KIT BOX": "ignored"}) == []
    # A decision is about outflows; income from the same merchant key still shows.
    pay = _series("MEAL KIT BOX", [("2026-08-07", 250000), ("2026-08-21", 250000), ("2026-09-04", 250000), ("2026-09-18", 250000)],
                  group="Income", category="Paycheck", direction="in")
    assert [item["tier"] for item in rec.detect(pay, TODAY, decisions={"MEAL KIT BOX": "ignored"})] == ["income"]


def test_display_names_are_cleaned():
    assert default_display("ACME AUTO FINAN") == "Acme Auto Finance"
    assert default_display("ZETA DENTAL OF NOWHERE") == "Zeta Dental of Nowhere"
    assert default_display("FAKE MUTUAL NEW") == "Fake Mutual New"
    assert default_display('ZZQ PERSON "MEMO TEXT"') == "ZZQ Person"
    assert default_display("QRX") == "QRX"


# --- decisions in the database ----------------------------------------------------------

CHK, CARD = "fake-chk", "fake-card"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    with connect() as conn:
        add_account(conn, CHK, "cash", last4="1001")
        add_account(conn, CARD, "liability", last4="1003")
        for month in ("2026-06", "2026-07", "2026-08", "2026-09"):
            add_txn(conn, f"rent{month}", CHK, f"{month}-02", -150000, "ZEBRA PROPERTY GROUP")
            add_txn(conn, f"kit{month}", CARD, f"{month}-10", -6000, "MEAL KIT BOX")
            add_txn(conn, f"int{month}", CARD, f"{month}-20", -2345, "INTEREST CHARGE ON PURCHASES")
            add_txn(conn, f"div{month}", CHK, f"{month}-15", 4, "CASH DIVIDEND OF $0.04")
        add_txn(conn, "movie", CARD, "2026-09-25", -2450, "FILM PALACE 0042")
        classify_all(conn)
        rent = cat_id(conn, "Housing", "Rent")
        groceries = cat_id(conn, "Food", "Groceries")
        for month in ("2026-06", "2026-07", "2026-08", "2026-09"):
            actions.categorize(conn, f"rent{month}", rent, None, actor="test")
            actions.categorize(conn, f"kit{month}", groceries, None, actor="test")
    return tmp_path


def _keys(items, tier=None):
    return sorted(item["merchant_key"] for item in items if tier is None or item["tier"] == tier)


def test_review_flow_confirm_ignore_undo(db):
    with connect() as conn:
        data = pa.recurring(conn)
        assert _keys(data["items"], "bill") == ["ZEBRA PROPERTY GROUP"]
        assert _keys(data["items"], "review") == ["MEAL KIT BOX"]
        assert data["counts"]["needs_review"] == 1
        review = pa.recurring_review(conn)
        assert review["needs_review"] == 1 and review["review"][0]["merchant"] == "Meal Kit Box"
        assert review["confirmed"] == [] and review["ignored"] == []

        actions.set_recurring_decision(conn, "meal kit box", "confirmed", actor="test")
        data = pa.recurring(conn)
        assert _keys(data["items"], "bill") == ["MEAL KIT BOX", "ZEBRA PROPERTY GROUP"]
        assert data["counts"]["needs_review"] == 0
        review = pa.recurring_review(conn)
        assert [row["merchant_key"] for row in review["confirmed"]] == ["MEAL KIT BOX"]
        assert review["confirmed"][0]["cadence"] == "monthly" and review["confirmed"][0]["typical_cents"] == 6000

        actions.set_recurring_decision(conn, "MEAL KIT BOX", "ignored", actor="test")
        assert "MEAL KIT BOX" not in _keys(pa.recurring(conn)["items"])
        assert [row["merchant"] for row in pa.recurring_review(conn)["ignored"]] == ["Meal Kit Box"]

        actions.clear_recurring_decision(conn, "MEAL KIT BOX", actor="test")
        assert _keys(pa.recurring(conn)["items"], "review") == ["MEAL KIT BOX"]
        with pytest.raises(HpgbooksError):
            actions.clear_recurring_decision(conn, "MEAL KIT BOX", actor="test")
        with pytest.raises(HpgbooksError):
            actions.set_recurring_decision(conn, "MEAL KIT BOX", "maybe", actor="test")
        with pytest.raises(HpgbooksError):
            actions.set_recurring_decision(conn, "  ", "confirmed", actor="test")
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'personal_recurring_decision'").fetchone()[0] == 3


def test_dashboard_upcoming_bills_are_outflow_bills_only(db):
    with connect() as conn:
        data = pa.dashboard(conn)
        merchants = {event["merchant"] for event in data["upcoming_bills"]}
        assert merchants == {"Zebra Property Group"}
        assert all(event["direction"] == "out" for event in data["upcoming_bills"])
        assert data["recurring_review_count"] == 1
        actions.set_recurring_decision(conn, "MEAL KIT BOX", "confirmed", actor="test")
        data = pa.dashboard(conn)
        assert {event["merchant"] for event in data["upcoming_bills"]} == {"Zebra Property Group", "Meal Kit Box"}
        assert data["recurring_review_count"] == 0
        bills = pa.bills(conn, 45)
        assert not any("Interest" in event["merchant"] or "Dividend" in event["merchant"] for event in bills["events"])


def test_review_api(db):
    from hpgbooks.web import create_app

    client = create_app().test_client()
    token = client.get("/api/session?mode=personal").get_json()["csrf_token"]

    def post(url, payload):
        return client.post(f"{url}?mode=personal", json=payload, headers={"X-CSRF-Token": token})

    review = client.get("/api/personal/recurring/review?mode=personal").get_json()
    assert review["ok"] and review["needs_review"] == 1 and review["review"][0]["merchant_key"] == "MEAL KIT BOX"
    assert post("/api/personal/recurring/confirm", {"merchant_key": "MEAL KIT BOX"}).status_code == 200
    review = client.get("/api/personal/recurring/review?mode=personal").get_json()
    assert review["needs_review"] == 0 and review["confirmed"][0]["merchant_key"] == "MEAL KIT BOX"
    assert post("/api/personal/recurring/ignore", {"merchant_key": "MEAL KIT BOX"}).get_json()["result"]["status"] == "ignored"
    assert client.get("/api/personal/recurring/review?mode=personal").get_json()["ignored"][0]["merchant_key"] == "MEAL KIT BOX"
    assert post("/api/personal/recurring/undo", {"merchant_key": "MEAL KIT BOX"}).status_code == 200
    assert post("/api/personal/recurring/undo", {"merchant_key": "MEAL KIT BOX"}).status_code == 404
    assert post("/api/personal/recurring/confirm", {"merchant_key": ""}).status_code == 400
    assert post("/api/personal/recurring/confirm", {}).status_code == 400
    assert client.post("/api/personal/recurring/confirm?mode=personal", json={"merchant_key": "X"}).status_code in (400, 403)
    assert client.get("/api/personal/dashboard?mode=personal").get_json()["recurring_review_count"] == 1
