"""Tailnet host allowlist, web passphrase, and sign-in session.

Loopback (127.0.0.1, localhost, [::1]) stays open. Any other Host must match an
allowlist entry exactly — no wildcards — and a signed-in session. The
passphrase is stored only as a salted scrypt hash in the encrypted database.
It is never logged and never written to the audit log.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
import time
from pathlib import Path

from flask import current_app, request, session

from hpgbooks.db import HpgbooksError, audit, connect, db_path, now_iso

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
ALLOWED_HOSTS_FILENAME = "allowed-hosts"
IDLE_SECONDS = 12 * 60 * 60
MIN_PASSPHRASE_LEN = 12
MAX_PASSPHRASE_LEN = 1024
MAX_PASSPHRASE_BYTES = 4096
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
LOCKOUT_AFTER = 5
LOCKOUT_CAP_SECONDS = 3600
_CONFIGURED_CACHE_SECONDS = 2.0

_HOST_NAME_RE = re.compile(
    r"^(?:(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)$"
)
_IPV6_RE = re.compile(r"^\[[0-9a-f:.]+\]$")


class _Lockout:
    def __init__(self) -> None:
        self.failures = 0
        self.until = 0.0


_lockout = _Lockout()
_configured_at = 0.0
_configured_value = False


def allowed_hosts_path() -> Path:
    """Mode-600 allowlist, next to the database so tests never touch the real data dir."""
    return Path(db_path()).parent / ALLOWED_HOSTS_FILENAME


def _split_host(value: str) -> tuple[str, str | None]:
    """Return (hostname, port or None). IPv6 keeps its brackets. Raises ValueError."""
    host = value.strip().lower()
    if not host or any(ch in host for ch in "*?/@ \t\r\n\\"):
        raise ValueError("invalid host")
    if len(host) > 300:
        raise ValueError("invalid host")
    if host.startswith("["):
        bracket = host.find("]")
        if bracket <= 1:
            raise ValueError("invalid host")
        name = host[: bracket + 1]
        rest = host[bracket + 1 :]
        if rest == "":
            return name, None
        if not rest.startswith(":"):
            raise ValueError("invalid host")
        return name, _canonical_port(rest[1:])
    if host.count(":") > 1:
        raise ValueError("invalid host")
    if ":" in host:
        name, port = host.rsplit(":", 1)
        return name, _canonical_port(port)
    return host, None


def _canonical_port(port: str) -> str:
    if not port.isdigit():
        raise ValueError("invalid host")
    number = int(port)
    if number < 1 or number > 65535 or str(number) != port:
        raise ValueError("invalid host")
    return str(number)


def _valid_ipv4(name: str) -> bool:
    parts = name.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit() or (len(part) > 1 and part.startswith("0")):
            return False
        if int(part) > 255:
            return False
    return True


def _valid_name(name: str) -> bool:
    if name in LOCAL_HOSTS:
        return True
    if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", name):
        return _valid_ipv4(name)
    if name.startswith("["):
        return _IPV6_RE.fullmatch(name) is not None and name.count(":") >= 2
    return _HOST_NAME_RE.fullmatch(name) is not None


def normalize_allowed_host(value: str) -> str:
    """Canonical host, with a port only when one was given. Rejects wildcards."""
    try:
        name, port = _split_host(value)
    except ValueError as exc:
        raise HpgbooksError(f"invalid host {value!r}") from exc
    if not _valid_name(name):
        raise HpgbooksError(f"invalid host {value!r}")
    if port is None:
        return name
    return f"{name}:{port}"


def host_is_loopback(value: str) -> bool:
    try:
        name, _port = _split_host(value)
    except ValueError:
        return False
    return name in LOCAL_HOSTS


def hosts_match(host_header: str, allowed: str) -> bool:
    """Exact hostname match. A missing port on either side does not widen the name.

    An entry without a port matches that host on any port. An entry with a port
    matches that host:port, and the same host when the request omits the port.
    Different ports do not match. Nothing here is a suffix or wildcard match.
    """
    try:
        req_name, req_port = _split_host(host_header)
        allow_name, allow_port = _split_host(allowed)
    except ValueError:
        return False
    if req_name != allow_name:
        return False
    if allow_port is None or req_port is None:
        return True
    return req_port == allow_port


def _chmod_private(path: Path) -> None:
    os.chmod(path, 0o600)


def _file_mode_is_private(path: Path) -> bool:
    mode = path.stat().st_mode & 0o777
    return mode & ~0o600 == 0


def _prepare_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.parent.stat().st_mode
    sticky_shared = bool(mode & 0o1000) and bool(mode & 0o002)
    if not sticky_shared:
        os.chmod(path.parent, 0o700)


def read_allowed_hosts() -> list[str]:
    """Hosts saved in the allowlist file. Missing file means none.

    A mode looser than 600 is refused so another user cannot add a Host.
    """
    path = allowed_hosts_path()
    if not path.exists():
        return []
    if not _file_mode_is_private(path):
        mode = path.stat().st_mode & 0o777
        raise HpgbooksError(
            f"allowed hosts file permissions are too open ({mode:03o}); require 600 or stricter"
        )
    hosts: list[str] = []
    text = path.read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        norm = normalize_allowed_host(line)
        if host_is_loopback(norm):
            continue
        if norm not in hosts:
            hosts.append(norm)
    return hosts


def write_allowed_hosts(hosts: list[str]) -> None:
    path = allowed_hosts_path()
    _prepare_parent(path)
    payload = ("\n".join(hosts) + "\n").encode("utf-8") if hosts else b""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    _chmod_private(tmp)
    os.replace(tmp, path)
    _chmod_private(path)


def add_allowed_hosts(hosts: list[str]) -> list[str]:
    """Merge hosts into the allowlist file. Loopback names are rejected."""
    current = read_allowed_hosts()
    changed = False
    for raw in hosts:
        norm = normalize_allowed_host(raw)
        if host_is_loopback(norm):
            raise HpgbooksError(f"{norm} is already allowed on loopback")
        if norm not in current:
            current.append(norm)
            changed = True
    if changed:
        write_allowed_hosts(current)
    return current


def load_allowed_hosts(extra: list[str] | None = None) -> tuple[str, ...]:
    """Union of HPGBOOKS_ALLOWED_HOSTS, the data file, and extra CLI hosts."""
    found: list[str] = []

    def add(value: str) -> None:
        if not value or not value.strip():
            return
        norm = normalize_allowed_host(value)
        if host_is_loopback(norm) or norm in found:
            return
        found.append(norm)

    # compat: HPBOOKS_ALLOWED_HOSTS is the pre-rename name
    env_hosts = os.environ.get("HPGBOOKS_ALLOWED_HOSTS") or os.environ.get("HPBOOKS_ALLOWED_HOSTS", "")
    for part in env_hosts.split(","):
        add(part)
    for saved in read_allowed_hosts():
        add(saved)
    for item in extra or []:
        add(item)
    return tuple(found)


def request_host() -> str:
    raw = request.environ.get("HTTP_HOST", "") or (request.host or "")
    return raw.strip()


def host_header_allowed() -> bool:
    host = request_host()
    if not host:
        return False
    if host_is_loopback(host):
        return True
    try:
        allowed = current_app.config.get("ALLOWED_HOSTS") or ()
    except RuntimeError:
        allowed = ()
    return any(hosts_match(host, item) for item in allowed)


def request_requires_login() -> bool:
    """Non-loopback hosts always require a session, even if one was already opened."""
    return not host_is_loopback(request_host())


def _table_exists(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'web_passphrase'"
    ).fetchone()
    return row is not None


def clear_passphrase_cache() -> None:
    global _configured_at
    _configured_at = 0.0


def passphrase_is_set(conn) -> bool:
    if not _table_exists(conn):
        return False
    row = conn.execute("SELECT 1 FROM web_passphrase WHERE id = 1").fetchone()
    return row is not None


def passphrase_configured() -> bool:
    """Whether a verifier is stored. Cached briefly so asset requests do not reopen the DB."""
    global _configured_at, _configured_value
    now = time.monotonic()
    if _configured_at and now - _configured_at < _CONFIGURED_CACHE_SECONDS:
        return _configured_value
    with connect(readonly=True) as conn:
        value = passphrase_is_set(conn)
    _configured_at = now
    _configured_value = value
    return value


def _passphrase_row(conn):
    if not _table_exists(conn):
        return None
    return conn.execute(
        "SELECT salt, hash, n, r, p FROM web_passphrase WHERE id = 1"
    ).fetchone()


def _as_bytes(value) -> bytes | None:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytearray):
        value = bytes(value)
    if isinstance(value, bytes):
        return value
    return None


def _scrypt(password: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(password, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)


def verify_passphrase(conn, candidate: str) -> bool:
    """Constant-time check. Always runs scrypt, including when no verifier is stored."""
    row = _passphrase_row(conn)
    salt = b"\x00" * 16
    expected = b"\x00" * SCRYPT_DKLEN
    usable_row = False
    if row is not None and int(row["n"]) == SCRYPT_N and int(row["r"]) == SCRYPT_R and int(row["p"]) == SCRYPT_P:
        stored_salt = _as_bytes(row["salt"])
        stored_hash = _as_bytes(row["hash"])
        if stored_salt and len(stored_salt) >= 8 and stored_hash and len(stored_hash) == SCRYPT_DKLEN:
            salt = stored_salt
            expected = stored_hash
            usable_row = True
    try:
        raw = candidate.encode("utf-8")
    except UnicodeError:
        raw = b""
    usable_input = 0 < len(raw) <= MAX_PASSPHRASE_BYTES and 0 < len(candidate) <= MAX_PASSPHRASE_LEN
    derived = _scrypt(raw if usable_input else b"invalid-login", salt)
    matched = secrets.compare_digest(derived, expected)
    return bool(matched and usable_row and usable_input)


def store_passphrase(conn, passphrase: str, *, actor: str = "cli") -> None:
    if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE_LEN:
        raise HpgbooksError("passphrase must be at least 12 characters")
    if len(passphrase) > MAX_PASSPHRASE_LEN or len(passphrase.encode("utf-8")) > MAX_PASSPHRASE_BYTES:
        raise HpgbooksError("passphrase is too long")
    salt = secrets.token_bytes(16)
    digest = _scrypt(passphrase.encode("utf-8"), salt)
    conn.execute("DELETE FROM web_passphrase")
    conn.execute(
        """
        INSERT INTO web_passphrase (id, salt, hash, n, r, p, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?)
        """,
        (salt, digest, SCRYPT_N, SCRYPT_R, SCRYPT_P, now_iso()),
    )
    # The hash and the passphrase stay out of the audit log.
    audit(conn, "web_passphrase_set", actor=actor)
    clear_passphrase_cache()


def clear_passphrase(conn, *, actor: str = "cli") -> bool:
    if not passphrase_is_set(conn):
        clear_passphrase_cache()
        return False
    conn.execute("DELETE FROM web_passphrase")
    audit(conn, "web_passphrase_clear", actor=actor)
    clear_passphrase_cache()
    return True


def read_new_passphrase() -> str:
    """Prompt, or take HPGBOOKS_NEW_PASSPHRASE. The value is removed from the environment."""
    phrase = os.environ.pop("HPGBOOKS_NEW_PASSPHRASE", None)
    legacy = os.environ.pop("HPBOOKS_NEW_PASSPHRASE", None)  # compat: pre-rename name
    if phrase is None:
        phrase = legacy
    if phrase is None:
        import getpass

        phrase = getpass.getpass("New web passphrase: ")
        again = getpass.getpass("Repeat web passphrase: ")
        if len(phrase) != len(again) or not secrets.compare_digest(phrase, again):
            raise HpgbooksError("passphrases did not match")
    if len(phrase) < MIN_PASSPHRASE_LEN:
        raise HpgbooksError("passphrase must be at least 12 characters")
    if len(phrase) > MAX_PASSPHRASE_LEN:
        raise HpgbooksError("passphrase is too long")
    return phrase


def reset_lockout() -> None:
    _lockout.failures = 0
    _lockout.until = 0.0


def lockout_remaining() -> float | None:
    now = time.monotonic()
    if now < _lockout.until:
        return _lockout.until - now
    return None


def lockout_payload() -> dict | None:
    remaining = lockout_remaining()
    if remaining is None:
        return None
    seconds = max(1, int(math.ceil(remaining)))
    return {
        "ok": False,
        "error": f"Too many attempts. Try again in {seconds} seconds.",
        "retry_after_seconds": seconds,
    }


def record_login_failure() -> None:
    """After 5 failures, back off for 1s, then 2s, 4s, … capped at one hour."""
    _lockout.failures += 1
    if _lockout.failures >= LOCKOUT_AFTER:
        exponent = min(_lockout.failures - LOCKOUT_AFTER, 12)
        delay = min(2**exponent, LOCKOUT_CAP_SECONDS)
        _lockout.until = time.monotonic() + delay


def _drop_expired_auth() -> None:
    session.pop("authenticated", None)
    session.pop("sid", None)
    session.pop("auth_seen", None)
    session.permanent = False
    session.modified = True


def session_is_authenticated() -> bool:
    """Valid login whose last activity is inside the idle window. Activity slides the window."""
    if session.get("authenticated") is not True:
        return False
    seen = session.get("auth_seen")
    now = int(time.time())
    if isinstance(seen, bool) or not isinstance(seen, (int, float)):
        _drop_expired_auth()
        return False
    if now - int(seen) >= IDLE_SECONDS:
        _drop_expired_auth()
        return False
    session["auth_seen"] = now
    session.permanent = True
    session.modified = True
    return True


def establish_session() -> None:
    """Replace the anonymous CSRF session so a pre-login cookie cannot be reused."""
    session.clear()
    session.permanent = True
    session["authenticated"] = True
    session["sid"] = secrets.token_urlsafe(32)
    session["auth_seen"] = int(time.time())
    session["csrf_token"] = secrets.token_urlsafe(32)


def clear_login_session() -> None:
    session.clear()
    session.permanent = False
    session["csrf_token"] = secrets.token_urlsafe(32)
