"""Server margins: what each server costs, which WHMCS services it carries,
the revenue those services bring in, and the margin.

Costs, the server-to-service mapping, and overhead lines are maintained data in
the margin_* tables. Revenue is never stored here. It is read from the synced
whmcs_services rows on every call and normalized to a month with the same
helper the MRR report uses, so a WHMCS sync updates the margins.

Server labels, mapping values (domains, service ids), and customer names can
identify customers. They stay in the encrypted database; the web API that
returns them requires sign-in, and the CLI identifies customers by brand and
client number only.
"""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from pathlib import Path

from hpgbooks import whmcs_reports as wr
from hpgbooks.db import HpgbooksError, audit, db_path, now_iso, to_cents
from hpgbooks.whmcs import brand_names, plan_labels, whmcs_ready

SERVER_KINDS = ("dedicated", "cloud_vm", "pool", "overhead")
SERVER_STATUSES = ("active", "retire_candidate", "retired")
RULE_TYPES = ("service_id", "domain", "whmcs_server_id", "product_group", "brand")
ALLOCATIONS = ("direct", "by_revenue")
OVERHEAD_KINDS = ("shared", "not_this_business")
# First matching rule type wins. Addons follow their parent unless a service_id rule names them.
RULE_ORDER = RULE_TYPES
MERGE_DEFAULT_KEY = "whatif_merge"
UNMAPPED = "unmapped"
MAX_MONTHLY_CENTS = 100_000_000  # $1,000,000 a month
TOP_UNMAPPED = 25

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
SERVICE_VALUE_RE = re.compile(r"^(addon:)?[1-9][0-9]{0,9}$")
DOLLAR_RE = re.compile(r"^\d{1,7}(\.\d{1,2})?$")


# --- validation -------------------------------------------------------------------------


def margins_ready(conn) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'margin_servers'").fetchone()
    return row is not None


def _require_tables(conn) -> None:
    if not margins_ready(conn):
        raise HpgbooksError("the margin tables do not exist yet; run any write command (for example: hpgbooks margins seed)")


def _text(value, name: str, *, max_len: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise HpgbooksError(f"{name} must be text")
    value = value.strip()
    if required and not value:
        raise HpgbooksError(f"{name} is required")
    if len(value) > max_len:
        raise HpgbooksError(f"{name} is longer than {max_len} characters")
    if any(ord(ch) < 32 for ch in value):
        raise HpgbooksError(f"{name} has control characters")
    return value


def _choice(value, name: str, options: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in options:
        raise HpgbooksError(f"{name} must be one of {', '.join(options)}")
    return value


def parse_cost(value, name: str = "monthly cost") -> int:
    """Dollars a month (string or number) to cents. No negatives, at most two decimals."""
    if isinstance(value, bool) or value is None:
        raise HpgbooksError(f"{name} is required")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        text = f"{value:.2f}"
    elif isinstance(value, str):
        text = value.strip().replace(",", "").replace("$", "")
    else:
        raise HpgbooksError(f"{name} must be a dollar amount")
    if not DOLLAR_RE.fullmatch(text):
        raise HpgbooksError(f"{name} must be a dollar amount of zero or more")
    cents = to_cents(text)
    if cents > MAX_MONTHLY_CENTS:
        raise HpgbooksError(f"{name} is out of range")
    return cents


def _brand_scope(value) -> str:
    if value in (None, "", "all"):
        return ""
    if not isinstance(value, str) or value not in brand_names():
        raise HpgbooksError(f"brand scope must be one of {', '.join(brand_names())}, or empty for every brand")
    return value


def normalize_rule(rule_type, rule_value, brand_scope) -> tuple[str, str, str]:
    """Validated (rule_type, rule_value, brand_scope). Values are stored in match form."""
    rule_type = _choice(rule_type, "rule type", RULE_TYPES)
    scope = _brand_scope(brand_scope)
    if isinstance(rule_value, int) and not isinstance(rule_value, bool):
        rule_value = str(rule_value)
    value = _text(rule_value, "rule value", max_len=200, required=True)
    if rule_type == "service_id":
        value = value.lower()
        if not SERVICE_VALUE_RE.fullmatch(value):
            raise HpgbooksError("a service_id rule is a WHMCS service number, or addon:<number> for an addon")
        if not scope:
            raise HpgbooksError("a service_id rule needs a brand scope; service numbers repeat across brands")
    elif rule_type == "whmcs_server_id":
        if not re.fullmatch(r"[1-9][0-9]{0,9}", value):
            raise HpgbooksError("a whmcs_server_id rule is a WHMCS server number")
        if not scope:
            raise HpgbooksError("a whmcs_server_id rule needs a brand scope; server numbers repeat across brands")
    elif rule_type == "domain":
        value = value.lower()
        if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,198}[a-z0-9])?", value):
            raise HpgbooksError("a domain rule is a host name such as example.com")
    elif rule_type == "brand":
        if value not in brand_names():
            raise HpgbooksError(f"a brand rule is one of {', '.join(brand_names())}")
    elif rule_type == "product_group":
        value = value.lower()
    return rule_type, value, scope


# --- reading the maintained data ------------------------------------------------------------


def _server_row(row) -> dict:
    return {
        "id": int(row["id"]),
        "slug": row["slug"],
        "label": row["label"],
        "vendor": row["vendor"],
        "kind": row["kind"],
        "status": row["status"],
        "location": row["location"],
        "notes": row["notes"],
    }


def load_servers(conn) -> list[dict]:
    servers = [_server_row(row) for row in conn.execute("SELECT * FROM margin_servers ORDER BY id")]
    by_id = {server["id"]: server for server in servers}
    for server in servers:
        server["components"] = []
        server["mappings"] = []
    for row in conn.execute("SELECT * FROM margin_server_costs ORDER BY server_id, id"):
        by_id[int(row["server_id"])]["components"].append(
            {"id": int(row["id"]), "component": row["component"], "monthly_cost_cents": int(row["monthly_cost_cents"]), "effective": row["effective"]}
        )
    for row in conn.execute("SELECT * FROM margin_mappings ORDER BY server_id, id"):
        by_id[int(row["server_id"])]["mappings"].append(
            {
                "id": int(row["id"]),
                "rule_type": row["rule_type"],
                "rule_value": row["rule_value"],
                "allocation": row["allocation"],
                "brand_scope": row["brand_scope"],
                "note": row["note"],
            }
        )
    for server in servers:
        server["cost_cents"] = 0 if server["status"] == "retired" else sum(item["monthly_cost_cents"] for item in server["components"])
    return servers


def load_overhead(conn) -> list[dict]:
    return [
        {
            "id": int(row["id"]),
            "label": row["label"],
            "vendor": row["vendor"],
            "monthly_cost_cents": int(row["monthly_cost_cents"]),
            "kind": row["kind"],
            "note": row["note"],
        }
        for row in conn.execute("SELECT * FROM margin_overhead ORDER BY id")
    ]


def _get_setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM margin_settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


# --- revenue from the WHMCS copy ---------------------------------------------------------------


def _client_names(conn) -> dict[tuple[str, int], str]:
    out = {}
    for row in conn.execute("SELECT brand, source_id, company, first_name, last_name FROM whmcs_clients"):
        company = html.unescape(row["company"] or "").strip()
        person = html.unescape(f"{row['first_name'] or ''} {row['last_name'] or ''}").strip()
        out[(row["brand"], int(row["source_id"]))] = company or person or f"client {row['source_id']}"
    return out


def load_active_services(conn) -> tuple[list[dict], dict[tuple[str, str, int], dict]]:
    """Active services (hosting and addons) with monthly revenue, and every service by key.

    Monthly revenue uses whmcs_reports.monthly_cents, the MRR report's cycle
    normalization: Annually/12, Quarterly/3, and so on; One Time and Free Account are 0.
    Every status is loaded so an addon can find a parent that is not Active.
    """
    groups = wr._groups(conn)
    labels = {brand: plan_labels(conn, brand) for brand in brand_names()}
    everything: dict[tuple[str, str, int], dict] = {}
    for row in conn.execute(
        """
        SELECT brand, kind, source_id, client_id, product_id, parent_id, domain, status,
               billing_cycle, amount_cents, server_id
        FROM whmcs_services
        """
    ):
        brand = row["brand"]
        kind = row["kind"]
        sid = int(row["source_id"])
        pid = row["product_id"]
        group = groups.get((brand, int(pid)), "") if kind == "hosting" and pid is not None else ""
        everything[(brand, kind, sid)] = {
            "brand": brand,
            "kind": kind,
            "id": sid,
            "client_id": int(row["client_id"]) if row["client_id"] is not None else None,
            "parent_id": int(row["parent_id"]) if row["parent_id"] else None,
            "domain": (row["domain"] or "").strip().lower(),
            "status": row["status"] or "",
            "cycle": row["billing_cycle"] or "",
            "amount_cents": int(row["amount_cents"] or 0),
            "server_id": int(row["server_id"]) if row["server_id"] else None,
            "group": group,
            "plan": labels.get(brand, {}).get((kind, sid), "Unknown plan"),
            "mrr": wr.monthly_cents(int(row["amount_cents"] or 0), row["billing_cycle"]),
        }
    active = [item for item in everything.values() if item["status"] == "Active"]
    for item in active:
        if item["kind"] == "addon":
            parent = everything.get((item["brand"], "hosting", item["parent_id"])) if item["parent_id"] else None
            item["group"] = f"Addons on {parent['group'] or 'ungrouped'}" if parent else "Addons"
    active.sort(key=lambda item: (item["brand"], item["kind"], item["id"]))
    return active, everything


# --- mapping ------------------------------------------------------------------------------------


class Mapper:
    """Resolve a service to a margin server with the stored rules.

    Rule order: service_id, domain, whmcs_server_id, product_group, brand. A brand-scoped
    rule beats an all-brand rule of the same type. Addons follow their parent service
    unless a service_id rule names the addon itself (addon:<id>).
    """

    def __init__(self, servers: list[dict], everything: dict):
        self.everything = everything
        self.index: dict[str, dict[tuple[str, str], tuple[dict, dict]]] = {kind: {} for kind in RULE_TYPES}
        for server in servers:
            for rule in server["mappings"]:
                self.index[rule["rule_type"]][(rule["brand_scope"], rule["rule_value"])] = (server, rule)
        self._cache: dict[tuple[str, str, int], tuple[dict | None, dict | None, str]] = {}

    def _lookup(self, rule_type: str, brand: str, value: str | None):
        if not value:
            return None
        table = self.index[rule_type]
        return table.get((brand, value)) or table.get(("", value))

    def resolve(self, service: dict, depth: int = 0) -> tuple[dict | None, dict | None, str]:
        """(server, rule, how). server is None when nothing matches."""
        key = (service["brand"], service["kind"], service["id"])
        if key in self._cache:
            return self._cache[key]
        result = self._resolve(service, depth)
        self._cache[key] = result
        return result

    def _resolve(self, service: dict, depth: int):
        brand = service["brand"]
        if service["kind"] == "addon":
            hit = self._lookup("service_id", brand, f"addon:{service['id']}")
            if hit:
                return hit[0], hit[1], "service_id"
            parent = self.everything.get((brand, "hosting", service["parent_id"])) if service["parent_id"] else None
            if parent is not None and depth < 3:
                server, rule, how = self.resolve(parent, depth + 1)
                return server, rule, f"parent:{how}" if server else "parent unmapped"
            hit = self._lookup("brand", brand, brand)
            if hit:
                return hit[0], hit[1], "brand"
            return None, None, "no parent"
        candidates = (
            ("service_id", str(service["id"])),
            ("domain", service["domain"]),
            ("whmcs_server_id", str(service["server_id"]) if service["server_id"] else None),
            ("product_group", service["group"].lower() if service["group"] else None),
            ("brand", brand),
        )
        for rule_type, value in candidates:
            hit = self._lookup(rule_type, brand, value)
            if hit:
                return hit[0], hit[1], rule_type
        return None, None, "no rule"


# --- the report -----------------------------------------------------------------------------------


def _pct(part: float, whole: float) -> float | None:
    if not whole:
        return None
    return round(part * 100.0 / whole, 1)


def _c(value: float) -> int:
    return int(round(value))


def build(conn, *, names: bool = True, today: str | None = None) -> dict:
    """The whole margins report. names=False leaves customer names and domains out (CLI)."""
    if not whmcs_ready(conn):
        return {"ready": False, "seeded": False}
    _require_tables(conn)
    servers = load_servers(conn)
    overhead = load_overhead(conn)
    active, everything = load_active_services(conn)
    client_names = _client_names(conn) if names else {}
    mapper = Mapper(servers, everything)

    by_server: dict[int, list[dict]] = defaultdict(list)
    unmapped: list[dict] = []
    for item in active:
        server, rule, how = mapper.resolve(item)
        if server is not None and server["status"] == "retired":
            item["unmapped_reason"] = "server retired"
            server = None
        item["server"] = server["id"] if server else None
        item["rule_id"] = rule["id"] if rule and server else None
        item["how"] = how
        if server is None:
            item.setdefault("unmapped_reason", how)
            unmapped.append(item)
        else:
            by_server[server["id"]].append(item)

    # Cost per service: the server's cost shared by revenue. Zero-revenue servers keep their cost.
    server_rev = {sid: sum(item["mrr"] for item in items) for sid, items in by_server.items()}
    cost_of = {server["id"]: server["cost_cents"] for server in servers}
    for item in active:
        sid = item["server"]
        rev = server_rev.get(sid, 0.0) if sid else 0.0
        item["alloc_cost"] = cost_of[sid] * item["mrr"] / rev if sid and rev > 0 else 0.0

    def client_name(brand: str, client_id: int | None) -> str | None:
        if not names or client_id is None:
            return None
        return client_names.get((brand, client_id), f"client {client_id}")

    server_rows = []
    for server in servers:
        items = by_server.get(server["id"], [])
        rev = server_rev.get(server["id"], 0.0)
        clients = {(item["brand"], item["client_id"]) for item in items if item["client_id"] is not None}
        paying_clients = defaultdict(float)
        for item in items:
            if item["client_id"] is not None:
                paying_clients[(item["brand"], item["client_id"])] += item["mrr"]
        flags = []
        if server["status"] == "retire_candidate":
            flags.append("retire_candidate")
        if server["status"] != "retired" and rev <= 0:
            flags.append("zero_revenue")
        if server["status"] != "retired" and rev - server["cost_cents"] < 0:
            flags.append("negative_margin")
        if server["status"] == "retired":
            flags.append("retired")
        single = None
        if len(clients) == 1:
            brand, cid = next(iter(clients))
            single = {"brand": brand, "client_id": cid, "name": client_name(brand, cid)}
        is_shared = server["kind"] == "pool" or any(rule["allocation"] == "by_revenue" for rule in server["mappings"]) or len(clients) > 1
        server_rows.append(
            {
                **server,
                "revenue_cents": _c(rev),
                "margin_cents": _c(rev) - server["cost_cents"],
                "margin_pct": _pct(_c(rev) - server["cost_cents"], _c(rev)),
                "services": len(items),
                "paying_services": sum(1 for item in items if item["mrr"] > 0),
                "customers": len(clients),
                "paying_customers": sum(1 for value in paying_clients.values() if value > 0),
                "brands": sorted({item["brand"] for item in items}),
                "flags": flags,
                "single_customer": single,
                "shared": is_shared,
            }
        )

    # Brand rollup: revenue of the brand's services and the server cost allocated to them.
    brand_rows = []
    for brand in brand_names():
        items = [item for item in active if item["brand"] == brand]
        if not items:
            continue
        rev = sum(item["mrr"] for item in items)
        cost = sum(item["alloc_cost"] for item in items)
        brand_rows.append(
            {
                "brand": brand,
                "services": len(items),
                "revenue_cents": _c(rev),
                "cost_cents": _c(cost),
                "margin_cents": _c(rev) - _c(cost),
                "margin_pct": _pct(_c(rev) - _c(cost), _c(rev)),
                "unmapped_cents": _c(sum(item["mrr"] for item in items if item["server"] is None)),
            }
        )

    # Customers on single-customer servers, one row per server, then the customer's
    # whole account (every active service, pool cost shared by revenue).
    server_by_id = {row["id"]: row for row in server_rows}
    single_rows = []
    single_clients: set[tuple[str, int]] = set()
    for row in server_rows:
        if row["single_customer"] and row["status"] != "retired":
            key = (row["single_customer"]["brand"], row["single_customer"]["client_id"])
            single_clients.add(key)
            single_rows.append(
                {
                    "server_id": row["id"],
                    "server": row["label"],
                    "brand": key[0],
                    "client_id": key[1],
                    "name": row["single_customer"]["name"],
                    "cost_cents": row["cost_cents"],
                    "revenue_cents": row["revenue_cents"],
                    "margin_cents": row["margin_cents"],
                    "margin_pct": row["margin_pct"],
                }
            )
    single_rows.sort(key=lambda r: (-r["revenue_cents"], r["server"]))

    by_client: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for item in active:
        if item["client_id"] is not None:
            by_client[(item["brand"], item["client_id"])].append(item)
    rollups = []
    for key in sorted(single_clients):
        items = by_client.get(key, [])
        hosts = {item["server"] for item in items}
        if len(hosts - {None}) < 2:
            continue
        rev = sum(item["mrr"] for item in items)
        cost = sum(item["alloc_cost"] for item in items)
        rollups.append(
            {
                "brand": key[0],
                "client_id": key[1],
                "name": client_name(*key),
                "services": len(items),
                "revenue_cents": _c(rev),
                "cost_cents": _c(cost),
                "margin_cents": _c(rev) - _c(cost),
                "margin_pct": _pct(_c(rev) - _c(cost), _c(rev)),
                "servers": sorted(server_by_id[h]["label"] if h else "no server" for h in hosts),
            }
        )
    rollups.sort(key=lambda r: -r["revenue_cents"])

    # Plan groups on shared servers: revenue-shared cost, and an even per-service split for contrast.
    plan_groups = []
    for row in server_rows:
        if not row["shared"] or row["status"] == "retired":
            continue
        items = by_server.get(row["id"], [])
        if not items:
            continue
        groups: dict[str, list[dict]] = defaultdict(list)
        for item in items:
            groups[item["group"] or "Ungrouped"].append(item)
        for group, members in groups.items():
            rev = sum(item["mrr"] for item in members)
            cost = sum(item["alloc_cost"] for item in members)
            even = row["cost_cents"] * len(members) / len(items)
            plan_groups.append(
                {
                    "server_id": row["id"],
                    "server": row["label"],
                    "group": group,
                    "services": len(members),
                    "paying_services": sum(1 for item in members if item["mrr"] > 0),
                    "revenue_cents": _c(rev),
                    "cost_cents": _c(cost),
                    "margin_cents": _c(rev) - _c(cost),
                    "margin_pct": _pct(_c(rev) - _c(cost), _c(rev)),
                    "even_cost_cents": _c(even),
                    "even_margin_cents": _c(rev) - _c(even),
                }
            )
    plan_groups.sort(key=lambda r: (r["server_id"], -r["revenue_cents"]))

    # Unmapped active revenue, grouped and the largest items.
    um_groups: dict[tuple[str, str, str], dict] = {}
    for item in unmapped:
        recurring = "One Time" if item["cycle"] == "One Time" else "recurring"
        key = (item["brand"], item["group"] or ("addon" if item["kind"] == "addon" else "Ungrouped"), recurring)
        entry = um_groups.setdefault(key, {"brand": key[0], "group": key[1], "cycle": key[2], "services": 0, "paying_services": 0, "revenue": 0.0})
        entry["services"] += 1
        entry["paying_services"] += 1 if item["mrr"] > 0 else 0
        entry["revenue"] += item["mrr"]
    um_group_rows = sorted(
        ({**{k: v for k, v in entry.items() if k != "revenue"}, "revenue_cents": _c(entry["revenue"])} for entry in um_groups.values()),
        key=lambda r: (-r["revenue_cents"], -r["services"]),
    )
    top = sorted((item for item in unmapped if item["mrr"] > 0), key=lambda item: (-item["mrr"], item["brand"], item["id"]))[:TOP_UNMAPPED]
    um_top = [
        {
            "brand": item["brand"],
            "kind": item["kind"],
            "service_id": item["id"],
            "client_id": item["client_id"],
            "name": client_name(item["brand"], item["client_id"]),
            "domain": item["domain"] if names else None,
            "plan": item["plan"],
            "group": item["group"],
            "cycle": item["cycle"],
            "whmcs_server_id": item["server_id"],
            "reason": item["unmapped_reason"],
            "revenue_cents": _c(item["mrr"]),
        }
        for item in top
    ]

    # Inputs for the client-side what-if: revenue per customer and per plan, split by server.
    def split(items: list[dict]) -> dict[str, int]:
        out: dict[str, float] = defaultdict(float)
        for item in items:
            out[str(item["server"]) if item["server"] else UNMAPPED] += item["mrr"]
        return {key: _c(value) for key, value in out.items() if _c(value)}

    customer_options = []
    for key, items in by_client.items():
        rev = sum(item["mrr"] for item in items)
        if rev <= 0:
            continue
        customer_options.append(
            {
                "key": f"{key[0]}:{key[1]}",
                "brand": key[0],
                "client_id": key[1],
                "name": client_name(*key),
                "revenue_cents": _c(rev),
                "paying_services": sum(1 for item in items if item["mrr"] > 0),
                "by_server": split(items),
            }
        )
    customer_options.sort(key=lambda r: -r["revenue_cents"])
    by_plan: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in active:
        by_plan[(item["brand"], item["plan"])].append(item)
    plan_options = []
    for (brand, plan), items in by_plan.items():
        rev = sum(item["mrr"] for item in items)
        if rev <= 0:
            continue
        plan_options.append(
            {
                "key": f"{brand}:{plan}",
                "brand": brand,
                "plan": plan,
                "group": items[0]["group"],
                "revenue_cents": _c(rev),
                "paying_services": sum(1 for item in items if item["mrr"] > 0),
                "by_server": split(items),
            }
        )
    plan_options.sort(key=lambda r: -r["revenue_cents"])

    total_mrr = sum(item["mrr"] for item in active)
    hosting_mrr = sum(item["mrr"] for item in active if item["kind"] == "hosting")
    server_cost = sum(row["cost_cents"] for row in server_rows)
    overhead_shared = sum(line["monthly_cost_cents"] for line in overhead if line["kind"] == "shared")
    overhead_excluded = sum(line["monthly_cost_cents"] for line in overhead if line["kind"] == "not_this_business")
    mrr_cents = _c(total_mrr)
    report_mrr = wr.mrr(conn, months=1, today=today)["mrr_cents"]
    blended = mrr_cents - server_cost - overhead_shared
    merge_default = None
    raw = _get_setting(conn, MERGE_DEFAULT_KEY)
    if raw:
        slugs = raw.split(",")
        ids = {row["slug"]: row["id"] for row in server_rows if row["status"] != "retired"}
        if len(slugs) == 2 and all(slug in ids for slug in slugs):
            merge_default = [ids[slugs[0]], ids[slugs[1]]]
    return {
        "ready": True,
        "seeded": bool(servers),
        "totals": {
            "mrr_cents": mrr_cents,
            "hosting_mrr_cents": _c(hosting_mrr),
            "addon_mrr_cents": mrr_cents - _c(hosting_mrr),
            "mrr_report_cents": report_mrr,
            "server_cost_cents": server_cost,
            "contribution_cents": mrr_cents - server_cost,
            "contribution_pct": _pct(mrr_cents - server_cost, mrr_cents),
            "overhead_cents": overhead_shared,
            "overhead_excluded_cents": overhead_excluded,
            "infrastructure_cents": server_cost + overhead_shared,
            "blended_margin_cents": blended,
            "blended_margin_pct": _pct(blended, mrr_cents),
            "active_services": len(active),
            "mapped_services": len(active) - len(unmapped),
            "unmapped_services": len(unmapped),
            "unmapped_cents": _c(sum(item["mrr"] for item in unmapped)),
            "retire_candidate_cents": sum(row["cost_cents"] for row in server_rows if row["status"] == "retire_candidate"),
        },
        "servers": server_rows,
        "brands": brand_rows,
        "single_customers": single_rows,
        "customer_rollups": rollups,
        "plan_groups": plan_groups,
        "unmapped": {"groups": um_group_rows, "top": um_top},
        "overhead": overhead,
        "whatif": {"merge_default": merge_default, "customers": customer_options, "plans": plan_options},
        "notes": [
            "Revenue is Active WHMCS services normalized to a month (One Time and Free Account count as $0); addons follow their parent service.",
            "Server cost is shared across a server's services by revenue. Overhead is spread across all revenue and shown separately.",
            "Payment-processing fees and payroll are not deducted.",
        ],
    }


# --- what-if (the same arithmetic the web page runs in the browser) ------------------------------


def whatif(report: dict, *, merge: tuple[int, int] | None = None, retire: list[int] | None = None,
           increases: list[dict] | None = None, exclude_overhead: list[int] | None = None) -> dict:
    """Apply scenarios to a built report. Nothing is written.

    merge (A, B): A's cost goes away and its revenue moves to B.
    retire X: X's cost and its revenue go away (use merge to keep the revenue).
    increases: {"target": customer or plan option, "pct": n} or {"target": ..., "flat_cents": n}
      (flat is per paying service a month).
    exclude_overhead: overhead line ids treated as not this business.
    """
    servers = {row["id"]: {"id": row["id"], "label": row["label"], "cost": row["cost_cents"], "revenue": row["revenue_cents"]} for row in report["servers"] if row["status"] != "retired"}
    unmapped = report["totals"]["unmapped_cents"]
    retire = list(retire or [])
    if merge:
        a, b = merge
        if a == b or a not in servers or b not in servers:
            raise HpgbooksError("merge needs two different active servers")
        if a in retire or b in retire:
            raise HpgbooksError("a merged server cannot also be retired")
    for x in retire:
        if x not in servers:
            raise HpgbooksError("retire needs an active server")
    before = {sid: dict(row) for sid, row in servers.items()}
    gone: set[int] = set()
    if merge:
        a, b = merge
        servers[b]["revenue"] += servers[a]["revenue"]
        servers[a]["revenue"] = 0
        servers[a]["cost"] = 0
        gone.add(a)
    for x in retire:
        servers[x]["revenue"] = 0
        servers[x]["cost"] = 0
        gone.add(x)
    added = 0
    for inc in increases or []:
        target = inc["target"]
        total = target["revenue_cents"]
        if "pct" in inc:
            extra = total * float(inc["pct"]) / 100.0
        else:
            extra = int(inc["flat_cents"]) * target["paying_services"]
        for key, part in target["by_server"].items():
            share = extra * part / total if total else 0
            if key == UNMAPPED:
                unmapped += share
                added += share
                continue
            sid = int(key)
            if merge and sid == merge[0]:
                sid = merge[1]
            if sid in gone or sid not in servers:
                continue
            servers[sid]["revenue"] += share
            added += share
    excluded = set(exclude_overhead or [])
    overhead = sum(line["monthly_cost_cents"] for line in report["overhead"] if line["kind"] == "shared" and line["id"] not in excluded)
    mrr = sum(row["revenue"] for row in servers.values()) + unmapped
    cost = sum(row["cost"] for row in servers.values())
    blended = mrr - cost - overhead
    base = report["totals"]
    changed = []
    for sid, row in servers.items():
        old = before[sid]
        if row != old:
            changed.append(
                {
                    "id": sid,
                    "label": row["label"],
                    "removed": sid in gone,
                    "cost_before_cents": old["cost"],
                    "cost_after_cents": row["cost"],
                    "revenue_before_cents": old["revenue"],
                    "revenue_after_cents": _c(row["revenue"]),
                    "margin_before_cents": old["revenue"] - old["cost"],
                    "margin_after_cents": _c(row["revenue"] - row["cost"]),
                    "margin_after_pct": _pct(row["revenue"] - row["cost"], row["revenue"]),
                }
            )
    return {
        "mrr_cents": _c(mrr),
        "server_cost_cents": cost,
        "overhead_cents": overhead,
        "cost_saved_cents": base["server_cost_cents"] + base["overhead_cents"] - cost - overhead,
        "revenue_added_cents": _c(added),
        "blended_margin_cents": _c(blended),
        "blended_margin_pct": _pct(blended, mrr),
        "blended_change_cents": _c(blended) - base["blended_margin_cents"],
        "servers": changed,
    }


# --- seed ---------------------------------------------------------------------------------------------


def default_seed_path() -> Path:
    return Path(db_path()).parent / "margins" / "seed.json"


def seed(conn, path: str | Path | None = None, *, actor: str = "cli") -> dict:
    """Load servers, cost components, mapping rules, and overhead from a JSON file.

    Idempotent: servers match on slug, components on (server, component), rules on
    (type, value, brand scope), overhead on label. Values from the file replace what
    is stored for those keys; rows the file does not mention are left alone.
    """
    _require_tables(conn)
    target = Path(path) if path else default_seed_path()
    if not target.is_file():
        raise HpgbooksError(f"seed file not found: {target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HpgbooksError(f"seed file is not valid JSON: {target}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("servers", []), list) or not isinstance(data.get("overhead", []), list):
        raise HpgbooksError("seed file needs a servers list and an overhead list")
    counts = defaultdict(int)
    stamp = now_iso()
    for entry in data.get("servers", []):
        if not isinstance(entry, dict):
            raise HpgbooksError("each server must be an object")
        slug = _text(entry.get("slug"), "slug", max_len=40, required=True)
        if not SLUG_RE.fullmatch(slug):
            raise HpgbooksError(f"bad server slug {slug!r}")
        fields = (
            _text(entry.get("label"), "label", max_len=200, required=True),
            _text(entry.get("vendor"), "vendor", max_len=80),
            _choice(entry.get("kind", "dedicated"), "kind", SERVER_KINDS),
            _choice(entry.get("status", "active"), "status", SERVER_STATUSES),
            _text(entry.get("location"), "location", max_len=80),
            _text(entry.get("notes"), "notes", max_len=1000),
        )
        row = conn.execute("SELECT id, label, vendor, kind, status, location, notes FROM margin_servers WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO margin_servers (slug, label, vendor, kind, status, location, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (slug, *fields, stamp, stamp),
            )
            server_id = cur.lastrowid
            counts["servers_added"] += 1
        else:
            server_id = int(row["id"])
            if tuple(row)[1:] != fields:
                conn.execute(
                    "UPDATE margin_servers SET label = ?, vendor = ?, kind = ?, status = ?, location = ?, notes = ?, updated_at = ? WHERE id = ?",
                    (*fields, stamp, server_id),
                )
                counts["servers_updated"] += 1
        for cost in entry.get("costs", []):
            component = _text(cost.get("component"), "component", max_len=80, required=True)
            cents = parse_cost(cost.get("monthly_cost"))
            effective = _text(cost.get("effective"), "effective", max_len=200)
            have = conn.execute("SELECT monthly_cost_cents, effective FROM margin_server_costs WHERE server_id = ? AND component = ?", (server_id, component)).fetchone()
            if have is None:
                conn.execute(
                    "INSERT INTO margin_server_costs (server_id, component, monthly_cost_cents, effective, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (server_id, component, cents, effective, stamp),
                )
                counts["costs_added"] += 1
            elif (int(have["monthly_cost_cents"]), have["effective"]) != (cents, effective):
                conn.execute(
                    "UPDATE margin_server_costs SET monthly_cost_cents = ?, effective = ?, updated_at = ? WHERE server_id = ? AND component = ?",
                    (cents, effective, stamp, server_id, component),
                )
                counts["costs_updated"] += 1
        for rule in entry.get("mappings", []):
            rule_type, value, scope = normalize_rule(rule.get("rule_type"), rule.get("rule_value"), rule.get("brand_scope"))
            allocation = _choice(rule.get("allocation", "direct"), "allocation", ALLOCATIONS)
            note = _text(rule.get("note"), "note", max_len=500)
            have = conn.execute(
                "SELECT server_id, allocation, note FROM margin_mappings WHERE rule_type = ? AND rule_value = ? AND brand_scope = ?",
                (rule_type, value, scope),
            ).fetchone()
            if have is None:
                conn.execute(
                    "INSERT INTO margin_mappings (server_id, rule_type, rule_value, allocation, brand_scope, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (server_id, rule_type, value, allocation, scope, note, stamp),
                )
                counts["mappings_added"] += 1
            elif (int(have["server_id"]), have["allocation"], have["note"]) != (server_id, allocation, note):
                conn.execute(
                    "UPDATE margin_mappings SET server_id = ?, allocation = ?, note = ? WHERE rule_type = ? AND rule_value = ? AND brand_scope = ?",
                    (server_id, allocation, note, rule_type, value, scope),
                )
                counts["mappings_updated"] += 1
    for line in data.get("overhead", []):
        label = _text(line.get("label"), "label", max_len=200, required=True)
        fields = (
            _text(line.get("vendor"), "vendor", max_len=80),
            parse_cost(line.get("monthly_cost")),
            _choice(line.get("kind", "shared"), "kind", OVERHEAD_KINDS),
            _text(line.get("note"), "note", max_len=500),
        )
        have = conn.execute("SELECT vendor, monthly_cost_cents, kind, note FROM margin_overhead WHERE label = ?", (label,)).fetchone()
        if have is None:
            conn.execute(
                "INSERT INTO margin_overhead (label, vendor, monthly_cost_cents, kind, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (label, *fields, stamp, stamp),
            )
            counts["overhead_added"] += 1
        elif (have["vendor"], int(have["monthly_cost_cents"]), have["kind"], have["note"]) != fields:
            conn.execute(
                "UPDATE margin_overhead SET vendor = ?, monthly_cost_cents = ?, kind = ?, note = ?, updated_at = ? WHERE label = ?",
                (*fields, stamp, label),
            )
            counts["overhead_updated"] += 1
    merge = (data.get("whatif") or {}).get("merge") if isinstance(data.get("whatif"), dict) else None
    if merge is not None:
        if not (isinstance(merge, list) and len(merge) == 2 and all(isinstance(s, str) and SLUG_RE.fullmatch(s) for s in merge)):
            raise HpgbooksError("whatif.merge must list two server slugs")
        value = ",".join(merge)
        if _get_setting(conn, MERGE_DEFAULT_KEY) != value:
            conn.execute(
                "INSERT INTO margin_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (MERGE_DEFAULT_KEY, value, stamp),
            )
            counts["settings_updated"] += 1
    result = dict(counts)
    if result:
        audit(conn, "margins_seed", new_value=json.dumps(result, sort_keys=True), actor=actor)
    return result


# --- edits (each one audited) -----------------------------------------------------------------------


def _audit(conn, action: str, old, new, actor: str, note: str | None = None) -> None:
    audit(
        conn,
        action,
        old_value=None if old is None else json.dumps(old, sort_keys=True),
        new_value=None if new is None else json.dumps(new, sort_keys=True),
        actor=actor,
        note=note,
    )


def _server(conn, server_id: int):
    row = conn.execute("SELECT * FROM margin_servers WHERE id = ?", (server_id,)).fetchone()
    if row is None:
        raise HpgbooksError("no such server")
    return row


def update_server(conn, server_id: int, *, status=None, notes=None, actor: str = "web") -> dict:
    _require_tables(conn)
    row = _server(conn, server_id)
    old = {"status": row["status"], "notes": row["notes"]}
    new = dict(old)
    if status is not None:
        new["status"] = _choice(status, "status", SERVER_STATUSES)
    if notes is not None:
        new["notes"] = _text(notes, "notes", max_len=1000)
    if new != old:
        conn.execute("UPDATE margin_servers SET status = ?, notes = ?, updated_at = ? WHERE id = ?", (new["status"], new["notes"], now_iso(), server_id))
        _audit(conn, "margin_server_update", {"server": row["slug"], **old}, {"server": row["slug"], **new}, actor)
    return _server_row(_server(conn, server_id))


def add_cost(conn, server_id: int, component, monthly_cost, effective=None, *, actor: str = "web") -> dict:
    _require_tables(conn)
    row = _server(conn, server_id)
    component = _text(component, "component", max_len=80, required=True)
    cents = parse_cost(monthly_cost)
    effective = _text(effective, "effective", max_len=200)
    if conn.execute("SELECT 1 FROM margin_server_costs WHERE server_id = ? AND component = ?", (server_id, component)).fetchone():
        raise HpgbooksError("that server already has a component with this name")
    cur = conn.execute(
        "INSERT INTO margin_server_costs (server_id, component, monthly_cost_cents, effective, updated_at) VALUES (?, ?, ?, ?, ?)",
        (server_id, component, cents, effective, now_iso()),
    )
    new = {"server": row["slug"], "component": component, "monthly_cost_cents": cents, "effective": effective}
    _audit(conn, "margin_cost_add", None, new, actor)
    return {"id": cur.lastrowid, **new}


def _cost(conn, cost_id: int):
    row = conn.execute(
        "SELECT c.*, s.slug FROM margin_server_costs c JOIN margin_servers s ON s.id = c.server_id WHERE c.id = ?",
        (cost_id,),
    ).fetchone()
    if row is None:
        raise HpgbooksError("no such cost component")
    return row


def update_cost(conn, cost_id: int, *, component=None, monthly_cost=None, effective=None, actor: str = "web") -> dict:
    _require_tables(conn)
    row = _cost(conn, cost_id)
    old = {"component": row["component"], "monthly_cost_cents": int(row["monthly_cost_cents"]), "effective": row["effective"]}
    new = dict(old)
    if component is not None:
        new["component"] = _text(component, "component", max_len=80, required=True)
    if monthly_cost is not None:
        new["monthly_cost_cents"] = parse_cost(monthly_cost)
    if effective is not None:
        new["effective"] = _text(effective, "effective", max_len=200)
    if new["component"] != old["component"] and conn.execute(
        "SELECT 1 FROM margin_server_costs WHERE server_id = ? AND component = ? AND id != ?", (row["server_id"], new["component"], cost_id)
    ).fetchone():
        raise HpgbooksError("that server already has a component with this name")
    if new != old:
        conn.execute(
            "UPDATE margin_server_costs SET component = ?, monthly_cost_cents = ?, effective = ?, updated_at = ? WHERE id = ?",
            (new["component"], new["monthly_cost_cents"], new["effective"], now_iso(), cost_id),
        )
        _audit(conn, "margin_cost_update", {"server": row["slug"], **old}, {"server": row["slug"], **new}, actor)
    return {"id": cost_id, "server": row["slug"], **new}


def delete_cost(conn, cost_id: int, *, actor: str = "web") -> None:
    _require_tables(conn)
    row = _cost(conn, cost_id)
    conn.execute("DELETE FROM margin_server_costs WHERE id = ?", (cost_id,))
    _audit(conn, "margin_cost_delete", {"server": row["slug"], "component": row["component"], "monthly_cost_cents": int(row["monthly_cost_cents"])}, None, actor)


def add_mapping(conn, server_id: int, rule_type, rule_value, *, allocation="direct", brand_scope=None, note=None, actor: str = "web") -> dict:
    _require_tables(conn)
    row = _server(conn, server_id)
    rule_type, value, scope = normalize_rule(rule_type, rule_value, brand_scope)
    allocation = _choice(allocation, "allocation", ALLOCATIONS)
    note = _text(note, "note", max_len=500)
    clash = conn.execute(
        "SELECT s.label FROM margin_mappings m JOIN margin_servers s ON s.id = m.server_id WHERE m.rule_type = ? AND m.rule_value = ? AND m.brand_scope = ?",
        (rule_type, value, scope),
    ).fetchone()
    if clash is not None:
        raise HpgbooksError(f"that rule already maps to {clash['label']}; remove it there first")
    cur = conn.execute(
        "INSERT INTO margin_mappings (server_id, rule_type, rule_value, allocation, brand_scope, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (server_id, rule_type, value, allocation, scope, note, now_iso()),
    )
    new = {"server": row["slug"], "rule_type": rule_type, "rule_value": value, "allocation": allocation, "brand_scope": scope, "note": note}
    _audit(conn, "margin_mapping_add", None, new, actor)
    return {"id": cur.lastrowid, **new}


def delete_mapping(conn, mapping_id: int, *, actor: str = "web") -> None:
    _require_tables(conn)
    row = conn.execute(
        "SELECT m.*, s.slug FROM margin_mappings m JOIN margin_servers s ON s.id = m.server_id WHERE m.id = ?",
        (mapping_id,),
    ).fetchone()
    if row is None:
        raise HpgbooksError("no such mapping")
    conn.execute("DELETE FROM margin_mappings WHERE id = ?", (mapping_id,))
    _audit(
        conn,
        "margin_mapping_delete",
        {"server": row["slug"], "rule_type": row["rule_type"], "rule_value": row["rule_value"], "brand_scope": row["brand_scope"], "allocation": row["allocation"]},
        None,
        actor,
    )


def add_overhead(conn, label, monthly_cost, *, vendor=None, kind="shared", note=None, actor: str = "web") -> dict:
    _require_tables(conn)
    label = _text(label, "label", max_len=200, required=True)
    new = {
        "label": label,
        "vendor": _text(vendor, "vendor", max_len=80),
        "monthly_cost_cents": parse_cost(monthly_cost),
        "kind": _choice(kind, "kind", OVERHEAD_KINDS),
        "note": _text(note, "note", max_len=500),
    }
    if conn.execute("SELECT 1 FROM margin_overhead WHERE label = ?", (label,)).fetchone():
        raise HpgbooksError("an overhead line with this label already exists")
    stamp = now_iso()
    cur = conn.execute(
        "INSERT INTO margin_overhead (label, vendor, monthly_cost_cents, kind, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (new["label"], new["vendor"], new["monthly_cost_cents"], new["kind"], new["note"], stamp, stamp),
    )
    _audit(conn, "margin_overhead_add", None, new, actor)
    return {"id": cur.lastrowid, **new}


def update_overhead(conn, line_id: int, *, monthly_cost=None, kind=None, note=None, actor: str = "web") -> dict:
    _require_tables(conn)
    row = conn.execute("SELECT * FROM margin_overhead WHERE id = ?", (line_id,)).fetchone()
    if row is None:
        raise HpgbooksError("no such overhead line")
    old = {"label": row["label"], "monthly_cost_cents": int(row["monthly_cost_cents"]), "kind": row["kind"], "note": row["note"]}
    new = dict(old)
    if monthly_cost is not None:
        new["monthly_cost_cents"] = parse_cost(monthly_cost)
    if kind is not None:
        new["kind"] = _choice(kind, "kind", OVERHEAD_KINDS)
    if note is not None:
        new["note"] = _text(note, "note", max_len=500)
    if new != old:
        conn.execute(
            "UPDATE margin_overhead SET monthly_cost_cents = ?, kind = ?, note = ?, updated_at = ? WHERE id = ?",
            (new["monthly_cost_cents"], new["kind"], new["note"], now_iso(), line_id),
        )
        _audit(conn, "margin_overhead_update", old, new, actor)
    return {"id": line_id, "vendor": row["vendor"], **new}


def find_server(report: dict, ref: str) -> dict:
    """A server by slug or numeric id (CLI)."""
    for row in report["servers"]:
        if row["slug"] == ref or str(row["id"]) == ref:
            return row
    raise HpgbooksError(f"no server {ref!r}; see: hpgbooks margins servers")
