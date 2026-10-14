"""Small hand-built personal ledgers for unit tests. Fake ids and names only."""

from __future__ import annotations

from golden_fixture import freeze_today, set_env

from hpgbooks.db import connect, init_db, now_iso


def make_db(tmp_path, monkeypatch) -> None:
    set_env(monkeypatch, tmp_path)
    freeze_today(monkeypatch)
    init_db()


def add_account(conn, account_id: str, account_class: str = "cash", *, scope: str = "personal", last4: str = "", name: str = "") -> None:
    account_type = "liability" if account_class in ("liability", "loan") else "cash"
    conn.execute(
        """
        INSERT INTO accounts (id, name, type, last4, institution, scope, class, created_at)
        VALUES (?, ?, ?, ?, 'Fake Bank', ?, ?, ?)
        """,
        (account_id, name or f"Fake {account_class} {last4}", account_type, last4 or None, scope, account_class, now_iso()),
    )


def add_txn(conn, txn_id: str, account_id: str, day: str, cents: int, name: str, *, merchant: str = "", pending: bool = False) -> None:
    conn.execute(
        """
        INSERT INTO transactions (id, account_id, date, amount_cents, name, merchant_name, pending,
                                  first_seen_at, last_seen_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'x', 'x', 'x')
        """,
        (txn_id, account_id, day, cents, name, merchant, 1 if pending else 0),
    )


def classify_all(conn) -> None:
    from hpgbooks.personal.classify import apply_transfers, classify_new_personal

    ids = [row["id"] for row in conn.execute(
        "SELECT t.id FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE a.scope = 'personal'"
    )]
    classify_new_personal(conn, ids)
    apply_transfers(conn)


def business_class(conn, txn_id: str, tag: str, category: str) -> None:
    from hpgbooks.classify import write_classification

    write_classification(conn, txn_id, tag, category, "manual", 1.0, "", None, overwrite_manual=True, actor="test", audit_write=False)


def anchor(conn, account_id: str, day: str, cents: int) -> None:
    conn.execute(
        "INSERT INTO balance_anchors (account_id, as_of_date, balance_cents, source, created_at) VALUES (?, ?, ?, 'finance', ?)",
        (account_id, day, cents, now_iso()),
    )


def cat_id(conn, group: str, name: str) -> int:
    return int(conn.execute("SELECT id FROM p_categories WHERE group_name = ? AND name = ?", (group, name)).fetchone()[0])


__all__ = ["make_db", "add_account", "add_txn", "classify_all", "business_class", "anchor", "cat_id", "connect"]


def build_full_ledger(tmp_path) -> None:
    """Golden business ledger plus the fake personal accounts and 14 months of personal rows."""
    from golden_fixture import build_business_ledger, run_cli
    from personal_fake import write_inbox

    build_business_ledger(tmp_path)
    day = write_inbox(tmp_path / "inbox")
    code, _out, err = run_cli(["accounts", "discover", str(tmp_path / "inbox" / "finance_list_accounts_2026-09-30.json"), "--as-of", "2026-09-30"])
    assert code == 0, err
    code, _out, err = run_cli(["import", str(day)])
    assert code == 0, err
