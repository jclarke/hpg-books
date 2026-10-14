"""Personal figures: net worth, accounts, spending, cash flow, budgets, goals,
bills, the monthly summary, and the reconciliation. Integer cents throughout.

Spending is outflow in expense categories, shown positive; refunds in an
expense category net against it. Income is inflow in income categories, plus
inflows still Uncategorized (shown as "Other income" until reviewed). Owner
draws are funding: a separate source line, never earned income, and never
added to or taken from business figures. Transfers are neither.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from hpgbooks.db import HpgbooksError
from hpgbooks.personal import recurring as rec
from hpgbooks.personal.balances import is_liability, load_series
from hpgbooks.personal.classify import (
    allocations,
    categories,
    load_personal,
    needs_review,
    personal_accounts,
    personal_ready,
)
from hpgbooks.reports import month_end, parse_month, prev_month, require_date
from hpgbooks.scope import label

CLASS_GROUPS = (
    ("cash", "Cash"),
    ("liability", "Credit cards"),
    ("investment", "Investments"),
    ("loan", "Loans & mortgages"),
    ("other", "Other"),
)
INCOME_SOURCES = ("Paycheck", "Interest & dividends", "Owner draws", "Other")
WARN_PCT = 80.0


def today() -> date:
    return date.today()


def month_of(day: date) -> str:
    return day.strftime("%Y-%m")


def months_back(month: str, count: int) -> list[str]:
    out = [month]
    for _ in range(count - 1):
        out.append(prev_month(out[-1]))
    return list(reversed(out))


def _pct(part: int, whole: int) -> float | None:
    if whole <= 0:
        return None
    return round(part / whole * 100.0, 1)


def _change(current: int, previous: int) -> dict:
    delta = current - previous
    pct = None if previous == 0 else round(delta / abs(previous) * 100.0, 1)
    return {"cents": current, "prior_cents": previous, "delta": delta, "pct": pct}


# --- totals over rows ----------------------------------------------------------


def totals(rows: list[dict], cats: dict[int, dict]) -> dict:
    """Income by source, owner draws, spending by category/group, transfers net."""
    sources = {name: 0 for name in INCOME_SOURCES}
    spending = 0
    by_category: dict[int, dict] = {}
    by_group: dict[str, int] = {}
    transfers = 0
    for row in rows:
        for piece in allocations(row, cats):
            cents = int(piece["amount_cents"])
            kind = piece["kind"]
            if kind == "transfer":
                transfers += cents
            elif kind == "funding":
                sources["Owner draws"] += cents
            elif kind == "income":
                name = piece["category"] if piece["category"] in sources else "Other"
                sources[name] += cents
            elif piece["category"] == "Uncategorized" and cents > 0:
                sources["Other"] += cents
            else:
                spent = -cents
                spending += spent
                key = piece["category_id"]
                item = by_category.setdefault(
                    key, {"category_id": key, "category": piece["category"], "group": piece["group"], "cents": 0, "count": 0}
                )
                item["cents"] += spent
                item["count"] += 1
                by_group[piece["group"]] = by_group.get(piece["group"], 0) + spent
    earned = sources["Paycheck"] + sources["Interest & dividends"] + sources["Other"]
    draws = sources["Owner draws"]
    income = earned + draws
    return {
        "income_cents": income,
        "earned_cents": earned,
        "owner_draws_cents": draws,
        "spending_cents": spending,
        "net_cents": income - spending,
        "savings_rate": _pct(income - spending, income),
        "savings_rate_without_draws": _pct(earned - spending, earned),
        "sources": sources,
        "by_category": sorted(by_category.values(), key=lambda item: (-item["cents"], item["category"])),
        "by_group": by_group,
        "transfers_net_cents": transfers,
    }


def _rows(conn, start: str, end: str, **kwargs) -> list[dict]:
    return load_personal(conn, start, end, **kwargs)


# --- net worth and accounts -------------------------------------------------------


def _included(acct: dict) -> bool:
    return bool(acct.get("include_in_net_worth", 1)) and not acct.get("closed")


def _signed(acct: dict, balance: int) -> int:
    return -balance if is_liability(acct) else balance


def _history_dates(start: date, end: date, interval: str) -> list[date]:
    points = []
    if interval == "week":
        cursor = end
        while cursor >= start:
            points.append(cursor)
            cursor -= timedelta(days=7)
        points.reverse()
        return points
    cursor = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    while cursor < end:
        points.append(cursor)
        nxt = cursor + timedelta(days=1)
        cursor = date(nxt.year, nxt.month, calendar.monthrange(nxt.year, nxt.month)[1])
    points.append(end)
    return points


RANGES = ("3M", "6M", "1Y", "YTD", "ALL")


def _range_start(conn, span: str, now: date, series) -> date:
    if span == "3M":
        return now - timedelta(days=91)
    if span == "6M":
        return now - timedelta(days=182)
    if span == "1Y":
        return now - timedelta(days=365)
    if span == "YTD":
        return date(now.year, 1, 1)
    firsts = [s.dates[0] for s in series.values() if s.dates] + [
        s.anchors[0]["as_of_date"] for s in series.values() if s.anchors
    ]
    return date.fromisoformat(min(firsts)) if firsts else now - timedelta(days=365)


def net_worth(conn, span: str = "1Y", interval: str | None = None) -> dict:
    if span not in RANGES:
        raise HpgbooksError("range must be 3M, 6M, 1Y, YTD, or ALL")
    now = today()
    accounts = personal_accounts(conn)
    series = load_series(conn, accounts)
    start = _range_start(conn, span, now, series)
    interval = interval or ("week" if span in ("3M", "6M") else "month")
    if interval not in ("week", "month"):
        raise HpgbooksError("interval must be week or month")
    points = []
    for day in _history_dates(start, now, interval):
        key = day.isoformat()
        assets = liabilities = 0
        for account_id, acct in accounts.items():
            if not _included(acct):
                continue
            bal = series[account_id].balance_at(key)
            if is_liability(acct):
                liabilities += bal
            else:
                assets += bal
        points.append({"date": key, "assets_cents": assets, "liabilities_cents": liabilities, "net_cents": assets - liabilities})
    rows = []
    by_class: dict[str, dict] = {cls: {"class": cls, "label": text, "cents": 0, "count": 0} for cls, text in CLASS_GROUPS}
    start_key = start.isoformat()
    for account_id, acct in accounts.items():
        s = series[account_id]
        bal = s.balance_at(now.isoformat())
        first = s.balance_at(start_key)
        included = _included(acct)
        rows.append(
            {
                "id": account_id,
                "label": label(acct),
                "last4": acct.get("last4") or "",
                "institution": acct.get("institution") or "",
                "class": acct.get("class") or "cash",
                "liability": is_liability(acct),
                "balance_cents": bal,
                "contribution_cents": _signed(acct, bal) if included else 0,
                "change_cents": (_signed(acct, bal) - _signed(acct, first)) if included else 0,
                "included": included,
                "anchored": bool(s.anchors),
                "last_updated": s.last_updated(),
                "stale": s.stale(now),
            }
        )
        if included:
            group = by_class.get(acct.get("class") or "other") or by_class["other"]
            group["cents"] += _signed(acct, bal)
            group["count"] += 1
    rows.sort(key=lambda row: (-abs(row["contribution_cents"]), row["label"]))
    current = points[-1] if points else {"assets_cents": 0, "liabilities_cents": 0, "net_cents": 0}

    def ago(days: int) -> int:
        key = (now - timedelta(days=days)).isoformat()
        total = 0
        for account_id, acct in accounts.items():
            if _included(acct):
                total += _signed(acct, series[account_id].balance_at(key))
        return total

    return {
        "as_of": now.isoformat(),
        "range": span,
        "interval": interval,
        "assets_cents": current["assets_cents"],
        "liabilities_cents": current["liabilities_cents"],
        "net_cents": current["net_cents"],
        "change_30_cents": current["net_cents"] - ago(30),
        "change_90_cents": current["net_cents"] - ago(90),
        "points": points,
        "accounts": rows,
        "by_class": [item for item in by_class.values() if item["count"]],
        "stale_count": sum(1 for row in rows if row["stale"] and row["included"]),
        "method": "Balance anchors plus ledger activity; see docs/personal-mode.md",
    }


def accounts_overview(conn) -> dict:
    now = today()
    accounts = personal_accounts(conn)
    series = load_series(conn, accounts)
    groups = []
    for cls, text in CLASS_GROUPS:
        members = [acct for acct in accounts.values() if (acct.get("class") or "other") == cls]
        if not members:
            continue
        rows = []
        total = 0
        for acct in members:
            s = series[acct["id"]]
            bal = s.balance_at(now.isoformat())
            spark = [s.balance_at((now - timedelta(days=offset)).isoformat()) for offset in range(29, -1, -1)]
            rows.append(
                {
                    "id": acct["id"],
                    "label": label(acct),
                    "last4": acct.get("last4") or "",
                    "institution": acct.get("institution") or "",
                    "class": cls,
                    "liability": is_liability(acct),
                    "balance_cents": bal,
                    "last_updated": s.last_updated(),
                    "stale": s.stale(now),
                    "anchored": bool(s.anchors),
                    "included": _included(acct),
                    "spark": spark,
                    "transaction_count": len(s.dates),
                }
            )
            total += bal
        groups.append({"class": cls, "label": text, "total_cents": total, "accounts": rows})
    return {"as_of": now.isoformat(), "groups": groups, "account_count": len(accounts)}


# --- spending -----------------------------------------------------------------


def _range_months(start: str, end: str) -> list[str]:
    from hpgbooks.reports import months_covering

    return months_covering(start, end)


def _year_ago(day: str) -> str:
    d = date.fromisoformat(day)
    try:
        return d.replace(year=d.year - 1).isoformat()
    except ValueError:
        return date(d.year - 1, 2, 28).isoformat()


def spending(conn, start: str, end: str) -> dict:
    from hpgbooks.analytics import compare_window

    start, end = require_date(start), require_date(end)
    if start > end:
        raise HpgbooksError("start must be on or before end")
    cats = categories(conn)
    rows = _rows(conn, start, end)
    current = totals(rows, cats)
    prior_start, prior_end = compare_window(start, end)
    prior = totals(_rows(conn, prior_start, prior_end), cats)
    ly_start, ly_end = _year_ago(start), _year_ago(end)
    last_year = totals(_rows(conn, ly_start, ly_end), cats)
    prior_by = {item["category_id"]: item["cents"] for item in prior["by_category"]}
    ly_by = {item["category_id"]: item["cents"] for item in last_year["by_category"]}
    categories_out = [
        {
            **item,
            "prior_cents": prior_by.get(item["category_id"], 0),
            "last_year_cents": ly_by.get(item["category_id"], 0),
            "share": _pct(item["cents"], current["spending_cents"]),
        }
        for item in current["by_category"]
    ]
    groups = []
    for group, cents in sorted(current["by_group"].items(), key=lambda item: (-item[1], item[0])):
        groups.append(
            {
                "group": group,
                "cents": cents,
                "prior_cents": prior["by_group"].get(group, 0),
                "last_year_cents": last_year["by_group"].get(group, 0),
                "share": _pct(cents, current["spending_cents"]),
                "categories": [item for item in categories_out if item["group"] == group],
            }
        )
    merchants: dict[str, dict] = {}
    for row in rows:
        for piece in allocations(row, cats):
            if piece["kind"] != "expense" or (piece["category"] == "Uncategorized" and piece["amount_cents"] > 0):
                continue
            item = merchants.setdefault(
                row["merchant_key"], {"merchant_key": row["merchant_key"], "merchant": row["merchant"], "cents": 0, "count": 0, "last_date": ""}
            )
            item["cents"] -= int(piece["amount_cents"])
            item["count"] += 1
            item["last_date"] = max(item["last_date"], row["date"])
    merchant_rows = sorted(merchants.values(), key=lambda item: (-item["cents"], item["merchant"]))
    for item in merchant_rows:
        item["average_cents"] = int(round(item["cents"] / item["count"])) if item["count"] else 0
    # Year to date against the same days last year.
    end_d = date.fromisoformat(end)
    ytd_start = f"{end_d.year:04d}-01-01"
    ytd = totals(_rows(conn, ytd_start, end), cats)["spending_cents"]
    ytd_prior = totals(_rows(conn, _year_ago(ytd_start), _year_ago(end)), cats)["spending_cents"]
    # Twelve-month trend for the largest groups.
    trend_months = months_back(end[:7], 12)
    trend_rows = _rows(conn, trend_months[0] + "-01", month_end(trend_months[-1]))
    top_groups = [item["group"] for item in groups[:5]]
    trend = []
    for month in trend_months:
        month_totals = totals([row for row in trend_rows if row["date"].startswith(month)], cats)
        trend.append(
            {
                "month": month,
                "total_cents": month_totals["spending_cents"],
                "groups": {group: month_totals["by_group"].get(group, 0) for group in top_groups},
            }
        )
    return {
        "start": start,
        "end": end,
        "prior_start": prior_start,
        "prior_end": prior_end,
        "last_year_start": ly_start,
        "last_year_end": ly_end,
        "total": _change(current["spending_cents"], prior["spending_cents"]),
        "same_period_last_year_cents": last_year["spending_cents"],
        "ytd": _change(ytd, ytd_prior),
        "groups": groups,
        "categories": categories_out,
        "merchants": merchant_rows[:50],
        "merchant_count": len(merchant_rows),
        "trend": trend,
        "trend_groups": top_groups,
    }


# --- cash flow ----------------------------------------------------------------


def cash_flow(conn, end_month: str | None = None, months: int = 12) -> dict:
    end_month = parse_month(end_month) if end_month else month_of(today())
    if months < 1 or months > 60:
        raise HpgbooksError("months must be 1 to 60")
    keys = months_back(end_month, months)
    cats = categories(conn)
    rows = _rows(conn, keys[0] + "-01", month_end(keys[-1]))
    out = []
    sum_sources = {name: 0 for name in INCOME_SOURCES}
    for month in keys:
        t = totals([row for row in rows if row["date"].startswith(month)], cats)
        for name in INCOME_SOURCES:
            sum_sources[name] += t["sources"][name]
        out.append(
            {
                "month": month,
                "income_cents": t["income_cents"],
                "earned_cents": t["earned_cents"],
                "owner_draws_cents": t["owner_draws_cents"],
                "spending_cents": t["spending_cents"],
                "net_cents": t["net_cents"],
                "savings_rate": t["savings_rate"],
                "savings_rate_without_draws": t["savings_rate_without_draws"],
                "sources": t["sources"],
            }
        )
    income = sum(item["income_cents"] for item in out)
    earned = sum(item["earned_cents"] for item in out)
    spent = sum(item["spending_cents"] for item in out)
    return {
        "start_month": keys[0],
        "end_month": keys[-1],
        "months": out,
        "income_cents": income,
        "earned_cents": earned,
        "owner_draws_cents": sum_sources["Owner draws"],
        "spending_cents": spent,
        "net_cents": income - spent,
        "savings_rate": _pct(income - spent, income),
        "savings_rate_without_draws": _pct(earned - spent, earned),
        "sources": [{"source": name, "cents": sum_sources[name]} for name in INCOME_SOURCES],
    }


# --- budgets ------------------------------------------------------------------


def _budget_rows(conn) -> list[dict]:
    return [{key: row[key] for key in row.keys()} for row in conn.execute("SELECT * FROM p_budgets ORDER BY id")]


def _effective(budgets: list[dict], month: str) -> dict[tuple, dict]:
    """(kind, key) -> budget row in effect for month: a month row beats the default row."""
    out: dict[tuple, dict] = {}
    for row in budgets:
        key = ("category", row["category_id"]) if row["category_id"] is not None else ("group", row["group_name"])
        if row["month"] is None:
            out.setdefault(key, row)
    for row in budgets:
        key = ("category", row["category_id"]) if row["category_id"] is not None else ("group", row["group_name"])
        if row["month"] == month:
            out[key] = row
    return out


def _spent_by(rows: list[dict], cats: dict[int, dict]) -> tuple[dict[int, int], dict[str, int]]:
    t = totals(rows, cats)
    return {item["category_id"]: item["cents"] for item in t["by_category"]}, t["by_group"]


def budgets(conn, month: str | None = None) -> dict:
    month = parse_month(month) if month else month_of(today())
    cats = categories(conn)
    all_budgets = _budget_rows(conn)
    effective = _effective(all_budgets, month)
    rows = _rows(conn, month + "-01", month_end(month))
    by_cat, by_group = _spent_by(rows, cats)
    now = today()
    days = calendar.monthrange(int(month[:4]), int(month[5:7]))[1]
    if month == month_of(now):
        elapsed = now.day
    elif month < month_of(now):
        elapsed = days
    else:
        elapsed = 0
    # Rollover: carry each earlier month's (budget - spent), up to 12 months back,
    # starting with the first of those months that has spending in the line.
    carry: dict[tuple, int] = {}
    started: set[tuple] = set()
    rollover_keys = [key for key, row in effective.items() if row["rollover"]]
    if rollover_keys:
        history = months_back(month, 13)[:-1]
        hist_rows = _rows(conn, history[0] + "-01", month_end(history[-1]))
        for past in history:
            past_eff = _effective(all_budgets, past)
            pcat, pgroup = _spent_by([row for row in hist_rows if row["date"].startswith(past)], cats)
            for key in rollover_keys:
                row = past_eff.get(key)
                if row is None or not row["rollover"]:
                    carry[key] = 0
                    continue
                spent = pcat.get(key[1], 0) if key[0] == "category" else pgroup.get(key[1], 0)
                if spent:
                    started.add(key)
                if key not in started:
                    continue
                carry[key] = carry.get(key, 0) + int(row["amount_cents"]) - spent
    out = []
    for key, row in effective.items():
        if key[0] == "category":
            cat = cats.get(key[1]) or {}
            name, group = cat.get("name") or "?", cat.get("group_name") or ""
            spent = by_cat.get(key[1], 0)
        else:
            name, group = key[1], key[1]
            spent = by_group.get(key[1], 0)
        available = int(row["amount_cents"]) + carry.get(key, 0)
        pct = _pct(spent, available) if available > 0 else (None if spent == 0 else 999.0)
        projected = int(round(spent / elapsed * days)) if elapsed and elapsed < days else spent
        if available <= 0 and spent > 0 or (available > 0 and spent > available):
            status = "over"
        elif pct is not None and pct >= WARN_PCT:
            status = "warning"
        else:
            status = "ok"
        out.append(
            {
                "id": row["id"],
                "kind": key[0],
                "category_id": key[1] if key[0] == "category" else None,
                "group_name": key[1] if key[0] == "group" else None,
                "name": name,
                "group": group,
                "month_specific": row["month"] == month,
                "budget_cents": int(row["amount_cents"]),
                "rollover": bool(row["rollover"]),
                "carry_cents": carry.get(key, 0),
                "available_cents": available,
                "spent_cents": spent,
                "remaining_cents": available - spent,
                "pct_used": pct,
                "projected_cents": projected,
                "projected_over": projected > available,
                "status": status,
            }
        )
    out.sort(key=lambda item: (item["group"], item["name"]))
    budgeted = sum(item["available_cents"] for item in out)
    spent_total = sum(item["spent_cents"] for item in out)
    unbudgeted = [
        {"category_id": item["category_id"], "category": item["category"], "group": item["group"], "cents": item["cents"]}
        for item in totals(rows, cats)["by_category"]
        if ("category", item["category_id"]) not in effective and ("group", item["group"]) not in effective
    ]
    return {
        "month": month,
        "days_in_month": days,
        "days_elapsed": elapsed,
        "rows": out,
        "budgeted_cents": budgeted,
        "spent_cents": spent_total,
        "remaining_cents": budgeted - spent_total,
        "total_spending_cents": totals(rows, cats)["spending_cents"],
        "unbudgeted": unbudgeted,
        "alerts": [
            {"name": item["name"], "status": item["status"], "pct_used": item["pct_used"], "spent_cents": item["spent_cents"], "available_cents": item["available_cents"]}
            for item in out
            if item["status"] != "ok"
        ],
        "counts": {status: sum(1 for item in out if item["status"] == status) for status in ("ok", "warning", "over")},
    }


def average_suggestions(conn, month: str | None = None) -> list[dict]:
    """Per-category average spending over the 3 full months before `month`, rounded up to dollars."""
    month = parse_month(month) if month else month_of(today())
    history = months_back(prev_month(month), 3)
    cats = categories(conn)
    rows = _rows(conn, history[0] + "-01", month_end(history[-1]))
    sums: dict[int, int] = {}
    for past in history:
        by_cat, _ = _spent_by([row for row in rows if row["date"].startswith(past)], cats)
        for cat_id, cents in by_cat.items():
            sums[cat_id] = sums.get(cat_id, 0) + cents
    out = []
    for cat_id, total in sorted(sums.items(), key=lambda item: -item[1]):
        if total <= 0 or cat_id is None:
            continue
        avg = total / 3
        dollars = -(-int(avg) // 100)
        cat = cats.get(cat_id) or {}
        out.append({"category_id": cat_id, "category": cat.get("name"), "group": cat.get("group_name"), "average_cents": dollars * 100, "months": history})
    return out


# --- goals --------------------------------------------------------------------


def _months_until(now: date, target: str | None) -> int | None:
    if not target:
        return None
    end = date.fromisoformat(target)
    months = (end.year - now.year) * 12 + end.month - now.month
    if end.day < now.day:
        months -= 1
    return max(1, months)


def goals(conn, include_archived: bool = False) -> list[dict]:
    now = today()
    accounts = personal_accounts(conn)
    series = load_series(conn, accounts)
    out = []
    for row in conn.execute("SELECT * FROM p_goals ORDER BY archived, id"):
        goal = {key: row[key] for key in row.keys()}
        if goal["archived"] and not include_archived:
            continue
        linked = goal["account_id"] if goal["account_id"] in accounts else None
        if linked:
            s = series[linked]
            current = s.balance_at(now.isoformat())
            observed = int(round((current - s.balance_at((now - timedelta(days=90)).isoformat())) / 3))
        else:
            current = int(goal["manual_current_cents"])
            observed = 0
        target = int(goal["target_cents"])
        remaining = max(0, target - current)
        months_left = _months_until(now, goal["target_date"])
        required = int(-(-remaining // months_left)) if months_left else None
        contribution = int(goal["monthly_contribution_cents"]) or max(0, observed)
        if current >= target:
            status = "done"
        elif required is None:
            status = "no_date"
        elif contribution >= required:
            status = "on_track"
        else:
            status = "behind"
        out.append(
            {
                "id": goal["id"],
                "name": goal["name"],
                "target_cents": target,
                "target_date": goal["target_date"],
                "account_id": linked,
                "account_label": label(accounts[linked]) if linked else "",
                "current_cents": current,
                "remaining_cents": remaining,
                "progress_pct": round(min(100.0, current / target * 100.0), 1) if target else 0.0,
                "months_left": months_left,
                "required_monthly_cents": required,
                "monthly_contribution_cents": int(goal["monthly_contribution_cents"]),
                "observed_monthly_cents": observed,
                "status": status,
                "archived": bool(goal["archived"]),
            }
        )
    return out


# --- recurring and bills ------------------------------------------------------------


def recurring_overrides(conn) -> dict[str, dict]:
    return {
        row["series_key"]: {key: row[key] for key in row.keys()}
        for row in conn.execute("SELECT * FROM p_recurring")
    }


def recurring(conn) -> dict:
    now = today()
    rows = load_personal(conn, (now - timedelta(days=800)).isoformat(), now.isoformat())
    items = rec.detect(rows, now, recurring_overrides(conn))
    live = [item for item in items if item["status"] not in ("ignored", "cancelled")]
    subs = [item for item in live if item["kind"] == "subscription" and not item["may_be_cancelled"]]
    bills = [item for item in live if item["kind"] == "bill" and not item["may_be_cancelled"]]
    return {
        "as_of": now.isoformat(),
        "items": items,
        "subscription_monthly_cents": sum(item["monthly_cents"] for item in subs),
        "subscription_annual_cents": sum(item["annual_cents"] for item in subs),
        "bill_monthly_cents": sum(item["monthly_cents"] for item in bills),
        "counts": {
            "subscriptions": len(subs),
            "bills": len(bills),
            "income": sum(1 for item in live if item["kind"] == "income"),
            "price_changes": sum(1 for item in live if item["price_change"]),
            "may_be_cancelled": sum(1 for item in live if item["may_be_cancelled"]),
            "possible_duplicates": sum(1 for item in live if item["possible_duplicate"]),
        },
    }


def refresh_recurring(conn) -> int:
    """Store the detector's current view in p_recurring, keeping user status and cadence edits."""
    from hpgbooks.db import now_iso

    data = recurring(conn)
    wrote = 0
    for item in data["items"]:
        conn.execute(
            """
            INSERT INTO p_recurring (series_key, merchant_key, category_id, cadence, typical_amount_cents, last_amount_cents,
                                     last_date, next_expected_date, kind, status, first_seen, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT(series_key) DO UPDATE SET
              category_id = excluded.category_id,
              cadence = CASE WHEN p_recurring.cadence_locked = 1 THEN p_recurring.cadence ELSE excluded.cadence END,
              typical_amount_cents = excluded.typical_amount_cents, last_amount_cents = excluded.last_amount_cents,
              last_date = excluded.last_date, next_expected_date = excluded.next_expected_date,
              kind = excluded.kind, first_seen = excluded.first_seen, updated_at = excluded.updated_at
            """,
            (
                item["series_key"], item["merchant_key"], item["category_id"], item["cadence"], item["typical_cents"],
                item["last_cents"], item["last_date"], item["next_expected"], item["kind"], item["first_seen"], now_iso(),
            ),
        )
        wrote += 1
    return wrote


def bills(conn, days: int = 45) -> dict:
    """Expected bills, subscriptions, and income from today through `days` ahead, plus this month's paid ones."""
    if days < 1 or days > 120:
        raise HpgbooksError("days must be 1 to 120")
    now = today()
    horizon = now + timedelta(days=days)
    month_start = now.replace(day=1)
    data = recurring(conn)
    events = []
    for item in data["items"]:
        if item["status"] in ("ignored", "cancelled") or item["may_be_cancelled"]:
            continue
        for day in item["dates"]:
            if month_start.isoformat() <= day <= now.isoformat():
                cents = item["last_cents"] if day == item["last_date"] else item["typical_cents"]
                events.append({**_event(item, day), "status": "paid", "amount_cents": cents})
        cursor = date.fromisoformat(item["next_expected"])
        grace = now - timedelta(days=3)
        guard = 0
        while cursor <= horizon and guard < 200:
            guard += 1
            if cursor >= grace:
                status = "due" if cursor >= now else "late"
                events.append({**_event(item, cursor.isoformat()), "status": status, "amount_cents": item["typical_cents"]})
            elif cursor.isoformat() == item["next_expected"]:
                # Expected more than 3 days ago with no charge since.
                events.append({**_event(item, cursor.isoformat()), "status": "overdue", "amount_cents": item["typical_cents"]})
            cursor = rec.next_date(cursor, item["cadence"])
    events.sort(key=lambda event: (event["date"], event["merchant"].lower()))
    week_end = (now + timedelta(days=7)).isoformat()
    month_last = month_end(month_of(now))

    def due_sum(until: str) -> int:
        return sum(
            event["amount_cents"]
            for event in events
            if event["direction"] == "out" and event["status"] in ("due", "late", "overdue") and event["date"] <= until
        )

    return {
        "as_of": now.isoformat(),
        "through": horizon.isoformat(),
        "events": events,
        "due_this_week_cents": due_sum(week_end),
        "due_this_month_cents": due_sum(month_last),
        "overdue_count": sum(1 for event in events if event["status"] == "overdue"),
        "income_expected_cents": sum(event["amount_cents"] for event in events if event["direction"] == "in" and event["status"] == "due"),
    }


def _event(item: dict, day: str) -> dict:
    return {
        "date": day,
        "series_key": item["series_key"],
        "merchant": item["merchant"],
        "kind": item["kind"],
        "direction": item["direction"],
        "cadence": item["cadence"],
        "category": item["category"],
        "account_label": item["account_label"],
    }


# --- dashboard ----------------------------------------------------------------


def dashboard(conn) -> dict:
    now = today()
    month = month_of(now)
    cats = categories(conn)
    rows = _rows(conn, month + "-01", month_end(month))
    t = totals(rows, cats)
    worth = net_worth(conn, "3M")
    budget = budgets(conn, month)
    recurring_data = recurring(conn)
    upcoming = [event for event in bills(conn, 14)["events"] if event["status"] in ("due", "late", "overdue")]
    recent = sorted(load_personal(conn, (now - timedelta(days=60)).isoformat(), now.isoformat()), key=lambda row: (row["date"], row["id"]), reverse=True)[:10]
    review = sum(1 for row in load_personal(conn, include_synthesized=False) if needs_review(row))
    return {
        "as_of": now.isoformat(),
        "month": month,
        "has_accounts": bool(personal_accounts(conn)),
        "has_data": bool(recent) or any(group["accounts"] for group in accounts_overview(conn)["groups"]),
        "net_worth": {
            "net_cents": worth["net_cents"],
            "change_30_cents": worth["change_30_cents"],
            "change_90_cents": worth["change_90_cents"],
            "stale_count": worth["stale_count"],
            "points": worth["points"],
        },
        "month_totals": {
            "income_cents": t["income_cents"],
            "earned_cents": t["earned_cents"],
            "owner_draws_cents": t["owner_draws_cents"],
            "spending_cents": t["spending_cents"],
            "net_cents": t["net_cents"],
            "savings_rate": t["savings_rate"],
            "savings_rate_without_draws": t["savings_rate_without_draws"],
        },
        "top_categories": t["by_category"][:6],
        "budget": {
            "budgeted_cents": budget["budgeted_cents"],
            "spent_cents": budget["spent_cents"],
            "counts": budget["counts"],
            "alerts": budget["alerts"][:6],
        },
        "upcoming_bills": upcoming[:10],
        "subscription_monthly_cents": recurring_data["subscription_monthly_cents"],
        "review_count": review,
        "goals": goals(conn)[:4],
        "recent": [_public_row(row) for row in recent],
    }


def _public_row(row: dict) -> dict:
    keys = (
        "id", "date", "amount_cents", "name", "merchant", "merchant_key", "account_id", "account_label",
        "category_id", "category", "group", "kind", "source", "confidence", "note", "tags", "pending",
        "from_business", "editable", "transfer_pair", "splits", "status",
    )
    return {key: row.get(key) for key in keys}


# --- monthly summary ------------------------------------------------------------


def monthly_summary(conn, month: str) -> dict:
    month = parse_month(month)
    cats = categories(conn)
    prior_month = prev_month(month)
    history = months_back(prior_month, 12)
    rows_all = _rows(conn, history[0] + "-01", month_end(month))
    this_rows = [row for row in rows_all if row["date"].startswith(month)]
    prior_rows = [row for row in rows_all if row["date"].startswith(prior_month)]
    t = totals(this_rows, cats)
    p = totals(prior_rows, cats)
    hist = [totals([row for row in rows_all if row["date"].startswith(m)], cats) for m in history]
    active_hist = [h for h in hist if h["income_cents"] or h["spending_cents"]] or hist
    avg_income = int(round(sum(h["income_cents"] for h in active_hist) / len(active_hist)))
    avg_spending = int(round(sum(h["spending_cents"] for h in active_hist) / len(active_hist)))
    prior_by = {item["category_id"]: item for item in p["by_category"]}
    changes = []
    seen = set()
    for item in t["by_category"]:
        seen.add(item["category_id"])
        before = prior_by.get(item["category_id"], {}).get("cents", 0)
        changes.append({"category": item["category"], "group": item["group"], "cents": item["cents"], "prior_cents": before, "delta": item["cents"] - before})
    for item in p["by_category"]:
        if item["category_id"] not in seen:
            changes.append({"category": item["category"], "group": item["group"], "cents": 0, "prior_cents": item["cents"], "delta": -item["cents"]})
    changes.sort(key=lambda item: (-abs(item["delta"]), item["category"]))
    biggest = sorted(
        [row for row in this_rows if row["kind"] == "expense" and row["amount_cents"] < 0],
        key=lambda row: (row["amount_cents"], row["date"]),
    )[:5]
    end_d = date.fromisoformat(month_end(month))
    rows_recurring = load_personal(conn, (end_d - timedelta(days=800)).isoformat(), end_d.isoformat())
    series = rec.detect(rows_recurring, end_d, recurring_overrides(conn))
    new_subs = [item for item in series if item["first_seen"].startswith(month) and item["direction"] == "out"]
    changed = [
        item for item in series
        if item["price_change"] and item["last_date"].startswith(month) and item["direction"] == "out"
    ]
    budget = budgets(conn, month)
    accounts = personal_accounts(conn)
    s = load_series(conn, accounts)

    def worth_on(day: str) -> int:
        return sum(_signed(acct, s[account_id].balance_at(day)) for account_id, acct in accounts.items() if _included(acct))

    start_worth = worth_on((date.fromisoformat(month + "-01") - timedelta(days=1)).isoformat())
    end_worth = worth_on(month_end(month))
    lines = _summary_lines(month, t, p, avg_spending, changes, new_subs, changed, budget, end_worth - start_worth)
    return {
        "month": month,
        "prior_month": prior_month,
        "income_cents": t["income_cents"],
        "earned_cents": t["earned_cents"],
        "owner_draws_cents": t["owner_draws_cents"],
        "spending_cents": t["spending_cents"],
        "net_cents": t["net_cents"],
        "savings_rate": t["savings_rate"],
        "savings_rate_without_draws": t["savings_rate_without_draws"],
        "prior": {"income_cents": p["income_cents"], "spending_cents": p["spending_cents"], "savings_rate": p["savings_rate"]},
        "average_12": {"income_cents": avg_income, "spending_cents": avg_spending},
        "sources": t["sources"],
        "category_changes": changes[:5],
        "biggest": [_public_row(row) for row in biggest],
        "new_subscriptions": [{"merchant": item["merchant"], "typical_cents": item["typical_cents"], "cadence": item["cadence"]} for item in new_subs],
        "changed_subscriptions": [
            {"merchant": item["merchant"], "from_cents": item["price_change"]["from_cents"], "to_cents": item["price_change"]["to_cents"]}
            for item in changed
        ],
        "budget": {"budgeted_cents": budget["budgeted_cents"], "spent_cents": budget["spent_cents"], "rows": budget["rows"], "counts": budget["counts"]},
        "net_worth_start_cents": start_worth,
        "net_worth_end_cents": end_worth,
        "net_worth_change_cents": end_worth - start_worth,
        "summary": lines,
    }


def _money(cents: int) -> str:
    from hpgbooks.db import format_money

    return "$" + format_money(abs(int(cents))) if cents >= 0 else "-$" + format_money(abs(int(cents)))


def _summary_lines(month, t, p, avg_spending, changes, new_subs, changed, budget, worth_change) -> list[str]:
    label_month = date.fromisoformat(month + "-01").strftime("%B %Y")
    lines = [f"In {label_month} you brought in {_money(t['income_cents'])} and spent {_money(t['spending_cents'])}."]
    if t["owner_draws_cents"]:
        lines.append(f"{_money(t['owner_draws_cents'])} of the money in came from owner draws; earned income was {_money(t['earned_cents'])}.")
    if t["savings_rate"] is not None:
        text = f"Savings rate was {t['savings_rate']:.1f}%"
        if t["savings_rate_without_draws"] is not None and t["owner_draws_cents"]:
            text += f" ({t['savings_rate_without_draws']:.1f}% without owner draws)"
        lines.append(text + ".")
    diff = t["spending_cents"] - p["spending_cents"]
    if p["spending_cents"]:
        word = "more" if diff > 0 else "less"
        lines.append(f"Spending was {_money(abs(diff))} {word} than the month before.")
    if avg_spending:
        word = "above" if t["spending_cents"] > avg_spending else "below"
        lines.append(f"That is {word} the 12-month average of {_money(avg_spending)}.")
    for item in changes[:3]:
        if item["delta"]:
            word = "up" if item["delta"] > 0 else "down"
            lines.append(f"{item['category']} was {word} {_money(abs(item['delta']))}.")
    for item in new_subs[:3]:
        lines.append(f"New recurring charge: {item['merchant']} at {_money(item['typical_cents'])} ({item['cadence']}).")
    for item in changed[:3]:
        lines.append(f"{item['merchant']} changed price from {_money(item['price_change']['from_cents'])} to {_money(item['price_change']['to_cents'])}.")
    over = [row["name"] for row in budget["rows"] if row["status"] == "over"]
    if budget["rows"]:
        if over:
            lines.append(f"Over budget: {', '.join(over[:5])}.")
        else:
            lines.append("Every budget stayed within its limit.")
    if worth_change:
        word = "rose" if worth_change > 0 else "fell"
        lines.append(f"Net worth {word} {_money(abs(worth_change))}.")
    return lines


# --- reconciliation -------------------------------------------------------------


def reconcile(conn, month: str) -> dict:
    """Tie personal income + draws - spending + transfers to the change in personal balances.

    Every personal-account row lands in one bucket, so the bucket sum equals the
    raw activity; the check is against the balance history (anchors), and the
    listed differences are pending rows, unmatched transfer legs, loan payments
    booked as spending, and anchor adjustments.
    """
    month = parse_month(month)
    start, end = month + "-01", month_end(month)
    cats = categories(conn)
    rows = _rows(conn, start, end)
    own = [row for row in rows if not row["from_business"]]
    synthesized = [row for row in rows if row["from_business"]]
    t = totals(own, cats)
    raw = sum(row["amount_cents"] for row in own)
    expected = t["earned_cents"] + t["owner_draws_cents"] - t["spending_cents"] + t["transfers_net_cents"]
    accounts = personal_accounts(conn)
    series = load_series(conn, accounts)
    before = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
    change = 0
    per_account = []
    for account_id, acct in accounts.items():
        s = series[account_id]
        delta = _signed(acct, s.balance_at(end)) - _signed(acct, s.balance_at(before))
        activity = sum(row["amount_cents"] for row in own if row["account_id"] == account_id)
        change += delta
        per_account.append({"id": account_id, "label": label(acct), "change_cents": delta, "activity_cents": activity, "anchor_adjustment_cents": delta - activity})
    pending = [_public_row(row) for row in own if row["pending"]]
    unmatched = [
        _public_row(row) for row in own
        if row["kind"] == "transfer" and not row.get("transfer_pair") and row["account_class"] != "loan"
    ]
    # The credit on a loan account is a transfer leg; its payment is spending on the paying side.
    loan_spend = [
        _public_row(row) for row in own
        if row["kind"] == "transfer" and row["account_class"] == "loan"
    ]
    return {
        "month": month,
        "earned_cents": t["earned_cents"],
        "owner_draws_cents": t["owner_draws_cents"],
        "spending_cents": t["spending_cents"],
        "transfers_net_cents": t["transfers_net_cents"],
        "expected_change_cents": expected,
        "activity_cents": raw,
        "balance_change_cents": change,
        "difference_cents": change - expected,
        "ties": raw == expected,
        "pending": pending,
        "pending_cents": sum(row["amount_cents"] for row in pending),
        "unmatched_transfers": unmatched,
        "loan_payments_as_spending": loan_spend,
        "loan_payments_cents": sum(row["amount_cents"] for row in loan_spend),
        "accounts": per_account,
        "paid_by_business": {
            "funding_cents": sum(row["amount_cents"] for row in synthesized if row["kind"] == "funding"),
            "spending_cents": -sum(row["amount_cents"] for row in synthesized if row["kind"] == "expense"),
            "count": len(synthesized),
        },
    }
