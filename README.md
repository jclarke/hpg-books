# hpbooks

A local, encrypted bookkeeping app for a small business and a household. It imports bank and card transactions, classifies them with rules you control, and produces a profit-and-loss statement, cash flow, vendor and tax-prep reports, from the command line or a web UI on your own machine. A separate **Personal** mode in the same database covers personal accounts: net worth, spending, budgets, recurring charges, bills, and goals.

Nothing leaves the machine. The database is SQLCipher-encrypted, the web server listens on `127.0.0.1` only, and the app makes no third-party requests. The product name shown in the UI comes from your config (default "Books").

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

# Web UI (optional: hpbooks/static/app is already built)
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
bin/hpbooks init
bin/hpbooks-web start          # http://127.0.0.1:8765
```

**Back up the key.** Without it the database cannot be opened. Keep a copy somewhere safe and separate from database backups.

`bin/hpbooks` runs `.venv/bin/python -m hpbooks`; `bin/hpbooks --help` lists every command. `bin/hpbooks-web start|stop|status` runs the server in the background (log and pid in `data/`); `bin/hpbooks web` runs it in the foreground. The database is `data/hpbooks.db` unless `HPBOOKS_DB` or `[paths] db` says otherwise. Business tags and categories become database constraints at `init`, so set them in the config first. See [docs/configuration.md](docs/configuration.md).

## First steps

```bash
bin/hpbooks accounts discover finance_list_accounts.json --dry-run   # register accounts from a saved account list
bin/hpbooks accounts set-scope <id|last4|name> business               # new accounts start as personal
bin/hpbooks import sync/inbox/2026-10-01/                             # load transactions
bin/hpbooks review --year 2026                                        # what still needs a decision
bin/hpbooks classify <txn-id-prefix> --tag general --category "Software & Licenses" --rule
bin/hpbooks pnl --year 2026 --by month
bin/hpbooks reconcile --year 2026
```

Most of this is easier in the web UI: the Review page, inline classification on Transactions, and "Create rule from this". The daily pull routine for an agent is in [sync/README.md](sync/README.md).

## Remote access with Tailscale (optional)

The server always binds to loopback. To reach it from your other devices, let Tailscale Serve forward a tailnet port to it, allowlist the host names you will type, and set a passphrase:

```bash
tailscale serve --bg --tcp 8765 tcp://127.0.0.1:8765     # check `tailscale serve --help` for your version
bin/hpbooks web-passphrase set                           # at least 12 characters; stored as a scrypt hash
bin/hpbooks-web stop
bin/hpbooks web --allow-host <machine>.<tailnet-domain> --allow-host <tailnet-ip>   # saves data/allowed-hosts (mode 600); Ctrl-C
bin/hpbooks-web start                                    # passes the saved hosts on every start
```

You can also edit `data/allowed-hosts` directly (one host per line, mode 600). Each host name signs in separately. With no passphrase set, allowlisted hosts get `503` and the books stay closed. Details: [SECURITY.md](SECURITY.md).

## WHMCS (optional)

Set `whmcs = true` under `[features]` and fill in `[whmcs]` and one `[[whmcs.brands]]` per install in `config/local.toml`, put the read-only MySQL password in `data/whmcs.secret` (mode 600), then `bin/hpbooks whmcs sync`. Setup, grants, and report definitions: [docs/whmcs.md](docs/whmcs.md).

## Updates

Installs follow the public snapshot repository (`[update]` in the config: `repo`, `remote`, `branch`; defaults `jclarke/hpg-books`, `public`, `main`). A git remote with the `remote` name is used when the clone has one; otherwise the repository URL.

```bash
git remote add public https://github.com/jclarke/hpg-books.git   # once, if the clone lacks it
bin/hpbooks update check          # exit 0 up to date, 1 update available, 2 error
bin/hpbooks update apply          # dry run: lists the new commits, changes nothing
bin/hpbooks update apply --yes    # git fetch + git merge --ff-only
bin/hpbooks-web stop && bin/hpbooks-web start   # load the new code
```

What is preserved: everything git ignores, which an update never writes: `config/local.toml`, `config/*.local.*`, `data/` (the database, key, logs, allowlist), keys and secrets, `.venv`, `sync/inbox/`, and exports. `apply` refuses when tracked files have uncommitted changes, when your clone has commits of its own (it only fast-forwards), or when upstream would change a protected path. The built UI in `hpbooks/static/app` arrives with the update; `--build-ui` also runs `bin/build-ui --no-install` when `web/` changed. If `requirements.txt` changed, run `.venv/bin/pip install -r requirements.txt`.

An install unpacked from a zip (no `.git`) is checked through the GitHub API (`gh` when installed, else `GITHUB_TOKEN` if set) against the revision in `.hpbooks-revision`; `apply --yes` downloads the branch and overlays it, skipping the same protected paths. Files deleted upstream are not removed from a zip install.

To be told about updates, see [docs/update-routine.md](docs/update-routine.md).

## Tests

```bash
.venv/bin/pytest                                 # Python; temporary database and key per test
HPBOOKS_TEST_WHMCS=off .venv/bin/pytest          # same, with the WHMCS feature off
(cd web && npm test && npm run typecheck)        # Vitest + tsc
```

Tests never open your real database. `tests/golden/` holds expected output for a fake ledger; regenerate it with `.venv/bin/python tests/make_golden.py` only for an intended change.

## Installing with an AI assistant

If an assistant (Claude Code or similar) sets this up for you, give it this checklist:

1. Clone, create `.venv`, `pip install -r requirements.txt`. Do not run `npm` unless the UI must be rebuilt.
2. Create the key file exactly as above. Never print, log, or commit the key; never paste it into chat.
3. Copy `config/config.example.toml` to `config/local.toml` and ask the user for the company name, businesses, and categories **before** `bin/hpbooks init`.
4. Run `bin/hpbooks init`, `bin/hpbooks-web start`, and check `curl -s http://127.0.0.1:8765/api/session`.
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

This repository is published from a private working copy as scanned snapshots, so the public history never contains private commits. One-time: `git remote add public <url>`, `git config publish.name` / `publish.email`, and three local deny lists in `config/` (or `HPBOOKS_PRIVACY_DIR`). Then:

```bash
scripts/publish.sh            # dry run: export, privacy scan, show what would be published
scripts/publish.sh --push     # publish a new snapshot commit to public/main
```

See [docs/publishing.md](docs/publishing.md).
