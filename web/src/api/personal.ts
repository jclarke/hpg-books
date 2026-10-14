/**
 * Personal mode API: types and React Query hooks for /api/personal/*.
 * Every request carries mode=personal (see client.ts). Nothing here is cached
 * outside React Query's memory; localStorage holds only the mode flag.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiDelete, apiGet, apiPatch, apiPost } from "./client";
import type { PersonalCategory } from "./types";

type Params = Record<string, string | number | boolean | null | undefined>;

export type Kind = "expense" | "income" | "transfer" | "funding";

export interface PSplit {
  category_id: number;
  amount_cents: number;
  note: string;
}

export interface PTxn {
  id: string;
  date: string;
  amount_cents: number;
  name: string;
  merchant: string;
  merchant_key: string;
  account_id: string;
  account_label: string;
  category_id: number | null;
  category: string;
  group: string;
  kind: Kind;
  source: string;
  confidence: number;
  note: string;
  tags: string[];
  pending: boolean;
  from_business: boolean;
  editable: boolean;
  transfer_pair: string | null;
  splits: PSplit[];
  status: string;
  day_end_balance_cents?: number | null;
  suggestion?: { category_id: number; category: string; reason: string; confidence: number } | null;
  similar?: number;
}

export interface PTxnPage {
  rows: PTxn[];
  total: number;
  in_cents: number;
  out_cents: number;
  offset: number;
  limit: number;
}

export interface PTxnDetail extends PTxn {
  description: string;
  raw_name?: string;
  history: { id: number; ts: string; action: string; field: string | null; old_value: string | null; new_value: string | null; actor: string | null; note: string | null }[];
}

export interface CategoryTotal {
  category_id: number;
  category: string;
  group: string;
  cents: number;
  count: number;
}

export interface NetWorthPoint {
  date: string;
  assets_cents: number;
  liabilities_cents: number;
  net_cents: number;
}

export interface NetWorthAccount {
  id: string;
  label: string;
  last4: string;
  institution: string;
  class: string;
  liability: boolean;
  balance_cents: number;
  contribution_cents: number;
  change_cents: number;
  included: boolean;
  anchored: boolean;
  last_updated: string | null;
  stale: boolean;
}

export interface NetWorth {
  as_of: string;
  range: string;
  interval: string;
  assets_cents: number;
  liabilities_cents: number;
  net_cents: number;
  change_30_cents: number;
  change_90_cents: number;
  points: NetWorthPoint[];
  accounts: NetWorthAccount[];
  by_class: { class: string; label: string; cents: number; count: number }[];
  stale_count: number;
}

export interface AccountsOverview {
  as_of: string;
  account_count: number;
  groups: {
    class: string;
    label: string;
    total_cents: number;
    accounts: {
      id: string;
      label: string;
      last4: string;
      institution: string;
      class: string;
      liability: boolean;
      balance_cents: number;
      last_updated: string | null;
      stale: boolean;
      anchored: boolean;
      included: boolean;
      spark: number[];
      transaction_count: number;
    }[];
  }[];
}

export interface BudgetRow {
  id: number;
  kind: "category" | "group";
  category_id: number | null;
  group_name: string | null;
  name: string;
  group: string;
  month_specific: boolean;
  budget_cents: number;
  rollover: boolean;
  carry_cents: number;
  available_cents: number;
  spent_cents: number;
  remaining_cents: number;
  pct_used: number | null;
  projected_cents: number;
  projected_over: boolean;
  status: "ok" | "warning" | "over";
}

export interface Budgets {
  month: string;
  days_in_month: number;
  days_elapsed: number;
  rows: BudgetRow[];
  budgeted_cents: number;
  spent_cents: number;
  remaining_cents: number;
  total_spending_cents: number;
  unbudgeted: { category_id: number; category: string; group: string; cents: number }[];
  alerts: { name: string; status: string; pct_used: number | null; spent_cents: number; available_cents: number }[];
  counts: Record<"ok" | "warning" | "over", number>;
}

export interface RecurringItem {
  series_key: string;
  merchant_key: string;
  merchant: string;
  direction: "in" | "out";
  account_id: string;
  account_label: string;
  category_id: number | null;
  category: string;
  group: string;
  cadence: "weekly" | "biweekly" | "monthly" | "quarterly" | "annual";
  count: number;
  first_seen: string;
  last_date: string;
  next_expected: string;
  typical_cents: number;
  last_cents: number;
  monthly_cents: number;
  annual_cents: number;
  price_change: { from_cents: number; to_cents: number; change_cents: number } | null;
  may_be_cancelled: boolean;
  new: boolean;
  possible_duplicate: boolean;
  kind: "bill" | "subscription" | "income";
  /** bill: a sure recurring charge; review: repeats, but the owner should say whether it is a bill. */
  tier: "bill" | "review" | "income";
  review_reasons: string[];
  decision: "confirmed" | null;
  status: "active" | "cancelled" | "ignored" | "confirmed";
  user_confirmed: boolean;
  notes: string;
}

export interface RecurringDecision {
  merchant_key: string;
  merchant: string;
  status: "confirmed" | "ignored";
  created_at: string;
  updated_at: string;
  cadence: RecurringItem["cadence"] | null;
  typical_cents: number | null;
}

export interface RecurringReview {
  as_of: string;
  needs_review: number;
  review: RecurringItem[];
  confirmed: RecurringDecision[];
  ignored: RecurringDecision[];
}

export interface Recurring {
  as_of: string;
  items: RecurringItem[];
  subscription_monthly_cents: number;
  subscription_annual_cents: number;
  bill_monthly_cents: number;
  counts: Record<string, number>;
}

export interface BillEvent {
  date: string;
  series_key: string;
  merchant: string;
  kind: "bill" | "subscription" | "income";
  direction: "in" | "out";
  cadence: string;
  category: string;
  account_label: string;
  status: "paid" | "due" | "late" | "overdue";
  amount_cents: number;
}

export interface Bills {
  as_of: string;
  through: string;
  events: BillEvent[];
  due_this_week_cents: number;
  due_this_month_cents: number;
  overdue_count: number;
  income_expected_cents: number;
}

export interface DebtAccount {
  id: string;
  label: string;
  last4: string;
  institution: string;
  kind: "card" | "loan";
  balance_cents: number;
  apr: number | null;
  apr_text: string;
  minimum_payment_cents: number | null;
  due_date: string | null;
  maturity_date?: string | null;
}

export interface DebtPayoff {
  as_of: string;
  cards: DebtAccount[];
  loans: DebtAccount[];
  mortgages_excluded: number;
  totals: {
    card_balance_cents: number;
    loan_balance_cents: number;
    card_minimums_cents: number;
    cards_missing_minimum: number;
    cards_missing_apr: number;
  };
}

export type OfferSource = "manual" | "grok" | "import";

export interface DebtOffer {
  id: number;
  lender: string;
  amount_cents: number;
  amount: number;
  apr: number;
  fee_pct: number;
  fee_from_proceeds: boolean;
  term_months: number;
  monthly_payment_cents: number | null;
  monthly_payment: number | null;
  source: OfferSource;
  notes: string;
  expires_on: string | null;
  expired: boolean;
  created_at: string;
  updated_at: string;
}

/** What the add/edit form sends. Money in cents. */
export interface DebtOfferInput {
  lender: string;
  amount_cents: number;
  apr: number;
  fee_pct: number;
  fee_from_proceeds: boolean;
  term_months: number;
  monthly_payment_cents: number | null;
  notes: string;
  expires_on: string | null;
  source?: OfferSource;
}

export interface DebtOfferWrite {
  ok: true;
  offer: DebtOffer | { id: number; deleted: true };
  rows: DebtOffer[];
}

export interface Goal {
  id: number;
  name: string;
  target_cents: number;
  target_date: string | null;
  account_id: string | null;
  account_label: string;
  current_cents: number;
  remaining_cents: number;
  progress_pct: number;
  months_left: number | null;
  required_monthly_cents: number | null;
  monthly_contribution_cents: number;
  observed_monthly_cents: number;
  status: "done" | "on_track" | "behind" | "no_date";
  archived: boolean;
}

export interface MonthTotals {
  income_cents: number;
  earned_cents: number;
  owner_draws_cents: number;
  spending_cents: number;
  net_cents: number;
  savings_rate: number | null;
  savings_rate_without_draws: number | null;
}

export interface PDashboard {
  as_of: string;
  month: string;
  has_accounts: boolean;
  has_data: boolean;
  net_worth: { net_cents: number; change_30_cents: number; change_90_cents: number; stale_count: number; points: NetWorthPoint[] };
  month_totals: MonthTotals;
  top_categories: CategoryTotal[];
  budget: { budgeted_cents: number; spent_cents: number; counts: Record<string, number>; alerts: Budgets["alerts"] };
  upcoming_bills: BillEvent[];
  subscription_monthly_cents: number;
  recurring_review_count: number;
  review_count: number;
  goals: Goal[];
  recent: PTxn[];
}

export interface Spending {
  start: string;
  end: string;
  prior_start: string;
  prior_end: string;
  last_year_start: string;
  last_year_end: string;
  total: { cents: number; prior_cents: number; delta: number; pct: number | null };
  same_period_last_year_cents: number;
  ytd: { cents: number; prior_cents: number; delta: number; pct: number | null };
  groups: { group: string; cents: number; prior_cents: number; last_year_cents: number; share: number | null; categories: (CategoryTotal & { prior_cents: number; last_year_cents: number; share: number | null })[] }[];
  categories: (CategoryTotal & { prior_cents: number; last_year_cents: number; share: number | null })[];
  merchants: { merchant_key: string; merchant: string; cents: number; count: number; average_cents: number; last_date: string }[];
  merchant_count: number;
  trend: { month: string; total_cents: number; groups: Record<string, number> }[];
  trend_groups: string[];
}

export interface CashFlowMonth extends MonthTotals {
  month: string;
  sources: Record<string, number>;
}

export interface CashFlow {
  start_month: string;
  end_month: string;
  months: CashFlowMonth[];
  income_cents: number;
  earned_cents: number;
  owner_draws_cents: number;
  spending_cents: number;
  net_cents: number;
  savings_rate: number | null;
  savings_rate_without_draws: number | null;
  sources: { source: string; cents: number }[];
}

export interface MonthlySummary extends MonthTotals {
  month: string;
  prior_month: string;
  prior: { income_cents: number; spending_cents: number; savings_rate: number | null };
  average_12: { income_cents: number; spending_cents: number };
  sources: Record<string, number>;
  category_changes: { category: string; group: string; cents: number; prior_cents: number; delta: number }[];
  biggest: PTxn[];
  new_subscriptions: { merchant: string; typical_cents: number; cadence: string }[];
  changed_subscriptions: { merchant: string; from_cents: number; to_cents: number }[];
  budget: { budgeted_cents: number; spent_cents: number; rows: BudgetRow[]; counts: Record<string, number> };
  net_worth_start_cents: number;
  net_worth_end_cents: number;
  net_worth_change_cents: number;
  summary: string[];
}

export interface PRule {
  id: number;
  priority: number;
  pattern: string;
  field: string;
  account_id: string | null;
  amount_sign: string | null;
  category_id: number;
  category: string;
  group: string;
  merchant_rename: string | null;
  confidence: number;
  active: boolean;
  note: string;
  hits: number;
  created_by: string;
}

export interface PCategoryRow extends PersonalCategory {
  transaction_count: number;
}

export interface Merchant {
  key: string;
  display_name: string;
  renamed: boolean;
  count: number;
  spellings: string[];
}

export interface AccountSetting {
  id: string;
  name: string;
  display_name: string;
  nickname: string;
  label: string;
  last4: string;
  institution: string;
  type: string;
  class: string;
  subtype: string;
  scope: "business" | "personal" | "excluded";
  include_in_net_worth: boolean;
  sync_enabled: boolean;
  closed: boolean;
  transaction_count: number;
}

export interface PStatus {
  ready: boolean;
  account_count: number;
  transaction_count: number;
  setup_steps: string[];
}

export interface WriteResult<T = unknown> {
  ok: true;
  result: T;
  review_count: number;
}

export const pkeys = {
  all: ["personal"] as const,
  status: () => ["personal", "status"] as const,
  dashboard: () => ["personal", "dashboard"] as const,
  netWorth: (p: Params) => ["personal", "net-worth", p] as const,
  accounts: () => ["personal", "accounts"] as const,
  register: (id: string, p: Params) => ["personal", "register", id, p] as const,
  transactions: (p: Params) => ["personal", "transactions", p] as const,
  transaction: (id: string) => ["personal", "transaction", id] as const,
  spending: (p: Params) => ["personal", "spending", p] as const,
  cashFlow: (p: Params) => ["personal", "cash-flow", p] as const,
  budgets: (month: string) => ["personal", "budgets", month] as const,
  suggestions: (month: string) => ["personal", "budget-suggestions", month] as const,
  recurring: () => ["personal", "recurring"] as const,
  recurringReview: () => ["personal", "recurring-review"] as const,
  bills: (days: number) => ["personal", "bills", days] as const,
  goals: () => ["personal", "goals"] as const,
  debtPayoff: () => ["personal", "debt-payoff"] as const,
  debtOffers: () => ["personal", "debt-offers"] as const,
  summary: (month: string) => ["personal", "summary", month] as const,
  review: () => ["personal", "review"] as const,
  categories: () => ["personal", "categories"] as const,
  rules: () => ["personal", "rules"] as const,
  merchants: () => ["personal", "merchants"] as const,
  accountSettings: ["account-settings"] as const,
};

function get<T>(path: string, params?: Params) {
  return ({ signal }: { signal?: AbortSignal }) => apiGet<T>(`/personal${path}`, params, { signal });
}

export const usePersonalStatus = () => useQuery({ queryKey: pkeys.status(), queryFn: get<PStatus>("/status") });
export const usePersonalDashboard = () => useQuery({ queryKey: pkeys.dashboard(), queryFn: get<PDashboard>("/dashboard") });
export const useNetWorth = (p: Params) => useQuery({ queryKey: pkeys.netWorth(p), queryFn: get<NetWorth>("/net-worth", p), placeholderData: keepPreviousData });
export const usePersonalAccounts = () => useQuery({ queryKey: pkeys.accounts(), queryFn: get<AccountsOverview>("/accounts") });
export const usePersonalRegister = (id: string, p: Params) =>
  useQuery({
    queryKey: pkeys.register(id, p),
    queryFn: get<PTxnPage & { account: { id: string; label: string; class: string; last4: string; institution: string } }>(`/accounts/${encodeURIComponent(id)}/register`, p),
    placeholderData: keepPreviousData,
  });
export const usePersonalTransactions = (p: Params) =>
  useQuery({ queryKey: pkeys.transactions(p), queryFn: get<PTxnPage>("/transactions", p), placeholderData: keepPreviousData });
export const usePersonalTransaction = (id: string | null) =>
  useQuery({
    queryKey: pkeys.transaction(id ?? ""),
    queryFn: get<{ transaction: PTxnDetail }>(`/transactions/${encodeURIComponent(id ?? "")}`),
    enabled: Boolean(id),
  });
export const useSpending = (p: Params) => useQuery({ queryKey: pkeys.spending(p), queryFn: get<Spending>("/spending", p), placeholderData: keepPreviousData });
export const useCashFlow = (p: Params) => useQuery({ queryKey: pkeys.cashFlow(p), queryFn: get<CashFlow>("/cash-flow", p), placeholderData: keepPreviousData });
export const useBudgets = (month: string) =>
  useQuery({ queryKey: pkeys.budgets(month), queryFn: get<Budgets>("/budgets", { month }), placeholderData: keepPreviousData });
export const useBudgetSuggestions = (month: string, enabled: boolean) =>
  useQuery({
    queryKey: pkeys.suggestions(month),
    queryFn: get<{ rows: { category_id: number; category: string; group: string; average_cents: number }[] }>("/budgets/suggestions", { month }),
    enabled,
  });
export const useRecurring = () => useQuery({ queryKey: pkeys.recurring(), queryFn: get<Recurring>("/recurring") });
export const useRecurringReview = () => useQuery({ queryKey: pkeys.recurringReview(), queryFn: get<RecurringReview>("/recurring/review") });
export const useBills = (days: number) => useQuery({ queryKey: pkeys.bills(days), queryFn: get<Bills>("/bills", { days }) });
export const useDebtPayoff = () => useQuery({ queryKey: pkeys.debtPayoff(), queryFn: get<DebtPayoff>("/debt-payoff") });
export const useDebtOffers = () => useQuery({ queryKey: pkeys.debtOffers(), queryFn: get<{ rows: DebtOffer[] }>("/debt-offers") });

/** Create (no id), update (id), or delete saved loan offers; the list is refetched after each. */
export function useDebtOfferWrite() {
  const client = useQueryClient();
  const onSuccess = (data: DebtOfferWrite) => {
    client.setQueryData(pkeys.debtOffers(), { ok: true, rows: data.rows });
    void client.invalidateQueries({ queryKey: pkeys.debtOffers() });
  };
  return {
    save: useMutation({
      mutationFn: ({ id, body }: { id?: number; body: DebtOfferInput }) =>
        id === undefined ? apiPost<DebtOfferWrite>("/personal/debt-offers", body) : apiPatch<DebtOfferWrite>(`/personal/debt-offers/${id}`, body),
      onSuccess,
    }),
    remove: useMutation({ mutationFn: (id: number) => apiDelete<DebtOfferWrite>(`/personal/debt-offers/${id}`), onSuccess }),
  };
}

export const useGoals = () => useQuery({ queryKey: pkeys.goals(), queryFn: get<{ rows: Goal[] }>("/goals") });
export const useMonthlySummary = (month: string) =>
  useQuery({ queryKey: pkeys.summary(month), queryFn: get<MonthlySummary>("/summary", { month }), placeholderData: keepPreviousData });
export const usePersonalReview = () => useQuery({ queryKey: pkeys.review(), queryFn: get<{ rows: PTxn[]; count: number }>("/review") });
export const usePersonalCategories = () => useQuery({ queryKey: pkeys.categories(), queryFn: get<{ rows: PCategoryRow[] }>("/categories") });
export const usePersonalRules = () => useQuery({ queryKey: pkeys.rules(), queryFn: get<{ rows: PRule[] }>("/rules") });
export const useMerchants = () => useQuery({ queryKey: pkeys.merchants(), queryFn: get<{ rows: Merchant[] }>("/merchants") });
export const useAccountSettings = () =>
  useQuery({
    queryKey: pkeys.accountSettings,
    queryFn: ({ signal }) => apiGet<{ rows: AccountSetting[]; counts: Record<string, number> }>("/accounts/settings", undefined, { signal }),
  });

/** A personal write; afterwards every personal figure and the session (review badge) refetch. */
export function usePersonalWrite<B, R = unknown>(path: string | ((body: B) => string)) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: B) => apiPost<WriteResult<R>>(`/personal${typeof path === "function" ? path(body) : path}`, body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: pkeys.all });
      void client.invalidateQueries({ queryKey: ["session"] });
    },
  });
}

export function useRulePreview() {
  return useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      apiPost<{ count: number; would_change: number; manual: number; sample: { id: string; date: string; amount_cents: number; name: string; category: string }[] }>(
        "/personal/rules/preview",
        body,
      ),
  });
}

export function useAccountSettingsWrite() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, changes }: { id: string; changes: Partial<Pick<AccountSetting, "scope" | "class" | "include_in_net_worth" | "sync_enabled" | "display_name" | "closed">> }) =>
      apiPatch<{ ok: true; account: AccountSetting; scope_change?: { old_scope: string; scope: string; transactions: number; changed: boolean } }>(
        `/accounts/${encodeURIComponent(id)}/settings`,
        changes,
      ),
    onSuccess: () => {
      // Moving an account changes both modes' numbers.
      void client.invalidateQueries();
    },
  });
}
