"""Apple Card CSV importer and statement tie-out. Synthetic data only."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from personal_helpers import add_account, add_txn, make_db

from hpgbooks import applecard
from hpgbooks.accounts_admin import discover
from hpgbooks.db import connect

HEADER = "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By\n"
# 2099-01: purchases, an identical twin pair and interest; 2099-02: a payment and a refund
CSV_TEXT = HEADER + "\n".join(
    [
        '02/10/2099,02/11/2099,"ACH DEPOSIT INTERNET TRANSFER FROM ACCOU",Payment,Payment,Payment,-100.00,"Fake Person"',
        '02/05/2099,02/06/2099,"PLANET FITNESS 123 MAIN ST ANYTOWN XX USA",Planet Fitness,Other,Purchase,-20.00,"Fake Person"',
        '01/31/2099,01/31/2099,"INTEREST CHARGE",Interest Charge,Interest,Interest,12.00,"Fake Person"',
        '01/20/2099,01/21/2099,"PATREON* MEMBERSHIP 1 FAKE ST",Patreon* Membership,Other,Purchase,1.00,"Fake Person"',
        '01/20/2099,01/21/2099,"PATREON* MEMBERSHIP 1 FAKE ST",Patreon* Membership,Other,Purchase,1.00,"Fake Person"',
        '01/12/2099,01/13/2099,"KROGER 0042 ANYTOWN XX USA",Kroger,Grocery,Purchase,70.00,"Fake Person"',
        '01/05/2099,01/06/2099,"PLANET FITNESS 123 MAIN ST ANYTOWN XX USA",Planet Fitness,Other,Purchase,20.00,"Fake Person"',
    ]
) + "\n"
# the 02/05 Planet Fitness row is a refund: a Purchase with a negative amount


def rows():
    return applecard.parse_csv_text(CSV_TEXT)


def test_signs_kinds_and_occurrence_index():
    parsed = rows()
    by_kind = {}
    for r in parsed:
        by_kind.setdefault(r.kind, []).append(r)
    assert [r.amount_cents for r in by_kind["payment"]] == [10000]  # ledger: payment received is positive
    assert by_kind["interest"][0].amount_cents == -1200
    assert sorted(r.amount_cents for r in by_kind["purchase"]) == [-7000, -2000, -100, -100]
    assert by_kind["refund"][0].amount_cents == 2000  # negative CSV purchase = refund = positive in ledger
    twins = [r for r in parsed if r.description.startswith("PATREON")]
    assert sorted(r.occurrence for r in twins) == [0, 1] and len({r.txn_id for r in twins}) == 2
    assert all(r.statement == r.clearing_date[:7] for r in parsed)
    assert not any(r.problem for r in parsed)


def test_sign_problems_are_flagged():
    bad = applecard.parse_csv_text(HEADER + '01/01/2099,01/02/2099,"ACH DEPOSIT",Payment,Payment,Payment,50.00,"X"\n')
    assert "check the sign" in bad[0].problem
    with pytest.raises(applecard.AppleError):
        applecard.parse_csv_text("a,b\n1,2\n")


# --- statements ---------------------------------------------------------------

def statement_text(label_month="Jan", day_end=31, prev="$1,000.00", total="$1,083.00", payments="", charges="$71.00", interest="$12.00", pay_rows="", tx_rows=""):
    return f"""
                                                         Statement
Apple Card Customer
Fake Person, fake@example.com                           {label_month} 1 — {label_month} {day_end}, 2099
Your {label_month} Balance
as of {label_month} {day_end}, 2099
                     Previous Monthly Balance                 {prev}
                     Previous Total Balance                   {prev}
                     Total Balance                            {total}
Payments
Date                         Description                                                          Amount
{pay_rows}{payments}
Transactions
Date                         Description                                         Daily Cash        Amount
{tx_rows}
Total Daily Cash this month                                                                         $0.20
Total charges, credits and returns                                                                  {charges}
Interest Charged
Interest charge                                                                                     {interest}
Total interest for this month                                                                       {interest}
Apple Card is issued by Goldman Sachs Bank USA.
"""


JAN = statement_text(
    tx_rows=(
        "01/05/2099                   PLANET FITNESS 123 MAIN ST ANYTOWN XX USA          2%     $0.40       $20.00\n"
        "01/12/2099                   KROGER 0042 ANYTOWN XX USA          2%     $1.40       $70.00\n"
        "01/20/2099                   PATREON* MEMBERSHIP 1 FAKE ST          1%     $0.01       $1.00\n"
        "01/20/2099                   PATREON* MEMBERSHIP 1 FAKE ST          1%     $0.01       $1.00\n"
    ),
    charges="$92.00",
    total="$1,104.00",
)
FEB = statement_text(
    label_month="Feb",
    day_end=28,
    prev="$1,104.00",
    total="$984.00",
    payments="\nTotal payments for this period                                                      -$100.00",
    pay_rows="02/10/2099                   ACH Deposit Internet transfer from account ending in 0000        -$100.00\n",
    tx_rows="02/05/2099                   PLANET FITNESS 123 MAIN ST ANYTOWN XX USA          2%     $0.40       -$20.00\n",
    charges="-$20.00",
    interest="$0.00",
)


def test_statement_parse_and_tie_out():
    # FEB uses interest $0.00 but the CSV has no February interest, so everything ties
    jan = applecard.parse_statement_text(JAN)
    feb = applecard.parse_statement_text(FEB)
    assert (jan.label, feb.label) == ("2099-01", "2099-02")
    assert jan.previous_balance == 100000 and jan.total_balance == 110400 and jan.interest == 1200
    assert feb.payments == -10000 and len(feb.rows) == 2
    reports = applecard.tie_out(rows(), [jan, feb])
    assert all(r["ok"] for r in reports), reports


def test_tie_out_reports_mismatches():
    wrong = CSV_TEXT.replace("70.00", "71.00")
    reports = applecard.tie_out(applecard.parse_csv_text(wrong), [applecard.parse_statement_text(JAN), applecard.parse_statement_text(FEB)])
    assert not reports[0]["ok"] and any("charges" in p for p in reports[0]["problems"])
    broken = applecard.parse_statement_text(FEB.replace("$984.00", "$990.00"))
    assert any("add up" in p for p in applecard.tie_out(rows(), [applecard.parse_statement_text(JAN), broken])[1]["problems"])


# --- database -----------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    return tmp_path


def _cat(conn, like):
    return conn.execute(
        """SELECT c.group_name, c.name, p.source FROM transactions t JOIN p_classifications p ON p.txn_id = t.id
           JOIN p_categories c ON c.id = p.category_id WHERE t.description || t.name LIKE ? AND t.account_id = 'manual-apple-card'""",
        (f"%{like}%",),
    ).fetchall()


def test_import_is_idempotent_and_classified(db):
    parsed = rows()
    with connect() as conn:
        account_id, created = applecard.ensure_account(conn)
        assert created and account_id == "manual-apple-card"
        assert applecard.ensure_account(conn) == (account_id, False)
        acct = dict(conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())
        assert (acct["scope"], acct["class"], acct["type"], acct["name"]) == ("personal", "liability", "liability", "Apple Card")
        assert acct["sync_enabled"] == 0 and acct["notes"].startswith("manual:")

        stats = applecard.import_rows(conn, account_id, parsed)
        assert stats["inserted"] == len(parsed) == 7 and stats["unchanged"] == 0
        again = applecard.import_rows(conn, account_id, parsed)
        assert again["inserted"] == 0 and again["unchanged"] == 7
        # an overlapping export with one extra row inserts only that row
        extra = applecard.parse_csv_text(CSV_TEXT + '03/01/2099,03/02/2099,"KROGER 0042 ANYTOWN XX USA",Kroger,Grocery,Purchase,5.00,"X"\n')
        assert applecard.import_rows(conn, account_id, extra)["inserted"] == 1
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ?", (account_id,)).fetchone()[0] == 8

        assert tuple(_cat(conn, "INTEREST")[0])[:2] == ("Fees & interest", "Fees & interest")
        assert tuple(_cat(conn, "ACH DEPOSIT")[0])[:2] == ("Transfers", "Credit card payment")
        assert {tuple(r)[:2] for r in _cat(conn, "PATREON")} == {("Subscriptions", "Subscriptions")}
        assert {tuple(r)[:2] for r in _cat(conn, "KROGER")} == {("Food", "Groceries")}
        fitness = sorted(tuple(r) for r in _cat(conn, "PLANET FITNESS"))
        assert [f[:2] for f in fitness] == [("Health", "Fitness")] * 2  # purchase by rule, refund nets against it
        assert sorted(f[2] for f in fitness) == ["manual", "rule"]
        amounts = {r["id"]: r["amount_cents"] for r in conn.execute("SELECT id, amount_cents FROM transactions WHERE account_id = ?", (account_id,))}
        assert sorted(amounts.values()) == [-7000, -2000, -1200, -500, -100, -100, 2000, 10000]


def test_account_id_comes_from_the_config(db):
    from hpgbooks import config as hpconfig

    assert applecard.account_id() == "manual-apple-card"  # the default
    hpconfig.override(importers=hpconfig.ImportersConfig(applecard_account_id="manual-sample-card"))
    assert applecard.account_id() == "manual-sample-card"
    with connect() as conn:
        assert applecard.ensure_account(conn) == ("manual-sample-card", True)
        assert applecard.find_account(conn)["id"] == "manual-sample-card"
    assert hpconfig.build({"importers": {"applecard_account_id": "x-card"}}).importers.applecard_account_id == "x-card"


def test_payment_pairs_with_the_bank_debit_and_no_double_count(db):
    with connect() as conn:
        add_account(conn, "bank1", "cash", last4="1111", name="Fake Checking")
        add_txn(conn, "bank-pay", "bank1", "2099-02-11", -10000, "APPLE CARD GSBANK PAYMENT")
        account_id, _ = applecard.ensure_account(conn)
        applecard.import_rows(conn, account_id, rows())
        legs = conn.execute(
            """SELECT p.source, c.kind FROM p_classifications p JOIN p_categories c ON c.id = p.category_id
               WHERE p.txn_id = 'bank-pay' OR p.txn_id IN (SELECT id FROM transactions WHERE description LIKE '%cleared 2099-02-11%' AND account_id = ?)""",
            (account_id,),
        ).fetchall()
        assert len(legs) == 2 and {r["kind"] for r in legs} == {"transfer"} and {r["source"] for r in legs} == {"transfer"}


def test_existing_linked_account_is_used_and_not_duplicated(db):
    with connect() as conn:
        add_account(conn, "finance-apple", "liability", name="Apple Card")
        # a row the linked feed already holds
        add_txn(conn, "fin-1", "finance-apple", "2099-01-12", -7000, "KROGER 0042")
        account_id, created = applecard.ensure_account(conn)
        assert (account_id, created) == ("finance-apple", False)
        stats = applecard.import_rows(conn, account_id, rows())
        assert stats["overlaps"] == 1 and stats["inserted"] == 6
        assert conn.execute("SELECT COUNT(*) FROM accounts WHERE name LIKE '%Apple%'").fetchone()[0] == 1


def test_discover_matches_the_manual_account_by_name(db):
    with connect() as conn:
        account_id, _ = applecard.ensure_account(conn)
        before = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        import json

        payload = json.dumps([{"id": "finance-uuid", "name": "Apple Card", "type": "credit", "subtype": "credit card", "current_balance": "5.00"}])
        result = discover(conn, payload, scope="personal", as_of="2099-05-01")
        assert result[0]["status"] == "manual-match" and result[0]["id"] == account_id
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == before
        assert conn.execute("SELECT COUNT(*) FROM balance_anchors WHERE account_id = ?", (account_id,)).fetchone()[0] == 0


def test_opening_anchor_and_balance_check(db):
    parsed = rows()
    jan = applecard.parse_statement_text(JAN)
    with connect() as conn:
        account_id, _ = applecard.ensure_account(conn)
        notes = applecard.ensure_opening_anchor(conn, account_id, parsed, [jan])
        assert notes and applecard.ensure_opening_anchor(conn, account_id, parsed, [jan]) == []
        applecard.import_rows(conn, account_id, parsed)
        from hpgbooks.balances import snapshot_for

        # 1,000.00 owed + 70 + 20 + 12 + 2 - 20 - 100 ... the ledger sum drives the balance
        owed = 100000 - sum(r.amount_cents for r in parsed)
        assert snapshot_for(conn, account_id)["real_balance_cents"] == owed
        anchor = conn.execute("SELECT as_of_date FROM balance_anchors WHERE account_id = ?", (account_id,)).fetchone()[0]
        assert anchor == "2099-01-04"  # the day before the first row


def _script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "import_applecard.py"
    spec = importlib.util.spec_from_file_location("import_applecard_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_end_to_end(db, tmp_path, monkeypatch, capsys):
    csv_path = tmp_path / "t.csv"
    csv_path.write_text(CSV_TEXT)
    folder = tmp_path / "pdfs"
    folder.mkdir()
    (folder / "statement-2099-01.pdf").write_bytes(b"x")
    (folder / "statement-2099-02.pdf").write_bytes(b"x")
    texts = {"statement-2099-01.pdf": JAN, "statement-2099-02.pdf": FEB}
    monkeypatch.setattr(applecard, "pdf_text", lambda p: texts[p.name])
    script = _script()
    assert script.main([str(csv_path), "--statements", str(folder), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would insert 7 new rows" in out and out.count("OK") >= 2
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts WHERE id = 'manual-apple-card'").fetchone()[0] == 0
    assert script.main([str(csv_path), "--statements", str(folder), "--balance", "1000.00", "--as-of", "2099-03-03"]) == 0
    assert "inserted 7 new rows" in capsys.readouterr().out
    assert script.main([str(csv_path)]) == 0
    assert "inserted 0 new rows; 7 already" in capsys.readouterr().out
    # a statement mismatch stops the import
    bad = tmp_path / "bad.csv"
    bad.write_text(CSV_TEXT.replace("70.00", "99.00"))
    assert script.main([str(bad), "--statements", str(folder)]) == 1
    assert "MISMATCH" in capsys.readouterr().out
