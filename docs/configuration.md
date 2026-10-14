# Configuration

Everything specific to one install (product and company name, businesses, categories, accounts, rules, integrations) lives in one TOML file outside git. The code holds only generic defaults.

## Where the config is read from

1. `$HPBOOKS_CONFIG`, when set.
2. `config/local.toml` in the repository, when it exists. It is gitignored.
3. Otherwise the built-in defaults: product name "Books", company "My Business", one `general` business, the default P&L categories, generic starter rules, no seeded accounts, and every optional feature off.

`config/config.example.toml` lists every key with its default and a comment. Start from it:

```bash
cp config/config.example.toml config/local.toml
chmod 600 config/local.toml
$EDITOR config/local.toml
```

Business tags, categories, and the accounts `init` seeds are read once per process: they become the database `CHECK` constraints and lookup tables when the database is created. Decide them before `bin/hpgbooks init`; adding a tag or category later needs a fresh database. Everything else (names, colours, feature flags, WHMCS settings, transfer pairs) can change at any time; restart the web server to pick it up.

## Sections

| Section | What it sets |
|---|---|
| `[app]` | `product_name` (browser title, sign-in screen, export headers), `company_name` (P&L titles, sidebar), `wordmark` (logo text) |
| `[paths]` | `db` and `key_file`. Relative paths are relative to the repository |
| `[features]` | `business` and `personal` (both default `true`; at least one must be on), `whmcs` (default `false`), `margins` (defaults to the `whmcs` value and needs it) |
| `[[businesses]]` | The business tags the P&L is split by: `slug`, `label`, `kind` (`brand`, `consulting`, `overhead`, or any text), `tone`, `color`, `color_dark`, `revenue_category`. The first `overhead` business (else the last one) receives generic costs and is the default tag |
| `[categories]` | `revenue`, `cogs`, `opex` lists. `Refunds`, `Owner Draw`, `Transfer`, and `Uncategorized` are built in. Opex names that match a Schedule C line get that line in the Schedule C report |
| `[[accounts.business]]` | Business accounts `init` registers: `id` (the aggregator's account id), `name`, `type` (`cash` or `liability`), `last4`, `institution`, `short_name`, `label`, `roles` (`operating`, `paypal`, `capitalone_default`). Empty by default; accounts can be registered later with `bin/hpgbooks accounts discover` |
| `[[transfer_pairs]]` | Payments between two accounts matched by equal amount within a window (`abs7` or `after0_7`), for example a card payment from checking |
| `[seed]` | `starter_rules` (generic rules on or off), `rules_file` (your own rules as JSON, relative to the config file), and an optional prior-analysis CSV |
| `[importers]` | Defaults for the scripts in `scripts/` (see [importers.md](importers.md)) |
| `[[payments.payer_hints]]` | How `accounts infer-payments` finds a loan's payments in a bank account |
| `[whmcs]` | Only read when `features.whmcs = true`; see [whmcs.md](whmcs.md) |
| `[update]` | `repo` (`owner/name`, default `jclarke/hpg-books`), `remote` (default `public`), `branch` (default `main`) for `hpgbooks update check\|apply`; see the README's Updates section |

Keep personal rule patterns, account ids, and real names in `config/local.toml` and in a `rules_file` such as `config/seed_rules.local.json`. Both match the `.gitignore` patterns (`config/local.toml`, `config/*.local.*`) and are never committed.

## Environment variables

| Variable | Meaning |
|---|---|
| `HPBOOKS_CONFIG` | Path of the config file |
| `HPBOOKS_DB` | Database path (default `data/hpbooks.db`) |
| `HPBOOKS_KEY` | The 64-hex database key itself. Takes precedence over the key file; mainly for tests and throwaway databases |
| `HPBOOKS_KEY_FILE` | Path of the key file (default `~/.config/hpbooks/key`) |
| `HPGBOOKS_ALLOWED_HOSTS` | Extra `Host` values to accept, comma-separated, for this process only |
| `HPGBOOKS_NEW_PASSPHRASE` | Read once by `web-passphrase set` for non-interactive use |
| `HPGBOOKS_SEED_CSV` | Overrides `[seed] prior_csv` |
| `HPGBOOKS_PRIVACY_DIR` | Where `scripts/privacy_scan.sh` finds the local deny lists (default `config/`) |
| `HPGBOOKS_TEST_WHMCS` | `off` runs the test suite with the WHMCS integration switched off |

Precedence for the database and key: environment variable, then `[paths]`, then the default.

The app was renamed from hpbooks to HPG Books (`hpgbooks`). The variables that locate existing data (`HPBOOKS_CONFIG`, `HPBOOKS_DB`, `HPBOOKS_KEY`, `HPBOOKS_KEY_FILE`) and the default paths `data/hpbooks.db` and `~/.config/hpbooks/key` kept their names. The renamed `HPGBOOKS_*` variables still fall back to their old `HPBOOKS_*` names, and `bin/hpbooks` / `bin/hpbooks-web` remain as shims for the new scripts.

## Feature flags

`features.whmcs = false` (the default): the `whmcs` and `margins` commands print a message and exit, `/api/whmcs/*`, `/api/margins/*`, and `/export/whmcs/*` answer 404, and the web UI hides those pages. Personal mode shows setup steps until a personal account exists.

`features.personal = false`: the `personal` command prints a message and exits, `/api/personal/*` answers 404, and the web UI hides the Business | Personal switch and sends `/personal/...` pages to their business counterpart. `features.business = false`: the business-only commands (`pnl`, `review`, `rules`, `txns`, `classify`, `balances`, `vendors`, `transfers`, `whmcs`, ...) print a message and exit, and the web UI opens in personal mode with no switch. `init`, `import`, `accounts`, `audit`, `web`, and `update` work in either case. Both off is a config error.

The web UI reads the product name, businesses, feature flags, and brand names from `GET /api/config`, so the built bundle contains nothing install-specific and does not need rebuilding when the config changes.
