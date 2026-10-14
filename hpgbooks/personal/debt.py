"""Debt payoff what-if: the personal cards and installment loans with a balance owed.

Read-only. Balances, minimums, due dates, and APR text come from payments.build
in personal mode, the same rows the Bills and Accounts pages show, so a business
account can never appear here. Mortgages are left out: they are not something a
card-consolidation loan would pay off. The simulation itself runs in the browser.
"""

from __future__ import annotations

import re

from hpgbooks import payments as pay

PREFERRED = re.compile(r"purchase|revolving|standard", re.I)
NUMBER = re.compile(r"(\d+(?:\.\d+)?)\s*%?")
MATURES_ISO = re.compile(r"matur\w*\s+(?:on\s+)?(\d{4}-\d{2}-\d{2})", re.I)
MATURES_MONTH = re.compile(r"matur\w*\s+(?:on\s+)?(\d{1,2})/(\d{4})\b", re.I)


def parse_apr(text: str | None) -> float | None:
    """The purchase / standard APR as a number from free text like '22.99% purchase / 28.49% cash'.

    A rate labelled purchase, revolving, or standard wins; otherwise the first number.
    None when there is no plausible rate (blank, 'n/a', above 100).
    """
    if not text or not str(text).strip():
        return None
    parts = [part for part in re.split(r"[/;,|]|\band\b", str(text)) if NUMBER.search(part)]
    if not parts:
        return None
    chosen = next((part for part in parts if PREFERRED.search(part)), parts[0])
    value = float(NUMBER.search(chosen).group(1))
    return value if 0 <= value <= 100 else None


def parse_maturity(notes: str | None) -> str | None:
    """'matures 2027-01-05' (or 'matures 12/2054', read as the 1st) from the notes, else None."""
    if not notes:
        return None
    match = MATURES_ISO.search(notes)
    if match:
        return match.group(1)
    match = MATURES_MONTH.search(notes)
    if match and 1 <= int(match.group(1)) <= 12:
        return f"{match.group(2)}-{int(match.group(1)):02d}-01"
    return None


def _is_mortgage(row: dict, acct: dict) -> bool:
    text = f"{acct.get('subtype') or ''} {row['label']}".lower()
    return "mortgage" in text or "heloc" in text


def build(conn, today: str | None = None) -> dict:
    from hpgbooks.scope import accounts_in

    data = pay.build(conn, "personal", today)
    accounts = {acct["id"]: acct for acct in accounts_in(conn, "personal")}
    cards: list[dict] = []
    loans: list[dict] = []
    mortgages = 0
    for row in data["rows"]:
        acct = accounts.get(row["id"])
        if acct is None or acct["scope"] != "personal" or row["balance_cents"] <= 0:
            continue
        kind = "loan" if row["class"] == "loan" else "card"
        if kind == "loan" and _is_mortgage(row, acct):
            mortgages += 1
            continue
        minimum = row["min_payment_cents"] or row["stored_min_cents"] or None
        item = {
            "id": row["id"],
            "label": row["label"],
            "last4": row["last4"],
            "institution": row["institution"],
            "kind": kind,
            "balance_cents": int(row["balance_cents"]),
            "apr": parse_apr(row["apr"]),
            "apr_text": row["apr"] or "",
            "minimum_payment_cents": int(minimum) if minimum else None,
            "due_date": row["effective_due_date"],
        }
        if kind == "loan":
            item["maturity_date"] = parse_maturity(row["notes"])
            loans.append(item)
        else:
            cards.append(item)
    cards.sort(key=lambda item: (-(item["apr"] or 0), -item["balance_cents"]))
    loans.sort(key=lambda item: -item["balance_cents"])
    return {
        "as_of": data["summary"]["today"],
        "cards": cards,
        "loans": loans,
        "mortgages_excluded": mortgages,
        "totals": {
            "card_balance_cents": sum(item["balance_cents"] for item in cards),
            "loan_balance_cents": sum(item["balance_cents"] for item in loans),
            "card_minimums_cents": sum(item["minimum_payment_cents"] or 0 for item in cards),
            "cards_missing_minimum": sum(1 for item in cards if item["minimum_payment_cents"] is None),
            "cards_missing_apr": sum(1 for item in cards if item["apr"] is None),
        },
    }
