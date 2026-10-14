# Daily Finance MCP sync

HPG Books does not call Finance MCP itself. An agent (or you) pulls transactions and drops the raw responses in `sync/inbox/`. `bin/hpgbooks import` then loads them. Those files are gitignored.

For the first load, pull the history you want (for example from January 1 of the current year) into a dated folder such as `sync/inbox/seed/` in date windows, and import it the same way. Daily runs after that pull a short overlapping window and import it.

## Accounts and tools

The account list comes from the database. Print it, with the tool and inbox file prefix for each account, before every run:

```bash
bin/hpgbooks accounts sync-list            # business and personal, sync-enabled, not excluded or closed
bin/hpgbooks accounts sync-list --json     # same, for an agent
```

Pull **per scope**: business accounts exactly as below (same commands and file names), personal accounts into `sync/inbox/YYYY-MM-DD/personal/<last4-or-label>_<page>.json` (the `file_prefix` column already includes `personal/`). Excluded accounts and accounts with daily sync turned off are not listed; do not pull them.

Pass one account per request. Use `date_from` and `date_to`. Do not use `start_date` / `end_date`.

Cash and investment accounts use `finance_query_account_transactions`. Liability accounts (cards, loans, mortgages) use `finance_query_liability_transactions`.

`sync-list` is the authoritative list of accounts, tools, and file prefixes. A business account's `file_prefix` is its last 4 (or a short label such as `paypal` for an account without one); a personal account's prefix starts with `personal/`.

If an aggregator has no history for an account before some date (a newly opened card, say), pulls before that day come back empty; that is expected.

## Fetch

Each day, pull the last 10 days for every account. Overlap is intentional: that is how a pending transaction drops out of a later pull and the posted transaction with a new id gets matched.

Use `limit` 500.

1. First call, one account:
   - cash: `finance_query_account_transactions`
   - cards: `finance_query_liability_transactions`
   - `account_ids`: `[<that account id>]`
   - `date_from`: 10 days ago (`YYYY-MM-DD`)
   - `date_to`: today
   - `limit`: `500`
2. Save the tool result **verbatim** (do not reshape it) to:

   `sync/inbox/YYYY-MM-DD/<account>_<page>.json`

   `<account>` is the account's `file_prefix` from `accounts sync-list` (its last 4, or a label such as `paypal`). `<page>` starts at 1. For a personal account, use its `file_prefix` from `accounts sync-list`, which puts the file in the `personal/` subfolder: `sync/inbox/YYYY-MM-DD/personal/1111_1.json`.

   The result is often:

   ```json
   {"format":"csv","csv":"id,account_id,date,amount,direction,currency,name,merchant_name,description,pending,category\n...","row_count":N,"next_cursor":"..."}
   ```

   Card results use a `liability_id` column instead of `account_id` and may omit `category`. Save them verbatim. You may add a `_query` object (`account_ids`, `date_from`, `date_to`) so an account that returned no rows still closes out pending transactions in that window. One file may contain several accounts; the importer groups them.

3. Pagination: the tool says pass `cursor` alone to page. The next call is only:

   ```json
   {"cursor": "<next_cursor>", "limit": 500}
   ```

   Do not repeat `account_ids` or the dates on later pages. Write page 2, page 3, and so on, until `next_cursor` is null or empty. Do not drop pages and do not edit the CSV text.

## Daily run checklist

1. Run `bin/hpgbooks accounts sync-list --json`. For each account it lists (business and personal), call its `tool` with `date_from` = today minus 10 days, `date_to` = today, and `limit` 500.
2. Save page 1 verbatim as `sync/inbox/YYYY-MM-DD/<file_prefix>_1.json` (business files at the top of the day folder, personal ones under `personal/`).
3. While `next_cursor` is set, call again with only `{"cursor": "<next_cursor>", "limit": 500}` and save the next page.
4. Import the directory. It reads the business files and the `personal/` subfolder; each account's scope decides how its rows are classified, and the output shows rows per scope:

   ```bash
   bin/hpgbooks import sync/inbox/YYYY-MM-DD/
   ```

   An account id the database does not know is skipped and named in the output. Register it with `accounts discover` (below), then import again.

5. Refresh the WHMCS billing copy. This always runs after the Finance import, so the PayPal reconciliation uses the same day's ledger (see [Daily WHMCS sync](#daily-whmcs-sync)):

   ```bash
   bin/hpgbooks whmcs sync
   ```

6. Refresh posted balances from Finance. Call `finance_list_accounts` with `class` `cash`, then `liability` (and `investment` if personal investment accounts are synced). Save the results as `sync/inbox/YYYY-MM-DD/accounts/finance_list_accounts.json` (in the `accounts/` subfolder, which `import` does not read; a JSON list of the tool results is fine). For every sync-enabled account of both scopes, record that row's `current_balance` (do not use `available_balance`). On a card or loan, `current_balance` is the amount owed and is entered as a positive number. The as-of date is today, because `current_balance` is the posted balance:

   ```bash
   bin/hpgbooks balances set <last4-or-label> --balance <current_balance> --as-of YYYY-MM-DD --source finance
   ```

   Run it once per account, business and personal; `balances set` accepts any registered account. Skip excluded accounts. Each `balances set` appends an anchor and an audit row; it does not edit transactions.

   Instead of one `balances set` per personal account you can run `bin/hpgbooks accounts discover sync/inbox/YYYY-MM-DD/accounts/finance_list_accounts.json --as-of YYYY-MM-DD`. It records a finance anchor for every non-excluded account in the file (skipping one already recorded with the same date and amount), registers any new account as personal, and never changes an existing account's scope.

7. Look at what still needs a human:

   ```bash
   bin/hpgbooks review --year YYYY
   bin/hpgbooks transfers
   bin/hpgbooks reconcile --year YYYY
   bin/hpgbooks pnl --year YYYY --by month
   bin/hpgbooks personal review
   bin/hpgbooks personal transfers          # ambiguous personal transfer pairs to confirm
   bin/hpgbooks personal budgets --alerts
   ```

## New personal accounts

1. Save the `finance_list_accounts` result (all classes) and preview: `bin/hpgbooks accounts discover <file> --dry-run`. The table shows last 4 only.
2. Register: `bin/hpgbooks accounts discover <file>`. New accounts become `personal` (use `--scope` for something else); fix any with `bin/hpgbooks accounts set-scope <last4> business|excluded`.
3. `bin/hpgbooks accounts sync-list --scope personal` now includes them.

### History backfill for a newly discovered personal account

Pull from the start of the period you keep books for, or the earliest date Finance has, in date windows (for example one calendar month per request, `limit` 500, paging with `{"cursor": ..., "limit": 500}` only) so no window hits the page limit unnoticed. Add a `_query` object (`account_ids`, `date_from`, `date_to`) to each saved page so empty windows still close out pending rows. Save the pages under `sync/inbox/backfill-<last4>/personal/<last4>_<n>.json` and run `bin/hpgbooks import sync/inbox/backfill-<last4>/`, then `bin/hpgbooks personal reclassify`. The import is idempotent, so overlapping windows are fine.

The importer is idempotent. Importing the same file again reports `unchanged` and does not duplicate rows. A pending transaction that disappears from a later pull is marked superseded. If a posted transaction in the new file matches it (same account, amount equal or within 20%, date within 7 days), a manual classification is copied onto the posted row.

`needs_review` is the queue of genuinely ambiguous items (IRS payments, card payments that matched no card, unknown merchants). Fix one with:

```bash
bin/hpgbooks classify <id-prefix> --tag general --category "Software & Licenses" --note "confirmed" --rule
```

`--rule` stores a rule and applies it to the other non-manual rows that match.

## Daily WHMCS sync

Only when `features.whmcs = true` in the config. `hpgbooks whmcs sync` refreshes the WHMCS billing copy. It needs nothing staged in `sync/inbox/`: it opens the SSH tunnels itself (the ports and key from `[whmcs]` in the config), reads every configured brand read-only, writes only the `whmcs_*` tables in the encrypted database, and closes the tunnels it opened. A tunnel that is already open is reused and left open.

Run it once a day, after the Finance MCP import so the PayPal reconciliation sees the same day's ledger:

```bash
bin/hpgbooks whmcs sync      # exit status 1 if any brand failed
bin/hpgbooks whmcs status    # last good sync per brand and the recent log
bin/hpgbooks whmcs reconcile # PayPal: matched, WHMCS only, PayPal only, per month
```

If a billing server's firewall lets in only some addresses, a tunnel can time out. The command retries each tunnel up to 12 times, 5 seconds apart, and a brand that still fails is logged with its error while the other brands go ahead. Running it again later is safe: the sync is a full refresh keyed by (brand, WHMCS id).

Placeholder credit is not imported. A `tblcredit` row at or above `whmcs.placeholder_credit_cents` ($1,000,000 by default) (either sign) is skipped, and a client or invoice credit that large is stored as 0. The sync output shows `skipped_placeholder_credit=N` when anything was skipped, and the sync log records the counts under `placeholder_credit`. Because the sync is a full refresh, rows imported before this rule are removed on the next run.

Nothing in the sync output, the sync log, or the audit log contains customer names, emails, or the MySQL password.
