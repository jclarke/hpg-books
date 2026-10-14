# HPG Books

> **Requires Grok Bot.** HPG Books is meant to be set up and run inside Grok Bot: the bot installs it on its own cloud computer, and your transactions come in through Grok Bot's Finance connector.

A local, encrypted bookkeeping app for a small business and a household. It imports bank and card transactions, classifies them with rules you control, and produces a profit-and-loss statement, cash flow, vendor and tax-prep reports, from the command line or a web UI on your own machine. A separate **Personal** mode in the same database covers personal accounts: net worth, spending, budgets, recurring charges, bills, and goals.

Nothing leaves the machine. The database is SQLCipher-encrypted, the web server listens on `127.0.0.1` only, and the app makes no third-party requests. The product name shown in the UI comes from your config (default "Books").

## How it works

```text
Grok Bot template ──> Grok Bot installs it ──> Connections ──────────────────────> App + routines run ──> You use it
                      on its own cloud          Finance connector (required)        syncs, bills, health,   web app via Tailscale,
                      computer                  Monarch (optional)                  update check            or ask your bot
                                                Tailscale (optional)
```

1. **Start from the [Grok Bot template](https://x.ai/bot/gHLmbUpkuYNbv0F-J2JwC).** It brings the app's skills and routines.
2. **Grok Bot installs it on its own cloud computer**, not your laptop. It creates the encrypted database and asks you a few questions.
3. **Connections.** The Grok Bot **Finance connector** is where bank and card transactions and balances come from (required for automatic sync). **Monarch** is optional, for accounts the Finance connector doesn't cover. **Tailscale** is optional, for private access to the web app from your devices.
4. **The app and its [routines](#grok-bot-routines-to-keep-it-updated) run**: syncs, payment due dates, a health check, and the update check.
5. **You use it**: open the web app over Tailscale, or just ask your bot.

**Without Grok Bot?** A manual, standalone install is possible ([Install](#install)), and the app can import CSV and statement files you download yourself ([docs/importers.md](docs/importers.md)). But automatic data sync relies on Grok Bot's Finance connector, so without Grok Bot you bring the data in by hand.

## Grok Bot template

**[Get the Grok Bot template](https://x.ai/bot/gHLmbUpkuYNbv0F-J2JwC)**: the fastest way to start. It sets up a Grok Bot with this app, the skills below, and the [routines that keep it updated](#grok-bot-routines-to-keep-it-updated). Grok Bot installs everything on its own cloud computer, not your laptop, and asks you a few questions along the way.

Skills shipped with the template:

- **books-getting-started**: the first install and setup, including the questions in step 2 below.
- **daily-books-sync**: the daily business books sync.
- **daily-personal-books-sync**: the daily personal books sync.

## Use it with Grok Bot

Open source · runs on your own Grok Bot cloud computer. HPG Books is a private bookkeeping app: business books, personal finances, or both, in one encrypted database.

Starting from the [Grok Bot template](https://x.ai/bot/gHLmbUpkuYNbv0F-J2JwC) handles step 1 and asks the step 2 questions for you. Without it, from a plain chat:

**1. Ask Grok Bot to install it.** Paste this into a chat:

```text
Install the books app from https://github.com/jclarke/hpg-books on your computer and set it up for me.
```

Grok Bot clones the repository, creates the database key (it never shows the key or puts it in chat), writes your config, creates the database, starts the web app, and runs the tests. The checklist it follows is in [Installing with an AI assistant](#installing-with-an-ai-assistant).

**2. Answer a few questions.** Before it creates the database, Grok Bot asks for:

- Your company name, shown in the sidebar and on P&L titles.
- Your timezone, so scheduled syncs and update checks run at sensible times.
- Business, personal, or both. Each mode is a switch in the config.
- Your businesses and categories, if you keep business books. These are fixed when the database is created.
- Which bank and card accounts to bring in, from the Finance connector you have linked in Grok Bot. New accounts start as personal; any of them can be moved to business.

**3. Use it day to day.** Just ask:

| Ask Grok Bot | What happens |
|---|---|
| "Sync my books" | Pulls the last 10 days for every linked account through the Finance connector, imports it (re-imports never duplicate), and refreshes balances. See [sync/README.md](sync/README.md). |
| "Show me my P&L for September" | Profit and loss by month or by business, with owner draws below the line. |
| "What's uncategorized?" | The review queue, largest first, with suggestions. A fix can be saved as a rule so the same merchant is handled next time. |
| "When are my cards due?" | Minimum payments, due dates, and APRs for every card and loan, soonest first. See [docs/payment-tracking.md](docs/payment-tracking.md). |
| "How's my spending this month?" | Personal spending by category, budgets, and recurring charges. See [docs/personal-mode.md](docs/personal-mode.md). |
| "Export a PDF report" | The P&L as a PDF; CSV and XLSX work too. |
| "Check for updates" | Runs `bin/hpgbooks update check` and tells you what is new. It updates only when you say so. See [Updates](#updates). |

Grok Bot can also run the syncs, a health check, and the update check on a schedule; see [Grok Bot routines to keep it updated](#grok-bot-routines-to-keep-it-updated).

**4. Open the web app.** On the computer where the books run, go to <http://127.0.0.1:8765>. The **Business | Personal** toggle in the header switches modes. To open it from your laptop or phone, ask Grok Bot to "set up Tailscale for my books": the app stays on `127.0.0.1`, Tailscale forwards a private tailnet port to it, and a passphrase is required. See [Remote access with Tailscale](#remote-access-with-tailscale-optional).

The app itself makes no network calls for ledger data: Grok Bot saves what the Finance connector returns as files on that computer, and HPG Books imports those files into the encrypted database.

## Grok Bot routines to keep it updated

The [Grok Bot template](https://x.ai/bot/gHLmbUpkuYNbv0F-J2JwC) schedules these for you. Without it, ask Grok Bot to set each one up as a routine. Times are suggestions in your own timezone, spread out so the jobs don't overlap.

| Routine | Suggested schedule | What it does |
|---|---|---|
| Daily business books sync | Weekdays, about 7:20am | Pulls recent transactions for the business accounts through the Finance connector, imports them (re-imports never duplicate), refreshes balances, applies your rules, and reports what still needs a decision. Uses the **daily-books-sync** skill; see [sync/README.md](sync/README.md). |
| Daily personal books sync | Every day, about 7:43am | The same for the personal accounts: new transactions, balances, net worth, and anything new in the review queue. Uses the **daily-personal-books-sync** skill. |
| Daily payment due dates update | Every day, about 8:12am | Refreshes minimum payments, due dates, and APRs for cards and loans so the Bills page stays current. See [docs/payment-tracking.md](docs/payment-tracking.md). |
| Weekly Monarch pull | Sundays | For accounts the Finance connector doesn't cover: pulls a Monarch export, imports only the rows the ledger is missing, and updates those accounts' balances. See [docs/importers.md](docs/importers.md#monarch-export-scriptsimport_monarchpy) and [docs/manual-balances.md](docs/manual-balances.md). |
| Books app health check | Every 30 minutes | Checks that the web server answers on `127.0.0.1:8765`. If it is down, restarts it with `bin/hpgbooks-web start`, and restarts Tailscale when remote access is set up. Quiet when everything is fine. |
| Update checker | Every day | Runs `bin/hpgbooks update check` and messages you only when there is a new version. It never applies an update itself. See [docs/update-routine.md](docs/update-routine.md). |

## Screenshots

Every screenshot below comes from a demo ledger with invented data ("Northwind Hosting Co.", three made-up businesses, and a fictional household). None of it is real.

**Business mode**

| | |
|---|---|
| <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/01-business-dashboard-dark.png"><img src="docs/screenshots/01-business-dashboard.png" alt="Business dashboard"></picture><br>**Dashboard**: revenue, expenses, and net income by month, compared with the prior period. | ![Profit and loss by month](docs/screenshots/02-business-pnl-by-month.png)<br>**Profit & Loss** by month. Click any figure to see the transactions behind it. |
| ![P&L by business](docs/screenshots/03-business-pnl-by-business.png)<br>**P&L by business**: each business side by side in one ledger. | ![Review inbox](docs/screenshots/04-business-review-inbox.png)<br>**Review inbox**: unclassified rows, largest first, with suggestions and "Accept + rule". |
| ![Transactions](docs/screenshots/05-business-transactions.png)<br>**Transactions** with inline business and category classification. | ![Schedule C-style summary](docs/screenshots/06-business-schedule-c.png)<br>**Schedule C-style summary**, a year-end bookkeeping aid. |
| ![Expenses by vendor](docs/screenshots/17-business-expenses-by-vendor.png)<br>**Expenses by vendor**: top merchants by spend, with CSV, XLSX, and PDF export. | |

**Personal mode**

| | |
|---|---|
| <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/07-personal-dashboard-dark.png"><img src="docs/screenshots/07-personal-dashboard.png" alt="Personal dashboard"></picture><br>**Personal dashboard**: net worth, this month's income and spending, and savings rate. | ![Net worth](docs/screenshots/08-personal-net-worth.png)<br>**Net worth** history from balance anchors plus activity. |
| ![Spending](docs/screenshots/09-personal-spending.png)<br>**Spending** by category, compared with the prior period. | ![Budgets](docs/screenshots/10-personal-budgets.png)<br>**Budgets** with progress, pace, and 80% alerts. |
| ![Recurring and subscriptions](docs/screenshots/11-personal-subscriptions.png)<br>**Recurring & subscriptions**: price changes and possible cancellations are flagged. | ![Goals](docs/screenshots/12-personal-goals.png)<br>**Goals** linked to an account or tracked by hand. |
| ![Debt payoff](docs/screenshots/13-personal-debt-payoff.png)<br>**Debt payoff**: compare saved loan offers with paying the cards as you are. | <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/14-personal-accounts-payment-terms-dark.png"><img src="docs/screenshots/14-personal-accounts-payment-terms.png" alt="Accounts with payment terms"></picture><br>**Accounts**: each card and loan with its minimum payment, due date, APR, and autopay. |
| ![Payments due](docs/screenshots/15-personal-payments-due.png)<br>**Payments due**: minimums due in the next 30 days, by date, and anything overdue. | ![Spending by merchant](docs/screenshots/16-personal-spending-by-merchant.png)<br>**Spending by merchant**: totals, counts, and averages; click a merchant to see its transactions. |

## Features

- **Import** aggregator pulls (JSON or CSV tool results), Capital One CSVs, Apple Card CSVs, store-card statement PDFs, and Monarch exports. Every import is idempotent; pending rows are matched to their posted versions. See [docs/importers.md](docs/importers.md).
- **Classify** with priority-ordered regex rules, one-click "create rule from this", a review inbox with suggestions, bulk edits with undo, and transfer matching between accounts. Manual decisions are never overwritten.
- **Report**: P&L by month or business (with owner draws below the line), P&L by business, cash flow, expenses by vendor, owner draws, a Schedule C aid, and a reconciliation that ties raw activity to net income. CSV, XLSX, and PDF exports.
- **Balances** from statement anchors, account registers with running balances, and vendor merges that never rewrite the ledger.
- **Personal mode**: separate categories and rules, transfer pairing, budgets with rollover, recurring and subscription detection, bills, goals, net worth history, monthly summary. See [docs/personal-mode.md](docs/personal-mode.md).
- **Several businesses** in one ledger, each a tag with its own label and colour.
- **Optional WHMCS integration** for hosting businesses: revenue, MRR, churn, collections, gateway reconciliation, server margins. See [docs/whmcs.md](docs/whmcs.md).
- **Optional remote access** over Tailscale with a host allowlist and passphrase sign-in.
- Audit log of every edit.

## Requirements

- Linux or macOS with Python 3.11+ (`python3 -m venv`).
- Node 20+ and npm, only to rebuild the web UI (the built app is committed).
- Python packages in `requirements.txt` (SQLCipher bindings via `sqlcipher3-binary`, Flask, openpyxl, reportlab, PyMySQL, pytest).
- Optional: `pdftotext` (poppler-utils) for the statement importers; `ripgrep` for `scripts/privacy_scan.sh`.

## Install

```bash
git clone <this repository> books && cd books

# Python
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Web UI (optional: hpgbooks/static/app is already built)
(cd web && npm ci)
bin/build-ui --no-install

# Database key: 64 hex characters in a mode-600 file
mkdir -p ~/.config/hpbooks && chmod 700 ~/.config/hpbooks
( umask 077; python3 -c 'import secrets; print(secrets.token_hex(32))' > ~/.config/hpbooks/key )
chmod 600 ~/.config/hpbooks/key

# Site config (optional; without it you get the generic defaults)
cp config/config.example.toml config/local.toml
chmod 600 config/local.toml
$EDITOR config/local.toml      # product and company name, businesses, categories (decide before init)

# Create the database and start the web UI
bin/hpgbooks init
bin/hpgbooks-web start          # http://127.0.0.1:8765
```

**Back up the key.** Without it the database cannot be opened. Keep a copy somewhere safe and separate from database backups.

`bin/hpgbooks` runs `.venv/bin/python -m hpgbooks`; `bin/hpgbooks --help` lists every command. `bin/hpgbooks-web start|stop|status` runs the server in the background (log and pid in `data/`); `bin/hpgbooks web` runs it in the foreground. The database is `data/hpbooks.db` unless `HPBOOKS_DB` or `[paths] db` says otherwise. Business tags and categories become database constraints at `init`, so set them in the config first. See [docs/configuration.md](docs/configuration.md).

## First steps

```bash
bin/hpgbooks accounts discover finance_list_accounts.json --dry-run   # register accounts from a saved account list
bin/hpgbooks accounts set-scope <id|last4|name> business               # new accounts start as personal
bin/hpgbooks import sync/inbox/2026-10-01/                             # load transactions
bin/hpgbooks review --year 2026                                        # what still needs a decision
bin/hpgbooks classify <txn-id-prefix> --tag general --category "Software & Licenses" --rule
bin/hpgbooks pnl --year 2026 --by month
bin/hpgbooks reconcile --year 2026
```

Most of this is easier in the web UI: the Review page, inline classification on Transactions, and "Create rule from this". The daily pull routine for an agent is in [sync/README.md](sync/README.md).

## Remote access with Tailscale (optional)

The server always binds to loopback. To reach it from your other devices, let Tailscale Serve forward a tailnet port to it, allowlist the host names you will type, and set a passphrase:

```bash
tailscale serve --bg --tcp 8765 tcp://127.0.0.1:8765     # check `tailscale serve --help` for your version
bin/hpgbooks web-passphrase set                           # at least 12 characters; stored as a scrypt hash
bin/hpgbooks-web stop
bin/hpgbooks web --allow-host <machine>.<tailnet-domain> --allow-host <tailnet-ip>   # saves data/allowed-hosts (mode 600); Ctrl-C
bin/hpgbooks-web start                                    # passes the saved hosts on every start
```

You can also edit `data/allowed-hosts` directly (one host per line, mode 600). Each host name signs in separately. With no passphrase set, allowlisted hosts get `503` and the books stay closed. Details: [SECURITY.md](SECURITY.md).

## WHMCS (optional)

Set `whmcs = true` under `[features]` and fill in `[whmcs]` and one `[[whmcs.brands]]` per install in `config/local.toml`, put the read-only MySQL password in `data/whmcs.secret` (mode 600), then `bin/hpgbooks whmcs sync`. Setup, grants, and report definitions: [docs/whmcs.md](docs/whmcs.md).

## Updates

Installs follow the public snapshot repository (`[update]` in the config: `repo`, `remote`, `branch`; defaults `jclarke/hpg-books`, `public`, `main`). A git remote with the `remote` name is used when the clone has one; otherwise the repository URL.

```bash
git remote add public https://github.com/jclarke/hpg-books.git   # once, if the clone lacks it
bin/hpgbooks update check          # exit 0 up to date, 1 update available, 2 error
bin/hpgbooks update apply          # dry run: lists the new commits, changes nothing
bin/hpgbooks update apply --yes    # git fetch + git merge --ff-only
bin/hpgbooks-web stop && bin/hpgbooks-web start   # load the new code
```

What is preserved: everything git ignores, which an update never writes: `config/local.toml`, `config/*.local.*`, `data/` (the database, key, logs, allowlist), keys and secrets, `.venv`, `sync/inbox/`, and exports. `apply` refuses when tracked files have uncommitted changes, when your clone has commits of its own (it only fast-forwards), or when upstream would change a protected path. The built UI in `hpgbooks/static/app` arrives with the update; `--build-ui` also runs `bin/build-ui --no-install` when `web/` changed. If `requirements.txt` changed, run `.venv/bin/pip install -r requirements.txt`.

An install unpacked from a zip (no `.git`) is checked through the GitHub API (`gh` when installed, else `GITHUB_TOKEN` if set) against the revision in `.hpgbooks-revision`; `apply --yes` downloads the branch and overlays it, skipping the same protected paths. Files deleted upstream are not removed from a zip install.

To be told about updates, see [docs/update-routine.md](docs/update-routine.md).

## Tests

```bash
.venv/bin/pytest                                 # Python; temporary database and key per test
HPGBOOKS_TEST_WHMCS=off .venv/bin/pytest          # same, with the WHMCS feature off
(cd web && npm test && npm run typecheck)        # Vitest + tsc
```

Tests never open your real database. `tests/golden/` holds expected output for a fake ledger; regenerate it with `.venv/bin/python tests/make_golden.py` only for an intended change.

## Installing with an AI assistant

If an assistant (Claude Code or similar) sets this up for you, give it this checklist:

1. Clone, create `.venv`, `pip install -r requirements.txt`. Do not run `npm` unless the UI must be rebuilt.
2. Create the key file exactly as above. Never print, log, or commit the key; never paste it into chat.
3. Copy `config/config.example.toml` to `config/local.toml` and ask the user for the company name, businesses, and categories **before** `bin/hpgbooks init`.
4. Run `bin/hpgbooks init`, `bin/hpgbooks-web start`, and check `curl -s http://127.0.0.1:8765/api/session`.
5. Run `.venv/bin/pytest -q` to confirm the install.
6. Never commit `data/`, `config/local.toml`, `config/*.local.*`, `sync/inbox/`, or exports, and never bind the server to anything but `127.0.0.1`.
7. For remote access, follow the Tailscale section and set a passphrase before allowlisting a host.

## Documentation

- [docs/architecture.md](docs/architecture.md): how the pieces fit
- [docs/configuration.md](docs/configuration.md): config file and environment variables
- [docs/importers.md](docs/importers.md): every import path
- [docs/personal-mode.md](docs/personal-mode.md), [docs/balances-vendors.md](docs/balances-vendors.md), [docs/manual-balances.md](docs/manual-balances.md), [docs/payment-tracking.md](docs/payment-tracking.md)
- [docs/whmcs.md](docs/whmcs.md): the optional billing integration
- [docs/update-routine.md](docs/update-routine.md): a daily update check that only speaks up when there is one
- [SECURITY.md](SECURITY.md): threat model and protections
- [web/README.md](web/README.md): front-end code and dev server

## Maintainers: publishing

This repository is published from a private working copy as scanned snapshots, so the public history never contains private commits. One-time: `git remote add public <url>`, `git config publish.name` / `publish.email`, and three local deny lists in `config/` (or `HPGBOOKS_PRIVACY_DIR`). Then:

```bash
scripts/publish.sh            # dry run: export, privacy scan, show what would be published
scripts/publish.sh --push     # publish a new snapshot commit to public/main
```

See [docs/publishing.md](docs/publishing.md).
