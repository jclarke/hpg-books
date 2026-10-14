# HPG Books: web front end

This is the single-page React app for HPG Books. Flask serves the built files from `hpgbooks/static/app/` and answers JSON under `/api/*`. The build output is committed, so `bin/hpgbooks web` runs without Node.

## Stack

- React 18 + TypeScript, bundled by Vite 5 (Node 20, npm 9).
- React Router 6 for client routes. TanStack Query 5 for fetching, caching, and optimistic updates.
- Plain CSS with design tokens (CSS variables) in `src/styles/tokens.css`: light and dark, following `prefers-color-scheme` unless the user picks one (stored in `localStorage`).
- Charts are hand-drawn SVG (`src/components/charts`). There are no runtime dependencies beyond React, the router, and Query. There are no CDNs, web fonts, or third-party requests.

## Layout

```
web/
  index.html              entry (no inline script; theme-init.js is a file)
  public/                 favicon.svg, theme-init.js (copied as-is)
  src/
    main.tsx, App.tsx     bootstrap, routes, query client
    api/                  client.ts (fetch + CSRF), types.ts, queries.ts (hooks)
    hooks/                session, global filters (URL), classify/undo, hotkeys, theme, …
    components/           shared design system (see below)
    components/charts/    ComboChart, BarList, DonutChart, AreaChart, Sparkline
    pages/                one file per screen
    lib/                  formatting, dates/presets, labels, nav, report list
    styles/               tokens.css, base.css, components.css, pages.css, print.css
    test/                 Vitest + React Testing Library suites, fixtures, fetch mock
```

Shared components: AppShell, LoginScreen, Sidebar, Topbar, BottomNav, CommandPalette, BusinessSwitcher, DateRangePicker, Popover, Card/CardHeader, KpiCard, DataTable (+Pagination), Money/MoneyCell, Badge/TagBadge/CountBadge, Button/IconButton, Select/TextField, SegmentedControl, Modal/Drawer, Toast (with Undo), EmptyState/ErrorState, Skeleton*, ErrorBoundary, PageHeader, InlineClassify, RuleDrawer, TransactionDrawer, DrilldownDrawer, SavedViews, ExportButtons, Logo/Wordmark, Icon.

The shell loads `GET /api/session` first. When `requires_login` is true and `authenticated` is false, it shows `LoginScreen` (passphrase field, a field error, and a lockout alert) and does not render the ledger. After sign-in, Sign out is in the top bar and the sidebar. A later `401` refetches the session and returns to that page. A loopback session leaves the shell open and shows "Local only · 127.0.0.1" in the sidebar.

## Commands

```bash
cd web
npm ci              # install exact versions from package-lock.json
npm run dev         # Vite on http://127.0.0.1:5173, proxying /api and /export to bin/hpgbooks web on :8765
npm test            # Vitest (jsdom)
npm run typecheck   # tsc
npm run build       # tsc + vite build -> web/dist
```

From the repo root, `bin/build-ui` runs `npm ci`, `npm run build`, and copies `web/dist` to `hpgbooks/static/app`. Use `bin/build-ui --no-install` to skip `npm ci`. Commit `hpgbooks/static/app` after building.

## Conventions

- Money crosses the API as integer cents and is formatted only in the browser (`lib/format.ts`). Use `<Money>`/`<MoneyCell>`: they use tabular figures, and negatives are red.
- The business filter and date range live in the URL (`?business=&start=&end=`). Use `useGlobalFilters()` to read them and `withGlobal()` to build links that keep them.
- Every mutation goes through `apiPost`, which sends `X-CSRF-Token` (from `/api/session`) and a JSON body. Classification edits are optimistic and show an Undo toast (`hooks/useClassify.ts`).
- No inline `<script>` or `<style>`. The server's CSP is `script-src 'self'; style-src 'self'`. Dynamic sizes go through React's `style` prop, which sets the CSSOM and is allowed.
