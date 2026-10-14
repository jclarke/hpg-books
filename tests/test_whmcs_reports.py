"""WHMCS reports on fake data. Temporary database and key only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpgbooks import whmcs_reports as wr
from hpgbooks.cli import main
from hpgbooks.db import HpgbooksError, connect, init_db
from fake_accounts import PAYPAL
from hpgbooks.whmcs import sync
from whmcs_fake import FakeSource, dataset, factory_for

KEY = "a1" * 32
TODAY = "2026-10-01"
PII = ("Alice", "Fakename", "Placeholder", "Testperson", "alice@example.test", "bob@example.test", "carol@example.test", "Sample Widgets")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HPBOOKS_KEY", KEY)
    monkeypatch.setenv("HPBOOKS_DB", str(tmp_path / "books.db"))
    monkeypatch.delenv("HPBOOKS_KEY_FILE", raising=False)
    init_db()
    return tmp_path


def load(tables=None):
    with connect() as conn:
        result = sync(conn, ["BrandA"], source_factory=factory_for({"BrandA": FakeSource(tables)}))
    assert result[0]["status"] == "ok", result


@pytest.fixture()
def synced(env):
    load()
    return env


def _paypal_feed(path: Path, rows: list[tuple[str, str, str, str]]) -> Path:
    path.write_text(
        json.dumps(
            {
                "source": "finance-mcp",
                "account_id": PAYPAL,
                "date_from": "2026-01-01",
                "date_to": "2026-09-30",
                "transactions": [
                    {
                        "id": txn_id,
                        "account_id": PAYPAL,
                        "date": day,
                        "amount": amount,
                        "direction": "out" if amount.startswith("-") else "in",
                        "currency": "USD",
                        "name": name,
                        "merchant_name": "",
                        "description": "",
                        "pending": "false",
                        "category": "",
                    }
                    for txn_id, day, amount, name in rows
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


# --- revenue ----------------------------------------------------------------------------------


def test_revenue_by_year_brand_and_plan(synced):
    with connect(readonly=True) as conn:
        data = wr.revenue(conn, start="2020-01-01", end="2026-12-31", by="year", today=TODAY)
    totals = data["totals"]
    assert (totals["gross_cents"], totals["fees_cents"], totals["refunds_cents"], totals["net_cents"]) == (33200, 530, 8000, 24670)
    assert [item["period"] for item in data["periods"]] == ["2020", "2021", "2022", "2023", "2024", "2025", "2026"]
    by_period = {item["period"]: item for item in data["periods"]}
    assert by_period["2025"]["gross_cents"] == 17000 and by_period["2025"]["refunds_cents"] == 5000
    assert by_period["2026"]["by_brand"]["BrandA"]["gross_cents"] == 16200
    plans = {item["plan"]: item for item in data["plans"]}
    assert plans["Pro"]["gross_cents"] == 24000
    assert plans["Starter"]["gross_cents"] == 1000
    assert plans["Addon: Backup"]["gross_cents"] == 200
    assert plans["Legacy"]["refunds_cents"] == 600
    assert plans["Domains"]["refunds_cents"] == 5000
    assert data["plans"][0]["plan"] == "Pro"
    assert sum(item["gross_cents"] for item in data["plans"]) == 33200


def test_revenue_by_quarter_and_brand_filter(synced):
    with connect(readonly=True) as conn:
        quarters = wr.revenue(conn, start="2026-01-01", end="2026-06-30", by="quarter", today=TODAY)
        other = wr.revenue(conn, start="2026-01-01", end="2026-06-30", brand="BrandB", today=TODAY)
        with pytest.raises(HpgbooksError):
            wr.revenue(conn, brand="NotABrand", today=TODAY)
        with pytest.raises(HpgbooksError):
            wr.revenue(conn, by="week", today=TODAY)
    assert [(item["period"], item["gross_cents"]) for item in quarters["periods"]] == [("2026-Q1", 1200), ("2026-Q2", 15000)]
    assert other["totals"]["gross_cents"] == 0


# --- MRR --------------------------------------------------------------------------------------


def test_mrr_counts_active_recurring_services_only(synced):
    with connect(readonly=True) as conn:
        data = wr.mrr(conn, months=0, today=TODAY)
    assert (data["mrr_cents"], data["arr_cents"], data["customers"], data["services"], data["arpu_cents"]) == (2200, 26400, 2, 3, 1100)
    plans = {item["plan"]: item["mrr_cents"] for item in data["plans"]}
    assert plans == {"Starter": 1000, "Pro": 1000, "Addon: Backup": 200}
    assert data["brands"][0]["brand"] == "BrandA" and data["brands"][0]["share_pct"] == 100.0


def test_mrr_trend_rebuilds_month_end_snapshots(synced):
    with connect(readonly=True) as conn:
        data = wr.mrr(conn, months=0, today=TODAY)
    trend = {point["month"]: point for point in data["trend"]}
    assert trend["2026-01"]["mrr_cents"] == 4200 and trend["2026-01"]["customers"] == 3
    assert trend["2026-02"]["mrr_cents"] == 3200 and trend["2026-02"]["customers"] == 2
    assert trend["2026-03"]["mrr_cents"] == 2200
    assert trend["2026-10"]["mrr_cents"] == 2200
    assert data["trend"][0]["month"] == "2025-01"
    assert "Estimate" in data["trend_note"]
    assert data["end_date_sources"] == {"termination": 1, "cancel_request": 1}


def test_billing_cycles_normalize_to_monthly():
    assert wr.monthly_cents(12000, "Annually") == 1000
    assert wr.monthly_cents(3000, "Quarterly") == 1000
    assert wr.monthly_cents(6000, "Semi-Annually") == 1000
    assert wr.monthly_cents(24000, "Biennially") == 1000
    assert wr.monthly_cents(36000, "Triennially") == 1000
    assert wr.monthly_cents(500, "Free Account") == 0
    assert wr.monthly_cents(500, "One Time") == 0


# --- churn ------------------------------------------------------------------------------------


def test_churn_months_rates_and_reasons(synced):
    with connect(readonly=True) as conn:
        data = wr.churn(conn, start="2026-01-01", end="2026-09-30", today=TODAY)
    months = {row["month"]: row for row in data["months"]}
    feb, mar = months["2026-02"], months["2026-03"]
    assert (feb["cancel_requests"], feb["end_of_period"], feb["immediate"]) == (1, 1, 0)
    assert (feb["start_customers"], feb["churned_customers"], feb["churned_services"]) == (3, 1, 1)
    assert feb["logo_churn_pct"] == 33.33 and feb["revenue_churn_pct"] == 23.81
    assert (mar["cancel_requests"], mar["immediate"], mar["churned_services"], mar["churned_customers"]) == (1, 1, 1, 0)
    assert mar["revenue_churn_pct"] == 31.25
    assert data["totals"]["churned_services"] == 2 and data["totals"]["net_adds"] == -2
    reasons = {item["category"]: item["count"] for item in data["reasons"]}
    assert reasons == {"Price": 1, "Moved to another provider": 1}
    assert all("@" not in item["reason"] for item in data["recent"])
    assert any("[email]" in item["reason"] for item in data["recent"])
    plans = {item["plan"]: item for item in data["plans"]}
    assert plans["Legacy"]["churned_services"] == 1 and plans["Legacy"]["service_churn_pct"] == 100.0


def test_reason_categories():
    assert wr.reason_category("") == "No reason given"
    assert wr.reason_category("Project finished, not needed anymore") == "No longer needed"
    assert wr.reason_category("Moving everything to GitHub") == "Moved to another provider"
    assert wr.reason_category("asdf") == "Other"


# --- refunds ------------------------------------------------------------------------------------


def test_refunds_rate_and_largest(synced):
    with connect(readonly=True) as conn:
        data = wr.refunds(conn, start="2025-01-01", end="2026-12-31", today=TODAY)
    assert data["totals"] == {"refunds_cents": 8000, "count": 2, "gross_cents": 33200, "rate_pct": 24.1}
    assert data["largest"][0]["refund_cents"] == 5000 and data["largest"][0]["plan"] == "Domains"
    assert data["largest"][1]["plan"] == "Pro"
    assert {item["gateway"]: item["refunds_cents"] for item in data["gateways"]} == {"cardgw": 5000, "paypal": 3000}


# --- dunning ------------------------------------------------------------------------------------


def test_dunning_aging_and_collections(synced):
    with connect(readonly=True) as conn:
        data = wr.dunning(conn, today=TODAY)
    assert data["totals"]["open_count"] == 2 and data["totals"]["open_cents"] == 4200
    buckets = {row["bucket"]: row["balance_cents"] for row in data["aging"]}
    assert buckets["1-30 days"] == 3000 and buckets["181-365 days"] == 1200
    assert [row["invoice_id"] for row in data["collections"]] == [1006, 1004]
    assert data["collections"][1]["days_overdue"] == 238
    trend = {row["month"]: row for row in data["trend"]}
    assert trend["2026-02"]["unpaid"] == 1 and trend["2026-02"]["capture_failed"] == 1
    assert trend["2026-03"]["cancelled"] == 1
    assert data["trend"][0]["month"] == "2024-11"


def test_partially_paid_invoice_shows_the_balance(env):
    data = dataset()
    data["tblaccounts"].append({"id": 9, "userid": 1, "currency": 0, "gateway": "paypal", "date": data["tblaccounts"][0]["date"], "amountin": 5, "fees": 0, "amountout": 0, "transid": "PPFAKE0009", "invoiceid": 1004, "refundid": 0})
    load(data)
    with connect(readonly=True) as conn:
        result = wr.dunning(conn, today=TODAY)
    row = next(item for item in result["collections"] if item["invoice_id"] == 1004)
    assert row["balance_cents"] == 700


# --- PayPal reconciliation ------------------------------------------------------------------------


def test_paypal_reconciliation_matches_net_amounts(env):
    data = dataset()
    data["tblaccounts"].append({"id": 7, "userid": 1, "currency": 0, "gateway": "paypal", "date": data["tblaccounts"][0]["date"].replace(month=4, day=1), "amountin": 9, "fees": 0.5, "amountout": 0, "transid": "PPFAKE0007", "invoiceid": 0, "refundid": 0})
    load(data)
    feed = _paypal_feed(
        env / "paypal.json",
        [
            ("pp-1", "2026-01-06", "11.35", "Payment from Someone Fake"),
            ("pp-2", "2026-06-01", "145.35", "Payment from Someone Else"),
            ("pp-3", "2026-06-11", "-30.00", "Refund to Someone Else"),
            ("pp-4", "2026-03-03", "13.71", "Payment from Not In WHMCS"),
        ],
    )
    assert main(["import", str(feed)]) == 0
    for txn_id, category in (("pp-1", "Revenue - Hosting"), ("pp-2", "Revenue - Hosting"), ("pp-3", "Refunds"), ("pp-4", "Revenue - Hosting")):
        assert main(["classify", txn_id, "--tag", "branda", "--category", category]) == 0
    with connect() as conn:
        before = conn.execute("SELECT COUNT(*), SUM(amount_cents) FROM transactions").fetchone()
        audit_before = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    with connect(readonly=True) as conn:
        result = wr.reconcile_paypal(conn, start="2026-01-01", end="2026-09-30", today=TODAY)
    statuses = {(row["status"], row["date"]): row for row in result["rows"]}
    assert statuses[("matched", "2026-01-05")]["books_id"] == "pp-1"
    assert statuses[("matched", "2026-01-05")]["match"] == "amount net of fee"
    assert statuses[("matched", "2026-06-10")]["books_cents"] == -3000
    assert statuses[("whmcs_only", "2026-04-01")]["whmcs_net_cents"] == 850
    assert statuses[("paypal_only", "2026-03-03")]["books_cents"] == 1371
    totals = result["totals"]
    assert (totals["matched"], totals["whmcs_only"], totals["paypal_only"]) == (3, 1, 1)
    assert totals["difference_cents"] == 1371 - 850
    march = next(row for row in result["months"] if row["month"] == "2026-03")
    assert march["paypal_only_cents"] == 1371 and march["difference_cents"] == 1371
    gateways = {(row["month"], row["gateway"]): row for row in result["gateways"]}
    assert gateways[("2026-06", "paypal")]["bank_cents"] == 14535 - 3000
    assert gateways[("2026-06", "paypal")]["net_cents"] == 15000 - 465 - 3000
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*), SUM(amount_cents) FROM transactions").fetchone() == before
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == audit_before


def test_reconciliation_prefers_a_transaction_id_in_the_ledger(env):
    load()
    feed = _paypal_feed(
        env / "paypal.json",
        [
            ("pp-a", "2026-01-05", "11.35", "Payment PPFAKE0001"),
            ("pp-b", "2026-01-05", "11.35", "Payment from Twin"),
        ],
    )
    assert main(["import", str(feed)]) == 0
    for txn_id in ("pp-a", "pp-b"):
        assert main(["classify", txn_id, "--tag", "branda", "--category", "Revenue - Hosting"]) == 0
    with connect(readonly=True) as conn:
        result = wr.reconcile_paypal(conn, start="2026-01-01", end="2026-01-31", today=TODAY)
    matched = [row for row in result["rows"] if row["status"] == "matched"]
    assert matched[0]["books_id"] == "pp-a" and matched[0]["match"] == "transaction id"


# --- customers ------------------------------------------------------------------------------------


def test_customer_search_by_name_email_company_domain_and_id(synced):
    with connect(readonly=True) as conn:
        assert [row["client_id"] for row in wr.customer_search(conn, "alice")] == [1]
        assert [row["client_id"] for row in wr.customer_search(conn, "BOB@example")] == [2]
        assert [row["client_id"] for row in wr.customer_search(conn, "sample widgets")] == [2]
        assert [row["client_id"] for row in wr.customer_search(conn, "carol.example")] == [3]
        assert wr.customer_search(conn, "3")[0]["client_id"] == 3
        assert wr.customer_search(conn, "%") == []
        assert wr.customer_search(conn, "a") == []
        hit = wr.customer_search(conn, "alice")[0]
    assert hit["total_paid_cents"] == 1200 and hit["mrr_cents"] == 1200 and hit["live_services"] == 3


def test_customer_detail(synced):
    with connect(readonly=True) as conn:
        alice = wr.customer_detail(conn, "BrandA", 1)
        carol = wr.customer_detail(conn, "branda", 3)
        assert wr.customer_detail(conn, "BrandA", 99) is None
    assert alice["name"] == "Alice Fakename" and alice["email"] == "alice@example.test"
    assert alice["totals"]["paid_cents"] == 1200 and alice["totals"]["unpaid_cents"] == 1200
    assert alice["credit_balance_cents"] == 500 and len(alice["credits"]) == 1
    assert {item["plan"] for item in alice["services"]} == {"Starter", "Addon: Backup"}
    assert carol["totals"]["refunds_cents"] == 3000 and carol["totals"]["paid_cents"] == 15000
    assert carol["cancellations"][0]["type"] == "Immediate"
    assert len(carol["invoices"]) == 2


def test_custom_addon_names_stay_off_reports(env):
    data = dataset()
    data["tblhostingaddons"].append({**data["tblhostingaddons"][0], "id": 202, "addonid": 0, "name": "Extra IP for secret-client.example.test"})
    data["tblinvoiceitems"].append({"id": 11, "invoiceid": 1001, "userid": 1, "type": "Addon", "relid": 202, "amount": 1, "duedate": None, "paymentmethod": "paypal"})
    load(data)
    with connect(readonly=True) as conn:
        text = json.dumps(wr.revenue(conn, start="2020-01-01", end="2026-12-31", today=TODAY)) + json.dumps(wr.mrr(conn, today=TODAY))
        detail = wr.customer_detail(conn, "BrandA", 1)
    assert "secret-client" not in text and "Addon: custom" in text
    assert any("secret-client" in item["plan"] for item in detail["services"])


# --- exports and CLI -------------------------------------------------------------------------------


def test_exports_have_no_names_or_emails(synced):
    with connect(readonly=True) as conn:
        datasets = {
            "revenue": wr.revenue(conn, start="2020-01-01", end="2026-12-31", today=TODAY),
            "mrr": wr.mrr(conn, today=TODAY),
            "churn": wr.churn(conn, start="2025-01-01", end="2026-09-30", today=TODAY),
            "refunds": wr.refunds(conn, start="2020-01-01", end="2026-12-31", today=TODAY),
            "dunning": wr.dunning(conn, today=TODAY),
            "reconcile": wr.reconcile_paypal(conn, start="2026-01-01", end="2026-09-30", today=TODAY),
        }
    tables = {
        "revenue": "revenue", "revenue-plans": "revenue", "mrr": "mrr", "mrr-trend": "mrr", "churn": "churn",
        "churn-plans": "churn", "refunds": "refunds", "refunds-largest": "refunds", "dunning": "dunning",
        "dunning-aging": "dunning", "reconcile": "reconcile", "reconcile-rows": "reconcile", "gateways": "reconcile",
    }
    for table, source in tables.items():
        text = wr.table_csv(table, datasets[source])
        assert text.count("\n") >= 1
        for secret in PII:
            assert secret not in text, (table, secret)
    revenue_csv = wr.table_csv("revenue", datasets["revenue"])
    assert "2026-01,BrandA,1,12.00,0.65,0.00,11.35" in revenue_csv


def test_csv_cells_cannot_start_formulas(synced):
    data = {"plans": [{"brand": "BrandA", "plan": "=HYPERLINK(1)", "group": "+x", "payments": 1, "gross_cents": 100, "fees_cents": 0, "refunds_cents": 0, "net_cents": 100}]}
    text = wr.table_csv("revenue-plans", data)
    assert "'=HYPERLINK(1)" in text and "'+x" in text


def test_cli_reports(synced, capsys):
    assert main(["whmcs", "revenue", "--by", "year", "--from", "2025-01-01", "--to", "2026-12-31"]) == 0
    out = capsys.readouterr()
    assert "BrandA" in out.out and "gross 332.00" in out.err
    assert main(["whmcs", "revenue", "--plans", "--format", "csv", "--from", "2025-01-01", "--to", "2026-12-31"]) == 0
    assert capsys.readouterr().out.startswith("Brand,Plan,Group")
    for args in (["mrr"], ["mrr", "--trend"], ["churn", "--from", "2026-01-01", "--to", "2026-09-30"], ["refunds"], ["dunning"], ["dunning", "--aging"], ["reconcile"]):
        assert main(["whmcs", *args]) == 0, args
    text = capsys.readouterr()
    for secret in PII:
        assert secret not in text.out + text.err
    assert main(["whmcs", "reconcile", "--brand", "BrandB"]) == 1


def test_reports_before_any_sync_are_empty(env):
    with connect(readonly=True) as conn:
        assert wr.summary(conn)["ready"] is False
        assert wr.mrr(conn, today=TODAY)["mrr_cents"] == 0


def test_summary_for_the_dashboard(synced):
    with connect(readonly=True) as conn:
        data = wr.summary(conn, today=TODAY)
    assert data["ready"] and data["mrr_cents"] == 2200 and data["customers"] == 2 and data["last_sync"]
    assert len(data["spark"]) == 12
