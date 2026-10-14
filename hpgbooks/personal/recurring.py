"""Recurring bills, subscriptions, and income.

detect() is a pure function over personal rows (see classify.load_personal):

- Rows are grouped by merchant key and direction (in/out). Transfers and
  owner-draw funding are skipped. Within a merchant, amounts are clustered:
  sorted amounts start a new cluster when one is more than 35% above the last,
  so a price change stays in one series while two plans of one merchant split.
- A cluster's cadence comes from the median gap between charge dates:
  weekly 5-9 days, biweekly 12-16, monthly 25-36, quarterly 80-100,
  annual 340-390. At least two thirds of the gaps must sit inside that band
  (a few days of drift are fine). Weekly, biweekly, and monthly need at least
  3 occurrences; quarterly and annual need 2.
- next_expected = last date + the cadence step (whole months for monthly and
  longer, so the 31st does not walk); typical = median of the last 3 amounts.
- price_change: latest amount differs from the one before by more than 3% or $1,
  for a series whose previous (up to 6) amounts were steady (within 3% or $1).
- may_be_cancelled: no charge for more than 1.5 cadences after the last one.
- new: first seen within 60 days of today.
- possible_duplicate: two active series share a merchant key (two plans or
  accounts with one merchant). A charge cannot show whether a subscription is
  used, so that is the only "unused" signal offered.
- kind: income for inflows; subscription for the Subscriptions / Entertainment
  groups; otherwise bill.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import median

CADENCES = {
    # name: (min gap, max gap, step days for weekly cadences or months, monthly-equivalent factor)
    "weekly": (5, 9, 7, 52 / 12),
    "biweekly": (12, 16, 14, 26 / 12),
    "monthly": (25, 36, 1, 1.0),
    "quarterly": (80, 100, 3, 1 / 3),
    "annual": (340, 390, 12, 1 / 12),
}
MIN_COUNT = {"weekly": 3, "biweekly": 3, "monthly": 3, "quarterly": 2, "annual": 2}
NOMINAL_DAYS = {"weekly": 7, "biweekly": 14, "monthly": 30, "quarterly": 91, "annual": 365}
CLUSTER_STEP = 1.35
SUBSCRIPTION_GROUPS = ("Subscriptions", "Entertainment")


def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    for candidate in (day.day, 30, 29, 28):
        try:
            return date(year, month, min(day.day, candidate))
        except ValueError:
            continue
    return date(year, month, 28)


def next_date(last: date, cadence: str) -> date:
    if cadence in ("weekly", "biweekly"):
        return last + timedelta(days=CADENCES[cadence][2])
    return add_months(last, CADENCES[cadence][2])


def monthly_equivalent(cents: int, cadence: str) -> int:
    return int(round(cents * CADENCES[cadence][3]))


def classify_cadence(gaps: list[int]) -> str | None:
    if not gaps:
        return None
    mid = median(gaps)
    for name, (low, high, _step, _factor) in CADENCES.items():
        if low <= mid <= high:
            inside = sum(1 for gap in gaps if low - 2 <= gap <= high + 2)
            if inside * 3 >= len(gaps) * 2:
                return name
    return None


def _clusters(rows: list[dict]) -> list[list[dict]]:
    ordered = sorted(rows, key=lambda row: (abs(int(row["amount_cents"])), row["date"]))
    groups: list[list[dict]] = []
    for row in ordered:
        size = abs(int(row["amount_cents"]))
        if groups:
            prev = abs(int(groups[-1][-1]["amount_cents"]))
            if size <= prev * CLUSTER_STEP or size - prev <= 100:
                groups[-1].append(row)
                continue
        groups.append([row])
    return groups


def _stable(amounts: list[int]) -> bool:
    """A fixed-price series: the amounts vary by at most 3% or $1."""
    low, high = min(amounts), max(amounts)
    return high - low <= max(100, int(high * 0.03))


def _price_change(amounts: list[int]) -> dict | None:
    """Latest amount against the one before, for series that were a fixed price until then.

    Variable bills (groceries, utilities) move every time, so they are not flagged.
    """
    if len(amounts) < 2 or not _stable(amounts[-7:-1]):
        return None
    last, prior = amounts[-1], amounts[-2]
    diff = last - prior
    if prior and (abs(diff) > abs(prior) * 0.03 or abs(diff) > 100):
        return {"from_cents": prior, "to_cents": last, "change_cents": diff}
    if not prior and diff:
        return {"from_cents": prior, "to_cents": last, "change_cents": diff}
    return None


def detect(rows: list[dict], today: date, overrides: dict[str, dict] | None = None) -> list[dict]:
    """Recurring series from personal rows. overrides: series_key -> {cadence, status, ...}."""
    overrides = overrides or {}
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if row.get("kind") in ("transfer", "funding"):
            continue
        if row.get("pending"):
            continue
        cents = int(row["amount_cents"])
        if cents == 0 or row.get("merchant_key") in (None, "", "(UNKNOWN)"):
            continue
        direction = "in" if cents > 0 else "out"
        groups.setdefault((row["merchant_key"], direction), []).append(row)
    found: list[dict] = []
    for (key, direction), merchant_rows in groups.items():
        for index, cluster in enumerate(_clusters(merchant_rows)):
            by_date: dict[str, int] = {}
            for row in cluster:
                by_date[row["date"]] = by_date.get(row["date"], 0) + abs(int(row["amount_cents"]))
            days = sorted(by_date)
            parsed = [date.fromisoformat(day) for day in days]
            gaps = [(parsed[i] - parsed[i - 1]).days for i in range(1, len(parsed))]
            series_key = f"{key}|{direction}|{index}"
            override = overrides.get(series_key, {})
            cadence = override.get("cadence") if override.get("cadence_locked") else classify_cadence(gaps)
            if cadence is None:
                continue
            if len(days) < MIN_COUNT[cadence] and not override.get("cadence_locked"):
                continue
            amounts = [by_date[day] for day in days]
            last = parsed[-1]
            nxt = next_date(last, cadence)
            typical = int(median(amounts[-3:]))
            latest_row = max(cluster, key=lambda row: (row["date"], row["id"]))
            group = latest_row.get("group") or ""
            if direction == "in":
                kind = "income"
            elif group in SUBSCRIPTION_GROUPS:
                kind = "subscription"
            else:
                kind = "bill"
            overdue_days = (today - last).days
            may_be_cancelled = overdue_days > NOMINAL_DAYS[cadence] * 1.5
            status = override.get("status") or "active"
            found.append(
                {
                    "series_key": series_key,
                    "merchant_key": key,
                    "merchant": latest_row.get("merchant") or key.title(),
                    "direction": direction,
                    "account_id": latest_row.get("account_id"),
                    "account_label": latest_row.get("account_label") or "",
                    "category_id": latest_row.get("category_id"),
                    "category": latest_row.get("category") or "",
                    "group": group,
                    "cadence": cadence,
                    "count": len(days),
                    "first_seen": days[0],
                    "last_date": days[-1],
                    "next_expected": nxt.isoformat(),
                    "typical_cents": typical,
                    "last_cents": amounts[-1],
                    "monthly_cents": monthly_equivalent(typical, cadence),
                    "annual_cents": monthly_equivalent(typical, cadence) * 12,
                    "price_change": _price_change(amounts),
                    "may_be_cancelled": may_be_cancelled,
                    "new": (today - parsed[0]).days <= 60,
                    "possible_duplicate": False,
                    "kind": kind,
                    "status": status,
                    "user_confirmed": bool(override.get("user_confirmed")),
                    "notes": override.get("notes") or "",
                    "dates": days[-12:],
                }
            )
    active = {}
    for item in found:
        if item["status"] in ("ignored", "cancelled") or item["may_be_cancelled"]:
            continue
        active.setdefault((item["merchant_key"], item["direction"]), []).append(item)
    for items in active.values():
        if len(items) > 1:
            for item in items:
                item["possible_duplicate"] = True
    found.sort(key=lambda item: (item["next_expected"], item["merchant"].lower()))
    return found


def occurrences(item: dict, start: date, end: date) -> list[date]:
    """Expected dates of a series between start and end (inclusive), stepping from its last date."""
    out: list[date] = []
    cursor = date.fromisoformat(item["last_date"])
    guard = 0
    while cursor <= end and guard < 400:
        cursor = next_date(cursor, item["cadence"])
        guard += 1
        if start <= cursor <= end:
            out.append(cursor)
    return out
