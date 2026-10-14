import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPatch, apiPost } from "./client";
import { getSiteConfig } from "../lib/siteConfig";
import type {
  AccountRow,
  AuditRow,
  CalendarData,
  Dashboard,
  Margins,
  PaymentRow,
  Payments,
  PaymentSummary,
  PaymentUpdate,
  Pnl,
  PnlLines,
  Register,
  ReviewRow,
  Rule,
  RulePreview,
  SearchResults,
  TableReport,
  TxnDetail,
  TxnPage,
  Vendors,
  WhmcsChurn,
  WhmcsCustomer,
  WhmcsCustomerHit,
  WhmcsDunning,
  WhmcsMrr,
  WhmcsNotReady,
  WhmcsReconcile,
  WhmcsRefunds,
  WhmcsRevenue,
  WhmcsStatus,
  WhmcsSummary,
} from "./types";

type Params = Record<string, string | number | boolean | null | undefined>;

export const keys = {
  dashboard: (p: Params) => ["dashboard", p] as const,
  transactions: (p: Params) => ["transactions", p] as const,
  transaction: (id: string) => ["transaction", id] as const,
  accounts: (p: Params) => ["accounts", p] as const,
  register: (id: string, p: Params) => ["register", id, p] as const,
  pnl: (p: Params) => ["pnl", p] as const,
  pnlLines: (p: Params) => ["pnl-lines", p] as const,
  report: (name: string, p: Params) => ["report", name, p] as const,
  vendors: (p: Params) => ["vendors", p] as const,
  review: (p: Params) => ["review", p] as const,
  rules: () => ["rules"] as const,
  rulePreview: (p: Params) => ["rule-preview", p] as const,
  calendar: (p: Params) => ["calendar", p] as const,
  audit: (p: Params) => ["audit", p] as const,
  search: (q: string, mode = "business") => ["search", q, mode] as const,
  whmcs: (name: string, p: Params = {}) => ["whmcs", name, p] as const,
  margins: ["margins"] as const,
  payments: (mode: "business" | "personal") => ["payments", mode] as const,
};

/** Query families whose figures change when a classification changes. */
export const LEDGER_FAMILIES = ["dashboard", "transactions", "transaction", "accounts", "register", "pnl", "pnl-lines", "report", "vendors", "review", "rules", "calendar", "audit", "search", "session", "payments"];

export function useDashboard(p: Params) {
  return useQuery({ queryKey: keys.dashboard(p), queryFn: ({ signal }) => apiGet<Dashboard>("/dashboard", p, { signal }), placeholderData: keepPreviousData });
}

export function useTransactions(p: Params) {
  return useQuery({ queryKey: keys.transactions(p), queryFn: ({ signal }) => apiGet<TxnPage>("/transactions", p, { signal }), placeholderData: keepPreviousData });
}

export function useTransaction(id: string | null) {
  return useQuery({
    queryKey: keys.transaction(id ?? ""),
    queryFn: ({ signal }) => apiGet<{ transaction: TxnDetail }>(`/transactions/${encodeURIComponent(id ?? "")}`, undefined, { signal }).then((r) => r.transaction),
    enabled: Boolean(id),
  });
}

export function useAccounts(p: Params) {
  return useQuery({ queryKey: keys.accounts(p), queryFn: ({ signal }) => apiGet<{ rows: AccountRow[]; start: string; end: string }>("/accounts", p, { signal }), placeholderData: keepPreviousData });
}

export function useRegister(id: string, p: Params) {
  return useQuery({ queryKey: keys.register(id, p), queryFn: ({ signal }) => apiGet<Register>(`/accounts/${encodeURIComponent(id)}/register`, p, { signal }), placeholderData: keepPreviousData });
}

export function usePnl(p: Params) {
  return useQuery({ queryKey: keys.pnl(p), queryFn: ({ signal }) => apiGet<Pnl>("/pnl", p, { signal }), placeholderData: keepPreviousData });
}

export function usePnlLines(p: Params | null) {
  return useQuery({
    queryKey: keys.pnlLines(p ?? {}),
    queryFn: ({ signal }) => apiGet<PnlLines>("/pnl/lines", p ?? {}, { signal }),
    enabled: p !== null,
  });
}

export function useReport(name: string, p: Params) {
  return useQuery({ queryKey: keys.report(name, p), queryFn: ({ signal }) => apiGet<TableReport>(`/reports/${name}`, p, { signal }), placeholderData: keepPreviousData });
}

export function useVendors(p: Params) {
  return useQuery({ queryKey: keys.vendors(p), queryFn: ({ signal }) => apiGet<Vendors>("/vendors", p, { signal }), placeholderData: keepPreviousData });
}

export function useReview(p: Params = {}) {
  return useQuery({ queryKey: keys.review(p), queryFn: ({ signal }) => apiGet<{ rows: ReviewRow[]; count: number }>("/review", p, { signal }) });
}

export function useRules() {
  return useQuery({ queryKey: keys.rules(), queryFn: ({ signal }) => apiGet<{ rows: Rule[] }>("/rules", undefined, { signal }).then((r) => r.rows) });
}

export function useRulePreview(pattern: string, field = "any") {
  const p = { pattern, field };
  return useQuery({
    queryKey: keys.rulePreview(p),
    queryFn: ({ signal }) => apiGet<RulePreview>("/rules/preview", p, { signal }),
    enabled: pattern.trim().length > 0,
    retry: false,
    placeholderData: keepPreviousData,
  });
}

export function useCalendar(p: Params) {
  return useQuery({ queryKey: keys.calendar(p), queryFn: ({ signal }) => apiGet<CalendarData>("/calendar", p, { signal }) });
}

export function useAudit(p: Params) {
  return useQuery<AuditRow[]>({ queryKey: keys.audit(p), queryFn: ({ signal }) => apiGet<{ rows: AuditRow[] }>("/audit", p, { signal }).then((r) => r.rows), placeholderData: keepPreviousData });
}

export function useSearch(q: string, mode: "business" | "personal" = "business") {
  return useQuery({
    queryKey: keys.search(q, mode),
    queryFn: ({ signal }) => apiGet<SearchResults>("/search", { q, mode }, { signal }),
    enabled: q.trim().length >= 2,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
}

// --- WHMCS billing (read-only; refreshed by `hpgbooks whmcs sync`) ----------------

type Ready<T> = T | WhmcsNotReady;

/** WHMCS endpoints answer 404 when the integration is off; never ask them. */
function whmcsOn(): boolean {
  return getSiteConfig().features.whmcs;
}

function whmcsQuery<T>(name: string, path: string, p: Params = {}, enabled = true) {
  return useQuery({
    queryKey: keys.whmcs(name, p),
    queryFn: ({ signal }) => apiGet<T>(path, p, { signal }),
    placeholderData: keepPreviousData,
    enabled: enabled && whmcsOn(),
  });
}

export function useWhmcsStatus() {
  return whmcsQuery<WhmcsStatus>("status", "/whmcs/status");
}

export function useWhmcsSummary() {
  return whmcsQuery<WhmcsSummary>("summary", "/whmcs/summary");
}

export function useWhmcsRevenue(p: Params) {
  return whmcsQuery<Ready<WhmcsRevenue>>("revenue", "/whmcs/revenue", p);
}

export function useWhmcsMrr(p: Params) {
  return whmcsQuery<Ready<WhmcsMrr>>("mrr", "/whmcs/mrr", p);
}

export function useWhmcsChurn(p: Params) {
  return whmcsQuery<Ready<WhmcsChurn>>("churn", "/whmcs/churn", p);
}

export function useWhmcsRefunds(p: Params) {
  return whmcsQuery<Ready<WhmcsRefunds>>("refunds", "/whmcs/refunds", p);
}

export function useWhmcsDunning(p: Params) {
  return whmcsQuery<Ready<WhmcsDunning>>("dunning", "/whmcs/dunning", p);
}

export function useWhmcsReconcile(p: Params) {
  return whmcsQuery<Ready<WhmcsReconcile>>("reconcile", "/whmcs/reconcile", p);
}

/** POST so the search text, often a name or email, stays out of URLs and request logs. */
export function useWhmcsCustomerSearch(q: string, brand: string) {
  const text = q.trim();
  return useQuery({
    queryKey: keys.whmcs("customers", { q: text, brand }),
    queryFn: () => apiPost<Ready<{ ready: true; rows: WhmcsCustomerHit[] }>>("/whmcs/customers/search", { q: text, brand }),
    placeholderData: keepPreviousData,
    enabled: whmcsOn() && (text.length >= 2 || /^\d+$/.test(text)),
  });
}

export function useWhmcsCustomer(brand: string, id: string) {
  return useQuery({
    queryKey: keys.whmcs("customer", { brand, id }),
    queryFn: ({ signal }) => apiGet<Ready<{ ready: true; customer: WhmcsCustomer }>>(`/whmcs/customers/${encodeURIComponent(brand)}/${encodeURIComponent(id)}`, undefined, { signal }),
    enabled: whmcsOn() && Boolean(brand && /^\d+$/.test(id)),
  });
}

// --- Server margins (costs and mappings are edited on the page; revenue follows each WHMCS sync) ---

export function useMargins() {
  return useQuery({
    queryKey: keys.margins,
    queryFn: ({ signal }) => apiGet<Ready<Margins>>("/margins", undefined, { signal }),
    placeholderData: keepPreviousData,
    enabled: getSiteConfig().features.margins,
  });
}

// --- Card and loan payments (both modes; the mode is always named, never inferred from the path) ---

export function usePayments(mode: "business" | "personal") {
  return useQuery({
    queryKey: keys.payments(mode),
    queryFn: ({ signal }) => apiGet<Payments>("/payments", { mode }, { signal }),
  });
}

export function useUpdatePayment(mode: "business" | "personal") {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, changes }: { id: string; changes: PaymentUpdate }) =>
      apiPatch<{ ok: true; row: PaymentRow; summary: PaymentSummary }>(`/payments/${encodeURIComponent(id)}`, changes, { mode }),
    onSuccess: (result) => {
      client.setQueryData<Payments>(keys.payments(mode), (data) =>
        data ? { ...data, rows: data.rows.map((row) => (row.id === result.row.id ? result.row : row)), summary: result.summary } : data,
      );
      void client.invalidateQueries({ queryKey: keys.payments(mode) });
    },
  });
}
