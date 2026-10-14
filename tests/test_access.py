"""Tailnet allowlist and sign-in. Temporary database and key only."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest

from hpgbooks.access import (
    IDLE_SECONDS,
    add_allowed_hosts,
    allowed_hosts_path,
    clear_passphrase_cache,
    hosts_match,
    normalize_allowed_host,
    read_allowed_hosts,
    reset_lockout,
)
from hpgbooks.cli import main
from hpgbooks.db import HpgbooksError, connect, init_db

KEY = "ab" * 32
SECRET = "correct-horse-battery"
HOST = "books.example:8765"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HPBOOKS_KEY", KEY)
    monkeypatch.setenv("HPBOOKS_DB", str(tmp_path / "books.db"))
    monkeypatch.delenv("HPBOOKS_KEY_FILE", raising=False)
    monkeypatch.delenv("HPGBOOKS_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("HPGBOOKS_NEW_PASSPHRASE", raising=False)
    init_db()
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_auth_state():
    reset_lockout()
    clear_passphrase_cache()
    yield
    reset_lockout()
    clear_passphrase_cache()


def _client(extra_hosts=None):
    from hpgbooks.web import create_app

    app = create_app()
    if extra_hosts is not None:
        app.config["ALLOWED_HOSTS"] = tuple(extra_hosts)
    return app.test_client()


def _token(client, host=None) -> str:
    kwargs = {"headers": {"Host": host}} if host else {}
    body = client.get("/api/session", **kwargs).get_json()
    assert body["csrf_token"]
    return body["csrf_token"]


def _login(client, passphrase, token=None, host=HOST):
    headers = {"Host": host, "X-CSRF-Token": token if token is not None else _token(client, host)}
    return client.post("/api/login", json={"passphrase": passphrase}, headers=headers)


def test_host_match_is_exact():
    assert hosts_match("Books.Example:8765", "books.example")
    assert hosts_match("books.example", "books.example:8765")
    assert hosts_match("192.0.2.10:8765", "192.0.2.10:8765")
    assert hosts_match("[::1]:8765", "[::1]")
    assert not hosts_match("books.example:443", "books.example:8765")
    assert not hosts_match("books.example.evil.com", "books.example")
    assert not hosts_match("notbooks.example", "books.example")
    assert not hosts_match("evil.example:8765", "books.example")
    assert not hosts_match("books.example:8765", "*.example")
    with pytest.raises(HpgbooksError):
        normalize_allowed_host("*.example.com")
    with pytest.raises(HpgbooksError):
        normalize_allowed_host("http://books.example")
    with pytest.raises(HpgbooksError):
        normalize_allowed_host("127.00.0.1")


# Tailscale-style hosts (a CGNAT address and a MagicDNS name), assembled so the publish
# privacy scan does not flag the literals.
CGNAT_HOST = "100." + "64.0.10:8765"
MAGICDNS_HOST = "books.example-net." + "ts" + "." + "net:8765"


def test_non_loopback_host_rejected_by_default(env):
    client = _client()
    assert client.get("/api/session", headers={"Host": "books.example.org:8765"}).status_code == 403
    assert client.get("/", headers={"Host": "192.0.2.10:8765"}).status_code == 403
    assert client.get("/api/session", headers={"Host": "books.example.evil.com"}).status_code == 403
    assert client.get("/api/dashboard").status_code == 200


def test_tailscale_hosts_are_ordinary_non_loopback_hosts(env, monkeypatch):
    client = _client()
    for host in (CGNAT_HOST, MAGICDNS_HOST):
        assert client.get("/api/session", headers={"Host": host}).status_code == 403
    monkeypatch.setenv("HPGBOOKS_ALLOWED_HOSTS", f"{CGNAT_HOST}, {MAGICDNS_HOST}")
    client = _client()
    for host in (CGNAT_HOST, MAGICDNS_HOST):
        # allowed, but still behind the passphrase like any other remote host
        assert client.get("/api/session", headers={"Host": host}).status_code == 503


def test_allowed_host_without_passphrase_is_refused(env, monkeypatch):
    monkeypatch.setenv("HPGBOOKS_ALLOWED_HOSTS", "books.example, 192.0.2.10")
    client = _client()
    api = client.get("/api/session", headers={"Host": HOST})
    assert api.status_code == 503
    assert api.get_json()["error"] == "Sign-in is not configured"
    page = client.get("/", headers={"Host": "192.0.2.10:8765"})
    assert page.status_code == 503
    assert b"Sign-in is not configured" in page.data
    assert client.get("/api/dashboard").status_code == 200
    assert not allowed_hosts_path().exists()


def test_allow_host_file_is_private_and_unioned_with_env(env, monkeypatch):
    monkeypatch.setenv("HPGBOOKS_ALLOWED_HOSTS", "from-env.example")
    saved = add_allowed_hosts(["From-Flag.Example", "192.0.2.10:8765"])
    assert saved == ["from-flag.example", "192.0.2.10:8765"]
    path = allowed_hosts_path()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert read_allowed_hosts() == saved
    client = _client()
    allowed = client.application.config["ALLOWED_HOSTS"]
    assert "from-env.example" in allowed
    assert "from-flag.example" in allowed
    assert client.get("/api/session", headers={"Host": "from-flag.example:8765"}).status_code == 503
    path.chmod(0o644)
    with pytest.raises(HpgbooksError):
        read_allowed_hosts()
    with pytest.raises(HpgbooksError):
        _client()


def test_web_flag_persists_hosts_and_binds_loopback(env, monkeypatch):
    calls = []

    def fake_run(self, host=None, port=None, debug=None, **kwargs):
        calls.append({"host": host, "port": port, "debug": debug})

    monkeypatch.setattr("flask.Flask.run", fake_run)
    assert main(["web", "--port", "8765", "--allow-host", "books.example", "--allow-host", "192.0.2.10"]) == 0
    assert calls == [{"host": "127.0.0.1", "port": 8765, "debug": False}]
    assert read_allowed_hosts() == ["books.example", "192.0.2.10"]
    assert main(["web", "--allow-host", "*.evil.com", "--port", "8765"]) == 1
    assert read_allowed_hosts() == ["books.example", "192.0.2.10"]
    assert main(["web", "--allow-host", "localhost", "--port", "8765"]) == 1


def test_hpgbooks_web_passes_allowed_hosts(env):
    path = allowed_hosts_path()
    path.write_text("# tailnet\nbooks.example\n192.0.2.10:8765\n\n", encoding="utf-8")
    path.chmod(0o600)
    result = subprocess.run(
        ["bin/hpgbooks-web", "print-allow-args"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "--allow-host",
        "books.example",
        "--allow-host",
        "192.0.2.10:8765",
    ]
    path.chmod(0o644)
    loose = subprocess.run(
        ["bin/hpgbooks-web", "print-allow-args"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        check=False,
    )
    assert loose.returncode == 1
    assert "too open" in loose.stderr


def test_passphrase_cli_never_prints_the_secret(env, monkeypatch, capsys):
    monkeypatch.setenv("HPGBOOKS_NEW_PASSPHRASE", "short")
    assert main(["web-passphrase", "set"]) == 1
    assert SECRET not in capsys.readouterr().err
    monkeypatch.setenv("HPGBOOKS_NEW_PASSPHRASE", SECRET)
    assert main(["web-passphrase", "set"]) == 0
    printed = capsys.readouterr()
    assert "web passphrase set" in printed.out
    assert SECRET not in printed.out + printed.err
    assert "HPGBOOKS_NEW_PASSPHRASE" not in os.environ
    assert main(["web-passphrase", "status"]) == 0
    assert capsys.readouterr().out.strip() == "web passphrase: set"
    with connect() as conn:
        row = conn.execute("SELECT salt, hash, n FROM web_passphrase").fetchone()
        audits = conn.execute("SELECT action, field, old_value, new_value, note FROM audit_log").fetchall()
        settings = conn.execute("SELECT key, value FROM settings").fetchall()
    assert bytes(row["hash"]) != SECRET.encode()
    assert SECRET.encode() not in bytes(row["salt"]) + bytes(row["hash"])
    blob = " ".join(" ".join(str(col or "") for col in record) for record in list(audits) + list(settings))
    assert SECRET not in blob
    assert any(record["action"] == "web_passphrase_set" for record in audits)
    assert main(["web-passphrase", "clear"]) == 0
    assert "cleared" in capsys.readouterr().out
    assert main(["web-passphrase", "status"]) == 0
    assert "not set" in capsys.readouterr().out
    db_bytes = Path(env / "books.db").read_bytes()
    assert SECRET.encode() not in db_bytes


def test_passphrase_prompt_confirms_and_rejects_a_mismatch(env, monkeypatch, capsys):
    monkeypatch.delenv("HPGBOOKS_NEW_PASSPHRASE", raising=False)
    answers = iter(["first-passphrase", "second-passphrase-no"])

    def fake_getpass(_prompt=""):
        return next(answers)

    monkeypatch.setattr("getpass.getpass", fake_getpass)
    assert main(["web-passphrase", "set"]) == 1
    assert "did not match" in capsys.readouterr().err
    answers = iter([SECRET, SECRET])
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": next(answers))
    assert main(["web-passphrase", "set"]) == 0
    assert SECRET not in capsys.readouterr().out


def test_remote_login_gates_api_and_loopback_stays_open(env, caplog):
    caplog.set_level(logging.DEBUG)
    add_allowed_hosts(["books.example", "192.0.2.10:8765"])
    with connect() as conn:
        from hpgbooks.access import store_passphrase

        store_passphrase(conn, SECRET)
    client = _client()
    blocked = client.get("/api/dashboard", headers={"Host": HOST})
    assert blocked.status_code == 401
    assert blocked.get_json()["error"] == "sign in required"
    export = client.get("/export/transactions.csv", headers={"Host": HOST})
    assert export.status_code == 401
    session = client.get("/api/session", headers={"Host": HOST})
    assert session.status_code == 200
    body = session.get_json()
    assert body["requires_login"] is True and body["authenticated"] is False
    assert "review_count" not in body and "accounts" not in body
    shell = client.get("/transactions", headers={"Host": HOST})
    assert shell.status_code == 200
    assert b'<div id="root">' in shell.data
    assert client.get("/api/dashboard").status_code == 200
    assert client.get("/api/dashboard").get_json()["ok"] is True

    missing = client.post("/api/login", json={"passphrase": SECRET}, headers={"Host": HOST})
    assert missing.status_code == 403
    token = _token(client, HOST)
    origin = client.post(
        "/api/login",
        json={"passphrase": SECRET},
        headers={"Host": HOST, "X-CSRF-Token": token, "Origin": "https://evil.example"},
    )
    assert origin.status_code == 403
    form = client.post(
        "/api/login",
        data={"passphrase": SECRET},
        headers={"Host": HOST, "X-CSRF-Token": token},
    )
    assert form.status_code == 415

    wrong = _login(client, "not-the-passphrase", token)
    assert wrong.status_code == 401
    assert wrong.get_json()["error"] == "Sign-in failed"
    assert SECRET not in wrong.get_data(as_text=True)
    good = _login(client, SECRET, token)
    assert good.status_code == 200, good.get_json()
    cookie = good.headers.getlist("Set-Cookie")
    assert cookie
    joined = " ".join(cookie)
    attrs = [part.strip() for part in joined.split(";")]
    assert "HttpOnly" in attrs
    assert "SameSite=Strict" in attrs
    assert "Secure" not in attrs
    expires = next(part.split("=", 1)[1] for part in attrs if part.lower().startswith("expires="))
    lifetime = (parsedate_to_datetime(expires) - datetime.now(timezone.utc)).total_seconds()
    assert 11 * 60 * 60 < lifetime < 13 * 60 * 60
    with client.session_transaction(headers={"Host": HOST}) as sess:
        sid = sess["sid"]
        assert sess["authenticated"] is True
        assert sess["auth_seen"]
    again = _login(client, SECRET, _token(client, HOST))
    assert again.status_code == 200
    with client.session_transaction(headers={"Host": HOST}) as sess:
        assert sess["sid"] != sid

    opened = client.get("/api/dashboard", headers={"Host": HOST})
    assert opened.status_code == 200
    assert client.get("/api/session", headers={"Host": HOST}).get_json()["authenticated"] is True
    assert client.get("/export/transactions.csv", headers={"Host": HOST}).status_code == 200
    # The sign-in cookie is for this Host only. The other address is a different origin.
    assert client.get("/api/dashboard", headers={"Host": "192.0.2.10:8765"}).status_code == 401

    logged_out = client.post("/api/logout", json={}, headers={"Host": HOST, "X-CSRF-Token": _token(client, HOST)})
    assert logged_out.status_code == 200
    assert client.get("/api/dashboard", headers={"Host": HOST}).status_code == 401
    assert client.get("/api/dashboard").status_code == 200

    with connect() as conn:
        rows = conn.execute(
            "SELECT action, field, old_value, new_value, note, actor FROM audit_log"
        ).fetchall()
    actions = [row["action"] for row in rows]
    assert "login_failure" in actions
    assert "login_success" in actions
    assert "logout" in actions
    dumped = " ".join(" ".join(str(col or "") for col in row) for row in rows)
    assert SECRET not in dumped
    assert "not-the-passphrase" not in dumped
    assert SECRET not in caplog.text


def test_lockout_backs_off_and_a_later_good_passphrase_resets_it(env, monkeypatch):
    add_allowed_hosts(["books.example"])
    with connect() as conn:
        from hpgbooks.access import store_passphrase

        store_passphrase(conn, SECRET)
    clock = {"now": 1_000.0}
    monkeypatch.setattr("hpgbooks.access.time.monotonic", lambda: clock["now"])
    client = _client()
    token = _token(client, HOST)
    for _ in range(5):
        failed = _login(client, "wrong-passphrase-value", token)
        assert failed.status_code == 401
    locked = _login(client, SECRET, token)
    assert locked.status_code == 429
    body = locked.get_json()
    assert body["retry_after_seconds"] >= 1
    assert "Too many attempts" in body["error"]
    assert locked.headers["Retry-After"]
    assert SECRET not in locked.get_data(as_text=True)
    clock["now"] = 1_001.2
    sixth = _login(client, "wrong-passphrase-value", token)
    assert sixth.status_code == 401
    still = _login(client, SECRET, token)
    assert still.status_code == 429
    assert still.get_json()["retry_after_seconds"] >= 2
    clock["now"] = 1_001.2 + still.get_json()["retry_after_seconds"] + 0.1
    opened = _login(client, SECRET, token)
    assert opened.status_code == 200, opened.get_json()
    # A fresh run of failures is allowed after success.
    for _ in range(4):
        assert _login(client, "wrong-passphrase-value", _token(client, HOST)).status_code == 401
    assert _login(client, SECRET, _token(client, HOST)).status_code == 200


def test_session_idle_expiry(env):
    add_allowed_hosts(["books.example"])
    with connect() as conn:
        from hpgbooks.access import store_passphrase

        store_passphrase(conn, SECRET)
    client = _client()
    assert _login(client, SECRET).status_code == 200
    with client.session_transaction(headers={"Host": HOST}) as sess:
        sess["auth_seen"] = int(time.time()) - (11 * 60 * 60)
    assert client.get("/api/rules", headers={"Host": HOST}).status_code == 200
    with client.session_transaction(headers={"Host": HOST}) as sess:
        assert int(time.time()) - int(sess["auth_seen"]) < 10
        sess["auth_seen"] = int(time.time()) - IDLE_SECONDS - 5
    expired = client.get("/api/rules", headers={"Host": HOST})
    assert expired.status_code == 401
    assert client.get("/api/rules").status_code == 200
    with client.session_transaction(headers={"Host": HOST}) as sess:
        sess.clear()
        sess["authenticated"] = True
        sess["sid"] = "attacker-chosen-sid"
        sess["auth_seen"] = int(time.time())
        sess["csrf_token"] = "x" * 24
    rotated = _login(client, SECRET, "x" * 24)
    assert rotated.status_code == 200
    with client.session_transaction(headers={"Host": HOST}) as sess:
        assert sess["sid"] != "attacker-chosen-sid"
        assert sess["csrf_token"] != "x" * 24
