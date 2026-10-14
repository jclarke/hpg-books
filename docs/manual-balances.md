# Balance-only manual accounts

Mortgages, loans, retirement accounts, houses and cars have no usable feed and no transactions to import. They live in the personal ledger as **balance-only manual accounts**: personal scope, sync off, no transactions, one balance anchor per refresh. Only the net-worth views read them; spending, cash flow, budgets, transfers and every business report only read transactions, so these accounts cannot change them.

`scripts/set_manual_balance.py` creates and refreshes them:

```bash
cp -p data/hpbooks.db data/hpbooks.db.bak-pre-manual-nw
# refresh one (also matches a unique piece of the name)
.venv/bin/python scripts/set_manual_balance.py "Example Mortgage 1234" 249500.00 --as-of 2026-11-03
# create one
.venv/bin/python scripts/set_manual_balance.py "Example Mortgage 1234" 250000.00 --as-of 2026-10-03 \
    --create --class loan --last4 1234 --institution "Example Lender" --subtype mortgage
# many at once: a JSON list of {"name", "balance", "class", "last4", "institution", "subtype"}
.venv/bin/python scripts/set_manual_balance.py --batch accounts.json --as-of 2026-10-03 --dry-run
```

- Liabilities (class `loan` for a mortgage, auto or personal loan; `liability` for a card) are entered as the **amount owed** (positive). Assets (`investment`, `cash`, `other` for property and vehicles) are their value.
- The account note is `manual: balance from <label> <date>, update manually` (`--label`, default Monarch). The `manual:` prefix is what `hpgbooks accounts discover` looks for.
- Re-running with the same balance and date changes nothing; a new date adds an anchor and the newest one is the balance shown.
- Only manual accounts can be refreshed this way, so a synced account is never given a hand-typed anchor. The script writes to `HPBOOKS_DB` / `data/hpbooks.db`; keep batch files with real balances out of git.

## Discovery

If Finance later lists the same account, `accounts discover` reports `manual-match` and registers nothing and adds no anchor. It matches by last 4 plus cash/liability type, or, for an account without a last 4, by name (one name contained in the other, at least 5 letters). The institution alone never matches (one provider often holds several accounts). Give an account a last 4 when it has one.
