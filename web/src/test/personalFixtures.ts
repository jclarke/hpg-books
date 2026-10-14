import type {
  AccountSetting,
  AccountsOverview,
  Bills,
  Budgets,
  CashFlow,
  DebtPayoff,
  Goal,
  MonthlySummary,
  NetWorth,
  PDashboard,
  PRule,
  PTxn,
  PTxnPage,
  Recurring,
  RecurringReview,
  Spending,
} from "../api/personal";
import type { Payments, PersonalCategory, Session } from "../api/types";
import { session as businessSession, paymentRow } from "./fixtures";

export const CHK = "fake-chk";
export const CARD = "fake-card";

export const pcategories: PersonalCategory[] = [
  { id: 1, name: "Paycheck", group: "Income", kind: "income", color: "", hidden: false, is_system: false },
  { id: 2, name: "Owner draws", group: "Income", kind: "funding", color: "", hidden: false, is_system: true },
  { id: 3, name: "Mortgage", group: "Housing", kind: "expense", color: "", hidden: false, is_system: false },
  { id: 4, name: "Groceries", group: "Food", kind: "expense", color: "", hidden: false, is_system: false },
  { id: 5, name: "Dining out", group: "Food", kind: "expense", color: "", hidden: false, is_system: false },
  { id: 6, name: "Subscriptions", group: "Subscriptions", kind: "expense", color: "", hidden: false, is_system: false },
  { id: 7, name: "Credit card payment", group: "Transfers", kind: "transfer", color: "", hidden: false, is_system: false },
  { id: 8, name: "Uncategorized", group: "Uncategorized", kind: "expense", color: "", hidden: false, is_system: true },
];

export const personalSession: Session = {
  ...businessSession,
  months: ["2026-08", "2026-09"],
  latest_month: "2026-09",
  review_count: 3,
  accounts: [
    { id: CHK, name: "Fake Everyday Checking", short_name: "Fake Everyday Checking", type: "cash", last4: "1111", institution: "Fake Bank" },
    { id: CARD, name: "Fake Rewards Visa", short_name: "Fake Rewards Visa", type: "liability", last4: "3333", institution: "Fake Card Co" },
  ],
  personal: {
    has_accounts: true,
    categories: pcategories,
    setup_steps: ["bin/hpgbooks accounts discover sync/inbox/YYYY-MM-DD/accounts/finance_list_accounts.json", "bin/hpgbooks import sync/inbox/YYYY-MM-DD/"],
  },
};

function txn(partial: Partial<PTxn> & Pick<PTxn, "id" | "merchant" | "amount_cents">): PTxn {
  return {
    date: "2026-09-10",
    name: partial.merchant.toUpperCase(),
    merchant_key: partial.merchant.toUpperCase(),
    account_id: CARD,
    account_label: "Fake Rewards Visa",
    category_id: 4,
    category: "Groceries",
    group: "Food",
    kind: "expense",
    source: "rule",
    confidence: 0.9,
    note: "",
    tags: [],
    pending: false,
    from_business: false,
    editable: true,
    transfer_pair: null,
    splits: [],
    status: "active",
    ...partial,
  };
}

export const ptxns: PTxn[] = [
  txn({ id: "p1", merchant: "Kroger", amount_cents: -12345 }),
  txn({ id: "p2", merchant: "Acme Corp", amount_cents: 400000, account_id: CHK, account_label: "Fake Everyday Checking", category_id: 1, category: "Paycheck", group: "Income", kind: "income" }),
  txn({ id: "biz:m1", merchant: "Fake Mortgage Servicer", amount_cents: -210000, account_label: "Bank 1111 (business)", category_id: 3, category: "Mortgage", group: "Housing", from_business: true, editable: false, source: "business" }),
  txn({ id: "p3", merchant: "Zzq Local Shop", amount_cents: -2500, category_id: 8, category: "Uncategorized", group: "Uncategorized", confidence: 0.2, source: "agent" }),
];

export const ptxnPage: PTxnPage = { rows: ptxns, total: ptxns.length, in_cents: 400000, out_cents: 224845, offset: 0, limit: 100 };

const points = [
  { date: "2026-07-31", assets_cents: 5000000, liabilities_cents: 40000000, net_cents: -35000000 },
  { date: "2026-08-31", assets_cents: 5200000, liabilities_cents: 39900000, net_cents: -34700000 },
  { date: "2026-09-30", assets_cents: 5400000, liabilities_cents: 39800000, net_cents: -34400000 },
];

export const netWorth: NetWorth = {
  as_of: "2026-09-30",
  range: "1Y",
  interval: "month",
  assets_cents: 5400000,
  liabilities_cents: 39800000,
  net_cents: -34400000,
  change_30_cents: 300000,
  change_90_cents: 600000,
  points,
  accounts: [
    { id: CHK, label: "Fake Everyday Checking", last4: "1111", institution: "Fake Bank", class: "cash", liability: false, balance_cents: 845025, contribution_cents: 845025, change_cents: 10000, included: true, anchored: true, last_updated: "2026-09-30", stale: false },
    { id: "fake-loan", label: "Fake Home Mortgage", last4: "5555", institution: "Fake Home Loans", class: "loan", liability: true, balance_cents: 39800000, contribution_cents: -39800000, change_cents: 100000, included: true, anchored: true, last_updated: "2026-09-03", stale: true },
  ],
  by_class: [
    { class: "cash", label: "Cash", cents: 845025, count: 1 },
    { class: "loan", label: "Loans & mortgages", cents: -39800000, count: 1 },
  ],
  stale_count: 1,
};

export const pdashboard: PDashboard = {
  as_of: "2026-09-30",
  month: "2026-09",
  has_accounts: true,
  has_data: true,
  net_worth: { net_cents: -34400000, change_30_cents: 300000, change_90_cents: 600000, stale_count: 1, points },
  month_totals: { income_cents: 900000, earned_cents: 650000, owner_draws_cents: 250000, spending_cents: 700000, net_cents: 200000, savings_rate: 22.2, savings_rate_without_draws: -7.7 },
  top_categories: [{ category_id: 3, category: "Mortgage", group: "Housing", cents: 500000, count: 1 }],
  budget: { budgeted_cents: 60000, spent_cents: 55000, counts: { ok: 0, warning: 1, over: 0 }, alerts: [{ name: "Groceries", status: "warning", pct_used: 91.7, spent_cents: 55000, available_cents: 60000 }] },
  upcoming_bills: [{ date: "2026-10-08", series_key: "NETFLIX|out|0", merchant: "Netflix", kind: "subscription", direction: "out", cadence: "monthly", category: "Subscriptions", account_label: "Fake Rewards Visa", status: "due", amount_cents: 1799 }],
  subscription_monthly_cents: 2998,
  recurring_review_count: 2,
  review_count: 3,
  goals: [],
  recent: ptxns,
};

export const paccounts: AccountsOverview = {
  as_of: "2026-09-30",
  account_count: 2,
  groups: [
    { class: "cash", label: "Cash", total_cents: 845025, accounts: [{ id: CHK, label: "Fake Everyday Checking", last4: "1111", institution: "Fake Bank", class: "cash", liability: false, balance_cents: 845025, last_updated: "2026-09-30", stale: false, anchored: true, included: true, spark: [1, 2, 3], transaction_count: 40 }] },
    { class: "liability", label: "Credit cards", total_cents: 141280, accounts: [{ id: CARD, label: "Fake Rewards Visa", last4: "3333", institution: "Fake Card Co", class: "liability", liability: true, balance_cents: 141280, last_updated: "2026-09-29", stale: false, anchored: true, included: true, spark: [3, 2, 1], transaction_count: 90 }] },
  ],
};

export const spending: Spending = {
  start: "2026-09-01",
  end: "2026-09-30",
  prior_start: "2026-08-01",
  prior_end: "2026-08-31",
  last_year_start: "2025-09-01",
  last_year_end: "2025-09-30",
  total: { cents: 700000, prior_cents: 650000, delta: 50000, pct: 7.7 },
  same_period_last_year_cents: 600000,
  ytd: { cents: 5000000, prior_cents: 4800000, delta: 200000, pct: 4.2 },
  groups: [
    {
      group: "Housing",
      cents: 500000,
      prior_cents: 500000,
      last_year_cents: 500000,
      share: 71.4,
      categories: [{ category_id: 3, category: "Mortgage", group: "Housing", cents: 500000, count: 1, prior_cents: 500000, last_year_cents: 500000, share: 71.4 }],
    },
    {
      group: "Food",
      cents: 200000,
      prior_cents: 150000,
      last_year_cents: 100000,
      share: 28.6,
      categories: [{ category_id: 4, category: "Groceries", group: "Food", cents: 200000, count: 9, prior_cents: 150000, last_year_cents: 100000, share: 28.6 }],
    },
  ],
  categories: [],
  merchants: [{ merchant_key: "KROGER", merchant: "Kroger", cents: 150000, count: 8, average_cents: 18750, last_date: "2026-09-28" }],
  merchant_count: 12,
  trend: [{ month: "2026-09", total_cents: 700000, groups: { Housing: 500000, Food: 200000 } }],
  trend_groups: ["Housing", "Food"],
};

export const cashFlow: CashFlow = {
  start_month: "2026-08",
  end_month: "2026-09",
  months: [
    { month: "2026-08", income_cents: 880000, earned_cents: 630000, owner_draws_cents: 250000, spending_cents: 650000, net_cents: 230000, savings_rate: 26.1, savings_rate_without_draws: -3.2, sources: { Paycheck: 620000, "Interest & dividends": 10000, "Owner draws": 250000, Other: 0 } },
    { month: "2026-09", income_cents: 900000, earned_cents: 650000, owner_draws_cents: 250000, spending_cents: 700000, net_cents: 200000, savings_rate: 22.2, savings_rate_without_draws: -7.7, sources: { Paycheck: 640000, "Interest & dividends": 10000, "Owner draws": 250000, Other: 0 } },
  ],
  income_cents: 1780000,
  earned_cents: 1280000,
  owner_draws_cents: 500000,
  spending_cents: 1350000,
  net_cents: 430000,
  savings_rate: 24.2,
  savings_rate_without_draws: -5.5,
  sources: [
    { source: "Paycheck", cents: 1260000 },
    { source: "Interest & dividends", cents: 20000 },
    { source: "Owner draws", cents: 500000 },
    { source: "Other", cents: 0 },
  ],
};

export const budgets: Budgets = {
  month: "2026-10",
  days_in_month: 31,
  days_elapsed: 1,
  rows: [
    { id: 11, kind: "category", category_id: 4, group_name: null, name: "Groceries", group: "Food", month_specific: false, budget_cents: 60000, rollover: false, carry_cents: 0, available_cents: 60000, spent_cents: 55000, remaining_cents: 5000, pct_used: 91.7, projected_cents: 70000, projected_over: true, status: "warning" },
  ],
  budgeted_cents: 60000,
  spent_cents: 55000,
  remaining_cents: 5000,
  total_spending_cents: 700000,
  unbudgeted: [{ category_id: 3, category: "Mortgage", group: "Housing", cents: 500000 }],
  alerts: [{ name: "Groceries", status: "warning", pct_used: 91.7, spent_cents: 55000, available_cents: 60000 }],
  counts: { ok: 0, warning: 1, over: 0 },
};

export const recurring: Recurring = {
  as_of: "2026-09-30",
  items: [
    { series_key: "NETFLIX|out|0", merchant_key: "NETFLIX", merchant: "Netflix", direction: "out", account_id: CARD, account_label: "Fake Rewards Visa", category_id: 6, category: "Subscriptions", group: "Subscriptions", cadence: "monthly", count: 12, first_seen: "2025-10-08", last_date: "2026-09-08", next_expected: "2026-10-08", typical_cents: 1799, last_cents: 1799, monthly_cents: 1799, annual_cents: 21588, price_change: { from_cents: 1549, to_cents: 1799, change_cents: 250 }, may_be_cancelled: false, new: false, possible_duplicate: false, kind: "subscription", tier: "bill", review_reasons: [], decision: null, status: "active", user_confirmed: false, notes: "" },
    { series_key: "HULU|out|0", merchant_key: "HULU", merchant: "Hulu", direction: "out", account_id: CARD, account_label: "Fake Rewards Visa", category_id: 6, category: "Subscriptions", group: "Subscriptions", cadence: "monthly", count: 8, first_seen: "2025-08-19", last_date: "2026-03-19", next_expected: "2026-04-19", typical_cents: 799, last_cents: 799, monthly_cents: 799, annual_cents: 9588, price_change: null, may_be_cancelled: true, new: false, possible_duplicate: false, kind: "subscription", tier: "bill", review_reasons: [], decision: null, status: "active", user_confirmed: false, notes: "" },
    { series_key: "ZZQ MEAL KIT|out|0", merchant_key: "ZZQ MEAL KIT", merchant: "Zzq Meal Kit", direction: "out", account_id: CARD, account_label: "Fake Rewards Visa", category_id: 4, category: "Groceries", group: "Food", cadence: "monthly", count: 5, first_seen: "2026-05-10", last_date: "2026-09-10", next_expected: "2026-10-10", typical_cents: 6000, last_cents: 6000, monthly_cents: 6000, annual_cents: 72000, price_change: null, may_be_cancelled: false, new: false, possible_duplicate: false, kind: "bill", tier: "review", review_reasons: ["Food is usually not a bill"], decision: null, status: "active", user_confirmed: false, notes: "" },
  ],
  subscription_monthly_cents: 1799,
  subscription_annual_cents: 21588,
  bill_monthly_cents: 50000,
  counts: { subscriptions: 1, bills: 3, income: 1, price_changes: 1, may_be_cancelled: 1, possible_duplicates: 0, needs_review: 1 },
};

export const recurringReview: RecurringReview = {
  as_of: "2026-09-30",
  needs_review: 1,
  review: [recurring.items[2]],
  confirmed: [{ merchant_key: "ZZQ LAWN CARE", merchant: "Zzq Lawn Care", status: "confirmed", created_at: "2026-09-20T10:00:00+00:00", updated_at: "2026-09-20T10:00:00+00:00", cadence: "biweekly", typical_cents: 4500 }],
  ignored: [{ merchant_key: "ZZQ CINEMA", merchant: "Zzq Cinema", status: "ignored", created_at: "2026-09-21T10:00:00+00:00", updated_at: "2026-09-21T10:00:00+00:00", cadence: null, typical_cents: null }],
};

export const bills: Bills = {
  as_of: "2026-09-30",
  through: "2026-11-14",
  events: [
    { date: "2026-09-08", series_key: "NETFLIX|out|0", merchant: "Netflix", kind: "subscription", direction: "out", cadence: "monthly", category: "Subscriptions", account_label: "Fake Rewards Visa", status: "paid", amount_cents: 1799 },
    { date: "2026-10-08", series_key: "NETFLIX|out|0", merchant: "Netflix", kind: "subscription", direction: "out", cadence: "monthly", category: "Subscriptions", account_label: "Fake Rewards Visa", status: "due", amount_cents: 1799 },
  ],
  due_this_week_cents: 0,
  due_this_month_cents: 1799,
  overdue_count: 0,
  income_expected_cents: 860000,
};

export const goals: Goal[] = [
  { id: 1, name: "Emergency fund", target_cents: 1000000, target_date: "2027-09-30", account_id: "fake-sav", account_label: "Fake Savings", current_cents: 500000, remaining_cents: 500000, progress_pct: 50, months_left: 12, required_monthly_cents: 41667, monthly_contribution_cents: 0, observed_monthly_cents: 50000, status: "on_track", archived: false },
];

export const summary: MonthlySummary = {
  month: "2026-09",
  prior_month: "2026-08",
  income_cents: 900000,
  earned_cents: 650000,
  owner_draws_cents: 250000,
  spending_cents: 700000,
  net_cents: 200000,
  savings_rate: 22.2,
  savings_rate_without_draws: -7.7,
  prior: { income_cents: 880000, spending_cents: 650000, savings_rate: 26.1 },
  average_12: { income_cents: 870000, spending_cents: 660000 },
  sources: { Paycheck: 640000, "Interest & dividends": 10000, "Owner draws": 250000, Other: 0 },
  category_changes: [{ category: "Groceries", group: "Food", cents: 200000, prior_cents: 150000, delta: 50000 }],
  biggest: [ptxns[2]],
  new_subscriptions: [],
  changed_subscriptions: [{ merchant: "Netflix", from_cents: 1549, to_cents: 1799 }],
  budget: { budgeted_cents: 60000, spent_cents: 55000, rows: budgets.rows, counts: budgets.counts },
  net_worth_start_cents: -34700000,
  net_worth_end_cents: -34400000,
  net_worth_change_cents: 300000,
  summary: ["In September 2026 you brought in $9,000.00 and spent $7,000.00.", "Netflix changed price from $15.49 to $17.99."],
};

export const prules: PRule[] = [
  { id: 1, priority: 30, pattern: "KROGER|PUBLIX", field: "any", account_id: null, amount_sign: "out", category_id: 4, category: "Groceries", group: "Food", merchant_rename: null, confidence: 0.9, active: true, note: "groceries", hits: 52, created_by: "seed" },
];

export const accountSettings: AccountSetting[] = [
  { id: "checking", name: "Example Bank Checking 1111", display_name: "", nickname: "", label: "Example Bank Checking 1111", last4: "1111", institution: "Example Bank", type: "cash", class: "cash", subtype: "", scope: "business", include_in_net_worth: true, sync_enabled: true, closed: false, transaction_count: 300 },
  { id: CARD, name: "Fake Rewards Visa", display_name: "", nickname: "", label: "Fake Rewards Visa", last4: "3333", institution: "Fake Card Co", type: "liability", class: "liability", subtype: "", scope: "personal", include_in_net_worth: true, sync_enabled: true, closed: false, transaction_count: 236 },
];

/** Personal cards and loans. today = 2026-10-01. */
export const ppayments: Payments = {
  ok: true,
  mode: "personal",
  rows: [
    paymentRow({ id: "fake-loan", label: "Fake Home Mortgage", last4: "5555", institution: "Fake Home Loans", class: "loan", balance_cents: 39800000, due_date: "2026-09-29", apr: "6.125%", autopay: "yes", source: "finance", as_of: "2026-09-03", min_payment_cents: 210000, status: "due_soon", effective_due_date: "2026-09-29", days_until: -2, estimated: true, note: "Due now (estimated)" }),
    paymentRow({ id: CARD, label: "Fake Rewards Visa", last4: "3333", institution: "Fake Card Co", balance_cents: 141280, due_date: "2026-10-01", apr: "22.49%", autopay: "no", source: "statement", as_of: "2026-09-04", stored_min_cents: 4000, min_payment_cents: 4000, status: "due_soon", effective_due_date: "2026-10-01", days_until: 0, note: "Due today" }),
    paymentRow({ id: "fake-auto", label: "Fake Auto Loan", last4: "9090", institution: "Fake Credit Union", class: "loan", balance_cents: 1250000, has_terms: false, note: "No payment info" }),
  ],
  summary: {
    today: "2026-10-01",
    horizon: "2026-10-31",
    next30_cents: 214000,
    next30_count: 2,
    overdue_cents: 0,
    overdue_count: 0,
    by_date: [
      { date: "2026-09-29", cents: 210000, count: 1 },
      { date: "2026-10-01", cents: 4000, count: 1 },
    ],
    unverified_count: 0,
    missing_count: 1,
  },
};

export const debtPayoff: DebtPayoff = {
  as_of: "2026-09-30",
  cards: [
    { id: "fake-dp-card-high", label: "Fake Store Card", last4: "4444", institution: "Fake Bank", kind: "card", balance_cents: 300000, apr: null, apr_text: "", minimum_payment_cents: null, due_date: null },
    { id: "fake-dp-card-a", label: "Fake Rewards Visa", last4: "3333", institution: "Fake Card Co", kind: "card", balance_cents: 800000, apr: 27.99, apr_text: "27.99% purchase / 29.99% cash", minimum_payment_cents: 24000, due_date: "2026-10-15" },
    { id: "fake-dp-card-b", label: "Fake Cashback Card", last4: "5555", institution: "Fake Bank", kind: "card", balance_cents: 500000, apr: 18.5, apr_text: "18.5%", minimum_payment_cents: 15000, due_date: "2026-10-20" },
  ],
  loans: [
    { id: "fake-dp-loan", label: "Fake Personal Loan", last4: "6666", institution: "Fake Lender", kind: "loan", balance_cents: 150000, apr: 11.5, apr_text: "11.5% interest rate", minimum_payment_cents: 50000, due_date: "2026-10-05", maturity_date: "2026-12-05" },
    { id: "fake-dp-auto", label: "Fake Auto Loan", last4: "", institution: "Fake Auto", kind: "loan", balance_cents: 1200000, apr: null, apr_text: "", minimum_payment_cents: 40000, due_date: "2026-10-11", maturity_date: null },
  ],
  mortgages_excluded: 1,
  totals: { card_balance_cents: 1600000, loan_balance_cents: 1350000, card_minimums_cents: 39000, cards_missing_minimum: 1, cards_missing_apr: 1 },
};
