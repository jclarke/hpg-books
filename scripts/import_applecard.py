#!/usr/bin/env python3
"""Import an Apple Card transactions CSV (Wallet export) into the personal ledger.

    .venv/bin/python scripts/import_applecard.py ~/Downloads/transactions-2026.csv
    .venv/bin/python scripts/import_applecard.py transactions-2026.csv --statements ~/Downloads/applecard --dry-run
    .venv/bin/python scripts/import_applecard.py transactions-2026.csv --balance 1234.56 --as-of 2026-10-03

Dedupe key: transaction date + clearing date + description + amount + the n-th
identical row, so a weekly re-run (or an overlapping export) inserts nothing new.
With --statements (a folder of statement-*.pdf) each month is tied out to the
CSV first; a mismatch stops the import unless --force. Uses the existing Apple
Card account if there is one, otherwise creates the personal liability account
"Apple Card". The database is HPBOOKS_DB / data/hpbooks.db like every hpgbooks
command. Details: docs/importers.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hpgbooks import applecard, cfna  # noqa: E402
from hpgbooks.balances import parse_dollars  # noqa: E402
from hpgbooks.db import HpgbooksError, connect  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Import an Apple Card transactions CSV into the personal ledger.")
    p.add_argument("csv", help="the Apple Card transactions CSV")
    p.add_argument("--statements", metavar="FOLDER", help="folder of statement-*.pdf to tie out against the CSV")
    p.add_argument("--glob", default="statement-*.pdf")
    p.add_argument("--dry-run", action="store_true", help="parse, tie out, count new rows; write nothing")
    p.add_argument("--parse-only", action="store_true", help="never open the database")
    p.add_argument("--csv-out", metavar="FILE", help="write the parsed rows (ledger sign) to this CSV (mode 0600)")
    p.add_argument("--balance", metavar="DOLLARS", help="amount owed as of --as-of")
    p.add_argument("--as-of", metavar="YYYY-MM-DD")
    p.add_argument("--force", action="store_true", help="import even when a sign or statement check fails")
    p.add_argument("--reclassify", action="store_true", help="run personal reclassify afterwards")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if bool(args.balance) != bool(args.as_of):
        print("--balance and --as-of go together", file=sys.stderr)
        return 2
    path = Path(args.csv).expanduser()
    try:
        rows = applecard.read_csv(path)
    except HpgbooksError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not rows:
        print("no rows in the CSV")
        return 1
    kinds: dict[str, list[int]] = {}
    for r in rows:
        kinds.setdefault(r.kind, []).append(r.amount_cents)
    print(f"{len(rows)} rows, {min(r.txn_date for r in rows)} .. {max(r.txn_date for r in rows)}")
    for kind, values in sorted(kinds.items()):
        print(f"  {kind:<9} {len(values):>4}  {cfna.fmt(sum(values))}")
    bad = False
    for r in rows:
        if r.problem:
            print(f"  ! {r.txn_date} {r.description[:30]}: {r.problem}")
            bad = True

    statements: list[applecard.Statement] = []
    if args.statements:
        folder = Path(args.statements).expanduser()
        pdfs = sorted(folder.glob(args.glob)) if folder.is_dir() else [folder]
        statements, notes = applecard.load_statements(pdfs)
        for note in notes:
            print(f"skipped {note}")
        print("statement  csv-rows  stmt-rows  total-balance  tie-out")
        for rep in applecard.tie_out(rows, statements):
            print(f"{rep['label']}    {rep['csv_rows']:>6}  {rep['statement_rows']:>9}  {cfna.fmt(rep['total']):>13}  {'OK' if rep['ok'] else 'MISMATCH'}")
            for problem in rep["problems"]:
                print(f"    - {problem}")
            bad = bad or not rep["ok"]
    if bad and not args.force:
        print("not importing: fix the problems above or pass --force")
        return 1

    if args.csv_out:
        out = Path(args.csv_out)
        out.write_text(applecard.to_csv(rows), encoding="utf-8")
        out.chmod(0o600)
        print(f"wrote {len(rows)} rows to {out}")
    if args.parse_only:
        return 0

    try:
        with connect() as conn:
            account_id, created = applecard.ensure_account(conn, dry_run=args.dry_run)
            print(f"account {account_id} {'(would be created)' if created and args.dry_run else '(created)' if created else '(exists)'}")
            for note in applecard.ensure_opening_anchor(conn, account_id, rows, statements, dry_run=args.dry_run):
                print(note)
            from hpgbooks import payments

            print(payments.ingest_latest_statement(conn, account_id, statements, dry_run=args.dry_run))
            stats = applecard.import_rows(conn, account_id, rows, dry_run=args.dry_run)
            verb = "would insert" if args.dry_run else "inserted"
            print(f"{verb} {stats['inserted']} new rows; {stats['unchanged']} already in the ledger; {stats['overlaps']} skipped (already held from another source)")
            for month, count in sorted(stats["by_month"].items()):
                print(f"  {month}: {count} new")
            for key in stats["unpaired"]:
                print(f"  un-paired a false transfer match: {key}")
            for key in stats["ambiguous"]:
                print(f"  check by hand (a payment matches more than one bank debit); then 'hpgbooks personal transfers confirm' or 'reject' this key: {key}")
            if args.balance:
                print(cfna.set_manual_balance(conn, account_id, parse_dollars(args.balance), args.as_of, "Apple Card balance (manual)", dry_run=args.dry_run))
            if args.reclassify and not args.dry_run:
                from hpgbooks.personal.classify import reclassify_personal

                print(f"reclassified {reclassify_personal(conn)} rows")
            if args.dry_run:
                conn.rollback()
    except HpgbooksError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
