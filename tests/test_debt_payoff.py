"""Debt payoff what-if: APR parsing, the card/loan listing, scope, and the API. Fake data only."""

from __future__ import annotations

import pytest
from personal_helpers import add_account, add_txn, anchor, make_db

from hpgbooks import payments as pay
from hpgbooks.db import connect
from hpgbooks.personal import debt

TODAY = "2026-09-30"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("22.99% purchase / 28.49% cash", 22.99),
        ("33.24% revolving; 25.49% protected", 33.24),
        ("9.14%", 9.14),
        ("9.14% interest rate", 9.14),
        ("5.375%", 5.375),
        ("27.49% cash / 21.24% purchase", 21.24),
        ("Standard 18.5%, penalty 29.99%", 18.5),
        ("24", 24.0),
        ("", None),
        (None, None),
        ("n/a", None),
        ("   ", None),
        ("150%", None),
    ],
)
def test_parse_apr(text, expected):
    assert debt.parse_apr(text) == expected


def test_parse_maturity():
    assert debt.parse_maturity("Fake Lender; principal $1.00; matures 2027-01-05") == "2027-01-05"
    assert debt.parse_maturity("escrow 1.00; matures 12/2054") == "2054-12-01"
    assert debt.parse_maturity("originated 2024-08-05") is None
    assert debt.parse_maturity(None) is None


@pytest.fixture()
def db(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    with connect() as conn:
        add_account(conn, "pcard-hi", "liability", last4="1111", name="Fake High Card")
        add_account(conn, "pcard-lo", "liability", last4="2222", name="Fake Low Card")
        add_account(conn, "pcard-zero", "liability", last4="3333", name="Fake Paid Card")
        add_account(conn, "pcard-nomin", "liability", last4="4444", name="Fake New Card")
        add_account(conn, "ploan", "loan", last4="5555", name="Fake Personal Loan")
        add_account(conn, "pmort", "loan", last4="6666", name="Fake Home Loan")
        add_account(conn, "pcash", "cash", last4="7777", name="Fake Checking")
        add_account(conn, "bizcard", "liability", scope="business", last4="8888", name="Fake Biz Card")
        add_account(conn, "xcard", "liability", scope="excluded", last4="9990", name="Fake Excluded Card")
        conn.execute("UPDATE accounts SET subtype = 'mortgage' WHERE id = 'pmort'")
        for account_id, cents in (("pcard-hi", 300000), ("pcard-lo", 500000), ("pcard-zero", 0), ("pcard-nomin", 120000),
                                  ("ploan", 900000), ("pmort", 20000000), ("pcash", 400000), ("bizcard", 777700), ("xcard", 55500)):
            anchor(conn, account_id, "2026-09-01", cents)
        add_txn(conn, "t-hi-1", "pcard-hi", "2026-09-10", -2500, "FAKE STORE")  # owed goes up by $25
        pay.merge_terms(conn, "pcard-hi", {"min_payment_cents": 9000, "due_date": "2026-10-15", "apr": "29.99% purchase / 31.99% cash"}, source="statement", as_of="2026-09-20")
        pay.merge_terms(conn, "pcard-lo", {"min_payment_cents": 15000, "due_date": "2026-10-20", "apr": "12.5%"}, source="statement", as_of="2026-09-20")
        pay.merge_terms(conn, "pcard-nomin", {"min_payment_cents": 0}, source="sheet", as_of="2026-09-20")
        pay.merge_terms(conn, "ploan", {"min_payment_cents": 30000, "due_date": "2026-10-05", "apr": "9.5% interest rate",
                                        "notes": "Fake Lender; matures 2027-01-05"}, source="statement", as_of="2026-09-20")
        pay.merge_terms(conn, "bizcard", {"min_payment_cents": 20000, "due_date": "2026-10-10", "apr": "19.99%"}, source="statement", as_of="2026-09-20")
    return tmp_path


def test_build_lists_personal_cards_and_loans_only(db):
    with connect(readonly=True) as conn:
        result = debt.build(conn, TODAY)
    cards = {item["id"]: item for item in result["cards"]}
    loans = {item["id"]: item for item in result["loans"]}
    assert list(cards) == ["pcard-hi", "pcard-lo", "pcard-nomin"]  # highest APR first, zero balance left out
    hi = cards["pcard-hi"]
    assert hi["kind"] == "card" and hi["balance_cents"] == 302500 and hi["last4"] == "1111"
    assert hi["apr"] == 29.99 and hi["apr_text"] == "29.99% purchase / 31.99% cash"
    assert hi["minimum_payment_cents"] == 9000 and hi["due_date"] == "2026-10-15"
    assert cards["pcard-nomin"]["minimum_payment_cents"] is None and cards["pcard-nomin"]["apr"] is None
    assert list(loans) == ["ploan"]  # the mortgage is not a consolidation candidate
    assert loans["ploan"] == {**loans["ploan"], "kind": "loan", "apr": 9.5, "balance_cents": 900000, "maturity_date": "2027-01-05"}
    assert result["mortgages_excluded"] == 1
    assert result["totals"] == {
        "card_balance_cents": 302500 + 500000 + 120000,
        "loan_balance_cents": 900000,
        "card_minimums_cents": 9000 + 15000,
        "cards_missing_minimum": 1,
        "cards_missing_apr": 1,
    }


def test_business_and_excluded_accounts_never_appear(db):
    with connect(readonly=True) as conn:
        result = debt.build(conn, TODAY)
        business = {row["id"] for row in conn.execute("SELECT id FROM accounts WHERE scope != 'personal'")}
    assert {"bizcard", "xcard"} <= business
    listed = {item["id"] for item in result["cards"] + result["loans"]}
    assert not listed & business
    assert all(item["label"] != "Fake Biz Card" for item in result["cards"])


def test_balances_match_the_payments_page(db):
    with connect(readonly=True) as conn:
        rows = {row["id"]: row for row in pay.build(conn, "personal", TODAY)["rows"]}
        result = debt.build(conn, TODAY)
    for item in result["cards"] + result["loans"]:
        assert item["balance_cents"] == rows[item["id"]]["balance_cents"]


@pytest.fixture()
def client(db):
    from hpgbooks.web import create_app

    return create_app().test_client()


def test_api_is_personal_only_and_read_only(client, db):
    with connect(readonly=True) as conn:
        before = conn.execute("SELECT COUNT(*), MAX(id) FROM audit_log").fetchone()[:]
    response = client.get("/api/personal/debt-payoff?mode=personal")
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert [item["id"] for item in body["cards"]] == ["pcard-hi", "pcard-lo", "pcard-nomin"]
    assert "bizcard" not in response.get_data(as_text=True)
    assert client.get("/api/personal/debt-payoff").status_code == 404  # absent mode means business
    assert client.get("/api/personal/debt-payoff?mode=business").status_code == 404
    assert client.post("/api/personal/debt-payoff?mode=personal", json={}).status_code in (403, 405)
    with connect(readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*), MAX(id) FROM audit_log").fetchone()[:] == before
