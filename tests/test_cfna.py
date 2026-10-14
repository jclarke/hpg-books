"""CFNA statement parser and importer. Synthetic statement text only: fake merchants,
fake references, made-up amounts. No real statement data lives in the repository."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from personal_helpers import add_account, add_txn, cat_id, make_db

from hpgbooks import cfna
from hpgbooks.accounts_admin import discover
from hpgbooks.db import connect

LAST4 = "8642"  # made up

PAGE_NOISE = """
 Please See Reverse Side for Important Information          Detach Here and Return the Portion Below With Payment
                 Payment      Account                MINIMUM          AMOUNT
 P.O. Box 00000                     01/01/2099    $1.00         8642          $2.00        $__ _____.___
Payments: You may at any time pay the unpaid balance (shown as the "New Balance" on the Statement, page 1)
                                                                              Page 2 of 6
"""


def statement_text(*, new_balance="$1,142.11", purchases="+$190.50", payments="-$100.00", other="-$20.00", fees="+$5.00", interest="+$12.34", prev="$1,054.27") -> str:
    # 1054.27 + 190.50 + 5.00 + 12.34 - 100.00 - 20.00 = 1142.11
    return f"""
                                          Questions?                      February 06, 2099 - March 05, 2099
                                          CFNA.COM                              28 days in billing cycle
   FAKE CARDHOLDER                                   ACCOUNT ENDING          MINIMUM PAYMENT DUE
                                                           8642
ACCOUNT SUMMARY                                                                      PAYMENT INFORMATION
 Statement Closing Da te                                           03/05/2099        NEW BALANCE                    {new_balance}
 Previous Ba la nce                                                  {prev}        PAYMENT DUE DATE              04/01/2099
 Payments                                                          {payments}        MINIMUM PAYMENT DUE             $25.00
 Other Credits                                                       {other}
 Purchases / Debits                                                {purchases}
 Fees Charged                                                         {fees}
 Interest Charged                                                    {interest}
 New B alance                                                       {new_balance}
 Credit L imit                                                    $1,000.00
{PAGE_NOISE}
TRANSACTIONS
PAYMENTS & CREDITS
Date           Reference #                        Descripti on                                    Amount
02/11/2099     1111111AA00XXAAA1                  WEB ACH PMT - THANK YOU                          -$100.00
02/12/2099     2222222BB00YYBBB2                  PLANET FITNESS ANYTOWN XX                              -$20.00
{PAGE_NOISE}
PURCHASES & DEBITS
FAKE CARDHOLDER #8642
Date           Reference #                        Descripti on                                    Amount
02/06/2099     3333333CC00ZZCCC3                  PLANET FITNESS ANYTOWN XX                              $20.00
02/07/2099     4444444DD00AADDD4                  JIFFY LUBE #1234 ANYTOWN XX                      $100.50
                                          Questions?                      February 06, 2099 - March 05, 2099
                                          Page 4 of 6
FAKE CARDHOLDER                                   ACCOUNT ENDING 8642

TRANSACTIONS - continued
FAKE CARDHOLDER #8642
02/20/2099     5555555EE00BBEEE5                  KROGER 0042 ANYTOWN XX                           $70.00
TRANSACTION TOTAL                                                                          $190.50
FEES
Date           Reference #                        Descripti on                                    Amount
03/01/2099                                         LATE FEE                                          $5.00
TOTAL FEES FOR THIS PERIOD                                                                     $5.00
INTEREST CHARGED
Date                                              Descripti on                                    Amount
03/05/2099                                         INTEREST CHARGE ON PURCHASES                    $12.34
TOTAL INTEREST FOR THIS PERIOD                                                                 $12.34

2099 TOTALS YEAR-TO-DATE
Total Interest Charged in 2099                                                                 $12.34
"""


def test_parse_summary_rows_and_signs():
    stmt = cfna.parse_statement_text(statement_text())
    assert stmt.closing_date == "2099-03-05"
    assert (stmt.period_start, stmt.period_end) == ("2099-02-06", "2099-03-05")
    assert stmt.last4 == "8642"
    assert stmt.summary["previous_balance"] == 105427
    assert stmt.summary["payments"] == -10000
    assert stmt.summary["new_balance"] == 114211
    kinds = [(t.kind, t.amount_cents) for t in stmt.txns]
    assert ("payment", 10000) in kinds  # payment received is positive in the ledger
    assert ("credit", 2000) in kinds  # a refund inside PAYMENTS & CREDITS
    assert ("purchase", -10050) in kinds  # purchases are negative
    assert ("fee", -500) in kinds
    assert ("interest", -1234) in kinds
    assert len(stmt.txns) == 7
    assert not stmt.warnings


def test_rows_after_a_page_break_keep_their_section_and_fee_rows_get_stable_refs():
    stmt = cfna.parse_statement_text(statement_text())
    after_break = next(t for t in stmt.txns if t.description.startswith("KROGER"))
    assert after_break.section == "purchases" and after_break.kind == "purchase"
    refund = cfna.parse_statement_text(
        statement_text().replace("KROGER 0042 ANYTOWN XX                           $70.00", "KROGER 0042 ANYTOWN XX                           $-70.00")
    )
    negative = next(t for t in refund.txns if t.description.startswith("KROGER"))
    assert negative.statement_amount_cents == -7000 and negative.kind == "refund"  # "$-70.00" is a credit inside PURCHASES
    fee = next(t for t in stmt.txns if t.kind == "fee")
    interest = next(t for t in stmt.txns if t.kind == "interest")
    assert (fee.reference, fee.has_reference) == ("FEE-20990301", False)
    assert interest.reference == "INT-20990305"
    assert stmt.txns[0].txn_id == "cfna:1111111AA00XXAAA1:2099-02-11:10000"


def test_reconcile_ok_and_mismatch():
    text = statement_text()
    stmt = cfna.parse_statement_text(text)
    result = cfna.reconcile(stmt)
    assert result["ok"], result["problems"]
    assert all(check["ok"] for check in result["checks"])

    broken = cfna.parse_statement_text(text.replace("$70.00", "$71.00"))
    result = cfna.reconcile(broken)
    assert not result["ok"]
    assert any(p.startswith("purchases") for p in result["problems"])
    assert any(p.startswith("balance") for p in result["problems"])

    wrong_new = cfna.parse_statement_text(statement_text(new_balance="$9.99"))
    assert not cfna.reconcile(wrong_new)["ok"]


def test_non_cfna_text_is_rejected():
    assert not cfna.looks_like_cfna("Some NDA text")
    with pytest.raises(cfna.StatementError):
        cfna.parse_statement_text("hello")


def test_unique_rows_drops_a_row_repeated_by_an_overlapping_statement():
    one = cfna.parse_statement_text(statement_text())
    two = cfna.parse_statement_text(statement_text())
    assert len(cfna.unique_rows([one, two])) == len(one.txns)


def test_two_identical_rows_in_one_statement_get_distinct_ids():
    text = statement_text().replace(
        "02/20/2099     5555555EE00BBEEE5                  KROGER 0042 ANYTOWN XX                           $70.00",
        "02/20/2099     5555555EE00BBEEE5                  KROGER 0042 ANYTOWN XX                           $35.00\n"
        "02/20/2099     5555555EE00BBEEE5                  KROGER 0042 ANYTOWN XX                           $35.00",
    )
    stmt = cfna.parse_statement_text(text)
    ids = [t.txn_id for t in stmt.txns if t.description.startswith("KROGER")]
    assert len(set(ids)) == 2 and ids[1].endswith("#2")


def test_csv_columns():
    rows = cfna.parse_statement_text(statement_text()).txns
    lines = cfna.to_csv(rows).splitlines()
    assert lines[0] == "date,reference,description,amount,kind,statement"
    assert "2099-02-11,1111111AA00XXAAA1,WEB ACH PMT - THANK YOU,100.00,payment,2099-03" in lines


# --- database -----------------------------------------------------------------


def _coherent_text() -> str:
    return statement_text()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    return tmp_path


def test_account_import_is_idempotent_classified_and_manual(db):
    stmt = cfna.parse_statement_text(_coherent_text())
    rows = cfna.unique_rows([stmt])
    with connect() as conn:
        account_id, created = cfna.ensure_account(conn, LAST4)
        assert created
        again, created_again = cfna.ensure_account(conn, LAST4)
        assert (again, created_again) == (account_id, False)
        acct = dict(conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())
        assert acct["scope"] == "personal" and acct["class"] == "liability" and acct["type"] == "liability"
        assert acct["name"] == "Credit First (CFNA) 8642" and acct["last4"] == "8642"
        assert acct["sync_enabled"] == 0 and acct["notes"].startswith("manual:")

        stats = cfna.import_rows(conn, account_id, rows)
        assert stats["inserted"] == len(rows) and stats["unchanged"] == 0
        stats2 = cfna.import_rows(conn, account_id, rows)
        assert stats2["inserted"] == 0 and stats2["unchanged"] == len(rows)
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ?", (account_id,)).fetchone()[0] == len(rows)

        def category(ref):
            return conn.execute(
                """
                SELECT c.group_name, c.name, p.source FROM transactions t
                JOIN p_classifications p ON p.txn_id = t.id JOIN p_categories c ON c.id = p.category_id
                WHERE t.id LIKE ?
                """,
                (f"cfna:{ref}:%",),
            ).fetchone()

        assert tuple(category("INT-20990305"))[:2] == ("Fees & interest", "Fees & interest")
        assert tuple(category("FEE-20990301"))[:2] == ("Fees & interest", "Fees & interest")
        assert tuple(category("1111111AA00XXAAA1"))[:2] == ("Transfers", "Credit card payment")
        assert tuple(category("4444444DD00AADDD4"))[:2] == ("Transportation", "Auto maintenance")
        assert tuple(category("5555555EE00BBEEE5"))[:2] == ("Food", "Groceries")
        # a refund nets against the purchase category and is manual (a reclassify keeps it)
        refund = category("2222222BB00YYBBB2")
        assert refund[2] == "manual" and refund[0] != "Income"
        from hpgbooks.personal.classify import reclassify_personal

        reclassify_personal(conn)
        assert tuple(category("2222222BB00YYBBB2"))[:2] == tuple(refund)[:2]
        # the stored sign is the ledger sign
        amounts = {r["id"]: r["amount_cents"] for r in conn.execute("SELECT id, amount_cents FROM transactions WHERE account_id = ?", (account_id,))}
        assert amounts["cfna:4444444DD00AADDD4:2099-02-07:-10050"] == -10050
        assert amounts["cfna:1111111AA00XXAAA1:2099-02-11:10000"] == 10000


def test_opening_anchor_and_manual_balance(db):
    stmt = cfna.parse_statement_text(_coherent_text())
    with connect() as conn:
        account_id, _ = cfna.ensure_account(conn, LAST4)
        notes = cfna.ensure_anchors(conn, account_id, [stmt])
        assert notes and cfna.ensure_anchors(conn, account_id, [stmt]) == []  # only when there is none
        cfna.import_rows(conn, account_id, cfna.unique_rows([stmt]))
        cfna.set_manual_balance(conn, account_id, 90000, "2099-04-01", "test")
        msg = cfna.set_manual_balance(conn, account_id, 90000, "2099-04-01", "test")
        assert "already recorded" in msg
        anchors = conn.execute("SELECT as_of_date, balance_cents FROM balance_anchors WHERE account_id = ? ORDER BY as_of_date", (account_id,)).fetchall()
        assert [(a[0], a[1]) for a in anchors] == [("2099-02-05", 105427), ("2099-04-01", 90000)]
        from hpgbooks.balances import snapshot_for

        assert snapshot_for(conn, account_id)["real_balance_cents"] == 90000  # owed; positive


def test_card_payment_pairs_with_the_bank_debit_and_is_not_spending(db):
    stmt = cfna.parse_statement_text(_coherent_text())
    with connect() as conn:
        add_account(conn, "bank1", "cash", last4="1111", name="Fake Checking")
        add_txn(conn, "bank-pay", "bank1", "2099-02-12", -10000, "CREDIT FIRST NA ACH PAYMENT")
        account_id, _ = cfna.ensure_account(conn, LAST4)
        cfna.import_rows(conn, account_id, cfna.unique_rows([stmt]))
        rows = conn.execute(
            """
            SELECT t.id, p.source, c.kind FROM transactions t
            JOIN p_classifications p ON p.txn_id = t.id JOIN p_categories c ON c.id = p.category_id
            WHERE t.id IN ('bank-pay', 'cfna:1111111AA00XXAAA1:2099-02-11:10000')
            """
        ).fetchall()
        assert {r["kind"] for r in rows} == {"transfer"}
        assert {r["source"] for r in rows} == {"transfer"}  # paired legs, not two stray rows


def test_discover_does_not_duplicate_or_clobber_the_manual_account(db):
    with connect() as conn:
        account_id, _ = cfna.ensure_account(conn, LAST4)
        before = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        payload = json.dumps(
            [{"id": "some-finance-uuid", "mask": "8642", "name": "CREDIT CARD", "official_name": "Credit First Card", "type": "credit", "subtype": "credit card", "current_balance": "1.00"}]
        )
        result = discover(conn, payload, scope="personal", as_of="2099-05-01")
        assert result[0]["status"] == "manual-match" and result[0]["id"] == account_id
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == before
        assert conn.execute("SELECT COUNT(*) FROM balance_anchors WHERE account_id = ?", (account_id,)).fetchone()[0] == 0
        # a different card with another last 4 is still registered
        other = json.dumps([{"id": "other-uuid", "mask": "9999", "name": "Other", "type": "credit", "subtype": "credit card"}])
        assert discover(conn, other, scope="personal")[0]["status"] == "new"
        from hpgbooks.accounts_admin import sync_list

        assert account_id not in {row["id"] for row in sync_list(conn)}


# --- script -------------------------------------------------------------------


def _load_script():
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "import_cfna.py"
    spec = importlib.util.spec_from_file_location("import_cfna_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_imports_only_new_rows_and_skips_other_pdfs(db, tmp_path, monkeypatch, capsys):
    folder = tmp_path / "pdfs"
    folder.mkdir()
    (folder / "statement-2099-03.pdf").write_bytes(b"%PDF fake")
    (folder / "statement-2099-04.pdf").write_bytes(b"%PDF fake")
    (folder / "nda.pdf").write_bytes(b"%PDF fake")  # not matched by the default glob
    texts = {"statement-2099-03.pdf": _coherent_text(), "statement-2099-04.pdf": "Some other document"}
    monkeypatch.setattr(cfna, "pdf_text", lambda path: texts[path.name])
    script = _load_script()

    assert script.main([str(folder), "--last4", LAST4, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would insert 7 new rows" in out and "not a CFNA statement" in out
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts WHERE id = ?", (cfna.account_id(LAST4),)).fetchone()[0] == 0  # dry run wrote nothing

    assert script.main([str(folder), "--last4", LAST4, "--balance", "900.00", "--as-of", "2099-04-01"]) == 0
    out = capsys.readouterr().out
    assert "inserted 7 new rows" in out and "balance set" in out
    assert script.main([str(folder), "--last4", LAST4]) == 0
    assert "inserted 0 new rows; 7 already in the ledger" in capsys.readouterr().out

    csv_path = tmp_path / "out.csv"
    assert script.main([str(folder), "--last4", LAST4, "--csv", str(csv_path), "--parse-only"]) == 0
    assert csv_path.read_text().splitlines()[0].startswith("date,reference,description,amount")


def test_script_skips_a_statement_that_does_not_reconcile(db, tmp_path, monkeypatch, capsys):
    folder = tmp_path / "pdfs"
    folder.mkdir()
    (folder / "statement-2099-03.pdf").write_bytes(b"%PDF fake")
    monkeypatch.setattr(cfna, "pdf_text", lambda path: _coherent_text().replace("$70.00", "$71.00"))
    script = _load_script()
    assert script.main([str(folder), "--last4", LAST4]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "inserted 0 new rows" in out
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'cfna_statement'").fetchone()[0] == 0


def test_account_id_and_other_cards_are_skipped(tmp_path, monkeypatch):
    assert cfna.account_id(LAST4) == f"manual-cfna-{LAST4}"
    pdf = tmp_path / "statement-2099-03.pdf"
    pdf.write_bytes(b"%PDF fake")
    monkeypatch.setattr(cfna, "pdf_text", lambda path: _coherent_text())
    statements, skipped = cfna.load_statements([pdf], LAST4)
    assert len(statements) == 1 and skipped == []
    statements, skipped = cfna.load_statements([pdf], "1357")
    assert statements == [] and skipped == [f"{pdf.name}: account ending {LAST4}, expected 1357"]


def test_script_needs_a_last4_from_the_flag_or_the_config(db, tmp_path, monkeypatch, capsys):
    from hpgbooks import config as hpconfig

    folder = tmp_path / "pdfs"
    folder.mkdir()
    (folder / "statement-2099-03.pdf").write_bytes(b"%PDF fake")
    monkeypatch.setattr(cfna, "pdf_text", lambda path: _coherent_text())
    assert hpconfig.get_config().importers.cfna_last4 is None  # the test config sets none
    assert _load_script().main([str(folder), "--dry-run"]) == 2
    assert "--last4 is required" in capsys.readouterr().err
    with pytest.raises(hpconfig.ConfigError):
        hpconfig.build({"importers": {"cfna_last4": "12a4"}})
    hpconfig.override(importers=hpconfig.build({"importers": {"cfna_last4": LAST4}}).importers)
    assert _load_script().main([str(folder)]) == 0
    assert "inserted 7 new rows" in capsys.readouterr().out
    with connect() as conn:
        assert cfna.find_account(conn, LAST4)["id"] == cfna.account_id(LAST4)


def test_a_purchase_is_never_left_paired_with_an_unrelated_deposit(db):
    stmt = cfna.parse_statement_text(_coherent_text())
    with connect() as conn:
        add_account(conn, "bank1", "cash", last4="1111", name="Fake Checking")
        # same size as the KROGER purchase (70.00 on 2099-02-20), 2 days apart, transfer-like wording
        add_txn(conn, "dep", "bank1", "2099-02-22", 7000, "TRANSFER FROM SAVINGS")
        account_id, _ = cfna.ensure_account(conn, LAST4)
        stats = cfna.import_rows(conn, account_id, cfna.unique_rows([stmt]))
        assert len(stats["unpaired"]) == 1
        kroger = conn.execute(
            """
            SELECT p.source, c.name FROM transactions t JOIN p_classifications p ON p.txn_id = t.id
            JOIN p_categories c ON c.id = p.category_id WHERE t.id LIKE 'cfna:5555555EE00BBEEE5:%'
            """
        ).fetchone()
        assert tuple(kroger) == ("rule", "Groceries")
        dep = conn.execute(
            "SELECT p.source, c.name FROM p_classifications p JOIN p_categories c ON c.id = p.category_id WHERE p.txn_id = 'dep'"
        ).fetchone()
        assert dep["name"] == "Savings transfer" and dep["source"] != "manual"
        # a second run changes nothing and finds nothing to undo
        again = cfna.import_rows(conn, account_id, cfna.unique_rows([stmt]))
        assert again["inserted"] == 0
