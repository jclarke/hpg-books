"""Read-only import of billing data from the brands' WHMCS databases.

Each brand's MySQL 5.5 database is reached through an SSH tunnel on a local
port. The MySQL user has column-level SELECT grants, so every query names its
columns from the allowlist below and never uses SELECT *. Columns that an older
install lacks are read as NULL.

The MySQL password is read from a mode-600 file next to the database. It is
passed to the driver and nowhere else: not printed, logged, stored, or put in
an error message. Customer names and emails are written only to the encrypted
hpgbooks database.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from hpgbooks.config import get_config
from hpgbooks.db import HpgbooksError, audit, db_path, now_iso, to_cents

TUNNEL_ATTEMPTS = 12
TUNNEL_RETRY_SECONDS = 5
TUNNEL_READY_SECONDS = 20


def configured_brands() -> list[dict]:
    """The WHMCS installs from the config ([[whmcs.brands]]), in display order."""
    return [
        {"name": b.name, "server": b.server, "database": b.database, "port": b.port, "secret": b.secret}
        for b in get_config().whmcs.brands
    ]


def brand_names() -> tuple[str, ...]:
    return get_config().whmcs_brand_names()


# Source columns read per table. Nothing outside this list is ever requested:
# no password hashes, card or bank fields, notes, security answers, street
# addresses, phone numbers, IPs, hostnames, or usernames.
SOURCE_COLUMNS = {
    "tblcurrencies": ("id", "code", "default"),
    "tblclients": ("id", "firstname", "lastname", "companyname", "email", "state", "country", "currency", "credit", "defaultgateway", "datecreated", "status"),
    "tblproductgroups": ("id", "name"),
    "tblproducts": ("id", "type", "gid", "name", "paytype", "retired"),
    "tbladdons": ("id", "name", "retired"),
    "tblhosting": ("id", "userid", "packageid", "server", "regdate", "domain", "paymentmethod", "firstpaymentamount", "amount", "billingcycle", "nextduedate", "termination_date", "domainstatus"),
    "tblhostingaddons": ("id", "hostingid", "addonid", "userid", "server", "name", "recurring", "billingcycle", "status", "regdate", "nextduedate", "termination_date", "paymentmethod"),
    "tblinvoices": ("id", "userid", "invoicenum", "date", "duedate", "datepaid", "date_refunded", "date_cancelled", "last_capture_attempt", "subtotal", "credit", "tax", "tax2", "total", "status", "paymentmethod"),
    "tblinvoiceitems": ("id", "invoiceid", "userid", "type", "relid", "amount", "duedate", "paymentmethod"),
    "tblaccounts": ("id", "userid", "currency", "gateway", "date", "amountin", "fees", "amountout", "transid", "invoiceid", "refundid"),
    "tblcancelrequests": ("id", "date", "relid", "reason", "type"),
    "tblcredit": ("id", "clientid", "date", "amount", "relid"),
    "tblservers": ("id", "name", "monthlycost", "active", "disabled"),
}

WHMCS_TABLES = (
    "whmcs_clients",
    "whmcs_products",
    "whmcs_services",
    "whmcs_invoices",
    "whmcs_invoice_items",
    "whmcs_payments",
    "whmcs_cancel_requests",
    "whmcs_credit",
    "whmcs_servers",
)

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


# --- secrets and tunnels -------------------------------------------------------


def secret_path(filename: str) -> Path:
    """The password files sit next to the database, so tests never read the real ones."""
    return Path(db_path()).parent / filename


def read_secret(filename: str) -> str:
    path = secret_path(filename)
    if not path.exists():
        raise HpgbooksError(f"WHMCS password file is missing: {path}")
    mode = path.stat().st_mode & 0o777
    if mode & ~0o600:
        raise HpgbooksError(f"WHMCS password file permissions are too open ({mode:03o}); require 600 or stricter")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise HpgbooksError(f"could not read WHMCS password file: {path}") from exc
    if not value:
        raise HpgbooksError(f"WHMCS password file is empty: {path}")
    return value


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ssh_command(port: int, server: str) -> list[str]:
    """The tunnel command, without -f so this process owns ssh and can close it."""
    return [
        "ssh",
        "-i",
        os.path.expanduser(get_config().whmcs.ssh_key),
        "-o", "IdentitiesOnly=yes",
        "-o", "HostKeyAlgorithms=+ssh-rsa",
        "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ExitOnForwardFailure=yes",
        "-N",
        "-L", f"{port}:127.0.0.1:3306",
        f"{get_config().whmcs.ssh_user}@{server}",
    ]


class Tunnels:
    """Open SSH tunnels for the length of a sync and close the ones it opened.

    A port that already accepts connections is reused and left alone, because
    another process (or a person) opened it. The source firewalls allow a
    rotating set of addresses, so a failed connection is retried.
    """

    def __init__(
        self,
        *,
        attempts: int = TUNNEL_ATTEMPTS,
        retry_seconds: float = TUNNEL_RETRY_SECONDS,
        ready_seconds: float = TUNNEL_READY_SECONDS,
        popen=subprocess.Popen,
        is_open=port_open,
        sleep=time.sleep,
    ):
        self.attempts = attempts
        self.retry_seconds = retry_seconds
        self.ready_seconds = ready_seconds
        self._popen = popen
        self._is_open = is_open
        self._sleep = sleep
        self.started: dict[int, object] = {}
        self.reused: set[int] = set()
        self.failed: dict[int, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False

    def ensure(self, port: int, server: str) -> str:
        """Return 'reused' or 'opened'. Raises HpgbooksError when every attempt fails."""
        if port in self.started:
            return "opened"
        if port in self.reused or self._is_open(port):
            self.reused.add(port)
            return "reused"
        if port in self.failed:
            raise HpgbooksError(self.failed[port])
        last = "no response"
        for attempt in range(1, self.attempts + 1):
            proc = self._popen(
                ssh_command(port, server),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + self.ready_seconds
            while True:
                if proc.poll() is not None:
                    last = _ssh_error(proc)
                    break
                if self._is_open(port):
                    self.started[port] = proc
                    return "opened"
                if time.monotonic() >= deadline:
                    last = "timed out waiting for the tunnel"
                    _stop(proc)
                    break
                self._sleep(0.25)
            if attempt < self.attempts:
                self._sleep(self.retry_seconds)
        message = f"could not open the SSH tunnel to {server} on port {port} after {self.attempts} attempts ({last})"
        self.failed[port] = message
        raise HpgbooksError(message)

    def close(self) -> None:
        for proc in self.started.values():
            _stop(proc)
        self.started.clear()


def _ssh_error(proc) -> str:
    try:
        err = proc.stderr.read() if proc.stderr else b""
    except Exception:
        err = b""
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    line = (err or "").strip().splitlines()
    return (line[-1] if line else f"ssh exited {proc.returncode}")[:200]


def _stop(proc) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
                proc.wait(timeout=5)
    except Exception:
        pass
    finally:
        stream = getattr(proc, "stderr", None)
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


# --- source access -------------------------------------------------------------


def build_select(table: str, wanted: tuple[str, ...], available: set[str]) -> tuple[str, list[str]]:
    """SELECT with explicit, backquoted columns that exist on this install."""
    if table not in SOURCE_COLUMNS:
        raise HpgbooksError(f"table {table} is not on the WHMCS allowlist")
    allowed = set(SOURCE_COLUMNS[table])
    present = [col for col in wanted if col in available and col in allowed]
    if "id" not in present:
        raise HpgbooksError(f"{table}.id is not readable")
    for name in (table, *present):
        if not _IDENT_RE.fullmatch(name):
            raise HpgbooksError("invalid identifier")
    cols = ", ".join(f"`{col}`" for col in present)
    return f"SELECT {cols} FROM `{table}` ORDER BY `id`", present


class MySQLSource:
    """One brand's WHMCS database, read through PyMySQL. Read-only by grant."""

    def __init__(self, brand: dict, *, attempts: int = 3, sleep=time.sleep):
        import pymysql

        self.brand = brand
        self.database = brand["database"]
        password = read_secret(brand["secret"])
        last: Exception | None = None
        self.conn = None
        try:
            for attempt in range(attempts):
                try:
                    self.conn = pymysql.connect(
                        host="127.0.0.1",
                        port=int(brand["port"]),
                        user=get_config().whmcs.mysql_user,
                        password=password,
                        database=self.database,
                        connect_timeout=15,
                        read_timeout=600,
                        charset="utf8mb4",
                        autocommit=True,
                    )
                    break
                except pymysql.MySQLError as exc:
                    last = exc
                    if attempt + 1 < attempts:
                        sleep(3)
        finally:
            password = ""
        if self.conn is None:
            raise HpgbooksError(f"could not connect to {brand['name']} WHMCS: {_clean_error(last)}")
        self._columns: dict[str, set[str]] = {}

    def columns(self, table: str) -> set[str]:
        if table not in self._columns:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
                    (self.database, table),
                )
                self._columns[table] = {str(row[0]).lower() for row in cur.fetchall()}
        return self._columns[table]

    def select(self, table: str) -> list[dict]:
        wanted = SOURCE_COLUMNS[table]
        sql, present = build_select(table, wanted, self.columns(table))
        with self.conn.cursor() as cur:
            cur.execute(sql)
            out = []
            for row in cur.fetchall():
                record = dict.fromkeys(wanted)
                record.update(zip(present, row))
                out.append(record)
        return out

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


def _clean_error(exc) -> str:
    """Short error text for logs. Never contains the password (it is not in scope here)."""
    if exc is None:
        return "unknown error"
    text = str(exc).replace("\n", " ")
    return text[:300]


# --- value cleanup ---------------------------------------------------------------


def _date(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text or text.startswith("0000") or not _DATE_RE.match(text):
        return None
    return text[:10]


def _ts(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat() + " 00:00:00"
    text = str(value).strip()
    if not text or text.startswith("0000") or not _DATE_RE.match(text):
        return None
    return text[:19]


def _cents(value) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float, Decimal)):
        return to_cents(value)
    try:
        return to_cents(str(value).strip())
    except HpgbooksError:
        return 0


def _int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value, limit: int = 200) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value).strip()
    return text[:limit] if text else None


def _flag(value) -> int:
    if value is None:
        return 0
    text = str(value).strip().lower()
    return 1 if text in ("1", "on", "true", "yes") else 0


# --- mapping source rows to hpgbooks rows ---------------------------------------------


# Credit at or above whmcs.placeholder_credit_cents (default $1,000,000) is a
# placeholder or test entry in WHMCS, not money owed.
def _is_placeholder_credit(cents: int) -> bool:
    return abs(cents) >= get_config().whmcs.placeholder_credit_cents


def _credit_cents(value, skipped: dict[str, int], key: str) -> int:
    """Credit amount in cents, or 0 for a placeholder value (counted under key)."""
    cents = _cents(value)
    if _is_placeholder_credit(cents):
        skipped[key] = skipped.get(key, 0) + 1
        return 0
    return cents


def map_rows(raw: dict[str, list[dict]], skipped: dict[str, int] | None = None) -> dict[str, list[dict]]:
    """Turn raw WHMCS rows into whmcs_* rows (without brand and synced_at).

    Placeholder credit (see whmcs.placeholder_credit_cents) is dropped: tblcredit rows are
    skipped and client or invoice credit becomes 0. Counts go into skipped, if given.
    """
    if skipped is None:
        skipped = {}
    currencies = {_int(row["id"]): _text(row["code"], 8) for row in raw.get("tblcurrencies", [])}
    default_currency = next(
        (_text(row["code"], 8) for row in raw.get("tblcurrencies", []) if _flag(row.get("default"))),
        next(iter(currencies.values()), None),
    )
    groups = {_int(row["id"]): _text(row["name"]) for row in raw.get("tblproductgroups", [])}
    out: dict[str, list[dict]] = {}

    out["whmcs_clients"] = [
        {
            "source_id": _int(row["id"]),
            "first_name": _text(row["firstname"]),
            "last_name": _text(row["lastname"]),
            "company": _text(row["companyname"]),
            "email": _text(row["email"]),
            "status": _text(row["status"], 20),
            "signup_date": _date(row["datecreated"]),
            "country": _text(row["country"], 8),
            "state": _text(row["state"], 60),
            "currency": currencies.get(_int(row["currency"])) or default_currency,
            "credit_cents": _credit_cents(row["credit"], skipped, "client_credit"),
            "default_gateway": _text(row["defaultgateway"], 40),
        }
        for row in raw.get("tblclients", [])
    ]

    products = [
        {
            "kind": "product",
            "source_id": _int(row["id"]),
            "name": _text(row["name"]),
            "group_name": groups.get(_int(row["gid"])),
            "type": _text(row["type"], 40),
            "pay_type": _text(row["paytype"], 40),
            "retired": _flag(row["retired"]),
        }
        for row in raw.get("tblproducts", [])
    ]
    products += [
        {
            "kind": "addon",
            "source_id": _int(row["id"]),
            "name": _text(row["name"]),
            "group_name": "Addons",
            "type": "addon",
            "pay_type": None,
            "retired": _flag(row["retired"]),
        }
        for row in raw.get("tbladdons", [])
    ]
    out["whmcs_products"] = products

    services = [
        {
            "kind": "hosting",
            "source_id": _int(row["id"]),
            "client_id": _int(row["userid"]),
            "product_id": _int(row["packageid"]),
            "parent_id": None,
            "addon_name": None,
            "domain": _text(row["domain"]),
            "status": _text(row["domainstatus"], 20),
            "billing_cycle": _text(row["billingcycle"], 30),
            "amount_cents": _cents(row["amount"]),
            "first_payment_cents": _cents(row["firstpaymentamount"]),
            "reg_date": _date(row["regdate"]),
            "next_due_date": _date(row["nextduedate"]),
            "termination_date": _date(row["termination_date"]),
            "server_id": _int(row["server"]),
            "payment_method": _text(row["paymentmethod"], 40),
        }
        for row in raw.get("tblhosting", [])
    ]
    services += [
        {
            "kind": "addon",
            "source_id": _int(row["id"]),
            "client_id": _int(row["userid"]),
            "product_id": _int(row["addonid"]) or None,
            "parent_id": _int(row["hostingid"]),
            "addon_name": _text(row["name"]),
            "domain": None,
            "status": _text(row["status"], 20),
            "billing_cycle": _text(row["billingcycle"], 30),
            "amount_cents": _cents(row["recurring"]),
            "first_payment_cents": 0,
            "reg_date": _date(row["regdate"]),
            "next_due_date": _date(row["nextduedate"]),
            "termination_date": _date(row["termination_date"]),
            "server_id": _int(row["server"]),
            "payment_method": _text(row["paymentmethod"], 40),
        }
        for row in raw.get("tblhostingaddons", [])
    ]
    out["whmcs_services"] = services

    out["whmcs_invoices"] = [
        {
            "source_id": _int(row["id"]),
            "client_id": _int(row["userid"]),
            "invoice_num": _text(row["invoicenum"], 40),
            "date": _date(row["date"]),
            "due_date": _date(row["duedate"]),
            "date_paid": _date(row["datepaid"]),
            "date_refunded": _date(row["date_refunded"]),
            "date_cancelled": _date(row["date_cancelled"]),
            "last_capture_attempt": _ts(row["last_capture_attempt"]),
            "subtotal_cents": _cents(row["subtotal"]),
            "credit_cents": _credit_cents(row["credit"], skipped, "invoice_credit"),
            "tax_cents": _cents(row["tax"]) + _cents(row["tax2"]),
            "total_cents": _cents(row["total"]),
            "status": _text(row["status"], 20),
            "payment_method": _text(row["paymentmethod"], 40),
        }
        for row in raw.get("tblinvoices", [])
    ]

    out["whmcs_invoice_items"] = [
        {
            "source_id": _int(row["id"]),
            "invoice_id": _int(row["invoiceid"]),
            "client_id": _int(row["userid"]),
            "type": _text(row["type"], 40) or "",
            "rel_id": _int(row["relid"]),
            "amount_cents": _cents(row["amount"]),
            "due_date": _date(row["duedate"]),
            "payment_method": _text(row["paymentmethod"], 40),
        }
        for row in raw.get("tblinvoiceitems", [])
    ]

    payments = []
    for row in raw.get("tblaccounts", []):
        amount_out = _cents(row["amountout"])
        refund_id = _int(row["refundid"]) or 0
        payments.append(
            {
                "source_id": _int(row["id"]),
                "client_id": _int(row["userid"]),
                "date": _date(row["date"]),
                "ts": _ts(row["date"]),
                "gateway": _text(row["gateway"], 40) or "",
                "amount_in_cents": _cents(row["amountin"]),
                "fees_cents": _cents(row["fees"]),
                "amount_out_cents": amount_out,
                "trans_id": _text(row["transid"], 120),
                "invoice_id": _int(row["invoiceid"]),
                "refund_id": refund_id,
                "is_refund": 1 if amount_out > 0 or refund_id > 0 else 0,
                "currency": currencies.get(_int(row["currency"])) or default_currency,
            }
        )
    out["whmcs_payments"] = payments

    out["whmcs_cancel_requests"] = [
        {
            "source_id": _int(row["id"]),
            "date": _date(row["date"]),
            "service_id": _int(row["relid"]),
            "reason": _text(row["reason"], 1000),
            "type": _text(row["type"], 40),
        }
        for row in raw.get("tblcancelrequests", [])
    ]

    credit = []
    for row in raw.get("tblcredit", []):
        amount = _cents(row["amount"])
        if _is_placeholder_credit(amount):
            skipped["credit"] = skipped.get("credit", 0) + 1
            continue
        credit.append(
            {
                "source_id": _int(row["id"]),
                "client_id": _int(row["clientid"]),
                "date": _date(row["date"]),
                "amount_cents": amount,
                "rel_id": _int(row["relid"]),
            }
        )
    out["whmcs_credit"] = credit

    out["whmcs_servers"] = [
        {
            "source_id": _int(row["id"]),
            "name": _text(row["name"]),
            "monthly_cost_cents": _cents(row["monthlycost"]),
            "active": _flag(row["active"]),
            "disabled": _flag(row["disabled"]),
        }
        for row in raw.get("tblservers", [])
    ]
    for table, rows in out.items():
        out[table] = [row for row in rows if row["source_id"] is not None]
    return out


_KEYS = {
    "whmcs_products": ("brand", "kind", "source_id"),
    "whmcs_services": ("brand", "kind", "source_id"),
}


def _upsert(conn, table: str, brand: str, rows: list[dict], run_ts: str) -> tuple[int, int]:
    """Insert or update rows by key, then drop rows this run did not see. Returns (rows, deleted)."""
    if table not in WHMCS_TABLES:
        raise HpgbooksError("unknown WHMCS table")
    keys = _KEYS.get(table, ("brand", "source_id"))
    if rows:
        cols = ["brand", *rows[0].keys(), "synced_at"]
        for name in cols:
            if not _IDENT_RE.fullmatch(name):
                raise HpgbooksError("invalid identifier")
        updates = ", ".join(f"{col} = excluded.{col}" for col in cols if col not in keys)
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET {updates}"
        )
        conn.executemany(sql, ([brand, *row.values(), run_ts] for row in rows))
    deleted = conn.execute(f"DELETE FROM {table} WHERE brand = ? AND synced_at != ?", (brand, run_ts)).rowcount
    return len(rows), max(0, deleted or 0)


# --- invoice-to-plan shares ------------------------------------------------------------


def _product_labels(conn, brand: str) -> dict[int, str]:
    """Product name per id. A name used twice gets its group, then its id, to stay unique."""
    rows = conn.execute(
        "SELECT source_id, name, group_name FROM whmcs_products WHERE brand = ? AND kind = 'product'",
        (brand,),
    ).fetchall()
    base = {int(row["source_id"]): (row["name"] or "").strip() or f"Product {row['source_id']}" for row in rows}
    counts: dict[str, int] = defaultdict(int)
    for name in base.values():
        counts[name] += 1
    labels = {}
    for row in rows:
        pid = int(row["source_id"])
        name = base[pid]
        if counts[name] > 1 and row["group_name"]:
            name = f"{name} ({row['group_name'].strip()})"
        labels[pid] = name
    seen: dict[str, int] = defaultdict(int)
    for label in labels.values():
        seen[label] += 1
    return {pid: (label if seen[label] == 1 else f"{label} #{pid}") for pid, label in labels.items()}


def addon_labels(conn, brand: str) -> dict[int, str]:
    """Catalog addon names. Custom addons (typed per customer) are not in here on purpose."""
    return {
        int(row["source_id"]): (row["name"] or "").strip() or f"Addon {row['source_id']}"
        for row in conn.execute("SELECT source_id, name FROM whmcs_products WHERE brand = ? AND kind = 'addon'", (brand,))
    }


def plan_labels(conn, brand: str) -> dict[tuple[str, int], str]:
    """Report label per (kind, service id). Custom addon names can hold domains or IPs,
    so reports call those 'Addon: custom'; the customer page shows the real name."""
    products = _product_labels(conn, brand)
    addons = addon_labels(conn, brand)
    out = {}
    for row in conn.execute("SELECT kind, source_id, product_id FROM whmcs_services WHERE brand = ?", (brand,)):
        pid = row["product_id"]
        if row["kind"] == "hosting":
            out[("hosting", int(row["source_id"]))] = products.get(int(pid), f"Product {pid}") if pid is not None else "Unknown plan"
        else:
            out[("addon", int(row["source_id"]))] = f"Addon: {addons[int(pid)]}" if pid is not None and int(pid) in addons else "Addon: custom"
    return out


def build_invoice_plans(conn, brand: str) -> int:
    """Rebuild whmcs_invoice_plans for one brand from its invoice items."""
    labels = plan_labels(conn, brand)
    hosting = {}
    for row in conn.execute("SELECT source_id, product_id FROM whmcs_services WHERE brand = ? AND kind = 'hosting'", (brand,)):
        hosting[int(row["source_id"])] = row["product_id"]

    direct: dict[int, dict[tuple[str, int | None], int]] = defaultdict(lambda: defaultdict(int))
    mass: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for row in conn.execute(
        "SELECT invoice_id, type, rel_id, amount_cents FROM whmcs_invoice_items WHERE brand = ? AND invoice_id IS NOT NULL",
        (brand,),
    ):
        invoice_id = int(row["invoice_id"])
        kind = row["type"] or ""
        rel = row["rel_id"]
        cents = int(row["amount_cents"])
        if kind == "Invoice":
            mass[invoice_id].append((rel or 0, cents))
            continue
        direct[invoice_id][_plan_for(kind, rel, hosting, labels)] += cents

    shares: dict[int, dict[tuple[str, int | None], float]] = {}
    for invoice_id, parts in direct.items():
        weights = _weights(parts)
        if weights:
            shares[invoice_id] = weights
    # A mass-payment invoice carries the plans of the invoices it pays.
    for invoice_id, refs in mass.items():
        combined: dict[tuple[str, int | None], float] = defaultdict(float)
        for rel, cents in refs:
            target = shares.get(rel)
            if target:
                for key, share in target.items():
                    combined[key] += cents * share
            else:
                combined[("Other", None)] += cents
        for key, cents in direct.get(invoice_id, {}).items():
            combined[key] += cents
        weights = _weights(combined)
        if weights:
            shares[invoice_id] = weights

    conn.execute("DELETE FROM whmcs_invoice_plans WHERE brand = ?", (brand,))
    conn.executemany(
        "INSERT INTO whmcs_invoice_plans (brand, invoice_id, plan, product_id, share) VALUES (?, ?, ?, ?, ?)",
        (
            (brand, invoice_id, plan, product_id, share)
            for invoice_id, weights in shares.items()
            for (plan, product_id), share in weights.items()
        ),
    )
    return len(shares)


def _plan_for(kind: str, rel, hosting: dict, labels: dict) -> tuple[str, int | None]:
    if kind in ("Hosting", "PromoHosting") or kind.startswith("ProrataProduct"):
        rel = int(rel) if rel is not None else None
        if rel not in hosting or hosting[rel] is None:
            return ("Unknown plan", None)
        return (labels[("hosting", rel)], int(hosting[rel]))
    if kind == "Addon" or kind.startswith("ProrataAddon"):
        label = labels.get(("addon", int(rel))) if rel is not None else None
        return (label or "Addon: unknown", None)
    if kind == "Upgrade":
        return ("Upgrades", None)
    if kind.startswith("Domain") or kind == "PromoDomain":
        return ("Domains", None)
    if kind == "AddFunds":
        return ("Account credit", None)
    return ("Other", None)


def _weights(parts) -> dict:
    total = sum(parts.values())
    if total > 0:
        return {key: value / total for key, value in parts.items() if value}
    positive = {key: value for key, value in parts.items() if value > 0}
    if positive:
        subtotal = sum(positive.values())
        return {key: value / subtotal for key, value in positive.items()}
    return {}


# --- sync ----------------------------------------------------------------------------


def fetch_brand(source) -> dict[str, list[dict]]:
    return {table: source.select(table) for table in SOURCE_COLUMNS}


def write_brand(conn, brand: str, raw: dict[str, list[dict]], run_ts: str) -> dict:
    skipped: dict[str, int] = {}
    mapped = map_rows(raw, skipped)
    counts: dict = {}
    deleted: dict[str, int] = {}
    for table in WHMCS_TABLES:
        rows, gone = _upsert(conn, table, brand, mapped.get(table, []), run_ts)
        counts[table.removeprefix("whmcs_")] = rows
        if gone:
            deleted[table.removeprefix("whmcs_")] = gone
    counts["invoice_plans"] = build_invoice_plans(conn, brand)
    if deleted:
        counts["deleted"] = deleted
    if skipped:
        counts["placeholder_credit"] = skipped
    return counts


def _brand(name: str) -> dict:
    for brand in configured_brands():
        if brand["name"].lower() == name.strip().lower():
            return brand
    raise HpgbooksError(f"unknown brand {name!r}; use one of {', '.join(brand_names())}")


def sync(conn, brands: list[str] | None = None, *, source_factory=None, tunnels=None, actor: str = "cli") -> list[dict]:
    """Full refresh of each brand. One brand failing does not stop the others.

    source_factory(brand) returns an object with select(table) and close().
    tunnels is a Tunnels instance, or None when the ports are already open.
    """
    chosen = [_brand(name) for name in brands] if brands else configured_brands()
    factory = source_factory or MySQLSource
    results = []
    for brand in chosen:
        started = now_iso()
        counts: dict = {}
        error = None
        try:
            if tunnels is not None:
                counts_tunnel = tunnels.ensure(int(brand["port"]), brand["server"])
            else:
                counts_tunnel = "existing"
            source = factory(brand)
            try:
                raw = fetch_brand(source)
            finally:
                source.close()
            # Microseconds keep two runs in the same second apart for stale-row cleanup.
            run_ts = datetime.now(timezone.utc).isoformat()
            conn.execute("SAVEPOINT whmcs_brand")
            try:
                counts = write_brand(conn, brand["name"], raw, run_ts)
            except Exception:
                conn.execute("ROLLBACK TO SAVEPOINT whmcs_brand")
                conn.execute("RELEASE SAVEPOINT whmcs_brand")
                raise
            conn.execute("RELEASE SAVEPOINT whmcs_brand")
            counts["tunnel"] = counts_tunnel
        except Exception as exc:  # recorded per brand; the next brand still runs
            error = exc.args[0] if isinstance(exc, HpgbooksError) and exc.args else f"{type(exc).__name__}: {_clean_error(exc)}"
            error = str(error)[:300]
        status = "ok" if error is None else "error"
        conn.execute(
            """
            INSERT INTO whmcs_sync_log (started_at, finished_at, brand, status, counts_json, error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (started, now_iso(), brand["name"], status, json.dumps(counts, sort_keys=True), error),
        )
        summary = ", ".join(f"{key}={value}" for key, value in counts.items() if isinstance(value, int))
        audit(
            conn,
            "whmcs_sync",
            field=brand["name"],
            new_value=status,
            actor=actor,
            note=(summary if error is None else f"error: {error}")[:500],
        )
        conn.commit()
        results.append({"brand": brand["name"], "status": status, "counts": counts, "error": error})
    return results


def run_sync(conn, brands: list[str] | None = None, *, manage_tunnels: bool = True, actor: str = "cli") -> list[dict]:
    """Open the tunnels this run needs, sync, and close what was opened."""
    if not manage_tunnels:
        return sync(conn, brands, actor=actor)
    with Tunnels() as tunnels:
        return sync(conn, brands, tunnels=tunnels, actor=actor)


# --- status ------------------------------------------------------------------------------


def whmcs_tables_exist(conn) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'whmcs_sync_log'").fetchone()
    return row is not None


def whmcs_ready(conn) -> bool:
    """True once the tables exist and at least one sync has run."""
    if not whmcs_tables_exist(conn):
        return False
    return conn.execute("SELECT 1 FROM whmcs_sync_log LIMIT 1").fetchone() is not None


def sync_status(conn, log_limit: int = 20) -> dict:
    """Last sync per brand, row counts, and recent log entries. No customer data."""
    if not whmcs_ready(conn):
        return {"ready": False, "brands": [], "log": [], "last_sync": None}
    brands = []
    for name in brand_names():
        last = conn.execute(
            "SELECT * FROM whmcs_sync_log WHERE brand = ? ORDER BY id DESC LIMIT 1",
            (name,),
        ).fetchone()
        last_ok = conn.execute(
            "SELECT finished_at FROM whmcs_sync_log WHERE brand = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
            (name,),
        ).fetchone()
        rows = {}
        for table in WHMCS_TABLES:
            rows[table.removeprefix("whmcs_")] = int(
                conn.execute(f"SELECT COUNT(*) FROM {table} WHERE brand = ?", (name,)).fetchone()[0]
            )
        span = conn.execute(
            "SELECT MIN(date), MAX(date), COALESCE(SUM(amount_in_cents), 0) FROM whmcs_payments WHERE brand = ?",
            (name,),
        ).fetchone()
        brands.append(
            {
                "brand": name,
                "last_run": last["finished_at"] if last else None,
                "last_status": last["status"] if last else None,
                "last_error": last["error"] if last else None,
                "last_ok": last_ok["finished_at"] if last_ok else None,
                "rows": rows,
                "first_payment": span[0],
                "last_payment": span[1],
                "payments_in_cents": int(span[2]),
            }
        )
    log = [
        {
            "id": row["id"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "brand": row["brand"],
            "status": row["status"],
            "counts": json.loads(row["counts_json"] or "{}"),
            "error": row["error"],
        }
        for row in conn.execute("SELECT * FROM whmcs_sync_log ORDER BY id DESC LIMIT ?", (log_limit,))
    ]
    oks = [item["last_ok"] for item in brands if item["last_ok"]]
    return {"ready": True, "brands": brands, "log": log, "last_sync": max(oks) if oks else None}
