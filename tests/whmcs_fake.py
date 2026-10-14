"""Fake WHMCS rows for tests. Every person, email, and domain here is made up."""

from __future__ import annotations

import copy
from datetime import date, datetime
from decimal import Decimal

from hpgbooks.whmcs import SOURCE_COLUMNS, build_select

D = Decimal


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _d(text: str) -> date:
    return date.fromisoformat(text)


BASE = {
    "tblcurrencies": [{"id": 1, "code": "USD", "default": 1}],
    "tblproductgroups": [{"id": 1, "name": "Shared"}],
    "tblproducts": [
        {"id": 1, "type": "hostingaccount", "gid": 1, "name": "Starter", "paytype": "recurring", "retired": 0},
        {"id": 2, "type": "hostingaccount", "gid": 1, "name": "Pro", "paytype": "recurring", "retired": 0},
        {"id": 3, "type": "hostingaccount", "gid": 1, "name": "Legacy", "paytype": "recurring", "retired": 1},
    ],
    "tbladdons": [{"id": 1, "name": "Backup", "retired": 0}],
    "tblclients": [
        {"id": 1, "firstname": "Alice", "lastname": "Fakename", "companyname": "", "email": "alice@example.test", "state": "CA", "country": "US", "currency": 1, "credit": D("5.00"), "defaultgateway": "paypal", "datecreated": _d("2025-01-05"), "status": "Active"},
        {"id": 2, "firstname": "Bob", "lastname": "Placeholder", "companyname": "Sample Widgets LLC", "email": "bob@example.test", "state": "NY", "country": "US", "currency": 1, "credit": D("0.00"), "defaultgateway": "cardgw", "datecreated": _d("2025-02-10"), "status": "Closed"},
        {"id": 3, "firstname": "Carol", "lastname": "Testperson", "companyname": "", "email": "carol@example.test", "state": "", "country": "DE", "currency": 1, "credit": D("0.00"), "defaultgateway": "paypal", "datecreated": _d("2025-06-01"), "status": "Active"},
    ],
    "tblhosting": [
        {"id": 101, "userid": 1, "packageid": 1, "server": 1, "regdate": _d("2025-01-05"), "domain": "alice.example.test", "paymentmethod": "paypal", "firstpaymentamount": D("10.00"), "amount": D("10.00"), "billingcycle": "Monthly", "nextduedate": _d("2026-10-05"), "termination_date": "0000-00-00", "domainstatus": "Active"},
        {"id": 102, "userid": 2, "packageid": 2, "server": 1, "regdate": _d("2025-02-10"), "domain": "widgets.example.test", "paymentmethod": "cardgw", "firstpaymentamount": D("120.00"), "amount": D("120.00"), "billingcycle": "Annually", "nextduedate": _d("2026-02-10"), "termination_date": _d("2026-02-10"), "domainstatus": "Cancelled"},
        {"id": 103, "userid": 3, "packageid": 2, "server": 1, "regdate": _d("2025-06-01"), "domain": "carol.example.test", "paymentmethod": "paypal", "firstpaymentamount": D("120.00"), "amount": D("120.00"), "billingcycle": "Annually", "nextduedate": _d("2027-06-01"), "termination_date": "0000-00-00", "domainstatus": "Active"},
        {"id": 104, "userid": 3, "packageid": 3, "server": 1, "regdate": _d("2025-06-01"), "domain": "old.example.test", "paymentmethod": "paypal", "firstpaymentamount": D("30.00"), "amount": D("30.00"), "billingcycle": "Quarterly", "nextduedate": _d("2026-03-01"), "termination_date": "0000-00-00", "domainstatus": "Terminated"},
        {"id": 105, "userid": 1, "packageid": 1, "server": 1, "regdate": _d("2025-01-05"), "domain": "", "paymentmethod": "paypal", "firstpaymentamount": D("0.00"), "amount": D("0.00"), "billingcycle": "Free Account", "nextduedate": "0000-00-00", "termination_date": "0000-00-00", "domainstatus": "Active"},
    ],
    "tblhostingaddons": [
        {"id": 201, "hostingid": 101, "addonid": 1, "userid": 1, "server": 1, "name": "", "recurring": D("2.00"), "billingcycle": "Monthly", "status": "Active", "regdate": _d("2025-03-01"), "nextduedate": _d("2026-10-05"), "termination_date": "0000-00-00", "paymentmethod": "paypal"},
    ],
    "tblinvoices": [
        {"id": 1001, "userid": 1, "invoicenum": "", "date": _d("2026-01-05"), "duedate": _d("2026-01-05"), "datepaid": _dt("2026-01-05 10:00:00"), "date_refunded": None, "date_cancelled": None, "last_capture_attempt": None, "subtotal": D("12.00"), "credit": D("0"), "tax": D("0"), "tax2": D("0"), "total": D("12.00"), "status": "Paid", "paymentmethod": "paypal"},
        {"id": 1002, "userid": 2, "invoicenum": "", "date": _d("2025-02-10"), "duedate": _d("2025-02-10"), "datepaid": _dt("2025-02-10 09:00:00"), "date_refunded": None, "date_cancelled": None, "last_capture_attempt": None, "subtotal": D("120.00"), "credit": D("0"), "tax": D("0"), "tax2": D("0"), "total": D("120.00"), "status": "Paid", "paymentmethod": "cardgw"},
        {"id": 1003, "userid": 3, "invoicenum": "", "date": _d("2026-06-01"), "duedate": _d("2026-06-01"), "datepaid": _dt("2026-06-01 08:00:00"), "date_refunded": None, "date_cancelled": None, "last_capture_attempt": None, "subtotal": D("150.00"), "credit": D("0"), "tax": D("0"), "tax2": D("0"), "total": D("150.00"), "status": "Paid", "paymentmethod": "paypal"},
        {"id": 1004, "userid": 1, "invoicenum": "", "date": _d("2026-02-05"), "duedate": _d("2026-02-05"), "datepaid": "0000-00-00 00:00:00", "date_refunded": None, "date_cancelled": None, "last_capture_attempt": _dt("2026-02-06 03:00:00"), "subtotal": D("12.00"), "credit": D("0"), "tax": D("0"), "tax2": D("0"), "total": D("12.00"), "status": "Unpaid", "paymentmethod": "paypal"},
        {"id": 1005, "userid": 2, "invoicenum": "", "date": _d("2025-03-01"), "duedate": _d("2025-03-01"), "datepaid": _dt("2025-03-01 12:00:00"), "date_refunded": _dt("2025-03-20 12:00:00"), "date_cancelled": None, "last_capture_attempt": None, "subtotal": D("50.00"), "credit": D("0"), "tax": D("0"), "tax2": D("0"), "total": D("50.00"), "status": "Refunded", "paymentmethod": "cardgw"},
        {"id": 1006, "userid": 3, "invoicenum": "", "date": _d("2026-09-20"), "duedate": _d("2026-09-25"), "datepaid": "0000-00-00 00:00:00", "date_refunded": None, "date_cancelled": None, "last_capture_attempt": None, "subtotal": D("30.00"), "credit": D("0"), "tax": D("0"), "tax2": D("0"), "total": D("30.00"), "status": "Unpaid", "paymentmethod": "paypal"},
        {"id": 1007, "userid": 1, "invoicenum": "", "date": _d("2026-03-05"), "duedate": _d("2026-03-05"), "datepaid": "0000-00-00 00:00:00", "date_refunded": None, "date_cancelled": _dt("2026-03-20 00:00:00"), "last_capture_attempt": None, "subtotal": D("12.00"), "credit": D("0"), "tax": D("0"), "tax2": D("0"), "total": D("12.00"), "status": "Cancelled", "paymentmethod": "paypal"},
    ],
    "tblinvoiceitems": [
        {"id": 1, "invoiceid": 1001, "userid": 1, "type": "Hosting", "relid": 101, "amount": D("10.00"), "duedate": _d("2026-01-05"), "paymentmethod": "paypal"},
        {"id": 2, "invoiceid": 1001, "userid": 1, "type": "Addon", "relid": 201, "amount": D("2.00"), "duedate": _d("2026-01-05"), "paymentmethod": "paypal"},
        {"id": 3, "invoiceid": 1002, "userid": 2, "type": "Hosting", "relid": 102, "amount": D("120.00"), "duedate": _d("2025-02-10"), "paymentmethod": "cardgw"},
        {"id": 4, "invoiceid": 1003, "userid": 3, "type": "Hosting", "relid": 103, "amount": D("120.00"), "duedate": _d("2026-06-01"), "paymentmethod": "paypal"},
        {"id": 5, "invoiceid": 1003, "userid": 3, "type": "Hosting", "relid": 104, "amount": D("30.00"), "duedate": _d("2026-06-01"), "paymentmethod": "paypal"},
        {"id": 6, "invoiceid": 1004, "userid": 1, "type": "Hosting", "relid": 101, "amount": D("10.00"), "duedate": _d("2026-02-05"), "paymentmethod": "paypal"},
        {"id": 7, "invoiceid": 1004, "userid": 1, "type": "Addon", "relid": 201, "amount": D("2.00"), "duedate": _d("2026-02-05"), "paymentmethod": "paypal"},
        {"id": 8, "invoiceid": 1005, "userid": 2, "type": "DomainRegister", "relid": 9, "amount": D("50.00"), "duedate": _d("2025-03-01"), "paymentmethod": "cardgw"},
        {"id": 9, "invoiceid": 1006, "userid": 3, "type": "Hosting", "relid": 104, "amount": D("30.00"), "duedate": _d("2026-09-25"), "paymentmethod": "paypal"},
        {"id": 10, "invoiceid": 1007, "userid": 1, "type": "Hosting", "relid": 101, "amount": D("12.00"), "duedate": _d("2026-03-05"), "paymentmethod": "paypal"},
    ],
    "tblaccounts": [
        {"id": 1, "userid": 1, "currency": 0, "gateway": "paypal", "date": _dt("2026-01-05 10:00:00"), "amountin": D("12.00"), "fees": D("0.65"), "amountout": D("0"), "transid": "PPFAKE0001", "invoiceid": 1001, "refundid": 0},
        {"id": 2, "userid": 2, "currency": 0, "gateway": "cardgw", "date": _dt("2025-02-10 09:00:00"), "amountin": D("120.00"), "fees": D("0"), "amountout": D("0"), "transid": "LTFAKE0001", "invoiceid": 1002, "refundid": 0},
        {"id": 3, "userid": 3, "currency": 0, "gateway": "paypal", "date": _dt("2026-06-01 08:00:00"), "amountin": D("150.00"), "fees": D("4.65"), "amountout": D("0"), "transid": "PPFAKE0002", "invoiceid": 1003, "refundid": 0},
        {"id": 4, "userid": 2, "currency": 0, "gateway": "cardgw", "date": _dt("2025-03-01 12:00:00"), "amountin": D("50.00"), "fees": D("0"), "amountout": D("0"), "transid": "LTFAKE0002", "invoiceid": 1005, "refundid": 0},
        {"id": 5, "userid": 2, "currency": 0, "gateway": "cardgw", "date": _dt("2025-03-20 12:00:00"), "amountin": D("0"), "fees": D("0"), "amountout": D("50.00"), "transid": "LTFAKE0003", "invoiceid": 1005, "refundid": 4},
        {"id": 6, "userid": 3, "currency": 0, "gateway": "paypal", "date": _dt("2026-06-10 08:00:00"), "amountin": D("0"), "fees": D("0"), "amountout": D("30.00"), "transid": "PPFAKE0003", "invoiceid": 1003, "refundid": 3},
    ],
    "tblcancelrequests": [
        {"id": 1, "date": _dt("2026-02-01 10:00:00"), "relid": 102, "reason": "Too expensive", "type": "End of Billing Period"},
        {"id": 2, "date": _dt("2026-03-15 10:00:00"), "relid": 104, "reason": "Moving to another host, write me at bob@example.test", "type": "Immediate"},
    ],
    "tblcredit": [{"id": 1, "clientid": 1, "date": _d("2026-01-01"), "amount": D("5.00"), "relid": 0}],
    "tblservers": [{"id": 1, "name": "web1", "monthlycost": D("100.00"), "active": 1, "disabled": 0}],
}


def dataset() -> dict[str, list[dict]]:
    return copy.deepcopy(BASE)


class FakeSource:
    """Stands in for MySQLSource. Records each SELECT so tests can inspect it."""

    def __init__(self, tables: dict[str, list[dict]] | None = None, missing: dict[str, set[str]] | None = None):
        self.tables = tables if tables is not None else dataset()
        self.missing = missing or {}
        self.queries: list[str] = []
        self.closed = False

    def columns(self, table: str) -> set[str]:
        return set(SOURCE_COLUMNS[table]) - self.missing.get(table, set())

    def select(self, table: str) -> list[dict]:
        sql, present = build_select(table, SOURCE_COLUMNS[table], self.columns(table))
        self.queries.append(sql)
        out = []
        for row in self.tables.get(table, []):
            record = dict.fromkeys(SOURCE_COLUMNS[table])
            record.update({col: row.get(col) for col in present})
            out.append(record)
        return out

    def close(self) -> None:
        self.closed = True


def factory_for(sources: dict[str, FakeSource]):
    def factory(brand: dict):
        source = sources.get(brand["name"])
        if source is None:
            raise RuntimeError(f"no fake source for {brand['name']}")
        return source

    return factory
