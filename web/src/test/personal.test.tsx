import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { session as businessSession } from "./fixtures";
import { pickOption } from "./combo";
import { MockResponse, mockApi, type Call } from "./mockApi";
import * as px from "./personalFixtures";
import { renderApp } from "./render";

type Handler = (call: Call) => unknown;

const PERSONAL: Record<string, Handler> = {
  "GET /api/session": (call) => (call.query.get("mode") === "personal" ? px.personalSession : businessSession),
  "GET /api/personal/status": () => ({ ok: true, ready: true, account_count: 2, transaction_count: 300, setup_steps: px.personalSession.personal?.setup_steps }),
  "GET /api/personal/dashboard": () => px.pdashboard,
  "GET /api/personal/net-worth": () => px.netWorth,
  "GET /api/personal/accounts": () => px.paccounts,
  [`GET /api/personal/accounts/${px.CHK}/register`]: () => ({ ...px.ptxnPage, account: { id: px.CHK, label: "Fake Everyday Checking", class: "cash", last4: "1111", institution: "Fake Bank" } }),
  "GET /api/personal/transactions": () => px.ptxnPage,
  "GET /api/personal/spending": () => px.spending,
  "GET /api/personal/cash-flow": () => px.cashFlow,
  "GET /api/personal/budgets": () => px.budgets,
  "GET /api/personal/budgets/suggestions": () => ({ rows: [{ category_id: 4, category: "Groceries", group: "Food", average_cents: 52000 }] }),
  "GET /api/personal/recurring": () => px.recurring,
  "GET /api/personal/bills": () => px.bills,
  "GET /api/personal/goals": () => ({ rows: px.goals }),
  "GET /api/personal/debt-payoff": () => px.debtPayoff,
  "GET /api/personal/summary": () => px.summary,
  "GET /api/personal/review": () => ({ rows: [{ ...px.ptxns[3], suggestion: { category_id: 5, category: "Dining out", reason: "2 of 2 earlier Zzq Local Shop rows", confidence: 1 }, similar: 4 }], count: 1 }),
  "GET /api/personal/categories": () => ({ rows: px.pcategories.map((cat) => ({ ...cat, transaction_count: 3 })) }),
  "GET /api/personal/rules": () => ({ rows: px.prules }),
  "GET /api/personal/merchants": () => ({ rows: [{ key: "KROGER", display_name: "Kroger", renamed: false, count: 52, spellings: ["KROGER #0451"] }] }),
  "GET /api/accounts/settings": () => ({ rows: px.accountSettings, counts: { business: 1, personal: 1, excluded: 0 } }),
  "POST /api/personal/budgets": () => ({ ok: true, result: { id: 12 }, review_count: 3 }),
  "POST /api/personal/recurring/update": () => ({ ok: true, result: {}, review_count: 3 }),
  "POST /api/personal/categorize": () => ({ ok: true, result: {}, review_count: 2 }),
  "POST /api/personal/categorize/similar": () => ({ ok: true, result: 4, review_count: 0 }),
  "POST /api/personal/rules/preview": () => ({ ok: true, count: 52, would_change: 3, manual: 1, sample: [] }),
  [`PATCH /api/accounts/${px.CARD}/settings`]: (call) => ({
    ok: true,
    account: { ...px.accountSettings[1], ...(call.body as object) },
    scope_change: { old_scope: "personal", scope: "excluded", transactions: 236, changed: true },
  }),
};

function topbar(): HTMLElement {
  const element = document.querySelector<HTMLElement>(".topbar");
  if (!element) throw new Error("no top bar");
  return element;
}

function personalApi(overrides: Record<string, Handler> = {}) {
  return mockApi({ ...PERSONAL, ...overrides });
}

const PAGES: [string, string, string | RegExp][] = [
  ["/personal", "Personal dashboard", "Top categories"],
  ["/personal/net-worth", "Net worth", "Fake Home Mortgage"],
  ["/personal/accounts", "Personal accounts", "Fake Rewards Visa"],
  [`/personal/accounts/${px.CHK}`, "Fake Everyday Checking", "Kroger"],
  ["/personal/transactions", "Personal transactions", "Acme Corp"],
  ["/personal/spending", "Spending", "Housing"],
  ["/personal/cash-flow", "Cash flow", "Income sources"],
  ["/personal/budgets", "Budgets", "Groceries"],
  ["/personal/recurring", "Recurring & subscriptions", "Netflix"],
  ["/personal/bills", "Upcoming bills", "Netflix"],
  ["/personal/goals", "Goals", "Emergency fund"],
  ["/personal/debt-payoff", "Debt payoff", "Fake Rewards Visa"],
  ["/personal/review-month", "Monthly summary", /changed price/],
  ["/personal/review", "Personal review", "Zzq Local Shop"],
  ["/personal/categories", "Categories & rules", "Owner draws"],
  ["/personal/settings", "Settings", "Fake Rewards Visa"],
];

const API_FOR: Record<string, string> = {
  "/personal": "GET /api/personal/dashboard",
  "/personal/net-worth": "GET /api/personal/net-worth",
  "/personal/accounts": "GET /api/personal/accounts",
  [`/personal/accounts/${px.CHK}`]: `GET /api/personal/accounts/${px.CHK}/register`,
  "/personal/transactions": "GET /api/personal/transactions",
  "/personal/spending": "GET /api/personal/spending",
  "/personal/cash-flow": "GET /api/personal/cash-flow",
  "/personal/budgets": "GET /api/personal/budgets",
  "/personal/recurring": "GET /api/personal/recurring",
  "/personal/bills": "GET /api/personal/bills",
  "/personal/goals": "GET /api/personal/goals",
  "/personal/debt-payoff": "GET /api/personal/debt-payoff",
  "/personal/review-month": "GET /api/personal/summary",
  "/personal/review": "GET /api/personal/review",
  "/personal/categories": "GET /api/personal/categories",
  "/personal/settings": "GET /api/accounts/settings",
};

beforeEach(() => {
  window.localStorage.clear();
});

describe("personal pages", () => {
  it.each(PAGES)("%s renders data", async (url, heading, content) => {
    personalApi();
    renderApp(url);
    expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
    expect((await screen.findAllByText(content)).length).toBeGreaterThan(0);
  });

  it.each(PAGES)("%s shows a loading state first", async (url) => {
    personalApi({ [API_FOR[url]]: () => new Promise(() => undefined) });
    renderApp(url);
    await screen.findByRole("heading", { level: 1 });
    expect(document.querySelector('[aria-busy="true"], [role="status"][aria-label="Loading"], .skeleton')).not.toBeNull();
  });

  it.each(PAGES)("%s shows an error state", async (url) => {
    personalApi({ [API_FOR[url]]: () => new MockResponse(500, { ok: false, error: "boom" }) });
    renderApp(url);
    await screen.findByRole("heading", { level: 1 });
    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
  });

  it("shows the setup steps when there are no personal accounts", async () => {
    personalApi({ "GET /api/personal/dashboard": () => ({ ...px.pdashboard, has_accounts: false, has_data: false }) });
    renderApp("/personal");
    expect(await screen.findByText("No personal accounts yet")).toBeInTheDocument();
    expect(await screen.findByText(/accounts discover/)).toBeInTheDocument();
  });

  it("shows empty states for empty lists", async () => {
    personalApi({
      "GET /api/personal/budgets": () => ({ ...px.budgets, rows: [], alerts: [], unbudgeted: [] }),
      "GET /api/personal/review": () => ({ rows: [], count: 0 }),
      "GET /api/personal/goals": () => ({ rows: [] }),
      "GET /api/personal/recurring": () => ({ ...px.recurring, items: [] }),
    });
    const first = renderApp("/personal/budgets");
    expect(await screen.findByText("No budgets for this month")).toBeInTheDocument();
    first.unmount();
    const second = renderApp("/personal/review");
    expect(await screen.findByText("All caught up")).toBeInTheDocument();
    second.unmount();
    const third = renderApp("/personal/goals");
    expect(await screen.findByText("No goals yet")).toBeInTheDocument();
    third.unmount();
    renderApp("/personal/recurring");
    expect(await screen.findByText("Nothing recurring here")).toBeInTheDocument();
  });

  it("labels rows paid from a business account and keeps them read-only", async () => {
    personalApi();
    renderApp("/personal/transactions");
    expect((await screen.findAllByText("paid from business account")).length).toBeGreaterThan(0);
    expect(screen.queryByLabelText("Category for Fake Mortgage Servicer")).toBeNull();
    expect(screen.getByLabelText("Category for Kroger")).toBeInTheDocument();
  });
});

describe("mode toggle", () => {
  it("switches mode, persists it, changes the nav, and keeps the date range", async () => {
    const calls = personalApi();
    const user = userEvent.setup();
    renderApp("/?start=2026-09-01&end=2026-09-30");
    await screen.findByRole("heading", { level: 1, name: "Dashboard" });
    const banner = topbar();
    const toggle = within(banner).getByRole("group", { name: "Mode" });
    const business = within(toggle).getByRole("button", { name: "Business" });
    const personal = within(toggle).getByRole("button", { name: "Personal" });
    expect(business).toHaveAttribute("aria-pressed", "true");
    expect(personal).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByLabelText("Business")).toBeInTheDocument();
    await user.click(personal);
    expect(await screen.findByRole("heading", { level: 1, name: "Personal dashboard" })).toBeInTheDocument();
    expect(window.localStorage.getItem("hpgbooks.mode")).toBe("personal");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: /Net worth/ })).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: /Vendors/ })).toBeNull();
    expect(within(nav).getByRole("link", { name: /Budgets/ }).getAttribute("href")).toContain("start=2026-09-01");
    expect(screen.queryByLabelText("Business")).toBeNull();
    expect(within(topbar()).getByRole("button", { name: "Personal" })).toHaveAttribute("aria-pressed", "true");
    const dash = calls.find((call) => call.path === "/api/personal/dashboard");
    expect(dash?.query.get("mode")).toBe("personal");
    await user.click(within(topbar()).getByRole("button", { name: "Business" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    expect(window.localStorage.getItem("hpgbooks.mode")).toBe("business");
  });

  it("goes to the matching page when one exists", async () => {
    personalApi();
    const user = userEvent.setup();
    renderApp("/transactions");
    await screen.findByRole("heading", { level: 1, name: "Transactions" });
    await user.click(within(topbar()).getByRole("button", { name: "Personal" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Personal transactions" })).toBeInTheDocument();
  });

  it("goes to the dashboard of the new mode when the page has no counterpart", async () => {
    personalApi();
    const user = userEvent.setup();
    renderApp("/personal/goals");
    await screen.findByRole("heading", { level: 1, name: "Goals" });
    await user.click(within(topbar()).getByRole("button", { name: "Business" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
  });

  it("remembers personal mode across a reload of the root page", async () => {
    window.localStorage.setItem("hpgbooks.mode", "personal");
    personalApi();
    renderApp("/");
    expect(await screen.findByRole("heading", { level: 1, name: "Personal dashboard" })).toBeInTheDocument();
  });

  it("lets the URL win over the remembered mode", async () => {
    window.localStorage.setItem("hpgbooks.mode", "personal");
    personalApi();
    const first = renderApp("/?mode=business");
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    expect(window.localStorage.getItem("hpgbooks.mode")).toBe("business");
    first.unmount();
    window.localStorage.setItem("hpgbooks.mode", "business");
    renderApp("/?mode=personal");
    expect(await screen.findByRole("heading", { level: 1, name: "Personal dashboard" })).toBeInTheDocument();
    expect(window.localStorage.getItem("hpgbooks.mode")).toBe("personal");
  });

  it("sends the mode on every API call", async () => {
    const calls = personalApi();
    const first = renderApp("/");
    await screen.findByRole("heading", { level: 1, name: "Dashboard" });
    await waitFor(() => expect(calls.some((call) => call.path === "/api/dashboard")).toBe(true));
    for (const call of calls) expect(call.query.get("mode")).toBe("business");
    first.unmount();
    calls.length = 0;
    renderApp("/personal/spending");
    await screen.findAllByText("Housing");
    expect(calls.length).toBeGreaterThan(1);
    for (const call of calls) expect(call.query.get("mode")).toBe("personal");
  });

  it("searches only the active mode from the command palette", async () => {
    const calls = personalApi({ "GET /api/search": () => ({ transactions: [], vendors: [{ name: "Kroger", count: 3, spend_cents: 100, last_date: "2026-09-01" }], accounts: [] }) });
    const user = userEvent.setup();
    renderApp("/personal");
    await screen.findByRole("heading", { level: 1, name: "Personal dashboard" });
    await user.keyboard("{Control>}k{/Control}");
    await user.type(await screen.findByRole("combobox"), "kro");
    await waitFor(() => expect(calls.some((call) => call.path === "/api/search")).toBe(true));
    expect(calls.filter((call) => call.path === "/api/search").every((call) => call.query.get("mode") === "personal")).toBe(true);
    expect(screen.queryByText("Profit & Loss")).toBeNull();
  });
});

describe("personal writes", () => {
  it("adds a budget and edits one", async () => {
    const calls = personalApi();
    const user = userEvent.setup();
    renderApp("/personal/budgets");
    await screen.findByText("Budget alerts:");
    await pickOption(user, screen.getByLabelText("Budget category"), "Dining out", "dining");
    await user.type(screen.getByLabelText("Amount per month ($)"), "150");
    await user.click(screen.getByRole("button", { name: "Save budget" }));
    await waitFor(() => expect(calls.some((call) => call.method === "POST" && call.path === "/api/personal/budgets")).toBe(true));
    const post = calls.find((call) => call.method === "POST" && call.path === "/api/personal/budgets");
    expect(post?.body).toMatchObject({ category_id: 5, amount_cents: 15000, month: null, rollover: false });
    expect(post?.headers["X-CSRF-Token"]).toBe("test-csrf-token-123456");
    expect(post?.query.get("mode")).toBe("personal");
    await user.click(screen.getByRole("button", { name: "Edit budget Groceries" }));
    const input = screen.getByLabelText("Budget for Groceries");
    await user.clear(input);
    await user.type(input, "700");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.filter((call) => call.path === "/api/personal/budgets" && call.method === "POST").length).toBe(2));
    expect(calls.filter((call) => call.path === "/api/personal/budgets" && call.method === "POST")[1].body).toMatchObject({ category_id: 4, amount_cents: 70000 });
    expect(screen.getByRole("progressbar", { name: /Groceries/ })).toHaveAttribute("aria-valuenow", "92");
  });

  it("confirms, ignores, and re-cadences recurring items", async () => {
    const calls = personalApi();
    const user = userEvent.setup();
    renderApp("/personal/recurring");
    await screen.findByText("Netflix");
    expect(screen.getByText("may be cancelled")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Ignore Hulu" }));
    await user.click(screen.getByRole("button", { name: "Confirm Netflix" }));
    await user.selectOptions(screen.getByLabelText("Cadence for Netflix"), "annual");
    await waitFor(() => expect(calls.filter((call) => call.path === "/api/personal/recurring/update").length).toBe(3));
    const bodies = calls.filter((call) => call.path === "/api/personal/recurring/update").map((call) => call.body);
    expect(bodies).toEqual([
      { series_key: "HULU|out|0", status: "ignored" },
      { series_key: "NETFLIX|out|0", status: "confirmed" },
      { series_key: "NETFLIX|out|0", cadence: "annual" },
    ]);
  });

  it("asks before moving an account between modes and shows how many rows move", async () => {
    const calls = personalApi();
    const user = userEvent.setup();
    renderApp("/personal/settings");
    await screen.findByText("Fake Rewards Visa");
    await user.selectOptions(screen.getByLabelText("Scope of Fake Rewards Visa"), "excluded");
    const dialog = await screen.findByRole("dialog", { name: "Move this account?" });
    expect(within(dialog).getByText(/236 transactions/)).toBeInTheDocument();
    expect(calls.some((call) => call.method === "PATCH")).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Move to excluded" }));
    await waitFor(() => expect(calls.some((call) => call.method === "PATCH")).toBe(true));
    const patch = calls.find((call) => call.method === "PATCH");
    expect(patch?.path).toBe(`/api/accounts/${px.CARD}/settings`);
    expect(patch?.body).toEqual({ scope: "excluded" });
    expect(patch?.headers["X-CSRF-Token"]).toBe("test-csrf-token-123456");
    expect(await screen.findByText(/236 transactions moved from personal to excluded/)).toBeInTheDocument();
  });

  it("cancelling the scope dialog changes nothing", async () => {
    const calls = personalApi();
    const user = userEvent.setup();
    renderApp("/personal/settings");
    await screen.findByText("Fake Rewards Visa");
    await user.selectOptions(screen.getByLabelText("Scope of Fake Rewards Visa"), "business");
    const dialog = await screen.findByRole("dialog", { name: "Move this account?" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(calls.some((call) => call.method === "PATCH")).toBe(false);
  });

  it("accepts a review suggestion with Enter and applies to similar with Shift+Enter", async () => {
    const calls = personalApi();
    const user = userEvent.setup();
    renderApp("/personal/review");
    await screen.findByText(/Suggested/);
    await user.keyboard("{Enter}");
    await waitFor(() => expect(calls.some((call) => call.path === "/api/personal/categorize")).toBe(true));
    expect(calls.find((call) => call.path === "/api/personal/categorize")?.body).toEqual({ txn_id: "p3", category_id: 5 });
    await user.keyboard("{Shift>}{Enter}{/Shift}");
    await waitFor(() => expect(calls.some((call) => call.path === "/api/personal/categorize/similar")).toBe(true));
  });

  it("previews a new rule against history", async () => {
    const calls = personalApi();
    const user = userEvent.setup();
    renderApp("/personal/categories");
    await screen.findByText("Owner draws");
    await user.click(screen.getByRole("radio", { name: "Rules" }));
    await user.type(await screen.findByLabelText("Pattern"), "KROGER");
    expect(await screen.findByText("52 matches")).toBeInTheDocument();
    expect(screen.getByText(/3 would change category/)).toBeInTheDocument();
    expect(calls.find((call) => call.path === "/api/personal/rules/preview")?.body).toMatchObject({ pattern: "KROGER" });
  });

  it("links CSV exports with the personal mode", async () => {
    personalApi();
    renderApp("/personal/cash-flow");
    await screen.findByText("Income sources");
    const link = screen.getByRole("link", { name: /CSV/ });
    expect(link.getAttribute("href")).toMatch(/^\/export\/personal\/cash-flow\.csv\?/);
    expect(link.getAttribute("href")).toContain("mode=personal");
  });
});

describe("dashboard top categories", () => {
  const dashboard = {
    ...px.pdashboard,
    as_of: "2026-09-15",
    top_categories: [
      { category_id: 3, category: "Mortgage", group: "Housing", cents: 500000, count: 1 },
      { category_id: 8, category: "Uncategorized", group: "Uncategorized", cents: 2500, count: 1 },
    ],
  };
  const lastTxnCall = (calls: Call[]) => calls.filter((call) => call.path === "/api/personal/transactions").pop();

  it("labels categories with their parent group, leaving Uncategorized plain", async () => {
    personalApi({ "GET /api/personal/dashboard": () => dashboard });
    renderApp("/personal?mode=personal");
    const list = await screen.findByRole("list", { name: "Top spending categories this month" });
    expect(within(list).getByText("Housing / Mortgage")).toBeInTheDocument();
    expect(within(list).getByText("Uncategorized")).toBeInTheDocument();
    expect(within(list).queryByText("Uncategorized / Uncategorized")).toBeNull();
  });

  it("opens a category's transactions for the dashboard month to date", async () => {
    const calls = personalApi({ "GET /api/personal/dashboard": () => dashboard });
    const user = userEvent.setup();
    renderApp("/personal?mode=personal&start=2026-01-01&end=2026-03-31");
    const list = await screen.findByRole("list", { name: "Top spending categories this month" });
    expect(within(list.closest(".card") as HTMLElement).getByRole("link", { name: "Spending" })).toBeInTheDocument();
    await user.click(within(list).getByRole("button", { name: /Mortgage/ }));
    expect(await screen.findByRole("heading", { level: 1, name: "Personal transactions" })).toBeInTheDocument();
    await waitFor(() => expect(lastTxnCall(calls)?.query.get("category_id")).toBe("3"));
    const call = lastTxnCall(calls);
    expect(call?.query.get("start")).toBe("2026-09-01");
    expect(call?.query.get("end")).toBe("2026-09-15");
    expect(call?.query.get("uncategorized")).toBeFalsy();
    expect(await screen.findByRole("button", { name: "Clear category filter Mortgage" })).toBeInTheDocument();
  });

  it("opens Uncategorized with the uncategorized filter, not a category id", async () => {
    const calls = personalApi({ "GET /api/personal/dashboard": () => dashboard });
    const user = userEvent.setup();
    renderApp("/personal?mode=personal");
    const list = await screen.findByRole("list", { name: "Top spending categories this month" });
    await user.click(within(list).getByRole("button", { name: /Uncategorized/ }));
    await waitFor(() => expect(lastTxnCall(calls)?.query.get("uncategorized")).toBe("1"));
    expect(lastTxnCall(calls)?.query.get("category_id")).toBeFalsy();
    expect(screen.getByRole("checkbox", { name: "Uncategorized only" })).toBeChecked();
    expect(screen.getByRole("button", { name: "Clear uncategorized filter" })).toBeInTheDocument();
  });

  it("clears a category chip and keeps the other filters", async () => {
    const calls = personalApi();
    const user = userEvent.setup();
    renderApp("/personal/transactions?mode=personal&start=2026-09-01&end=2026-09-15&category_id=3&pending=1");
    await user.click(await screen.findByRole("button", { name: "Clear category filter Mortgage" }));
    await waitFor(() => expect(lastTxnCall(calls)?.query.get("category_id")).toBeFalsy());
    const call = lastTxnCall(calls);
    expect(call?.query.get("pending")).toBe("1");
    expect(call?.query.get("start")).toBe("2026-09-01");
    expect(call?.query.get("end")).toBe("2026-09-15");
    expect(screen.queryByRole("group", { name: "Active filters" })).not.toBeInTheDocument();
  });

  it("clears every chip at once", async () => {
    const calls = personalApi();
    const user = userEvent.setup();
    renderApp("/personal/transactions?mode=personal&category_id=3&uncategorized=1");
    await user.click(await screen.findByRole("button", { name: "Clear filters" }));
    await waitFor(() => expect(lastTxnCall(calls)?.query.get("uncategorized")).toBeFalsy());
    expect(lastTxnCall(calls)?.query.get("category_id")).toBeFalsy();
  });
});

describe("spending by category", () => {
  const lastTxnCall = (calls: Call[]) => calls.filter((call) => call.path === "/api/personal/transactions").pop();

  it("links a group name to its filtered transactions and expands separately", async () => {
    const calls = personalApi();
    const user = userEvent.setup();
    renderApp("/personal/spending?mode=personal");
    const table = await screen.findByRole("table", { name: "Spending by group and category" });
    await user.click(within(table).getByRole("button", { name: "Expand Food" }));
    expect(within(table).getByRole("link", { name: "Groceries" })).toBeInTheDocument();
    await user.click(within(table).getByRole("link", { name: "Housing" }));
    await waitFor(() => expect(lastTxnCall(calls)?.query.get("group")).toBe("Housing"));
  });

  it("labels the prior column 'Last month' for a whole-month range", async () => {
    personalApi();
    renderApp("/personal/spending?mode=personal&start=2026-09-01&end=2026-09-30");
    const table = await screen.findByRole("table", { name: "Spending by group and category" });
    expect(within(table).getByRole("columnheader", { name: "Last month" })).toBeInTheDocument();
  });

  it("falls back to 'Prior period' for a non-month range", async () => {
    personalApi({ "GET /api/personal/spending": () => ({ ...px.spending, prior_start: "2026-08-16", prior_end: "2026-08-31" }) });
    renderApp("/personal/spending?mode=personal&start=2026-09-05&end=2026-09-20");
    const table = await screen.findByRole("table", { name: "Spending by group and category" });
    expect(within(table).getByRole("columnheader", { name: "Prior period" })).toBeInTheDocument();
  });

  it("omits the YTD delta badge when last year had nothing", async () => {
    personalApi({ "GET /api/personal/spending": () => ({ ...px.spending, ytd: { cents: 5000000, prior_cents: 0, delta: 5000000, pct: null } }) });
    renderApp("/personal/spending?mode=personal");
    expect(await screen.findByText("No spending the same days last year")).toBeInTheDocument();
    expect(screen.queryByText("vs the same days last year")).not.toBeInTheDocument();
  });

  it("keeps both CSV exports available", async () => {
    personalApi();
    renderApp("/personal/spending?mode=personal");
    await screen.findByRole("table", { name: "Spending by group and category" });
    expect(screen.getByRole("link", { name: "Categories (CSV)" }).getAttribute("href")).toContain("/export/personal/spending.csv");
    expect(screen.getByRole("link", { name: "Merchants (CSV)" }).getAttribute("href")).toContain("/export/personal/merchants.csv");
  });
});
