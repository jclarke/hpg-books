import { useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useBills, useRecurringReview, type BillEvent } from "../../api/personal";
import { Badge } from "../../components/Badge";
import { Card, CardHeader } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { KpiCard } from "../../components/KpiCard";
import { Money } from "../../components/MoneyCell";
import { PageHeader } from "../../components/PageHeader";
import { SegmentedControl } from "../../components/SegmentedControl";
import { SkeletonCard } from "../../components/Skeleton";
import { CsvLink, PERSONAL_EYEBROW, PersonalLoadError } from "../../components/personal/PersonalKit";
import { withGlobal } from "../../hooks/useGlobalFilters";
import { cx } from "../../lib/cx";
import { formatDate, formatMonth } from "../../lib/format";

const TONE: Record<BillEvent["status"], "pos" | "info" | "warn" | "neg"> = { paid: "pos", due: "info", late: "warn", overdue: "neg" };

export default function BillsPage() {
  const [days, setDays] = useState<"30" | "45" | "60">("45");
  const query = useBills(Number(days));
  const data = query.data;
  const [view, setView] = useState<"list" | "calendar">("list");
  const review = useRecurringReview().data?.needs_review ?? 0;
  const location = useLocation();
  return (
    <div className="page page--personal">
      <PageHeader
        title="Upcoming bills"
        eyebrow={PERSONAL_EYEBROW}
        subtitle="Expected from recurring charges; paid means a matching charge already posted this period. An estimate."
        actions={
          <>
            <SegmentedControl label="Window" value={days} onChange={setDays} options={[{ value: "30", label: "30 days" }, { value: "45", label: "45 days" }, { value: "60", label: "60 days" }]} size="sm" />
            <SegmentedControl label="View" value={view} onChange={setView} options={[{ value: "list", label: "List" }, { value: "calendar", label: "Calendar" }]} size="sm" />
            <CsvLink name="bills" params={{ days }} />
          </>
        }
      />
      {query.isError ? <PersonalLoadError error={query.error} onRetry={() => query.refetch()} /> : null}
      {review ? (
        <p className="muted small">
          {review} repeating {review === 1 ? "charge is" : "charges are"} not counted until you decide.{" "}
          <Link to={`${withGlobal("/personal/recurring", location.search)}#needs-review`}>Review them</Link>
        </p>
      ) : null}
      {query.isPending ? <SkeletonCard height={200} /> : null}
      {data ? (
        <>
          <section className="kpi-grid" aria-label="Due">
            <KpiCard label="Due this week" cents={data.due_this_week_cents} />
            <KpiCard label="Due this month" cents={data.due_this_month_cents} />
            <KpiCard label="Expected income" cents={data.income_expected_cents} />
            <KpiCard label="Overdue" display={String(data.overdue_count)} footer={data.overdue_count ? <span className="muted small">Expected 3+ days ago with no charge</span> : undefined} />
          </section>
          {data.events.length === 0 ? <EmptyState title="No recurring bills found yet" /> : null}
          {view === "list" && data.events.length ? (
            <Card padded={false}>
              <table className="table">
                <caption className="sr-only">Bills and expected income</caption>
                <thead>
                  <tr>
                    <th scope="col">Date</th>
                    <th scope="col">Merchant</th>
                    <th scope="col" className="hide-mobile">Kind</th>
                    <th scope="col">Status</th>
                    <th scope="col" className="num">Amount</th>
                  </tr>
                </thead>
                <tbody>
                  {data.events.map((event) => (
                    <tr key={`${event.series_key}-${event.date}-${event.status}`}>
                      <td className="nowrap">{formatDate(event.date)}</td>
                      <td>
                        {event.merchant} <span className="muted small">{event.account_label}</span>
                      </td>
                      <td className="hide-mobile">{event.kind}</td>
                      <td>
                        <Badge tone={TONE[event.status]}>{event.status}</Badge>
                      </td>
                      <td className="num">
                        <Money cents={event.direction === "in" ? event.amount_cents : -event.amount_cents} colorPositive />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          ) : null}
          {view === "calendar" && data.events.length ? <BillCalendar events={data.events} today={data.as_of} /> : null}
        </>
      ) : null}
    </div>
  );
}

function BillCalendar({ events, today }: { events: BillEvent[]; today: string }) {
  const months = useMemo(() => Array.from(new Set(events.map((event) => event.date.slice(0, 7)))).sort(), [events]);
  return (
    <>
      {months.map((month) => {
        const [year, mon] = month.split("-").map(Number);
        const first = new Date(Date.UTC(year, mon - 1, 1)).getUTCDay();
        const days = new Date(Date.UTC(year, mon, 0)).getUTCDate();
        const cells: (number | null)[] = [...Array.from({ length: first }, () => null), ...Array.from({ length: days }, (_, i) => i + 1)];
        return (
          <Card key={month}>
            <CardHeader title={formatMonth(month)} />
            <div className="month-cal" role="grid" aria-label={`Bills in ${formatMonth(month)}`}>
              {["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((day) => (
                <div key={day} className="month-cal__head" role="columnheader">
                  {day}
                </div>
              ))}
              {cells.map((day, index) => {
                const iso = day ? `${month}-${String(day).padStart(2, "0")}` : "";
                const items = day ? events.filter((event) => event.date === iso) : [];
                return (
                  <div key={index} className={cx("month-cal__cell", iso === today && "is-today", !day && "is-blank")} role="gridcell" aria-label={day ? `${formatDate(iso)}, ${items.length} items` : undefined}>
                    {day ? <span className="month-cal__day">{day}</span> : null}
                    {items.map((event) => (
                      <span key={`${event.series_key}-${event.status}`} className={cx("month-cal__item", `month-cal__item--${event.status}`)} title={`${event.merchant} ${event.status}`}>
                        {event.merchant} <Money cents={event.amount_cents} whole />
                      </span>
                    ))}
                  </div>
                );
              })}
            </div>
          </Card>
        );
      })}
    </>
  );
}
