# Security

HPG Books keeps a ledger of real financial data on one machine. The design assumes a single trusted user on that machine and treats everything else (other hosts, browsers on the network, the git remote) as untrusted.

## Storage

- The database is a SQLCipher file encrypted with a 256-bit raw key (64 hex characters). The key comes from `HPBOOKS_KEY` or from a key file (`~/.config/hpbooks/key` by default, or `HPBOOKS_KEY_FILE`, or `[paths] key_file`). The key file must be mode `600` or stricter; a looser mode, a missing file, or a malformed key is a hard error.
- The key is never printed, logged, sent to the browser, or written into the database, exports, or git. Opening the database with plain `sqlite3` fails.
- `data/` is kept at mode `0700` and the database file at `0600`. Export files written to disk are `0600`; web exports are built in memory.
- `data/`, `*.db*`, keys, `exports/`, `sync/inbox/`, `.env*`, `config/local.toml`, and `config/*.local.*` are gitignored. Never commit them. `scripts/privacy_scan.sh` checks a tree for personal data before it is published (see [docs/publishing.md](docs/publishing.md)).

## Web server

- The server binds to `127.0.0.1:8765` only. It never listens on a LAN, tailnet, or public interface. Remote access goes through a local proxy such as Tailscale Serve (below).
- A request is accepted only when its `Host` header is loopback (`127.0.0.1`, `localhost`, `[::1]`, with or without a port) or exactly matches an allowlist entry. Anything else gets `403`, which blocks DNS rebinding. Wildcards and suffix matches are rejected.
- The UI is a single-page React app served from the same origin as the JSON API. It loads no CDN, web font, or third-party resource.
- Every response sends a strict Content-Security-Policy (`default-src 'self'`, no inline script or style, `frame-ancestors 'none'`), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, same-origin opener and resource policies, and a restrictive `Permissions-Policy`. `/api/*` and `/export/*` responses are `Cache-Control: no-store`.
- **CSRF.** Every mutating request (POST, PUT, PATCH, DELETE) must carry the session's token in `X-CSRF-Token` (from `GET /api/session`), have an `Origin` (if sent) whose host matches `Host`, and use a JSON body (`Content-Type: application/json`, at most 1 MB). The session cookie is `HttpOnly` and `SameSite=Strict`.
- GET endpoints open the database with `PRAGMA query_only = ON`.

## Remote access with Tailscale Serve (optional)

`tailscale serve` can forward a port on your tailnet to `127.0.0.1:8765`. Requests then arrive from loopback with your MagicDNS name or tailnet IP as `Host`. Those hosts must be allowlisted, and every allowlisted host must sign in with a passphrase.

- **Allowlist.** Extra hosts come from `bin/hpgbooks web --allow-host HOST` (repeatable; saved to `allowed-hosts` next to the database, mode `600`), from that file directly (one host per line, `#` comments), and from `HPGBOOKS_ALLOWED_HOSTS` (this process only). A file with a looser mode is refused. `bin/hpgbooks-web start` passes the saved hosts through on every start. An entry without a port matches any port; with a port, only that port.
- **Passphrase.** `bin/hpgbooks web-passphrase set` stores a salted scrypt hash (`n = 2^14`, `r = 8`, `p = 1`, 16-byte salt) in the encrypted database. Minimum 12 characters. The passphrase and hash are never logged or audited. Verification always runs scrypt and compares in constant time.
- With no passphrase stored, allowlisted hosts get `503 Sign-in is not configured` everywhere; the books stay closed. With one stored, only the sign-in shell and static files load before sign-in; `/api/*` and `/export/*` answer `401`.
- After 5 failed attempts the server waits 1 second, then 2, 4, and so on up to an hour (`429` with `Retry-After`). The counter is global because the proxy makes every client look like loopback; `X-Forwarded-For` is not trusted.
- Sessions expire after 12 idle hours. Sign-in replaces the session id and CSRF token. The cookie has no `Domain`, so each allowlisted host name signs in separately. `Secure` is off because the local proxy hop is plain HTTP; Tailscale (WireGuard) encrypts the path from the browser to the machine.
- Routes that return customer names (WHMCS customer pages, margins) check sign-in in the handler as well as in the app-wide guard.

Loopback (the local browser, the CLI, scripts) never needs the passphrase. Anyone with a shell on the machine as your user can read the key; protect the account accordingly.

## Input handling

- All SQL uses bound parameters. Dates, ids, accounts, tags, categories, sort keys, page sizes, and amounts are validated before they reach SQL; free text has length limits; rule patterns must compile and are capped at 200 characters.
- The API returns JSON and React renders it as text, never as HTML. CSV exports prefix cells starting with `=`, `+`, `-`, `@`, tab, or carriage return so spreadsheets do not run them as formulas.

## Audit

Every edit from the web UI or an editing CLI command writes an `audit_log` row with the old and new values: classifications and undo, rules, settings, balance anchors, vendor merges, account scope and settings, every personal-mode write (`personal_*`), margin edits, and WHMCS syncs. Sign-in events (`login_success`, `login_failure`, `logout`) and passphrase changes record the action only.

## Optional integrations

- **WHMCS** (off by default): read-only MySQL user with column-level grants, reached over SSH tunnels the sync opens and closes; the password is a mode-600 file next to the database. Customer names and emails stay in the encrypted database and appear only on signed-in customer pages. See [docs/whmcs.md](docs/whmcs.md).
- **Aggregator data** arrives as files an agent saves in `sync/inbox/` (gitignored). HPG Books makes no network calls for ledger data.
- **Personal mode** shares the same database, key, and web protections. Business and personal queries are separated by account scope with bound parameters, and a test walks every GET route in both modes to check that nothing crosses over. Only the last 4 digits of account numbers are stored.

## Tests

Tests set `HPBOOKS_KEY` and `HPBOOKS_DB` to temporary values; `tests/conftest.py` fails any test that would open the real database. All fixtures are invented. Frontend tests mock the network.

## Reporting a problem

This is a personal project published as is. If you find a security issue, open an issue without exploit details or contact the maintainer privately through the repository host.
