"""Capital One CSV import and statement checks. Synthetic fixtures only."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from hpgbooks.capitalone import (
    descriptions_match,
    import_capitalone_csv,
    parse_statement_text,
    synthetic_id,
    verify_statements,
)
import pytest

from hpgbooks.cli import main
from hpgbooks.db import connect, init_db
from fake_accounts import SAMPLE_CARD
from hpgbooks.seed_rules import rule_specs

KEY = "ab" * 32


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HPBOOKS_KEY", KEY)
    monkeypatch.setenv("HPBOOKS_DB", str(tmp_path / "books.db"))
    monkeypatch.delenv("HPBOOKS_KEY_FILE", raising=False)
    init_db()
    return tmp_path


def _txn(txn_id, account, day, amount, name, pending="false", merchant=""):
    return {
        "id": txn_id,
        "account_id": account,
        "date": day,
        "amount": amount,
        "direction": "in" if not str(amount).startswith("-") else "out",
        "currency": "USD",
        "name": name,
        "merchant_name": merchant,
        "description": "",
        "pending": pending,
        "category": "",
    }


def _write_feed(path: Path, account: str, txns: list[dict], date_from="2026-01-01", date_to="2026-12-31"):
    path.write_text(
        json.dumps(
            {
                "source": "finance-mcp",
                "account_id": account,
                "date_from": date_from,
                "date_to": date_to,
                "transactions": txns,
            }
        ),
        encoding="utf-8",
    )


def _class(txn_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM classifications WHERE txn_id = ?", (txn_id,)).fetchone()
        return {key: row[key] for key in row.keys()}


def _txn_row(txn_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        return {key: row[key] for key in row.keys()}


def _first_rule(name: str):
    ranked = sorted(enumerate(rule_specs()), key=lambda item: (item[1][0], item[0]))
    for _index, spec in ranked:
        _priority, pattern, _field, account, _sign, tag, category, confidence, _note = spec
        if account:
            continue
        if re.search(pattern, name, re.IGNORECASE):
            return tag, category, confidence
    return None


def test_new_rules_win():
    # More specific (lower priority number) rules beat broader ones.
    assert _first_rule("PAYPAL *SAMPLE BACKUP")[:2] == ("branda", "Hosting & Infrastructure")
    assert _first_rule("Amazon web services")[:2] == ("branda", "Hosting & Infrastructure")
    assert _first_rule("Google CLOUD T2wS8C")[:2] == ("branda", "Hosting & Infrastructure")
    assert _first_rule("PAYPAL *WIDGETSOFT")[:2] == ("branda", "Software & Licenses")
    assert _first_rule("SAMPLE HELPDESK")[:2] == ("branda", "Software & Licenses")
    assert _first_rule("PANELSOFT LICENSE")[:2] == ("branda", "Software & Licenses")
    assert _first_rule("SAMPLE DOMAINS")[:2] == ("branda", "Domains/DNS/SSL")
    assert _first_rule("EXAMPLE NIC")[:2] == ("branda", "Domains/DNS/SSL")
    assert _first_rule("PAYPAL *FAKE INK")[:2] == ("general", "Office/Other")
    assert _first_rule("PADDLE.NET* SAMPLE ANALYTICS")[:2] == ("branda", "Software & Licenses")
    assert descriptions_match("Amazon web services", "AWS", "Amazon Web Services")


def test_csv_ids_idempotent_and_overlap(env, tmp_path):
    feed = tmp_path / "connector.json"
    _write_feed(
        feed,
        SAMPLE_CARD,
        [_txn("aws-1", SAMPLE_CARD, "2026-07-02", "-3.33", "AWS", merchant="Amazon Web Services")],
    )
    assert main(["import", str(feed)]) == 0
    csv_path = tmp_path / "card.csv"
    csv_path.write_text(
        "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
        "2026-07-02,2026-07-02,0404,Amazon web services,Internet,3.33,\n"
        "2026-03-01,2026-03-02,0404,SAMPLE HELPDESK,Other Services,10.00,\n"
        "2026-03-01,2026-03-02,0404,SAMPLE HELPDESK,Other Services,10.00,\n"
        "2026-03-04,2026-03-04,9999,OTHER CARD,Other,1.00,\n",
        encoding="utf-8",
    )
    assert main(["import-capitalone", str(csv_path)]) == 0
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, source, amount_cents FROM transactions WHERE source = 'capitalone_csv' ORDER BY name, id"
        ).fetchall()
        log = conn.execute(
            "SELECT inserted, skipped FROM import_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert log["inserted"] == 2
    assert log["skipped"] == 2  # one overlap, one wrong card
    assert len(rows) == 2
    assert rows[0]["id"] != rows[1]["id"]
    assert rows[0]["name"] == "SAMPLE HELPDESK"
    assert rows[0]["id"].startswith("capone:")
    assert len(rows[0]["id"].split(":", 1)[1]) == 24
    expected_0 = synthetic_id(SAMPLE_CARD, "2026-03-01", "2026-03-02", "SAMPLE HELPDESK", -1000, 0)
    expected_1 = synthetic_id(SAMPLE_CARD, "2026-03-01", "2026-03-02", "SAMPLE HELPDESK", -1000, 1)
    assert {rows[0]["id"], rows[1]["id"]} == {expected_0, expected_1}
    assert _class(rows[0]["id"])["category"] == "Software & Licenses"

    assert main(["import-capitalone", str(csv_path)]) == 0
    with connect() as conn:
        again = conn.execute(
            "SELECT inserted, unchanged FROM import_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        count = conn.execute(
            "SELECT count(*) FROM transactions WHERE source = 'capitalone_csv'"
        ).fetchone()[0]
    assert again["inserted"] == 0
    assert again["unchanged"] == 2
    assert count == 2


def test_connector_supersedes_csv_and_pending_ignores_csv(env, tmp_path):
    csv_path = tmp_path / "card.csv"
    csv_path.write_text(
        "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
        "2026-04-10,2026-04-10,0404,WIDGET AI SUBSCR,Internet,20.00,\n",
        encoding="utf-8",
    )
    assert main(["import-capitalone", str(csv_path)]) == 0
    with connect() as conn:
        csv_id = conn.execute("SELECT id FROM transactions WHERE source = 'capitalone_csv'").fetchone()[0]
    assert main(["classify", csv_id, "--tag", "general", "--category", "Office/Other", "--note", "keep"]) == 0

    connector = tmp_path / "later.json"
    _write_feed(
        connector,
        SAMPLE_CARD,
        [_txn("widget-ai-new", SAMPLE_CARD, "2026-04-10", "-20.00", "WIDGET AI SUBSCR", merchant="Widget AI")],
        date_from="2026-04-10",
        date_to="2026-04-10",
    )
    # A pending connector row and a pending-flagged CSV row share the window.
    pending = tmp_path / "pending.json"
    _write_feed(
        pending,
        SAMPLE_CARD,
        [_txn("pend-1", SAMPLE_CARD, "2026-04-12", "-4.00", "SOME HOLD", pending="true")],
        date_from="2026-04-01",
        date_to="2026-04-30",
    )
    assert main(["import", str(pending)]) == 0
    with connect() as conn:
        conn.execute(
            "UPDATE transactions SET pending = 1 WHERE id = ?",
            (csv_id,),
        )
    posted = tmp_path / "posted.json"
    _write_feed(
        posted,
        SAMPLE_CARD,
        [_txn("posted-1", SAMPLE_CARD, "2026-04-12", "-4.00", "SOME HOLD")],
        date_from="2026-04-01",
        date_to="2026-04-30",
    )
    assert main(["import", str(connector), str(posted)]) == 0
    csv_row = _txn_row(csv_id)
    assert csv_row["status"] == "superseded"
    assert csv_row["superseded_by"] == "widget-ai-new"
    carried = _class("widget-ai-new")
    assert carried["source"] == "manual"
    assert carried["category"] == "Office/Other"
    assert carried["note"] == "keep"
    assert _txn_row("pend-1")["status"] == "superseded"
    assert _txn_row(csv_id)["status"] == "superseded"
    # The CSV row was flagged pending but must not be the thing the pending pass removed.
    with connect() as conn:
        note = conn.execute(
            "SELECT note FROM audit_log WHERE txn_id = ? AND action = 'supersede'",
            (csv_id,),
        ).fetchone()["note"]
    assert "widget-ai-new" in note


def test_statement_parser_matches_synthetic_csv():
    text = """
Dec 22, 2025 - Jan 21, 2026 | 31 days in Billing Cycle
Previous Balance                                                          $100.00
Payments                                                                - $100.00
Other Credits                                                                   $0.00
Transactions                                                           + $50.00
Cash Advances                                                              + $0.00
Fees Charged                                                               + $0.00
Interest Charged                                                           + $0.00
New Balance                                                            = $50.00

EXAMPLE CARDHOLDER #0404: Payments, Credits and Adjustments
Trans Date     Post Date      Description                                                                                               Amount
Jan 13         Jan 13         CAPITAL ONE ONLINE PYMT                                                                                - $100.00

EXAMPLE CARDHOLDER #0404: Transactions
Trans Date     Post Date      Description                                                                                               Amount
Dec 21         Dec 22         OUT OF RANGE CITY                                                                                          $9.00
Jan 2          Jan 3          PAYPAL *PANEL SOFT402-555-0100CA                                                                         $40.00
Jan 4          Jan 4          SAMPLE HELPDESKANYTOWN                                                                                     $10.00
"""
    # The $9 out-of-range line is not part of the $50 summary; adjust summary to include it
    # only inside coverage. Rebuild a consistent summary: charges in range are 50, and the
    # Dec 21 line is outside the comparison window so it must not create a line diff.
    statement = parse_statement_text(text, "synthetic.txt")
    assert statement["start"] == "2025-12-22"
    assert statement["end"] == "2026-01-21"
    assert statement["balance_ok"]
    dec = [item for item in statement["items"] if item["trans_date"].startswith("2025-")]
    assert dec and dec[0]["trans_date"] == "2025-12-21"
    csv_rows = [
        {
            "date": "2026-01-13",
            "posted_date": "2026-01-13",
            "amount_cents": 10000,
            "name": "CAPITAL ONE ONLINE PYMT",
        },
        {
            "date": "2026-01-02",
            "posted_date": "2026-01-03",
            "amount_cents": -4000,
            "name": "PAYPAL *PANEL SOFT",
        },
        {
            "date": "2026-01-04",
            "posted_date": "2026-01-04",
            "amount_cents": -1000,
            "name": "SAMPLE HELPDESK",
        },
    ]
    # Summary transactions are $50 but itemized lines are 9+40+10. That is a parser
    # fixture inconsistency for the balance only; line comparison is what this checks.
    # Force the summary figures to agree with itemized in-range totals by using the
    # parsed items and a statement whose summary matches 9+40+10 and the payment.
    text = text.replace("+ $50.00", "+ $59.00").replace("= $50.00", "= $59.00")
    statement = parse_statement_text(text, "synthetic.txt")
    assert statement["balance_ok"]
    reports = verify_statements(csv_rows, [statement])
    assert reports[0]["coverage"] == "partial"
    assert reports[0]["status"] == "OK"
    assert reports[0]["stmt_transactions"] == 5000
    assert reports[0]["csv_debits"] == 5000
    assert date.fromisoformat(reports[0]["start"]) < date(2026, 1, 1)


def _first_rule_for(name: str, account: str):
    ranked = sorted(enumerate(rule_specs()), key=lambda item: (item[1][0], item[0]))
    for _index, spec in ranked:
        _priority, pattern, _field, rule_account, _sign, tag, category, confidence, _note = spec
        if rule_account and rule_account != account:
            continue
        if re.search(pattern, name, re.IGNORECASE):
            return tag, category, confidence
    return None


def test_business_split_rules():
    from fake_accounts import CHARGE_CARD, REWARDS

    for name in ("WIDGET AI SUBSCR", "WIDGETAI.EXAMPLE"):
        assert _first_rule(name)[:2] == ("consulting", "AI Tools"), name
    for name in ("SAMPLE IDE (BY EXAMPLE)", "TUNNELCO INC."):
        assert _first_rule(name)[:2] == ("consulting", "Software & Licenses"), name
    assert _first_rule("SAMPLE PAAS.COM")[:2] == ("general", "Hosting & Infrastructure")
    # Account-scoped rules win on their own account; the generic one applies elsewhere.
    assert _first_rule_for("PURCHASE INTEREST CHARGE", REWARDS)[:2] == ("general", "Interest")
    assert _first_rule_for("RENEWAL MEMBERSHIP FEE", CHARGE_CARD)[:2] == ("general", "Bank & Card Fees")
    assert _first_rule_for("ANNUAL MEMBERSHIP FEE", REWARDS)[:2] == ("general", "Bank & Card Fees")
    assert _first_rule("ANNUAL MEMBERSHIP FEE")[:2] == ("branda", "Bank & Card Fees")
    # Word boundaries: similar-looking merchants are not caught.
    assert _first_rule("TUNNELCOMPANY") is None
    assert _first_rule("SAMPLE PAASCOM") is None
