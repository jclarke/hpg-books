"""Reports over the WHMCS copy: revenue, MRR, churn, refunds, dunning,
PayPal reconciliation, and the customer lookup.

Everything here reads the encrypted hpgbooks database only. Aggregate reports
identify customers by brand and client id, never by name or email; names and
emails appear only in customer_search and customer_detail, which the web app
serves behind sign-in.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import date, timedelta

from hpgbooks.config import get_config
from hpgbooks.db import HpgbooksError, cents_to_dollars, format_money
from hpgbooks.reports import months_covering, require_date
from hpgbooks.whmcs import brand_names, plan_labels, whmcs_ready, whmcs_tables_exist

# Months per billing cycle. Free and one-time services carry no recurring revenue.
CYCLE_MONTHS = {
    "monthly": 1,
    "quarterly": 3,
    "semiannually": 6,
    "annually": 12,
    "biennially": 24,
    "triennially": 36,
}
ENDED_STATUSES = ("Cancelled", "Terminated")
LIVE_STATUSES = ("Active", "Suspended")
AGING_BUCKETS = (
    ("Not yet due", None, 0),
    ("1-30 days", 1, 30),
    ("31-60 days", 31, 60),
    ("61-90 days", 61, 90),
    ("91-180 days", 91, 180),
    ("181-365 days", 181, 365),
    ("Over 1 year", 366, None),
)
REASON_CATEGORIES = (
    ("Price", re.compile(r"expens|price|pricing|cost|cheap|afford|money|budget|pay less|too much", re.I)),
    ("Moved to another provider", re.compile(r"moving|moved|migrat|switch|another (host|provider|service|company)|other (host|provider)|github|gitlab|bitbucket|aws|amazon|heroku|digital ?ocean|linode|vps elsewhere|in-house|own server|self[- ]host", re.I)),
    ("No longer needed", re.compile(r"no longer|not (needed|using|used)|don'?t need|dont need|do not need|unused|not in use|project (ended|over|finished|complete|cancel)|finished|retir|shut|clos(e|ing|ed)|out of business|discontinu", re.I)),
    ("Service or support problems", re.compile(r"slow|down ?time|outage|support|problem|issue|unreliab|reliab|performance|bug|broken|error|poor", re.I)),
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# --- small helpers ---------------------------------------------------------------------


def check_brand(brand: str | None) -> str | None:
    """None for all brands, else the exact brand name. Raises on anything else."""
    if brand is None or brand == "" or brand.lower() == "all":
        return None
    for name in brand_names():
        if name.lower() == brand.strip().lower():
            return name
    raise HpgbooksError(f"unknown brand {brand!r}; use one of {', '.join(brand_names())}")


def _brand_sql(brand: str | None, column: str = "brand") -> tuple[str, list]:
    if brand is None:
        return "", []
    return f" AND {column} = ?", [brand]


def cycle_months(cycle: str | None) -> int | None:
    key = re.sub(r"[^a-z]", "", (cycle or "").lower())
    return CYCLE_MONTHS.get(key)


def monthly_cents(amount_cents: int, cycle: str | None) -> float:
    months = cycle_months(cycle)
    if not months or amount_cents <= 0:
        return 0.0
    return amount_cents / months


def _pct(part: float, whole: float) -> float | None:
    if not whole:
        return None
    return round(part * 100.0 / whole, 2)


def _idx(day: str) -> int:
    return int(day[:4]) * 12 + int(day[5:7]) - 1


def _month(idx: int) -> str:
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _period(month: str, by: str) -> str:
    if by == "year":
        return month[:4]
    if by == "quarter":
        return f"{month[:4]}-Q{(int(month[5:7]) - 1) // 3 + 1}"
    return month


def _today(today: str | None) -> str:
    return require_date(today) if today else date.today().isoformat()


def _default_range(start: str | None, end: str | None, today: str) -> tuple[str, str]:
    if start and end:
        start, end = require_date(start), require_date(end)
        if start > end:
            raise HpgbooksError("start must be on or before end")
        return start, end
    if start or end:
        raise HpgbooksError("start and end go together")
    return f"{today[:4]}-01-01", today


def redact(text: str | None) -> str:
    """Free text from customers, with email addresses removed."""
    return EMAIL_RE.sub("[email]", (text or "").strip())


def reason_category(reason: str | None) -> str:
    text = (reason or "").strip()
    if not text:
        return "No reason given"
    for label, pattern in REASON_CATEGORIES:
        if pattern.search(text):
            return label
    return "Other"


def _require_ready(conn) -> None:
    if not whmcs_tables_exist(conn):
        raise HpgbooksError("WHMCS has not been synced yet; run: hpgbooks whmcs sync")


def _groups(conn) -> dict[tuple[str, int], str]:
    return {
        (row["brand"], int(row["source_id"])): row["group_name"] or ""
        for row in conn.execute("SELECT brand, source_id, group_name FROM whmcs_products WHERE kind = 'product'")
    }


# --- services and lifetimes ---------------------------------------------------------------


def load_services(conn, brand: str | None = None) -> list[dict]:
    """Recurring services with a reconstructed [start, end) lifetime.

    End date, in order of preference: the WHMCS termination date, the latest
    cancellation request, then the next due date (the paid-through date).
    Active and Suspended services have no end. Pending, Fraud, and Completed
    services, and free or one-time cycles, are left out.
    """
    clause, params = _brand_sql(brand)
    cancels = {
        (row["brand"], int(row["service_id"])): row["d"]
        for row in conn.execute(
            f"SELECT brand, service_id, MAX(date) AS d FROM whmcs_cancel_requests WHERE service_id IS NOT NULL{clause} GROUP BY brand, service_id",
            params,
        )
    }
    groups = _groups(conn)
    labels: dict[str, dict] = {}
    out = []
    for row in conn.execute(
        f"""
        SELECT brand, kind, source_id, client_id, product_id, status, billing_cycle, amount_cents,
               reg_date, next_due_date, termination_date
        FROM whmcs_services WHERE 1 = 1{clause}
        """,
        params,
    ):
        status = row["status"] or ""
        if status not in LIVE_STATUSES + ENDED_STATUSES:
            continue
        mrr = monthly_cents(int(row["amount_cents"]), row["billing_cycle"])
        if mrr <= 0 or not row["reg_date"]:
            continue
        name = row["brand"]
        if name not in labels:
            labels[name] = plan_labels(conn, name)
        sid = int(row["source_id"])
        end = None
        end_source = None
        if status in ENDED_STATUSES:
            for source, value in (
                ("termination", row["termination_date"]),
                ("cancel_request", cancels.get((name, sid)) if row["kind"] == "hosting" else None),
                ("next_due", row["next_due_date"]),
            ):
                if value:
                    end, end_source = value[:10], source
                    break
            if end is None:
                end, end_source = row["reg_date"], "unknown"
        pid = row["product_id"]
        out.append(
            {
                "brand": name,
                "kind": row["kind"],
                "id": sid,
                "client_id": row["client_id"],
                "plan": labels[name].get((row["kind"], sid), "Unknown plan"),
                "group": groups.get((name, int(pid)), "") if row["kind"] == "hosting" and pid is not None else "Addons",
                "status": status,
                "cycle": row["billing_cycle"],
                "amount_cents": int(row["amount_cents"]),
                "mrr": mrr,
                "start": row["reg_date"],
                "end": end,
                "end_source": end_source,
            }
        )
    return out


def _span(service: dict, now_idx: int) -> tuple[int, int] | None:
    """First and last month-end (as month indexes) at which the service was active."""
    first = _idx(service["start"])
    last = now_idx if service["end"] is None else min(now_idx, _idx(service["end"]) - 1)
    if service["end"] is not None and service["end"] <= service["start"]:
        return None
    if last < first:
        return None
    return first, last


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for first, last in sorted(spans):
        if merged and first <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], last)
        else:
            merged.append([first, last])
    return [(a, b) for a, b in merged]


class _Timeline:
    """Month-end snapshots plus new/churn events derived from service lifetimes."""

    def __init__(self, services: list[dict], now_idx: int, key=lambda s: "all"):
        self.now_idx = now_idx
        self.active_mrr: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        self.active_services: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        self.new_services: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        self.new_mrr: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        self.churned_services: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        self.churned_mrr: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        self.customers: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        self.new_customers: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        self.churned_customers: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        client_spans: dict[tuple[str, str, int], list[tuple[int, int]]] = defaultdict(list)
        self.first_idx = now_idx
        for service in services:
            span = _span(service, now_idx)
            if span is None:
                continue
            first, last = span
            self.first_idx = min(self.first_idx, first)
            group = key(service)
            # Difference arrays: +at first, -after last.
            self.active_mrr[group][first] += service["mrr"]
            self.active_mrr[group][last + 1] -= service["mrr"]
            self.active_services[group][first] += 1
            self.active_services[group][last + 1] -= 1
            self.new_services[group][first] += 1
            self.new_mrr[group][first] += service["mrr"]
            if last < now_idx:
                self.churned_services[group][last + 1] += 1
                self.churned_mrr[group][last + 1] += service["mrr"]
            if service["client_id"] is not None:
                client_spans[(group, service["brand"], int(service["client_id"]))].append(span)
        for (group, _brand, _client), spans in client_spans.items():
            for first, last in _merge(spans):
                self.customers[group][first] += 1
                self.customers[group][last + 1] -= 1
                self.new_customers[group][first] += 1
                if last < now_idx:
                    self.churned_customers[group][last + 1] += 1
        self._cache: dict[tuple[str, str], dict[int, float]] = {}

    def groups(self) -> list[str]:
        return sorted(self.active_mrr)

    def _running(self, group: str, name: str) -> dict[int, float]:
        cache_key = (group, name)
        if cache_key not in self._cache:
            deltas = getattr(self, name)[group]
            running = {}
            total = 0.0
            for idx in range(min(deltas, default=self.now_idx), self.now_idx + 1):
                total += deltas.get(idx, 0)
                running[idx] = total
            self._cache[cache_key] = running
        return self._cache[cache_key]

    def at(self, group: str, idx: int) -> dict:
        """Snapshot at the end of month idx (for the current month: today)."""
        if idx < self.first_idx:
            return {"mrr": 0.0, "services": 0, "customers": 0}
        idx = min(idx, self.now_idx)
        return {
            "mrr": self._running(group, "active_mrr").get(idx, 0.0),
            "services": int(round(self._running(group, "active_services").get(idx, 0))),
            "customers": int(round(self._running(group, "customers").get(idx, 0))),
        }

    def events(self, group: str, idx: int) -> dict:
        return {
            "new_services": self.new_services[group].get(idx, 0),
            "new_mrr": self.new_mrr[group].get(idx, 0.0),
            "churned_services": self.churned_services[group].get(idx, 0),
            "churned_mrr": self.churned_mrr[group].get(idx, 0.0),
            "new_customers": self.new_customers[group].get(idx, 0),
            "churned_customers": self.churned_customers[group].get(idx, 0),
        }


# --- revenue -------------------------------------------------------------------------------


def _money_row(row) -> dict:
    gross = int(round(row["gross"] or 0))
    fees = int(round(row["fees"] or 0))
    refunds = int(round(row["refunds"] or 0))
    return {
        "gross_cents": gross,
        "fees_cents": fees,
        "refunds_cents": refunds,
        "net_cents": gross - fees - refunds,
        "payments": int(row["payments"] or 0),
        "refund_count": int(row["refund_count"] or 0),
    }


def _add(target: dict, source: dict) -> None:
    for key, value in source.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            target[key] = target.get(key, 0) + value


def revenue(conn, *, start: str | None = None, end: str | None = None, brand: str | None = None, by: str = "month", today: str | None = None) -> dict:
    """Payments in, gateway fees, refunds out, and net, by period, brand, and plan."""
    _require_ready(conn)
    if by not in ("month", "quarter", "year"):
        raise HpgbooksError("by must be month, quarter, or year")
    brand = check_brand(brand)
    start, end = _default_range(start, end, _today(today))
    clause, params = _brand_sql(brand, "p.brand")
    window = [start, end + " 23:59:59"]
    periods: dict[str, dict] = {}
    brands: dict[str, dict] = {}
    for row in conn.execute(
        f"""
        SELECT p.brand, substr(p.date, 1, 7) AS month,
               SUM(p.amount_in_cents) AS gross, SUM(p.fees_cents) AS fees, SUM(p.amount_out_cents) AS refunds,
               SUM(p.amount_in_cents > 0) AS payments, SUM(p.amount_out_cents > 0) AS refund_count
        FROM whmcs_payments p
        WHERE p.date BETWEEN ? AND ?{clause}
        GROUP BY p.brand, month
        """,
        window + params,
    ):
        figures = _money_row(row)
        key = _period(row["month"], by)
        period = periods.setdefault(key, {"period": key, "by_brand": {}})
        _add(period, figures)
        _add(period["by_brand"].setdefault(row["brand"], {}), figures)
        _add(brands.setdefault(row["brand"], {"brand": row["brand"]}), figures)
    for month in months_covering(start, end):
        key = _period(month, by)
        periods.setdefault(key, {"period": key, "by_brand": {}})
    period_rows = []
    for key in sorted(periods):
        item = periods[key]
        for field in ("gross_cents", "fees_cents", "refunds_cents", "net_cents", "payments", "refund_count"):
            item.setdefault(field, 0)
        period_rows.append(item)

    groups = _groups(conn)
    plans = []
    for row in conn.execute(
        f"""
        SELECT p.brand, COALESCE(s.plan, 'Unattributed') AS plan, s.product_id,
               SUM(p.amount_in_cents * COALESCE(s.share, 1.0)) AS gross,
               SUM(p.fees_cents * COALESCE(s.share, 1.0)) AS fees,
               SUM(p.amount_out_cents * COALESCE(s.share, 1.0)) AS refunds,
               SUM(CASE WHEN p.amount_in_cents > 0 THEN COALESCE(s.share, 1.0) ELSE 0 END) AS payments,
               SUM(CASE WHEN p.amount_out_cents > 0 THEN COALESCE(s.share, 1.0) ELSE 0 END) AS refund_count
        FROM whmcs_payments p
        LEFT JOIN whmcs_invoice_plans s ON s.brand = p.brand AND s.invoice_id = p.invoice_id
        WHERE p.date BETWEEN ? AND ?{clause}
        GROUP BY p.brand, plan, s.product_id
        """,
        window + params,
    ):
        figures = _money_row(row)
        figures["payments"] = int(round(row["payments"] or 0))
        figures["refund_count"] = int(round(row["refund_count"] or 0))
        if not any(figures[key] for key in ("gross_cents", "fees_cents", "refunds_cents")):
            continue
        pid = row["product_id"]
        plans.append(
            {
                "brand": row["brand"],
                "plan": row["plan"],
                "group": groups.get((row["brand"], int(pid)), "") if pid is not None else "",
                **figures,
            }
        )
    plans.sort(key=lambda item: (-item["gross_cents"], item["brand"], item["plan"]))
    totals: dict = {}
    for item in brands.values():
        _add(totals, {key: value for key, value in item.items() if key != "brand"})
    for field in ("gross_cents", "fees_cents", "refunds_cents", "net_cents", "payments", "refund_count"):
        totals.setdefault(field, 0)
    return {
        "start": start,
        "end": end,
        "brand": brand or "all",
        "by": by,
        "periods": period_rows,
        "brands": [brands[name] for name in brand_names() if name in brands],
        "plans": plans,
        "totals": totals,
    }


# --- MRR -------------------------------------------------------------------------------------


def mrr(conn, *, brand: str | None = None, months: int = 36, today: str | None = None) -> dict:
    """Current MRR from Active services, and an estimated month-end trend."""
    _require_ready(conn)
    brand = check_brand(brand)
    today = _today(today)
    services = load_services(conn, brand)
    now_idx = _idx(today)

    active = [item for item in services if item["status"] == "Active"]
    by_brand: dict[str, dict] = {}
    by_plan: dict[tuple[str, str], dict] = {}
    customers: set[tuple[str, int]] = set()
    for item in active:
        entry = by_brand.setdefault(item["brand"], {"brand": item["brand"], "mrr": 0.0, "services": 0, "_customers": set()})
        entry["mrr"] += item["mrr"]
        entry["services"] += 1
        if item["client_id"] is not None:
            entry["_customers"].add(item["client_id"])
            customers.add((item["brand"], item["client_id"]))
        plan = by_plan.setdefault(
            (item["brand"], item["plan"]),
            {"brand": item["brand"], "plan": item["plan"], "group": item["group"], "mrr": 0.0, "services": 0, "_customers": set()},
        )
        plan["mrr"] += item["mrr"]
        plan["services"] += 1
        if item["client_id"] is not None:
            plan["_customers"].add(item["client_id"])
    total_mrr = sum(item["mrr"] for item in active)

    def finish(entry: dict) -> dict:
        count = len(entry.pop("_customers"))
        value = entry.pop("mrr")
        entry.update(
            {
                "mrr_cents": int(round(value)),
                "arr_cents": int(round(value * 12)),
                "customers": count,
                "arpu_cents": int(round(value / count)) if count else 0,
                "share_pct": _pct(value, total_mrr),
            }
        )
        return entry

    brand_rows = [finish(by_brand[name]) for name in brand_names() if name in by_brand]
    plan_rows = sorted((finish(item) for item in by_plan.values()), key=lambda item: -item["mrr_cents"])

    cycles: dict[str, dict] = {}
    for item in active:
        entry = cycles.setdefault(item["cycle"] or "", {"cycle": item["cycle"] or "", "services": 0, "mrr": 0.0})
        entry["services"] += 1
        entry["mrr"] += item["mrr"]
    cycle_rows = sorted(
        ({"cycle": item["cycle"], "services": item["services"], "mrr_cents": int(round(item["mrr"]))} for item in cycles.values()),
        key=lambda item: -item["mrr_cents"],
    )
    suspended = [item for item in services if item["status"] == "Suspended"]

    timeline = _Timeline(services, now_idx, key=lambda s: s["brand"])
    first = timeline.first_idx if months <= 0 else max(timeline.first_idx, now_idx - months + 1)
    trend = []
    for idx in range(first, now_idx + 1):
        point = {"month": _month(idx), "mrr_cents": 0, "customers": 0, "services": 0, "by_brand": {}}
        for group in timeline.groups():
            snap = timeline.at(group, idx)
            point["by_brand"][group] = int(round(snap["mrr"]))
            point["mrr_cents"] += snap["mrr"]
            point["customers"] += snap["customers"]
            point["services"] += snap["services"]
        point["mrr_cents"] = int(round(point["mrr_cents"]))
        trend.append(point)
    return {
        "today": today,
        "brand": brand or "all",
        "mrr_cents": int(round(total_mrr)),
        "arr_cents": int(round(total_mrr * 12)),
        "customers": len(customers),
        "services": len(active),
        "arpu_cents": int(round(total_mrr / len(customers))) if customers else 0,
        "suspended_mrr_cents": int(round(sum(item["mrr"] for item in suspended))),
        "suspended_services": len(suspended),
        "brands": brand_rows,
        "plans": plan_rows,
        "cycles": cycle_rows,
        "trend": trend,
        "trend_note": (
            "Estimate. Month-end MRR is rebuilt from registration, termination, and cancellation dates "
            "at each service's current price. Suspended services count as active in the trend; "
            "the current MRR figure counts Active services only."
        ),
        "end_date_sources": _end_sources(services),
    }


def _end_sources(services: list[dict]) -> dict:
    counts: dict[str, int] = defaultdict(int)
    for item in services:
        if item["end_source"]:
            counts[item["end_source"]] += 1
    return dict(counts)


def summary(conn, today: str | None = None) -> dict:
    """Dashboard widget: current MRR, active customers, last sync. No customer data."""
    from hpgbooks.whmcs import sync_status

    if not whmcs_ready(conn):
        return {"ready": False, "mrr_cents": 0, "arr_cents": 0, "customers": 0, "last_sync": None, "brands": []}
    report = mrr(conn, months=12, today=today)
    status = sync_status(conn, log_limit=1)
    return {
        "ready": True,
        "mrr_cents": report["mrr_cents"],
        "arr_cents": report["arr_cents"],
        "customers": report["customers"],
        "last_sync": status["last_sync"],
        "last_status": status["log"][0]["status"] if status["log"] else None,
        "brands": [{"brand": row["brand"], "mrr_cents": row["mrr_cents"], "customers": row["customers"]} for row in report["brands"]],
        "spark": [point["mrr_cents"] for point in report["trend"]],
    }


# --- churn -------------------------------------------------------------------------------------


def churn(conn, *, start: str | None = None, end: str | None = None, brand: str | None = None, today: str | None = None) -> dict:
    """Cancellations, ended services, logo and revenue churn, and net adds per month."""
    _require_ready(conn)
    brand = check_brand(brand)
    today = _today(today)
    start, end = _default_range(start, end, today)
    months = months_covering(start, end)
    now_idx = _idx(today)
    services = load_services(conn, brand)
    overall = _Timeline(services, now_idx)
    plans = _Timeline(services, now_idx, key=lambda s: f"{s['brand']}\u0000{s['plan']}")
    brands = _Timeline(services, now_idx, key=lambda s: s["brand"])

    clause, params = _brand_sql(brand, "r.brand")
    labels = {name: plan_labels(conn, name) for name in brand_names()}
    requests = conn.execute(
        f"""
        SELECT r.brand, r.source_id, r.date, r.type, r.reason, r.service_id
        FROM whmcs_cancel_requests r
        WHERE r.date BETWEEN ? AND ?{clause}
        ORDER BY r.date DESC, r.source_id DESC
        """,
        [start, end] + params,
    ).fetchall()
    req_by_month: dict[str, dict] = defaultdict(lambda: {"requests": 0, "immediate": 0, "end_of_period": 0})
    categories: dict[str, int] = defaultdict(int)
    plan_requests: dict[tuple[str, str], int] = defaultdict(int)
    recent = []
    for row in requests:
        month = row["date"][:7]
        kind = "immediate" if (row["type"] or "").lower().startswith("immediate") else "end_of_period"
        req_by_month[month]["requests"] += 1
        req_by_month[month][kind] += 1
        category = reason_category(row["reason"])
        categories[category] += 1
        plan = labels.get(row["brand"], {}).get(("hosting", int(row["service_id"] or 0)), "Unknown plan")
        plan_requests[(row["brand"], plan)] += 1
        if len(recent) < 200:
            recent.append(
                {
                    "id": f"{row['brand']}:{row['source_id']}",
                    "date": row["date"],
                    "brand": row["brand"],
                    "plan": plan,
                    "type": "Immediate" if kind == "immediate" else "End of billing period",
                    "category": category,
                    "reason": redact(row["reason"])[:300],
                }
            )

    rows = []
    totals = defaultdict(float)
    for month in months:
        idx = _idx(month)
        before = overall.at("all", idx - 1)
        after = overall.at("all", idx)
        events = overall.events("all", idx)
        req = req_by_month.get(month, {"requests": 0, "immediate": 0, "end_of_period": 0})
        row = {
            "month": month,
            "cancel_requests": req["requests"],
            "immediate": req["immediate"],
            "end_of_period": req["end_of_period"],
            "start_customers": before["customers"],
            "end_customers": after["customers"],
            "new_customers": events["new_customers"],
            "churned_customers": events["churned_customers"],
            "net_customer_adds": events["new_customers"] - events["churned_customers"],
            "start_services": before["services"],
            "new_services": events["new_services"],
            "churned_services": events["churned_services"],
            "net_adds": events["new_services"] - events["churned_services"],
            "start_mrr_cents": int(round(before["mrr"])),
            "new_mrr_cents": int(round(events["new_mrr"])),
            "churned_mrr_cents": int(round(events["churned_mrr"])),
            "end_mrr_cents": int(round(after["mrr"])),
            "logo_churn_pct": _pct(events["churned_customers"], before["customers"]),
            "revenue_churn_pct": _pct(events["churned_mrr"], before["mrr"]),
        }
        rows.append(row)
        for key in ("cancel_requests", "immediate", "end_of_period", "new_customers", "churned_customers", "new_services", "churned_services", "new_mrr_cents", "churned_mrr_cents"):
            totals[key] += row[key]

    first_idx = _idx(months[0])
    last_idx = _idx(months[-1])

    def range_figures(timeline: _Timeline, group: str) -> dict:
        before = timeline.at(group, first_idx - 1)
        after = timeline.at(group, last_idx)
        new = churned = new_customers = churned_customers = 0
        new_mrr = churned_mrr = 0.0
        for idx in range(first_idx, last_idx + 1):
            events = timeline.events(group, idx)
            new += events["new_services"]
            churned += events["churned_services"]
            new_mrr += events["new_mrr"]
            churned_mrr += events["churned_mrr"]
            new_customers += events["new_customers"]
            churned_customers += events["churned_customers"]
        return {
            "start_services": before["services"],
            "end_services": after["services"],
            "start_customers": before["customers"],
            "new_services": new,
            "churned_services": churned,
            "net_adds": new - churned,
            "new_customers": new_customers,
            "churned_customers": churned_customers,
            "start_mrr_cents": int(round(before["mrr"])),
            "new_mrr_cents": int(round(new_mrr)),
            "churned_mrr_cents": int(round(churned_mrr)),
            "service_churn_pct": _pct(churned, before["services"]),
            "logo_churn_pct": _pct(churned_customers, before["customers"]),
            "revenue_churn_pct": _pct(churned_mrr, before["mrr"]),
        }

    plan_rows = []
    for group in plans.groups():
        brand_name, plan = group.split("\u0000", 1)
        figures = range_figures(plans, group)
        figures["cancel_requests"] = plan_requests.get((brand_name, plan), 0)
        if not (figures["start_services"] or figures["new_services"] or figures["churned_services"] or figures["cancel_requests"]):
            continue
        plan_rows.append({"brand": brand_name, "plan": plan, **figures})
    plan_rows.sort(key=lambda item: (-item["churned_mrr_cents"], -item["churned_services"], item["plan"]))
    brand_rows = [{"brand": name, **range_figures(brands, name)} for name in brand_names() if name in brands.groups()]
    summary_figures = range_figures(overall, "all")
    summary_figures.update({key: int(totals[key]) for key in ("cancel_requests", "immediate", "end_of_period")})
    return {
        "start": start,
        "end": end,
        "brand": brand or "all",
        "months": rows,
        "totals": summary_figures,
        "brands": brand_rows,
        "plans": plan_rows,
        "reasons": [
            {"category": label, "count": count, "share_pct": _pct(count, len(requests))}
            for label, count in sorted(categories.items(), key=lambda item: -item[1])
        ],
        "recent": recent,
        "note": (
            "A service churns in the month its termination date (or cancellation request, or paid-through date) falls; "
            "a customer churns when their last paid service ends. Logo churn is churned customers over customers "
            "at the start of the month; revenue churn is MRR lost over MRR at the start of the month (estimate, current prices)."
        ),
    }


# --- refunds -------------------------------------------------------------------------------------


def refunds(conn, *, start: str | None = None, end: str | None = None, brand: str | None = None, today: str | None = None, limit: int = 25) -> dict:
    _require_ready(conn)
    brand = check_brand(brand)
    start, end = _default_range(start, end, _today(today))
    rev = revenue(conn, start=start, end=end, brand=brand, by="month", today=today)
    months = [
        {
            "month": item["period"],
            "refunds_cents": item["refunds_cents"],
            "count": item["refund_count"],
            "gross_cents": item["gross_cents"],
            "rate_pct": _pct(item["refunds_cents"], item["gross_cents"]),
        }
        for item in rev["periods"]
    ]
    brand_rows = [
        {
            "brand": item["brand"],
            "refunds_cents": item["refunds_cents"],
            "count": item["refund_count"],
            "gross_cents": item["gross_cents"],
            "rate_pct": _pct(item["refunds_cents"], item["gross_cents"]),
        }
        for item in rev["brands"]
    ]
    plan_rows = [
        {
            "brand": item["brand"],
            "plan": item["plan"],
            "refunds_cents": item["refunds_cents"],
            "count": item["refund_count"],
            "gross_cents": item["gross_cents"],
            "rate_pct": _pct(item["refunds_cents"], item["gross_cents"]),
        }
        for item in rev["plans"]
        if item["refunds_cents"]
    ]
    plan_rows.sort(key=lambda item: -item["refunds_cents"])
    clause, params = _brand_sql(brand, "p.brand")
    window = [start, end + " 23:59:59"]
    gateways = [
        {"gateway": row["gateway"] or "(none)", "refunds_cents": int(row["refunds"]), "count": int(row["n"])}
        for row in conn.execute(
            f"""
            SELECT p.gateway, SUM(p.amount_out_cents) AS refunds, COUNT(*) AS n FROM whmcs_payments p
            WHERE p.amount_out_cents > 0 AND p.date BETWEEN ? AND ?{clause}
            GROUP BY p.gateway ORDER BY refunds DESC
            """,
            window + params,
        )
    ]
    main_plan = _main_plans(conn)
    largest = [
        {
            "id": f"{row['brand']}:{row['source_id']}",
            "date": row["date"],
            "brand": row["brand"],
            "client_id": row["client_id"],
            "invoice_id": row["invoice_id"] or None,
            "gateway": row["gateway"] or "",
            "refund_cents": int(row["amount_out_cents"]),
            "plan": main_plan.get((row["brand"], row["invoice_id"]), "Unattributed"),
            "trans_id": row["trans_id"] or "",
        }
        for row in conn.execute(
            f"""
            SELECT p.source_id, p.date, p.brand, p.client_id, p.invoice_id, p.gateway, p.amount_out_cents, p.trans_id
            FROM whmcs_payments p
            WHERE p.amount_out_cents > 0 AND p.date BETWEEN ? AND ?{clause}
            ORDER BY p.amount_out_cents DESC, p.date DESC LIMIT ?
            """,
            window + params + [max(1, min(limit, 500))],
        )
    ]
    totals = {
        "refunds_cents": rev["totals"]["refunds_cents"],
        "count": rev["totals"]["refund_count"],
        "gross_cents": rev["totals"]["gross_cents"],
        "rate_pct": _pct(rev["totals"]["refunds_cents"], rev["totals"]["gross_cents"]),
    }
    return {"start": start, "end": end, "brand": brand or "all", "months": months, "brands": brand_rows, "plans": plan_rows, "gateways": gateways, "largest": largest, "totals": totals}


def _main_plans(conn) -> dict[tuple[str, int], str]:
    """The plan with the largest share of each invoice."""
    out: dict[tuple[str, int], tuple[float, str]] = {}
    for row in conn.execute("SELECT brand, invoice_id, plan, share FROM whmcs_invoice_plans"):
        key = (row["brand"], int(row["invoice_id"]))
        if key not in out or row["share"] > out[key][0]:
            out[key] = (row["share"], row["plan"])
    return {key: value[1] for key, value in out.items()}


# --- dunning ---------------------------------------------------------------------------------------


def _bucket(days_overdue: int) -> str:
    for label, low, high in AGING_BUCKETS:
        if low is None and days_overdue <= high:
            return label
        if low is not None and days_overdue >= low and (high is None or days_overdue <= high):
            return label
    return AGING_BUCKETS[-1][0]


def dunning(conn, *, brand: str | None = None, start: str | None = None, end: str | None = None, today: str | None = None, limit: int = 1000) -> dict:
    """Open (Unpaid) invoices with aging, a collections list, and the unpaid trend."""
    _require_ready(conn)
    brand = check_brand(brand)
    today = _today(today)
    if not (start or end):
        # Default trend window: the last 24 months, this one included.
        start, end = _month(_idx(today) - 23) + "-01", today
    start, end = _default_range(start, end, today)
    clause, params = _brand_sql(brand, "i.brand")
    live = {
        (row["brand"], int(row["client_id"])): int(row["n"])
        for row in conn.execute(
            "SELECT brand, client_id, COUNT(*) AS n FROM whmcs_services WHERE status IN ('Active', 'Suspended') AND client_id IS NOT NULL GROUP BY brand, client_id"
        )
    }
    today_date = date.fromisoformat(today)
    open_rows = []
    for row in conn.execute(
        f"""
        SELECT i.brand, i.source_id, i.client_id, i.date, i.due_date, i.total_cents, i.payment_method,
               i.last_capture_attempt, c.status AS client_status,
               COALESCE(p.paid, 0) AS paid
        FROM whmcs_invoices i
        LEFT JOIN whmcs_clients c ON c.brand = i.brand AND c.source_id = i.client_id
        LEFT JOIN (
            SELECT brand, invoice_id, SUM(amount_in_cents) - SUM(amount_out_cents) AS paid
            FROM whmcs_payments GROUP BY brand, invoice_id
        ) p ON p.brand = i.brand AND p.invoice_id = i.source_id
        WHERE i.status = 'Unpaid'{clause}
        """,
        params,
    ):
        balance = int(row["total_cents"]) - max(0, int(row["paid"] or 0))
        if balance <= 0:
            continue
        due = row["due_date"] or row["date"]
        days = (today_date - date.fromisoformat(due)).days if due else 0
        services = live.get((row["brand"], int(row["client_id"] or 0)), 0)
        open_rows.append(
            {
                "brand": row["brand"],
                "invoice_id": int(row["source_id"]),
                "client_id": row["client_id"],
                "client_status": row["client_status"] or "",
                "date": row["date"],
                "due_date": due,
                "days_overdue": max(0, days),
                "bucket": _bucket(days),
                "balance_cents": balance,
                "total_cents": int(row["total_cents"]),
                "payment_method": row["payment_method"] or "",
                "last_capture_attempt": row["last_capture_attempt"],
                "live_services": services,
                "collectible": services > 0,
            }
        )
    aging = []
    for label, _low, _high in AGING_BUCKETS:
        items = [row for row in open_rows if row["bucket"] == label]
        aging.append(
            {
                "bucket": label,
                "count": len(items),
                "balance_cents": sum(row["balance_cents"] for row in items),
                "collectible_cents": sum(row["balance_cents"] for row in items if row["collectible"]),
            }
        )
    gateways: dict[str, dict] = {}
    for row in open_rows:
        entry = gateways.setdefault(row["payment_method"] or "(none)", {"gateway": row["payment_method"] or "(none)", "count": 0, "balance_cents": 0, "overdue_count": 0, "overdue_cents": 0})
        entry["count"] += 1
        entry["balance_cents"] += row["balance_cents"]
        if row["days_overdue"] > 0:
            entry["overdue_count"] += 1
            entry["overdue_cents"] += row["balance_cents"]

    trend_rows = {month: {"month": month, "invoices": 0, "invoiced_cents": 0, "paid": 0, "paid_cents": 0, "unpaid": 0, "unpaid_cents": 0, "cancelled": 0, "cancelled_cents": 0, "refunded": 0, "capture_failed": 0} for month in months_covering(start, end)}
    trend_gateways: dict[str, dict] = {}
    for row in conn.execute(
        f"""
        SELECT substr(i.due_date, 1, 7) AS month, i.status, i.payment_method,
               COUNT(*) AS n, SUM(i.total_cents) AS cents,
               SUM(i.last_capture_attempt IS NOT NULL) AS attempted
        FROM whmcs_invoices i
        WHERE i.due_date BETWEEN ? AND ? AND i.total_cents > 0{clause}
        GROUP BY month, i.status, i.payment_method
        """,
        [start, end] + params,
    ):
        item = trend_rows.get(row["month"])
        if item is None:
            continue
        n, cents = int(row["n"]), int(row["cents"] or 0)
        status = (row["status"] or "").lower()
        item["invoices"] += n
        item["invoiced_cents"] += cents
        if status in ("paid", "unpaid", "cancelled", "refunded"):
            item[status] += n
            if status != "refunded":
                item[f"{status}_cents"] += cents
        if status in ("unpaid", "cancelled"):
            item["capture_failed"] += int(row["attempted"] or 0)
            gateway = row["payment_method"] or "(none)"
            entry = trend_gateways.setdefault(gateway, {"gateway": gateway, "invoices": 0, "unpaid_or_cancelled": 0, "cents": 0})
            entry["unpaid_or_cancelled"] += n
            entry["cents"] += cents
        gateway = row["payment_method"] or "(none)"
        trend_gateways.setdefault(gateway, {"gateway": gateway, "invoices": 0, "unpaid_or_cancelled": 0, "cents": 0})["invoices"] += n
    trend = []
    for item in trend_rows.values():
        item["unpaid_rate_pct"] = _pct(item["unpaid"] + item["cancelled"], item["invoices"])
        trend.append(item)
    for entry in trend_gateways.values():
        entry["unpaid_rate_pct"] = _pct(entry["unpaid_or_cancelled"], entry["invoices"])

    overdue = sorted((row for row in open_rows if row["days_overdue"] > 0), key=lambda row: (not row["collectible"], row["days_overdue"], -row["balance_cents"]))
    totals = {
        "open_count": len(open_rows),
        "open_cents": sum(row["balance_cents"] for row in open_rows),
        "overdue_count": len(overdue),
        "overdue_cents": sum(row["balance_cents"] for row in overdue),
        "collectible_overdue_cents": sum(row["balance_cents"] for row in overdue if row["collectible"]),
        "collectible_overdue_count": sum(1 for row in overdue if row["collectible"]),
    }
    return {
        "today": today,
        "start": start,
        "end": end,
        "brand": brand or "all",
        "totals": totals,
        "aging": aging,
        "gateways": sorted(gateways.values(), key=lambda item: -item["balance_cents"]),
        "trend": trend,
        "trend_gateways": sorted(trend_gateways.values(), key=lambda item: -item["invoices"]),
        "collections": overdue[: max(1, min(limit, 5000))],
        "note": "Collectible means the client still has an Active or Suspended service. Failed captures are unpaid or cancelled invoices that recorded a capture attempt.",
    }


# --- PayPal reconciliation ------------------------------------------------------------------------------


def _paypal_account() -> str | None:
    """The books account WHMCS PayPal payments land in: config role whmcs.paypal_account_role."""
    cfg = get_config()
    return cfg.account_for_role(cfg.whmcs.paypal_account_role)


def _books_rows(conn, account_id: str | None, start: str, end: str) -> list[dict]:
    """Revenue deposits and refunds in one books account (revenue category from whmcs.revenue_category)."""
    if not account_id:
        return []
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT t.id, t.date, t.amount_cents, t.name, t.merchant_name, t.description, t.raw_json, c.category
            FROM transactions t JOIN classifications c ON c.txn_id = t.id
            WHERE t.account_id = ? AND t.status = 'active' AND t.pending = 0 AND t.date BETWEEN ? AND ?
              AND ((c.category = ? AND t.amount_cents > 0) OR (c.category = 'Refunds' AND t.amount_cents < 0))
            ORDER BY t.date, t.id
            """,
            (account_id, start, end, get_config().whmcs.revenue_category),
        )
    ]


def _month_end(day: str) -> str:
    first_next = date(int(day[:4]) + (day[5:7] == "12"), int(day[5:7]) % 12 + 1, 1)
    return (first_next - timedelta(days=1)).isoformat()


def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def reconcile_paypal(conn, *, start: str | None = None, end: str | None = None, window: int = 3, today: str | None = None) -> dict:
    """Match WHMCS PayPal payments and refunds to the PayPal account in the books.

    Read-only: it does not change transactions or classifications. Books
    deposits are net of the PayPal fee, so a WHMCS payment matches on its net
    (amount in minus fees) or, failing that, its gross amount, within
    `window` days. A PayPal transaction id found in the books text wins first.
    """
    _require_ready(conn)
    if window < 0 or window > 15:
        raise HpgbooksError("window must be 0 to 15 days")
    today = _today(today)
    span = conn.execute(
        "SELECT MIN(date), MAX(date) FROM transactions WHERE account_id = ? AND status = 'active'",
        (_paypal_account(),),
    ).fetchone()
    books_first, books_last = span[0], span[1]
    if start and end:
        start, end = _default_range(start, end, today)
    elif start or end:
        raise HpgbooksError("start and end go together")
    else:
        start, end = books_first or f"{today[:4]}-01-01", today
    coverage = {"books_first": books_first, "books_last": books_last}
    # The ledger covers whole months from its first to its last PayPal row.
    effective_start = max(start, books_first[:7] + "-01") if books_first else start
    effective_end = min(end, _month_end(books_last)) if books_last else end
    if effective_start > effective_end:
        effective_start, effective_end = start, end

    lo, hi = _shift(effective_start, -window), _shift(effective_end, window)
    books = _books_rows(conn, _paypal_account(), lo, hi)
    whmcs = [
        dict(row)
        for row in conn.execute(
            """
            SELECT brand, source_id, client_id, invoice_id, date, gateway, amount_in_cents, fees_cents, amount_out_cents, trans_id
            FROM whmcs_payments
            WHERE lower(gateway) LIKE 'paypal%' AND date BETWEEN ? AND ? AND (amount_in_cents > 0 OR amount_out_cents > 0)
            ORDER BY date, brand, source_id
            """,
            (lo, hi + " 23:59:59"),
        )
    ]
    tokens: dict[str, int] = {}
    for index, row in enumerate(books):
        text = " ".join(str(row.get(key) or "") for key in ("name", "merchant_name", "description", "raw_json"))
        for token in re.findall(r"[A-Za-z0-9]{10,}", text):
            tokens.setdefault(token.upper(), index)
    used: set[int] = set()
    by_amount: dict[int, list[int]] = defaultdict(list)
    for index, row in enumerate(books):
        by_amount[int(row["amount_cents"])].append(index)
    matches: dict[int, tuple[int, str]] = {}
    for w_index, row in enumerate(whmcs):
        trans = (row["trans_id"] or "").strip().upper()
        if len(trans) >= 10 and trans in tokens and tokens[trans] not in used:
            matches[w_index] = (tokens[trans], "transaction id")
            used.add(tokens[trans])
    for w_index, row in enumerate(whmcs):
        if w_index in matches:
            continue
        if row["amount_in_cents"] > 0:
            targets = [(int(row["amount_in_cents"]) - int(row["fees_cents"]), "amount net of fee"), (int(row["amount_in_cents"]), "gross amount")]
        else:
            targets = [(-int(row["amount_out_cents"]), "refund amount")]
        day = date.fromisoformat(row["date"])
        best = None
        for rank, (amount, how) in enumerate(targets):
            for b_index in by_amount.get(amount, []):
                if b_index in used:
                    continue
                gap = abs((date.fromisoformat(books[b_index]["date"]) - day).days)
                if gap > window:
                    continue
                score = (gap, rank, b_index)
                if best is None or score < best[0]:
                    best = (score, b_index, how)
        if best is not None:
            matches[w_index] = (best[1], best[2])
            used.add(best[1])

    def in_range(day: str) -> bool:
        return effective_start <= day <= effective_end

    rows = []
    for w_index, row in enumerate(whmcs):
        if not in_range(row["date"]):
            continue
        net = int(row["amount_in_cents"]) - int(row["fees_cents"]) - int(row["amount_out_cents"])
        item = {
            "key": f"w:{row['brand']}:{row['source_id']}",
            "status": "matched" if w_index in matches else "whmcs_only",
            "kind": "payment" if row["amount_in_cents"] > 0 else "refund",
            "date": row["date"],
            "brand": row["brand"],
            "client_id": row["client_id"],
            "invoice_id": row["invoice_id"] or None,
            "trans_id": row["trans_id"] or "",
            "whmcs_gross_cents": int(row["amount_in_cents"]) - int(row["amount_out_cents"]),
            "whmcs_fees_cents": int(row["fees_cents"]),
            "whmcs_net_cents": net,
            "books_id": None,
            "books_date": None,
            "books_cents": None,
            "books_name": None,
            "match": None,
        }
        if w_index in matches:
            b_index, how = matches[w_index]
            book = books[b_index]
            item.update({"books_id": book["id"], "books_date": book["date"], "books_cents": int(book["amount_cents"]), "match": how})
        rows.append(item)
    for b_index, book in enumerate(books):
        if b_index in used or not in_range(book["date"]):
            continue
        rows.append(
            {
                "key": f"b:{book['id']}",
                "status": "paypal_only",
                "kind": "payment" if int(book["amount_cents"]) > 0 else "refund",
                "date": book["date"],
                "brand": None,
                "client_id": None,
                "invoice_id": None,
                "trans_id": "",
                "whmcs_gross_cents": None,
                "whmcs_fees_cents": None,
                "whmcs_net_cents": None,
                "books_id": book["id"],
                "books_date": book["date"],
                "books_cents": int(book["amount_cents"]),
                "books_name": (book["name"] or book["merchant_name"] or "")[:120],
                "match": None,
            }
        )
    rows.sort(key=lambda item: (item["date"], item["status"], item["books_id"] or "", item["trans_id"]))

    months: dict[str, dict] = {}
    for month in months_covering(effective_start, effective_end):
        months[month] = {
            "month": month,
            "matched": 0,
            "matched_whmcs_cents": 0,
            "matched_books_cents": 0,
            "whmcs_only": 0,
            "whmcs_only_cents": 0,
            "paypal_only": 0,
            "paypal_only_cents": 0,
            "whmcs_net_cents": 0,
            "books_cents": 0,
        }
    for item in rows:
        entry = months.get(item["date"][:7])
        if entry is None:
            continue
        if item["status"] == "matched":
            entry["matched"] += 1
            entry["matched_whmcs_cents"] += item["whmcs_net_cents"]
            entry["matched_books_cents"] += item["books_cents"]
        elif item["status"] == "whmcs_only":
            entry["whmcs_only"] += 1
            entry["whmcs_only_cents"] += item["whmcs_net_cents"]
        else:
            entry["paypal_only"] += 1
            entry["paypal_only_cents"] += item["books_cents"]
        if item["whmcs_net_cents"] is not None:
            entry["whmcs_net_cents"] += item["whmcs_net_cents"]
        if item["books_cents"] is not None:
            entry["books_cents"] += item["books_cents"]
    month_rows = []
    for entry in months.values():
        entry["difference_cents"] = entry["books_cents"] - entry["whmcs_net_cents"]
        month_rows.append(entry)
    totals: dict = {}
    for entry in month_rows:
        _add(totals, {key: value for key, value in entry.items() if key != "month"})
    return {
        "start": effective_start,
        "end": effective_end,
        "requested_start": start,
        "requested_end": end,
        "window_days": window,
        "coverage": coverage,
        "months": month_rows,
        "totals": totals,
        "rows": rows,
        "gateways": gateway_totals(conn, start=effective_start, end=effective_end),
        "note": (
            f"Books side: PayPal account rows classified {get_config().whmcs.revenue_category} (deposits) or Refunds. "
            "PayPal deposits arrive net of the PayPal fee, so WHMCS amounts are compared net of fees. "
            "This report reads the ledger and never changes it."
        ),
    }


def gateway_totals(conn, *, start: str, end: str) -> list[dict]:
    """WHMCS totals per month and gateway, all brands, beside the bank-side totals where known.

    Each [[whmcs.bank_sides]] entry compares one gateway with the revenue
    deposits less refunds in a books account (by role), optionally only rows
    whose text matches its pattern. Other gateways have no bank side here.
    """
    out: dict[tuple[str, str], dict] = {}
    for row in conn.execute(
        """
        SELECT substr(date, 1, 7) AS month, lower(gateway) AS gateway,
               SUM(amount_in_cents) AS gross, SUM(fees_cents) AS fees, SUM(amount_out_cents) AS refunds, COUNT(*) AS n
        FROM whmcs_payments WHERE date BETWEEN ? AND ?
        GROUP BY month, lower(gateway)
        """,
        (start, end + " 23:59:59"),
    ):
        gateway = row["gateway"] or "(none)"
        gross, fees, refunds_out = int(row["gross"] or 0), int(row["fees"] or 0), int(row["refunds"] or 0)
        out[(row["month"], gateway)] = {
            "month": row["month"],
            "gateway": gateway,
            "count": int(row["n"]),
            "gross_cents": gross,
            "fees_cents": fees,
            "refunds_cents": refunds_out,
            "net_cents": gross - fees - refunds_out,
            "bank_cents": None,
            "bank_label": None,
        }
    cfg = get_config()
    bank: dict[tuple[str, str], int] = defaultdict(int)
    labels: dict[str, str] = {}
    for side in cfg.whmcs.bank_sides:
        account = cfg.account_for_role(side.account_role)
        if not account:
            continue
        labels[side.gateway] = side.label
        pattern = re.compile(side.match, re.I) if side.match else None
        for book in _books_rows(conn, account, start, end):
            if pattern is not None:
                text = f"{book['name'] or ''} {book['merchant_name'] or ''}"
                if book["category"] != cfg.whmcs.revenue_category or not pattern.search(text):
                    continue
            bank[(book["date"][:7], side.gateway)] += int(book["amount_cents"])
    for (month, gateway), cents in bank.items():
        entry = out.setdefault(
            (month, gateway),
            {"month": month, "gateway": gateway, "count": 0, "gross_cents": 0, "fees_cents": 0, "refunds_cents": 0, "net_cents": 0, "bank_cents": None, "bank_label": None},
        )
        entry["bank_cents"] = cents
    for entry in out.values():
        if entry["gateway"] in labels:
            entry["bank_label"] = labels[entry["gateway"]]
            if entry["bank_cents"] is None:
                entry["bank_cents"] = 0
        entry["difference_cents"] = None if entry["bank_cents"] is None else entry["bank_cents"] - entry["net_cents"]
    return sorted(out.values(), key=lambda item: (item["month"], item["gateway"]))


# --- customers (sign-in only on the web) ------------------------------------------------------------------


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def customer_search(conn, query: str, *, brand: str | None = None, limit: int = 50) -> list[dict]:
    """Find clients by name, email, company, domain, or client id across brands."""
    _require_ready(conn)
    brand = check_brand(brand)
    text = (query or "").strip()
    if len(text) < 2 and not text.isdigit():
        return []
    if len(text) > 100:
        raise HpgbooksError("search is too long")
    clause, params = _brand_sql(brand, "c.brand")
    like = _like(text.lower())
    id_value = int(text.lstrip("#")) if text.lstrip("#").isdigit() else -1
    rows = conn.execute(
        f"""
        SELECT c.brand, c.source_id, c.first_name, c.last_name, c.company, c.email, c.status, c.signup_date, c.country
        FROM whmcs_clients c
        WHERE (
            c.source_id = ?
            OR lower(COALESCE(c.first_name, '') || ' ' || COALESCE(c.last_name, '')) LIKE ? ESCAPE '\\'
            OR lower(COALESCE(c.email, '')) LIKE ? ESCAPE '\\'
            OR lower(COALESCE(c.company, '')) LIKE ? ESCAPE '\\'
            OR EXISTS (
                SELECT 1 FROM whmcs_services s
                WHERE s.brand = c.brand AND s.client_id = c.source_id AND lower(COALESCE(s.domain, '')) LIKE ? ESCAPE '\\'
            )
        ){clause}
        ORDER BY (c.source_id = ?) DESC, c.status = 'Active' DESC, c.brand, c.source_id
        LIMIT ?
        """,
        [id_value, like, like, like, like] + params + [id_value, max(1, min(limit, 200))],
    ).fetchall()
    if not rows:
        return []
    keys = [(row["brand"], int(row["source_id"])) for row in rows]
    paid = _client_totals(conn, keys)
    live = _client_services(conn, keys)
    out = []
    for row in rows:
        key = (row["brand"], int(row["source_id"]))
        out.append(
            {
                "brand": row["brand"],
                "client_id": int(row["source_id"]),
                "name": " ".join(part for part in (row["first_name"], row["last_name"]) if part) or "",
                "company": row["company"] or "",
                "email": row["email"] or "",
                "status": row["status"] or "",
                "signup_date": row["signup_date"],
                "country": row["country"] or "",
                "total_paid_cents": paid.get(key, (0, 0))[0],
                "refunds_cents": paid.get(key, (0, 0))[1],
                "live_services": live.get(key, (0, 0.0))[0],
                "mrr_cents": int(round(live.get(key, (0, 0.0))[1])),
            }
        )
    return out


def _client_totals(conn, keys: list[tuple[str, int]]) -> dict:
    out = {}
    for brand in {key[0] for key in keys}:
        ids = [key[1] for key in keys if key[0] == brand]
        marks = ", ".join("?" for _ in ids)
        for row in conn.execute(
            f"SELECT client_id, SUM(amount_in_cents) AS paid, SUM(amount_out_cents) AS refunded FROM whmcs_payments WHERE brand = ? AND client_id IN ({marks}) GROUP BY client_id",
            [brand] + ids,
        ):
            out[(brand, int(row["client_id"]))] = (int(row["paid"] or 0), int(row["refunded"] or 0))
    return out


def _client_services(conn, keys: list[tuple[str, int]]) -> dict:
    out: dict[tuple[str, int], tuple[int, float]] = {}
    for brand in {key[0] for key in keys}:
        ids = [key[1] for key in keys if key[0] == brand]
        marks = ", ".join("?" for _ in ids)
        for row in conn.execute(
            f"SELECT client_id, status, billing_cycle, amount_cents FROM whmcs_services WHERE brand = ? AND client_id IN ({marks})",
            [brand] + ids,
        ):
            key = (brand, int(row["client_id"]))
            count, value = out.get(key, (0, 0.0))
            if row["status"] in LIVE_STATUSES:
                count += 1
            if row["status"] == "Active":
                value += monthly_cents(int(row["amount_cents"]), row["billing_cycle"])
            out[key] = (count, value)
    return out


def customer_detail(conn, brand: str, client_id: int) -> dict | None:
    """One client: profile, services, payments, invoices, cancellations, credit, and totals."""
    _require_ready(conn)
    brand = check_brand(brand)
    if brand is None:
        raise HpgbooksError("brand is required")
    client = conn.execute("SELECT * FROM whmcs_clients WHERE brand = ? AND source_id = ?", (brand, client_id)).fetchone()
    if client is None:
        return None
    labels = plan_labels(conn, brand)
    groups = _groups(conn)
    services = []
    for row in conn.execute(
        """
        SELECT s.*, sv.name AS server_name FROM whmcs_services s
        LEFT JOIN whmcs_servers sv ON sv.brand = s.brand AND sv.source_id = s.server_id
        WHERE s.brand = ? AND s.client_id = ?
        ORDER BY s.status IN ('Active', 'Suspended') DESC, s.reg_date DESC
        """,
        (brand, client_id),
    ):
        plan = labels.get((row["kind"], int(row["source_id"])), "Unknown plan")
        if row["kind"] == "addon" and plan == "Addon: custom" and row["addon_name"]:
            plan = f"Addon: {row['addon_name']}"
        services.append(
            {
                "kind": row["kind"],
                "id": int(row["source_id"]),
                "plan": plan,
                "group": groups.get((brand, int(row["product_id"])), "") if row["kind"] == "hosting" and row["product_id"] is not None else "Addons",
                "domain": row["domain"] or "",
                "status": row["status"] or "",
                "billing_cycle": row["billing_cycle"] or "",
                "amount_cents": int(row["amount_cents"]),
                "mrr_cents": int(round(monthly_cents(int(row["amount_cents"]), row["billing_cycle"]))) if row["status"] == "Active" else 0,
                "reg_date": row["reg_date"],
                "next_due_date": row["next_due_date"],
                "termination_date": row["termination_date"],
                "payment_method": row["payment_method"] or "",
                "server": row["server_name"] or "",
            }
        )
    payments = [
        {
            "id": int(row["source_id"]),
            "date": row["ts"] or row["date"],
            "gateway": row["gateway"] or "",
            "amount_in_cents": int(row["amount_in_cents"]),
            "fees_cents": int(row["fees_cents"]),
            "amount_out_cents": int(row["amount_out_cents"]),
            "trans_id": row["trans_id"] or "",
            "invoice_id": row["invoice_id"] or None,
            "is_refund": bool(row["is_refund"]),
        }
        for row in conn.execute(
            "SELECT * FROM whmcs_payments WHERE brand = ? AND client_id = ? ORDER BY ts DESC, source_id DESC",
            (brand, client_id),
        )
    ]
    invoices = [
        {
            "id": int(row["source_id"]),
            "number": row["invoice_num"] or "",
            "date": row["date"],
            "due_date": row["due_date"],
            "date_paid": row["date_paid"],
            "status": row["status"] or "",
            "total_cents": int(row["total_cents"]),
            "credit_cents": int(row["credit_cents"]),
            "payment_method": row["payment_method"] or "",
        }
        for row in conn.execute(
            "SELECT * FROM whmcs_invoices WHERE brand = ? AND client_id = ? ORDER BY date DESC, source_id DESC",
            (brand, client_id),
        )
    ]
    service_ids = {item["id"]: item for item in services if item["kind"] == "hosting"}
    cancellations = []
    if service_ids:
        marks = ", ".join("?" for _ in service_ids)
        for row in conn.execute(
            f"SELECT * FROM whmcs_cancel_requests WHERE brand = ? AND service_id IN ({marks}) ORDER BY date DESC",
            [brand] + list(service_ids),
        ):
            service = service_ids.get(int(row["service_id"]))
            cancellations.append(
                {
                    "date": row["date"],
                    "service_id": int(row["service_id"]),
                    "plan": service["plan"] if service else "",
                    "domain": service["domain"] if service else "",
                    "type": row["type"] or "",
                    "reason": (row["reason"] or "").strip(),
                }
            )
    credits = [
        {"date": row["date"], "amount_cents": int(row["amount_cents"])}
        for row in conn.execute("SELECT date, amount_cents FROM whmcs_credit WHERE brand = ? AND client_id = ? ORDER BY date DESC, source_id DESC", (brand, client_id))
    ]
    paid_in = sum(item["amount_in_cents"] for item in payments)
    refunded = sum(item["amount_out_cents"] for item in payments)
    fees = sum(item["fees_cents"] for item in payments)
    unpaid = sum(item["total_cents"] for item in invoices if item["status"] == "Unpaid")
    dates = [item["date"] for item in payments if item["amount_in_cents"] > 0]
    return {
        "brand": brand,
        "client_id": client_id,
        "name": " ".join(part for part in (client["first_name"], client["last_name"]) if part),
        "first_name": client["first_name"] or "",
        "last_name": client["last_name"] or "",
        "company": client["company"] or "",
        "email": client["email"] or "",
        "status": client["status"] or "",
        "signup_date": client["signup_date"],
        "country": client["country"] or "",
        "state": client["state"] or "",
        "currency": client["currency"] or "",
        "default_gateway": client["default_gateway"] or "",
        "credit_balance_cents": int(client["credit_cents"]),
        "totals": {
            "paid_cents": paid_in,
            "refunds_cents": refunded,
            "fees_cents": fees,
            "net_cents": paid_in - refunded - fees,
            "unpaid_cents": unpaid,
            "mrr_cents": sum(item["mrr_cents"] for item in services),
            "live_services": sum(1 for item in services if item["status"] in LIVE_STATUSES),
            "first_payment": min(dates)[:10] if dates else None,
            "last_payment": max(dates)[:10] if dates else None,
            "payments": len(dates),
            "invoices": len(invoices),
        },
        "services": services,
        "payments": payments,
        "invoices": invoices,
        "cancellations": cancellations,
        "credits": credits,
    }


# --- tables for the CLI and CSV exports (no names or emails) ----------------------------------------------

MONEY, TEXT, NUMBER, PCT = "money", "text", "number", "pct"


def report_table(name: str, data: dict) -> tuple[list[tuple[str, str]], list[list]]:
    """(columns, rows) for one report. Columns are (header, kind)."""
    if name == "revenue":
        cols = [("Period", TEXT), ("Brand", TEXT), ("Payments", NUMBER), ("Gross in", MONEY), ("Fees", MONEY), ("Refunds", MONEY), ("Net", MONEY)]
        rows = []
        for period in data["periods"]:
            for brand in brand_names():
                figures = period["by_brand"].get(brand)
                if figures:
                    rows.append([period["period"], brand, figures["payments"], figures["gross_cents"], figures["fees_cents"], figures["refunds_cents"], figures["net_cents"]])
            rows.append([period["period"], "Total", period["payments"], period["gross_cents"], period["fees_cents"], period["refunds_cents"], period["net_cents"]])
        return cols, rows
    if name == "revenue-plans":
        cols = [("Brand", TEXT), ("Plan", TEXT), ("Group", TEXT), ("Payments", NUMBER), ("Gross in", MONEY), ("Fees", MONEY), ("Refunds", MONEY), ("Net", MONEY)]
        return cols, [[row["brand"], row["plan"], row["group"], row["payments"], row["gross_cents"], row["fees_cents"], row["refunds_cents"], row["net_cents"]] for row in data["plans"]]
    if name == "mrr":
        cols = [("Brand", TEXT), ("Plan", TEXT), ("Group", TEXT), ("Services", NUMBER), ("Customers", NUMBER), ("MRR", MONEY), ("ARR", MONEY), ("Share", PCT)]
        rows = [[row["brand"], row["plan"], row["group"], row["services"], row["customers"], row["mrr_cents"], row["arr_cents"], row["share_pct"]] for row in data["plans"]]
        rows += [[row["brand"], "All plans", "", row["services"], row["customers"], row["mrr_cents"], row["arr_cents"], row["share_pct"]] for row in data["brands"]]
        rows.append(["All brands", "All plans", "", data["services"], data["customers"], data["mrr_cents"], data["arr_cents"], 100.0 if data["mrr_cents"] else None])
        return cols, rows
    if name == "mrr-trend":
        cols = [("Month", TEXT), ("MRR (estimate)", MONEY), ("Customers", NUMBER), ("Services", NUMBER)] + [(brand, MONEY) for brand in brand_names()]
        return cols, [[row["month"], row["mrr_cents"], row["customers"], row["services"]] + [row["by_brand"].get(brand, 0) for brand in brand_names()] for row in data["trend"]]
    if name == "churn":
        cols = [
            ("Month", TEXT), ("Cancel requests", NUMBER), ("Immediate", NUMBER), ("End of period", NUMBER),
            ("Start customers", NUMBER), ("New customers", NUMBER), ("Churned customers", NUMBER), ("Logo churn", PCT),
            ("New services", NUMBER), ("Churned services", NUMBER), ("Net adds", NUMBER),
            ("Start MRR", MONEY), ("New MRR", MONEY), ("Churned MRR", MONEY), ("Revenue churn", PCT),
        ]
        return cols, [
            [
                row["month"], row["cancel_requests"], row["immediate"], row["end_of_period"],
                row["start_customers"], row["new_customers"], row["churned_customers"], row["logo_churn_pct"],
                row["new_services"], row["churned_services"], row["net_adds"],
                row["start_mrr_cents"], row["new_mrr_cents"], row["churned_mrr_cents"], row["revenue_churn_pct"],
            ]
            for row in data["months"]
        ]
    if name == "churn-plans":
        cols = [("Brand", TEXT), ("Plan", TEXT), ("Start services", NUMBER), ("New", NUMBER), ("Churned", NUMBER), ("Churn", PCT), ("Churned MRR", MONEY), ("Cancel requests", NUMBER)]
        return cols, [[row["brand"], row["plan"], row["start_services"], row["new_services"], row["churned_services"], row["service_churn_pct"], row["churned_mrr_cents"], row["cancel_requests"]] for row in data["plans"]]
    if name == "refunds":
        cols = [("Month", TEXT), ("Refunds", NUMBER), ("Refunded", MONEY), ("Gross in", MONEY), ("Refund rate", PCT)]
        return cols, [[row["month"], row["count"], row["refunds_cents"], row["gross_cents"], row["rate_pct"]] for row in data["months"]]
    if name == "refunds-largest":
        cols = [("Date", TEXT), ("Brand", TEXT), ("Client", NUMBER), ("Invoice", NUMBER), ("Gateway", TEXT), ("Plan", TEXT), ("Refund", MONEY)]
        return cols, [[row["date"], row["brand"], row["client_id"], row["invoice_id"], row["gateway"], row["plan"], row["refund_cents"]] for row in data["largest"]]
    if name == "dunning":
        cols = [("Brand", TEXT), ("Invoice", NUMBER), ("Client", NUMBER), ("Client status", TEXT), ("Due", TEXT), ("Days overdue", NUMBER), ("Aging", TEXT), ("Balance", MONEY), ("Method", TEXT), ("Last capture attempt", TEXT), ("Live services", NUMBER)]
        return cols, [
            [row["brand"], row["invoice_id"], row["client_id"], row["client_status"], row["due_date"], row["days_overdue"], row["bucket"], row["balance_cents"], row["payment_method"], (row["last_capture_attempt"] or "")[:10], row["live_services"]]
            for row in data["collections"]
        ]
    if name == "dunning-aging":
        cols = [("Aging", TEXT), ("Invoices", NUMBER), ("Balance", MONEY), ("Collectible", MONEY)]
        return cols, [[row["bucket"], row["count"], row["balance_cents"], row["collectible_cents"]] for row in data["aging"]]
    if name == "reconcile":
        cols = [("Month", TEXT), ("Matched", NUMBER), ("Matched WHMCS net", MONEY), ("Matched PayPal", MONEY), ("WHMCS only", NUMBER), ("WHMCS only net", MONEY), ("PayPal only", NUMBER), ("PayPal only amount", MONEY), ("WHMCS net", MONEY), ("PayPal", MONEY), ("Difference", MONEY)]
        return cols, [
            [row["month"], row["matched"], row["matched_whmcs_cents"], row["matched_books_cents"], row["whmcs_only"], row["whmcs_only_cents"], row["paypal_only"], row["paypal_only_cents"], row["whmcs_net_cents"], row["books_cents"], row["difference_cents"]]
            for row in data["months"]
        ]
    if name == "reconcile-rows":
        # Payer names from the ledger are left out of the export; the books id finds the row.
        cols = [("Status", TEXT), ("Kind", TEXT), ("Date", TEXT), ("Brand", TEXT), ("Client", NUMBER), ("Invoice", NUMBER), ("PayPal transaction", TEXT), ("WHMCS gross", MONEY), ("WHMCS fees", MONEY), ("WHMCS net", MONEY), ("Books date", TEXT), ("Books amount", MONEY), ("Books id", TEXT), ("Matched on", TEXT)]
        return cols, [
            [row["status"], row["kind"], row["date"], row["brand"] or "", row["client_id"], row["invoice_id"], row["trans_id"], row["whmcs_gross_cents"], row["whmcs_fees_cents"], row["whmcs_net_cents"], row["books_date"] or "", row["books_cents"], row["books_id"] or "", row["match"] or ""]
            for row in data["rows"]
        ]
    if name == "gateways":
        cols = [("Month", TEXT), ("Gateway", TEXT), ("Count", NUMBER), ("Gross in", MONEY), ("Fees", MONEY), ("Refunds", MONEY), ("Net", MONEY), ("Bank side", MONEY), ("Difference", MONEY), ("Bank source", TEXT)]
        return cols, [[row["month"], row["gateway"], row["count"], row["gross_cents"], row["fees_cents"], row["refunds_cents"], row["net_cents"], row["bank_cents"], row["difference_cents"], row["bank_label"] or "not matched"] for row in data["gateways"]]
    raise HpgbooksError(f"unknown WHMCS table {name}")


def _csv_text(value: str) -> str:
    if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def table_csv(name: str, data: dict) -> str:
    cols, rows = report_table(name, data)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([header for header, _kind in cols])
    for row in rows:
        out = []
        for (_header, kind), value in zip(cols, row):
            if value is None:
                out.append("")
            elif kind == MONEY:
                out.append(cents_to_dollars(int(value)))
            elif kind == PCT:
                out.append(f"{value:.2f}")
            elif kind == NUMBER:
                out.append(str(value))
            else:
                out.append(_csv_text(str(value)))
        writer.writerow(out)
    return buffer.getvalue()


def table_text(name: str, data: dict, limit: int | None = None) -> str:
    from hpgbooks.reports import render_table

    cols, rows = report_table(name, data)
    if limit is not None:
        rows = rows[:limit]
    body = []
    for row in rows:
        out = []
        for (_header, kind), value in zip(cols, row):
            if value is None:
                out.append("")
            elif kind == MONEY:
                out.append(format_money(int(value)))
            elif kind == PCT:
                out.append(f"{value:.1f}%")
            elif kind == NUMBER:
                out.append(f"{int(value):,}" if isinstance(value, int) else str(value))
            else:
                out.append(str(value))
        body.append(out)
    # render_table right-aligns every column from the first numeric one on.
    first_number = next((index for index, (_h, kind) in enumerate(cols) if kind != TEXT), len(cols))
    return render_table([header for header, _kind in cols], body, right_from=first_number)
