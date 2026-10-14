# Personal mode

HPG Books keeps two sets of books in one encrypted database. **Business** mode is the business ledger and P&L. **Personal** mode covers personal bank, card, loan, and investment accounts and covers what a personal finance app does: net worth, spending, cash flow, budgets, recurring charges and subscriptions, bills, goals, a review queue, and a monthly summary.

The header has a **Business | Personal** toggle. It is not the business switcher (All plus each configured business), which picks a business inside Business mode and is shown only there.

## Account scope

Every account has a scope:

| Scope | Shown in |
|---|---|
| `business` | Business mode only |
| `personal` | Personal mode only |
| `excluded` | Neither mode; only on the accounts settings screen |

Transactions carry no scope of their own. A row's scope is its account's scope, read through the join on every query, so moving an account between modes re-scopes every report without rewriting ledger rows. The confirmation dialog says how many transactions move, and the move is audited (`account_scope`).

Each account also has a `class` (cash, credit card, investment, loan or mortgage, other), an include-in-net-worth flag, a daily-sync flag, and an optional display name. `type` keeps its old meaning (cash = asset balance, liability = amount owed); a loan is `type` liability, an investment `type` cash.

```bash
bin/hpgbooks accounts                                  # business accounts, same output as before
bin/hpgbooks accounts list [--scope personal] [--json]
bin/hpgbooks accounts set-scope <id|last4|name> <business|personal|excluded>
bin/hpgbooks accounts set-class <account> <cash|liability|investment|loan|other>
bin/hpgbooks accounts rename <account> "Household checking"
bin/hpgbooks accounts discover saved_finance_list_accounts.json [--scope personal] [--dry-run] [--as-of YYYY-MM-DD]
bin/hpgbooks accounts sync-list [--scope personal] [--json]
```

`accounts discover` reads a saved `finance_list_accounts` result (the tool-result wrapper with CSV text, a plain JSON list, or `{"accounts": [...]}`), accepting `id`, `name`/`official_name`, `mask`/`last4`, `institution`, `class`/`type`/`subtype`, and `current_balance`. Unknown accounts are registered as personal (or `--scope`). An existing account keeps its scope; its name, last 4, and class are filled in only if blank. `current_balance` becomes a `finance` balance anchor dated `--as-of` (default today), skipped if the latest anchor already has that date and amount, so running it twice changes nothing. Only the last 4 digits of a number are kept or printed; a long digit run inside a provider name is cut to its last 4.

An account that is not registered is never created by `import`; its rows are skipped and the import prints the account id with a hint to run `accounts discover`.

## Navigation

Personal sidebar: Dashboard, Net worth, Accounts, Transactions, Spending, Cash flow, Budgets, Recurring, Bills, Goals, Monthly summary, Review, Categories & rules, Settings. URLs are under `/personal/...`; the API under `/api/personal/...`; CSV exports under `/export/personal/<table>.csv`; the CLI under `hpgbooks personal ...`.

The mode comes from the path. On load `?mode=business|personal` wins; with no `?mode=` on the root page the last mode (localStorage `hpgbooks.mode`) is used; the default is business. Switching keeps the date range and goes to the matching page (Transactions, Accounts, Review, Settings, Calendar → Bills, Rules → Categories) or that mode's dashboard. Personal pages carry a violet accent and a "Personal" label. Every API request sends `mode=`; a business route asked for `mode=personal`, or a personal route for `mode=business`, answers 404, and a transaction id from the other scope is 404 on detail and edit routes. The command palette searches only the active mode.

## Sign conventions

- `amount_cents` keeps the ledger sign: positive is money into that account. A card purchase is negative; a card payment received is positive.
- Balances: cash and investment balances are what is held; card and loan balances are what is owed (positive). Net worth = cash + investments + other assets − cards − loans.
- **Spending** is outflow in expense categories, shown as a positive number. A refund in an expense category nets against that category. Transfers and owner-draw funding are never spending.
- **Income** is inflow in income categories (Paycheck, Interest & dividends, Refunds & reimbursements, Other income), plus inflows still Uncategorized, which count as "Other" until reviewed.
- **Owner draws** are funding: a separate source line, never earned income. The savings rate is shown with and without them: `(income − spending) / income` and `(earned − spending) / earned`.

## Classification

Personal rows use their own engine (`hpgbooks/personal/classify.py`) and tables (`p_categories`, `p_rules`, `p_classifications`, `p_splits`, `p_merchants`, `p_tags`, `p_txn_tags`); business rules never run on them and personal rules never run on business rows. Order: manual > transfer pair > first matching personal rule (priority, then id) > the merchant's default category > Uncategorized. A manual category is never replaced by rules, transfer detection, re-import, or reclassify. A pending row that posts under a new id keeps its manual category and tags.

The review queue holds rows that are Uncategorized or below 0.6 confidence (manual and transfer rows never). Suggestions come from how the same merchant was categorized before.

Starter categories and rules are seeded by `bin/hpgbooks init` and `bin/hpgbooks personal seed-rules` (idempotent). Rules name only well-known public merchants (streaming services, grocery chains, fuel brands, utilities, …) and generic bank wording (PAYROLL / DIRECT DEP, CARD PAYMENT, MORTGAGE). Venmo, Zelle, and Cash App are placeholders that land in review.

Splits must add up exactly to the row amount (two or more parts). Merchant names are cleaned into a key (processor prefixes like `SQ *`, marketplace codes like `AMZN Mktp US*2K4…`, store numbers, ACH `DES:` descriptors, phone numbers, and a trailing city/state are removed); a rename is stored once per key in `p_merchants` and applies to every matching row.

## Transfers

Two personal rows pair when they are in different personal accounts, have opposite signs and the same size, are within 3 days, and at least one looks like a transfer (a transfer-category rule matched it, or its text says transfer/payment/deposit/contribution). Both legs become a Transfers category (Credit card payment, Savings transfer, Loan payment, Internal transfer) and count as neither spending nor income, while still moving balances. A card payment from checking is a transfer; the card's purchases are the spending.

A leg with more than one candidate is **ambiguous**: it is listed (`hpgbooks personal transfers`, and the Transfers section of the API) and not applied until confirmed (`hpgbooks personal transfers confirm <out_id|in_id>`) or rejected. Detection is deterministic: the same ledger gives the same pairs.

Loans: a payment to a mortgage or auto loan is spending on the paying side (Housing / Mortgage, Transportation / Auto payment); only the loan account's credit is a transfer. Nothing is counted twice.

## Owner draws and the two mortgages

Business mode does not change: owner draws stay `owner_draw` (below the line), transfers stay transfers, and nothing in personal mode adds to or subtracts from business P&L or totals. Business classifications cannot be edited from personal mode.

On the personal side:

1. **Draw with a personal deposit.** A business-account row the business books tag `owner_draw` (or `transfer`) and a personal deposit of the same amount within 3 days are one transfer. The deposit becomes **Owner draws** funding; the business row is not counted again.
2. **Draw with no personal leg** (the receiving account is not linked). The personal view adds one read-only **Owner draws** funding row built from the business row, labeled "paid from business account".
3. **Mortgage paid straight from business checking** (an `owner_draw` whose text is not a transfer, e.g. a mortgage servicer). The personal view adds the funding row and a matching read-only expense row, categorized by the personal rules (Housing / Mortgage). Personal cash is unchanged, which is right: the money never touched a personal account.
4. **Mortgage paid from a personal account after a draw.** The draw is funding (case 1) and the mortgage payment is spending. Counted once each.

Pairing runs again after every import, so a business draw that arrives after its personal deposit (or the reverse) still pairs. These rows have ids `biz:<business id>` and `biz:<business id>:spend`.

`hpgbooks personal reconcile --month YYYY-MM` (and `/api/personal/reconcile`) ties earned income + owner draws received − spending + transfers net to the change in personal balances. Every personal row lands in exactly one bucket; the listed differences are pending rows, loan credits (spent on the paying side), unmatched transfer legs, and anchor adjustments. Business-paid funding and spending are shown as a memo and are not part of personal balances.

## Recurring charges and subscriptions

`hpgbooks/personal/recurring.py` is a pure function, run on every API request and by `hpgbooks personal recurring detect` (which also stores the series in `p_recurring`).

- Rows are grouped by merchant key and direction; transfers and funding are skipped. Within a merchant, amounts are clustered (a new cluster starts more than 35% above the last amount), so a price change stays one series and two plans split.
- Cadence is the median gap: weekly 5–9 days, biweekly 12–16, monthly 25–36, quarterly 80–100, annual 340–390, with two thirds of the gaps inside the band (±2 days). Weekly, biweekly, and monthly need 3 occurrences; quarterly and annual need 2.
- Next expected date is the last date plus the cadence (calendar months for monthly and longer). Typical amount is the median of the last three. Monthly equivalent: weekly × 52/12, biweekly × 26/12, quarterly ÷ 3, annual ÷ 12.
- **Price change**: the latest amount differs from the one before by more than 3% or $1, for a series whose previous (up to 6) amounts were steady. Groceries and utilities vary every month, so they are not flagged.
- **May be cancelled**: no charge for more than 1.5 cadences. **New**: first seen within 60 days. **Possible duplicate**: two active series with the same merchant key. A charge cannot show whether a subscription is used, so no "unused" flag is offered.
- Kind: income for inflows; subscription for the Subscriptions and Entertainment groups; bill otherwise.
- You can confirm, ignore, mark cancelled, or change the cadence (`p_recurring`, audited `personal_recurring`). Ignored and cancelled series leave the totals.

**Bills** lists expected dates for the next 30–60 days from these series: `paid` (a matching charge already posted this month), `due`, `late` (up to 3 days past), and `overdue` (expected more than 3 days ago with no charge since). Totals due this week and this month are outflows only. It is an estimate.

## Budgets

Monthly budgets per category or per group. A budget without a month is the default for every month; a month row overrides it. Progress is spending (refunds netted) against the available amount; warning at 80%, over above 100%. Pace = spending so far ÷ days elapsed × days in the month. **Rollover** carries each earlier month's unspent (or overspent) amount, up to 12 months back, starting with the first of those months that has spending in that line. "Copy last month" gives a month its own copy of every budget in effect the month before; "3-month average" suggests the average of the three full months before, rounded up to whole dollars. `hpgbooks personal budgets --alerts` prints lines at 80% or more. Budgets run on calendar months.

## Net worth and balance history

Balances come from balance anchors (Finance `current_balance` via `balances set` or `accounts discover`, or a statement) plus ledger activity, the same idea as `hpgbooks/balances.py`, extended to any date:

- With an anchor on or before the date: that anchor plus every active row after it through the date (pending included, since a posted anchor does not contain them).
- Before the first anchor: the first anchor walked back by the posted rows in between.
- With no anchor: the running sum of imported activity.

History is weekly (3M, 6M) or month-end (1Y, YTD, All). Investment accounts change with the market, which the ledger does not record: between anchors their line carries the earlier anchor forward plus contributions and steps at the next anchor. An account whose newest anchor and newest transaction are both more than 7 days old is labeled **stale** and counted on the page, so a number is never silently old. Accounts with include-in-net-worth off, and closed accounts, are left out. `p_net_worth_snapshots` was not needed and is not created.

## Goals

A goal has a target, an optional date, and either a linked personal account (progress = its balance) or a manual "saved so far". Needed per month = remaining ÷ months left. On track when the planned monthly contribution (or, for a linked account with none planned, its average change over the last 90 days) covers that.

## Monthly summary

`/personal/review-month` and `hpgbooks personal summary --month YYYY-MM [--format table|csv|json]`: income, spending, savings rate against the prior month and the 12-month average, the five largest category changes, the biggest transactions, new and re-priced subscriptions, budget results, net worth change, and a short plain-English list built from templates (no AI calls). The page prints cleanly.

## Data and security

All personal data lives in the same SQLCipher database (tables prefixed `p_`), behind the same key, sign-in, host allowlist, CSRF, and Origin checks. Every personal write is audited with an action prefixed `personal_` (`personal_classify`, `personal_split`, `personal_tags`, `personal_note`, `personal_rule_create`/`_enable`/`_disable`, `personal_category`, `personal_budget`, `personal_goal`, `personal_recurring`, `personal_transfer_review`, `personal_merchant_rename`) plus `account_scope`, `account_register`, `account_class`, `account_rename`, and `account_settings`. The audit page shows each mode its own rows; a scope move shows in both modes it touches. The browser stores only the mode flag (and the existing theme and saved-filter preferences); no balances or merchants are cached in localStorage. Exports are built on request and never stored.

## Decisions and known limits

- **Accounts screen lives in Personal → Settings.** It lists every account of every scope (business ones included) so the business Settings page stays exactly as it was; the CLI (`accounts set-scope`) works from either mode.
- **No table rebuild for accounts.** `type` keeps cash/liability (the sign rule); the new `class` column carries loan, investment, and other. The migration only adds columns, so foreign keys and ids are untouched.
- **Personal routes need `mode=personal`.** A request with no mode is business, as for old clients and the CLI, so a personal route without `mode=personal` is 404.
- **`accounts discover` records anchors for every non-excluded account in the file**, business ones too, which is the same thing the daily `balances set ... --source finance` step does. Skip the business ones by removing them from the file if you prefer to set those by hand.
- **Uncategorized inflows count as "Other" income** until reviewed; uncategorized outflows count as Uncategorized spending.
- **Loan payments are spending** on the paying side; principal and interest are not split (the ledger does not carry the split).
- **Investment history** between anchors is anchors plus contributions (no market prices).
- **Recurring "unused"** cannot be known from charges; only "may be cancelled", "possible duplicate", and "new" are flagged.
- **Budget month** is the calendar month; a custom start day is not implemented.
- **Personal history before the business opening date** (2026-01-01, `hpgbooks/balances.py`) imports and reports normally. That date applies only to business balances.
