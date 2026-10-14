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
  3 occurrences; quarterly and annual need 2, and then the one gap must sit in
  the strict band (STRICT).
- Never recurring: interest charges, fees, transfers and card payments (by
  group or wording), outflows under $1; on the income side interest,
  dividends, refunds, credits, and anything under $20.
- Every outflow series gets a tier. bill: on cadence (two thirds of gaps in the
  strict band), a steady amount (charge to charge within 10%, 50% for
  utilities and taxes, one jump allowed per four), a specific merchant name,
  and a category that holds bills. Otherwise review, with the reasons. Uneven
  everyday spending (food, shopping, ...) and frequent purchases are dropped.
- decisions (p_recurring_decisions, by merchant key) override the tier for
  outflows: confirmed is always a bill, ignored is never shown.
- next_expected = last date + the cadence step (whole months for monthly and
  longer, so the 31st does not walk); typical = median of the last 3 amounts.
- price_change: latest amount differs from the one before by more than 3% or $1,
  for a series whose previous (up to 6) amounts were steady (within 3% or $1).
- may_be_cancelled: no charge for more than 1.5 cadences after the last one.
  Stale series that would need review are dropped.
- new: first seen within 60 days of today.
- possible_duplicate: two active bill-tier series share a merchant key (two
  plans or accounts with one merchant). A charge cannot show whether a
  subscription is used, so that is the only "unused" signal offered.
- kind: income for inflows; subscription for the Subscriptions / Entertainment
  groups; otherwise bill.
"""

from __future__ import annotations

import re
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
# Strict bands: a series counts as a sure bill only when most gaps sit inside these.
STRICT = {"weekly": (6, 8), "biweekly": (13, 15), "monthly": (26, 35), "quarterly": (84, 98), "annual": (350, 380)}
STRONG_FIT = 2 / 3
# Bills that come from a meter or a statement and move every month.
VARIABLE_GROUPS = ("Utilities", "Taxes")
# Everyday spending: a repeating charge here needs the owner to say it is a bill.
REVIEW_GROUPS = ("Food", "Shopping", "Travel", "Entertainment", "Personal care", "Pets", "Gifts & donations", "Cash & ATM", "Uncategorized")
EXCLUDE_GROUPS = ("Fees & interest", "Transfers")
# Groups whose rows may say "fee" without being a bank fee (HOA fee, insurance fee).
NAME_SAFE_GROUPS = ("Housing", "Utilities", "Insurance")
INCOME_SKIP_CATEGORIES = ("Interest & dividends", "Refunds & reimbursements")
NOT_A_BILL_RE = re.compile(
    r"\bINTEREST\b|FINANCE CHARGE|\bFEES?\b|LATE CHARGE|PENALTY|DIVIDEND|CASH ?BACK|\bREWARDS?\b|REFUND|"
    r"\bTRANSFER\b|\bXFER\b|PAYMENT THANK YOU|AUTOPAY PAYMENT|CA?RD PAYMENT|\bEPAY\b"
)
INCOME_SKIP_RE = re.compile(r"\bINTEREST\b|DIVIDEND|REFUND|\bRETURN|\bREWARDS?\b|CASH ?BACK|\bCREDIT\b|REBATE|ADJUSTMENT")
MIN_BILL_CENTS = 100
MIN_INCOME_CENTS = 2000
# Merchant keys that say nothing about who was paid.
GENERIC_KEYS = frozenset({
    "DIGITAL", "PAYMENT", "PAYMENTS", "PURCHASE", "DEBIT", "ONLINE", "WEB", "ACH", "CHECK", "WITHDRAWAL", "POS",
    "LOAN PAYMENT", "BILL PAYMENT", "BILL PAY", "ONLINE PAYMENT", "RECURRING", "MEMBERSHIP", "SERVICE", "SERVICES",
    "SUBSCRIPTION", "AUTOPAY", "MISC", "OTHER",
})


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


def _fit(gaps: list[int], cadence: str) -> float:
    """Share of gaps inside the strict band for a cadence (1.0 with no gaps)."""
    if not gaps:
        return 1.0
    low, high = STRICT[cadence]
    return sum(1 for gap in gaps if low <= gap <= high) / len(gaps)


def _amounts_ok(amounts: list[int], group: str) -> bool:
    """Charge to charge, the amount holds: within 10% (or $1) for fixed prices, 50% for metered bills.

    Compared pairwise, allowing one jump in four (at least one), so a price increase is not "varies".
    """
    if len(amounts) < 2:
        return True
    share = 0.5 if group in VARIABLE_GROUPS else 0.10
    pairs = list(zip(amounts, amounts[1:]))
    misses = sum(1 for prev, cur in pairs if abs(cur - prev) > max(100, max(prev, cur) * share))
    return misses <= max(1, len(pairs) // 4)


def _excluded(rows: list[dict], direction: str, typical: int) -> str | None:
    """Why a series is never a bill (or never recurring income), or None."""
    latest = max(rows, key=lambda row: (row["date"], row["id"]))
    group = latest.get("group") or ""
    category = latest.get("category") or ""
    text = " ".join(str(latest.get(field) or "") for field in ("merchant_key", "name", "merchant")).upper()
    if group == "Transfers" or latest.get("kind") == "transfer":
        return "transfer or card payment"
    if direction == "in":
        if category in INCOME_SKIP_CATEGORIES or INCOME_SKIP_RE.search(text):
            return "interest, dividend, refund, or credit"
        if typical < MIN_INCOME_CENTS:
            return "tiny amount"
        return None
    if group in EXCLUDE_GROUPS:
        return "interest or fee"
    if group not in NAME_SAFE_GROUPS and NOT_A_BILL_RE.search(text):
        return "interest, fee, or transfer"
    if typical < MIN_BILL_CENTS:
        return "tiny amount"
    return None


def generic_key(key: str) -> bool:
    letters = re.sub(r"[^A-Z]", "", key)
    return key in GENERIC_KEYS or len(letters) < 3


def detect(
    rows: list[dict],
    today: date,
    overrides: dict[str, dict] | None = None,
    decisions: dict[str, str] | None = None,
) -> list[dict]:
    """Recurring series from personal rows.

    overrides: series_key -> {cadence, status, ...} (p_recurring).
    decisions: merchant_key -> confirmed | ignored (p_recurring_decisions), for outflows.
    Each item has tier bill, review, or income; excluded and ignored series are left out.
    """
    overrides = overrides or {}
    decisions = decisions or {}
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
    candidates: list[dict] = []
    in_series: dict[tuple[str, str], set] = {}
    for (key, direction), merchant_rows in groups.items():
        decision = decisions.get(key) if direction == "out" else None
        if decision == "ignored":
            continue
        confirmed = decision == "confirmed"
        for index, cluster in enumerate(_clusters(merchant_rows)):
            by_date: dict[str, int] = {}
            for row in cluster:
                by_date[row["date"]] = by_date.get(row["date"], 0) + abs(int(row["amount_cents"]))
            days = sorted(by_date)
            parsed = [date.fromisoformat(day) for day in days]
            gaps = [(parsed[i] - parsed[i - 1]).days for i in range(1, len(parsed))]
            series_key = f"{key}|{direction}|{index}"
            override = overrides.get(series_key, {})
            locked = bool(override.get("cadence_locked"))
            cadence = override.get("cadence") if locked else classify_cadence(gaps)
            if cadence is None:
                continue
            fit = _fit(gaps, cadence)
            if not (locked or confirmed):
                if len(days) < MIN_COUNT[cadence]:
                    continue
                # Two quarterly or annual charges count only when the one gap is on cadence.
                if len(days) < 3 and fit < 1.0:
                    continue
            elif len(days) < 2:
                continue
            in_series.setdefault((key, direction), set()).update(row["id"] for row in cluster)
            candidates.append(
                {"key": key, "direction": direction, "cluster": cluster, "days": days, "parsed": parsed,
                 "amounts": [by_date[day] for day in days], "series_key": series_key, "override": override,
                 "cadence": cadence, "fit": fit, "confirmed": confirmed}
            )
    found: list[dict] = []
    for cand in candidates:
        key, direction, cluster, days, parsed, amounts = (
            cand["key"], cand["direction"], cand["cluster"], cand["days"], cand["parsed"], cand["amounts"]
        )
        cadence, override = cand["cadence"], cand["override"]
        last = parsed[-1]
        typical = int(median(amounts[-3:]))
        latest_row = max(cluster, key=lambda row: (row["date"], row["id"]))
        group = latest_row.get("group") or ""
        overdue_days = (today - last).days
        may_be_cancelled = overdue_days > NOMINAL_DAYS[cadence] * 1.5
        span_start = (parsed[0] - timedelta(days=NOMINAL_DAYS[cadence])).isoformat()
        others = sum(
            1 for row in groups[(key, direction)]
            if row["id"] not in in_series[(key, direction)] and span_start <= row["date"] <= days[-1]
        )
        steady = _amounts_ok(amounts, group)
        reasons: list[str] = []
        excluded = _excluded(cluster, direction, typical)
        if direction == "in":
            kind, tier = "income", "income"
        else:
            kind = "subscription" if group in SUBSCRIPTION_GROUPS else "bill"
            if excluded is None:
                spending = group in REVIEW_GROUPS or not group
                if spending and (cand["fit"] < STRONG_FIT or not steady or others >= max(3, len(days))):
                    excluded = "everyday spending"
                elif others >= max(3, len(days)) and not steady:
                    excluded = "frequent purchases"
            if excluded is None:
                if group in REVIEW_GROUPS or not group:
                    reasons.append(f"{group or 'uncategorized'} is usually not a bill")
                if generic_key(key):
                    reasons.append("generic merchant name")
                if cand["fit"] < STRONG_FIT:
                    reasons.append("timing is irregular")
                if not steady:
                    reasons.append("amount varies")
                if others >= 2:
                    reasons.append(f"{others} other charges at this merchant")
            tier = "review" if reasons else "bill"
            if cand["confirmed"]:
                excluded, reasons, tier = None, [], "bill"
        if excluded is not None:
            continue
        if tier == "review" and may_be_cancelled:
            continue
        status = override.get("status") or "active"
        found.append(
            {
                "series_key": cand["series_key"],
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
                "next_expected": next_date(last, cadence).isoformat(),
                "typical_cents": typical,
                "last_cents": amounts[-1],
                "monthly_cents": monthly_equivalent(typical, cadence),
                "annual_cents": monthly_equivalent(typical, cadence) * 12,
                "price_change": _price_change(amounts),
                "may_be_cancelled": may_be_cancelled,
                "new": (today - parsed[0]).days <= 60,
                "possible_duplicate": False,
                "kind": kind,
                "tier": tier,
                "review_reasons": reasons,
                "decision": "confirmed" if cand["confirmed"] else None,
                "status": status,
                "user_confirmed": bool(override.get("user_confirmed")) or cand["confirmed"],
                "notes": override.get("notes") or "",
                "dates": days[-12:],
            }
        )
    active = {}
    for item in found:
        if item["status"] in ("ignored", "cancelled") or item["may_be_cancelled"] or item["tier"] == "review":
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
