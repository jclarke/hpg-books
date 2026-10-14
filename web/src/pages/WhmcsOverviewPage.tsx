import { Link, useLocation } from "react-router-dom";
import { useWhmcsStatus, useWhmcsSummary } from "../api/queries";
import type { WhmcsSyncLogRow } from "../api/types";
import { Card, CardHeader } from "../components/Card";
import { DataTable, type Column } from "../components/DataTable";
import { ErrorState } from "../components/EmptyState";
import { Icon } from "../components/Icon";
import { KpiCard } from "../components/KpiCard";
import { Money } from "../components/MoneyCell";
import { PageHeader } from "../components/PageHeader";
import { NotSynced, StatusBadgeText } from "../components/Whmcs";
import { withGlobal } from "../hooks/useGlobalFilters";
import { formatDate, formatTimestamp } from "../lib/format";
import { useConfig } from "../hooks/useConfig";
import { navFor } from "../lib/nav";
import { joinList } from "../lib/siteConfig";

export default function WhmcsOverviewPage() {
  const location = useLocation();
  const { features, whmcs_brands: brands } = useConfig();
  const status = useWhmcsStatus();
  const summary = useWhmcsSummary();
  const data = status.data;
  const money = summary.data;

  const logColumns: Column<WhmcsSyncLogRow>[] = [
    { key: "finished", header: "Finished", accessor: (row) => row.finished_at ?? row.started_at, cell: (row) => <span className="nowrap">{formatTimestamp(row.finished_at ?? row.started_at)}</span>, sortable: true },
    { key: "brand", header: "Brand", accessor: (row) => row.brand, sortable: true },
    { key: "status", header: "Status", accessor: (row) => row.status, cell: (row) => <StatusBadgeText status={row.status} />, width: "90px" },
    { key: "clients", header: "Clients", accessor: (row) => num(row.counts.clients), format: "number", hideOnMobile: true },
    { key: "services", header: "Services", accessor: (row) => num(row.counts.services), format: "number", hideOnMobile: true },
    { key: "invoices", header: "Invoices", accessor: (row) => num(row.counts.invoices), format: "number", hideOnMobile: true },
    { key: "payments", header: "Payments", accessor: (row) => num(row.counts.payments), format: "number" },
    { key: "error", header: "Error", accessor: (row) => row.error ?? "", cell: (row) => (row.error ? <span className="text-neg small">{row.error}</span> : <span className="muted">—</span>) },
  ];

  return (
    <div className="page">
      <PageHeader
        title="WHMCS billing"
        subtitle={
          <>
            {brands.length ? `${joinList(brands)}, copied read-only from WHMCS.` : "Copied read-only from WHMCS."}
            {data?.last_sync ? <span className="muted"> Last sync {formatTimestamp(data.last_sync)}.</span> : null}
          </>
        }
      />
      {status.isError ? <ErrorState error={status.error} onRetry={() => status.refetch()} /> : null}
      {data && !data.ready ? (
        <NotSynced />
      ) : (
        <>
          <section className="kpi-grid" aria-label="Recurring revenue">
            {!money ? (
              [0, 1, 2, 3].map((i) => <KpiCard key={i} label="" loading />)
            ) : (
              <>
                <KpiCard label="MRR" cents={money.mrr_cents} spark={money.spark} sparkTone="rev" footer={<span className="muted">Active services, normalized to monthly</span>} />
                <KpiCard label="ARR" cents={money.arr_cents} footer={<span className="muted">MRR × 12</span>} />
                <KpiCard label="Active customers" display={money.customers.toLocaleString("en-US")} footer={<span className="muted">With an active paid service</span>} />
                <KpiCard label="ARPU" cents={money.customers ? Math.round(money.mrr_cents / money.customers) : 0} footer={<span className="muted">MRR per active customer</span>} />
              </>
            )}
          </section>

          <div className="whmcs-brand-grid">
            {(data?.brands ?? []).map((brand) => {
              const mrr = money?.brands.find((item) => item.brand === brand.brand);
              return (
                <Card key={brand.brand} as="article">
                  <CardHeader title={brand.brand} subtitle={brand.last_ok ? `Synced ${formatTimestamp(brand.last_ok)}` : "Never synced"} actions={brand.last_status ? <StatusBadgeText status={brand.last_status} /> : null} />
                  <dl className="whmcs-facts">
                    <div><dt>MRR</dt><dd><Money cents={mrr?.mrr_cents ?? 0} /></dd></div>
                    <div><dt>Active customers</dt><dd className="num">{(mrr?.customers ?? 0).toLocaleString("en-US")}</dd></div>
                    <div><dt>Payments in, all time</dt><dd><Money cents={brand.payments_in_cents} /></dd></div>
                    <div><dt>History</dt><dd>{brand.first_payment ? `${formatDate(brand.first_payment)} – ${formatDate(brand.last_payment)}` : "—"}</dd></div>
                    <div><dt>Clients · services</dt><dd className="num">{(brand.rows.clients ?? 0).toLocaleString("en-US")} · {(brand.rows.services ?? 0).toLocaleString("en-US")}</dd></div>
                    <div><dt>Invoices · payments</dt><dd className="num">{(brand.rows.invoices ?? 0).toLocaleString("en-US")} · {(brand.rows.payments ?? 0).toLocaleString("en-US")}</dd></div>
                  </dl>
                  {brand.last_status === "error" && brand.last_error ? <p className="text-neg small">Last run failed: {brand.last_error}</p> : null}
                </Card>
              );
            })}
          </div>

          <div className="report-grid">
            {navFor("business", features).filter((item) => item.section === "WHMCS" && item.to !== "/whmcs").map((item) => (
              <Card key={item.to} as="article" className="report-card">
                <Link to={withGlobal(item.to, location.search)} className="report-card__link">
                  <span className="report-card__icon">
                    <Icon name={item.icon} size={20} />
                  </span>
                  <span className="report-card__text">
                    <span className="report-card__title">{item.label}</span>
                    <span className="report-card__desc">{DESCRIPTIONS[item.to]}</span>
                  </span>
                  <Icon name="chevronRight" size={18} className="report-card__chev" />
                </Link>
              </Card>
            ))}
          </div>

          <Card padded={false} className="table-section">
            <div className="table-section__head">
              <CardHeader title="Sync log" subtitle="Each run of hpgbooks whmcs sync, per brand. Row counts only; no customer data is logged." />
            </div>
            <DataTable caption="WHMCS sync log" columns={logColumns} rows={data?.log ?? []} rowKey={(row) => String(row.id)} loading={status.isPending} dense empty="No sync has run yet." />
          </Card>
        </>
      )}
    </div>
  );
}

function num(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

const DESCRIPTIONS: Record<string, string> = {
  "/whmcs/revenue": "Payments in, fees, refunds, and net by month, quarter, or year, per brand and plan.",
  "/whmcs/mrr": "Monthly recurring revenue from active services, ARPU, and the estimated trend.",
  "/whmcs/churn": "Cancellations, reasons, logo and revenue churn, and net adds.",
  "/whmcs/refunds": "Refunds per month, brand, and plan, refund rate, and the largest refunds.",
  "/whmcs/collections": "Unpaid and overdue invoices, aging, failed captures, and a collections list.",
  "/whmcs/reconciliation": "WHMCS PayPal payments matched to the PayPal account in the books, plus gateway totals.",
  "/whmcs/customers": "Search customers across brands. Signed-in only.",
};
