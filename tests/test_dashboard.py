"""Dashboard, reports, and the local web UI. Temporary database only."""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import pytest

from hpgbooks.analytics import (
    account_register,
    build_dashboard,
    cash_flow_report,
    cash_outlook,
    period_figures,
    recurring_items,
    schedule_c_report,
    vendor_report,
)
from hpgbooks.cli import main
from hpgbooks.db import connect, init_db, set_setting
from fake_accounts import BANK, REWARDS
from hpgbooks.reports import _load_txns, build_pnl, prior_period, with_prior_period

KEY = "ab" * 32


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HPBOOKS_KEY", KEY)
    monkeypatch.setenv("HPBOOKS_DB", str(tmp_path / "books.db"))
    monkeypatch.delenv("HPBOOKS_KEY_FILE", raising=False)
    init_db()
    return tmp_path


def _txn(txn_id, account, day, amount, name, merchant=""):
    return {
        "id": txn_id,
        "account_id": account,
        "date": day,
        "amount": amount,
        "direction": "in" if not str(amount).startswith("-") else "out",
        "currency": "USD",
        "name": name,
        "merchant_name": merchant or name,
        "description": "",
        "pending": "false",
        "category": "",
    }


def _feed(path: Path, account: str, txns: list[dict]):
    path.write_text(
        __import__("json").dumps(
            {
                "source": "finance-mcp",
                "account_id": account,
                "date_from": "2026-01-01",
                "date_to": "2026-12-31",
                "fetched_at": "2026-03-01T00:00:00Z",
                "transactions": txns,
            }
        ),
        encoding="utf-8",
    )


def _csrf(client) -> str:
    token = client.get("/api/session").get_json()["csrf_token"]
    assert token
    return token


def test_prior_period_windows():
    assert prior_period("2026-01-01", "2026-12-31") == ("2025-01-01", "2025-12-31")
    assert prior_period("2026-01-01", "2026-10-01") == ("2025-01-01", "2025-10-01")
    assert prior_period("2026-03-01", "2026-03-31") == ("2026-01-29", "2026-02-28")


def test_dashboard_matches_pnl_and_moves_month_over_month(env, tmp_path):
    feed = tmp_path / "feed.json"
    _feed(
        feed,
        BANK,
        [
            _txn("aug-rev", BANK, "2026-08-04", "50.00", "ZZZ HOSTING AUG"),
            _txn("sep-rev", BANK, "2026-09-04", "100.00", "ZZZ HOSTING SEP"),
            _txn("sep-soft", BANK, "2026-09-05", "-40.00", "ZZZ SOFTWARE BILL"),
            _txn("sep-draw", BANK, "2026-09-06", "-10.00", "ZZZ DRAW"),
        ],
    )
    assert main(["import", str(feed)]) == 0
    assert main(["classify", "aug-rev", "--tag", "branda", "--category", "Revenue - Hosting"]) == 0
    assert main(["classify", "sep-rev", "--tag", "branda", "--category", "Revenue - Hosting"]) == 0
    assert main(["classify", "sep-soft", "--tag", "general", "--category", "Software & Licenses"]) == 0
    assert main(["classify", "sep-draw", "--tag", "owner_draw", "--category", "Owner Draw"]) == 0

    with connect() as conn:
        report = build_pnl(conn, 2026, by="month", business="all")
        figures = period_figures(
            [txn for txn in _load_txns(conn, "2026-09-01", "2026-09-30")],
            "all",
        )
        header = "Sep 2026"
        index = report.columns.index(header)
        net = next(row.values[index] for row in report.rows if row.label == "Net Income")
        assert figures["net_income"] == net
        assert figures["net_revenue"] == 10000
        assert figures["total_expenses"] == 4000
        assert figures["owner_draws"] == 1000
        assert figures["net_after_draws"] == 5000
        dash = build_dashboard(conn, "2026-09", "all")
    by_label = {item["label"]: item for item in dash["kpis"]}
    assert by_label["Net revenue"]["cents"] == 10000
    assert by_label["Net revenue"]["delta"] == 5000
    assert by_label["Net revenue"]["pct"] == 100.0
    assert by_label["Total expenses"]["cents"] == 4000
    assert by_label["Owner draws"]["cents"] == 1000
    assert by_label["Net after draws"]["cents"] == 5000
    assert dash["points"][0]["month"] == "2026-01"
    assert dash["points"][-1]["month"] == "2026-09"

    from hpgbooks.web import create_app

    client = create_app().test_client()
    page = client.get("/api/dashboard?start=2026-09-01&end=2026-09-30&business=all")
    assert page.status_code == 200
    body = page.get_json()
    kpis = {item["label"]: item for item in body["kpis"]}
    assert kpis["Net revenue"]["cents"] == 10000
    assert kpis["Net revenue"]["delta"] == 5000
    assert body["review_count"] == 0
    csp = page.headers["Content-Security-Policy"]
    assert csp.startswith("default-src 'self'")
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert page.headers["X-Frame-Options"] == "DENY"
    assert page.headers["X-Content-Type-Options"] == "nosniff"
    assert page.headers["Referrer-Policy"] == "no-referrer"
    assert client.get("/api/dashboard?business=nope").status_code == 400
    assert client.get("/api/dashboard?start=2026-13-01&end=2026-13-30").status_code == 400


def test_dashboard_chart_uses_the_full_month_when_the_range_ends_early(env, tmp_path):
    """A mid-month range keeps the KPI inside the dates asked for, while the
    September chart point is the whole calendar month.
    """
    feed = tmp_path / "chart.json"
    _feed(
        feed,
        BANK,
        [
            _txn("chart-early", BANK, "2026-09-04", "100.00", "ZZZ HOSTING CHART EARLY"),
            _txn("chart-late", BANK, "2026-09-20", "50.00", "ZZZ HOSTING CHART LATE"),
            _txn("chart-oct", BANK, "2026-10-02", "25.00", "ZZZ HOSTING CHART OCT"),
        ],
    )
    assert main(["import", str(feed)]) == 0
    for txn_id in ("chart-early", "chart-late", "chart-oct"):
        assert main(["classify", txn_id, "--tag", "branda", "--category", "Revenue - Hosting"]) == 0
    from hpgbooks.web import create_app

    client = create_app().test_client()
    partial = client.get("/api/dashboard?start=2026-09-01&end=2026-09-15").get_json()
    kpis = {item["key"]: item for item in partial["kpis"]}
    september = next(point for point in partial["points"] if point["month"] == "2026-09")
    assert kpis["net_revenue"]["cents"] == 10000
    assert kpis["net_income"]["cents"] == 10000
    assert september["revenue"] == 15000
    assert september["net"] == 15000
    assert kpis["net_income"]["spark"][-1] == 15000
    assert all(point["month"] != "2026-10" for point in partial["points"])
    full = client.get("/api/dashboard?start=2026-09-01&end=2026-09-30").get_json()
    full_kpis = {item["key"]: item for item in full["kpis"]}
    full_september = next(point for point in full["points"] if point["month"] == "2026-09")
    assert full_kpis["net_revenue"]["cents"] == 15000
    assert full_september["revenue"] == 15000


def test_transactions_filters_sort_and_csv(env, tmp_path):
    feed = tmp_path / "feed.json"
    _feed(
        feed,
        BANK,
        [
            _txn("big", BANK, "2026-05-02", "-80.00", "ZZZ BIG BILL"),
            _txn("small", BANK, "2026-05-03", "-5.00", "ZZZ SMALL BILL"),
            _txn("inflow", BANK, "2026-05-04", "20.00", "ZZZ INFLOW"),
        ],
    )
    assert main(["import", str(feed)]) == 0
    from hpgbooks.web import create_app

    client = create_app().test_client()
    narrow = client.get("/api/transactions?month=2026-05&min_amount=-10&max_amount=0&sort=amount&dir=asc")
    assert narrow.status_code == 200
    names = [row["name"] for row in narrow.get_json()["rows"]]
    assert names == ["ZZZ SMALL BILL"]
    exported = client.get("/export/transactions.csv?month=2026-05&status=active&sort=amount&dir=asc")
    assert exported.status_code == 200
    assert exported.mimetype.startswith("text/csv")
    csv_text = exported.get_data(as_text=True)
    assert "ZZZ BIG BILL" in csv_text
    assert csv_text.index("ZZZ BIG BILL") < csv_text.index("ZZZ SMALL BILL")
    ranged = client.get("/export/transactions.csv?start=2026-05-01&end=2026-05-31&sort=amount&dir=asc")
    assert "ZZZ BIG BILL" in ranged.get_data(as_text=True)
    assert client.get("/api/transactions?status=nope").status_code == 400
    assert client.get("/api/transactions?account=not-an-account").status_code == 400


def test_accounts_register_running_balance(env, tmp_path):
    feed = tmp_path / "feed.json"
    _feed(
        feed,
        BANK,
        [
            _txn("dep", BANK, "2026-04-01", "100.00", "ZZZ DEPOSIT"),
            _txn("pay", BANK, "2026-04-02", "-30.00", "ZZZ PAYMENT"),
        ],
    )
    assert main(["import", str(feed)]) == 0
    with connect() as conn:
        first, total, balance = account_register(conn, BANK, limit=1, offset=0)
        second, _, _ = account_register(conn, BANK, limit=1, offset=1)
    assert total == 2
    assert balance == 7000
    assert first[0]["description"] == "ZZZ PAYMENT"
    assert first[0]["payment_cents"] == 3000
    assert first[0]["running_cents"] == 7000
    assert second[0]["deposit_cents"] == 10000
    assert second[0]["running_cents"] == 10000

    from hpgbooks.web import create_app

    client = create_app().test_client()
    page = client.get("/api/accounts?start=2026-04-01&end=2026-04-30")
    assert page.status_code == 200
    rows = page.get_json()["rows"]
    assert {row["last4"] for row in rows} == {"0101", "0303", "0404", "0505", None}
    assert any(row["short_name"] == "PayPal" for row in rows)
    bank = next(row for row in rows if row["id"] == BANK)
    assert bank["balance_cents"] == 7000 and bank["net_cents"] == 7000
    detail = client.get(f"/api/accounts/{BANK}/register")
    assert detail.status_code == 200
    reg = detail.get_json()
    assert reg["rows"][-1]["name"] == "ZZZ DEPOSIT"
    assert reg["balance_cents"] == 7000
    assert client.get("/api/accounts/not-a-real-account/register").status_code == 404
    # The register is a client-side route of the React app.
    assert client.get(f"/accounts/{BANK}").status_code == 200


def test_reports_exports_and_vendor_rules(env, tmp_path):
    feed = tmp_path / "feed.json"
    _feed(
        feed,
        BANK,
        [
            _txn("rev", BANK, "2026-06-01", "100.00", "ZZZ HOST REV", merchant="ZZZ HOST REV"),
            _txn("soft", BANK, "2026-06-02", "-25.00", "ZZZ VENDOR SOFT", merchant="ZZZ VENDOR SOFT"),
            _txn("soft2", BANK, "2026-06-03", "-15.00", "ZZZ VENDOR SOFT", merchant="ZZZ VENDOR SOFT"),
            _txn("xfer-out", BANK, "2026-06-04", "-40.00", "ZZZ INTERNAL OUT"),
            _txn("draw", BANK, "2026-06-05", "-8.00", "ZZZ OWNER TAKE"),
            _txn("evil", BANK, "2026-06-06", "-3.00", "=1+1<script>", merchant="=1+1<script>"),
        ],
    )
    rewards = tmp_path / "rewards.json"
    _feed(rewards, REWARDS, [_txn("xfer-in", REWARDS, "2026-06-04", "40.00", "ZZZ INTERNAL IN")])
    assert main(["import", str(feed)]) == 0
    assert main(["import", str(rewards)]) == 0
    assert main(["classify", "rev", "--tag", "branda", "--category", "Revenue - Hosting"]) == 0
    assert main(["classify", "soft", "--tag", "general", "--category", "Software & Licenses"]) == 0
    assert main(["classify", "soft2", "--tag", "general", "--category", "Software & Licenses"]) == 0
    assert main(["classify", "xfer-out", "--tag", "transfer", "--category", "Transfer"]) == 0
    assert main(["classify", "xfer-in", "--tag", "transfer", "--category", "Transfer"]) == 0
    assert main(["classify", "draw", "--tag", "owner_draw", "--category", "Owner Draw"]) == 0
    assert main(["classify", "evil", "--tag", "general", "--category", "Office/Other"]) == 0

    with connect() as conn:
        vendors, vendor_table = vendor_report(conn, "2026-06-01", "2026-06-30", "all", limit=10)
        names = [row["merchant"] for row in vendors]
        assert "ZZZ VENDOR SOFT" in names
        assert "ZZZ INTERNAL OUT" not in names
        assert "ZZZ OWNER TAKE" not in names
        soft = next(row for row in vendors if row["merchant"] == "ZZZ VENDOR SOFT")
        assert soft["spend_cents"] == 4000
        assert soft["count"] == 2
        flow = cash_flow_report(conn, "2026-06-01", "2026-06-30", "all")
        bank_june = next(row for row in flow.rows if row[0].text.startswith("Jun") and "0101" in row[1].text)
        assert bank_june[2].cents == 10000
        assert bank_june[3].cents == 5100
        assert not any("Rewards" in row[1].text or "0303" in row[1].text for row in flow.rows)
        from hpgbooks.analytics import owner_draws_report

        draws = owner_draws_report(conn, "2026-06-01", "2026-06-30")
        assert any(cell.cents == 800 for row in draws.rows for cell in row if cell.cents is not None)
        schedule = schedule_c_report(conn, "2026-06-01", "2026-06-30", "all")
        amounts = {row[1].text: row[2].cents for row in schedule.rows}
        assert amounts["Gross receipts or sales"] == 10000
        assert amounts["Other: Software & Licenses"] == 4000
        assert amounts["Office expense"] == 300
        assert amounts["Net profit (or loss)"] == 10000 - 4000 - 300
        memo = amounts["Memo: net profit plus uncategorized equals P&L net income"]
        pnl = build_pnl(conn, 2026, by="year", business="all", start="2026-06-01", end="2026-06-30")
        assert memo == pnl.net_income_cents
        compared = with_prior_period(conn, 2026, by="month", business="all")
        assert compared.prior_start == "2025-01-01"
        assert compared.prior_values is not None
        assert len(compared.prior_values) == len(compared.rows)

    from hpgbooks.web import create_app

    client = create_app().test_client()
    index = client.get("/api/reports/schedule-c?start=2026-06-01&end=2026-06-30")
    assert index.status_code == 200
    assert index.get_json()["heading"] == "Schedule C-style summary"
    vendor_page = client.get("/api/vendors?start=2026-06-01&end=2026-06-30")
    assert vendor_page.status_code == 200
    assert vendor_page.mimetype == "application/json"
    # Text is returned as data; the React app renders it as text, never HTML.
    assert any(row["merchant"] == "=1+1<script>" for row in vendor_page.get_json()["rows"])
    csv_page = client.get("/export/expenses-by-vendor.csv?start=2026-06-01&end=2026-06-30&business=all")
    assert csv_page.status_code == 200
    assert b"'=1+1" in csv_page.data
    assert b"ZZZ VENDOR SOFT" in csv_page.data
    for name in ("pnl-by-business", "expenses-by-vendor", "cash-flow", "owner-draws", "schedule-c"):
        for ext, magic in (("csv", b""), ("xlsx", b"PK"), ("pdf", b"%PDF")):
            response = client.get(f"/export/{name}.{ext}?start=2026-06-01&end=2026-06-30&business=all")
            assert response.status_code == 200, name + ext
            if magic:
                assert response.data[: len(magic)] == magic
    xlsx = client.get("/export/pnl.xlsx?year=2026&by=month&business=all")
    assert xlsx.status_code == 200
    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(xlsx.data))
    assert "Prior" in [cell.value for cell in book["P&L"][1]]
    assert "% Change" in [cell.value for cell in book["P&L"][1]]
    ranged_xlsx = client.get("/export/pnl.xlsx?year=2026&by=month&business=all&start=2026-06-01&end=2026-06-30")
    assert ranged_xlsx.status_code == 200
    pnl_page = client.get("/api/pnl?start=2026-01-01&end=2026-12-31&by=month&business=all").get_json()
    assert pnl_page["prior_start"] == "2025-01-01"
    assert all("pct" in row and "prior" in row for row in pnl_page["rows"])
    side = client.get("/api/reports/pnl-by-business?start=2026-06-01&end=2026-06-30")
    assert side.status_code == 200
    assert "Brand A" in side.get_json()["columns"]
    assert client.get("/export/not-a-report.csv").status_code == 404


def test_review_bulk_calendar_and_audit(env, tmp_path):
    feed = tmp_path / "feed.json"
    _feed(
        feed,
        BANK,
        [
            _txn("r1", BANK, "2026-07-01", "-100.00", "ZZZ RENT", merchant="ZZZ RENT"),
            _txn("r2", BANK, "2026-08-01", "-100.00", "ZZZ RENT", merchant="ZZZ RENT"),
            _txn("r3", BANK, "2026-09-01", "-100.00", "ZZZ RENT", merchant="ZZZ RENT"),
            _txn("q1", BANK, "2026-09-11", "-4.00", "ZZZ QUEUE ONE"),
            _txn("q2", BANK, "2026-09-12", "-6.00", "ZZZ QUEUE TWO"),
        ],
    )
    assert main(["import", str(feed)]) == 0
    with connect() as conn:
        items = recurring_items(conn)
        rent = next(item for item in items if item["merchant"] == "ZZZ RENT")
        assert rent["last_cents"] == -10000
        assert rent["next_date"] == "2026-10-02"
        outlook = cash_outlook(conn, today=date(2026, 10, 1), reserve_cents=5000)
    assert outlook["opening_cents"] == -30000 - 400 - 600
    assert outlook["ending_cents"] == outlook["opening_cents"] - 20000
    assert outlook["after_reserve_cents"] == outlook["ending_cents"] - 5000
    assert any(row["date"] == "2026-10-02" for row in outlook["timeline"])

    from hpgbooks.web import create_app

    client = create_app().test_client()
    review = client.get("/api/review")
    assert review.status_code == 200
    assert {"q1", "q2"} <= {row["id"] for row in review.get_json()["rows"]}
    token = _csrf(client)
    denied = client.post(
        "/api/classify/bulk",
        json={"txn_ids": ["q1"], "tag": "general", "category": "Office/Other"},
        headers={"Origin": "https://evil.example", "X-CSRF-Token": token},
    )
    assert denied.status_code == 403
    missing = client.post(
        "/api/classify",
        json={"txn_id": "q1", "tag": "general", "category": "Office/Other"},
    )
    assert missing.status_code == 403
    saved = client.post(
        "/api/classify/bulk",
        json={"txn_ids": ["q1", "q2"], "tag": "general", "category": "Office/Other", "note": "bulk"},
        headers={"X-CSRF-Token": token},
    )
    assert saved.status_code == 200
    remaining = {row["id"] for row in client.get("/api/review").get_json()["rows"]}
    assert "q1" not in remaining and "q2" not in remaining
    calendar = client.get("/api/calendar")
    assert calendar.status_code == 200
    assert calendar.get_json()["outlook"]["estimate"] is True
    assert any(item["merchant"] == "ZZZ RENT" for item in calendar.get_json()["items"])
    updated = client.post("/api/settings", json={"reserve_cents": 12550}, headers={"X-CSRF-Token": token})
    assert updated.status_code == 200
    assert client.get("/api/calendar").get_json()["reserve_cents"] == 12550
    audit = client.get("/api/audit")
    assert audit.status_code == 200
    actions = {(row["action"], row["field"]) for row in audit.get_json()["rows"]}
    assert ("setting", "reserve_cents") in actions
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'reserve_cents'").fetchone()
        assert row["value"] == "12550"
        set_setting(conn, "reserve_cents", "12550", actor="web")
    rules = client.get("/api/rules")
    assert rules.status_code == 200
    assert client.get("/healthz").headers["X-Frame-Options"] == "DENY"
