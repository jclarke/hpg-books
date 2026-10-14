import { Link, useLocation, useNavigate } from "react-router-dom";
import { usePersonalDashboard, type BillEvent, type CategoryTotal, type PDashboard, type PTxn } from "../../api/personal";
import { Badge } from "../../components/Badge";
import { Card, CardHeader } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { KpiCard } from "../../components/KpiCard";
import { Money } from "../../components/MoneyCell";
import { PageHeader } from "../../components/PageHeader";
import { SkeletonCard } from "../../components/Skeleton";
import { AreaChart } from "../../components/charts/AreaChart";
import { BarList } from "../../components/charts/BarList";
import { NoPersonalData, PERSONAL_EYEBROW, PersonalLoadError, ProgressBar } from "../../components/personal/PersonalKit";
import { withGlobal } from "../../hooks/useGlobalFilters";
import { monthEnd, monthStart } from "../../lib/dates";
import { formatDate, formatMonth, formatPct } from "../../lib/format";

type LinkFn = (path: string, extra?: Record<string, string>) => string;
type CardProps = { data: PDashboard; link: LinkFn };

const BILL_ROWS = 8;
const RECENT_ROWS = 8;
const BILL_STATUS: Record<BillEvent["status"], { label: string; tone: "neutral" | "warn" | "neg" | "pos" }> = {
  due: { label: "Due", tone: "neutral" },
  late: { label: "Late", tone: "warn" },
  overdue: { label: "Overdue", tone: "neg" },
  paid: { label: "Paid", tone: "pos" },
};

function capitalize(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

/** Month over day, as on the business dashboard's bill list. */
function DateChip({ iso }: { iso: string }) {
  const [month, day] = formatDate(iso, { year: false }).split(" ");
  return (
    <time className="dash-row__date" dateTime={iso} title={formatDate(iso)}>
      <span className="dash-row__month">{month}</span>
      <span className="dash-row__day num">{day}</span>
    </time>
  );
}

/** `Group / Category`, or just the category when there is no distinct parent group. */
function categoryLabel(item: Pick<CategoryTotal, "category" | "group">): string {
  const group = item.group?.trim();
  return group && group !== item.category ? `${group} / ${item.category}` : item.category;
}

/** URL filters that open a dashboard category's transactions for the dashboard month (to date). */
function categoryTxnFilters(item: Pick<CategoryTotal, "category_id" | "category" | "group">, month: string, asOf?: string | null): Record<string, string> {
  const start = monthStart(month);
  const last = monthEnd(month);
  const end = asOf && asOf >= start && asOf < last ? asOf : last;
  const uncategorized = item.category === "Uncategorized" || item.group === "Uncategorized";
  return uncategorized ? { start, end, uncategorized: "1" } : { start, end, category_id: String(item.category_id) };
}

export default function PersonalDashboardPage() {
  const query = usePersonalDashboard();
  const location = useLocation();
  const navigate = useNavigate();
  const data = query.data;
  const link = (path: string, extra: Record<string, string> = {}) => withGlobal(path, location.search, extra);
  return (
    <div className="page page--personal">
      <PageHeader title="Personal dashboard" eyebrow={PERSONAL_EYEBROW} subtitle={data ? `${formatMonth(data.month)} · as of ${formatDate(data.as_of)}` : undefined} />
      {query.isError ? <PersonalLoadError error={query.error} onRetry={() => query.refetch()} /> : null}
      {query.isPending ? (
        <div className="kpi-grid">
          {[0, 1, 2, 3].map((i) => (
            <SkeletonCard key={i} height={70} />
          ))}
        </div>
      ) : null}
      {data && !data.has_accounts ? <NoPersonalData /> : null}
      {data && data.has_accounts ? (
        <>
          <section className="kpi-grid" aria-label="This month">
            <KpiCard
              label="Net worth"
              cents={data.net_worth.net_cents}
              delta={data.net_worth.change_30_cents}
              comparisonLabel="30-day change"
              spark={data.net_worth.points.map((point) => point.net_cents)}
              footer={
                <span className="muted small">
                  90 days <Money cents={data.net_worth.change_90_cents} signed colorPositive />
                  {data.net_worth.stale_count ? <> · <Badge tone="warn">{data.net_worth.stale_count} stale</Badge></> : null}
                </span>
              }
            />
            <KpiCard
              label="Income this month"
              cents={data.month_totals.income_cents}
              footer={
                <span className="muted small">
                  Earned <Money cents={data.month_totals.earned_cents} /> · Owner draws <Money cents={data.month_totals.owner_draws_cents} />
                </span>
              }
            />
            <KpiCard label="Spending this month" cents={data.month_totals.spending_cents} />
            <KpiCard
              label="Savings rate"
              display={formatPct(data.month_totals.savings_rate)}
              footer={<span className="muted small">Without owner draws {formatPct(data.month_totals.savings_rate_without_draws)}</span>}
            />
          </section>
          <div className="dash-grid dash-grid--personal">
            <Card>
              <CardHeader title="Net worth" actions={<Link to={link("/personal/net-worth")}>Details</Link>} />
              <AreaChart points={data.net_worth.points.map((point) => ({ date: point.date, value: point.net_cents }))} label="Net worth over the last three months" valueLabel="Net worth" height={180} />
            </Card>
            <Card>
              <CardHeader title="Top categories" subtitle="This month" actions={<Link to={link("/personal/spending")}>Spending</Link>} />
              {data.top_categories.length ? (
                <BarList
                  label="Top spending categories this month"
                  items={data.top_categories.map((item, index) => ({ key: String(item.category_id), label: categoryLabel(item), cents: item.cents, tone: `cat-${(index % 10) + 1}` }))}
                  showShare
                  onSelect={(selected) => {
                    const item = data.top_categories.find((row) => String(row.category_id) === selected.key);
                    if (item) navigate(link("/personal/transactions", categoryTxnFilters(item, data.month, data.as_of)));
                  }}
                />
              ) : (
                <EmptyState compact title="No spending yet this month" />
              )}
            </Card>
            <UpcomingBillsCard data={data} link={link} />
            <RecentCard data={data} link={link} />
            {data.budget.budgeted_cents || data.goals.length ? (
              <>
                <BudgetCard data={data} link={link} />
                <GoalsCard data={data} link={link} />
              </>
            ) : (
              <Card className="dash-grid__wide">
                <CardHeader title="Budgets & goals" />
                <div className="dash-empty-pair">
                  <p className="dash-empty">
                    No budgets yet · <Link to={link("/personal/budgets")}>Set budgets</Link>
                  </p>
                  <p className="dash-empty">
                    No goals yet · <Link to={link("/personal/goals")}>Add a goal</Link>
                  </p>
                </div>
              </Card>
            )}
          </div>
        </>
      ) : null}
    </div>
  );
}

function UpcomingBillsCard({ data, link }: CardProps) {
  const bills = data.upcoming_bills;
  const review = data.recurring_review_count ?? 0;
  return (
    <Card className="dash-card">
      <CardHeader
        title="Upcoming bills"
        subtitle="Next 14 days"
        actions={
          <>
            {review ? (
              <Link className="dash-review-link" to={`${link("/personal/recurring")}#needs-review`}>
                {review} need review
              </Link>
            ) : null}
            <Link to={link("/personal/bills")}>Bills</Link>
          </>
        }
      />
      {bills.length ? (
        <ul className="dash-list" aria-label="Upcoming bills">
          {bills.slice(0, BILL_ROWS).map((bill) => {
            const status = bill.date === data.as_of && bill.status === "due" ? { label: "Today", tone: "brand" as const } : BILL_STATUS[bill.status];
            return (
              <li key={`${bill.series_key}-${bill.date}`} className="dash-row">
                <DateChip iso={bill.date} />
                <span className="dash-row__main">
                  <span className="dash-row__name" title={bill.merchant}>
                    {bill.merchant}
                  </span>
                  <span className="dash-row__meta">
                    {capitalize(bill.cadence)}
                    {bill.category ? ` · ${bill.category}` : ""}
                  </span>
                </span>
                <span className="dash-row__chip">
                  <Badge tone={status.tone}>{status.label}</Badge>
                </span>
                <span className="dash-row__amount">
                  <Money cents={bill.amount_cents} />
                </span>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="dash-empty">Nothing due in the next two weeks</p>
      )}
      <p className="dash-card__foot muted small">
        {bills.length > BILL_ROWS ? (
          <>
            <Link to={link("/personal/bills")}>+{bills.length - BILL_ROWS} more</Link> ·{" "}
          </>
        ) : null}
        Subscriptions <Money cents={data.subscription_monthly_cents} /> a month · <Link to={link("/personal/recurring")}>Recurring</Link>
      </p>
    </Card>
  );
}

function txnChip(row: PTxn): { label: string; tone: "neutral" | "warn" | "info" } {
  if (row.pending) return { label: "Pending", tone: "info" };
  if (!row.category || row.category === "Uncategorized") return { label: "Uncategorized", tone: "warn" };
  return { label: row.category, tone: "neutral" };
}

function RecentCard({ data, link }: CardProps) {
  return (
    <Card className="dash-card">
      <CardHeader
        title="Recent transactions"
        actions={
          <>
            <Link to={link("/personal/review")}>
              Review <Badge tone={data.review_count ? "warn" : "neutral"}>{data.review_count}</Badge>
            </Link>
            <Link to={link("/personal/transactions")}>All</Link>
          </>
        }
      />
      {data.recent.length ? (
        <ul className="dash-list" aria-label="Recent transactions">
          {data.recent.slice(0, RECENT_ROWS).map((row) => {
            const chip = txnChip(row);
            return (
              <li key={row.id}>
                <Link className="dash-row dash-row--link" to={link("/personal/transactions", { txn: row.id })}>
                  <DateChip iso={row.date} />
                  <span className="dash-row__main">
                    <span className="dash-row__name" title={row.merchant}>
                      {row.merchant}
                    </span>
                    {row.account_label ? <span className="dash-row__meta">{row.account_label}</span> : null}
                  </span>
                  <span className="dash-row__chip">
                    <Badge tone={chip.tone} title={chip.label} className="dash-row__badge">
                      {chip.label}
                    </Badge>
                  </span>
                  <span className="dash-row__amount">
                    <Money cents={row.amount_cents} colorPositive signed />
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="dash-empty">No transactions in the last 60 days</p>
      )}
    </Card>
  );
}

function BudgetCard({ data, link }: CardProps) {
  return (
    <Card>
      <CardHeader title="Budgets" actions={<Link to={link("/personal/budgets")}>Budgets</Link>} />
      {data.budget.budgeted_cents ? (
        <>
          <p>
            <Money cents={data.budget.spent_cents} strong /> of <Money cents={data.budget.budgeted_cents} />
          </p>
          <ProgressBar pct={(data.budget.spent_cents / Math.max(1, data.budget.budgeted_cents)) * 100} label="Budget used this month" status={data.budget.counts.over ? "over" : data.budget.counts.warning ? "warning" : "ok"} />
          <ul className="alert-list">
            {data.budget.alerts.map((alert) => (
              <li key={alert.name}>
                <Badge tone={alert.status === "over" ? "neg" : "warn"}>{alert.status === "over" ? "Over" : "80%+"}</Badge> {alert.name} {formatPct(alert.pct_used)}
              </li>
            ))}
          </ul>
        </>
      ) : (
        <p className="dash-empty">
          No budgets yet · <Link to={link("/personal/budgets")}>Set budgets</Link>
        </p>
      )}
    </Card>
  );
}

function GoalsCard({ data, link }: CardProps) {
  return (
    <Card>
      <CardHeader title="Goals" actions={<Link to={link("/personal/goals")}>Goals</Link>} />
      {data.goals.length ? (
        <ul className="goal-mini">
          {data.goals.map((goal) => (
            <li key={goal.id}>
              <span>{goal.name}</span> <span className="muted small">{formatPct(goal.progress_pct)}</span>
              <ProgressBar pct={goal.progress_pct} status={goal.status} label={`${goal.name} progress`} />
            </li>
          ))}
        </ul>
      ) : (
        <p className="dash-empty">
          No goals yet · <Link to={link("/personal/goals")}>Add a goal</Link>
        </p>
      )}
    </Card>
  );
}
