"""Server margins on fake data. Temporary database and key only; every server,
customer, and domain here is made up."""

from __future__ import annotations

import json

import pytest

from hpgbooks import margins as mg
from hpgbooks.access import clear_passphrase_cache, reset_lockout
from hpgbooks.cli import main
from hpgbooks.db import MIGRATIONS, HpgbooksError, _apply_migrations, connect, init_db, now_iso

KEY = "c3" * 32
SECRET = "correct-horse-battery"
HOST = "books.example:8765"
NAMES = ("Fake Corp A", "Pat Example", "Third Co", "Repo User")

# (brand, kind, id, client, product, parent, domain, status, cycle, amount_cents, whmcs server)
SERVICES = [
    ("BrandA", "hosting", 10, 1, 1, None, "a.fake-a.test", "Active", "Monthly", 1000, 7),
    ("BrandA", "hosting", 11, 2, 1, None, "b.fake-b.test", "Active", "Annually", 12000, 7),
    ("BrandA", "hosting", 12, 2, 2, None, "vps.fake-b.test", "Active", "Quarterly", 9000, 7),
    ("BrandA", "hosting", 13, 3, 3, None, "box.fake-c.test", "Active", "Monthly", 20000, 0),
    ("BrandA", "hosting", 14, 3, 1, None, "free.fake-c.test", "Active", "Free Account", 5000, 7),
    ("BrandA", "hosting", 15, 1, 1, None, "old.fake-a.test", "Cancelled", "Monthly", 4000, 7),
    ("BrandA", "hosting", 16, 1, 3, None, "lost.fake-a.test", "Active", "Monthly", 7500, 0),
    ("BrandA", "hosting", 17, 2, 1, None, "", "Active", "One Time", 2500, 9),
    ("BrandA", "hosting", 18, 3, 1, None, "special.fake-c.test", "Active", "Semi-Annually", 6000, 7),
    ("BrandA", "addon", 201, 1, 1, 10, None, "Active", "Monthly", 500, 0),
    ("BrandA", "addon", 202, 3, 1, 13, None, "Active", "Monthly", 2000, 0),
    ("BrandA", "addon", 203, 1, 1, 15, None, "Active", "Monthly", 300, 0),
    ("BrandA", "addon", 204, 1, 1, 16, None, "Active", "Monthly", 200, 0),
    ("BrandB", "hosting", 30, 1, 1, None, "r.fake-r.test", "Active", "Monthly", 800, 3),
    ("BrandB", "hosting", 31, 2, 1, None, "s.fake-r.test", "Active", "Biennially", 4800, 3),
]

SEED = {
    "servers": [
        {
            "slug": "POOL", "label": "Fake pool box", "vendor": "Vendor One", "kind": "pool", "location": "Nowhere",
            "costs": [{"component": "server", "monthly_cost": "30.00"}, {"component": "license", "monthly_cost": "10.00"}],
            "mappings": [{"rule_type": "whmcs_server_id", "rule_value": "7", "brand_scope": "BrandA", "allocation": "by_revenue"}],
        },
        {
            "slug": "DED", "label": "Fake dedicated", "vendor": "Vendor One", "kind": "dedicated",
            "costs": [{"component": "server", "monthly_cost": "150"}],
            "mappings": [{"rule_type": "service_id", "rule_value": "13", "brand_scope": "BrandA"}],
        },
        {
            "slug": "VM", "label": "Fake cloud vm", "vendor": "Vendor Two", "kind": "cloud_vm",
            "costs": [{"component": "server", "monthly_cost": "5.00"}],
            "mappings": [{"rule_type": "domain", "rule_value": "Special.Fake-C.test"}],
        },
        {
            "slug": "REPO", "label": "Fake repo box", "vendor": "Vendor Two", "kind": "dedicated",
            "costs": [{"component": "server", "monthly_cost": "4.00"}],
            "mappings": [{"rule_type": "brand", "rule_value": "BrandB", "brand_scope": "BrandB", "allocation": "by_revenue"}],
        },
        {"slug": "OLD", "label": "Fake idle box", "vendor": "Vendor One", "kind": "dedicated", "status": "retire_candidate", "costs": [{"component": "server", "monthly_cost": "50.00"}]},
        {
            "slug": "GONE", "label": "Fake retired box", "vendor": "Vendor One", "kind": "dedicated", "status": "retired",
            "costs": [{"component": "server", "monthly_cost": "30.00"}],
            "mappings": [{"rule_type": "service_id", "rule_value": "16", "brand_scope": "BrandA"}],
        },
    ],
    "overhead": [
        {"label": "Licenses", "vendor": "Vendor Three", "monthly_cost": "25.00", "kind": "shared"},
        {"label": "Side project", "vendor": "Vendor Four", "monthly_cost": "10.00", "kind": "not_this_business"},
    ],
    "whatif": {"merge": ["VM", "DED"]},
}


def _load_whmcs(conn) -> None:
    stamp = now_iso()
    for brand, products in (("BrandA", [(1, "Starter", "Shared"), (2, "VPS Small", "VPS"), (3, "Dedicated", "Dedicated Servers")]), ("BrandB", [(1, "Repo", "Repos")])):
        for pid, name, group in products:
            conn.execute(
                "INSERT INTO whmcs_products (brand, source_id, kind, name, group_name, synced_at) VALUES (?, ?, 'product', ?, ?, ?)",
                (brand, pid, name, group, stamp),
            )
        conn.execute("INSERT INTO whmcs_products (brand, source_id, kind, name, synced_at) VALUES (?, 1, 'addon', 'Backup', ?)", (brand, stamp))
    for brand, cid, company, first, last in (
        ("BrandA", 1, "Fake Corp A", "Ann", "Zed"),
        ("BrandA", 2, "", "Pat", "Example"),
        ("BrandA", 3, "Third Co", "", ""),
        ("BrandB", 1, "Repo User", "", ""),
        ("BrandB", 2, "", "Sam", "Sample"),
    ):
        conn.execute(
            "INSERT INTO whmcs_clients (brand, source_id, company, first_name, last_name, status, synced_at) VALUES (?, ?, ?, ?, ?, 'Active', ?)",
            (brand, cid, company, first, last, stamp),
        )
    for brand, kind, sid, client, product, parent, domain, status, cycle, amount, server in SERVICES:
        conn.execute(
            """INSERT INTO whmcs_services (brand, kind, source_id, client_id, product_id, parent_id, domain, status,
               billing_cycle, amount_cents, reg_date, server_id, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2025-01-01', ?, ?)""",
            (brand, kind, sid, client, product, parent, domain, status, cycle, amount, server, stamp),
        )
    conn.execute("INSERT INTO whmcs_sync_log (started_at, finished_at, brand, status, counts_json) VALUES (?, ?, 'BrandA', 'ok', '{}')", (stamp, stamp))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HPBOOKS_KEY", KEY)
    monkeypatch.setenv("HPBOOKS_DB", str(tmp_path / "books.db"))
    monkeypatch.delenv("HPBOOKS_KEY_FILE", raising=False)
    monkeypatch.delenv("HPGBOOKS_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("HPGBOOKS_NEW_PASSPHRASE", raising=False)
    init_db()
    reset_lockout()
    clear_passphrase_cache()
    yield tmp_path
    reset_lockout()
    clear_passphrase_cache()


@pytest.fixture()
def seed_file(env):
    path = env / "seed.json"
    path.write_text(json.dumps(SEED), encoding="utf-8")
    return path


@pytest.fixture()
def seeded(env, seed_file):
    with connect() as conn:
        _load_whmcs(conn)
        mg.seed(conn, seed_file)
    return env


def _report(names: bool = True) -> dict:
    with connect(readonly=True) as conn:
        return mg.build(conn, names=names, today="2026-10-02")


def _server(report: dict, slug: str) -> dict:
    return next(row for row in report["servers"] if row["slug"] == slug)


# --- migration ------------------------------------------------------------------------------


def test_fresh_database_has_the_margin_tables(env):
    with connect(readonly=True) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_version ORDER BY version")]
    assert {"margin_servers", "margin_server_costs", "margin_mappings", "margin_overhead", "margin_settings"} <= tables
    assert versions == [version for version, _sql in MIGRATIONS] and 7 in versions


def test_migration_on_an_existing_database_keeps_its_data(env):
    with connect() as conn:
        _load_whmcs(conn)
        conn.execute("DELETE FROM schema_version WHERE version = 7")
        for table in ("margin_server_costs", "margin_mappings", "margin_overhead", "margin_settings", "margin_servers"):
            conn.execute(f"DROP TABLE {table}")
        rules = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        services = conn.execute("SELECT COUNT(*) FROM whmcs_services").fetchone()[0]
    with connect(readonly=True) as conn:
        assert not mg.margins_ready(conn)
        with pytest.raises(HpgbooksError):
            mg.build(conn)
    with connect() as conn:
        assert mg.margins_ready(conn)
        assert conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == rules
        assert conn.execute("SELECT COUNT(*) FROM whmcs_services").fetchone()[0] == services
        _apply_migrations(conn)  # a second run is a no-op
        assert conn.execute("SELECT COUNT(*) FROM schema_version WHERE version = 7").fetchone()[0] == 1
    report = _report()
    assert report["ready"] and not report["seeded"]
    assert report["totals"]["unmapped_cents"] == report["totals"]["mrr_cents"] == 37500


# --- margin math ----------------------------------------------------------------------------


def test_revenue_is_active_services_normalized_to_a_month(seeded):
    totals = _report()["totals"]
    # Annually/12, Quarterly/3, Semi-Annually/6, Biennially/24; One Time, Free Account, and Cancelled are 0.
    assert totals["mrr_cents"] == 37500
    assert totals["mrr_report_cents"] == 37500  # same figure as the MRR report
    assert totals["hosting_mrr_cents"] == 34500 and totals["addon_mrr_cents"] == 3000
    assert totals["active_services"] == 14


def test_per_server_margin_and_flags(seeded):
    report = _report()
    pool = _server(report, "POOL")
    assert (pool["cost_cents"], pool["revenue_cents"], pool["margin_cents"], pool["margin_pct"]) == (4000, 5800, 1800, 31.0)
    assert (pool["services"], pool["paying_services"], pool["customers"], pool["paying_customers"]) == (6, 5, 3, 2)
    assert [item["component"] for item in pool["components"]] == ["server", "license"]
    ded = _server(report, "DED")
    assert (ded["revenue_cents"], ded["margin_cents"], ded["margin_pct"]) == (22000, 7000, 31.8)
    assert ded["single_customer"] == {"brand": "BrandA", "client_id": 3, "name": "Third Co"}
    assert _server(report, "VM")["revenue_cents"] == 1000
    assert _server(report, "REPO")["margin_cents"] == 600
    old = _server(report, "OLD")
    assert old["flags"] == ["retire_candidate", "zero_revenue", "negative_margin"] and old["margin_pct"] is None
    gone = _server(report, "GONE")
    assert gone["cost_cents"] == 0 and "retired" in gone["flags"]


def test_overall_totals_and_brands(seeded):
    report = _report()
    t = report["totals"]
    assert t["server_cost_cents"] == 4000 + 15000 + 500 + 400 + 5000
    assert t["contribution_cents"] == 37500 - 24900
    assert (t["overhead_cents"], t["overhead_excluded_cents"]) == (2500, 1000)
    assert t["infrastructure_cents"] == 27400
    assert (t["blended_margin_cents"], t["blended_margin_pct"]) == (10100, 26.9)
    assert t["retire_candidate_cents"] == 5000
    brands = {row["brand"]: row for row in report["brands"]}
    assert (brands["BrandA"]["revenue_cents"], brands["BrandA"]["cost_cents"]) == (36500, 19500)
    assert (brands["BrandB"]["margin_cents"], brands["BrandB"]["margin_pct"]) == (600, 60.0)


def test_pool_cost_is_shared_by_revenue(seeded):
    groups = {(row["server_id"], row["group"]): row for row in _report()["plan_groups"]}
    pool = _server(_report(), "POOL")["id"]
    shared = groups[(pool, "Shared")]
    vps = groups[(pool, "VPS")]
    addons = groups[(pool, "Addons on Shared")]
    assert (shared["services"], shared["paying_services"], shared["revenue_cents"], shared["cost_cents"]) == (3, 2, 2000, 1379)
    assert (vps["revenue_cents"], vps["cost_cents"], vps["margin_pct"]) == (3000, 2069, 31.0)
    assert addons["cost_cents"] == 552
    # The even split shows cross-subsidy: half the services carry a third of the revenue.
    assert (shared["even_cost_cents"], vps["even_cost_cents"], addons["even_cost_cents"]) == (2000, 667, 1333)
    assert not any(row["server_id"] == _server(_report(), "DED")["id"] for row in _report()["plan_groups"])


def test_addons_follow_their_parent(seeded):
    report = _report()
    with connect(readonly=True) as conn:
        servers = mg.load_servers(conn)
        active, everything = mg.load_active_services(conn)
    mapper = mg.Mapper(servers, everything)
    resolved = {(item["kind"], item["id"]): mapper.resolve(item) for item in active}
    ids = {row["slug"]: row["id"] for row in report["servers"]}
    assert resolved[("addon", 201)][0]["id"] == ids["POOL"]
    assert resolved[("addon", 202)][0]["id"] == ids["DED"] and resolved[("addon", 202)][2] == "parent:service_id"
    # The parent is Cancelled; the addon still sits on the parent's server.
    assert resolved[("addon", 203)][0]["id"] == ids["POOL"]
    assert resolved[("hosting", 18)][2] == "domain"  # a domain rule beats the WHMCS server rule


def test_addon_rule_overrides_the_parent(seeded):
    with connect() as conn:
        vm = conn.execute("SELECT id FROM margin_servers WHERE slug = 'VM'").fetchone()[0]
        mg.add_mapping(conn, vm, "service_id", "addon:201", brand_scope="BrandA", actor="test")
    report = _report()
    assert _server(report, "VM")["revenue_cents"] == 1500
    assert _server(report, "POOL")["revenue_cents"] == 5300


def test_unmapped_detection(seeded):
    report = _report()
    assert (report["totals"]["unmapped_services"], report["totals"]["unmapped_cents"]) == (3, 7700)
    top = report["unmapped"]["top"]
    assert [(row["service_id"], row["reason"]) for row in top] == [(16, "server retired"), (204, "server retired")]
    assert top[0]["domain"] == "lost.fake-a.test" and top[0]["name"] == "Fake Corp A"
    groups = {(row["group"], row["cycle"]): row for row in report["unmapped"]["groups"]}
    assert groups[("Shared", "One Time")]["revenue_cents"] == 0
    with connect() as conn:
        conn.execute("UPDATE margin_servers SET status = 'active' WHERE slug = 'GONE'")
    assert _report()["totals"]["unmapped_cents"] == 0


def test_customer_rollups(seeded):
    report = _report()
    singles = [(row["server"], row["client_id"]) for row in report["single_customers"]]
    assert ("Fake dedicated", 3) in singles and ("Fake cloud vm", 3) in singles
    rollup = report["customer_rollups"][0]
    assert (rollup["client_id"], rollup["revenue_cents"], rollup["cost_cents"], rollup["margin_cents"]) == (3, 23000, 15500, 7500)
    assert rollup["name"] == "Third Co"


def test_cli_view_leaves_names_and_domains_out(seeded):
    """Mapping rules and server labels are the owner's own data; names and WHMCS domains stay out."""
    text = json.dumps(_report(names=False))
    for secret in NAMES + ("lost.fake-a.test", "box.fake-c.test", "a.fake-a.test"):
        assert secret not in text


def test_revenue_follows_a_whmcs_sync(seeded):
    with connect() as conn:
        conn.execute("UPDATE whmcs_services SET amount_cents = 30000 WHERE brand = 'BrandA' AND source_id = 13")
    assert _server(_report(), "DED")["revenue_cents"] == 32000


# --- what-if --------------------------------------------------------------------------------


def test_whatif_merge(seeded):
    report = _report()
    ids = {row["slug"]: row["id"] for row in report["servers"]}
    assert report["whatif"]["merge_default"] == [ids["VM"], ids["DED"]]
    result = mg.whatif(report, merge=(ids["VM"], ids["DED"]))
    assert result["cost_saved_cents"] == 500 and result["blended_margin_cents"] == 10600
    assert result["mrr_cents"] == 37500
    ded = next(row for row in result["servers"] if row["id"] == ids["DED"])
    assert (ded["revenue_after_cents"], ded["margin_after_cents"]) == (23000, 8000)
    with pytest.raises(HpgbooksError):
        mg.whatif(report, merge=(ids["VM"], ids["VM"]))
    with pytest.raises(HpgbooksError):
        mg.whatif(report, merge=(ids["VM"], ids["GONE"]))


def test_whatif_retire_increase_and_overhead_combine(seeded):
    report = _report()
    ids = {row["slug"]: row["id"] for row in report["servers"]}
    customer = next(row for row in report["whatif"]["customers"] if row["key"] == "BrandA:2")
    plan = next(row for row in report["whatif"]["plans"] if row["key"] == "BrandA:Starter")
    assert (customer["revenue_cents"], plan["revenue_cents"], plan["paying_services"]) == (4000, 3000, 3)
    licenses = next(line["id"] for line in report["overhead"] if line["label"] == "Licenses")
    result = mg.whatif(
        report,
        merge=(ids["VM"], ids["DED"]),
        retire=[ids["OLD"]],
        increases=[{"target": customer, "pct": 10}, {"target": plan, "flat_cents": 100}],
        exclude_overhead=[licenses],
    )
    assert result["revenue_added_cents"] == 400 + 300
    assert result["blended_margin_cents"] == 10100 + 500 + 5000 + 700 + 2500
    retire_only = mg.whatif(report, retire=[ids["DED"]])
    assert retire_only["blended_change_cents"] == 15000 - 22000


# --- seed and edits -------------------------------------------------------------------------


def test_seed_is_idempotent_and_audited(seeded, seed_file):
    with connect() as conn:
        assert mg.seed(conn, seed_file) == {}
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("margin_servers", "margin_server_costs", "margin_mappings", "margin_overhead")}
        assert counts == {"margin_servers": 6, "margin_server_costs": 7, "margin_mappings": 5, "margin_overhead": 2}
        rows = conn.execute("SELECT new_value FROM audit_log WHERE action = 'margins_seed'").fetchall()
    assert len(rows) == 1 and json.loads(rows[0][0])["servers_added"] == 6


def test_seed_rejects_bad_files(env, tmp_path):
    bad = tmp_path / "bad.json"
    for data in (
        {"servers": [{"slug": "x y", "label": "L"}]},
        {"servers": [{"slug": "A", "label": "L", "kind": "rack"}]},
        {"servers": [{"slug": "A", "label": "L", "costs": [{"component": "c", "monthly_cost": "-5"}]}]},
        {"servers": [{"slug": "A", "label": "L", "mappings": [{"rule_type": "service_id", "rule_value": "12"}]}]},
        {"servers": [{"slug": "A", "label": "L", "mappings": [{"rule_type": "sql", "rule_value": "1"}]}]},
    ):
        bad.write_text(json.dumps(data), encoding="utf-8")
        with connect() as conn, pytest.raises(HpgbooksError):
            mg.seed(conn, bad)
    with connect() as conn, pytest.raises(HpgbooksError):
        mg.seed(conn, tmp_path / "missing.json")


def test_edits_write_audit_rows(seeded):
    with connect() as conn:
        pool = conn.execute("SELECT id FROM margin_servers WHERE slug = 'POOL'").fetchone()[0]
        cost = mg.add_cost(conn, pool, "ip block", "8.50", "Oct", actor="test")
        mg.update_cost(conn, cost["id"], monthly_cost="9", actor="test")
        mg.delete_cost(conn, cost["id"], actor="test")
        mg.update_server(conn, pool, status="retire_candidate", notes="check capacity", actor="test")
        mapping = mg.add_mapping(conn, pool, "product_group", "VPS", brand_scope="BrandA", allocation="by_revenue", actor="test")
        mg.delete_mapping(conn, mapping["id"], actor="test")
        line = mg.add_overhead(conn, "Monitoring", "12.00", vendor="Vendor Five", actor="test")
        mg.update_overhead(conn, line["id"], kind="not_this_business", actor="test")
        actions = [row[0] for row in conn.execute("SELECT action FROM audit_log WHERE actor = 'test' ORDER BY id")]
        whmcs = conn.execute("SELECT COUNT(*) FROM whmcs_services").fetchone()[0]
    assert actions == [
        "margin_cost_add", "margin_cost_update", "margin_cost_delete", "margin_server_update",
        "margin_mapping_add", "margin_mapping_delete", "margin_overhead_add", "margin_overhead_update",
    ]
    assert whmcs == len(SERVICES)
    assert _report()["totals"]["overhead_excluded_cents"] == 2200


def test_edit_validation(seeded):
    with connect() as conn:
        pool = conn.execute("SELECT id FROM margin_servers WHERE slug = 'POOL'").fetchone()[0]
        for call in (
            lambda: mg.add_cost(conn, pool, "server", "1"),  # duplicate component
            lambda: mg.add_cost(conn, pool, "x", "abc"),
            lambda: mg.add_cost(conn, pool, "x", "88888888"),
            lambda: mg.add_cost(conn, 999, "x", "1"),
            lambda: mg.update_server(conn, pool, status="gone"),
            lambda: mg.add_mapping(conn, pool, "whmcs_server_id", "7", brand_scope="BrandA"),  # already mapped
            lambda: mg.add_mapping(conn, pool, "brand", "NotABrand"),
            lambda: mg.add_mapping(conn, pool, "domain", "bad domain'; --"),
            lambda: mg.add_overhead(conn, "Licenses", "1"),
            lambda: mg.add_overhead(conn, "New", "1", kind="maybe"),
        ):
            with pytest.raises(HpgbooksError):
                call()


# --- API ------------------------------------------------------------------------------------


def _client(extra_hosts=None):
    from hpgbooks.web import create_app

    app = create_app()
    if extra_hosts is not None:
        app.config["ALLOWED_HOSTS"] = tuple(extra_hosts)
    return app.test_client()


def _token(client, headers=None):
    return client.get("/api/session", headers=headers or {}).get_json()["csrf_token"]


def test_api_report_and_not_ready(env):
    client = _client()
    assert client.get("/api/margins").get_json()["ready"] is False
    with connect() as conn:
        _load_whmcs(conn)
    body = client.get("/api/margins").get_json()
    assert body["ready"] and not body["seeded"]


def test_api_report(seeded):
    client = _client()
    response = client.get("/api/margins")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    body = response.get_json()
    assert body["totals"]["blended_margin_cents"] == 10100
    assert any(row["name"] == "Third Co" for row in body["single_customers"])
    assert client.get("/whmcs/margins").status_code == 200
    assert client.get("/api/margins/nope").status_code == 404


def test_api_edits_need_csrf_and_are_audited(seeded):
    client = _client()
    report = client.get("/api/margins").get_json()
    pool = next(row for row in report["servers"] if row["slug"] == "POOL")
    assert client.post(f"/api/margins/servers/{pool['id']}/costs", json={"component": "ip", "monthly_cost": "3"}).status_code == 403
    headers = {"X-CSRF-Token": _token(client)}
    assert client.post(f"/api/margins/servers/{pool['id']}/costs", json={"component": "ip", "monthly_cost": "3.00"}, headers=headers).status_code == 200
    assert client.get("/api/margins").get_json()["totals"]["server_cost_cents"] == 24900 + 300
    cost_id = pool["components"][0]["id"]
    assert client.post(f"/api/margins/costs/{cost_id}", json={"monthly_cost": "31"}, headers=headers).get_json()["cost"]["monthly_cost_cents"] == 3100
    assert client.post(f"/api/margins/costs/{cost_id}/delete", json={}, headers=headers).status_code == 200
    assert client.post(f"/api/margins/servers/{pool['id']}", json={"status": "retire_candidate"}, headers=headers).status_code == 200
    mapping = client.post("/api/margins/mappings", json={"server_id": pool["id"], "rule_type": "service_id", "rule_value": "16", "brand_scope": "BrandB"}, headers=headers)
    assert mapping.status_code == 200
    assert client.post(f"/api/margins/mappings/{mapping.get_json()['mapping']['id']}/delete", json={}, headers=headers).status_code == 200
    line = client.post("/api/margins/overhead", json={"label": "Status page", "monthly_cost": "4", "kind": "shared"}, headers=headers).get_json()["line"]
    assert client.post(f"/api/margins/overhead/{line['id']}", json={"kind": "not_this_business"}, headers=headers).status_code == 200
    with connect(readonly=True) as conn:
        actions = [row[0] for row in conn.execute("SELECT action FROM audit_log WHERE actor = 'web' ORDER BY id")]
    assert actions == [
        "margin_cost_add", "margin_cost_update", "margin_cost_delete", "margin_server_update",
        "margin_mapping_add", "margin_mapping_delete", "margin_overhead_add", "margin_overhead_update",
    ]


def test_api_rejects_bad_input(seeded):
    client = _client()
    headers = {"X-CSRF-Token": _token(client)}
    assert client.post("/api/margins/servers/1", json={"status": "gone"}, headers=headers).status_code == 400
    assert client.post("/api/margins/servers/1", json={"label": "x"}, headers=headers).status_code == 400
    assert client.post("/api/margins/servers/999", json={"status": "active"}, headers=headers).status_code == 404
    assert client.post("/api/margins/servers/1/costs", json={"component": "x", "monthly_cost": "-1"}, headers=headers).status_code == 400
    assert client.post("/api/margins/mappings", json={"server_id": "1", "rule_type": "brand", "rule_value": "BrandB"}, headers=headers).status_code == 400
    form = client.post("/api/margins/overhead", data="label=x", headers={**headers, "Content-Type": "application/x-www-form-urlencoded"})
    assert form.status_code == 415
    assert client.delete("/api/margins/costs/1", headers=headers).status_code in (404, 405)


def test_api_needs_sign_in_on_the_tailnet(seeded, monkeypatch):
    monkeypatch.setenv("HPGBOOKS_NEW_PASSPHRASE", SECRET)
    assert main(["web-passphrase", "set"]) == 0
    clear_passphrase_cache()
    client = _client([HOST])
    headers = {"Host": HOST}
    response = client.get("/api/margins", headers=headers)
    assert response.status_code == 401 and b"Third Co" not in response.data
    token = _token(client, headers)
    assert client.post("/api/margins/overhead", json={"label": "x", "monthly_cost": "1"}, headers={**headers, "X-CSRF-Token": token}).status_code == 401
    assert client.post("/api/login", json={"passphrase": SECRET}, headers={**headers, "X-CSRF-Token": token}).status_code == 200
    assert client.get("/api/margins", headers=headers).status_code == 200


def test_api_handler_checks_sign_in_itself(seeded, monkeypatch):
    import hpgbooks.margins_api as module

    monkeypatch.setattr(module, "request_requires_login", lambda: True)
    monkeypatch.setattr(module, "session_is_authenticated", lambda: False)
    client = _client()
    assert client.get("/api/margins").status_code == 401


# --- CLI ------------------------------------------------------------------------------------


def test_cli(seeded, seed_file, capsys):
    assert main(["margins", "seed", "--file", str(seed_file)]) == 0
    assert "no changes" in capsys.readouterr().out
    assert main(["margins", "show"]) == 0
    out = capsys.readouterr().out
    assert "Blended margin" in out and "101.00" in out and "26.9%" in out
    assert "Fake pool box" in out and "retire_candidate zero_revenue" in out
    for name in ("servers", "overhead", "unmapped"):
        assert main(["margins", name]) == 0
    out = capsys.readouterr().out
    assert "maps whmcs_server_id=7" in out and "Side project" in out and "server retired" in out
    assert main(["margins", "show", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["totals"]["blended_margin_cents"] == 10100
    assert main(["margins", "whatif", "--merge", "VM", "DED", "--retire", "OLD"]) == 0
    out = capsys.readouterr().out
    assert "156.00" in out and "removed" in out
    assert main(["margins", "whatif", "--retire", "NOPE"]) == 1
    for secret in NAMES + ("fake-a.test",):
        assert secret not in out
