import { lazy, Suspense, useEffect, useRef, useState, type ReactNode } from "react";
import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { allowedMode, counterpartPath, counterpartSearch, isMode, modeFromPath, readStoredMode, storeMode } from "./lib/mode";
import { ApiError } from "./api/client";
import { sessionKey } from "./hooks/useSession";
import { useConfig } from "./hooks/useConfig";
import { routeEnabled } from "./lib/nav";
import { AppShell } from "./components/AppShell";
import { SkeletonCard } from "./components/Skeleton";
import { ToastProvider } from "./components/Toast";

const DashboardPage = lazy(() => import("./pages/DashboardPage"));
const TransactionsPage = lazy(() => import("./pages/TransactionsPage"));
const AccountsPage = lazy(() => import("./pages/AccountsPage"));
const RegisterPage = lazy(() => import("./pages/RegisterPage"));
const ReportsPage = lazy(() => import("./pages/ReportsPage"));
const PnlPage = lazy(() => import("./pages/PnlPage"));
const TableReportPage = lazy(() => import("./pages/TableReportPage"));
const VendorsPage = lazy(() => import("./pages/VendorsPage"));
const ReviewPage = lazy(() => import("./pages/ReviewPage"));
const CalendarPage = lazy(() => import("./pages/CalendarPage"));
const RulesPage = lazy(() => import("./pages/RulesPage"));
const AuditPage = lazy(() => import("./pages/AuditPage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));
const NotFoundPage = lazy(() => import("./pages/NotFoundPage"));
const WhmcsOverviewPage = lazy(() => import("./pages/WhmcsOverviewPage"));
const WhmcsRevenuePage = lazy(() => import("./pages/WhmcsRevenuePage"));
const WhmcsMrrPage = lazy(() => import("./pages/WhmcsMrrPage"));
const WhmcsChurnPage = lazy(() => import("./pages/WhmcsChurnPage"));
const WhmcsRefundsPage = lazy(() => import("./pages/WhmcsRefundsPage"));
const WhmcsCollectionsPage = lazy(() => import("./pages/WhmcsCollectionsPage"));
const WhmcsReconciliationPage = lazy(() => import("./pages/WhmcsReconciliationPage"));
const WhmcsCustomersPage = lazy(() => import("./pages/WhmcsCustomersPage"));
const WhmcsCustomerPage = lazy(() => import("./pages/WhmcsCustomerPage"));
const WhmcsMarginsPage = lazy(() => import("./pages/WhmcsMarginsPage"));
const PersonalDashboardPage = lazy(() => import("./pages/personal/PersonalDashboardPage"));
const NetWorthPage = lazy(() => import("./pages/personal/NetWorthPage"));
const PersonalAccountsPage = lazy(() => import("./pages/personal/PersonalAccountsPage"));
const PersonalRegisterPage = lazy(() => import("./pages/personal/PersonalRegisterPage"));
const PersonalTransactionsPage = lazy(() => import("./pages/personal/PersonalTransactionsPage"));
const SpendingPage = lazy(() => import("./pages/personal/SpendingPage"));
const CashFlowPage = lazy(() => import("./pages/personal/CashFlowPage"));
const BudgetsPage = lazy(() => import("./pages/personal/BudgetsPage"));
const RecurringPage = lazy(() => import("./pages/personal/RecurringPage"));
const BillsPage = lazy(() => import("./pages/personal/BillsPage"));
const GoalsPage = lazy(() => import("./pages/personal/GoalsPage"));
const DebtPayoffPage = lazy(() => import("./pages/personal/DebtPayoffPage"));
const MonthlySummaryPage = lazy(() => import("./pages/personal/MonthlySummaryPage"));
const PersonalReviewPage = lazy(() => import("./pages/personal/PersonalReviewPage"));
const CategoriesPage = lazy(() => import("./pages/personal/CategoriesPage"));
const PersonalSettingsPage = lazy(() => import("./pages/personal/PersonalSettingsPage"));

export function makeQueryClient(): QueryClient {
  // A 401 means the tailnet session went idle. Refetching /api/session shows the sign-in screen.
  const bounceToLogin = (error: unknown) => {
    if (error instanceof ApiError && error.status === 401) {
      void client.invalidateQueries({ queryKey: sessionKey });
    }
  };
  const client = new QueryClient({
    queryCache: new QueryCache({ onError: bounceToLogin }),
    mutationCache: new MutationCache({ onError: bounceToLogin }),
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        refetchOnWindowFocus: false,
        retry: (count, error) => !(error instanceof ApiError && error.status >= 400 && error.status < 500) && count < 2,
      },
    },
  });
  return client;
}

export const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true } as const;

/** Routes behind a feature flag (WHMCS, server margins) are "not found" when it is off. */
function Gated({ children }: { children: ReactNode }) {
  const { features } = useConfig();
  const { pathname } = useLocation();
  return routeEnabled(pathname, features) ? <>{children}</> : <NotFoundPage />;
}

function PnlRedirect() {
  const location = useLocation();
  return <Navigate to={`/reports/pnl${location.search}`} replace />;
}

/**
 * Applies the mode on load: ?mode= wins; with no ?mode= on the root page, the
 * remembered mode (localStorage hpgbooks.mode) decides; the default is business.
 * Afterwards the path is the mode, and it is remembered on every change.
 * A mode the install does not have (features.business / features.personal)
 * sends you to the matching page of the other mode.
 */
export function ModeGate({ children }: { children: ReactNode }) {
  const location = useLocation();
  const { features } = useConfig();
  const first = useRef(true);
  const urlMode = new URLSearchParams(location.search).get("mode");
  const pathMode = modeFromPath(location.pathname);
  const onlyMode = allowedMode(pathMode, features);
  let target: string | null = null;
  if (onlyMode !== pathMode) {
    target = `${counterpartPath(location.pathname, onlyMode)}${counterpartSearch(location.search, onlyMode)}`;
  } else if (isMode(urlMode) && urlMode !== pathMode && allowedMode(urlMode, features) === urlMode) {
    target = `${counterpartPath(location.pathname, urlMode)}${counterpartSearch(location.search, urlMode)}`;
  } else if (first.current && !isMode(urlMode) && location.pathname === "/" && readStoredMode() === "personal" && allowedMode("personal", features) === "personal") {
    target = `/personal${counterpartSearch(location.search, "personal")}`;
  }
  useEffect(() => {
    first.current = false;
    if (target === null) storeMode(pathMode);
  }, [target, pathMode]);
  if (target !== null) return <Navigate to={target} replace />;
  return <>{children}</>;
}

export function AppRoutes() {
  return (
    <ModeGate>
      <AppShell>
        <Suspense
          fallback={
            <div className="page-fallback">
              <SkeletonCard height={60} />
              <SkeletonCard height={220} />
            </div>
          }
        >
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/transactions" element={<TransactionsPage />} />
            <Route path="/accounts" element={<AccountsPage />} />
            <Route path="/accounts/:accountId" element={<RegisterPage />} />
            <Route path="/reports" element={<ReportsPage />} />
            <Route path="/reports/pnl" element={<PnlPage />} />
            <Route path="/pnl" element={<PnlRedirect />} />
            <Route path="/reports/:name" element={<TableReportPage />} />
            <Route path="/vendors" element={<VendorsPage />} />
            <Route path="/review" element={<ReviewPage />} />
            <Route path="/calendar" element={<CalendarPage />} />
            <Route path="/rules" element={<RulesPage />} />
            <Route path="/audit" element={<AuditPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/whmcs" element={<Gated><WhmcsOverviewPage /></Gated>} />
            <Route path="/whmcs/revenue" element={<Gated><WhmcsRevenuePage /></Gated>} />
            <Route path="/whmcs/mrr" element={<Gated><WhmcsMrrPage /></Gated>} />
            <Route path="/whmcs/churn" element={<Gated><WhmcsChurnPage /></Gated>} />
            <Route path="/whmcs/refunds" element={<Gated><WhmcsRefundsPage /></Gated>} />
            <Route path="/whmcs/collections" element={<Gated><WhmcsCollectionsPage /></Gated>} />
            <Route path="/whmcs/reconciliation" element={<Gated><WhmcsReconciliationPage /></Gated>} />
            <Route path="/whmcs/margins" element={<Gated><WhmcsMarginsPage /></Gated>} />
            <Route path="/whmcs/customers" element={<Gated><WhmcsCustomersPage /></Gated>} />
            <Route path="/whmcs/customers/:brand/:id" element={<Gated><WhmcsCustomerPage /></Gated>} />
            <Route path="/personal" element={<PersonalDashboardPage />} />
            <Route path="/personal/net-worth" element={<NetWorthPage />} />
            <Route path="/personal/accounts" element={<PersonalAccountsPage />} />
            <Route path="/personal/accounts/:accountId" element={<PersonalRegisterPage />} />
            <Route path="/personal/transactions" element={<PersonalTransactionsPage />} />
            <Route path="/personal/spending" element={<SpendingPage />} />
            <Route path="/personal/cash-flow" element={<CashFlowPage />} />
            <Route path="/personal/budgets" element={<BudgetsPage />} />
            <Route path="/personal/recurring" element={<RecurringPage />} />
            <Route path="/personal/bills" element={<BillsPage />} />
            <Route path="/personal/goals" element={<GoalsPage />} />
            <Route path="/personal/debt-payoff" element={<DebtPayoffPage />} />
            <Route path="/personal/review-month" element={<MonthlySummaryPage />} />
            <Route path="/personal/review" element={<PersonalReviewPage />} />
            <Route path="/personal/categories" element={<CategoriesPage />} />
            <Route path="/personal/settings" element={<PersonalSettingsPage />} />
            <Route path="*" element={<NotFoundPage />} />
          </Routes>
        </Suspense>
      </AppShell>
    </ModeGate>
  );
}

/** The app also answers under /app/ (where its files live); everywhere else it owns the root. */
function routerBasename(): string | undefined {
  const path = window.location.pathname;
  return path === "/app" || path.startsWith("/app/") ? "/app" : undefined;
}

export function App({ client }: { client?: QueryClient }) {
  const [queryClient] = useState(() => client ?? makeQueryClient());
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <BrowserRouter basename={routerBasename()} future={ROUTER_FUTURE}>
          <AppRoutes />
        </BrowserRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}
