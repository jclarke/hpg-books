"""Deterministic fake business ledger and the business outputs captured from it.

The golden files under tests/golden/ were captured from the code on master
before personal mode existed (see tests/make_golden.py). test_golden.py rebuilds
the same ledger and requires byte-for-byte identical output, with and without
personal accounts in the same database.

Every name, amount, and id here is made up. Dates are fixed and "today" is
frozen so recurring items and year-to-date windows never drift.
"""

from __future__ import annotations

import contextlib
import io
import json
from datetime import date as _real_date
from pathlib import Path

from fake_accounts import BANK, CHARGE_CARD, PAYPAL, REWARDS, SAMPLE_CARD

KEY = "ab" * 32
TODAY = _real_date(2026, 9, 30)
GOLDEN_DIR = Path(__file__).parent / "golden"


class FrozenDate(_real_date):
    """datetime.date with today() pinned to TODAY."""

    @classmethod
    def today(cls):
        return cls(TODAY.year, TODAY.month, TODAY.day)


# Modules that call date.today(). Personal-mode modules are added when present.
FROZEN_MODULES = (
    "hpgbooks.reports",
    "hpgbooks.analytics",
    "hpgbooks.api",
    "hpgbooks.webargs",
    "hpgbooks.accounts_admin",
    "hpgbooks.personal.analytics",
    "hpgbooks.payments",
)


# Modules that imported db.now_iso by name; timestamps land in created_at columns.
NOW_MODULES = (
    "hpgbooks.db",
    "hpgbooks.balances",
    "hpgbooks.classify",
    "hpgbooks.importer",
    "hpgbooks.seed_rules",
    "hpgbooks.vendors",
    "hpgbooks.capitalone",
)
FROZEN_NOW = "2026-09-30T12:00:00+00:00"


def freeze_today(monkeypatch) -> None:
    import importlib

    for name in FROZEN_MODULES:
        module = importlib.import_module(name)
        if hasattr(module, "date"):
            monkeypatch.setattr(module, "date", FrozenDate)
    for name in NOW_MODULES:
        module = importlib.import_module(name)
        if hasattr(module, "now_iso"):
            monkeypatch.setattr(module, "now_iso", lambda: FROZEN_NOW)


def set_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HPBOOKS_KEY", KEY)
    monkeypatch.setenv("HPBOOKS_DB", str(tmp_path / "books.db"))
    monkeypatch.delenv("HPBOOKS_KEY_FILE", raising=False)
    # Never read the prior-analysis seed file in tests.
    monkeypatch.setenv("HPGBOOKS_SEED_CSV", str(tmp_path / "no-seed.csv"))
    from hpgbooks.classify import reset_seed_cache

    reset_seed_cache()


def _t(txn_id, account, day, amount, name, merchant="", pending=False):
    return {
        "id": txn_id,
        "account_id": account,
        "date": day,
        "amount": amount,
        "direction": "out" if str(amount).startswith("-") else "in",
        "currency": "USD",
        "name": name,
        "merchant_name": merchant,
        "description": "",
        "pending": "true" if pending else "false",
        "category": "",
    }


def business_feeds() -> dict[str, list[dict]]:
    """Account id -> fake transactions, January through September 2026."""
    bank: list[dict] = []
    rewards: list[dict] = []
    charge: list[dict] = []
    paypal: list[dict] = []
    card: list[dict] = []
    for month in range(1, 10):
        m = f"2026-{month:02d}"
        bank += [
            _t(f"g-bank-proc-{month}", BANK, f"{m}-03", f"{2100 + month * 37}.15", "EXAMPLE PAYMENTS DES:FUNDS DISB ID:0001"),
            _t(f"g-bank-pfee-{month}", BANK, f"{m}-04", f"-{40 + month}.20", "EXAMPLE PAYMENTS DES:FUNDS DISB ID:0002"),
            _t(f"g-bank-client-{month}", BANK, f"{m}-05", "4000.00", "SAMPLE CLIENT LLC DES:PAYMENTS ID:9"),
            _t(f"g-bank-payroll-{month}", BANK, f"{m}-15", "-1500.00", "WIDGET PAYROLL DES:PAYROLL ID:77"),
            _t(f"g-bank-fee-{month}", BANK, f"{m}-28", "-16.00", "Monthly Maintenance Fee"),
            _t(f"g-bank-rewpay-{month}", BANK, f"{m}-20", f"-{300 + month * 11}.00", "SAMPLE REWARDS DES:EPAY ID:5"),
            _t(f"g-bank-chgpay-{month}", BANK, f"{m}-21", f"-{200 + month * 7}.00", "CHARGE CARD CO DES:ACH PMT ID:6"),
            _t(f"g-bank-draw-{month}", BANK, f"{m}-10", "-2500.00", "Online Banking transfer to CHK 0000 Confirmation# 1"),
            _t(f"g-bank-ppin-{month}", BANK, f"{m}-12", f"{150 + month}.00", "PAYPAL DES:TRANSFER ID:8"),
            _t(f"g-bank-mystery-{month}", BANK, f"{m}-17", f"-{60 + month * 3}.45", "ZZZ MYSTERY VENDOR"),
        ]
        rewards += [
            _t(f"g-rew-pay-{month}", REWARDS, f"{m}-22", f"{300 + month * 11}.00", "Payment Thank You-Mobile"),
            _t(f"g-rew-acme-{month}", REWARDS, f"{m}-02", f"-{120 + month}.00", "ACME HOSTING HOLDINGS", "Acme Hosting"),
            _t(f"g-rew-books-{month}", REWARDS, f"{m}-06", "-90.00", "WIDGET CLOUD *BOOKS ONLINE"),
            _t(f"g-rew-cert-{month}", REWARDS, f"{m}-09", "-24.99", "SAMPLE CERTS INC", "Sample Certs"),
        ]
        charge += [
            _t(f"g-chg-pay-{month}", CHARGE_CARD, f"{m}-23", f"{200 + month * 7}.00", "ONLINE PAYMENT - THANK YOU"),
            _t(f"g-chg-ai-{month}", CHARGE_CARD, f"{m}-11", "-20.00", "FAKE AI TOOLS INC", "Fake AI"),
            _t(f"g-chg-mart-{month}", CHARGE_CARD, f"{m}-14", f"-{30 + month}.10", "FAKE MART #123", "Fake Mart"),
        ]
        paypal += [
            _t(f"g-pp-pay-{month}", PAYPAL, f"{m}-07", f"{80 + month}.00", f"Payment from Customer {month}"),
            _t(f"g-pp-fee-{month}", PAYPAL, f"{m}-07", "-3.10", "PayPal Fee"),
            _t(f"g-pp-out-{month}", PAYPAL, f"{m}-12", f"-{150 + month}.00", "Money Transfer to Bank"),
        ]
    for month in range(7, 10):
        m = f"2026-{month:02d}"
        card.append(_t(f"g-card-host-{month}", SAMPLE_CARD, f"{m}-08", "-75.00", "FAKE HOSTING CO", "Fake Hosting"))
        card.append(_t(f"g-card-pay-{month}", SAMPLE_CARD, f"{m}-26", "75.00", "SAMPLE CARD ONLINE PYMT"))
        bank.append(_t(f"g-bank-cardpay-{month}", BANK, f"{m}-25", "-75.00", "SAMPLE CARD DES:ONLINE PMT ID:1"))
    # A mortgage paid straight from business checking, booked as an owner draw.
    for month in range(1, 10):
        bank.append(_t(f"g-bank-mortgage-{month}", BANK, f"2026-{month:02d}-01", "-2100.00", "FAKE MORTGAGE SERVICER DES:MTG PYMT"))
    # Pending rows, one superseded later by a posted row.
    bank.append(_t("g-bank-pending-1", BANK, "2026-09-27", "-42.00", "ZZZ PENDING SHOP", pending=True))
    rewards.append(_t("g-rew-pending-1", REWARDS, "2026-09-29", "-18.00", "ZZZ COFFEE CART", pending=True))
    return {BANK: bank, REWARDS: rewards, CHARGE_CARD: charge, PAYPAL: paypal, SAMPLE_CARD: card}


def _write_feed(path: Path, account: str, txns: list[dict]) -> Path:
    path.write_text(
        json.dumps(
            {
                "source": "finance-mcp",
                "account_id": account,
                "date_from": "2026-01-01",
                "date_to": "2026-09-30",
                "transactions": txns,
            }
        ),
        encoding="utf-8",
    )
    return path


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    from hpgbooks.cli import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def build_business_ledger(tmp_path: Path) -> None:
    """Import the fake feeds, classify by hand, set anchors, and merge vendors."""
    from hpgbooks.db import init_db

    init_db()
    inbox = tmp_path / "golden-inbox"
    inbox.mkdir(exist_ok=True)
    for index, (account, txns) in enumerate(business_feeds().items()):
        _write_feed(inbox / f"feed{index}.json", account, txns)
    code, _out, err = run_cli(["import", str(inbox)])
    assert code == 0, err
    # A later pull: the pending bank row posts with a new id.
    later = tmp_path / "golden-later"
    later.mkdir(exist_ok=True)
    (later / "bank.json").write_text(
        json.dumps(
            {
                "format": "csv",
                "csv": "id,account_id,date,amount,direction,currency,name,merchant_name,description,pending,category\n"
                f"g-bank-posted-1,{BANK},2026-09-28,-42.00,out,USD,ZZZ PENDING SHOP,,,false,\n",
                "row_count": 1,
                "next_cursor": None,
                "_query": {"account_ids": [BANK], "date_from": "2026-09-25", "date_to": "2026-09-30"},
            }
        ),
        encoding="utf-8",
    )
    code, _out, err = run_cli(["import", str(later)])
    assert code == 0, err
    manual = []
    for month in range(1, 10):
        manual.append((f"g-bank-mystery-{month}", "general", "Office/Other"))
        manual.append((f"g-rew-acme-{month}", "branda", "Hosting & Infrastructure"))
        manual.append((f"g-chg-ai-{month}", "general", "AI Tools"))
        manual.append((f"g-bank-mortgage-{month}", "owner_draw", "Owner Draw"))
    for month in range(7, 10):
        manual.append((f"g-card-host-{month}", "branda", "Hosting & Infrastructure"))
    manual.append(("g-bank-mystery-9", "consulting", "Office/Other"))
    for txn_id, tag, category in manual:
        code, _out, err = run_cli(["classify", txn_id, "--tag", tag, "--category", category])
        assert code == 0, err
    for argv in (
        ["balances", "set", "0101", "--balance", "15000.00", "--as-of", "2026-06-30", "--source", "statement"],
        ["balances", "set", "0101", "--balance", "16250.55", "--as-of", "2026-09-25", "--source", "finance"],
        ["balances", "set", "0303", "--balance", "512.40", "--as-of", "2026-09-25", "--source", "finance"],
        ["vendors", "merge", "ACME HOSTING HOLDINGS", "Acme Hosting", "--into", "Acme Hosting"],
    ):
        code, _out, err = run_cli(argv)
        assert code == 0, err


CLI_CASES = {
    "cli_pnl_month.txt": ["pnl", "--year", "2026", "--by", "month"],
    "cli_pnl_year.txt": ["pnl", "--year", "2026", "--by", "year"],
    "cli_pnl_ytd.txt": ["pnl", "--year", "2026", "--ytd"],
    "cli_pnl_branda.txt": ["pnl", "--year", "2026", "--business", "branda"],
    "cli_pnl_consulting.txt": ["pnl", "--year", "2026", "--business", "consulting"],
    "cli_pnl_general_year.txt": ["pnl", "--year", "2026", "--by", "year", "--business", "general"],
    "cli_pnl_csv.txt": ["pnl", "--year", "2026", "--by", "month", "--format", "csv"],
    "cli_reconcile.txt": ["reconcile", "--year", "2026"],
    "cli_transfers.txt": ["transfers"],
    "cli_accounts.txt": ["accounts"],
    "cli_balances_list.txt": ["balances", "list"],
    "cli_balances_history.txt": ["balances", "history", "0101"],
    "cli_review.txt": ["review", "--year", "2026"],
    "cli_review_csv.txt": ["review", "--year", "2026", "--format", "csv"],
    "cli_txns_month.txt": ["txns", "--month", "2026-03"],
    "cli_txns_account.txt": ["txns", "--account", "0101", "--limit", "40"],
    "cli_txns_tag.txt": ["txns", "--tag", "needs_review"],
    "cli_txns_search.txt": ["txns", "--search", "fake mart"],
    "cli_rules.txt": ["rules", "list"],
    "cli_vendors_aliases.txt": ["vendors", "aliases"],
}

RANGE = "start=2026-01-01&end=2026-09-30"
API_CASES = {
    "api_dashboard.json": "/api/dashboard",
    "api_dashboard_range.json": f"/api/dashboard?{RANGE}",
    "api_dashboard_branda.json": f"/api/dashboard?{RANGE}&business=branda",
    "api_pnl.json": "/api/pnl",
    "api_pnl_range.json": f"/api/pnl?{RANGE}&by=month",
    "api_pnl_general.json": f"/api/pnl?{RANGE}&business=general&by=year",
    "api_pnl_lines.json": f"/api/pnl/lines?{RANGE}&label=Net%20Income",
    "api_pnl_lines_draws.json": f"/api/pnl/lines?{RANGE}&label=Owner%20Draws%20(below%20the%20line)",
    "api_vendors.json": f"/api/vendors?{RANGE}",
    "api_vendors_aliases.json": "/api/vendors/aliases",
    "api_accounts.json": f"/api/accounts?{RANGE}",
    "api_register_bank.json": f"/api/accounts/{BANK}/register?limit=50",
    "api_register_rewards.json": f"/api/accounts/{REWARDS}/register",
    "api_balances.json": "/api/balances",
    "api_balances_bank.json": f"/api/balances?account={BANK}",
    "api_calendar.json": "/api/calendar",
    "api_review.json": "/api/review",
    "api_rules.json": "/api/rules",
    "api_rules_preview.json": "/api/rules/preview?pattern=FAKE%20MART",
    "api_transactions.json": f"/api/transactions?{RANGE}&limit=60",
    "api_transactions_review.json": "/api/transactions?needs_review=1",
    "api_transactions_search.json": "/api/transactions?search=payment&sort=amount&dir=asc",
    "api_transaction_detail.json": "/api/transactions/g-bank-client-3",
    "api_search.json": "/api/search?q=fake%20mart",
    "api_search_account.json": "/api/search?q=rewards",
    "api_review_count.json": "/api/review-count",
    "api_settings.json": "/api/settings",
    "export_transactions.csv": f"/export/transactions.csv?{RANGE}",
    "export_transactions_month.csv": "/export/transactions.csv",
    "export_pnl.csv": f"/export/pnl.csv?year=2026&{RANGE}",
    "export_pnl_year.csv": "/export/pnl.csv?year=2026&by=year",
}
for _name in ("pnl-by-business", "expenses-by-vendor", "cash-flow", "owner-draws", "schedule-c"):
    API_CASES[f"api_report_{_name}.json"] = f"/api/reports/{_name}?{RANGE}"
    API_CASES[f"export_{_name}.csv"] = f"/export/{_name}.csv?{RANGE}"

# The session body carries a random CSRF token; it is dropped before comparing.
SESSION_CASE = "api_session.json"


def capture_outputs() -> dict[str, bytes]:
    """Every golden output as bytes, keyed by file name."""
    from hpgbooks.web import create_app

    outputs: dict[str, bytes] = {}
    for name, argv in CLI_CASES.items():
        code, out, err = run_cli(argv)
        outputs[name] = f"exit={code}\n--- stdout\n{out}--- stderr\n{err}".encode("utf-8")
    client = create_app().test_client()
    for name, url in API_CASES.items():
        response = client.get(url)
        outputs[name] = f"status={response.status_code}\n".encode("utf-8") + response.get_data()
    session = client.get("/api/session").get_json()
    session.pop("csrf_token", None)
    outputs[SESSION_CASE] = json.dumps(session, sort_keys=True, indent=1).encode("utf-8")
    return outputs
