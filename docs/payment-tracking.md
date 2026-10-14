# Minimum payments, due dates, and APR

The Accounts page (business and personal) lists every credit card and loan with its minimum
payment due, due date, APR, autopay, and where the figures came from. A summary on top shows
the minimums due in the next 30 days (and by date) and anything overdue.

## Where the figures come from (highest trust first)

| source | meaning |
| --- | --- |
| `manual` | typed in on the Accounts page or with `accounts set-payment` |
| `statement` | parsed from a statement PDF by an importer (`scripts/import_cfna.py`, `scripts/import_applecard.py`) |
| `issuer site` / `finance` | the issuer's site, or the Finance feed (`next_payment_due_date`, `minimum_payment_amount`, `interest_rate_percentage` in `accounts discover`, when the provider sends them) |
| `sheet` | a spreadsheet you keep (exported as JSON or CSV), loaded with `scripts/sync_payment_sheet.py` |
| `inferred from payment history` | loans only: typical amount and day of month from the last four payments |

A lower-trust source never replaces a higher one. It only fills blanks, and a disagreement is
printed and written to the audit log (`payment_sync_conflict`). The one exception: a lower-trust
row that is **newer** replaces the old one when the old due date had already passed on that day
(the old billing cycle is over). Manual entries are replaced only by manual entries.

## Paid, overdue, and rolling

* A **payment** is a credit on the card or loan account classified as a transfer (Credit card
  payment / Loan payment, or a business "transfer"), or a bank debit matching the account's
  `payer_pattern` (loans and some store cards are paid from a bank account that has no link to the card
  row). Payments dated on or before the date the figures were taken belong to the cycle before.
* When the due date has passed and the payments since the previous due date add up to at least the
  minimum, the row is paid and rolls to the same day next month, flagged **estimated**, until a
  statement, the sheet, or you refresh it. Several months can roll in one go.
* With no such payment it is **overdue** (red). Estimated dates (inferred, or rolled) get 3 days of
  grace and show amber "due now".
* A statement-backed card whose payments only arrive with the next statement also counts as paid
  when the balance has fallen by at least the minimum since the statement balance.
* **Mark paid** (page button or `--paid`) records that this cycle was paid even when no payment
  row shows it. Entering a new due date clears it.
* Due within 7 days: amber. Minimum 0: "No payment due".

## Commands

```
bin/hpgbooks accounts set-payment 1234 --min 85 --due 2026-10-01 --apr 24.99 --source statement
bin/hpgbooks accounts set-payment 5678 --autopay no --notes "pay on the 15th"
bin/hpgbooks accounts set-payment 9012 --paid               # this cycle's payment was made
bin/hpgbooks accounts payments [--mode business] [--json]    # soonest first, with the 30-day summary
bin/hpgbooks accounts infer-payments [--dry-run]             # loans from payment history
```

The statement importers `scripts/import_cfna.py` and `scripts/import_applecard.py` also store the
newest statement's minimum, due date, and APR (source `statement`, as of the closing date).

## The weekly sheet routine

`scripts/sync_payment_sheet.py ROWS.json|ROWS.csv --as-of YYYY-MM-DD [--dry-run] [--conflicts-out FILE]`

Rows are objects (or CSV columns, any case) with `account` and/or `last4`, `balance`, `min`, `due`,
`apr`, `autopay`, `notes`, `unverified`, `statement_balance`, `as_of`, `skip`. Accounts are matched by
last 4, then by name; unmatched rows (for example another person's card) are listed and skipped.
`balance` is only compared with the books and reported when it differs by $1 or more. An empty `min`
with `unverified: yes` stores the due date and flags the minimum as unverified. Re-running with the
same rows changes nothing.

Keep the rows file out of git (it holds real balances).

## Data and migration

Table `account_payment_terms` (migration 10, one row per account, created automatically the first
time a writing command opens the database; the table is empty until filled). Nothing here writes to
transactions, classifications, or balance anchors.
