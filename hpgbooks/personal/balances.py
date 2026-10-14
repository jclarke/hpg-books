"""Personal account balances on any date, from balance anchors plus ledger activity.

Method (the same anchor idea as hpgbooks/balances.py, extended to history):

- A row moves an account by its amount (cash, investment, other assets) or by
  minus its amount (cards and loans, whose balance is the amount owed).
- On a date with an anchor on or before it, the balance is the latest such
  anchor plus every active row dated after that anchor through the date
  (pending rows included, since a posted anchor does not contain them).
- On a date before the first anchor, the first anchor is walked back by the
  posted rows between the date and the anchor.
- With no anchor, the balance is the running sum of imported activity.

Investment accounts change with the market, which the ledger does not record,
so between anchors their history carries the earlier anchor forward plus
contributions; a new anchor steps the line to the provider's value.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, timedelta

STALE_DAYS = 7


def effect(acct: dict, amount_cents: int) -> int:
    return -int(amount_cents) if acct.get("type") == "liability" else int(amount_cents)


class AccountSeries:
    def __init__(self, acct: dict, txns: list[dict], anchors: list[dict]):
        self.acct = acct
        self.anchors = sorted(anchors, key=lambda row: (row["as_of_date"], row.get("created_at") or "", row["id"]))
        ordered = sorted(txns, key=lambda row: (row["date"], row["id"]))
        self.dates = [row["date"] for row in ordered]
        self.all_cum = []
        self.posted_cum = []
        total = posted = 0
        for row in ordered:
            change = effect(acct, row["amount_cents"])
            total += change
            if not int(row.get("pending") or 0):
                posted += change
            self.all_cum.append(total)
            self.posted_cum.append(posted)
        self.last_txn = self.dates[-1] if self.dates else None

    def _cum(self, series: list[int], day: str) -> int:
        index = bisect_right(self.dates, day)
        return series[index - 1] if index else 0

    def balance_at(self, day: str) -> int:
        if not self.anchors:
            return self._cum(self.all_cum, day)
        before = [row for row in self.anchors if row["as_of_date"] <= day]
        if before:
            anchor = before[-1]
            return int(anchor["balance_cents"]) + self._cum(self.all_cum, day) - self._cum(self.all_cum, anchor["as_of_date"])
        anchor = self.anchors[0]
        return int(anchor["balance_cents"]) - (self._cum(self.posted_cum, anchor["as_of_date"]) - self._cum(self.posted_cum, day))

    def last_updated(self) -> str | None:
        candidates = [value for value in (self.last_txn, self.anchors[-1]["as_of_date"] if self.anchors else None) if value]
        return max(candidates) if candidates else None

    def stale(self, today: date) -> bool:
        updated = self.last_updated()
        if updated is None:
            return True
        return date.fromisoformat(updated) < today - timedelta(days=STALE_DAYS)


def is_liability(acct: dict) -> bool:
    return acct.get("type") == "liability"


def load_series(conn, accounts: dict[str, dict]) -> dict[str, AccountSeries]:
    if not accounts:
        return {}
    ids = list(accounts)
    marks = ", ".join("?" for _ in ids)
    txns: dict[str, list[dict]] = {account_id: [] for account_id in ids}
    for row in conn.execute(
        f"""
        SELECT id, account_id, date, amount_cents, pending FROM transactions
        WHERE status = 'active' AND account_id IN ({marks})
        """,
        ids,
    ):
        txns[row["account_id"]].append({key: row[key] for key in row.keys()})
    anchors: dict[str, list[dict]] = {account_id: [] for account_id in ids}
    for row in conn.execute(
        f"SELECT id, account_id, as_of_date, balance_cents, created_at FROM balance_anchors WHERE account_id IN ({marks})",
        ids,
    ):
        anchors[row["account_id"]].append({key: row[key] for key in row.keys()})
    return {account_id: AccountSeries(accounts[account_id], txns[account_id], anchors[account_id]) for account_id in ids}
