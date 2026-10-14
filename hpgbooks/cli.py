"""Command line for hpgbooks. The database key is never printed."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hpgbooks.config import BUSINESS_DISABLED, PERSONAL_DISABLED, WHMCS_DISABLED, get_config
from hpgbooks.db import short_id
from hpgbooks.db import HpgbooksError, connect, init_db
from hpgbooks.reports import BUSINESS_FILTERS


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    disabled = _disabled_feature(args.cmd)
    if disabled:
        print(f"hpgbooks: {disabled}", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except HpgbooksError as exc:
        print(f"hpgbooks: {exc}", file=sys.stderr)
        return 1


# Commands that only make sense with business mode on. init, import, accounts, audit,
# web, web-passphrase, personal, and update work in either mode.
BUSINESS_COMMANDS = frozenset({
    "seed-rules", "reclassify", "classify", "review", "pnl", "reconcile", "txns", "rules",
    "transfers", "balances", "vendors", "seedcheck", "whmcs", "margins",
    "import-capitalone", "verify-capitalone",
})


def _disabled_feature(cmd: str) -> str | None:
    """Message for a subcommand whose feature is off in the config, else None."""
    cfg = get_config()
    if cmd == "personal" and not cfg.personal_enabled:
        return PERSONAL_DISABLED
    if cmd in BUSINESS_COMMANDS and not cfg.business_enabled:
        return BUSINESS_DISABLED
    if cmd in ("whmcs", "margins") and not cfg.whmcs_enabled:
        return WHMCS_DISABLED
    if cmd == "margins" and not cfg.margins_enabled:
        return "server margins are disabled (set features.margins = true in config/local.toml)"
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hpgbooks", description=f"Ledger for {get_config().company_name}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="create the database, accounts, and seed rules")
    init.set_defaults(func=cmd_init)

    seed = sub.add_parser("seed-rules", help="insert any missing seed rules")
    seed.set_defaults(func=cmd_seed_rules)

    imp = sub.add_parser("import", help="import JSON files, a directory, or a glob")
    imp.add_argument("paths", nargs="+")
    imp.add_argument("--from", dest="date_from", help="YYYY-MM-DD inclusive")
    imp.add_argument("--to", dest="date_to", help="YYYY-MM-DD inclusive")
    imp.set_defaults(func=cmd_import)

    rec = sub.add_parser("reclassify", help="re-run rules on non-manual rows")
    rec.add_argument("--all", action="store_true", help="accepted for compatibility; non-manual rows are always reclassified")
    rec.set_defaults(func=cmd_reclassify)

    classify = sub.add_parser("classify", help="set a manual classification")
    classify.add_argument("txn", help="transaction id or unique prefix")
    classify.add_argument("--tag", required=True)
    classify.add_argument("--category", required=True)
    classify.add_argument("--note", default="")
    classify.add_argument("--rule", action="store_true", help="also create a rule and apply it to other non-manual rows")
    classify.add_argument("--pattern", help="regex for --rule; default is an escaped merchant/name prefix")
    classify.set_defaults(func=cmd_classify)

    review = sub.add_parser("review", help="list needs_review rows")
    review.add_argument("--year", type=int)
    review.add_argument("--format", default="table", choices=("table", "csv"))
    review.set_defaults(func=cmd_review)

    pnl = sub.add_parser("pnl", help="profit and loss")
    pnl.add_argument("--year", type=int, default=2026)
    pnl.add_argument("--by", default="month", choices=("month", "year"))
    pnl.add_argument("--ytd", action="store_true")
    pnl.add_argument("--business", default="all", choices=BUSINESS_FILTERS)
    pnl.add_argument("--format", default="table", choices=("table", "csv", "xlsx", "pdf"))
    pnl.add_argument("--out")
    pnl.set_defaults(func=cmd_pnl)

    reconcile = sub.add_parser("reconcile", help="tie raw activity to P&L net income")
    reconcile.add_argument("--year", type=int, default=2026)
    reconcile.set_defaults(func=cmd_reconcile)

    txns = sub.add_parser("txns", help="list transactions")
    txns.add_argument("--month", help="YYYY-MM")
    txns.add_argument("--account", help="id, last4, or name")
    txns.add_argument("--tag")
    txns.add_argument("--search")
    txns.add_argument("--limit", type=int, default=200)
    txns.set_defaults(func=cmd_txns)

    rules = sub.add_parser("rules", help="list, add, or disable rules")
    rules_sub = rules.add_subparsers(dest="rules_cmd", required=True)
    rules_list = rules_sub.add_parser("list")
    rules_list.set_defaults(func=cmd_rules_list)
    rules_add = rules_sub.add_parser("add")
    rules_add.add_argument("--pattern", required=True)
    rules_add.add_argument("--tag", required=True)
    rules_add.add_argument("--category", required=True)
    rules_add.add_argument("--field", default="any", choices=("name", "merchant", "any"))
    rules_add.add_argument("--account")
    rules_add.add_argument("--sign", choices=("in", "out"))
    rules_add.add_argument("--min", dest="min_amount", type=float)
    rules_add.add_argument("--max", dest="max_amount", type=float)
    rules_add.add_argument("--priority", type=int, default=12)
    rules_add.add_argument("--confidence", type=float, default=0.9)
    rules_add.add_argument("--note", default="")
    rules_add.set_defaults(func=cmd_rules_add)
    rules_disable = rules_sub.add_parser("disable")
    rules_disable.add_argument("rule_id", type=int)
    rules_disable.set_defaults(func=cmd_rules_disable)

    transfers = sub.add_parser("transfers", help="inter-account payment pairs and mismatches")
    transfers.set_defaults(func=cmd_transfers)

    accounts = sub.add_parser("accounts", help="balances from imported activity; list, scope, and discover accounts")
    accounts.set_defaults(func=cmd_accounts)
    accounts_sub = accounts.add_subparsers(dest="accounts_cmd")
    accounts_list = accounts_sub.add_parser("list", help="every registered account with scope and class")
    accounts_list.add_argument("--scope", choices=("business", "personal", "excluded"))
    accounts_list.add_argument("--json", action="store_true")
    accounts_list.set_defaults(func=cmd_accounts_list)
    accounts_scope = accounts_sub.add_parser("set-scope", help="move an account to business, personal, or excluded")
    accounts_scope.add_argument("account", help="id, last4, or a unique name fragment")
    accounts_scope.add_argument("scope", choices=("business", "personal", "excluded"))
    accounts_scope.set_defaults(func=cmd_accounts_set_scope)
    accounts_class = accounts_sub.add_parser("set-class", help="cash, liability, investment, loan, or other")
    accounts_class.add_argument("account")
    accounts_class.add_argument("account_class", choices=("cash", "liability", "investment", "loan", "other"))
    accounts_class.set_defaults(func=cmd_accounts_set_class)
    accounts_rename = accounts_sub.add_parser("rename", help="set the display name (empty string clears it)")
    accounts_rename.add_argument("account")
    accounts_rename.add_argument("display_name")
    accounts_rename.set_defaults(func=cmd_accounts_rename)
    accounts_discover = accounts_sub.add_parser("discover", help="register accounts from a saved finance_list_accounts result")
    accounts_discover.add_argument("file", help="JSON file, or - for stdin")
    accounts_discover.add_argument("--scope", default="personal", choices=("business", "personal", "excluded"))
    accounts_discover.add_argument("--as-of", dest="as_of", help="date of current_balance (default today)")
    accounts_discover.add_argument("--dry-run", action="store_true")
    accounts_discover.set_defaults(func=cmd_accounts_discover)
    accounts_pay = accounts_sub.add_parser("set-payment", help="record minimum payment, due date, APR for a card or loan")
    accounts_pay.add_argument("account", help="id, last4, or a unique name fragment")
    accounts_pay.add_argument("--min", dest="min_payment", help="minimum payment due in dollars (0 = nothing due)")
    accounts_pay.add_argument("--due", help="payment due date YYYY-MM-DD")
    accounts_pay.add_argument("--apr", help="APR, e.g. 29.64 or '19.74% purchase / 28.74% cash'")
    accounts_pay.add_argument("--source", default="manual", help="manual (default), statement, issuer site, sheet, inferred")
    accounts_pay.add_argument("--as-of", dest="as_of", help="date the figures describe (default today)")
    accounts_pay.add_argument("--autopay", choices=("yes", "no", "unknown"))
    accounts_pay.add_argument("--notes")
    accounts_pay.add_argument("--payer-pattern", dest="payer_pattern", help="regex of the bank debit that pays a loan (for loans paid from a bank account)")
    accounts_pay.add_argument("--unverified", action="store_true", help="flag the figures as unverified")
    accounts_pay.add_argument("--paid", nargs="?", const="today", metavar="DATE", help="record that this cycle's payment was made (default today)")
    accounts_pay.set_defaults(func=cmd_accounts_set_payment)
    accounts_pays = accounts_sub.add_parser("payments", help="minimum payments and due dates, soonest first")
    accounts_pays.add_argument("--mode", choices=("business", "personal"), default="personal")
    accounts_pays.add_argument("--json", action="store_true")
    accounts_pays.set_defaults(func=cmd_accounts_payments)
    accounts_infer = accounts_sub.add_parser("infer-payments", help="estimate loan payments and next due dates from payment history")
    accounts_infer.add_argument("--dry-run", action="store_true")
    accounts_infer.set_defaults(func=cmd_accounts_infer_payments)
    accounts_sync = accounts_sub.add_parser("sync-list", help="accounts, Finance tool, and inbox prefix for the daily pull")
    accounts_sync.add_argument("--scope", choices=("business", "personal"))
    accounts_sync.add_argument("--json", action="store_true")
    accounts_sync.set_defaults(func=cmd_accounts_sync_list)

    balances = sub.add_parser("balances", help="statement balance anchors")
    balances_sub = balances.add_subparsers(dest="balances_cmd", required=True)
    balances_list = balances_sub.add_parser("list", help="real balance for each business account")
    balances_list.set_defaults(func=cmd_balances_list)
    balances_set = balances_sub.add_parser("set", help="record a posted balance anchor")
    balances_set.add_argument("account", help="id, last4, or name")
    balances_set.add_argument("--balance", required=True, help="dollars; liability is the amount owed")
    balances_set.add_argument("--as-of", dest="as_of", required=True, help="YYYY-MM-DD")
    balances_set.add_argument("--source", default="statement", choices=("finance", "statement"))
    balances_set.add_argument("--note", default="")
    balances_set.set_defaults(func=cmd_balances_set)
    balances_history = balances_sub.add_parser("history", help="anchors recorded for one account")
    balances_history.add_argument("account", help="id, last4, or name")
    balances_history.set_defaults(func=cmd_balances_history)

    vendors = sub.add_parser("vendors", help="merge vendor spellings for reports")
    vendors_sub = vendors.add_subparsers(dest="vendors_cmd", required=True)
    vendors_merge = vendors_sub.add_parser("merge", help="map spellings onto one display name")
    vendors_merge.add_argument("names", nargs="+")
    vendors_merge.add_argument("--into", required=True)
    vendors_merge.set_defaults(func=cmd_vendors_merge)
    vendors_unmerge = vendors_sub.add_parser("unmerge", help="remove one spelling from a merge")
    vendors_unmerge.add_argument("alias")
    vendors_unmerge.set_defaults(func=cmd_vendors_unmerge)
    vendors_rename = vendors_sub.add_parser("rename", help="rename a canonical vendor")
    vendors_rename.add_argument("old")
    vendors_rename.add_argument("new")
    vendors_rename.set_defaults(func=cmd_vendors_rename)
    vendors_aliases = vendors_sub.add_parser("aliases", help="list vendor spellings")
    vendors_aliases.set_defaults(func=cmd_vendors_aliases)

    audit = sub.add_parser("audit", help="recent audit log")
    audit.add_argument("--limit", type=int, default=200)
    audit.add_argument("--mode", default="business", choices=("business", "personal", "all"))
    audit.set_defaults(func=cmd_audit)

    seedcheck = sub.add_parser("seedcheck", help="list rule-vs-seed disagreements")
    seedcheck.set_defaults(func=cmd_seedcheck)

    web = sub.add_parser("web", help="run the local web UI on 127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HOST",
        help="extra Host to accept (exact, with or without port; repeatable; saved mode 600)",
    )
    web.set_defaults(func=cmd_web)

    passphrase = sub.add_parser("web-passphrase", help="set, check, or clear the tailnet sign-in passphrase")
    passphrase_sub = passphrase.add_subparsers(dest="passphrase_cmd", required=True)
    passphrase_set = passphrase_sub.add_parser("set", help="store a salted hash; prompts, or reads HPGBOOKS_NEW_PASSPHRASE")
    passphrase_set.set_defaults(func=cmd_web_passphrase_set)
    passphrase_status = passphrase_sub.add_parser("status", help="report whether a web passphrase is set")
    passphrase_status.set_defaults(func=cmd_web_passphrase_status)
    passphrase_clear = passphrase_sub.add_parser("clear", help="remove the web passphrase")
    passphrase_clear.set_defaults(func=cmd_web_passphrase_clear)

    brands = ", ".join(get_config().whmcs_brand_names()) or "a configured brand"
    whmcs = sub.add_parser("whmcs", help="WHMCS billing import and reports" + ("" if get_config().whmcs_enabled else " (disabled)"))
    whmcs_sub = whmcs.add_subparsers(dest="whmcs_cmd", required=True)
    whmcs_sync = whmcs_sub.add_parser("sync", help="full refresh from the WHMCS databases (opens the SSH tunnels itself)")
    whmcs_sync.add_argument("--brand", action="append", default=[], help=f"{brands} (repeatable; default all)")
    whmcs_sync.add_argument("--no-tunnel", action="store_true", help="use tunnels that are already open")
    whmcs_sync.set_defaults(func=cmd_whmcs_sync)
    whmcs_status = whmcs_sub.add_parser("status", help="last sync per brand and recent sync log")
    whmcs_status.add_argument("--limit", type=int, default=10)
    whmcs_status.set_defaults(func=cmd_whmcs_status)
    for name, help_text in (
        ("revenue", "payments, fees, refunds, and net by period, brand, and plan"),
        ("mrr", "current MRR and ARR by brand and plan, and the estimated trend"),
        ("churn", "cancellations, churned services and customers, logo and revenue churn"),
        ("refunds", "refunds by month, brand, and plan, and the largest refunds"),
        ("dunning", "open and overdue invoices, aging, and the collections list"),
        ("reconcile", "match WHMCS PayPal payments to the PayPal account in the books"),
    ):
        report = whmcs_sub.add_parser(name, help=help_text)
        report.add_argument("--brand", default="all", help=f"{brands}, or all")
        report.add_argument("--from", dest="date_from", help="YYYY-MM-DD inclusive")
        report.add_argument("--to", dest="date_to", help="YYYY-MM-DD inclusive")
        report.add_argument("--format", default="table", choices=("table", "csv"))
        report.add_argument("--limit", type=int, default=40, help="rows shown in table output")
        if name == "revenue":
            report.add_argument("--by", default="month", choices=("month", "quarter", "year"))
            report.add_argument("--plans", action="store_true", help="show the per-plan table")
        if name == "mrr":
            report.add_argument("--months", type=int, default=36, help="trend length; 0 for all history")
            report.add_argument("--trend", action="store_true", help="show the month-end trend")
        if name == "churn":
            report.add_argument("--plans", action="store_true", help="show churn by plan")
        if name == "refunds":
            report.add_argument("--largest", action="store_true", help="show the largest refunds")
        if name == "dunning":
            report.add_argument("--aging", action="store_true", help="show aging buckets only")
        if name == "reconcile":
            report.add_argument("--window", type=int, default=3, help="days either side for a match")
            report.add_argument("--rows", action="store_true", help="list each row instead of monthly totals")
            report.add_argument("--gateways", action="store_true", help="show totals per gateway")
        report.set_defaults(func=cmd_whmcs_report, report=name)

    margins = sub.add_parser("margins", help="server costs, the services they carry, and the margin" + ("" if get_config().margins_enabled else " (disabled)"))
    margins_sub = margins.add_subparsers(dest="margins_cmd", required=True)
    margins_seed = margins_sub.add_parser("seed", help="load servers, costs, mappings, and overhead from a JSON file (idempotent)")
    margins_seed.add_argument("--file", default=None, help="seed JSON (default data/margins/seed.json next to the database)")
    margins_seed.set_defaults(func=cmd_margins_seed)
    margins_show = margins_sub.add_parser("show", help="headline numbers, per-server margin, and brand rollup")
    margins_show.add_argument("--json", action="store_true", help="the whole report as JSON (customers by brand and client number)")
    margins_show.set_defaults(func=cmd_margins_show)
    for name, help_text in (
        ("servers", "each server's cost components and mapping rules"),
        ("overhead", "overhead lines spread across all revenue"),
        ("unmapped", "active revenue with no server, grouped and largest items"),
    ):
        margins_sub.add_parser(name, help=help_text).set_defaults(func=cmd_margins_list, what=name)
    margins_whatif = margins_sub.add_parser("whatif", help="merge or retire servers and show the effect (writes nothing)")
    margins_whatif.add_argument("--merge", nargs=2, metavar=("A", "B"), help="move server A's services onto server B (slug or id)")
    margins_whatif.add_argument("--retire", action="append", default=[], metavar="X", help="drop server X with its revenue (repeatable)")
    margins_whatif.set_defaults(func=cmd_margins_whatif)

    from hpgbooks.personal.cli import add_parser as add_personal_parser

    add_personal_parser(sub)

    from hpgbooks.update import add_parser as add_update_parser

    add_update_parser(sub)

    capone = sub.add_parser("import-capitalone", help="import a Capital One CSV export")
    capone.add_argument("csv")
    capone.add_argument("--account", default=None, help="account id (default: the config account with role capitalone_default)")
    capone.add_argument("--dry-run", action="store_true")
    capone.set_defaults(func=cmd_import_capitalone)

    verify = sub.add_parser("verify-capitalone", help="compare a Capital One CSV to statement PDFs")
    verify.add_argument("--csv", required=True)
    verify.add_argument("--statements", nargs="+", required=True)
    verify.add_argument("--account", default=None)
    verify.set_defaults(func=cmd_verify_capitalone)
    return parser


def _date(value: str | None, label: str) -> str | None:
    import re

    if not value:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HpgbooksError(f"{label} must be YYYY-MM-DD")
    return value


def cmd_init(_args) -> int:
    print(init_db())
    return 0


def cmd_seed_rules(_args) -> int:
    from hpgbooks.db import _apply_migrations, seed_accounts
    from hpgbooks.seed_rules import seed_rules

    with connect() as conn:
        _apply_migrations(conn)
        seed_accounts(conn)
        added, present = seed_rules(conn)
    print(f"rules added {added}, already present {present}")
    return 0


def cmd_import(args) -> int:
    from hpgbooks.importer import import_paths

    date_from = _date(args.date_from, "--from")
    date_to = _date(args.date_to, "--to")
    with connect() as conn:
        summaries = import_paths(conn, args.paths, date_from, date_to)
    for stats in summaries:
        print(
            f"{stats['file']}: inserted={stats['inserted']} updated={stats['updated']} "
            f"unchanged={stats['unchanged']} superseded={stats['superseded']} skipped={stats['skipped']}"
        )
        if stats["by_scope"].get("personal") or stats["by_scope"].get("excluded"):
            parts = " ".join(f"{scope}={count}" for scope, count in sorted(stats["by_scope"].items()))
            print(f"  rows by scope: {parts}")
        if stats["unknown_accounts"]:
            print(
                f"warning: skipped {stats['unknown_accounts']} rows with unknown account_id",
                file=sys.stderr,
            )
            for account_id in stats.get("unknown_account_ids", [])[:20]:
                print(
                    f"  unregistered account {account_id[:12]}: run `hpgbooks accounts discover` or skip it",
                    file=sys.stderr,
                )
    return 0


def cmd_reclassify(_args) -> int:
    from hpgbooks.classify import reclassify

    with connect() as conn:
        changed = reclassify(conn)
    print(f"reclassified {changed} rows")
    return 0


def cmd_classify(args) -> int:
    from hpgbooks.classify import classify_manual

    with connect() as conn:
        txn, applied = classify_manual(
            conn,
            args.txn,
            args.tag,
            args.category,
            args.note,
            make_rule=args.rule,
            pattern=args.pattern,
            actor="cli",
        )
        stored = conn.execute(
            "SELECT business_tag, category FROM classifications WHERE txn_id = ?",
            (txn["id"],),
        ).fetchone()
        tag = stored["business_tag"]
        category = stored["category"]
        txn_id = txn["id"]
    print(f"classified {txn_id} as {tag} / {category}")
    if args.rule:
        print(f"rule applied to {applied} other transactions")
    return 0


def cmd_review(args) -> int:
    from hpgbooks.reports import query_review, render_review

    with connect() as conn:
        rows = query_review(conn, args.year)
    print(render_review(rows, args.format), end="" if args.format == "csv" else "\n")
    print(f"{len(rows)} needs_review", file=sys.stderr)
    return 0


def cmd_pnl(args) -> int:
    from hpgbooks.reports import build_pnl, write_pnl

    with connect() as conn:
        report = build_pnl(conn, args.year, by=args.by, business=args.business, ytd=args.ytd)
    path = Path(args.out) if args.out else None
    if path is None and args.format in ("xlsx", "pdf"):
        path = Path("exports") / f"pnl-{args.year}-{args.by}-{args.business}.{args.format}"
    result = write_pnl(report, args.format, path)
    if args.format in ("xlsx", "pdf"):
        print(result)
    else:
        print(result, end="" if result.endswith("\n") else "\n")
    return 0


def cmd_reconcile(args) -> int:
    from hpgbooks.reports import reconcile, render_reconcile

    with connect() as conn:
        result = reconcile(conn, args.year)
    print(render_reconcile(result))
    return 0 if result.ok else 1


def cmd_txns(args) -> int:
    from hpgbooks.reports import query_transactions, render_txns, resolve_account_arg

    with connect() as conn:
        account_id = resolve_account_arg(conn, args.account)
        rows, total = query_transactions(
            conn,
            month=args.month,
            account_id=account_id,
            tag=args.tag,
            search=args.search,
            limit=args.limit,
        )
    print(render_txns(rows))
    print(f"{len(rows)} shown of {total}", file=sys.stderr)
    return 0


def cmd_rules_list(_args) -> int:
    from hpgbooks.reports import query_rules, render_rules

    with connect() as conn:
        rows = query_rules(conn)
    print(render_rules(rows))
    return 0


def cmd_rules_add(args) -> int:
    from hpgbooks.classify import add_rule
    from hpgbooks.reports import resolve_account_arg

    with connect() as conn:
        account_id = resolve_account_arg(conn, args.account)
        rule_id = add_rule(
            conn,
            pattern=args.pattern,
            tag=args.tag,
            category=args.category,
            confidence=args.confidence,
            note=args.note,
            field=args.field,
            account_id=account_id,
            amount_sign=args.sign,
            min_amount=args.min_amount,
            max_amount=args.max_amount,
            priority=args.priority,
            actor="cli",
        )
    print(f"added rule {rule_id}")
    return 0


def cmd_rules_disable(args) -> int:
    from hpgbooks.classify import disable_rule

    with connect() as conn:
        disable_rule(conn, args.rule_id, actor="cli")
    print(f"disabled rule {args.rule_id}")
    return 0


def cmd_transfers(_args) -> int:
    from hpgbooks.reports import render_transfers, transfer_rows

    with connect() as conn:
        rows = transfer_rows(conn)
    print(render_transfers(rows))
    return 0


def cmd_accounts(_args) -> int:
    from hpgbooks.reports import query_accounts, render_accounts

    with connect() as conn:
        rows = query_accounts(conn)
    print(render_accounts(rows))
    return 0


def _account_table(rows: list[dict]) -> str:
    from hpgbooks.reports import render_table

    body = [
        [
            row["id"][:8],
            row["last4"] or "",
            row["label"][:40],
            row["class"],
            row["scope"],
            "yes" if row["include_in_net_worth"] else "no",
            "yes" if row["sync_enabled"] else "no",
            str(row.get("transaction_count", "")),
        ]
        for row in rows
    ]
    return render_table(["id", "last4", "name", "class", "scope", "net worth", "sync", "txns"], body, right_from=7)


def cmd_accounts_set_payment(args) -> int:
    from hpgbooks import payments as pay
    from hpgbooks.scope import label, resolve_account

    fields: dict = {}
    if args.min_payment is not None:
        fields["min_payment_cents"] = pay.cents_arg(args.min_payment, "minimum payment")
    if args.due is not None:
        fields["due_date"] = args.due
    for name in ("apr", "autopay", "notes", "payer_pattern"):
        if getattr(args, name) is not None:
            fields[name] = getattr(args, name)
    if args.unverified:
        fields["unverified"] = True
    if not fields and args.paid is None:
        raise HpgbooksError("nothing to set: give --min, --due, --apr, --autopay, --notes, or --paid")
    with connect() as conn:
        acct = resolve_account(conn, args.account, scopes=("business", "personal"))
        if fields:
            result = pay.set_manual(conn, acct["id"], fields, as_of=args.as_of, source=args.source, actor="cli")
            print(f"{label(acct)}: {result['action']} ({', '.join(result['changed'] + result['filled']) or 'no change'})")
            for line in result["conflicts"]:
                print(f"  conflict: {line}")
        if args.paid is not None:
            done = pay.mark_paid(conn, acct["id"], None if args.paid == "today" else args.paid, actor="cli")
            print(f"{label(acct)}: marked paid on {done['paid_on']}")
    return 0


def cmd_accounts_payments(args) -> int:
    import json

    from hpgbooks import payments as pay
    from hpgbooks.db import format_money
    from hpgbooks.reports import render_table

    with connect(readonly=True) as conn:
        data = pay.build(conn, args.mode)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    body = []
    for r in data["rows"]:
        due = r["effective_due_date"] or ""
        if r["rolled"]:
            due += " (est., paid)"
        elif r["estimated"]:
            due += " (est.)"
        body.append(
            [
                r["label"][:34], r["last4"], format_money(r["balance_cents"]),
                "" if r["min_payment_cents"] is None else format_money(r["min_payment_cents"]),
                due, r["status"] + (" (unverified)" if r["unverified"] else ""), (r["apr"] or "")[:24], r["autopay"] if r["autopay"] != "unknown" else "",
                f"{r['source'] or ''} {r['as_of'] or ''}".strip(),
            ]
        )
    print(render_table(["account", "last4", "owed", "min due", "due", "status", "apr", "autopay", "source / as of"], body))
    s = data["summary"]
    print(f"\nNext 30 days: {format_money(s['next30_cents'])} in {s['next30_count']} minimums"
          + (f"; overdue {format_money(s['overdue_cents'])} ({s['overdue_count']})" if s["overdue_count"] else ""))
    for item in s["by_date"]:
        print(f"  {item['date']}  {format_money(item['cents']):>12}  ({item['count']})")
    return 0


def cmd_accounts_infer_payments(args) -> int:
    from hpgbooks import payments as pay
    from hpgbooks.db import format_money

    with connect() as conn:
        rows = pay.infer_loans(conn, dry_run=args.dry_run, actor="cli")
    for r in rows:
        sched = r["schedule"]
        if sched is None:
            print(f"{r['label']}: no usable payment history ({r['payments_seen']} payments seen)")
            continue
        print(f"{r['label']}: about {format_money(sched['min_payment_cents'])} next due {sched['due_date']} (estimated from {sched['count']} payments, last {sched['last_paid']}) -> {r['action']}")
        for line in r.get("conflicts", []):
            print(f"  conflict: {line}")
    return 0


def cmd_accounts_list(args) -> int:
    import json

    from hpgbooks.accounts_admin import list_accounts

    with connect() as conn:
        rows = list_accounts(conn, args.scope)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(_account_table(rows))
    return 0


def cmd_accounts_set_scope(args) -> int:
    from hpgbooks.accounts_admin import set_scope
    from hpgbooks.scope import label, resolve_account

    with connect() as conn:
        acct = resolve_account(conn, args.account)
        result = set_scope(conn, acct["id"], args.scope, actor="cli")
    if result["changed"]:
        print(
            f"{label(acct)}: {result['old_scope']} -> {result['scope']} "
            f"({result['transactions']} transactions move; ledger rows unchanged)"
        )
    else:
        print(f"{label(acct)} is already {result['scope']}")
    return 0


def cmd_accounts_set_class(args) -> int:
    from hpgbooks.accounts_admin import set_class
    from hpgbooks.scope import resolve_account

    with connect() as conn:
        acct = resolve_account(conn, args.account)
        row = set_class(conn, acct["id"], args.account_class, actor="cli")
    print(f"{row['label']}: class {row['class']} ({row['type']})")
    return 0


def cmd_accounts_rename(args) -> int:
    from hpgbooks.accounts_admin import rename
    from hpgbooks.scope import resolve_account

    with connect() as conn:
        acct = resolve_account(conn, args.account)
        row = rename(conn, acct["id"], args.display_name, actor="cli")
    print(f"{row['id'][:8]}: display name {row['display_name'] or '(cleared)'}")
    return 0


def cmd_accounts_discover(args) -> int:
    from hpgbooks.accounts_admin import discover
    from hpgbooks.db import format_money
    from hpgbooks.reports import render_table

    text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    with connect() as conn:
        rows = discover(conn, text, scope=args.scope, dry_run=args.dry_run, as_of=args.as_of, actor="cli")
        if not args.dry_run:
            from datetime import date

            from hpgbooks import payments as pay
            from hpgbooks.accounts_admin import _parse_payload

            for done in pay.ingest_finance_items(conn, _parse_payload(text), as_of=args.as_of or date.today().isoformat()):
                print(f"payment terms from the feed: {done['id'][:8]} {done['action']}")
    body = [
        [
            row["status"],
            row["last4"] or "",
            (row["name"] or "")[:40],
            (row["institution"] or "")[:24],
            row["class"],
            row["scope"],
            "" if row["balance_cents"] is None else format_money(row["balance_cents"]),
            "yes" if row["anchored"] else "no",
        ]
        for row in rows
    ]
    print(render_table(["status", "last4", "name", "institution", "class", "scope", "balance", "anchor"], body, right_from=6))
    new = sum(1 for row in rows if row["status"] == "new")
    prefix = "dry run: would register" if args.dry_run else "registered"
    print(f"{prefix} {new} new, {len(rows) - new} already known")
    return 0


def cmd_accounts_sync_list(args) -> int:
    import json

    from hpgbooks.accounts_admin import sync_list
    from hpgbooks.reports import render_table

    with connect() as conn:
        rows = sync_list(conn, args.scope)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    body = [[row["scope"], row["last4"] or "", row["label"][:40], row["tool"], row["file_prefix"], row["id"]] for row in rows]
    print(render_table(["scope", "last4", "account", "tool", "inbox file prefix", "id"], body, right_from=6))
    return 0


def _print_balance(snap: dict) -> None:
    from hpgbooks.db import format_money, short_account

    name = short_account(snap["name"])
    if not snap["anchored"]:
        print(
            f"{name}: imported activity {format_money(snap['display_cents'])} "
            f"({snap['type']}; no statement balance)"
        )
        return
    line = (
        f"{name}: {format_money(snap['real_balance_cents'])} as of {snap['as_of_date']} "
        f"({snap['source']}), opening {format_money(snap['opening_cents'])}"
    )
    if snap["drift_cents"] is None or snap["drift_cents"] == 0:
        line += f", reconciles to {format_money(snap['reconcile_cents'])}"
    else:
        line += (
            f", reconciliation difference {format_money(snap['drift_cents'])} "
            f"(statement {format_money(snap['anchor_cents'])}, "
            f"computed {format_money(snap['computed_cents'])})"
        )
    print(line)


def cmd_balances_list(_args) -> int:
    from hpgbooks.balances import account_snapshots

    with connect() as conn:
        snaps = account_snapshots(conn)
    for snap in snaps:
        _print_balance(snap)
    return 0


def cmd_balances_set(args) -> int:
    from hpgbooks.balances import parse_dollars, set_anchor
    from hpgbooks.reports import resolve_account_arg

    cents = parse_dollars(args.balance)
    with connect() as conn:
        account_id = resolve_account_arg(conn, args.account, ("business", "personal"))
        if not account_id:
            raise HpgbooksError("account is required")
        snap = set_anchor(conn, account_id, cents, args.as_of, args.source, args.note or "", actor="cli")
    _print_balance(snap)
    return 0


def cmd_balances_history(args) -> int:
    from hpgbooks.balances import anchor_history
    from hpgbooks.db import format_money
    from hpgbooks.reports import render_table, resolve_account_arg

    with connect() as conn:
        account_id = resolve_account_arg(conn, args.account, ("business", "personal"))
        if not account_id:
            raise HpgbooksError("account is required")
        rows = anchor_history(conn, account_id)
    if not rows:
        print("no balance anchors")
        return 0
    body = [
        [
            row["as_of_date"],
            format_money(int(row["balance_cents"])),
            row["source"],
            (row["note"] or "")[:48],
            (row["created_at"] or "")[:19],
        ]
        for row in rows
    ]
    print(render_table(["as of", "balance", "source", "note", "created"], body, right_from=1))
    return 0


def cmd_vendors_merge(args) -> int:
    from hpgbooks.vendors import merge_vendors

    with connect() as conn:
        result = merge_vendors(conn, args.names, args.into, actor="cli")
    print(f"merged {len(args.names)} names into {result['canonical']} ({result['changed']} updated)")
    return 0


def cmd_vendors_unmerge(args) -> int:
    from hpgbooks.vendors import unmerge_vendor

    with connect() as conn:
        result = unmerge_vendor(conn, args.alias, actor="cli")
    print(f"unmerged {result['alias']} from {result['canonical']}")
    return 0


def cmd_vendors_rename(args) -> int:
    from hpgbooks.vendors import rename_canonical

    with connect() as conn:
        result = rename_canonical(conn, args.old, args.new, actor="cli")
    print(f"renamed {args.old} to {result['canonical']}")
    return 0


def cmd_vendors_aliases(_args) -> int:
    from hpgbooks.reports import render_table
    from hpgbooks.vendors import list_aliases

    with connect() as conn:
        rows = list_aliases(conn)
    if not rows:
        print("no vendor aliases")
        return 0
    body = [
        [row["alias_key"], row["canonical_name"], row["created_by"], (row["created_at"] or "")[:19]]
        for row in rows
    ]
    print(render_table(["alias", "canonical", "by", "created"], body, right_from=3))
    return 0


def cmd_audit(args) -> int:
    from hpgbooks.reports import query_audit, render_audit

    with connect() as conn:
        rows = query_audit(conn, args.limit, args.mode)
    print(render_audit(rows))
    return 0


def cmd_seedcheck(_args) -> int:
    from hpgbooks.classify import seed_disagreements
    from hpgbooks.reports import render_table

    with connect() as conn:
        checked, rows = seed_disagreements(conn)
    print(f"checked {checked} transactions from the prior analysis; {len(rows)} disagreements")
    if not rows:
        return 0
    body = [
        [
            short_id(row["id"]),
            row["date"],
            row["seed_category"],
            row["rule_category"],
            row["rule_tag"],
            str(row["rule_id"]),
            (row["name"] or "")[:48],
        ]
        for row in rows
    ]
    print(
        render_table(
            ["id", "date", "seed", "rule", "tag", "rule_id", "name"],
            body,
            right_from=5,
        )
    )
    return 0


def cmd_import_capitalone(args) -> int:
    from hpgbooks.capitalone import default_account, format_import_report, import_capitalone_csv

    account = args.account or default_account()
    with connect() as conn:
        stats = import_capitalone_csv(conn, Path(args.csv), account, dry_run=args.dry_run)
    print(format_import_report(stats))
    return 0


def cmd_verify_capitalone(args) -> int:
    from hpgbooks.capitalone import default_account, verify_files

    account = args.account or default_account()
    _reports, text = verify_files(Path(args.csv), [Path(path) for path in args.statements], account)
    print(text)
    return 0 if all(report["status"] == "OK" for report in _reports) else 1


def cmd_web(args) -> int:
    from hpgbooks.access import add_allowed_hosts
    from hpgbooks.web import create_app

    if args.port < 1 or args.port > 65535:
        raise HpgbooksError("port is out of range")
    if args.allow_host:
        add_allowed_hosts(args.allow_host)
    app = create_app()
    # Bind stays on loopback. Tailscale serve forwards tailnet traffic here.
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
    return 0


def cmd_web_passphrase_set(_args) -> int:
    from hpgbooks.access import read_new_passphrase, store_passphrase

    phrase = read_new_passphrase()
    try:
        with connect() as conn:
            store_passphrase(conn, phrase)
    finally:
        phrase = ""
    print("web passphrase set")
    return 0


def cmd_web_passphrase_status(_args) -> int:
    from hpgbooks.access import passphrase_is_set

    with connect(readonly=True) as conn:
        # Older databases have not created the table yet. A read cannot migrate.
        exists = passphrase_is_set(conn)
    print("web passphrase: set" if exists else "web passphrase: not set")
    return 0


def cmd_web_passphrase_clear(_args) -> int:
    from hpgbooks.access import clear_passphrase

    with connect() as conn:
        removed = clear_passphrase(conn)
    print("web passphrase cleared" if removed else "web passphrase: not set")
    return 0


def cmd_whmcs_sync(args) -> int:
    from hpgbooks.whmcs import run_sync

    with connect() as conn:
        results = run_sync(conn, args.brand or None, manage_tunnels=not args.no_tunnel)
    for item in results:
        if item["status"] == "ok":
            counts = item["counts"]
            skipped = counts.get("placeholder_credit") or {}
            note = f" skipped_placeholder_credit={sum(skipped.values())}" if skipped else ""
            print(
                f"{item['brand']}: ok clients={counts.get('clients', 0)} services={counts.get('services', 0)} "
                f"invoices={counts.get('invoices', 0)} payments={counts.get('payments', 0)} "
                f"cancellations={counts.get('cancel_requests', 0)}{note} (tunnel {counts.get('tunnel', '?')})"
            )
        else:
            print(f"{item['brand']}: error {item['error']}", file=sys.stderr)
    return 0 if all(item["status"] == "ok" for item in results) else 1


def cmd_whmcs_status(args) -> int:
    from hpgbooks.db import format_money
    from hpgbooks.reports import render_table
    from hpgbooks.whmcs import sync_status

    with connect(readonly=True) as conn:
        status = sync_status(conn, log_limit=max(1, min(args.limit, 200)))
    if not status["ready"]:
        print("WHMCS has not been synced yet. Run: hpgbooks whmcs sync")
        return 0
    body = [
        [
            item["brand"],
            (item["last_ok"] or "never")[:19],
            item["last_status"] or "",
            f"{item['rows']['clients']:,}",
            f"{item['rows']['services']:,}",
            f"{item['rows']['invoices']:,}",
            f"{item['rows']['payments']:,}",
            format_money(item["payments_in_cents"]),
        ]
        for item in status["brands"]
    ]
    print(render_table(["brand", "last ok sync", "last run", "clients", "services", "invoices", "payments", "payments in"], body, right_from=3))
    print()
    log = [
        [
            (row["finished_at"] or row["started_at"])[:19],
            row["brand"],
            row["status"],
            (row["error"] or "")[:70],
            str(row["counts"].get("payments", "")),
        ]
        for row in status["log"]
    ]
    if log:
        print(render_table(["finished", "brand", "status", "error", "payments"], log, right_from=4))
    return 0


def cmd_whmcs_report(args) -> int:
    from hpgbooks import whmcs_reports as wr

    date_from = _date(args.date_from, "--from")
    date_to = _date(args.date_to, "--to")
    name = args.report
    with connect(readonly=True) as conn:
        if name == "revenue":
            data = wr.revenue(conn, start=date_from, end=date_to, brand=args.brand, by=args.by)
            table = "revenue-plans" if args.plans else "revenue"
        elif name == "mrr":
            data = wr.mrr(conn, brand=args.brand, months=args.months)
            table = "mrr-trend" if args.trend else "mrr"
        elif name == "churn":
            data = wr.churn(conn, start=date_from, end=date_to, brand=args.brand)
            table = "churn-plans" if args.plans else "churn"
        elif name == "refunds":
            data = wr.refunds(conn, start=date_from, end=date_to, brand=args.brand)
            table = "refunds-largest" if args.largest else "refunds"
        elif name == "dunning":
            data = wr.dunning(conn, start=date_from, end=date_to, brand=args.brand)
            table = "dunning-aging" if args.aging else "dunning"
        else:
            if args.brand not in ("", "all"):
                raise HpgbooksError("reconcile covers every brand; leave out --brand")
            data = wr.reconcile_paypal(conn, start=date_from, end=date_to, window=args.window)
            table = "reconcile-rows" if args.rows else "gateways" if args.gateways else "reconcile"
    if args.format == "csv":
        print(wr.table_csv(table, data), end="")
        return 0
    print(wr.table_text(table, data, limit=None if args.limit <= 0 else args.limit))
    print(_whmcs_footer(name, data), file=sys.stderr)
    return 0


def _whmcs_footer(name: str, data: dict) -> str:
    from hpgbooks.db import format_money

    if name == "revenue":
        totals = data["totals"]
        return (
            f"{data['start']} to {data['end']}: gross {format_money(totals['gross_cents'])}, fees {format_money(totals['fees_cents'])}, "
            f"refunds {format_money(totals['refunds_cents'])}, net {format_money(totals['net_cents'])}"
        )
    if name == "mrr":
        return (
            f"MRR {format_money(data['mrr_cents'])}, ARR {format_money(data['arr_cents'])}, {data['customers']} active customers, "
            f"ARPU {format_money(data['arpu_cents'])}. Trend: {data['trend_note']}"
        )
    if name == "churn":
        totals = data["totals"]
        return (
            f"{data['start']} to {data['end']}: {totals['cancel_requests']} cancellation requests, {totals['churned_services']} services and "
            f"{totals['churned_customers']} customers churned, {totals['new_services']} new services. {data['note']}"
        )
    if name == "refunds":
        totals = data["totals"]
        rate = "n/a" if totals["rate_pct"] is None else f"{totals['rate_pct']:.2f}%"
        return f"{totals['count']} refunds, {format_money(totals['refunds_cents'])} ({rate} of gross)"
    if name == "dunning":
        totals = data["totals"]
        return (
            f"{totals['open_count']} open invoices {format_money(totals['open_cents'])}; overdue {totals['overdue_count']} "
            f"{format_money(totals['overdue_cents'])}; collectible overdue {format_money(totals['collectible_overdue_cents'])}"
        )
    totals = data["totals"]
    return (
        f"{data['start']} to {data['end']} (books cover {data['coverage']['books_first']} to {data['coverage']['books_last']}): "
        f"matched {totals.get('matched', 0)}, WHMCS only {totals.get('whmcs_only', 0)}, PayPal only {totals.get('paypal_only', 0)}, "
        f"difference {format_money(totals.get('difference_cents', 0))}"
    )


# --- server margins -----------------------------------------------------------------------------


def _pct_text(value) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def _margins_report(conn) -> dict:
    from hpgbooks import margins as mg

    report = mg.build(conn, names=False)
    if not report["ready"]:
        raise HpgbooksError("WHMCS has not been synced yet; run: hpgbooks whmcs sync")
    return report


def cmd_margins_seed(args) -> int:
    from hpgbooks import margins as mg

    with connect() as conn:
        counts = mg.seed(conn, args.file)
    if not counts:
        print("margins seed: no changes")
    else:
        print("margins seed: " + ", ".join(f"{key} {value}" for key, value in sorted(counts.items())))
    return 0


def cmd_margins_show(args) -> int:
    import json

    from hpgbooks.db import format_money
    from hpgbooks.reports import render_table

    with connect(readonly=True) as conn:
        report = _margins_report(conn)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    t = report["totals"]
    lines = [
        ["Active MRR (hosting " + format_money(t["hosting_mrr_cents"]) + " + addons " + format_money(t["addon_mrr_cents"]) + ")", format_money(t["mrr_cents"]), "100.0%"],
        ["Server-attached cost", format_money(t["server_cost_cents"]), _pct_text(round(t["server_cost_cents"] * 100 / t["mrr_cents"], 1) if t["mrr_cents"] else None)],
        ["Contribution after server cost", format_money(t["contribution_cents"]), _pct_text(t["contribution_pct"])],
        ["Shared overhead", format_money(t["overhead_cents"]), _pct_text(round(t["overhead_cents"] * 100 / t["mrr_cents"], 1) if t["mrr_cents"] else None)],
        ["Total infrastructure cost", format_money(t["infrastructure_cents"]), _pct_text(round(t["infrastructure_cents"] * 100 / t["mrr_cents"], 1) if t["mrr_cents"] else None)],
        ["Blended margin", format_money(t["blended_margin_cents"]), _pct_text(t["blended_margin_pct"])],
    ]
    print(render_table(["item", "$/month", "% of MRR"], lines, right_from=1))
    print()
    rows = [
        [
            row["label"], row["vendor"], row["kind"], row["status"],
            format_money(row["cost_cents"]), f"{row['services']:,}", f"{row['paying_customers']:,}",
            format_money(row["revenue_cents"]), format_money(row["margin_cents"]), _pct_text(row["margin_pct"]),
            " ".join(row["flags"]),
        ]
        for row in report["servers"]
    ]
    rows.append(["Unmapped (no server)", "", "", "", "0.00", f"{t['unmapped_services']:,}", "", format_money(t["unmapped_cents"]), format_money(t["unmapped_cents"]), "", ""])
    rows.append(["Shared overhead", "", "", "", format_money(t["overhead_cents"]), "", "", "", format_money(-t["overhead_cents"]), "", ""])
    rows.append(["Total", "", "", "", format_money(t["infrastructure_cents"]), f"{t['active_services']:,}", "", format_money(t["mrr_cents"]), format_money(t["blended_margin_cents"]), _pct_text(t["blended_margin_pct"]), ""])
    print(render_table(["server", "vendor", "kind", "status", "cost", "services", "paying cust", "revenue", "margin", "margin %", "flags"], rows, right_from=4))
    print()
    brands = [
        [row["brand"], f"{row['services']:,}", format_money(row["revenue_cents"]), format_money(row["cost_cents"]), format_money(row["margin_cents"]), _pct_text(row["margin_pct"])]
        for row in report["brands"]
    ]
    print(render_table(["brand", "services", "revenue", "server cost", "margin", "margin %"], brands, right_from=1))
    if not report["seeded"]:
        print("No servers yet. Run: hpgbooks margins seed", file=sys.stderr)
    if t["mrr_report_cents"] != t["mrr_cents"]:
        print(f"note: the MRR report shows {format_money(t['mrr_report_cents'])}; it leaves out services with no registration date", file=sys.stderr)
    print("Server cost is shared by revenue within a server; brand rows are before overhead. Customers are shown by client number only.", file=sys.stderr)
    return 0


def cmd_margins_list(args) -> int:
    from hpgbooks.db import format_money
    from hpgbooks.reports import render_table

    with connect(readonly=True) as conn:
        report = _margins_report(conn)
    if args.what == "servers":
        rows = []
        for server in report["servers"]:
            rows.append([server["slug"], str(server["id"]), server["label"], server["kind"], server["status"], "TOTAL", format_money(server["cost_cents"])])
            for item in server["components"]:
                rows.append(["", "", "", "", "", "  " + item["component"], format_money(item["monthly_cost_cents"])])
            for rule in server["mappings"]:
                scope = f" [{rule['brand_scope']}]" if rule["brand_scope"] else ""
                rows.append(["", "", "", "", "", f"  maps {rule['rule_type']}={rule['rule_value']}{scope} ({rule['allocation']})", ""])
        print(render_table(["slug", "id", "label", "kind", "status", "component / rule", "$/month"], rows, right_from=6))
    elif args.what == "overhead":
        rows = [[str(line["id"]), line["label"], line["vendor"], line["kind"], format_money(line["monthly_cost_cents"]), line["note"]] for line in report["overhead"]]
        rows.append(["", "Total shared", "", "", format_money(report["totals"]["overhead_cents"]), ""])
        if report["totals"]["overhead_excluded_cents"]:
            rows.append(["", "Not this business (left out)", "", "", format_money(report["totals"]["overhead_excluded_cents"]), ""])
        print(render_table(["id", "label", "vendor", "kind", "$/month", "note"], rows, right_from=4))
    else:
        unmapped = report["unmapped"]
        rows = [[row["brand"], row["group"], row["cycle"], f"{row['services']:,}", f"{row['paying_services']:,}", format_money(row["revenue_cents"])] for row in unmapped["groups"]]
        print(render_table(["brand", "group", "cycle", "services", "paying", "MRR"], rows, right_from=3))
        print()
        top = [
            [row["brand"], row["kind"], str(row["service_id"]), str(row["client_id"] or ""), row["plan"], row["group"], str(row["whmcs_server_id"] or ""), row["reason"], format_money(row["revenue_cents"])]
            for row in unmapped["top"]
        ]
        print(render_table(["brand", "kind", "service", "client", "plan", "group", "whmcs server", "why", "MRR"], top, right_from=8))
        print(f"{report['totals']['unmapped_services']:,} unmapped active services, {format_money(report['totals']['unmapped_cents'])} MRR", file=sys.stderr)
    return 0


def cmd_margins_whatif(args) -> int:
    from hpgbooks import margins as mg
    from hpgbooks.db import format_money
    from hpgbooks.reports import render_table

    if not args.merge and not args.retire:
        raise HpgbooksError("give --merge A B and/or --retire X")
    with connect(readonly=True) as conn:
        report = _margins_report(conn)
    merge = None
    if args.merge:
        merge = (mg.find_server(report, args.merge[0])["id"], mg.find_server(report, args.merge[1])["id"])
    retire = [mg.find_server(report, ref)["id"] for ref in args.retire]
    result = mg.whatif(report, merge=merge, retire=retire)
    rows = [
        [
            row["label"], "removed" if row["removed"] else "",
            format_money(row["cost_before_cents"]), format_money(row["cost_after_cents"]),
            format_money(row["revenue_before_cents"]), format_money(row["revenue_after_cents"]),
            format_money(row["margin_before_cents"]), format_money(row["margin_after_cents"]),
        ]
        for row in result["servers"]
    ]
    print(render_table(["server", "", "cost before", "cost after", "revenue before", "revenue after", "margin before", "margin after"], rows, right_from=2))
    t = report["totals"]
    print()
    print(
        render_table(
            ["", "now", "what-if"],
            [
                ["MRR", format_money(t["mrr_cents"]), format_money(result["mrr_cents"])],
                ["Server cost", format_money(t["server_cost_cents"]), format_money(result["server_cost_cents"])],
                ["Overhead", format_money(t["overhead_cents"]), format_money(result["overhead_cents"])],
                ["Blended margin", format_money(t["blended_margin_cents"]), format_money(result["blended_margin_cents"])],
                ["Blended margin %", _pct_text(t["blended_margin_pct"]), _pct_text(result["blended_margin_pct"])],
            ],
            right_from=1,
        )
    )
    print(f"cost saved {format_money(result['cost_saved_cents'])}/month; blended margin change {format_money(result['blended_change_cents'])}/month. Nothing was written.", file=sys.stderr)
    return 0
