"""Site config: generic defaults, a fresh install with no config, and WHMCS switched off."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hpgbooks import config as hpconfig
from hpgbooks.config import ConfigError, build, load_rules_file

ROOT = Path(__file__).resolve().parent.parent
KEY = "cd" * 32

BUSINESS_GETS = [
    "/api/session",
    "/api/config",
    "/api/dashboard",
    "/api/dashboard?start=2026-01-01&end=2026-09-30",
    "/api/pnl",
    "/api/pnl?business=general",
    "/api/pnl/lines?label=Net%20Income",
    "/api/accounts",
    "/api/accounts/settings",
    "/api/vendors",
    "/api/review",
    "/api/review-count",
    "/api/rules",
    "/api/settings",
    "/api/calendar",
    "/api/balances",
    "/api/search?q=fee",
    "/api/transactions",
    "/api/payments",
    "/api/audit",
    "/api/reports/pnl-by-business",
    "/api/reports/owner-draws",
    "/api/reports/schedule-c",
    "/api/reports/cash-flow",
    "/api/reports/expenses-by-vendor",
    "/export/pnl.csv",
    "/export/transactions.csv",
    "/app/",
]
PERSONAL_GETS = [
    f"/api/personal/{name}?mode=personal"
    for name in (
        "status", "dashboard", "accounts", "net-worth", "spending", "cash-flow", "budgets", "recurring",
        "bills", "goals", "summary", "categories", "rules", "merchants", "transactions", "transfers",
        "review", "reconcile",
    )
] + ["/api/session?mode=personal"]
WHMCS_GETS = [
    "/api/whmcs/status",
    "/api/whmcs/summary",
    "/api/whmcs/revenue",
    "/api/whmcs/reconcile",
    "/api/margins",
    "/export/whmcs/revenue.csv",
]

# Runs in a child process: business tags and categories are fixed at import,
# and this test process already imported hpgbooks with the test config.
FRESH_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from hpgbooks.cli import main
    assert main(["init"]) == 0
    from hpgbooks.db import connect
    from hpgbooks.web import create_app
    out = {"statuses": {}, "bodies": {}}
    client = create_app().test_client()
    for url in json.loads(sys.argv[1]):
        response = client.get(url)
        out["statuses"][url] = response.status_code
        if response.is_json:
            out["bodies"][url] = response.get_json()
    with connect(readonly=True) as conn:
        out["accounts"] = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        out["rules"] = [dict(row) for row in conn.execute("SELECT business_tag, category, account_id FROM rules")]
        out["p_categories"] = conn.execute("SELECT COUNT(*) FROM p_categories").fetchone()[0]
        out["p_rules"] = conn.execute("SELECT COUNT(*) FROM p_rules").fetchone()[0]
        out["schema"] = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'rules'").fetchone()[0]
    from hpgbooks.seed_rules import STARTER_RULES, rule_specs
    specs = rule_specs()
    out["starter"] = {"total": len(STARTER_RULES), "resolved": len(specs),
                      "stripe": sorted({spec[6] for spec in specs if "STRIPE" in spec[1]})}
    out["whmcs_cli"] = main(["whmcs", "status"])
    out["margins_cli"] = main(["margins", "show"])
    print("RESULT" + json.dumps(out))
    """
)


def test_fresh_install_with_no_config_works(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith(("HPGBOOKS_", "HPBOOKS_"))}
    env.update(
        HPBOOKS_DB=str(tmp_path / "data" / "hpbooks.db"),
        HPBOOKS_KEY=KEY,
        HPBOOKS_CONFIG=str(tmp_path / "missing.toml"),
        HPGBOOKS_SEED_CSV="",
    )
    urls = BUSINESS_GETS + PERSONAL_GETS + WHMCS_GETS
    proc = subprocess.run(
        [sys.executable, "-c", FRESH_SCRIPT, json.dumps(urls)],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.split("RESULT", 1)[1])
    statuses = result["statuses"]
    for url in BUSINESS_GETS + PERSONAL_GETS:
        assert statuses[url] == 200, (url, statuses[url], result["bodies"].get(url))
    for url in WHMCS_GETS:
        assert statuses[url] == 404, url
        assert result["bodies"][url] == {"ok": False, "error": "WHMCS integration is disabled"}
    config = result["bodies"]["/api/config"]
    assert config["product"] == "Books" and config["company"] == "My Business"
    assert config["features"] == {"whmcs": False, "margins": False, "business": True, "personal": True}
    assert [b["slug"] for b in config["businesses"]] == ["general"]
    assert config["whmcs_brands"] == [] and config["operating_account"] is None
    session = result["bodies"]["/api/session"]
    assert session["product"] == "Books" and session["businesses"] == ["all", "general"]
    assert session["tags"] == ["general", "owner_draw", "transfer", "needs_review"]
    assert result["accounts"] == 0
    # Generic starter rules only: no account ids, only the generic business.
    assert result["rules"] and all(rule["account_id"] is None for rule in result["rules"])
    assert {rule["business_tag"] for rule in result["rules"]} <= {"general", "transfer"}
    # Every starter rule fits the default categories; Stripe payouts are sales revenue.
    assert result["starter"]["resolved"] == result["starter"]["total"] > 0
    assert result["starter"]["stripe"] == ["Revenue - Sales"]
    assert len(result["rules"]) == result["starter"]["total"]
    # Schedule C lines come from the default operating expense categories.
    schedule = [(row[0]["text"], row[1]["text"]) for row in result["bodies"]["/api/reports/schedule-c"]["rows"]]
    assert schedule[3] == ("4", "Cost of goods sold")
    assert [code for code, _ in schedule[7:-5]] == DEFAULT_SCHEDULE_C_CODES
    assert ("27a", "Other: Software & Licenses") in schedule and ("26", "Wages") in schedule
    assert result["p_categories"] > 0 and result["p_rules"] > 0
    assert "'general', 'owner_draw', 'transfer', 'needs_review'" in result["schema"]
    assert result["whmcs_cli"] == 2 and result["margins_cli"] == 2
    assert "WHMCS integration is disabled" in proc.stderr
    assert "Traceback" not in proc.stderr


# Schedule C line codes for the default operating expense categories, in report order.
DEFAULT_SCHEDULE_C_CODES = ["8", "11", "15", "16b", "17", "18", "20b", "23", "24a", "24b", "25", "26", "27a", "27b", "27c"]


def test_generic_defaults():
    cfg = build({})
    assert cfg.product_name == "Books" and cfg.company_name == "My Business" and cfg.wordmark == "Books"
    assert cfg.whmcs_enabled is False and cfg.margins_enabled is False
    assert cfg.business_slugs == ("general",)
    assert cfg.business_filter_error == "business must be all or general"
    assert cfg.accounts == () and cfg.transfer_pairs == () and cfg.whmcs.brands == ()
    assert cfg.revenue_categories == ("Revenue - Sales", "Revenue - Services")
    assert cfg.cogs_categories == ("Cost of Goods Sold",)
    assert cfg.categories[:3] == ("Revenue - Sales", "Revenue - Services", "Refunds")
    assert cfg.categories[-3:] == ("Owner Draw", "Transfer", "Uncategorized")
    for name in ("Software & Licenses", "Payroll", "Contractors", "Professional Services", "Advertising", "Insurance",
                 "Rent", "Utilities", "Travel", "Meals", "Taxes & Licenses", "Office/Other"):
        assert name in cfg.opex_categories, name
    assert cfg.short_account("Some Bank 1234") == "Some Bank 1234"
    assert cfg.account_for_role("operating") is None
    assert cfg.payer_hints == () and cfg.prior_status == "business"
    assert cfg.importers == hpconfig.ImportersConfig()
    assert cfg.importers.applecard_account_id == "manual-apple-card" and cfg.importers.cfna_last4 is None


def test_whmcs_defaults_and_revenue_category():
    cfg = build({})
    assert cfg.whmcs.ssh_key == "~/.ssh/hpbooks_whmcs"
    # An unset revenue category means the first revenue category.
    assert cfg.whmcs.revenue_category == "Revenue - Sales"
    brand = build({"whmcs": {"brands": [{"name": "BrandA", "server": "billing.branda.example"}]}}).whmcs.brands[0]
    assert (brand.secret, brand.database, brand.port) == ("whmcs.secret", "whmcs", 3307)
    assert build({"whmcs": {"revenue_category": "Revenue - Services"}}).whmcs.revenue_category == "Revenue - Services"
    # Only checked when the integration is on.
    assert build({"whmcs": {"revenue_category": "Nope"}}).whmcs.revenue_category == "Nope"
    with pytest.raises(ConfigError):
        build({"features": {"whmcs": True}, "whmcs": {"revenue_category": "Nope"}})
    # The test config names its own.
    assert hpconfig.get_config().whmcs.revenue_category == "Revenue - Hosting"


def test_seed_prior_status_marks_business_rows(tmp_path, monkeypatch):
    from hpgbooks.classify import _seed_decision, reset_seed_cache

    seed_csv = tmp_path / "prior.csv"
    seed_csv.write_text(
        "txn_id,status,category,vendor\n"
        "t-biz,business,Licenses,Sample Software Co\n"
        "t-own,keep,Licenses,Sample Software Co\n"
        "t-unmapped,business,Unknown,\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HPGBOOKS_SEED_CSV", str(seed_csv))
    reset_seed_cache()
    try:
        assert build({"seed": {"prior_status": "keep"}}).prior_status == "keep"
        decision = _seed_decision("t-biz")
        assert (decision["business_tag"], decision["category"], decision["source"]) == ("branda", "Software & Licenses", "seed")
        assert decision["note"] == "seed from prior analysis: Sample Software Co"
        assert _seed_decision("t-own") is None and _seed_decision("t-unmapped") is None
        hpconfig.override(prior_status="keep")
        assert _seed_decision("t-biz") is None and _seed_decision("t-own")["category"] == "Software & Licenses"
    finally:
        reset_seed_cache()


def test_schedule_c_lines_follow_the_configured_categories():
    from hpgbooks.analytics import _cogs_line_label, _schedule_c_lines

    defaults = build({})
    hpconfig.override(opex_categories=defaults.opex_categories, cogs_categories=defaults.cogs_categories)
    lines = _schedule_c_lines()
    assert [code for code, _, _ in lines] == DEFAULT_SCHEDULE_C_CODES
    by_category = {category: (code, label) for code, label, category in lines}
    assert by_category["Advertising"] == ("8", "Advertising")
    assert by_category["Contractors"] == ("11", "Contract labor")
    assert by_category["Insurance"] == ("15", "Insurance (other than health)")
    assert by_category["Interest"] == ("16b", "Interest (other)")
    assert by_category["Professional Services"] == ("17", "Legal and professional services")
    assert by_category["Office/Other"] == ("18", "Office expense")
    assert by_category["Rent"] == ("20b", "Rent or lease (other business property)")
    assert by_category["Taxes & Licenses"] == ("23", "Taxes and licenses")
    assert by_category["Travel"] == ("24a", "Travel")
    assert by_category["Meals"] == ("24b", "Deductible meals")
    assert by_category["Utilities"] == ("25", "Utilities")
    assert by_category["Payroll"] == ("26", "Wages")
    # Categories without a line of their own become 27a, 27b, ... in config order.
    assert [(code, label) for code, label, _ in lines[-3:]] == [
        ("27a", "Other: Software & Licenses"),
        ("27b", "Other: Bank & Card Fees"),
        ("27c", "Other: Payment Processing Fees"),
    ]
    assert _cogs_line_label() == "Cost of goods sold"

    # A site without some categories just has fewer lines; the order and lettering still hold.
    hpconfig.override(opex_categories=("Widgets", "Payroll", "Rent", "Gadgets"), cogs_categories=("Hosting & Infrastructure",))
    assert [(code, label) for code, label, _ in _schedule_c_lines()] == [
        ("20b", "Rent or lease (other business property)"),
        ("26", "Wages"),
        ("27a", "Other: Widgets"),
        ("27b", "Other: Gadgets"),
    ]
    assert _cogs_line_label() == "Cost of goods sold (hosting & infrastructure)"
    hpconfig.override(opex_categories=(), cogs_categories=())
    assert _schedule_c_lines() == []
    assert _cogs_line_label() == "Cost of goods sold"
    hpconfig.override(cogs_categories=("Materials", "Freight"))
    assert _cogs_line_label() == "Cost of goods sold"


def test_schedule_c_report_with_the_test_categories(tmp_path, monkeypatch):
    """The test config lacks most Schedule C categories; the report still adds up."""
    from hpgbooks.analytics import schedule_c_report
    from hpgbooks.db import connect, init_db

    monkeypatch.setenv("HPBOOKS_KEY", KEY)
    monkeypatch.setenv("HPBOOKS_DB", str(tmp_path / "books.db"))
    monkeypatch.delenv("HPBOOKS_KEY_FILE", raising=False)
    init_db()
    with connect() as conn:
        report = schedule_c_report(conn, "2026-01-01", "2026-12-31", "all")
    codes = [row[0].text for row in report.rows]
    labels = {row[0].text: row[1].text for row in report.rows}
    assert labels["4"] == "Cost of goods sold (hosting & infrastructure)"
    assert "15" not in codes and "24a" not in codes  # no Insurance or Travel category here
    assert labels["27a"] == "Other: Software & Licenses" and labels["27b"] == "Other: Domains/DNS/SSL"
    assert codes.index("26") < codes.index("27a") < codes.index("28") < codes.index("31")


def test_warn_if_exposed(tmp_path, capsys):
    private = tmp_path / "local.toml"
    private.write_text("[app]\n", encoding="utf-8")
    private.chmod(0o600)
    assert hpconfig.warn_if_exposed(private) is False
    assert capsys.readouterr().err == ""
    private.chmod(0o644)
    assert hpconfig.warn_if_exposed(private) is True
    err = capsys.readouterr().err
    assert "readable by other users" in err and f"chmod 600 {private}" in err
    hpconfig.load(private)  # load() warns too
    assert "readable by other users" in capsys.readouterr().err
    # The committed example and the test fixtures are never flagged.
    assert hpconfig.warn_if_exposed(ROOT / "config" / "config.example.toml") is False
    assert hpconfig.warn_if_exposed(ROOT / "tests" / "fixtures" / "config.test.toml") is False
    assert hpconfig.warn_if_exposed(tmp_path / "missing.toml") is False


def test_margins_follow_whmcs():
    assert build({"features": {"whmcs": True}}).margins_enabled is True
    assert build({"features": {"whmcs": True, "margins": False}}).margins_enabled is False
    assert build({"features": {"margins": True}}).margins_enabled is False


def test_accounts_roles_and_short_names():
    cfg = build(
        {
            "accounts": {
                "business": [
                    {"id": "a1", "name": "Big Bank Checking 1234", "type": "cash", "short_name": "Big 1234",
                     "short_name_prefix": "Big Bank", "roles": ["operating"]},
                    {"id": "c1", "name": "Card 9", "type": "liability", "roles": "capitalone_default"},
                ]
            },
            "transfer_pairs": [
                {"kind": "card", "left_account": "operating", "left_pattern": "PAY", "right_account": "c1",
                 "right_pattern": "THANK YOU"}
            ],
        }
    )
    assert cfg.account_for_role("operating") == "a1"
    assert cfg.account_for_role("capitalone_default") == "c1"
    assert cfg.resolve_account("operating") == "a1" and cfg.resolve_account("c1") == "c1"
    assert cfg.short_account("Big Bank Checking 1234") == "Big 1234"
    assert cfg.short_account("Big Bank Savings") == "Big 1234"
    assert cfg.short_account("Other") == "Other"
    assert cfg.transfer_pairs[0].window == "abs7"


@pytest.mark.parametrize(
    "data",
    [
        {"businesses": []},
        {"businesses": [{"slug": "owner_draw"}]},
        {"businesses": [{"slug": "Bad Slug"}]},
        {"businesses": [{"slug": "a"}, {"slug": "a"}]},
        {"businesses": [{"slug": "a", "revenue_category": "Nope"}]},
        {"categories": {"revenue": []}},
        {"categories": {"opex": ["Refunds"]}},
        {"categories": {"opex": ["It's"]}},
        {"accounts": {"business": [{"id": "x", "name": "X", "type": "loan"}]}},
        {"transfer_pairs": [{"kind": "k"}]},
        {"transfer_pairs": [{"kind": "k", "left_account": "a", "left_pattern": "p", "right_account": "b",
                             "right_pattern": "q", "window": "never"}]},
        {"whmcs": {"brands": [{"name": "A"}]}},
        {"features": {"whmcs": "yes"}},
    ],
)
def test_invalid_config_is_rejected(data):
    with pytest.raises(ConfigError):
        build(data)


def test_config_file_and_rules_file(tmp_path, monkeypatch):
    (tmp_path / "rules.json").write_text(
        json.dumps([{"priority": 5, "pattern": "ACME", "tag": "general", "category": "Office/Other",
                     "confidence": 0.9, "note": "n", "account": None, "sign": "out"}]),
        encoding="utf-8",
    )
    path = tmp_path / "site.toml"
    path.write_text('[app]\nproduct_name = "Acme Books"\n[seed]\nrules_file = "rules.json"\n', encoding="utf-8")
    monkeypatch.setenv("HPBOOKS_CONFIG", str(path))
    hpconfig.reset()
    cfg = hpconfig.get_config()
    assert cfg.product_name == "Acme Books" and cfg.wordmark == "Acme Books"
    assert cfg.rules_file == tmp_path / "rules.json"
    assert load_rules_file(cfg.rules_file) == [(5, "ACME", "any", None, "out", "general", "Office/Other", 0.9, "n")]
    path.write_text("[app\n", encoding="utf-8")
    hpconfig.reset()
    with pytest.raises(ConfigError):
        hpconfig.get_config()


def test_missing_config_file_means_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("HPBOOKS_CONFIG", str(tmp_path / "nope.toml"))
    assert hpconfig.config_path() is None
    hpconfig.reset()
    assert hpconfig.get_config().product_name == "Books"


def test_starter_rules_resolve_against_the_config():
    from hpgbooks.seed_rules import STARTER_RULES, rule_specs

    hpconfig.override(starter_rules=True, rules_file=None)
    specs = rule_specs()
    assert 0 < len(specs) <= len(STARTER_RULES)
    assert all(spec[3] is None for spec in specs)
    # The overhead business gets the costs; payouts go to its (or the first) revenue category.
    assert {spec[5] for spec in specs} <= {"general", "transfer"}
    assert any(spec[6] == "Revenue - Hosting" for spec in specs)
    # Starter rules for categories this site lacks are skipped, not errors.
    assert len(specs) < len(STARTER_RULES)
    assert not any(spec[6] in ("Insurance", "Travel") for spec in specs)


# --- WHMCS off -----------------------------------------------------------------------------


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HPBOOKS_KEY", KEY)
    monkeypatch.setenv("HPBOOKS_DB", str(tmp_path / "books.db"))
    monkeypatch.setenv("HPGBOOKS_SEED_CSV", str(tmp_path / "no-seed.csv"))
    monkeypatch.delenv("HPBOOKS_KEY_FILE", raising=False)
    from hpgbooks.db import init_db

    init_db()
    return tmp_path


@pytest.mark.whmcs_on
def test_config_endpoint_with_whmcs_on(app_env):
    from hpgbooks.web import create_app

    body = create_app().test_client().get("/api/config").get_json()
    assert body["product"] == "Example Books" and body["wordmark"] == "Example"
    assert body["features"] == {"whmcs": True, "margins": True, "business": True, "personal": True}
    assert [b["slug"] for b in body["businesses"]] == ["branda", "consulting", "general"]
    assert body["businesses"][0]["tone"] == "brand" and body["businesses"][1]["tone"] == "violet"
    assert body["default_business"] == "general"
    assert body["whmcs_brands"] == ["BrandA", "BrandB", "BrandC"]
    assert body["operating_account"]["short_name"] == "Bank 0101"
    assert [a["label"] for a in body["accounts"] if a["type"] == "cash"] == ["Bank", "PayPal"]


def test_whmcs_routes_404_when_disabled(app_env, whmcs_off):
    from hpgbooks.web import create_app

    client = create_app().test_client()
    for url in WHMCS_GETS + ["/api/whmcs/customers?q=a", "/api/margins/servers"]:
        response = client.get(url)
        assert response.status_code == 404, url
        assert response.get_json() == {"ok": False, "error": "WHMCS integration is disabled"}
    body = client.get("/api/config").get_json()
    assert body["features"] == {"whmcs": False, "margins": False, "business": True, "personal": True} and body["whmcs_brands"] == []
    for url in ("/api/dashboard", "/api/session", "/api/pnl", "/api/calendar"):
        assert client.get(url).status_code == 200, url


@pytest.mark.whmcs_on
def test_margins_routes_404_when_only_margins_disabled(app_env):
    from hpgbooks.web import create_app

    hpconfig.override(margins_enabled=False)
    client = create_app().test_client()
    assert client.get("/api/margins").status_code == 404
    assert client.get("/api/whmcs/status").status_code == 200


def test_whmcs_cli_disabled_message(app_env, whmcs_off, capsys):
    from hpgbooks.cli import main

    assert main(["whmcs", "status"]) == 2
    assert main(["margins", "show"]) == 2
    err = capsys.readouterr().err
    assert "WHMCS integration is disabled (set features.whmcs = true in config/local.toml)" in err
    assert "Traceback" not in err


def test_business_and_personal_flags_default_on():
    cfg = build({})
    assert cfg.business_enabled and cfg.personal_enabled
    off = build({"features": {"personal": False}})
    assert off.business_enabled and not off.personal_enabled
    assert off.public_payload(detail=False)["features"]["personal"] is False
    with pytest.raises(ConfigError):
        build({"features": {"business": False, "personal": False}})


def test_personal_off_hides_cli_and_api(app_env, capsys):
    from hpgbooks.cli import main
    from hpgbooks.web import create_app

    hpconfig.override(personal_enabled=False)
    assert main(["personal", "dashboard"]) == 2
    assert "personal mode is disabled" in capsys.readouterr().err
    client = create_app().test_client()
    assert client.get("/api/personal/status").status_code == 404
    assert client.get("/api/config").get_json()["features"]["personal"] is False
    assert client.get("/api/dashboard").status_code == 200


def test_business_off_hides_business_cli(app_env, capsys):
    from hpgbooks.cli import main

    hpconfig.override(business_enabled=False)
    assert main(["pnl"]) == 2
    assert main(["rules", "list"]) == 2
    assert "business mode is disabled" in capsys.readouterr().err
    assert main(["accounts", "list"]) == 0


def test_example_config_documents_the_defaults():
    import dataclasses

    example = hpconfig.load(ROOT / "config" / "config.example.toml")
    defaults = build({})
    for item in dataclasses.fields(example):
        if item.name not in ("source", "base_dir"):
            assert getattr(example, item.name) == getattr(defaults, item.name), item.name
