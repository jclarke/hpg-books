#!/usr/bin/env python3
"""Import Credit First N.A. (CFNA) statement PDFs into the personal ledger.

    .venv/bin/python scripts/import_cfna.py /path/to/folder --last4 1234            # import new rows
    .venv/bin/python scripts/import_cfna.py /path/to/folder --last4 1234 --dry-run  # parse + reconcile, show what would change
    .venv/bin/python scripts/import_cfna.py /path/to/folder --last4 1234 --csv out.csv --parse-only   # no database at all
    .venv/bin/python scripts/import_cfna.py /path/to/folder --last4 1234 --balance 1234.56 --as-of 2026-10-03

Reads `statement-*.pdf` (override with --glob) with `pdftotext -layout`, checks
each statement's rows against its ACCOUNT SUMMARY, and inserts only rows that
are not in the ledger yet (id = cfna:<reference>:<date>:<cents>), so a weekly
re-run is safe. A statement that does not reconcile is skipped unless --force.
The database is HPBOOKS_DB / data/hpbooks.db like every hpgbooks command.
Details: docs/importers.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hpgbooks import cfna  # noqa: E402
from hpgbooks.balances import parse_dollars  # noqa: E402
from hpgbooks.config import get_config  # noqa: E402
from hpgbooks.db import HpgbooksError, connect  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Import CFNA statement PDFs into the personal ledger.")
    p.add_argument("paths", nargs="+", help="a folder of statement PDFs, or PDF files")
    p.add_argument("--glob", default="statement-*.pdf", help="file pattern inside a folder (default statement-*.pdf)")
    p.add_argument("--last4", default=get_config().importers.cfna_last4,
                   help="card last 4 (default: config importers.cfna_last4); other cards are skipped")
    p.add_argument("--dry-run", action="store_true", help="parse, reconcile, and count new rows; write nothing")
    p.add_argument("--parse-only", action="store_true", help="never open the database (use with --csv)")
    p.add_argument("--csv", metavar="FILE", help="write every parsed row to this CSV (mode 0600)")
    p.add_argument("--balance", metavar="DOLLARS", help="amount owed as of --as-of (for example 1234.56)")
    p.add_argument("--as-of", metavar="YYYY-MM-DD", help="date for --balance")
    p.add_argument("--force", action="store_true", help="import statements even when they do not reconcile")
    p.add_argument("--reclassify", action="store_true", help="run `personal reclassify` after importing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.last4 or not (len(args.last4) == 4 and args.last4.isdigit()):
        print("--last4 is required: the card's last 4 digits (or set importers.cfna_last4 in the config)", file=sys.stderr)
        return 2
    if bool(args.balance) != bool(args.as_of):
        print("--balance and --as-of go together", file=sys.stderr)
        return 2
    pdfs: list[Path] = []
    for item in args.paths:
        path = Path(item).expanduser()
        if not path.exists():
            print(f"not found: {path.name}", file=sys.stderr)
            return 2
        pdfs.extend(cfna.find_statement_pdfs(path, args.glob))
    if not pdfs:
        print("no statement PDFs found")
        return 1
    statements, skipped = cfna.load_statements(pdfs, args.last4)
    for note in skipped:
        print(f"skipped {note}")
    if not statements:
        print("no CFNA statements for this card")
        return 1

    good: list[cfna.Statement] = []
    failed = 0
    print("statement  rows  new-balance   reconcile")
    for stmt in statements:
        result = cfna.reconcile(stmt)
        new_balance = stmt.summary.get("new_balance")
        status = "OK" if result["ok"] else "MISMATCH"
        print(f"{stmt.label}    {len(stmt.txns):>4}  {cfna.fmt(new_balance):>11}   {status}")
        for problem in result["problems"]:
            print(f"    - {problem}")
        for warning in stmt.warnings:
            print(f"    ! {warning}")
        if result["ok"] or args.force:
            good.append(stmt)
        else:
            failed += 1
    rows = cfna.unique_rows(good)

    if args.csv:
        out = Path(args.csv)
        out.write_text(cfna.to_csv(rows), encoding="utf-8")
        out.chmod(0o600)
        print(f"wrote {len(rows)} rows to {out}")
    if args.parse_only:
        return 1 if failed else 0

    try:
        with connect() as conn:
            account_id, created = cfna.ensure_account(conn, args.last4, dry_run=args.dry_run)
            print(f"account {account_id} {'(would be created)' if created and args.dry_run else '(created)' if created else '(exists)'}")
            for note in cfna.ensure_anchors(conn, account_id, good, dry_run=args.dry_run):
                print(note)
            from hpgbooks import payments

            print(payments.ingest_latest_statement(conn, account_id, good, dry_run=args.dry_run))
            stats = cfna.import_rows(conn, account_id, rows, dry_run=args.dry_run)
            prefix = "would insert" if args.dry_run else "inserted"
            print(f"{prefix} {stats['inserted']} new rows; {stats['unchanged']} already in the ledger")
            for label, per in sorted(stats["by_statement"].items()):
                print(f"  {label}: {per['inserted']} new of {per['rows']}")
            for key in stats.get("unpaired", []):
                print(f"  un-paired a false transfer match: {key}")
            for key in stats.get("ambiguous", []):
                print(f"  check by hand (a card payment matches more than one bank debit); then 'hpgbooks personal transfers confirm' or 'reject' this key: {key}")
            if args.balance:
                print(cfna.set_manual_balance(conn, account_id, parse_dollars(args.balance), args.as_of, "CFNA account summary (manual)", dry_run=args.dry_run))
            if args.reclassify and not args.dry_run:
                from hpgbooks.personal.classify import reclassify_personal

                print(f"reclassified {reclassify_personal(conn)} rows")
            if args.dry_run:
                conn.rollback()
    except HpgbooksError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
