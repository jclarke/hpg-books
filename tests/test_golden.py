"""Business output is byte-for-byte what master produced before personal mode."""

from __future__ import annotations

import pytest

from golden_fixture import GOLDEN_DIR, build_business_ledger, capture_outputs, freeze_today, set_env


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    set_env(monkeypatch, tmp_path)
    freeze_today(monkeypatch)
    build_business_ledger(tmp_path)
    return tmp_path


def _assert_matches_golden(outputs: dict[str, bytes]) -> None:
    expected = {path.name: path.read_bytes() for path in GOLDEN_DIR.iterdir()}
    assert sorted(outputs) == sorted(expected)
    different = [name for name in sorted(expected) if outputs[name] != expected[name]]
    assert different == [], f"business output changed: {different}"


def test_business_outputs_match_golden(ledger):
    _assert_matches_golden(capture_outputs())


def _add_personal_rows_directly(tmp_path) -> None:
    """Personal accounts (via discover) and raw personal transactions that look like business ones."""
    from golden_fixture import run_cli
    from hpgbooks.db import connect
    from personal_fake import CARD_A, CHECKING, accounts_payload, transactions

    payload = tmp_path / "accounts.json"
    payload.write_text(accounts_payload(), encoding="utf-8")
    code, _out, err = run_cli(["accounts", "discover", str(payload), "--as-of", "2026-09-30"])
    assert code == 0, err
    with connect() as conn:
        rows = transactions()
        # Business-looking text on personal accounts must stay out of business reports.
        rows.append({"id": "fake-px-1", "account_id": CHECKING, "date": "2026-03-20", "amount": "-333.00",
                     "name": "SAMPLE REWARDS DES:EPAY ID:5", "merchant_name": "", "pending": False})
        rows.append({"id": "fake-px-2", "account_id": CARD_A, "date": "2026-03-21", "amount": "-44.00",
                     "name": "FAKE MART #123", "merchant_name": "Fake Mart", "pending": False})
        for row in rows:
            conn.execute(
                """
                INSERT INTO transactions (id, account_id, date, amount_cents, name, merchant_name, pending,
                                          first_seen_at, last_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'x', 'x', 'x')
                """,
                (row["id"], row["account_id"], row["date"], round(float(row["amount"]) * 100),
                 row["name"], row["merchant_name"], 1 if row["pending"] else 0),
            )
        # A stray business-table classification on a personal row is ignored too.
        conn.execute(
            "INSERT INTO classifications (txn_id, business_tag, category, source, confidence, note, updated_at) "
            "VALUES ('fake-px-1', 'general', 'Office/Other', 'manual', 1.0, '', 'x')"
        )
        conn.execute("UPDATE audit_log SET ts = '2026-09-30T12:00:00+00:00'")


def test_business_outputs_unchanged_with_personal_rows_in_the_same_db(ledger):
    _add_personal_rows_directly(ledger)
    _assert_matches_golden(capture_outputs())


def test_business_outputs_unchanged_after_full_personal_import(ledger):
    """Discover, import 14 months of fake personal data, run personal rules, transfers, and edits."""
    from golden_fixture import run_cli
    from hpgbooks.db import connect
    from hpgbooks.personal import actions
    from hpgbooks.personal.analytics import refresh_recurring
    from hpgbooks.personal.classify import reclassify_personal
    from personal_fake import write_inbox

    day = write_inbox(ledger / "inbox")
    assert run_cli(["accounts", "discover", str(ledger / "inbox" / "finance_list_accounts_2026-09-30.json"), "--as-of", "2026-09-30"])[0] == 0
    code, _out, err = run_cli(["import", str(day)])
    assert code == 0, err
    with connect() as conn:
        reclassify_personal(conn)
        refresh_recurring(conn)
        groceries = conn.execute("SELECT id FROM p_categories WHERE name = 'Groceries'").fetchone()[0]
        actions.set_budget(conn, {"category_id": groceries, "amount_cents": 50000}, actor="test")
        conn.execute("UPDATE audit_log SET ts = '2026-09-30T12:00:00+00:00'")
    _assert_matches_golden(capture_outputs())
