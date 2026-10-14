import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { usePersonalWrite, useRecurring, useRecurringReview, type RecurringDecision, type RecurringItem } from "../../api/personal";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card, CardHeader } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { KpiCard } from "../../components/KpiCard";
import { Money } from "../../components/MoneyCell";
import { PageHeader } from "../../components/PageHeader";
import { SegmentedControl } from "../../components/SegmentedControl";
import { Select } from "../../components/Select";
import { SkeletonTable } from "../../components/Skeleton";
import { useToast } from "../../components/Toast";
import { CsvLink, PERSONAL_EYEBROW, PersonalLoadError } from "../../components/personal/PersonalKit";
import { formatDate } from "../../lib/format";

type Filter = "all" | "subscription" | "bill" | "income" | "flagged" | "hidden";
const CADENCES = ["weekly", "biweekly", "monthly", "quarterly", "annual"];

export default function RecurringPage() {
  const query = useRecurring();
  const data = query.data;
  const toast = useToast();
  const [filter, setFilter] = useState<Filter>("all");
  const update = usePersonalWrite<{ series_key: string; status?: string; cadence?: string }>("/recurring/update");
  const refresh = usePersonalWrite<Record<string, never>>("/recurring/refresh");
  const act = (item: RecurringItem, body: { status?: string; cadence?: string }, title: string) =>
    update.mutate(
      { series_key: item.series_key, ...body },
      {
        onSuccess: () => toast.toast({ tone: "success", title }),
        onError: (error) => toast.toast({ tone: "error", title: "Couldn't save", description: error instanceof Error ? error.message : "" }),
      },
    );
  const decide = useDecide();
  const items = (data?.items ?? []).filter((item) => {
    if (item.tier === "review") return false;
    const hidden = item.status === "ignored" || item.status === "cancelled";
    if (filter === "hidden") return hidden;
    if (hidden) return false;
    if (filter === "flagged") return Boolean(item.price_change) || item.may_be_cancelled || item.possible_duplicate || item.new;
    return filter === "all" || item.kind === filter;
  });
  return (
    <div className="page page--personal">
      <PageHeader
        title="Recurring & subscriptions"
        eyebrow={PERSONAL_EYEBROW}
        subtitle="Found from repeating charges; confirm, ignore, mark cancelled, or fix the cadence"
        actions={
          <>
            <Button size="sm" icon="refresh" loading={refresh.isPending} onClick={() => refresh.mutate({}, { onSuccess: () => toast.toast({ tone: "success", title: "Detection refreshed" }) })}>
              Detect again
            </Button>
            <CsvLink name="recurring" />
          </>
        }
      />
      {query.isError ? <PersonalLoadError error={query.error} onRetry={() => query.refetch()} /> : null}
      <RecurringReviewSection />
      {data ? (
        <section className="kpi-grid" aria-label="Recurring totals">
          <KpiCard label="Subscriptions / month" cents={data.subscription_monthly_cents} />
          <KpiCard label="Subscriptions / year" cents={data.subscription_annual_cents} />
          <KpiCard label="Bills / month" cents={data.bill_monthly_cents} />
          <KpiCard label="Flags" display={String((data.counts.price_changes ?? 0) + (data.counts.may_be_cancelled ?? 0) + (data.counts.possible_duplicates ?? 0))} footer={<span className="muted small">{data.counts.price_changes} price changes · {data.counts.may_be_cancelled} may be cancelled · {data.counts.possible_duplicates} possible duplicates</span>} />
        </section>
      ) : null}
      <SegmentedControl
        label="Show"
        value={filter}
        onChange={setFilter}
        options={[
          { value: "all", label: "All" },
          { value: "subscription", label: "Subscriptions" },
          { value: "bill", label: "Bills" },
          { value: "income", label: "Income" },
          { value: "flagged", label: "Flagged" },
          { value: "hidden", label: "Ignored / cancelled" },
        ]}
        size="sm"
      />
      <Card padded={false}>
        {query.isPending ? <SkeletonTable rows={6} cols={6} /> : null}
        {data && items.length === 0 ? <EmptyState title="Nothing recurring here" >Recurring charges appear after three monthly (or two yearly) occurrences.</EmptyState> : null}
        {items.length ? (
          <table className="table">
            <caption className="sr-only">Recurring items</caption>
            <thead>
              <tr>
                <th scope="col">Merchant</th>
                <th scope="col">Cadence</th>
                <th scope="col" className="num">Typical</th>
                <th scope="col" className="hide-mobile">Next</th>
                <th scope="col" className="num">Monthly</th>
                <th scope="col" className="num hide-mobile">Yearly</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.series_key}>
                  <th scope="row">
                    <span>{item.merchant}</span> <span className="muted small">{item.category}</span>
                    <span className="flag-list">
                      <Badge tone={item.kind === "income" ? "pos" : item.kind === "subscription" ? "violet" : "neutral"}>{item.kind}</Badge>
                      {item.status === "confirmed" || item.decision === "confirmed" ? <Badge tone="pos">confirmed</Badge> : null}
                      {item.status === "cancelled" ? <Badge tone="neutral">cancelled</Badge> : null}
                      {item.status === "ignored" ? <Badge tone="neutral">ignored</Badge> : null}
                      {item.price_change ? (
                        <Badge tone="warn" title="Latest charge differs from the one before">
                          price {item.price_change.change_cents > 0 ? "up" : "down"} <Money cents={item.price_change.from_cents} /> → <Money cents={item.price_change.to_cents} />
                        </Badge>
                      ) : null}
                      {item.may_be_cancelled ? <Badge tone="warn" title="No charge for more than 1.5 cadences">may be cancelled</Badge> : null}
                      {item.possible_duplicate ? <Badge tone="neg" title="Another active series with this merchant">possible duplicate</Badge> : null}
                      {item.new ? <Badge tone="info">new</Badge> : null}
                    </span>
                  </th>
                  <td>
                    <Select
                      label={`Cadence for ${item.merchant}`}
                      hideLabel
                      size="sm"
                      value={item.cadence}
                      options={CADENCES.map((value) => ({ value, label: value }))}
                      onChange={(cadence) => act(item, { cadence }, "Cadence saved")}
                    />
                  </td>
                  <td className="num"><Money cents={item.typical_cents} /></td>
                  <td className="hide-mobile nowrap">{formatDate(item.next_expected)}</td>
                  <td className="num"><Money cents={item.monthly_cents} /></td>
                  <td className="num hide-mobile"><Money cents={item.annual_cents} /></td>
                  <td>
                    <div className="row-actions">
                      {item.status === "ignored" || item.status === "cancelled" ? (
                        <Button size="sm" variant="ghost" onClick={() => act(item, { status: "active" }, `${item.merchant} restored`)} aria-label={`Restore ${item.merchant}`}>
                          Restore
                        </Button>
                      ) : (
                        <>
                          {item.direction === "out" ? (
                            <Button size="sm" variant="ghost" onClick={() => decide("ignore", item.merchant_key, `${item.merchant} marked not recurring`)} aria-label={`Not recurring: ${item.merchant}`}>
                              Not recurring
                            </Button>
                          ) : null}
                          <Button size="sm" variant="ghost" onClick={() => act(item, { status: "cancelled" }, `${item.merchant} marked cancelled`)} aria-label={`Mark ${item.merchant} cancelled`}>
                            Cancelled
                          </Button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Card>
    </div>
  );
}

type Verb = "confirm" | "ignore" | "undo";

/** Confirm / not recurring / undo, by merchant key, with a toast either way. */
function useDecide() {
  const toast = useToast();
  const write = usePersonalWrite<{ merchant_key: string; verb: Verb }>((body) => `/recurring/${body.verb}`);
  return (verb: Verb, merchantKey: string, title: string) =>
    write.mutate({ merchant_key: merchantKey, verb }, {
      onSuccess: () => toast.toast({ tone: "success", title }),
      onError: (error) => toast.toast({ tone: "error", title: "Couldn't save", description: error instanceof Error ? error.message : "" }),
    });
}

function RecurringReviewSection() {
  const query = useRecurringReview();
  const data = query.data;
  const decide = useDecide();
  const location = useLocation();
  useEffect(() => {
    if (data && location.hash === "#needs-review") document.getElementById("needs-review")?.scrollIntoView?.({ block: "start" });
  }, [data, location.hash]);
  if (!data) return null;
  const decided = data.confirmed.length + data.ignored.length;
  return (
    <>
      <Card id="needs-review" className="review-card">
        <CardHeader
          title={
            <>
              Needs review {data.needs_review ? <Badge tone="warn">{data.needs_review}</Badge> : null}
            </>
          }
          subtitle="These repeat, but may not be bills. Confirmed ones always count as bills; not recurring ones are hidden."
        />
        {data.review.length ? (
          <ul className="review-list" aria-label="Recurring charges to review">
            {data.review.map((item) => (
              <li key={item.series_key} className="review-row">
                <span className="review-row__main">
                  <span className="review-row__name" title={item.merchant}>
                    {item.merchant}
                  </span>
                  <span className="review-row__meta">
                    {item.cadence} · <Money cents={item.typical_cents} /> typical · {item.count} charges · last {formatDate(item.last_date)}
                    {item.category ? ` · ${item.category}` : ""}
                  </span>
                  {item.review_reasons.length ? <span className="review-row__why">{item.review_reasons.join("; ")}</span> : null}
                </span>
                <span className="review-row__actions">
                  <Button size="sm" variant="primary" onClick={() => decide("confirm", item.merchant_key, `${item.merchant} confirmed as a bill`)} aria-label={`Confirm ${item.merchant} as a bill`}>
                    Confirm as bill
                  </Button>
                  <Button size="sm" variant="secondary" onClick={() => decide("ignore", item.merchant_key, `${item.merchant} marked not recurring`)} aria-label={`${item.merchant} is not recurring`}>
                    Not recurring
                  </Button>
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="dash-empty">Nothing to review. New repeating charges show up here.</p>
        )}
      </Card>
      {decided ? (
        <div className="split-2 decision-lists">
          <DecisionList title="Confirmed bills" rows={data.confirmed} onUndo={(row) => decide("undo", row.merchant_key, `${row.merchant} back to automatic`)} />
          <DecisionList title="Not recurring" rows={data.ignored} onUndo={(row) => decide("undo", row.merchant_key, `${row.merchant} back to automatic`)} />
        </div>
      ) : null}
    </>
  );
}

function DecisionList({ title, rows, onUndo }: { title: string; rows: RecurringDecision[]; onUndo: (row: RecurringDecision) => void }) {
  return (
    <Card>
      <CardHeader title={title} subtitle={`${rows.length} merchant${rows.length === 1 ? "" : "s"}`} />
      {rows.length ? (
        <ul className="review-list" aria-label={title}>
          {rows.map((row) => (
            <li key={row.merchant_key} className="review-row">
              <span className="review-row__main">
                <span className="review-row__name" title={row.merchant}>
                  {row.merchant}
                </span>
                <span className="review-row__meta">
                  {row.cadence && row.typical_cents !== null ? (
                    <>
                      {row.cadence} · <Money cents={row.typical_cents} /> ·{" "}
                    </>
                  ) : null}
                  since {formatDate(row.updated_at.slice(0, 10))}
                </span>
              </span>
              <span className="review-row__actions">
                <Button size="sm" variant="ghost" onClick={() => onUndo(row)} aria-label={`Undo ${title.toLowerCase()} for ${row.merchant}`}>
                  Undo
                </Button>
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="dash-empty">None yet</p>
      )}
    </Card>
  );
}
