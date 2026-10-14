#!/usr/bin/env python3
"""Set (or refresh) the balance of a balance-only manual account.

    .venv/bin/python scripts/set_manual_balance.py "Example 401k" 12345.67 --as-of 2026-10-03
    .venv/bin/python scripts/set_manual_balance.py "Example Mortgage 1234" 250000.00 --as-of 2026-10-03 \\
        --create --class loan --last4 1234 --institution "Example Lender" --subtype mortgage
    .venv/bin/python scripts/set_manual_balance.py --batch accounts.json --as-of 2026-10-03 [--dry-run]

Liabilities (card, loan, mortgage) are entered as the amount owed (positive); assets
as their value. --batch takes a JSON list of {"name", "balance", "class", "last4",
"institution", "subtype"}; accounts that do not exist yet are created (personal scope,
sync off, notes "manual: balance from <label> <date>, update manually"). Re-running
with the same balance and date changes nothing. Database: HPBOOKS_DB / data/hpbooks.db.
Details: docs/manual-balances.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hpgbooks import manual_accounts  # noqa: E402
from hpgbooks.db import HpgbooksError, connect  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", nargs="?", help="account name (or id, or a unique part of it)")
    p.add_argument("balance", nargs="?", help="dollars: amount owed for a liability, value for an asset")
    p.add_argument("--as-of", default=date.today().isoformat(), help="balance date YYYY-MM-DD (default today)")
    p.add_argument("--batch", metavar="FILE", help="JSON list of accounts to set (creates the missing ones)")
    p.add_argument("--create", action="store_true", help="create the account if it does not exist (needs --class)")
    p.add_argument("--class", dest="account_class", choices=["cash", "liability", "investment", "loan", "other"],
                   help="class of a new account: loan (mortgage, auto, personal), liability (card), investment, cash, other (property, vehicle)")
    p.add_argument("--last4", help="last 4 digits (lets `accounts discover` match a later Finance account)")
    p.add_argument("--institution")
    p.add_argument("--subtype", help="free text such as mortgage, 401k, Roth IRA")
    p.add_argument("--label", default=manual_accounts.DEFAULT_LABEL, help="where the number came from (default Monarch)")
    p.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    return p


def load_batch(path: str) -> list[dict]:
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(item, dict) and "name" in item and "balance" in item for item in data):
        raise HpgbooksError("batch file must be a JSON list of objects with name and balance")
    return data


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.batch:
            items = load_batch(args.batch)
        elif args.name is not None and args.balance is not None:
            items = [{"name": args.name, "balance": args.balance, "class": args.account_class if args.create else None,
                      "last4": args.last4, "institution": args.institution, "subtype": args.subtype}]
            if args.create and not args.account_class:
                raise HpgbooksError("--create needs --class")
        else:
            raise HpgbooksError("give NAME BALANCE, or --batch FILE")
        with connect() as conn:
            for item in items:
                print(
                    manual_accounts.set_balance(
                        conn,
                        str(item["name"]),
                        item["balance"],
                        item.get("as_of") or args.as_of,
                        create_class=item.get("class") or None,
                        last4=item.get("last4") or None,
                        institution=item.get("institution") or None,
                        subtype=item.get("subtype") or None,
                        label=args.label,
                        dry_run=args.dry_run,
                    )
                )
            if args.dry_run:
                conn.rollback()
    except (HpgbooksError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
