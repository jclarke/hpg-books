import { Link, useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { useDashboard, useWhmcsSummary } from "../api/queries";
import type { Dashboard } from "../api/types";
import { Badge } from "../components/Badge";
import { Card, CardHeader } from "../components/Card";
import { BarList } from "../components/charts/BarList";
import { ComboChart } from "../components/charts/ComboChart";
import { DonutChart } from "../components/charts/DonutChart";
import { EmptyState, ErrorState } from "../components/EmptyState";
import { Icon } from "../components/Icon";
import { KpiCard } from "../components/KpiCard";
import { Money } from "../components/MoneyCell";
import { PageHeader } from "../components/PageHeader";
import { SkeletonCard } from "../components/Skeleton";
import { Sparkline } from "../components/charts/Sparkline";
import { useConfig } from "../hooks/useConfig";
import { useGlobalFilters, withGlobal } from "../hooks/useGlobalFilters";
import { monthEnd, monthStart } from "../lib/dates";
import { shownCents, sourceLabel } from "../lib/balances";
import { formatDate, formatMoney, formatPct, formatRange, formatTimestamp, pluralize } from "../lib/format";
import { businessLabel } from "../lib/labels";
import { joinList } from "../lib/siteConfig";

function compareLabel(data: Dashboard): string {
  return `vs ${formatRange(data.prior_start, data.prior_end)}`;
}

export default function DashboardPage() {
  const filters = useGlobalFilters();
  const navigate = useNavigate();
  const location = useLocation();
  const { features } = useConfig();
  const query = useDashboard({ start: filters.start, end: filters.end, business: filters.business });
  const data = query.data;
  const link = (path: string, extra: Record<string, string> = {}) => withGlobal(path, location.search, extra);

  return (
    <div className="page page--dashboard">
      <PageHeader
        title="Dashboard"
        subtitle={
          <>
            {formatRange(filters.start, filters.end)} · {businessLabel(filters.business)}
            {data ? <span className="muted"> · compared with {formatRange(data.prior_start, data.prior_end)}</span> : null}
          </>
        }
        actions={
          <Link className="btn btn--secondary btn--md" to={link("/reports/pnl")}>
            <Icon name="reports" size={17} /> View P&amp;L
          </Link>
        }
      />

      {query.isError ? <ErrorState error={query.error} onRetry={() => query.refetch()} /> : null}

      {data ? <ReviewCallout count={data.review_count} to={link("/review")} /> : null}

      <section className="kpi-grid" aria-label="Key figures">
        {!data ? (
          [0, 1, 2, 3].map((i) => <KpiCard key={i} label="" loading />)
        ) : (
          <>
            {data.kpis.map((kpi) => (
              <KpiCard
                key={kpi.key}
                label={kpi.label}
                cents={kpi.cents}
                delta={kpi.delta}
                pct={kpi.pct}
                inverse={kpi.key === "total_expenses"}
                comparisonLabel={compareLabel(data)}
                spark={kpi.spark}
                sparkTone={kpi.key === "net_revenue" ? "rev" : kpi.key === "total_expenses" ? "exp" : "net"}
                footer={<span className="muted">Prior: <Money cents={kpi.prior_cents} /></span>}
              />
            ))}
            <KpiCard
              label="Net margin"
              display={formatPct(data.margin.pct)}
              delta={data.margin.delta}
              deltaText={data.margin.delta === null ? "—" : `${Math.abs(data.margin.delta).toFixed(1)} pts`}
              comparisonLabel={compareLabel(data)}
              footer={<span className="muted">Prior: {formatPct(data.margin.prior_pct)}</span>}
            />
          </>
        )}
      </section>

      {data && data.plug !== 0 ? (
        <p className="notice notice--warn" role="note">
          <Icon name="info" size={16} /> <Money cents={data.plug} /> of activity in this range is uncategorized or needs review. It is included in net income.
        </p>
      ) : null}

      <div className="dash-grid">
        <Card className="dash-grid__wide">
          <CardHeader
            title="Revenue, expenses & net income"
            subtitle="By month since January 2026. Click a month to focus on it."
          />
          {!data ? (
            <SkeletonCard height={260} />
          ) : data.points.every((p) => p.revenue === 0 && p.expenses === 0) ? (
            <EmptyState icon="reports" title="No activity yet" compact>
              Import transactions to see monthly trends.
            </EmptyState>
          ) : (
            <ComboChart
              label="Net revenue and expenses as bars, net income as a line, by month"
              points={data.points.map((p) => ({ key: p.month, label: p.label, bars: [p.revenue, p.expenses], line: p.net }))}
              barLabels={["Net revenue", "Expenses"]}
              barTones={["rev", "exp"]}
              lineLabel="Net income"
              lineTone="net"
              highlightKeys={monthsInRange(filters.start, filters.end, data.points.map((p) => p.month))}
              onSelect={(month) => filters.setRange({ start: monthStart(month), end: monthEnd(month) })}
            />
          )}
        </Card>

        <Card>
          <CardHeader title="Expense breakdown" subtitle="Cost of revenue and operating expenses" />
          {!data ? (
            <SkeletonCard />
          ) : data.expenses.length === 0 ? (
            <EmptyState icon="reports" title="No categorized expenses" compact>
              Nothing was spent in this range{filters.business !== "all" ? " for this business" : ""}.
            </EmptyState>
          ) : (
            <BarList
              label="Expenses by category"
              showShare
              tone="exp"
              items={[...data.expenses].sort((a, b) => b.cents - a.cents).map((row, index) => ({ key: row.category, label: row.category, cents: row.cents, tone: `cat-${(index % 10) + 1}` }))}
              onSelect={(item) => navigate(link("/transactions", { category: item.key }))}
            />
          )}
        </Card>

        {data && data.revenue_split.length === 0 ? null : (
          <Card>
            <CardHeader title="Revenue split" subtitle={data ? splitSubtitle(data.revenue_split) : undefined} />
            {!data ? (
              <SkeletonCard />
            ) : (
              <>
                <DonutChart
                  label="Revenue by line of business"
                  centerLabel="Gross revenue"
                  slices={data.revenue_split.map((row) => ({ key: row.business, label: row.label, cents: row.cents, tone: `biz-${row.business}` }))}
                />
                {data.refunds !== 0 ? (
                  <p className="muted small">
                    Refunds: <Money cents={data.refunds} />
                  </p>
                ) : null}
              </>
            )}
          </Card>
        )}

        <Card>
          <CardHeader
            title="Cash by account"
            subtitle="Statement balances when an anchor is set. Cards are the amount owed."
            actions={<Link className="link-quiet" to={link("/accounts")}>All accounts</Link>}
          />
          {!data ? (
            <SkeletonCard />
          ) : (
            <CashByAccount
              rows={data.cash}
              onOpen={(id) => navigate(link(`/accounts/${id}`))}
            />
          )}
        </Card>

        <Card>
          <CardHeader title="Top vendors" subtitle="By spend in this range" actions={<Link className="link-quiet" to={link("/vendors")}>All vendors</Link>} />
          {!data ? (
            <SkeletonCard />
          ) : data.top_vendors.length === 0 ? (
            <EmptyState icon="vendors" title="No vendor spend" compact />
          ) : (
            <BarList
              label="Top vendors by spend"
              tone="exp"
              items={data.top_vendors.map((row) => ({ key: row.merchant, label: row.merchant, cents: row.spend_cents, tone: "exp", meta: `${pluralize(row.count, "charge")} · ${row.category}` }))}
              onSelect={(item) => navigate(link("/transactions", { vendor: item.key, search: "" }))}
            />
          )}
        </Card>

        <Card>
          <CardHeader
            title="Upcoming bills"
            subtitle={<>Next 30 days <Badge tone="warn">Estimate</Badge></>}
            actions={<Link className="link-quiet" to={link("/calendar")}>Calendar</Link>}
          />
          {!data ? (
            <SkeletonCard />
          ) : data.upcoming.length === 0 ? (
            <EmptyState icon="calendar" title="Nothing due soon" compact>
              No monthly charges are expected in the next 30 days.
            </EmptyState>
          ) : (
            <ul className="bill-list">
              {data.upcoming.slice(0, 7).map((bill) => (
                <li key={`${bill.merchant}-${bill.account_id}`} className="bill-list__item">
                  <span className="bill-list__date">
                    <span className="bill-list__month">{formatDate(bill.due_date, { year: false }).split(" ")[0]}</span>
                    <span className="bill-list__day num">{Number(bill.due_date.slice(8, 10))}</span>
                  </span>
                  <span className="bill-list__what">
                    <span className="bill-list__name" title={bill.merchant}>{bill.merchant}</span>
                    <span className="muted small">{bill.account_name} · every ~{bill.gap_days} days</span>
                  </span>
                  <Money cents={bill.cents} />
                </li>
              ))}
              {data.upcoming.length > 7 ? (
                <li className="bill-list__more">
                  <Link to={link("/calendar")}>+{data.upcoming.length - 7} more</Link>
                </li>
              ) : null}
            </ul>
          )}
        </Card>

        {features.whmcs ? <WhmcsWidget to={link("/whmcs")} /> : null}
      </div>
    </div>
  );
}

/** Recurring hosting revenue from WHMCS. Figures only; no customer names. */
function WhmcsWidget({ to }: { to: string }) {
  const { whmcs_brands: brands } = useConfig();
  const query = useWhmcsSummary();
  const data = query.data;
  return (
    <Card className="dash-grid__wide whmcs-widget-card">
      <CardHeader
        title="Hosting billing (WHMCS)"
        subtitle={data?.last_sync ? <>Last sync {formatTimestamp(data.last_sync)}{data.last_status === "error" ? <Badge tone="warn">Last run had an error</Badge> : null}</> : joinList(brands)}
        actions={<Link className="link-quiet" to={to}>Billing overview</Link>}
      />
      {query.isError && query.error instanceof ApiError && query.error.status === 404 ? (
        <EmptyState icon="server" title="Restart the web server to load WHMCS reports" compact>
          The app was updated, but this server process started before the WHMCS reports existed.
        </EmptyState>
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => query.refetch()} compact />
      ) : !data ? (
        <SkeletonCard height={80} />
      ) : !data.ready ? (
        <EmptyState icon="server" title="WHMCS has not been synced yet" compact>
          Run <code>bin/hpgbooks whmcs sync</code> to load billing data.
        </EmptyState>
      ) : (
        <>
          <div className="whmcs-widget">
            <div className="whmcs-widget__figure">
              <span className="muted small">MRR</span>
              <span className="whmcs-widget__value num">{formatMoney(data.mrr_cents)}</span>
            </div>
            <div className="whmcs-widget__figure">
              <span className="muted small">ARR</span>
              <span className="whmcs-widget__value num">{formatMoney(data.arr_cents, { whole: true })}</span>
            </div>
            <div className="whmcs-widget__figure">
              <span className="muted small">Active customers</span>
              <span className="whmcs-widget__value num">{data.customers.toLocaleString("en-US")}</span>
            </div>
            <div className="whmcs-widget__figure">
              <span className="muted small">MRR, last 12 months (estimate)</span>
              {data.spark && data.spark.length > 1 ? <Sparkline values={data.spark} tone="rev" label="Estimated MRR over the last 12 months" /> : <span className="muted">—</span>}
            </div>
          </div>
          <p className="whmcs-widget__brands">
            {data.brands.map((row) => (
              <span key={row.brand}>
                <span className="strong">{row.brand}</span> <Money cents={row.mrr_cents} /> · {pluralize(row.customers, "customer")}
              </span>
            ))}
          </p>
        </>
      )}
    </Card>
  );
}

/** "Shop versus consulting": the first label as is, the rest lower-cased. */
function splitSubtitle(rows: Dashboard["revenue_split"]): string {
  return rows.map((row, index) => (index === 0 ? row.label : row.label.toLowerCase())).join(" versus ");
}

function monthsInRange(start: string, end: string, months: string[]): string[] {
  return months.filter((month) => monthEnd(month) >= start && monthStart(month) <= end);
}

function ReviewCallout({ count, to }: { count: number; to: string }) {
  if (count === 0) {
    return (
      <div className="callout callout--ok" role="status">
        <Icon name="check" size={18} />
        <span>
          <strong>All caught up.</strong> Every transaction is classified.
        </span>
      </div>
    );
  }
  return (
    <div className="callout callout--warn">
      <Icon name="review" size={18} />
      <span>
        <strong>{pluralize(count, "transaction")} need review.</strong> They count toward net income as uncategorized until you classify them.
      </span>
      <Link to={to} className="btn btn--primary btn--sm">
        Review now <Icon name="arrowRight" size={15} />
      </Link>
    </div>
  );
}

function accountMeta(row: Dashboard["cash"][number]): string {
  if (row.anchored && row.as_of_date) {
    return `as of ${formatDate(row.as_of_date)}${row.source ? ` · ${sourceLabel(row.source)}` : ""}`;
  }
  if (row.last_date) return `imported activity · ${formatDate(row.last_date)}`;
  return "imported activity";
}

function CashByAccount({ rows, onOpen }: { rows: Dashboard["cash"]; onOpen: (id: string) => void }) {
  const cash = rows.filter((row) => row.type !== "liability");
  const cards = rows.filter((row) => row.type === "liability");
  const cashTotal = cash.reduce((acc, row) => acc + shownCents(row), 0);
  const owed = cards.reduce((acc, row) => acc + shownCents(row), 0);
  const items = (list: Dashboard["cash"], tone: string) =>
    list.map((row) => ({
      key: row.id,
      label: row.short_name,
      cents: shownCents(row),
      tone,
      meta: accountMeta(row),
    }));
  return (
    <>
      <dl className="cash-summary">
        <div>
          <dt>Cash</dt>
          <dd><Money cents={cashTotal} strong /></dd>
        </div>
        <div>
          <dt>Cards owed</dt>
          <dd><Money cents={owed} strong /></dd>
        </div>
        <div>
          <dt>Net</dt>
          <dd><Money cents={cashTotal - owed} strong /></dd>
        </div>
      </dl>
      {cash.length > 0 ? (
        <BarList label="Cash accounts" tone="rev" items={items(cash, "rev")} onSelect={(item) => onOpen(item.key)} />
      ) : null}
      {cards.length > 0 ? (
        <BarList label="Credit cards owed" tone="cat-10" items={items(cards, "cat-10")} onSelect={(item) => onOpen(item.key)} />
      ) : null}
    </>
  );
}

