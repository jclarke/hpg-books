# WHMCS billing (optional)

For hosting businesses that bill with [WHMCS](https://www.whmcs.com/). HPG Books copies the billing tables of one or more WHMCS installs into the encrypted database, read-only, and reports revenue, MRR, churn, refunds, collections, a payment-gateway reconciliation against the ledger, and (optionally) per-server margins. It is off by default; with it off the commands exit with a message, the `/api/whmcs/*`, `/api/margins/*`, and `/export/whmcs/*` routes answer 404, and the UI hides the pages.

## Turn it on

In `config/local.toml`:

```toml
[features]
whmcs = true
# margins = true          # server margins; defaults to the whmcs value

[whmcs]
ssh_user = "hpgbooks"                 # SSH user on each billing server
ssh_key = "~/.ssh/hpbooks_whmcs"     # private key for the tunnels
mysql_user = "books_ro"              # read-only MySQL user
placeholder_credit_cents = 100000000 # credit at or above this is a placeholder, not money
# revenue_category = "Revenue - Sales"   # default: the first revenue category
paypal_account_role = "paypal"       # account role used by the PayPal reconciliation

[[whmcs.brands]]
name = "ExampleHost"                 # shown in reports and the brand picker
server = "billing.example.com"       # SSH host
database = "whmcs"                   # MySQL database on that host
port = 3307                          # local tunnel port; brands on one server share a port
secret = "whmcs.secret"              # password file next to the database
```

Restart the web server after changing the config.

## Source setup (on each WHMCS server)

1. A read-only MySQL user with column-level `SELECT` grants on only the columns HPG Books reads. The list is `COLUMNS` in `hpgbooks/whmcs.py` (clients, products, services, addons, invoices and items, payments, cancellation requests, credit, currencies, servers). Password hashes, card and bank fields, addresses, phone numbers, notes, and security answers are never requested; the importer asks for an explicit column list intersected with what `information_schema` shows, never `SELECT *`.
2. An SSH user that can open a local forward to `127.0.0.1:3306` with the key in `whmcs.ssh_key`.
3. The MySQL password in a file next to the database (`data/whmcs.secret` by default), mode 600. A looser mode is refused. It is read only to pass to the driver and never printed or stored.

## Sync

```bash
bin/hpgbooks whmcs sync                       # all brands; opens and closes the SSH tunnels itself
bin/hpgbooks whmcs sync --brand ExampleHost --no-tunnel   # use a tunnel that is already open
bin/hpgbooks whmcs status                     # last sync per brand and the recent log
```

The sync is a full refresh upserted by (brand, WHMCS id), so running it twice changes nothing and rows deleted in WHMCS disappear. A port that already accepts connections is reused and left open; a tunnel the sync opened is closed. Tunnels retry 12 times, 5 seconds apart, and one failing brand does not stop the others (exit status 1 if any failed). Each run writes `whmcs_sync_log` rows and an audit row per brand. Credit rows at or above `placeholder_credit_cents` are skipped and counted.

Run it daily after the aggregator import so the reconciliation sees the same day's ledger.

## Reports

```bash
bin/hpgbooks whmcs revenue --by year [--plans]
bin/hpgbooks whmcs mrr [--trend --months 0]
bin/hpgbooks whmcs churn --from 2026-01-01 --to 2026-09-30 [--plans]
bin/hpgbooks whmcs refunds [--largest]
bin/hpgbooks whmcs dunning [--aging]
bin/hpgbooks whmcs reconcile [--rows | --gateways] [--window 3]
```

Every report takes `--brand`, `--from`/`--to`, and `--format table|csv`. The same data is on the `/whmcs` pages, each table with a CSV link.

- **Payments** are `tblaccounts` rows: gross in, gateway fees, refunds out; cash basis by payment date. Each payment is split across the plans on its invoice in proportion to the items.
- **MRR** counts Active services with a recurring cycle, normalized to a month (quarterly ÷ 3, annual ÷ 12, and so on). Free, one-time, pending, suspended, fraud, and completed services count as zero. ARR = MRR × 12; ARPU = MRR ÷ customers with an active service.
- **MRR trend** is an estimate: each month end counts services registered by then and not yet ended, at today's prices (WHMCS keeps no price history).
- **Churn**: a service churns in the month it ends (termination date, else the latest cancellation request, else the paid-through date); a customer churns when their last paid service ends.
- **Collections**: unpaid invoices aged from the due date. "Collectible" means the client still has an Active or Suspended service.
- **Reconciliation**: WHMCS PayPal payments are matched to rows of the account with role `paypal` (by PayPal transaction id, else net or gross amount within ±`--window` days). `[[whmcs.bank_sides]]` adds per-gateway bank totals (for example card processor deposits in the operating account). The ledger is never changed.

## Privacy

Customer names, emails, companies, and domains are stored only in the encrypted database. Reports, the sync log, the audit log, CLI output, and CSV exports identify customers by brand and client number. Names appear only on the customer search and customer pages, which need a signed-in session on any non-loopback host. Custom addon names are reported as `Addon: custom`, and email addresses inside cancellation reasons are masked.

## Server margins

With `margins` on, `/whmcs/margins` and `bin/hpgbooks margins` show each server's monthly cost, the services it carries, their monthly revenue (read live from the synced services), and the margin. Costs, the server-to-service mapping rules (service id, domain, WHMCS server, product group, brand; first match wins), and overhead lines are maintained data, editable on the page (audited) or loaded from a local JSON file:

```bash
bin/hpgbooks margins seed --file data/margins/seed.json   # idempotent; keep this file out of git
bin/hpgbooks margins show [--json]
bin/hpgbooks margins whatif --merge A B | --retire X
```

## Tests

The WHMCS tests use a fake source with invented customers (`tests/whmcs_fake.py`) and never open a tunnel or a MySQL connection. `HPGBOOKS_TEST_WHMCS=off .venv/bin/pytest` runs the suite with the feature off.
