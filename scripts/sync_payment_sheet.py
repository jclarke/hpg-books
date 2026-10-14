#!/usr/bin/env python3
"""Load minimum payment / due date / APR rows (from the Credit Card Payment Tracker sheet).

    .venv/bin/python scripts/sync_payment_sheet.py rows.json --as-of 2026-10-02 [--dry-run]

Input is a JSON list of objects, or a CSV with a header row. Recognized columns (any case):
account (or name), last4, balance, min, due, apr, autopay, as_of, notes, unverified,
statement_balance, skip. Rows are matched to credit-card and loan accounts by last 4, then by
name. The rows are stored with source "sheet", which ranks below statements, the issuer's
feed, and anything entered by hand: a sheet row never replaces newer statement data, only
fills blanks and logs the disagreement. It replaces older data only when that old due date
had already passed. Balances in the sheet are compared with the books and reported, never
stored. Conflicts print and go to the audit log. Details: docs/payment-tracking.md
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hpgbooks import payments as pay  # noqa: E402
from hpgbooks.db import HpgbooksError, audit, connect, format_money  # noqa: E402
from hpgbooks.scope import all_accounts, label  # noqa: E402

ALIASES = {
    "account": ("account", "name", "card"),
    "last4": ("last4", "mask", "last_4"),
    "balance": ("balance", "bal", "current_balance"),
    "min": ("min", "minimum", "min_payment", "minimum_payment", "min_due"),
    "due": ("due", "due_date", "payment_due_date"),
    "apr": ("apr",),
    "autopay": ("autopay",),
    "as_of": ("as_of", "checked", "as of"),
    "notes": ("notes", "note"),
    "unverified": ("unverified",),
    "statement_balance": ("statement_balance", "statement_bal"),
    "skip": ("skip",),
}


def read_rows(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        raw = data.get("rows") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            raise HpgbooksError("JSON input must be a list of rows")
    else:
        raw = list(csv.DictReader(text.splitlines()))
    rows = []
    for item in raw:
        lowered = {str(k).strip().lower(): v for k, v in item.items()}
        row = {}
        for key, names in ALIASES.items():
            for name in names:
                if name in lowered and lowered[name] not in (None, ""):
                    row[key] = lowered[name]
                    break
        rows.append(row)
    return rows


def find_account(conn, row: dict) -> dict | None:
    pool = [a for a in all_accounts(conn) if a["scope"] in ("business", "personal") and (a["class"] in ("liability", "loan") or a["type"] == "liability")]
    last4 = str(row.get("last4") or "").strip()
    name = str(row.get("account") or "").strip().lower()
    if last4:
        hits = [a for a in pool if a.get("last4") == last4]
        if len(hits) > 1 and name:
            hits = [a for a in hits if name in (a.get("name") or "").lower() or name in (a.get("display_name") or "").lower()] or hits
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None
    if name:
        hits = [a for a in pool if name in (a.get("name") or "").lower() or name in (a.get("display_name") or "").lower()]
        if len(hits) == 1:
            return hits[0]
    return None


def truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "y", "x")


def sync(conn, rows: list[dict], *, as_of: str, source: str = "sheet", dry_run: bool = False) -> dict:
    owed = {**pay.balances_owed(conn, "personal"), **pay.balances_owed(conn, "business")}
    report = {"updated": [], "unchanged": [], "unmatched": [], "conflicts": [], "balance_diffs": [], "skipped": []}
    for row in rows:
        title = f"{row.get('account') or ''} {row.get('last4') or ''}".strip() or "(blank row)"
        if truthy(row.get("skip")):
            report["skipped"].append(title)
            continue
        acct = find_account(conn, row)
        if acct is None:
            report["unmatched"].append(title)
            continue
        fields: dict = {"estimated": 0}
        if "min" in row:
            fields["min_payment_cents"] = pay.cents_arg(row["min"], "minimum payment")
        if "due" in row:
            fields["due_date"] = row["due"]
        for key, target in (("apr", "apr"), ("autopay", "autopay"), ("notes", "notes")):
            if key in row:
                fields[target] = row[key]
        if "statement_balance" in row:
            fields["statement_balance_cents"] = pay.cents_arg(row["statement_balance"], "statement balance")
        fields["unverified"] = truthy(row.get("unverified", ""))
        when = pay.parse_day(row.get("as_of")) or as_of
        if dry_run:
            before = pay.get_terms(conn, acct["id"])
            report["updated"].append(f"{label(acct)}: would apply (existing: {before['source'] + ' ' + str(before['as_of']) if before else 'none'})")
        else:
            result = pay.merge_terms(conn, acct["id"], fields, source=source, as_of=when, actor="sync_payment_sheet")
            bucket = "updated" if result["action"] in ("created", "updated") else "unchanged"
            report[bucket].append(f"{label(acct)}: {result['action']}" + (f" ({', '.join(result['changed'] + result['filled'])})" if result["changed"] or result["filled"] else ""))
            for line in result["conflicts"]:
                report["conflicts"].append(f"{label(acct)}: {line}")
                audit(conn, "payment_sync_conflict", field=acct["id"], new_value=line[:300], actor="sync_payment_sheet", note=source)
        if "balance" in row and "statement_balance" not in row:
            sheet_cents = pay.cents_arg(row["balance"], "balance")
            ours = owed.get(acct["id"])
            if ours is not None and sheet_cents is not None and abs(sheet_cents - ours) >= 100:
                report["balance_diffs"].append(
                    f"{label(acct)}: sheet {format_money(sheet_cents)} vs books {format_money(ours)} (sheet - books {format_money(sheet_cents - ours)})"
                )
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("input", help="JSON or CSV of rows")
    p.add_argument("--as-of", dest="as_of", default=pay.today_iso(), help="date the sheet was checked (default today)")
    p.add_argument("--source", default="sheet")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--conflicts-out", metavar="FILE", help="also write conflicts and balance differences as text")
    args = p.parse_args(argv)
    try:
        rows = read_rows(Path(args.input).expanduser())
        as_of = pay.parse_day(args.as_of)
        with connect() as conn:
            report = sync(conn, rows, as_of=as_of, source=args.source, dry_run=args.dry_run)
            if args.dry_run:
                conn.rollback()
    except (HpgbooksError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for key in ("updated", "unchanged", "skipped", "unmatched", "conflicts", "balance_diffs"):
        if report[key]:
            print(f"{key} ({len(report[key])}):")
            for line in report[key]:
                print(f"  {line}")
    if args.conflicts_out:
        out = Path(args.conflicts_out)
        out.write_text("\n".join(report["conflicts"] + report["balance_diffs"]) + "\n", encoding="utf-8")
        out.chmod(0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
