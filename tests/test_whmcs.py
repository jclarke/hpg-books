"""WHMCS import. Temporary database, temporary key, and fake sources only."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from hpgbooks.cli import main
from hpgbooks.db import MIGRATIONS, HpgbooksError, _apply_migrations, connect, init_db
from hpgbooks.whmcs import (
    SOURCE_COLUMNS,
    Tunnels,
    build_select,
    read_secret,
    ssh_command,
    sync,
    sync_status,
)
from whmcs_fake import D, FakeSource, dataset, factory_for

KEY = "ef" * 32
FAKE_PASSWORD = "not-a-real-password-123"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HPBOOKS_KEY", KEY)
    monkeypatch.setenv("HPBOOKS_DB", str(tmp_path / "books.db"))
    monkeypatch.delenv("HPBOOKS_KEY_FILE", raising=False)
    init_db()
    return tmp_path


def _sync(sources, brands=None):
    with connect() as conn:
        return sync(conn, brands, source_factory=factory_for(sources))


# --- schema ----------------------------------------------------------------------------


def test_migration_adds_whmcs_tables_without_touching_existing_data(env):
    with connect() as conn:
        conn.execute("DELETE FROM schema_version WHERE version = 6")
        for table in [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'whmcs_%'")]:
            conn.execute(f"DROP TABLE {table}")
        before = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    with connect() as conn:
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_version ORDER BY version")]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert versions == [version for version, _sql in MIGRATIONS]
        assert {"whmcs_clients", "whmcs_payments", "whmcs_sync_log", "whmcs_invoice_plans"} <= tables
        assert conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == before
        # Running the migrations again is a no-op.
        _apply_migrations(conn)


# --- source queries ----------------------------------------------------------------------


def test_select_names_columns_and_skips_missing_ones():
    sql, present = build_select("tblhosting", SOURCE_COLUMNS["tblhosting"], set(SOURCE_COLUMNS["tblhosting"]) - {"termination_date"})
    assert "*" not in sql
    assert "`termination_date`" not in sql
    assert sql.startswith("SELECT `id`, `userid`")
    assert "termination_date" not in present


def test_select_refuses_unknown_tables_and_sensitive_columns():
    with pytest.raises(HpgbooksError):
        build_select("tbladmins", ("id",), {"id"})
    sql, present = build_select("tblclients", ("id", "password", "address1", "phonenumber"), {"id", "password", "address1", "phonenumber"})
    assert present == ["id"]
    assert "password" not in sql and "address1" not in sql and "phonenumber" not in sql


def test_allowlist_has_no_sensitive_columns():
    banned = {"password", "cardnum", "cardtype", "bankacct", "bankcode", "notes", "securityqans", "address1", "address2", "phonenumber", "ip", "host", "username", "assignedips", "dedicatedip", "transfersecret"}
    for table, cols in SOURCE_COLUMNS.items():
        assert not banned & set(cols), table


# --- sync ------------------------------------------------------------------------------------


def test_sync_is_idempotent_and_records_a_log(env):
    sources = {"BrandA": FakeSource()}
    first = _sync(sources, ["BrandA"])
    assert first[0]["status"] == "ok", first
    assert first[0]["counts"]["payments"] == 6
    assert all("*" not in sql for sql in sources["BrandA"].queries)
    assert sources["BrandA"].closed
    second = _sync({"BrandA": FakeSource()}, ["BrandA"])
    assert second[0]["status"] == "ok"
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM whmcs_payments").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM whmcs_services").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM whmcs_sync_log WHERE status = 'ok'").fetchone()[0] == 2
        audits = conn.execute("SELECT action, field, new_value, note FROM audit_log WHERE action = 'whmcs_sync'").fetchall()
        assert len(audits) == 2
        assert audits[0]["field"] == "BrandA"
        status = sync_status(conn)
    assert status["ready"] and status["last_sync"]
    rp = next(item for item in status["brands"] if item["brand"] == "BrandA")
    assert rp["rows"]["clients"] == 3
    assert rp["payments_in_cents"] == 33200


def test_sync_updates_changed_rows_and_drops_deleted_ones(env):
    _sync({"BrandA": FakeSource()}, ["BrandA"])
    data = dataset()
    data["tblhosting"][0]["amount"] = 15
    data["tblaccounts"] = data["tblaccounts"][:-1]
    _sync({"BrandA": FakeSource(data)}, ["BrandA"])
    with connect() as conn:
        amount = conn.execute("SELECT amount_cents FROM whmcs_services WHERE kind = 'hosting' AND source_id = 101").fetchone()[0]
        assert amount == 1500
        assert conn.execute("SELECT COUNT(*) FROM whmcs_payments").fetchone()[0] == 5
        log = json.loads(conn.execute("SELECT counts_json FROM whmcs_sync_log ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert log["deleted"] == {"payments": 1}


def test_placeholder_credit_is_skipped_and_removed_on_resync(env):
    # Rows a sync before this rule existed would have imported.
    _sync({"BrandB": FakeSource()}, ["BrandB"])
    with connect() as conn:
        conn.execute(
            "INSERT INTO whmcs_credit (brand, source_id, client_id, date, amount_cents, rel_id, synced_at) "
            "VALUES ('BrandB', 2, 2, '2020-01-01', 500000000, 0, 'old')"
        )
        conn.execute("UPDATE whmcs_clients SET credit_cents = 499950950 WHERE brand = 'BrandB' AND source_id = 2")
        conn.commit()

    data = dataset()
    data["tblcredit"] += [
        {"id": 2, "clientid": 2, "date": "2020-01-01", "amount": D("5000000.00"), "relid": 0},
        {"id": 3, "clientid": 2, "date": "2020-01-02", "amount": D("-1000000.00"), "relid": 0},
        {"id": 4, "clientid": 3, "date": "2020-01-03", "amount": D("999998.50"), "relid": 0},
    ]
    data["tblclients"][1]["credit"] = D("4999509.50")
    data["tblclients"][2]["credit"] = D("1000000.00")
    data["tblinvoices"][0]["credit"] = D("2000000.00")
    result = _sync({"BrandB": FakeSource(data)}, ["BrandB"])[0]

    assert result["counts"]["placeholder_credit"] == {"credit": 2, "client_credit": 2, "invoice_credit": 1}
    with connect() as conn:
        credit = dict(conn.execute("SELECT source_id, amount_cents FROM whmcs_credit WHERE brand = 'BrandB'").fetchall())
        clients = dict(conn.execute("SELECT source_id, credit_cents FROM whmcs_clients WHERE brand = 'BrandB'").fetchall())
        invoice = conn.execute("SELECT credit_cents FROM whmcs_invoices WHERE source_id = 1001").fetchone()[0]
        log = json.loads(conn.execute("SELECT counts_json FROM whmcs_sync_log ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert credit == {1: 500, 4: 99999850}
    assert clients == {1: 500, 2: 0, 3: 0}
    assert invoice == 0
    assert log["placeholder_credit"]["credit"] == 2


def test_brands_are_kept_apart(env):
    _sync({name: FakeSource() for name in ("BrandA", "BrandB", "BrandC")})
    with connect() as conn:
        rows = conn.execute("SELECT brand, COUNT(*) FROM whmcs_clients GROUP BY brand ORDER BY brand").fetchall()
    assert [(row[0], row[1]) for row in rows] == [("BrandA", 3), ("BrandB", 3), ("BrandC", 3)]


def test_missing_columns_on_older_installs_become_null(env):
    source = FakeSource(missing={"tblhosting": {"termination_date"}, "tblinvoices": {"last_capture_attempt", "date_refunded"}})
    result = _sync({"BrandC": source}, ["BrandC"])
    assert result[0]["status"] == "ok"
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM whmcs_services WHERE kind = 'hosting' AND termination_date IS NOT NULL").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM whmcs_invoices WHERE last_capture_attempt IS NOT NULL").fetchone()[0] == 0


def test_zero_dates_are_null_and_amounts_are_cents(env):
    _sync({"BrandA": FakeSource()}, ["BrandA"])
    with connect() as conn:
        free = conn.execute("SELECT next_due_date, termination_date FROM whmcs_services WHERE source_id = 105").fetchone()
        pay = conn.execute("SELECT date, ts, amount_in_cents, fees_cents, is_refund, currency FROM whmcs_payments WHERE source_id = 1").fetchone()
        refund = conn.execute("SELECT is_refund, amount_out_cents FROM whmcs_payments WHERE source_id = 5").fetchone()
    assert free["next_due_date"] is None and free["termination_date"] is None
    assert (pay["date"], pay["ts"], pay["amount_in_cents"], pay["fees_cents"], pay["is_refund"], pay["currency"]) == ("2026-01-05", "2026-01-05 10:00:00", 1200, 65, 0, "USD")
    assert (refund["is_refund"], refund["amount_out_cents"]) == (1, 5000)


def test_invoice_plans_split_by_item(env):
    _sync({"BrandA": FakeSource()}, ["BrandA"])
    with connect() as conn:
        rows = {
            (row["invoice_id"], row["plan"]): round(row["share"], 4)
            for row in conn.execute("SELECT invoice_id, plan, share FROM whmcs_invoice_plans")
        }
    assert rows[(1001, "Starter")] == round(10 / 12, 4)
    assert rows[(1001, "Addon: Backup")] == round(2 / 12, 4)
    assert rows[(1003, "Pro")] == 0.8
    assert rows[(1003, "Legacy")] == 0.2
    assert rows[(1005, "Domains")] == 1.0


def test_one_brand_failing_does_not_stop_the_others(env):
    sources = {"BrandB": FakeSource()}
    results = _sync(sources, ["BrandA", "BrandB"])
    assert [item["status"] for item in results] == ["error", "ok"]
    with connect() as conn:
        log = conn.execute("SELECT brand, status, error FROM whmcs_sync_log ORDER BY id").fetchall()
        assert log[0]["status"] == "error" and "no fake source" in log[0]["error"]
        assert conn.execute("SELECT COUNT(*) FROM whmcs_clients WHERE brand = 'BrandB'").fetchone()[0] == 3


def test_failed_write_rolls_back_that_brand(env, monkeypatch):
    _sync({"BrandA": FakeSource()}, ["BrandA"])
    import hpgbooks.whmcs as module

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(module, "build_invoice_plans", boom)
    data = dataset()
    data["tblclients"] = data["tblclients"][:1]
    results = _sync({"BrandA": FakeSource(data)}, ["BrandA"])
    assert results[0]["status"] == "error"
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM whmcs_clients").fetchone()[0] == 3


def test_customer_names_stay_out_of_logs_and_audit(env):
    _sync({"BrandA": FakeSource()}, ["BrandA"])
    with connect() as conn:
        text = json.dumps([dict(row) for row in conn.execute("SELECT * FROM whmcs_sync_log")])
        text += json.dumps([dict(row) for row in conn.execute("SELECT * FROM audit_log")])
    for secret in ("Alice", "Fakename", "alice@example.test", "Sample Widgets", "example.test"):
        assert secret not in text


# --- secrets ---------------------------------------------------------------------------


def test_secret_file_must_be_private(env):
    path = env / "whmcs_a.secret"
    path.write_text(FAKE_PASSWORD + "\n")
    os.chmod(path, 0o644)
    with pytest.raises(HpgbooksError) as err:
        read_secret("whmcs_a.secret")
    assert FAKE_PASSWORD not in str(err.value)
    os.chmod(path, 0o600)
    assert read_secret("whmcs_a.secret") == FAKE_PASSWORD


def test_missing_secret_is_a_clean_error(env):
    with pytest.raises(HpgbooksError, match="missing"):
        read_secret("whmcs_b.secret")


# --- tunnels -----------------------------------------------------------------------------


class FakeProc:
    def __init__(self, exits: bool = False):
        self.exits = exits
        self.returncode = 255 if exits else None
        self.terminated = False
        self.stderr = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_tunnels_reuse_an_open_port_and_never_kill_it():
    started = []
    tunnels = Tunnels(popen=lambda *a, **k: started.append(a) or FakeProc(), is_open=lambda port: True, sleep=lambda s: None)
    with tunnels:
        assert tunnels.ensure(3307, "billing.example.test") == "reused"
    assert started == []


def test_tunnels_close_what_they_opened():
    procs = []
    opened = set()

    def popen(cmd, **_kwargs):
        proc = FakeProc()
        procs.append(proc)
        opened.add(int(cmd[cmd.index("-L") + 1].split(":")[0]))
        return proc

    tunnels = Tunnels(popen=popen, is_open=lambda port: port in opened, sleep=lambda s: None)
    with tunnels:
        assert tunnels.ensure(3308, "billing.example.test") == "opened"
        assert tunnels.ensure(3308, "billing.example.test") == "opened"
    assert len(procs) == 1 and procs[0].terminated


def test_tunnels_retry_then_give_up():
    procs = []
    tunnels = Tunnels(attempts=3, popen=lambda *a, **k: procs.append(FakeProc(exits=True)) or procs[-1], is_open=lambda port: False, sleep=lambda s: None)
    with pytest.raises(HpgbooksError, match="after 3 attempts"):
        tunnels.ensure(3307, "billing.example.test")
    assert len(procs) == 3
    # A second brand on the same port fails fast instead of retrying again.
    with pytest.raises(HpgbooksError):
        tunnels.ensure(3307, "billing.example.test")
    assert len(procs) == 3


def test_ssh_command_matches_the_documented_options():
    cmd = ssh_command(3307, "billing.example.test")
    assert "-f" not in cmd
    assert cmd[-1] == "hpgbooks@billing.example.test"
    for option in ("IdentitiesOnly=yes", "BatchMode=yes", "ExitOnForwardFailure=yes", "HostKeyAlgorithms=+ssh-rsa", "PubkeyAcceptedAlgorithms=+ssh-rsa"):
        assert option in cmd
    assert "3307:127.0.0.1:3306" in cmd


# --- CLI ------------------------------------------------------------------------------------


def test_cli_status_before_and_after_sync(env, capsys):
    assert main(["whmcs", "status"]) == 0
    assert "not been synced" in capsys.readouterr().out
    _sync({"BrandA": FakeSource()}, ["BrandA"])
    assert main(["whmcs", "status"]) == 0
    out = capsys.readouterr().out
    assert "BrandA" in out and "332.00" in out
    assert "Alice" not in out


def test_cli_sync_reports_errors_without_secrets(env, monkeypatch, capsys):
    path = env / "whmcs_a.secret"
    path.write_text(FAKE_PASSWORD)
    os.chmod(path, 0o600)
    import hpgbooks.whmcs as module

    class Refused:
        def __init__(self, brand):
            read_secret(brand["secret"])
            raise HpgbooksError("could not connect to BrandA WHMCS: (2003, connection refused)")

    monkeypatch.setattr(module, "MySQLSource", Refused)
    assert main(["whmcs", "sync", "--brand", "branda", "--no-tunnel"]) == 1
    captured = capsys.readouterr()
    assert "connection refused" in captured.err
    assert FAKE_PASSWORD not in captured.out + captured.err
    with connect() as conn:
        rows = json.dumps([dict(row) for row in conn.execute("SELECT * FROM whmcs_sync_log")])
    assert FAKE_PASSWORD not in rows
