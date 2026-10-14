# Architecture

HPG Books is a single-user ledger that runs on one machine. A Python package owns the data and every calculation; a React app in the browser only displays and edits through a JSON API.

```
aggregator pulls / CSVs / PDFs ──> importers ──> SQLCipher database (data/hpbooks.db)
                                                   │
                         classify (rules, transfers, manual decisions)
                                                   │
             ┌─────────────────────────────────────┼──────────────────────────┐
         CLI (bin/hpgbooks)              Flask on 127.0.0.1:8765        exports (CSV/XLSX/PDF)
                                         ├─ /api/*  JSON
                                         └─ /       React app (hpgbooks/static/app)
```

## Storage

- One SQLCipher database, keyed with a 256-bit raw key (64 hex characters) from `HPBOOKS_KEY` or a mode-600 key file (`~/.config/hpbooks/key` by default). `hpgbooks/db.py` creates the schema, runs numbered migrations on every writing connection, and keeps `data/` at mode 700 and the file at mode 600.
- Money is stored and computed as integer cents. Dollars appear only at the edges (CLI arguments and output, the browser's formatter).
- Ledger rows (`transactions`) are kept as imported. Decisions about them live in separate tables: `classifications` (business), `p_classifications` and friends (personal), `vendor_aliases`, `balance_anchors`. Re-importing never loses a manual decision.
- Every write made from the web UI or a CLI editing command adds an `audit_log` row with the old and new values.

## Modules (`hpgbooks/`)

| Module | Role |
|---|---|
| `config.py` | Reads the site config (see [configuration.md](configuration.md)) |
| `db.py` | Key loading, connection, schema, migrations, money helpers |
| `importer.py` | Idempotent import of aggregator JSON and CSV tool results; pending → posted matching |
| `classify.py`, `seed_rules.py` | Business classification: manual, then rules by priority, then an optional prior-analysis seed, then `needs_review` |
| `reports.py`, `analytics.py` | P&L, reconciliation, cash flow, vendors, Schedule C aid, dashboard figures |
| `balances.py` | Statement balance anchors and the real balance derived from them |
| `vendors.py` | Vendor merges as a display map; ledger text is never rewritten |
| `scope.py`, `accounts_admin.py` | Account scope (`business`, `personal`, `excluded`) and the SQL filters that keep the two modes apart |
| `personal/` | Personal mode: its own categories, rules, transfer pairing, budgets, recurring detection, net worth |
| `payments.py`, `manual_accounts.py` | Card and loan payment terms; balance-only manual accounts |
| `capitalone.py`, `applecard.py`, `cfna.py`, `monarch.py` | File importers (see [importers.md](importers.md)) |
| `whmcs*.py`, `margins*.py` | Optional WHMCS billing copy, reports, and server margins (see [whmcs.md](whmcs.md)) |
| `web.py`, `api.py`, `webargs.py`, `access.py` | Flask app, JSON API, CSRF and request checks, host allowlist and sign-in |
| `cli.py` | The `hpgbooks` command line |

## Business and personal modes

Every account has a scope. Business queries read only `business` accounts and personal queries only `personal` ones; a transaction's scope always comes from its account through a join, so moving an account re-scopes every report without rewriting rows. The only business data personal mode reads is rows tagged `owner_draw` or `transfer`, shown read-only as funding. Details: [personal-mode.md](personal-mode.md).

## Classification (business)

For each non-manual row: the first matching rule (lowest priority number, then id), else the optional prior-analysis CSV, else `needs_review`. A manual classification is never replaced by an import or `reclassify`. `owner_draw` rows are always category Owner Draw and `transfer` rows always Transfer; both stay off the income statement (owner draws are shown below the line). `bin/hpgbooks reconcile` checks that raw activity minus transfers and owner draws equals P&L net income, per year and per business.

## Web app

`web/` is a React 18 + TypeScript app built with Vite. `bin/build-ui` builds it into `hpgbooks/static/app`, which is committed so the server runs without Node. The app reads install-specific data (product name, businesses, features) from `GET /api/config`. It loads nothing from third parties. See [web/README.md](../web/README.md).

## Tests

- `tests/` (pytest) uses a temporary database and key per test; `tests/conftest.py` fails any test that would open the real database. Fixtures are invented (`tests/fake_accounts.py`, `tests/personal_fake.py`, `tests/whmcs_fake.py`, `tests/fixtures/config.test.toml`).
- `tests/golden/` holds byte-for-byte expected CLI and API output for a fixed fake ledger. Regenerate with `.venv/bin/python tests/make_golden.py` only when an output change is intended.
- `web/src/test/` (Vitest + Testing Library) mocks the network.
