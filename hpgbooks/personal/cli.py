"""`hpgbooks personal ...` commands. Personal data only; business numbers are never printed here."""

from __future__ import annotations

import json
import sys

from hpgbooks.db import HpgbooksError, connect, format_money

FORMATS = ("table", "csv", "json")


def _money(cents) -> str:
    return "" if cents is None else format_money(int(cents))


def _pct(value) -> str:
    return "" if value is None else f"{value:.1f}%"


def _table(headers, rows, right_from=1) -> str:
    from hpgbooks.reports import render_table

    return render_table(headers, rows, right_from=right_from)


def _emit(args, data, text_fn, csv_spec=None) -> int:
    fmt = getattr(args, "format", "table")
    if getattr(args, "json", False) or fmt == "json":
        print(json.dumps(data, indent=2, default=str))
    elif fmt == "csv" and csv_spec is not None:
        from hpgbooks.personal.queries import rows_csv

        columns, rows = csv_spec
        print(rows_csv(columns, rows), end="")
    else:
        print(text_fn(data))
    return 0


def _category(conn, text: str) -> int:
    from hpgbooks.personal.classify import categories

    text = (text or "").strip()
    cats = list(categories(conn).values())
    if "/" in text:
        group, name = [part.strip() for part in text.split("/", 1)]
        hits = [cat for cat in cats if cat["group_name"].lower() == group.lower() and cat["name"].lower() == name.lower()]
    else:
        hits = [cat for cat in cats if cat["name"].lower() == text.lower()]
    if len(hits) == 1:
        return int(hits[0]["id"])
    if not hits:
        raise HpgbooksError(f"no category named {text}")
    raise HpgbooksError(f"{text} matches more than one category; use Group/Name")


def add_parser(sub) -> None:
    personal = sub.add_parser("personal", help="personal finance mode (personal accounts only)")
    psub = personal.add_subparsers(dest="personal_cmd", required=True)

    def cmd(name, func, help_text, fmt=False, json_flag=False):
        parser = psub.add_parser(name, help=help_text)
        parser.set_defaults(func=func)
        if fmt:
            parser.add_argument("--format", default="table", choices=FORMATS)
        if json_flag:
            parser.add_argument("--json", action="store_true")
        return parser

    cmd("seed-rules", cmd_seed_rules, "insert any missing starter categories and rules")
    cmd("reclassify", cmd_reclassify, "re-run personal rules and transfer pairing on non-manual rows")
    cmd("dashboard", cmd_dashboard, "net worth, this month, budgets, bills, review count", json_flag=True)
    p = cmd("net-worth", cmd_net_worth, "assets, liabilities, per-account balances", json_flag=True)
    p.add_argument("--range", default="1Y", choices=("3M", "6M", "1Y", "YTD", "ALL"))
    cmd("accounts", cmd_accounts, "personal accounts by class", json_flag=True)
    p = cmd("transactions", cmd_transactions, "list personal transactions", fmt=True)
    p.add_argument("--month")
    p.add_argument("--search", default="")
    p.add_argument("--account", default="")
    p.add_argument("--category", default="")
    p.add_argument("--review", action="store_true")
    p.add_argument("--limit", type=int, default=100)
    p = cmd("spending", cmd_spending, "spending by group, category, and merchant", fmt=True)
    p.add_argument("--month")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    p = cmd("cash-flow", cmd_cash_flow, "income sources, spending, savings rate by month", fmt=True)
    p.add_argument("--month")
    p.add_argument("--months", type=int, default=12)
    p = cmd("budgets", cmd_budgets, "budget progress for a month", fmt=True)
    p.add_argument("--month")
    p.add_argument("--alerts", action="store_true", help="only budgets at 80%% or more")
    bsub = p.add_subparsers(dest="budgets_cmd")
    b = bsub.add_parser("set", help="set a budget (category or Group/Name, dollars)")
    b.add_argument("category")
    b.add_argument("amount")
    b.add_argument("--month", help="YYYY-MM; omit for the default every month")
    b.add_argument("--group", action="store_true", help="the name is a whole group")
    b.add_argument("--rollover", action="store_true")
    b.set_defaults(func=cmd_budgets_set)
    b = bsub.add_parser("copy", help="copy last month's budgets into --month")
    b.add_argument("--month", required=True)
    b.set_defaults(func=cmd_budgets_copy)
    b = bsub.add_parser("average", help="3-month average spending per category")
    b.add_argument("--month", required=True)
    b.add_argument("--apply", action="store_true", help="set each as the month's budget")
    b.set_defaults(func=cmd_budgets_average)
    p = cmd("recurring", cmd_recurring, "recurring bills, subscriptions, and income", fmt=True)
    rsub = p.add_subparsers(dest="recurring_cmd")
    r = rsub.add_parser("detect", help="run the detector and store its results")
    r.add_argument("--format", default="table", choices=FORMATS)
    r.set_defaults(func=cmd_recurring_detect)
    r = rsub.add_parser("set", help="confirm, ignore, cancel, or change cadence")
    r.add_argument("series_key")
    r.add_argument("--status", choices=("active", "cancelled", "ignored", "confirmed"))
    r.add_argument("--cadence", choices=("weekly", "biweekly", "monthly", "quarterly", "annual"))
    r.set_defaults(func=cmd_recurring_set)
    p = cmd("bills", cmd_bills, "expected bills and income", fmt=True)
    p.add_argument("--days", type=int, default=45)
    cmd("goals", cmd_goals, "savings goals and progress", fmt=True)
    p = cmd("summary", cmd_summary, "monthly summary", fmt=True)
    p.add_argument("--month", required=True)
    p = cmd("transfers", cmd_transfers, "preview transfer pairs, ambiguous pairs, and owner draws", json_flag=True)
    tsub = p.add_subparsers(dest="transfers_cmd")
    for name in ("confirm", "reject"):
        t = tsub.add_parser(name, help=f"{name} an ambiguous pair (key out_id|in_id)")
        t.add_argument("pair_key")
        t.set_defaults(func=cmd_transfers_decide, decision="confirmed" if name == "confirm" else "rejected")
    cmd("review", cmd_review, "uncategorized and low-confidence rows", fmt=True)
    p = cmd("categorize", cmd_categorize, "set a personal category by hand")
    p.add_argument("txn")
    p.add_argument("category", help="Name or Group/Name")
    p.add_argument("--note")
    p = cmd("rules", cmd_rules, "list or add personal rules")
    rsub = p.add_subparsers(dest="rules_cmd")
    r = rsub.add_parser("add")
    r.add_argument("--pattern", required=True)
    r.add_argument("--category", required=True)
    r.add_argument("--sign", choices=("in", "out"))
    r.add_argument("--priority", type=int, default=12)
    r.set_defaults(func=cmd_rules_add)
    r = rsub.add_parser("disable")
    r.add_argument("rule_id", type=int)
    r.set_defaults(func=cmd_rules_disable)
    p = cmd("reconcile", cmd_reconcile, "tie income, draws, spending, and transfers to balance changes", json_flag=True)
    p.add_argument("--month", required=True)
    p = cmd("offers", cmd_offers_list, "saved loan offers for the Debt payoff page", json_flag=True)
    osub = p.add_subparsers(dest="offers_cmd")
    o = osub.add_parser("list", help="list saved offers")
    o.add_argument("--json", action="store_true")
    o.set_defaults(func=cmd_offers_list)
    o = osub.add_parser("add", help="save an offer")
    _offer_flags(o, required=True)
    o.set_defaults(func=cmd_offers_add)
    o = osub.add_parser("update", help="change an offer (only the flags given)")
    o.add_argument("offer_id", type=int)
    _offer_flags(o, required=False)
    o.set_defaults(func=cmd_offers_update)
    o = osub.add_parser("delete", help="delete an offer")
    o.add_argument("offer_id", type=int)
    o.set_defaults(func=cmd_offers_delete)


def _offer_flags(parser, *, required: bool) -> None:
    parser.add_argument("--lender", required=required)
    parser.add_argument("--amount", required=required, help='dollars: 187500, "187,500", or "$187,500.00"')
    parser.add_argument("--apr", required=required, help="percent, e.g. 9.76")
    parser.add_argument("--fee", help="origination fee percent, 0 to 10 (default 0)")
    fee = parser.add_mutually_exclusive_group()
    fee.add_argument("--fee-from-proceeds", dest="fee_from_proceeds", action="store_const", const=True, default=None,
                     help="the fee comes out of the loan proceeds (the default)")
    fee.add_argument("--fee-added", dest="fee_from_proceeds", action="store_const", const=False,
                     help="full proceeds; the fee is paid separately")
    parser.add_argument("--term", required=required, help="months, 1 to 360")
    parser.add_argument("--payment", help="monthly payment as quoted, dollars")
    parser.add_argument("--notes")
    parser.add_argument("--expires", help="YYYY-MM-DD")
    parser.add_argument("--source", choices=("manual", "grok", "import"), default=None if not required else "manual")


def cmd_seed_rules(_args) -> int:
    from hpgbooks.personal.seed import seed_personal

    with connect() as conn:
        added, present = seed_personal(conn)
    print(f"personal rules added {added}, already present {present}")
    return 0


def cmd_reclassify(_args) -> int:
    from hpgbooks.personal.classify import reclassify_personal

    with connect() as conn:
        changed = reclassify_personal(conn)
    print(f"reclassified {changed} personal rows")
    return 0


def cmd_dashboard(args) -> int:
    from hpgbooks.personal.analytics import dashboard

    with connect(readonly=True) as conn:
        data = dashboard(conn)

    def text(d):
        if not d["has_accounts"]:
            return "No personal accounts yet. Run: hpgbooks accounts discover <finance_list_accounts.json>"
        t = d["month_totals"]
        lines = [
            f"Personal dashboard {d['as_of']}",
            f"Net worth            {_money(d['net_worth']['net_cents']):>16}  (30d {_money(d['net_worth']['change_30_cents'])}, 90d {_money(d['net_worth']['change_90_cents'])})",
            f"{d['month']} income       {_money(t['income_cents']):>16}  (earned {_money(t['earned_cents'])}, owner draws {_money(t['owner_draws_cents'])})",
            f"{d['month']} spending     {_money(t['spending_cents']):>16}",
            f"Savings rate         {_pct(t['savings_rate']):>16}  (without draws {_pct(t['savings_rate_without_draws'])})",
            f"Subscriptions/month  {_money(d['subscription_monthly_cents']):>16}",
            f"Needs review         {d['review_count']:>16}",
            f"Budgets              {d['budget']['counts'].get('over', 0)} over, {d['budget']['counts'].get('warning', 0)} near the limit",
        ]
        for bill in d["upcoming_bills"][:8]:
            lines.append(f"  due {bill['date']}  {bill['merchant'][:30]:<30} {_money(bill['amount_cents']):>12}")
        return "\n".join(lines)

    return _emit(args, data, text)


def cmd_net_worth(args) -> int:
    from hpgbooks.personal.analytics import net_worth

    with connect(readonly=True) as conn:
        data = net_worth(conn, args.range)

    def text(d):
        body = [
            [row["label"][:36], row["last4"], row["class"], _money(row["balance_cents"]), _money(row["contribution_cents"]),
             row["last_updated"] or "", "stale" if row["stale"] else "", "" if row["included"] else "excluded"]
            for row in d["accounts"]
        ]
        head = (f"Net worth {_money(d['net_cents'])} as of {d['as_of']} (assets {_money(d['assets_cents'])}, "
                f"liabilities {_money(d['liabilities_cents'])}; 30d {_money(d['change_30_cents'])})")
        return head + "\n\n" + _table(["account", "last4", "class", "balance", "net worth", "updated", "", ""], body, right_from=3)

    return _emit(args, data, text)


def cmd_accounts(args) -> int:
    from hpgbooks.personal.analytics import accounts_overview

    with connect(readonly=True) as conn:
        data = accounts_overview(conn)

    def text(d):
        body = []
        for group in d["groups"]:
            for row in group["accounts"]:
                body.append([group["label"], row["label"][:36], row["last4"], _money(row["balance_cents"]), row["last_updated"] or "", "stale" if row["stale"] else ""])
        return _table(["group", "account", "last4", "balance", "updated", ""], body, right_from=3) if body else "no personal accounts"

    return _emit(args, data, text)


def cmd_transactions(args) -> int:
    from hpgbooks.personal.queries import query
    from hpgbooks.reports import month_end, parse_month
    from hpgbooks.scope import resolve_account

    with connect(readonly=True) as conn:
        start = end = None
        if args.month:
            month = parse_month(args.month)
            start, end = month + "-01", month_end(month)
        account = resolve_account(conn, args.account, scopes=("personal",))["id"] if args.account else ""
        category = _category(conn, args.category) if args.category else None
        data = query(conn, start=start, end=end, search=args.search, account=account, category_id=category, review=args.review, limit=max(1, args.limit))
    from hpgbooks.personal.api import TXN_COLUMNS

    def text(d):
        body = [
            [row["date"], row["account_label"][:24], _money(row["amount_cents"]), row["merchant"][:32], row["category"][:24],
             "business" if row["from_business"] else row["source"], row["id"][:14]]
            for row in d["rows"]
        ]
        return _table(["date", "account", "amount", "merchant", "category", "source", "id"], body, right_from=2) + f"\n{len(d['rows'])} shown of {d['total']}"

    return _emit(args, data, text, (TXN_COLUMNS, data["rows"]))


def cmd_spending(args) -> int:
    from hpgbooks.personal.analytics import month_of, spending, today
    from hpgbooks.reports import month_end, parse_month

    if args.date_from or args.date_to:
        if not (args.date_from and args.date_to):
            raise HpgbooksError("--from and --to go together")
        start, end = args.date_from, args.date_to
    else:
        month = parse_month(args.month) if args.month else month_of(today())
        start, end = month + "-01", month_end(month)
    with connect(readonly=True) as conn:
        data = spending(conn, start, end)

    def text(d):
        body = [[g["group"], _money(g["cents"]), _money(g["prior_cents"]), _money(g["last_year_cents"]), _pct(g["share"])] for g in d["groups"]]
        head = f"Spending {d['start']} to {d['end']}: {_money(d['total']['cents'])} (prior {_money(d['total']['prior_cents'])}, same period last year {_money(d['same_period_last_year_cents'])})"
        merchants = _table(["merchant", "spent", "count", "average"], [[m["merchant"][:32], _money(m["cents"]), str(m["count"]), _money(m["average_cents"])] for m in d["merchants"][:10]])
        return head + "\n\n" + _table(["group", "spent", "prior", "last year", "share"], body) + "\n\nTop merchants\n" + merchants

    columns = [("group", "Group"), ("category", "Category"), ("cents_cents", "Spent"), ("prior_cents", "Prior"), ("last_year_cents", "Last year")]
    return _emit(args, data, text, (columns, [{**c, "cents_cents": c["cents"]} for c in data["categories"]]))


def cmd_cash_flow(args) -> int:
    from hpgbooks.personal.analytics import cash_flow

    with connect(readonly=True) as conn:
        data = cash_flow(conn, args.month, args.months)

    def text(d):
        body = [
            [m["month"], _money(m["sources"]["Paycheck"]), _money(m["sources"]["Interest & dividends"]), _money(m["owner_draws_cents"]),
             _money(m["sources"]["Other"]), _money(m["spending_cents"]), _money(m["net_cents"]), _pct(m["savings_rate"]), _pct(m["savings_rate_without_draws"])]
            for m in d["months"]
        ]
        return _table(["month", "paycheck", "interest", "owner draws", "other", "spending", "net", "savings", "w/o draws"], body)

    columns = [("month", "Month"), ("income_cents", "Income"), ("owner_draws_cents", "Owner draws"), ("spending_cents", "Spending"), ("net_cents", "Net"),
               ("savings_rate", "Savings rate %"), ("savings_rate_without_draws", "Without draws %")]
    return _emit(args, data, text, (columns, data["months"]))


def cmd_budgets(args) -> int:
    from hpgbooks.personal.analytics import budgets

    with connect(readonly=True) as conn:
        data = budgets(conn, args.month)
    rows = [row for row in data["rows"] if row["status"] != "ok"] if args.alerts else data["rows"]

    def text(d):
        if not rows:
            return "no budget alerts" if args.alerts else "no budgets set (hpgbooks personal budgets set <category> <dollars>)"
        body = [[r["group"][:18], r["name"][:24], _money(r["available_cents"]), _money(r["spent_cents"]), _money(r["remaining_cents"]),
                 _pct(r["pct_used"]), _money(r["projected_cents"]), r["status"]] for r in rows]
        head = f"Budgets {d['month']}: {_money(d['spent_cents'])} of {_money(d['budgeted_cents'])}"
        return head + "\n\n" + _table(["group", "budget", "available", "spent", "remaining", "used", "projected", "status"], body, right_from=2)

    columns = [("name", "Budget"), ("available_cents", "Available"), ("spent_cents", "Spent"), ("pct_used", "% used"), ("status", "Status")]
    _emit(args, {**data, "rows": rows}, text, (columns, rows))
    return 0


def cmd_budgets_set(args) -> int:
    from hpgbooks.balances import parse_dollars
    from hpgbooks.personal.actions import set_budget

    cents = parse_dollars(args.amount)
    with connect() as conn:
        body = {"amount_cents": cents, "month": args.month, "rollover": bool(args.rollover)}
        if args.group:
            body["group_name"] = args.category
        else:
            body["category_id"] = _category(conn, args.category)
        set_budget(conn, body, actor="cli")
    print(f"budget {args.category} {args.month or 'every month'}: {_money(cents)}")
    return 0


def cmd_budgets_copy(args) -> int:
    from hpgbooks.personal.actions import copy_budgets

    with connect() as conn:
        count = copy_budgets(conn, args.month, actor="cli")
    print(f"copied {count} budgets into {args.month}")
    return 0


def cmd_budgets_average(args) -> int:
    from hpgbooks.personal.actions import apply_average
    from hpgbooks.personal.analytics import average_suggestions

    with connect() as conn:
        rows = average_suggestions(conn, args.month)
        applied = apply_average(conn, args.month, None, actor="cli") if args.apply else 0
    print(_table(["group", "category", "3-month average"], [[r["group"], r["category"], _money(r["average_cents"])] for r in rows], right_from=2))
    if args.apply:
        print(f"set {applied} budgets for {args.month}")
    return 0


def _recurring_text(d) -> str:
    body = [
        [item["merchant"][:28], item["kind"], item["cadence"], _money(item["typical_cents"]), item["last_date"], item["next_expected"],
         _money(item["monthly_cents"]),
         " ".join(flag for flag, on in (("price-change", item["price_change"]), ("may-be-cancelled", item["may_be_cancelled"]),
                                        ("new", item["new"]), ("duplicate?", item["possible_duplicate"])) if on),
         item["status"], item["tier"]]
        for item in d["items"]
    ]
    head = f"Subscriptions {_money(d['subscription_monthly_cents'])}/month, {_money(d['subscription_annual_cents'])}/year"
    return head + "\n\n" + _table(["merchant", "kind", "cadence", "typical", "last", "next", "monthly", "flags", "status", "tier"], body, right_from=3)


RECURRING_COLUMNS = [("merchant", "Merchant"), ("kind", "Kind"), ("cadence", "Cadence"), ("typical_cents", "Typical"),
                     ("next_expected", "Next"), ("monthly_cents", "Monthly"), ("annual_cents", "Annual"), ("status", "Status"), ("tier", "Tier"), ("series_key", "Key")]


def cmd_recurring(args) -> int:
    from hpgbooks.personal.analytics import recurring

    with connect(readonly=True) as conn:
        data = recurring(conn)
    return _emit(args, data, _recurring_text, (RECURRING_COLUMNS, data["items"]))


def cmd_recurring_detect(args) -> int:
    from hpgbooks.personal.analytics import recurring, refresh_recurring

    with connect() as conn:
        stored = refresh_recurring(conn)
        data = recurring(conn)
    _emit(args, data, _recurring_text, (RECURRING_COLUMNS, data["items"]))
    print(f"stored {stored} recurring series", file=sys.stderr)
    return 0


def cmd_recurring_set(args) -> int:
    from hpgbooks.personal.actions import update_recurring

    body = {key: value for key, value in (("status", args.status), ("cadence", args.cadence)) if value}
    with connect() as conn:
        result = update_recurring(conn, args.series_key, body, actor="cli")
    print(json.dumps(result))
    return 0


def cmd_bills(args) -> int:
    from hpgbooks.personal.analytics import bills

    with connect(readonly=True) as conn:
        data = bills(conn, args.days)

    def text(d):
        body = [[e["date"], e["merchant"][:30], e["kind"], _money(e["amount_cents"]), e["status"]] for e in d["events"]]
        head = f"Due this week {_money(d['due_this_week_cents'])}, this month {_money(d['due_this_month_cents'])}, overdue {d['overdue_count']}"
        return head + "\n\n" + _table(["date", "merchant", "kind", "amount", "status"], body, right_from=3)

    columns = [("date", "Date"), ("merchant", "Merchant"), ("kind", "Kind"), ("amount_cents", "Amount"), ("status", "Status")]
    return _emit(args, data, text, (columns, data["events"]))


def cmd_goals(args) -> int:
    from hpgbooks.personal.analytics import goals

    with connect(readonly=True) as conn:
        rows = goals(conn)

    def text(d):
        if not d:
            return "no goals"
        return _table(["goal", "target", "current", "done", "by", "needed/month", "status"],
                      [[g["name"][:28], _money(g["target_cents"]), _money(g["current_cents"]), _pct(g["progress_pct"]), g["target_date"] or "",
                        _money(g["required_monthly_cents"]), g["status"]] for g in d])

    columns = [("name", "Goal"), ("target_cents", "Target"), ("current_cents", "Current"), ("status", "Status")]
    return _emit(args, rows, text, (columns, rows))


def cmd_summary(args) -> int:
    from hpgbooks.personal.analytics import monthly_summary

    with connect(readonly=True) as conn:
        data = monthly_summary(conn, args.month)

    def text(d):
        lines = [f"Monthly summary {d['month']}", ""]
        lines += [f"- {line}" for line in d["summary"]]
        lines += ["", f"Income {_money(d['income_cents'])}  Spending {_money(d['spending_cents'])}  Net {_money(d['net_cents'])}  "
                      f"Savings {_pct(d['savings_rate'])} (w/o draws {_pct(d['savings_rate_without_draws'])})"]
        return "\n".join(lines)

    columns = [("metric", "Metric"), ("value_cents", "Value")]
    rows = [{"metric": key, "value_cents": data[key]} for key in ("income_cents", "earned_cents", "owner_draws_cents", "spending_cents", "net_cents", "net_worth_change_cents")]
    return _emit(args, data, text, (columns, rows))


def cmd_transfers(args) -> int:
    from hpgbooks.personal.queries import transfers_view

    with connect(readonly=True) as conn:
        data = transfers_view(conn)

    def text(d):
        def leg(item):
            return [item["out"]["date"], _money(item["out"]["amount_cents"]), item["out"]["account_label"][:20],
                    item["in"]["date"], _money(item["in"]["amount_cents"]), item["in"]["account_label"][:20], item["key"]]

        headers = ["out date", "out", "from", "in date", "in", "to", "pair key"]
        parts = [f"Transfer pairs: {len(d['pairs'])} matched, {len(d['ambiguous'])} need confirmation, "
                 f"{len(d['owner_draws'])} owner draws paired, {len(d['owner_draws_unpaired'])} draws without a personal leg"]
        if d["ambiguous"]:
            parts += ["", "Needs confirmation (hpgbooks personal transfers confirm <key>)", _table(headers, [leg(i) for i in d["ambiguous"]])]
        if d["pairs"]:
            parts += ["", "Matched", _table(headers, [leg(i) for i in d["pairs"][:50]])]
        return "\n".join(parts)

    return _emit(args, data, text)


def cmd_transfers_decide(args) -> int:
    from hpgbooks.personal.classify import set_transfer_decision

    with connect() as conn:
        set_transfer_decision(conn, args.pair_key, args.decision, actor="cli")
    print(f"{args.decision} {args.pair_key}")
    return 0


def cmd_review(args) -> int:
    from hpgbooks.personal.api import TXN_COLUMNS
    from hpgbooks.personal.queries import review_queue

    with connect(readonly=True) as conn:
        rows = review_queue(conn)

    def text(d):
        body = [[r["date"], r["account_label"][:20], _money(r["amount_cents"]), r["merchant"][:30], r["category"][:20],
                 (r.get("suggestion") or {}).get("category") or "", r["id"][:14]] for r in d]
        return _table(["date", "account", "amount", "merchant", "category", "suggested", "id"], body, right_from=2) + f"\n{len(d)} need review"

    return _emit(args, rows, text, (TXN_COLUMNS, rows))


def cmd_categorize(args) -> int:
    from hpgbooks.personal.actions import categorize

    with connect() as conn:
        category = _category(conn, args.category)
        categorize(conn, args.txn, category, args.note, actor="cli")
    print(f"categorized {args.txn} as {args.category}")
    return 0


def cmd_rules(args) -> int:
    from hpgbooks.personal.actions import list_rules

    with connect(readonly=True) as conn:
        rows = list_rules(conn)
    print(_table(["id", "pri", "on", "sign", "group", "category", "hits", "pattern"],
                 [[str(r["id"]), str(r["priority"]), "yes" if r["active"] else "no", r["amount_sign"] or "", r["group"], r["category"], str(r["hits"]), r["pattern"][:60]] for r in rows],
                 right_from=0))
    return 0


def cmd_rules_add(args) -> int:
    from hpgbooks.personal.actions import add_rule

    with connect() as conn:
        result = add_rule(conn, {"pattern": args.pattern, "category_id": _category(conn, args.category), "amount_sign": args.sign, "priority": args.priority}, actor="cli")
    print(f"added personal rule {result['id']} ({result['changed']} rows changed)")
    return 0


def cmd_rules_disable(args) -> int:
    from hpgbooks.personal.actions import set_rule_active

    with connect() as conn:
        set_rule_active(conn, args.rule_id, False, actor="cli")
    print(f"disabled personal rule {args.rule_id}")
    return 0


def cmd_reconcile(args) -> int:
    from hpgbooks.personal.analytics import reconcile

    with connect(readonly=True) as conn:
        data = reconcile(conn, args.month)

    def text(d):
        lines = [
            f"Personal reconcile {d['month']}",
            f"Earned income                 {_money(d['earned_cents']):>14}",
            f"Owner draws received          {_money(d['owner_draws_cents']):>14}",
            f"Spending (personal accounts) {_money(-d['spending_cents']):>15}",
            f"Transfers net                 {_money(d['transfers_net_cents']):>14}",
            f"Expected change               {_money(d['expected_change_cents']):>14}",
            f"Change in personal balances   {_money(d['balance_change_cents']):>14}",
            f"Difference                    {_money(d['difference_cents']):>14}",
            f"  pending rows ({len(d['pending'])})          {_money(d['pending_cents']):>14}",
            f"  loan credits (spent on the paying side) {_money(d['loan_payments_cents'])}",
            f"  unmatched transfer legs: {len(d['unmatched_transfers'])}",
            f"Memo: paid from business accounts: funding {_money(d['paid_by_business']['funding_cents'])}, "
            f"spending {_money(d['paid_by_business']['spending_cents'])} (not in the balances above)",
            "OK" if d["ties"] else "CHECK",
        ]
        for acct in d["accounts"]:
            if acct["anchor_adjustment_cents"]:
                lines.append(f"  anchor adjustment {acct['label']}: {_money(acct['anchor_adjustment_cents'])}")
        return "\n".join(lines)

    return _emit(args, data, text)


# --- loan offers ------------------------------------------------------------------


def _offer_body(args) -> dict:
    body: dict = {}
    for flag, key in (("lender", "lender"), ("amount", "amount"), ("apr", "apr"), ("fee", "fee_pct"), ("term", "term_months"),
                      ("payment", "monthly_payment"), ("notes", "notes"), ("expires", "expires_on"), ("source", "source")):
        value = getattr(args, flag, None)
        if value is not None:
            body[key] = value
    if args.fee_from_proceeds is not None:
        body["fee_from_proceeds"] = args.fee_from_proceeds
    return body


def _offers_text(rows: list[dict]) -> str:
    if not rows:
        return "no saved offers (add one: hpgbooks personal offers add --lender ... --amount ... --apr ... --term ...)"
    table = [
        [
            str(row["id"]), row["lender"], _money(row["amount_cents"]), f"{row['apr']:g}%", f"{row['fee_pct']:g}%",
            "proceeds" if row["fee_from_proceeds"] else "added", str(row["term_months"]), _money(row["monthly_payment_cents"]),
            (row["expires_on"] or "") + (" (expired)" if row["expired"] else ""), row["source"], row["notes"],
        ]
        for row in rows
    ]
    return _table(["ID", "Lender", "Amount", "APR", "Fee", "Fee from", "Term", "Quoted pmt", "Expires", "Source", "Notes"], table, right_from=2)


def _offer_line(offer: dict) -> str:
    fee = f"{offer['fee_pct']:g}% fee {'from proceeds' if offer['fee_from_proceeds'] else 'added'}"
    payment = f", quoted {_money(offer['monthly_payment_cents'])}/mo" if offer["monthly_payment_cents"] else ""
    return f"#{offer['id']} {offer['lender']}: {_money(offer['amount_cents'])} at {offer['apr']:g}% for {offer['term_months']} months, {fee}{payment}"


def cmd_offers_list(args) -> int:
    from hpgbooks.personal.offers import list_offers

    with connect(readonly=True) as conn:
        rows = list_offers(conn)
    return _emit(args, rows, _offers_text)


def cmd_offers_add(args) -> int:
    from hpgbooks.personal.offers import create_offer

    with connect() as conn:
        offer = create_offer(conn, _offer_body(args), actor="cli")
    print(f"saved offer {_offer_line(offer)}")
    return 0


def cmd_offers_update(args) -> int:
    from hpgbooks.personal.offers import update_offer

    body = _offer_body(args)
    if not body:
        raise HpgbooksError("nothing to change: give at least one flag")
    with connect() as conn:
        offer = update_offer(conn, args.offer_id, body, actor="cli")
    print(f"updated offer {_offer_line(offer)}")
    return 0


def cmd_offers_delete(args) -> int:
    from hpgbooks.personal.offers import delete_offer, get_offer

    with connect() as conn:
        offer = get_offer(conn, args.offer_id)
        delete_offer(conn, args.offer_id, actor="cli")
    print(f"deleted offer {_offer_line(offer)}")
    return 0
