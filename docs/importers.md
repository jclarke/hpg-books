# Importers

Every importer is idempotent: running it again on the same (or an overlapping) file adds nothing. Rows are only imported for accounts registered in the database, and each row is classified by the engine of its account's scope (business rules for business accounts, personal rules for personal ones; excluded accounts are stored but not classified).

Back up before a large import: `cp -p data/hpbooks.db data/hpbooks.db.bak-$(date +%F)`. Keep source files (exports, statements, pulls) out of git: `sync/inbox/`, `exports/`, and `data/` are gitignored.

## Aggregator pulls (`bin/hpgbooks import`)

The main feed is a transaction aggregator reached through an MCP tool (the "Finance" tools: `finance_list_accounts`, `finance_query_account_transactions`, `finance_query_liability_transactions`). HPG Books never calls it; an agent or a person saves the raw tool results under `sync/inbox/` and runs:

```bash
bin/hpgbooks import sync/inbox/2026-10-01/             # a file, a directory (and its personal/ subfolder), or a glob
bin/hpgbooks import pull.json --from 2026-01-01 --to 2026-01-31
```

Accepted shapes: an object with a `transactions` array; a raw tool result `{"format": "csv", "csv": "...", "row_count": N, "next_cursor": ...}`; or a JSON list of either (several pages in one file). A `liability_id` column is read as the account id. An optional `_query` object (`account_ids`, `date_from`, `date_to`) makes that window authoritative, so a pending row that vanished from the pull is marked superseded even when the account returned no rows. A posted row that replaces a pending one (same account, amount equal or within 20%, date within 7 days) inherits its manual classification.

Rows for an unregistered account are skipped and the account id is printed. Register accounts from a saved `finance_list_accounts` result:

```bash
bin/hpgbooks accounts discover sync/inbox/2026-10-01/accounts/finance_list_accounts.json --dry-run
bin/hpgbooks accounts discover sync/inbox/2026-10-01/accounts/finance_list_accounts.json   # new accounts become personal
bin/hpgbooks accounts set-scope <id|last4|name> business
```

The daily routine (which tools, paging, file names, balance refresh) is in [sync/README.md](../sync/README.md).

## Capital One CSV (`bin/hpgbooks import-capitalone`)

```bash
bin/hpgbooks import-capitalone transaction_download.csv [--account ID] [--dry-run]
bin/hpgbooks verify-capitalone --csv transaction_download.csv --statements Statement_*.pdf [--account ID]
```

Reads the card download (`Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit`). The card number column must match the account's last 4. Without `--account` the account with role `capitalone_default` in the config is used. Rows that duplicate an aggregator transaction are skipped, and a later aggregator import supersedes CSV rows it overlaps. `verify-capitalone` checks the rows against statement PDFs.

## Apple Card CSV (`scripts/import_applecard.py`)

```bash
.venv/bin/python scripts/import_applecard.py transactions.csv --statements ~/Downloads/applecard --dry-run
.venv/bin/python scripts/import_applecard.py transactions.csv --statements ~/Downloads/applecard --balance 1234.56 --as-of 2026-10-03
```

Loads the Wallet transactions export into a personal liability account (an existing account with "Apple Card" in its name, else a new one with id `importers.applecard_account_id`). A row's id is a hash of transaction date, clearing date, description, amount, and an occurrence index, so two identical purchases on one day are both kept, once each. With `--statements` (a folder of `statement-YYYY-MM.pdf`) each month is tied out: charges, interest, and payments add up and previous balance + charges + interest − payments = new balance. A failing month stops the import unless `--force`. Other options: `--parse-only`, `--csv-out FILE`, `--glob`, `--reclassify`.

## Store-card statement PDFs (`scripts/import_cfna.py`)

```bash
.venv/bin/python scripts/import_cfna.py ~/Downloads/statements --last4 1234 --dry-run
.venv/bin/python scripts/import_cfna.py ~/Downloads/statements --last4 1234 --balance 1234.56 --as-of 2026-10-03
```

For Credit First N.A. (CFNA) store cards, which have no aggregator feed. Reads `statement-*.pdf` with `pdftotext -layout`, checks each statement's rows against its account summary (payments, credits, purchases, fees, interest, and the balance equation), and inserts rows whose id `cfna:<reference>:<date>:<cents>` is new. A statement that does not reconcile is skipped unless `--force`. `--last4` defaults to `importers.cfna_last4`. Other options: `--parse-only`, `--csv FILE`, `--glob`, `--reclassify`.

Both statement importers mark their account `manual:` with daily sync off, record an opening balance from the earliest statement, store the newest statement's minimum payment, due date, and APR, and pair card payments with the bank-side debit (ambiguous pairs are listed for `bin/hpgbooks personal transfers confirm|reject`). `accounts discover` recognizes such an account later (`manual-match`) instead of creating a twin. They need `pdftotext` (poppler-utils).

## Monarch export (`scripts/import_monarch.py`)

```bash
.venv/bin/python scripts/import_monarch.py monarch-transactions.csv                  # report only
.venv/bin/python scripts/import_monarch.py monarch-transactions.csv --apply          # insert missing rows
.venv/bin/python scripts/import_monarch.py monarch-transactions.csv --rules [--apply] # category rule hints
```

A cross-check, not a feed. Personal accounts only. Monarch accounts are matched to yours by an alias (`importers.monarch_aliases` or `--map "Monarch name=part of ours"`), the last 4 in the name, or the exact name. A Monarch row counts as present when the ledger has the same amount within a few days, when Monarch split one charge into several rows, or when it flipped a card payment's sign. Only missing rows are inserted (id `monarch:<Monarch id>`). Accounts fed by the statement importers are compared, never imported, and the last `--recent-days` on synced accounts are left to the feed. `--rules` suggests rules for review-queue merchants whose Monarch category is consistent. Options: `--since DATE`, `--report FILE.csv`, `--list-missing`.

## Balance-only accounts (`scripts/set_manual_balance.py`)

Mortgages, retirement accounts, property, and vehicles have no transactions; they get one balance anchor per refresh and count only toward net worth. See [manual-balances.md](manual-balances.md).

## Payment terms (`scripts/sync_payment_sheet.py`)

Loads minimum payment, due date, and APR rows from a spreadsheet you maintain (JSON or CSV). See [payment-tracking.md](payment-tracking.md).

## Writing a new importer

Follow `hpgbooks/applecard.py` or `hpgbooks/cfna.py`: parse into rows with integer cents in the ledger sign (money into the account is positive), give each row a stable id derived from the source, insert only ids that are new, check the file against its own totals before writing, and run the scope's classifier on the new rows. Add a test with an invented file under `tests/`.
