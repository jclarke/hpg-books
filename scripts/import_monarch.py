#!/usr/bin/env python3
"""Reconcile the personal ledger with a Monarch transactions export, fill gaps, hint categories.

    .venv/bin/python scripts/import_monarch.py monarch-transactions-all.csv                 # report only
    .venv/bin/python scripts/import_monarch.py monarch-transactions-all.csv --apply         # insert missing 2026 rows
    .venv/bin/python scripts/import_monarch.py monarch-transactions-all.csv --rules         # show suggested rules
    .venv/bin/python scripts/import_monarch.py monarch-transactions-all.csv --rules --apply # create them

Only personal accounts are touched, only rows dated --since (default 2026-01-01) or later
that our books do not already hold (same amount within a few days, Monarch splits and
sign-flipped card payments count as present). Accounts fed by the Apple Card CSV or the
CFNA statements are compared, never imported. Rows from the last --recent-days on
Finance-synced accounts are left to the feed. Safe to re-run. Details: docs/importers.md
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hpgbooks import monarch  # noqa: E402
from hpgbooks.db import HpgbooksError, connect  # noqa: E402


def money(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv", help="Monarch transactions export")
    p.add_argument("--apply", action="store_true", help="write changes (default: report only)")
    p.add_argument("--rules", action="store_true", help="suggest (and with --apply create) category rules from Monarch's categories")
    p.add_argument("--since", default=monarch.SINCE, help="first date to consider (default 2026-01-01)")
    p.add_argument("--recent-days", type=int, default=3, help="leave this many days to the Finance feed (default 3)")
    p.add_argument("--today", default=date.today().isoformat(), help=argparse.SUPPRESS)
    p.add_argument("--map", action="append", default=[], metavar="MONARCH=OURS", help='alias a Monarch account name to part of one of our account names (e.g. "Savings=Apple Savings")')
    p.add_argument("--report", metavar="FILE", help="also write the per-account reconciliation as CSV")
    p.add_argument("--list-missing", action="store_true", help="print every row that is missing")
    return p


def print_report(report: list[dict], list_missing: bool) -> None:
    print(f"{'Monarch account':42} {'our account':30} {'action':34} {'Monarch n/sum':>20} {'ours n/sum':>20} {'missing':>18}")
    for item in report:
        acct = item["account"]
        missing = f"{len(item['missing'])} {money(sum(r.amount_cents for r in item['missing']))}"
        print(
            f"{item['monarch'][:42]:42} {(acct['name'][:30] if acct else '-'):30} {item['action'][:34]:34} "
            f"{item['monarch_n']:>6} {money(item['monarch_cents']):>13} {item['ours_n']:>6} {money(item['ours_cents']):>13} {missing:>18}"
        )
        if item["monarch_dups"]:
            print(f"    {len(item['monarch_dups'])} rows are Monarch duplicates (same day/amount/text twice where Finance holds one); not imported")
        if item["skipped_recent"]:
            print(f"    {len(item['skipped_recent'])} rows from the last days left to the Finance feed")
        if list_missing:
            for r in item["missing"]:
                print(f"    + {r.date} {money(r.amount_cents):>11}  {r.statement[:44]}  [{r.category}]")


def write_report(report: list[dict], path: str) -> None:
    import csv

    with open(path, "w", newline="", encoding="utf-8") as fh:
        out = csv.writer(fh)
        out.writerow(["monarch_account", "our_account", "action", "monarch_rows", "monarch_sum", "our_rows", "our_sum", "missing_rows", "missing_sum", "unmatched_ours_rows", "unmatched_ours_sum"])
        for item in report:
            acct = item["account"]
            out.writerow([
                item["monarch"], acct["name"] if acct else "", item["action"], item["monarch_n"], item["monarch_cents"] / 100, item["ours_n"], item["ours_cents"] / 100,
                len(item["missing"]), sum(r.amount_cents for r in item["missing"]) / 100, len(item["left_over"]), sum(h["amount_cents"] for h in item["left_over"]) / 100,
            ])
    Path(path).chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        overrides = dict(item.split("=", 1) for item in args.map)
        rows = monarch.read_csv(args.csv)
        print(f"{len(rows)} Monarch rows, {min(r.date for r in rows)} .. {max(r.date for r in rows)}")
        with connect() as conn:
            if args.rules:
                result = monarch.suggest_rules(conn, rows)
                print(f"review queue: {result['queue']} rows; {len(result['rules'])} rules suggested, {len(result['skipped'])} merchants skipped")
                for rule in sorted(result["rules"], key=lambda r: -r["cents"]):
                    print(f"  {rule['pattern'][:40]:40} -> {rule['category'][0]}/{rule['category'][1]:<20} rows {rule['rows']:>3} {money(rule['cents']):>11}  Monarch {rule['monarch']} history {rule['history']}")
                if args.apply:
                    outcome = monarch.apply_rules(conn, result["rules"])
                    if outcome["unexpected"]:
                        conn.rollback()
                        print(f"stopped: {len(outcome['unexpected'])} rows that were already categorized would change; nothing written", file=sys.stderr)
                        return 1
                    from hpgbooks.personal.queries import review_queue

                    print(f"created {len(result['rules'])} rules; {outcome['moved']} rows moved out of Uncategorized; review queue now {len(review_queue(conn))}")
                else:
                    conn.rollback()
                return 0
            report = monarch.reconcile(conn, rows, since=args.since, recent_days=args.recent_days, today=args.today, overrides=overrides)
            print_report(report, args.list_missing)
            if args.report:
                write_report(report, args.report)
            todo = sum(len(i["missing"]) for i in report if i["action"] == "import")
            if not args.apply:
                print(f"\nreport only: {todo} rows would be inserted (run with --apply)")
                conn.rollback()
                return 0
            stats = monarch.apply_report(conn, report)
            print(f"\ninserted {sum(s['inserted'] for s in stats.values())} rows")
            for name, st in stats.items():
                kinds = ", ".join(f"{n} {k}" for k, n in sorted(st["kinds"].items()))
                print(f"  {name}: {st['inserted']} new ({kinds}); {st['unchanged']} already imported; {st['rules']} payment rules")
                for key in st["unpaired"]:
                    print(f"    un-paired a false transfer match: {key}")
                for key in st["ambiguous"]:
                    print(f"    check by hand (more than one candidate): hpgbooks personal transfers confirm|reject '{key}'")
    except (HpgbooksError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
