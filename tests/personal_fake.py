"""Synthetic personal accounts and ~14 months of transactions, for tests and dev only.

Everything here is invented: account ids start with "fake-", names are public
merchant names or obvious placeholders, and amounts come from a fixed formula
or a seeded RNG, so two runs produce the same files.

    from personal_fake import write_inbox
    write_inbox(Path("/tmp/personal_dev/inbox"))       # files for `hpgbooks import`
    accounts_payload()                                  # finance_list_accounts result
"""

from __future__ import annotations

import csv
import io
import json
import random
from datetime import date, timedelta
from pathlib import Path

CHECKING = "fake-p-checking-0001"
SAVINGS = "fake-p-savings-0002"
CARD_A = "fake-p-card-a-0003"
CARD_B = "fake-p-card-b-0004"
MORTGAGE = "fake-p-mortgage-0005"
AUTO = "fake-p-autoloan-0006"
BROKERAGE = "fake-p-brokerage-0007"

ACCOUNTS = [
    {"id": CHECKING, "name": "Everyday Checking", "official_name": "Fake Bank Everyday Checking", "mask": "1111",
     "institution": "Fake Bank", "class": "cash", "type": "depository", "subtype": "checking", "current_balance": "8450.25"},
    {"id": SAVINGS, "name": "High Yield Savings", "official_name": "Fake Bank High Yield Savings", "mask": "2222",
     "institution": "Fake Bank", "class": "cash", "type": "depository", "subtype": "savings", "current_balance": "12010.00"},
    {"id": CARD_A, "name": "Rewards Visa", "official_name": "Fake Card Co Rewards Visa", "mask": "3333",
     "institution": "Fake Card Co", "class": "liability", "type": "credit", "subtype": "credit card", "current_balance": "1412.80"},
    {"id": CARD_B, "name": "Cash Back Card", "official_name": "Fake Card Co Cash Back", "mask": "4444",
     "institution": "Fake Card Co", "class": "liability", "type": "credit", "subtype": "credit card", "current_balance": "655.10"},
    {"id": MORTGAGE, "name": "Home Mortgage", "official_name": "Fake Home Loans Mortgage", "mask": "5555",
     "institution": "Fake Home Loans", "class": "liability", "type": "loan", "subtype": "mortgage", "current_balance": "410000.00"},
    {"id": AUTO, "name": "Auto Loan", "official_name": "Fake Auto Finance Loan", "mask": "6666",
     "institution": "Fake Auto Finance", "class": "liability", "type": "loan", "subtype": "auto", "current_balance": "14200.00"},
    {"id": BROKERAGE, "name": "Brokerage", "official_name": "Fake Invest Brokerage", "mask": "7777",
     "institution": "Fake Invest", "class": "investment", "type": "investment", "subtype": "brokerage", "current_balance": "56300.40"},
]
LIABILITIES = {CARD_A, CARD_B, MORTGAGE, AUTO}

START = date(2025, 8, 1)
END = date(2026, 9, 30)


def accounts_payload(wrapper: bool = True) -> str:
    """A saved finance_list_accounts result (tool-result wrapper with csv text, or a plain list)."""
    if not wrapper:
        return json.dumps(ACCOUNTS)
    columns = ["id", "name", "official_name", "mask", "institution", "class", "type", "subtype", "current_balance", "available_balance"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in ACCOUNTS:
        writer.writerow({**row, "available_balance": ""})
    return json.dumps({"format": "csv", "csv": buffer.getvalue(), "row_count": len(ACCOUNTS), "next_cursor": None})


def _months():
    year, month = START.year, START.month
    while (year, month) <= (END.year, END.month):
        yield year, month
        month += 1
        if month == 13:
            year, month = year + 1, 1


def _d(year: int, month: int, day: int) -> date:
    day = min(day, 28)
    return date(year, month, day)


def transactions() -> list[dict]:
    rng = random.Random(20260930)
    rows: list[dict] = []
    seq = {"n": 0}

    def add(account: str, when: date, amount: float, name: str, merchant: str = "", pending: bool = False):
        if when > END or when < START:
            return
        seq["n"] += 1
        rows.append(
            {
                "id": f"fake-ptx-{seq['n']:05d}",
                "account_id": account,
                "date": when.isoformat(),
                "amount": f"{amount:.2f}",
                "name": name,
                "merchant_name": merchant,
                "pending": pending,
            }
        )

    # Paychecks every other Friday into checking.
    day = date(2025, 8, 1)
    while day <= END:
        add(CHECKING, day, 4300.00, "ACME CORP DES:PAYROLL DIRECT DEP")
        day += timedelta(days=14)
    for year, month in _months():
        # Owner draws from the business account (same day and amount as the business-side draw).
        add(CHECKING, _d(year, month, 10), 2500.00, "Online Banking transfer from CHK 0101 EXAMPLE CO")
        # Mortgage paid from personal checking after the draw.
        add(CHECKING, _d(year, month, 2), -7000.00, "FAKE HOME LOANS DES:MORTGAGE PMT")
        add(MORTGAGE, _d(year, month, 3), 7000.00, "PAYMENT RECEIVED - THANK YOU")
        # Auto loan.
        add(CHECKING, _d(year, month, 15), -450.00, "FAKE AUTO FINANCE DES:AUTO PMT")
        add(AUTO, _d(year, month, 16), 450.00, "PAYMENT RECEIVED - THANK YOU")
        # Savings and brokerage moves.
        add(CHECKING, _d(year, month, 5), -500.00, "TRANSFER TO SAVINGS 2222")
        add(SAVINGS, _d(year, month, 5), 500.00, "TRANSFER FROM CHECKING 1111")
        add(SAVINGS, _d(year, month, 28), 3.00 + month / 10, "INTEREST PAYMENT")
        add(CHECKING, _d(year, month, 6), -1000.00, "FAKE INVEST DES:BROKERAGE TRANSFER")
        add(BROKERAGE, _d(year, month, 7), 1000.00, "CONTRIBUTION FROM CHECKING")
        add(BROKERAGE, _d(year, month, 20), 40.00 + month, "DIVIDEND RECEIVED")
        # Bills.
        add(CARD_B, _d(year, month, 12), -(110 + (month * 7) % 40), "DUKE ENERGY", "Duke Energy")
        add(CARD_B, _d(year, month, 18), -89.99, "COMCAST XFINITY", "Xfinity")
        add(CARD_B, _d(year, month, 22), -75.00, "VERIZON WIRELESS", "Verizon")
        add(CHECKING, _d(year, month, 25), -142.00, "STATE FARM INSURANCE", "State Farm")
        # Subscriptions: Netflix changes price in April 2026; Hulu stops after March 2026.
        netflix = 17.99 if (year, month) >= (2026, 4) else 15.49
        add(CARD_A, _d(year, month, 8), -netflix, "NETFLIX.COM", "Netflix")
        add(CARD_A, _d(year, month, 14), -11.99, "SPOTIFY USA", "Spotify")
        if (year, month) <= (2026, 3):
            add(CARD_A, _d(year, month, 19), -7.99, "HULU 877-8244858", "Hulu")
        # Card payments from checking, paid in full on the 26th.
        add(CHECKING, _d(year, month, 26), -(600.00 + month * 10), "FAKE CARD CO DES:CARD PAYMENT ID:3333")
        add(CARD_A, _d(year, month, 27), 600.00 + month * 10, "PAYMENT THANK YOU")
        add(CHECKING, _d(year, month, 26), -(300.00 + month * 5), "FAKE CARD CO DES:CARD PAYMENT ID:4444")
        add(CARD_B, _d(year, month, 27), 300.00 + month * 5, "PAYMENT THANK YOU")
        # Day-to-day spending.
        for week in range(4):
            add(CARD_A, _d(year, month, 3 + week * 7), -round(rng.uniform(95, 185), 2), "KROGER #0451", "Kroger")
            if week % 2 == 0:
                add(CARD_A, _d(year, month, 4 + week * 7), -round(rng.uniform(35, 60), 2), "SHELL OIL 5744", "Shell")
            add(CARD_A, _d(year, month, 5 + week * 7), -round(rng.uniform(4, 9), 2), "STARBUCKS STORE 1234", "Starbucks")
        add(CARD_B, _d(year, month, 9), -round(rng.uniform(60, 240), 2), "TARGET 00012345", "Target")
        add(CARD_B, _d(year, month, 21), -round(rng.uniform(80, 300), 2), "COSTCO WHSE #0123", "Costco")
        add(CARD_A, _d(year, month, 11), -round(rng.uniform(20, 70), 2), "DOORDASH*CHIPOTLE", "DoorDash")
        add(CARD_A, _d(year, month, 16), -round(rng.uniform(15, 120), 2), "AMZN Mktp US*2K4AB12C3", "")
        add(CARD_A, _d(year, month, 23), -round(rng.uniform(10, 40), 2), "ZZQ LOCAL SHOP 4471", "")
    # Annual subscription, a refund, and a pending charge.
    add(CARD_A, date(2025, 9, 3), -99.99, "FAKE CLOUD STORAGE ANNUAL", "Fake Cloud Storage")
    add(CARD_A, date(2026, 9, 3), -99.99, "FAKE CLOUD STORAGE ANNUAL", "Fake Cloud Storage")
    add(CARD_A, date(2026, 6, 18), 45.50, "AMZN Mktp US*RF7 REFUND", "")
    add(CARD_A, date(2026, 9, 29), -23.40, "STARBUCKS STORE 1234", "Starbucks", pending=True)
    return rows


def _tool_result(rows: list[dict], liability: bool, account_id: str) -> dict:
    id_col = "liability_id" if liability else "account_id"
    columns = ["id", id_col, "date", "amount", "direction", "currency", "name", "merchant_name", "description", "pending"]
    if not liability:
        columns.append("category")
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "id": row["id"],
                id_col: row["account_id"],
                "date": row["date"],
                "amount": row["amount"],
                "direction": "out" if row["amount"].startswith("-") else "in",
                "currency": "USD",
                "name": row["name"],
                "merchant_name": row["merchant_name"],
                "description": "",
                "pending": "true" if row["pending"] else "false",
                "category": "",
            }
        )
    return {
        "format": "csv",
        "csv": buffer.getvalue(),
        "row_count": len(rows),
        "next_cursor": None,
        "_query": {"account_ids": [account_id], "date_from": START.isoformat(), "date_to": END.isoformat()},
    }


def write_inbox(root: Path, day: str = "2026-09-30") -> Path:
    """Write sync/inbox-style files: <root>/<day>/personal/<last4>_1.json. Returns the day dir."""
    folder = root / day / "personal"
    folder.mkdir(parents=True, exist_ok=True)
    by_account: dict[str, list[dict]] = {}
    for row in transactions():
        by_account.setdefault(row["account_id"], []).append(row)
    for acct in ACCOUNTS:
        payload = _tool_result(by_account.get(acct["id"], []), acct["id"] in LIABILITIES, acct["id"])
        (folder / f"{acct['mask']}_1.json").write_text(json.dumps(payload), encoding="utf-8")
    # Outside the day folder so `hpgbooks import <day>` does not read it as transactions.
    (root / f"finance_list_accounts_{day}.json").write_text(accounts_payload(), encoding="utf-8")
    return root / day
