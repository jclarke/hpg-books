import { useEffect, useState, type ReactNode } from "react";
import { useDebtOfferWrite, useDebtOffers, useDebtPayoff, type DebtAccount, type DebtOffer, type DebtPayoff } from "../../api/personal";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card, CardHeader } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { Money } from "../../components/MoneyCell";
import { PageHeader } from "../../components/PageHeader";
import { SegmentedControl } from "../../components/SegmentedControl";
import { Select, TextField } from "../../components/Select";
import { SkeletonCard } from "../../components/Skeleton";
import { useToast } from "../../components/Toast";
import { LineChart } from "../../components/charts/LineChart";
import { CUSTOM_KEY, OffersCard, feeText, offerKey, quoteNote, toLoanOffer } from "../../components/personal/DebtOffers";
import { PERSONAL_EYEBROW, PersonalLoadError } from "../../components/personal/PersonalKit";
import { readStorage, writeStorage } from "../../hooks/useLocalStorage";
import { cx } from "../../lib/cx";
import {
  addMonths,
  estimateMinimum,
  grossUp,
  nextMonth,
  round2,
  simulateBaseline,
  simulateOffers,
  sum,
  type BaselineOptions,
  type CardStatus,
  type ConsolidationResult,
  type Debt,
  type LoanOffer,
  type OfferScenario,
  type SimResult,
  type Strategy,
} from "../../lib/debtPayoff";
import { formatDate, formatMoney, formatMonth } from "../../lib/format";

export const STORAGE_KEY = "hpgbooks.debtPayoff.v2";
/** v1 had the same fields minus the scenario selection; it is carried over once and removed. */
export const LEGACY_STORAGE_KEY = "hpbooks.debtPayoff.v1";
/** compat: v2 under the pre-rename prefix; carried over once and removed. */
export const PRE_RENAME_STORAGE_KEY = "hpbooks.debtPayoff.v2";
/** Used for an account whose APR is not on file until you type one. */
const UNKNOWN_APR = { card: 25, loan: 10 } as const;

type Term = "36" | "48" | "60";

export interface Settings {
  strategy: Strategy;
  /** Null means the current sum of minimums. */
  budget: number | null;
  /** Null means the sum of the checked balances. */
  amount: number | null;
  apr: number;
  feePct: number;
  feeDeducted: boolean;
  term: Term;
  extraMonthly: number;
  stepUpOn: boolean;
  /** Null means the prefill from a loan that ends soon. */
  stepUpAmount: number | null;
  stepUpMonth: string | null;
  /** Cards left out of the loan (new cards are in by default). */
  excludedCards: string[];
  /** Loans are context only until checked. */
  includedLoans: string[];
  aprOverrides: Record<string, number>;
  minOverrides: Record<string, number>;
  /** Scenarios to compare ("custom", "offer:<id>"). Null: the live saved offers, or the custom what-if when there are none. */
  selected: string[] | null;
  /** Scenario whose payoff the account tables show; default the first selected. */
  statusFor: string | null;
}

export const DEFAULT_SETTINGS: Settings = {
  strategy: "minimums",
  budget: null,
  amount: null,
  apr: 9.76,
  feePct: 3,
  feeDeducted: true,
  term: "60",
  extraMonthly: 0,
  stepUpOn: false,
  stepUpAmount: null,
  stepUpMonth: null,
  excludedCards: [],
  includedLoans: [],
  aprOverrides: {},
  minOverrides: {},
  selected: null,
  statusFor: null,
};

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** Saved settings, moving v1 settings to the v2 key the first time. */
export function loadSettings(): Partial<Settings> {
  const current = readStorage<unknown>(STORAGE_KEY, null);
  if (isObject(current)) return current as Partial<Settings>;
  const renamed = readStorage<unknown>(PRE_RENAME_STORAGE_KEY, null);
  if (isObject(renamed)) {
    writeStorage(STORAGE_KEY, renamed);
    try {
      window.localStorage.removeItem(PRE_RENAME_STORAGE_KEY);
    } catch {
      /* storage unavailable */
    }
    return renamed as Partial<Settings>;
  }
  const legacy = readStorage<unknown>(LEGACY_STORAGE_KEY, null);
  if (!isObject(legacy)) return {};
  const migrated: Partial<Settings> = { ...(legacy as Partial<Settings>), selected: null, statusFor: null };
  writeStorage(STORAGE_KEY, migrated);
  try {
    window.localStorage.removeItem(LEGACY_STORAGE_KEY);
  } catch {
    /* storage unavailable */
  }
  return migrated;
}

interface Row {
  account: DebtAccount;
  debt: Debt;
  aprUnknown: boolean;
  aprEdited: boolean;
  minEstimated: boolean;
  included: boolean;
}

const dollars = (cents: number) => round2(cents / 100);

function nameOf(account: DebtAccount): string {
  return account.last4 && !account.label.includes(account.last4) ? `${account.label} ${account.last4}` : account.label;
}

function buildRows(accounts: DebtAccount[], settings: Settings): Row[] {
  return accounts.map((account) => {
    const balance = dollars(account.balance_cents);
    const override = settings.aprOverrides[account.id];
    const apr = override ?? account.apr ?? UNKNOWN_APR[account.kind];
    const known = account.minimum_payment_cents ? dollars(account.minimum_payment_cents) : null;
    const minimum = settings.minOverrides[account.id] ?? known ?? estimateMinimum(balance, apr);
    const included = account.kind === "card" ? !settings.excludedCards.includes(account.id) : settings.includedLoans.includes(account.id);
    return {
      account,
      debt: { id: account.id, label: account.label, balance, apr, minimum },
      aprUnknown: account.apr === null,
      aprEdited: override !== undefined,
      minEstimated: known === null,
      included,
    };
  });
}

/** A loan that ends within a year frees its payment: the default "add $X starting" example. */
function stepUpPrefill(data: DebtPayoff): { amount: number; month: string; label: string } | null {
  const soon = data.loans
    .filter((loan) => loan.maturity_date && loan.maturity_date > data.as_of && loan.minimum_payment_cents)
    .sort((a, b) => (a.maturity_date as string).localeCompare(b.maturity_date as string))[0];
  if (!soon || !soon.maturity_date) return null;
  const month = addMonths(soon.maturity_date.slice(0, 7), 1);
  if (month > addMonths(data.as_of.slice(0, 7), 13)) return null;
  return { amount: Math.round(dollars(soon.minimum_payment_cents as number)), month, label: `when ${soon.label} ends (${formatDate(soon.maturity_date)})` };
}

export default function DebtPayoffPage() {
  const query = useDebtPayoff();
  const data = query.data;
  return (
    <div className="page page--personal debt-page">
      <PageHeader
        title="Debt payoff"
        eyebrow={PERSONAL_EYEBROW}
        subtitle="What if one personal loan paid off the cards? Compare it with keeping on as you are. Nothing here is saved to your books."
      />
      {query.isError ? <PersonalLoadError error={query.error} onRetry={() => query.refetch()} /> : null}
      {query.isPending ? <SkeletonCard height={320} /> : null}
      {data && data.cards.length === 0 && data.loans.length === 0 ? <EmptyState icon="wallet" title="No card or loan balances owed" /> : null}
      {data && (data.cards.length > 0 || data.loans.length > 0) ? <DebtPayoffView data={data} /> : null}
    </div>
  );
}

function DebtPayoffView({ data }: { data: DebtPayoff }) {
  const [settings, setSettings] = useState<Settings>(() => ({ ...DEFAULT_SETTINGS, ...loadSettings() }));
  const update = (patch: Partial<Settings>) => {
    setSettings((current) => {
      const next = { ...current, ...patch };
      writeStorage(STORAGE_KEY, next);
      return next;
    });
  };
  const reset = () => {
    setSettings(DEFAULT_SETTINGS);
    writeStorage(STORAGE_KEY, {});
  };
  const offersQuery = useDebtOffers();
  const savedOffers = offersQuery.data?.rows ?? [];
  const offersSettled = !offersQuery.isPending;
  const { save } = useDebtOfferWrite();
  const toast = useToast();

  const firstMonth = nextMonth(data.as_of);
  const cards = buildRows(data.cards, settings);
  const loans = buildRows(data.loans, settings);
  const prefill = stepUpPrefill(data);

  const inScope = [...cards, ...loans.filter((row) => row.included)];
  const minimumsTotal = round2(sum(inScope.map((row) => Math.min(row.debt.minimum, row.debt.balance))));
  const included = inScope.filter((row) => row.included);
  const excluded = cards.filter((row) => !row.included);
  const includedTotal = round2(sum(included.map((row) => row.debt.balance)));
  const budget = settings.budget ?? minimumsTotal;
  const amount = settings.amount ?? includedTotal;
  const term = Number(settings.term);
  const stepUpAmount = settings.stepUpAmount ?? prefill?.amount ?? 0;
  const stepUpMonth = settings.stepUpMonth ?? prefill?.month ?? addMonths(firstMonth, 3);
  const coverAmount = grossUp(includedTotal, settings.feePct);

  const custom: LoanOffer = {
    key: CUSTOM_KEY,
    label: "Custom / what-if",
    amount,
    apr: settings.apr,
    feePct: settings.feePct,
    feeDeducted: settings.feeDeducted,
    termMonths: term,
    quotedPayment: null,
  };
  const loanOffers = [custom, ...savedOffers.map((offer) => toLoanOffer(offer, savedOffers))];
  const valid = new Set(loanOffers.map((offer) => offer.key));
  // Never chosen: the live offers when there are saved ones, else the custom what-if.
  const live = savedOffers.filter((offer) => !offer.expired);
  const defaultSelected = savedOffers.length ? (live.length ? live : savedOffers).map((offer) => offerKey(offer.id)) : [CUSTOM_KEY];
  const selected = offersSettled ? (settings.selected ?? defaultSelected).filter((key) => valid.has(key)) : [];
  const chosen = loanOffers.filter((offer) => selected.includes(offer.key));
  const toggleScenario = (key: string, on: boolean) => update({ selected: on ? [...selected.filter((item) => item !== key), key] : selected.filter((item) => item !== key) });

  // A few hundred months over a couple dozen balances: cheap enough to run on every render.
  const common: BaselineOptions = {
    strategy: settings.strategy,
    budget,
    firstMonth,
    extraMonthly: settings.extraMonthly,
    stepUp: settings.stepUpOn ? { amount: stepUpAmount, startMonth: stepUpMonth } : null,
  };
  const baselineDebts = inScope.map((row) => row.debt);
  const baseline = simulateBaseline(baselineDebts, common);
  // Minimums-only that never ends compares with nothing; also show the same money as a fixed budget.
  const budgetBaseline = baseline.neverPaysOff && settings.strategy === "minimums" ? simulateBaseline(baselineDebts, { ...common, strategy: "budget", budget: minimumsTotal }) : null;
  const scenarios = simulateOffers(
    included.map((row) => row.debt),
    excluded.map((row) => row.debt),
    chosen,
    { ...common, baselineInterest: baseline.neverPaysOff ? undefined : baseline.totalInterest },
  );
  const tones = new Map(chosen.map((offer, index) => [offer.key, SCENARIO_TONES[index % SCENARIO_TONES.length]]));
  const statusScenario = scenarios.find((item) => item.offer.key === settings.statusFor) ?? scenarios[0] ?? null;
  const status = new Map((statusScenario?.result.allocations ?? []).map((row) => [row.id, row]));
  const labels = new Map([...cards, ...loans].map((row) => [row.account.id, nameOf(row.account)]));
  const customRun = scenarios.find((item) => item.offer.key === CUSTOM_KEY)?.result ?? simulateOffers(included.map((row) => row.debt), [], [custom], common)[0].result;

  const saveCustom = () =>
    save.mutate(
      {
        body: {
          lender: `What-if ${settings.apr}% ${term} mo`,
          amount_cents: Math.round(amount * 100),
          apr: settings.apr,
          fee_pct: settings.feePct,
          fee_from_proceeds: settings.feeDeducted,
          term_months: term,
          monthly_payment_cents: null,
          notes: "Saved from the custom what-if",
          expires_on: null,
        },
      },
      {
        onSuccess: (result) => {
          const offer = result.offer as DebtOffer;
          toast.toast({ tone: "success", title: "Saved as an offer", description: offer.lender });
          update({ selected: [...selected, offerKey(offer.id)] });
        },
        onError: (error) => toast.toast({ tone: "error", title: "Couldn't save the offer", description: error instanceof Error ? error.message : "" }),
      },
    );

  const toggleCard = (id: string, on: boolean) =>
    update({ excludedCards: on ? settings.excludedCards.filter((item) => item !== id) : [...settings.excludedCards, id] });
  const toggleLoan = (id: string, on: boolean) =>
    update({ includedLoans: on ? [...settings.includedLoans, id] : settings.includedLoans.filter((item) => item !== id) });
  const setApr = (id: string, value: number | null) => {
    const next = { ...settings.aprOverrides };
    if (value === null) delete next[id];
    else next[id] = value;
    update({ aprOverrides: next });
  };
  const setMin = (id: string, value: number) => update({ minOverrides: { ...settings.minOverrides, [id]: value } });
  const short = baseline.budgetBelowMinimums || scenarios.some((item) => item.result.budgetBelowMinimums);
  const statusLabel = statusScenario ? statusScenario.offer.label : null;

  return (
    <>
      <Card className="debt-inputs-card">
        <CardHeader
          title="Scenario"
          subtitle={`Balances as of ${formatDate(data.as_of)}. These settings stay in this browser only.`}
          actions={
            <Button size="sm" variant="secondary" icon="undo" onClick={reset}>
              Reset to defaults
            </Button>
          }
        />
        <div className="debt-inputs">
          <fieldset className="debt-group">
            <legend className="debt-group__title">Keep paying the cards</legend>
            <SegmentedControl
              label="Baseline strategy"
              value={settings.strategy}
              onChange={(strategy) => update({ strategy })}
              options={[
                { value: "minimums", label: "Minimums only" },
                { value: "budget", label: "Fixed monthly budget" },
              ]}
            />
            {settings.strategy === "budget" ? (
              <NumberField
                label="Monthly budget ($)"
                value={round2(budget)}
                min={0}
                step={50}
                onChange={(value) => update({ budget: value })}
                hint={`All minimums first (${formatMoney(minimumsTotal * 100)}), the rest to the highest APR. Used for every scenario.`}
                error={budget + 0.005 < minimumsTotal ? "Below the sum of minimums: minimums are used instead." : null}
              />
            ) : (
              <p className="debt-note">Each card pays today's minimum ({formatMoney(minimumsTotal * 100)} in all) until it is gone. Minimums are held constant.</p>
            )}
          </fieldset>

          <fieldset className="debt-group">
            <legend className="debt-group__title">Custom / what-if loan</legend>
            <NumberField label="Loan amount ($)" value={amount} min={0} step={100} onChange={(value) => update({ amount: value })} />
            <div className="debt-actions">
              <Button size="sm" variant="secondary" onClick={() => update({ amount: null })} disabled={settings.amount === null}>
                Match checked ({formatMoney(includedTotal * 100, { whole: true })})
              </Button>
              {settings.feeDeducted && settings.feePct > 0 ? (
                <Button size="sm" variant="secondary" onClick={() => update({ amount: coverAmount })}>
                  Cover fee: borrow {formatMoney(coverAmount * 100, { whole: true })}
                </Button>
              ) : null}
            </div>
            <NumberField label="APR (%)" value={settings.apr} min={0} max={36} step={0.01} onChange={(value) => update({ apr: value })} />
            <div className="field">
              <span className="field__label" aria-hidden="true">
                Term
              </span>
              <SegmentedControl
                label="Term"
                value={settings.term}
                onChange={(value) => update({ term: value })}
                options={[
                  { value: "36", label: "36 mo" },
                  { value: "48", label: "48 mo" },
                  { value: "60", label: "60 mo" },
                ]}
              />
            </div>
            <RangeField label="Origination fee" value={settings.feePct} min={0} max={10} step={0.25} display={`${settings.feePct}%`} onChange={(value) => update({ feePct: value })} />
            <label className="debt-check">
              <input type="checkbox" role="switch" checked={settings.feeDeducted} onChange={(event) => update({ feeDeducted: event.target.checked })} />
              <span>
                Fee taken from the proceeds
                <span className="debt-check__hint">
                  {settings.feeDeducted
                    ? `${formatMoney(customRun.proceeds * 100)} reaches the cards; the ${formatMoney(customRun.fee * 100)} fee stays owed.`
                    : `Full amount reaches the cards; the ${formatMoney(customRun.fee * 100)} fee is paid separately.`}
                </span>
              </span>
            </label>
          </fieldset>

          <fieldset className="debt-group">
            <legend className="debt-group__title">Extra payments (every scenario)</legend>
            <RangeField
              label="Extra each month"
              value={settings.extraMonthly}
              min={0}
              max={5000}
              step={50}
              display={formatMoney(settings.extraMonthly * 100, { whole: true })}
              onChange={(value) => update({ extraMonthly: value })}
            />
            <label className="debt-check">
              <input type="checkbox" role="switch" checked={settings.stepUpOn} onChange={(event) => update({ stepUpOn: event.target.checked })} />
              <span>
                Add {formatMoney(stepUpAmount * 100, { whole: true })} a month starting {formatMonth(stepUpMonth)}
                <span className="debt-check__hint">{prefill && settings.stepUpAmount === null && settings.stepUpMonth === null ? prefill.label : "From that month on."}</span>
              </span>
            </label>
            {settings.stepUpOn ? (
              <div className="debt-pair">
                <NumberField label="Extra ($/mo)" value={stepUpAmount} min={0} step={10} onChange={(value) => update({ stepUpAmount: value })} />
                <TextField
                  label="Starting"
                  type="month"
                  value={stepUpMonth}
                  min={firstMonth}
                  onChange={(event) => /^\d{4}-\d{2}$/.test(event.target.value) && update({ stepUpMonth: event.target.value })}
                />
              </div>
            ) : null}
            <p className="debt-note">Extras go to the loan in each consolidation (then to any cards left), and to the highest-APR card when you keep paying the cards.</p>
          </fieldset>
        </div>
      </Card>

      <OffersCard
        offers={savedOffers}
        loading={offersQuery.isPending}
        error={offersQuery.isError ? offersQuery.error : null}
        custom={custom}
        selected={selected}
        onSelect={toggleScenario}
        onSaveCustom={saveCustom}
        savingCustom={save.isPending}
        onSaved={(offer, created) => {
          if (created) update({ selected: [...selected, offerKey(offer.id)] });
        }}
      />

      {offersSettled ? (
        <>
          <ComparisonCard baseline={baseline} budgetBaseline={budgetBaseline} minimumsTotal={minimumsTotal} scenarios={scenarios} tones={tones} labels={labels} savedOffers={savedOffers} strategy={settings.strategy} />
          {short ? (
            <p className="notice notice--warn debt-warning" role="note">
              The monthly budget is below the minimums{!baseline.budgetBelowMinimums ? " once a loan payment is added" : ""}, so minimums are used instead.
            </p>
          ) : null}
          <Card>
            <CardHeader title="Total debt remaining" subtitle="Cards and included loans, month by month" />
            <PayoffChart baseline={baseline} scenarios={scenarios} tones={tones} />
          </Card>
        </>
      ) : (
        <SkeletonCard height={260} />
      )}

      <Card padded={false}>
        <div className="debt-table-head">
          <CardHeader
            title="Cards"
            subtitle={`Check the cards a loan pays off. ${formatMoney(data.totals.card_balance_cents)} owed on ${data.cards.length} cards; proceeds go to the highest APR first.`}
            actions={
              scenarios.length > 1 ? (
                <Select
                  label="Show payoff for"
                  size="sm"
                  value={statusScenario?.offer.key ?? ""}
                  options={scenarios.map((item) => ({ value: item.offer.key, label: item.offer.label }))}
                  onChange={(value) => update({ statusFor: value })}
                />
              ) : null
            }
          />
        </div>
        <AccountTable rows={cards} kind="card" status={status} statusLabel={statusLabel} onToggle={toggleCard} onApr={setApr} onMin={setMin} />
      </Card>

      {loans.length ? (
        <Card padded={false}>
          <div className="debt-table-head">
            <CardHeader
              title="Loans"
              subtitle={`Context only unless checked: an unchecked loan is in no scenario's totals. A checked loan is paid off by the consolidation loan too.${
                data.mortgages_excluded ? " Mortgages are not shown." : ""
              }`}
            />
          </div>
          <AccountTable rows={loans} kind="loan" status={status} statusLabel={statusLabel} onToggle={toggleLoan} onApr={setApr} onMin={setMin} />
        </Card>
      ) : null}

      <p className="debt-disclaimer">
        Estimates only. Interest is charged monthly at APR ÷ 12 on today's balances with no new spending; real card interest is daily and minimums fall as balances do. The
        real rate, fee, and term are set by the lender after approval. Extra and step-up payments apply to every scenario.
      </p>
    </>
  );
}

/** Distinct from the baseline's orange; every other line is dashed so color is not the only cue. */
const SCENARIO_TONES = ["net", "cat-1", "cat-4", "cat-5", "cat-7", "cat-6", "cat-9", "cat-10"];

function stuckNames(sim: SimResult, labels: Map<string, string>): string[] {
  return Object.entries(sim.paidOffAt)
    .filter(([, month]) => month === null)
    .map(([id]) => labels.get(id) ?? id);
}

function money(dollarsValue: number): string {
  return formatMoney(Math.round(dollarsValue * 100), { whole: true });
}

function ComparisonCard({
  baseline,
  budgetBaseline,
  minimumsTotal,
  scenarios,
  tones,
  labels,
  savedOffers,
  strategy,
}: {
  baseline: SimResult;
  budgetBaseline: SimResult | null;
  minimumsTotal: number;
  scenarios: OfferScenario[];
  tones: Map<string, string>;
  labels: Map<string, string>;
  savedOffers: DebtOffer[];
  strategy: Strategy;
}) {
  const offerById = new Map(savedOffers.map((offer) => [offerKey(offer.id), offer]));
  const costs = [
    { key: "baseline", sim: baseline as SimResult },
    ...scenarios.map((item) => ({ key: item.offer.key, sim: item.result as SimResult })),
  ].filter((row) => !row.sim.neverPaysOff);
  const cheapest = costs.length > 1 ? costs.reduce((best, row) => (row.sim.totalInterest + row.sim.totalFees < best.sim.totalInterest + best.sim.totalFees ? row : best)).key : null;
  const stuck = baseline.neverPaysOff ? stuckNames(baseline, labels) : [];
  return (
    <Card padded={false} className="debt-compare-card">
      <div className="debt-table-head">
        <CardHeader
          title="Compare"
          subtitle={scenarios.length ? "Every scenario uses the checked cards and loans, the same baseline strategy, and the same extras." : "Check an offer or the custom what-if above to compare it with keeping on."}
        />
      </div>
      <div className="debt-table-scroll" tabIndex={0} role="region" aria-label="Comparison table">
        <table className="table debt-table debt-compare debt-stack">
          <caption className="sr-only">Scenario comparison</caption>
          <thead>
            <tr>
              <th scope="col">Scenario</th>
              <th scope="col" className="num">
                Month 1 payment
              </th>
              <th scope="col">Debt-free</th>
              <th scope="col" className="num">
                Interest
              </th>
              <th scope="col" className="num">
                Fees
              </th>
              <th scope="col" className="num">
                Total paid
              </th>
              <th scope="col" className="num">
                Saved vs baseline
              </th>
              <th scope="col">Cards paid in full</th>
            </tr>
          </thead>
          <tbody>
            <tr className="debt-compare__baseline">
              <th scope="row" data-label="Scenario">
                <ScenarioName label="Keep paying cards" tone="exp" best={cheapest === "baseline"} />
                <span className="debt-sub debt-sub--block">{strategy === "budget" && !baseline.budgetBelowMinimums ? "Fixed budget, highest APR first" : "Minimum payments"}</span>
                {stuck.length ? <span className="debt-stuck debt-sub--block">Payment never covers the interest on {stuck.join(", ")}.</span> : null}
              </th>
              <td className="num" data-label="Month 1 payment">
                {formatMoney(baseline.firstMonthPayment * 100)}
              </td>
              <td data-label="Debt-free">{baseline.neverPaysOff ? <span className="text-neg">Never at this rate</span> : formatMonth(baseline.payoffMonth as string)}</td>
              <td className="num" data-label="Interest">
                {baseline.neverPaysOff ? "—" : money(baseline.totalInterest)}
              </td>
              <td className="num" data-label="Fees">
                —
              </td>
              <td className="num" data-label="Total paid">
                {baseline.neverPaysOff ? "—" : money(baseline.totalPaid)}
              </td>
              <td className="num" data-label="Saved vs baseline">
                —
              </td>
              <td data-label="Cards paid in full">—</td>
            </tr>
            {scenarios.map((item) => {
              const { offer, result } = item;
              const saved = result.interestSaved;
              const sooner = baseline.months !== null && result.months !== null ? baseline.months - result.months : null;
              const saved2 = budgetBaseline && !budgetBaseline.neverPaysOff && !result.neverPaysOff ? round2(budgetBaseline.totalInterest - (result.totalInterest + result.totalFees)) : null;
              const saved2Text = saved2 === null ? null : `${money(Math.abs(saved2))} ${saved2 >= 0 ? "saved" : "more"} vs paying ${money(minimumsTotal)}/mo`;
              const saved1 =
                result.neverPaysOff ? (
                  <span className="text-neg">Never pays off</span>
                ) : baseline.neverPaysOff ? (
                  <span className="debt-pos">Pays off</span>
                ) : saved === null ? (
                  "—"
                ) : (
                  <span className={saved >= 0 ? "debt-pos" : "text-neg"}>
                    {money(Math.abs(saved))} {saved >= 0 ? "saved" : "more"}
                  </span>
                );
              const saved1Label = !result.neverPaysOff && !baseline.neverPaysOff && saved !== null && saved < 0 ? "Costs more" : null;
              const meta = offerById.get(offer.key);
              const note = quoteNote(item.quote);
              const stuckHere = result.neverPaysOff ? stuckNames(result, labels) : [];
              return (
                <tr key={offer.key}>
                  <th scope="row" data-label="Scenario">
                    <ScenarioName label={offer.label} tone={tones.get(offer.key) ?? "net"} dashed={dashed(offer.key, tones)} best={cheapest === offer.key} />
                    <span className="debt-sub debt-sub--block">
                      {money(offer.amount)} at {offer.apr}% · {offer.termMonths} mo · {feeText(offer.feePct, offer.feeDeducted)} · loan {formatMoney(result.loanPayment * 100)}/mo
                    </span>
                    {meta?.expires_on ? (
                      <span className="debt-sub debt-sub--block">
                        {meta.expired ? <Badge tone="warn">expired</Badge> : null} {meta.expired ? "Expired" : "Expires"} {formatDate(meta.expires_on)}
                      </span>
                    ) : null}
                    {result.surplus > 0.005 ? <span className="debt-sub debt-sub--block">{formatMoney(result.surplus * 100)} more than the checked balances comes back to you as cash (still repaid with interest).</span> : null}
                    {note ? <span className="debt-sub debt-sub--block debt-quote-note">{note}</span> : null}
                    {stuckHere.length ? <span className="debt-stuck debt-sub--block">Payment never covers the interest on {stuckHere.join(", ")}.</span> : null}
                  </th>
                  <td className="num" data-label="Month 1 payment">
                    {formatMoney(result.firstMonthPayment * 100)}
                  </td>
                  <td data-label="Debt-free">
                    {result.neverPaysOff ? <span className="text-neg">Never at this rate</span> : formatMonth(result.payoffMonth as string)}
                    {sooner ? <span className="debt-sub debt-sub--block">{Math.abs(sooner)} months {sooner > 0 ? "sooner" : "later"}</span> : null}
                  </td>
                  <td className="num" data-label="Interest">
                    {result.neverPaysOff ? "—" : money(result.totalInterest)}
                  </td>
                  <td className="num" data-label="Fees">
                    {money(result.totalFees)}
                  </td>
                  <td className="num" data-label="Total paid">
                    {result.neverPaysOff ? "—" : money(result.totalPaid)}
                  </td>
                  <td className="num" data-label="Saved vs baseline">
                    {saved1Label ? <span className="sr-only">{saved1Label}: </span> : null}
                    {saved1}
                    {saved2Text ? <span className="debt-sub debt-sub--block">{saved2Text}</span> : null}
                  </td>
                  <td data-label="Cards paid in full">
                    {item.cardsPaid} of {item.cardsTotal}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function dashed(key: string, tones: Map<string, string>): boolean {
  return [...tones.keys()].indexOf(key) % 2 === 1;
}

function ScenarioName({ label, tone, dashed: isDashed, best }: { label: string; tone: string; dashed?: boolean; best?: boolean }) {
  return (
    <span className="debt-scenario-name">
      <span className={cx("legend__swatch", isDashed ? "legend__swatch--dash" : "legend__swatch--line", `tone-${tone}`)} aria-hidden="true" />
      <span className="debt-name">{label}</span>
      {best ? <Badge tone="pos">Lowest cost</Badge> : null}
    </span>
  );
}

function PayoffChart({ baseline, scenarios, tones }: { baseline: SimResult; scenarios: OfferScenario[]; tones: Map<string, string> }) {
  const sims: SimResult[] = [baseline, ...scenarios.map((item) => item.result)];
  const finite = sims.map((sim) => sim.months).filter((value): value is number => value !== null);
  const horizon = finite.length ? Math.min(600, Math.max(12, ...finite)) : 120;
  const start = baseline.series[0]?.month ?? scenarios[0]?.result.series[0].month;
  if (!start) return null;
  const months = Array.from({ length: horizon + 1 }, (_, index) => addMonths(start, index));
  const values = (sim: SimResult) => months.map((_, index) => (index < sim.series.length ? Math.round(sim.series[index].total * 100) : null));
  return (
    <LineChart
      months={months}
      label={`Total debt remaining by month: keep paying cards${scenarios.map((item) => `, ${item.offer.label}`).join("")}`}
      series={[
        { label: "Keep paying cards", tone: "exp", values: values(baseline) },
        ...scenarios.map((item) => ({ label: item.offer.label, tone: tones.get(item.offer.key) ?? "net", dashed: dashed(item.offer.key, tones), values: values(item.result) })),
      ]}
    />
  );
}

const STATUS_TEXT: Record<CardStatus, { text: string; tone: "pos" | "warn" | "neutral" | "info" }> = {
  paid: { text: "Paid off", tone: "pos" },
  partial: { text: "Partly paid", tone: "warn" },
  unpaid: { text: "Not reached", tone: "warn" },
  excluded: { text: "Not included", tone: "neutral" },
};

function AccountTable({
  rows,
  kind,
  status,
  statusLabel,
  onToggle,
  onApr,
  onMin,
}: {
  rows: Row[];
  kind: "card" | "loan";
  status: Map<string, ConsolidationResult["allocations"][number]>;
  statusLabel: string | null;
  onToggle: (id: string, on: boolean) => void;
  onApr: (id: string, value: number | null) => void;
  onMin: (id: string, value: number) => void;
}) {
  return (
    <div className="debt-table-scroll" tabIndex={0} role="region" aria-label={kind === "card" ? "Cards table" : "Loans table"}>
      <table className="table debt-table">
        <caption className="sr-only">{kind === "card" ? "Credit cards" : "Installment loans"}</caption>
        <thead>
          <tr>
            <th scope="col" className="debt-table__check">
              Include
            </th>
            <th scope="col">{kind === "card" ? "Card" : "Loan"}</th>
            <th scope="col" className="num">
              Balance
            </th>
            <th scope="col">APR</th>
            <th scope="col" className="num">
              {kind === "card" ? "Minimum" : "Payment"}
            </th>
            <th scope="col">{kind === "card" ? "Due" : "Matures"}</th>
            <th scope="col">{statusLabel ? `After ${statusLabel}` : "After consolidation"}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const allocation = status.get(row.account.id);
            const state: CardStatus | null = row.included ? allocation?.status ?? null : kind === "card" ? "excluded" : null;
            return (
              <tr key={row.account.id}>
                <td className="debt-table__check">
                  <input type="checkbox" checked={row.included} onChange={(event) => onToggle(row.account.id, event.target.checked)} aria-label={`Include ${row.account.label}`} />
                </td>
                <td>
                  <span className="debt-name">{row.account.label}</span> {row.account.last4 && !row.account.label.includes(row.account.last4) ? <span className="debt-sub">···· {row.account.last4}</span> : null}
                  {row.account.apr_text && row.account.apr !== null && row.account.apr_text.replace(/%$/, "") !== String(row.account.apr) ? <span className="debt-sub debt-sub--block">{row.account.apr_text}</span> : null}
                </td>
                <td className="num">
                  <Money cents={row.account.balance_cents} />
                </td>
                <td>
                  <div className="debt-apr">
                    <NumberField label={`APR for ${row.account.label}`} hideLabel inputSize="sm" value={row.debt.apr} min={0} max={100} step={0.01} onChange={(value) => onApr(row.account.id, value)} />
                    {row.aprUnknown && !row.aprEdited ? (
                      <Badge tone="warn" title={`Not on file; ${UNKNOWN_APR[kind]}% assumed until you enter one`}>
                        APR unknown
                      </Badge>
                    ) : null}
                    {row.aprEdited ? (
                      <button type="button" className="link-button" onClick={() => onApr(row.account.id, null)}>
                        {row.aprUnknown ? "clear" : `reset to ${row.account.apr}%`}
                      </button>
                    ) : null}
                  </div>
                </td>
                <td className="num">
                  {row.minEstimated ? (
                    <div className="debt-apr debt-apr--end">
                      <NumberField label={`Minimum for ${row.account.label}`} hideLabel inputSize="sm" value={row.debt.minimum} min={0} step={5} onChange={(value) => onMin(row.account.id, value)} />
                      <Badge tone="warn" title="No minimum on file; estimated as 1% of the balance plus a month's interest">
                        est.
                      </Badge>
                    </div>
                  ) : (
                    <Money cents={Math.round(row.debt.minimum * 100)} />
                  )}
                  {row.debt.minimum <= (row.debt.balance * row.debt.apr) / 1200 ? (
                    <span className="debt-sub debt-sub--block debt-sub--neg">below monthly interest</span>
                  ) : null}
                </td>
                <td className="nowrap">{formatDate(kind === "card" ? row.account.due_date : row.account.maturity_date ?? null) || "—"}</td>
                <td>
                  {state ? (
                    <Badge tone={STATUS_TEXT[state].tone}>
                      {STATUS_TEXT[state].text}
                      {state === "partial" && allocation ? ` · ${formatMoney(allocation.remaining * 100, { whole: true })} left` : ""}
                    </Badge>
                  ) : (
                    <span className="debt-sub">{row.included ? "—" : "Context only"}</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function clamp(value: number, min?: number, max?: number): number {
  let out = value;
  if (min !== undefined) out = Math.max(min, out);
  if (max !== undefined) out = Math.min(max, out);
  return out;
}

function NumberField({
  label,
  value,
  onChange,
  min,
  max,
  step,
  hint,
  error,
  hideLabel,
  inputSize,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  hint?: ReactNode;
  error?: string | null;
  hideLabel?: boolean;
  inputSize?: "sm" | "md";
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => {
    setDraft((current) => (Number.parseFloat(current) === value ? current : String(value)));
  }, [value]);
  return (
    <TextField
      label={label}
      hideLabel={hideLabel}
      inputSize={inputSize}
      type="number"
      inputMode="decimal"
      min={min}
      max={max}
      step={step}
      value={draft}
      hint={hint}
      error={error}
      onChange={(event) => {
        setDraft(event.target.value);
        const parsed = Number.parseFloat(event.target.value);
        if (Number.isFinite(parsed)) onChange(clamp(parsed, min, max));
      }}
      onBlur={() => setDraft(String(value))}
    />
  );
}

function RangeField({ label, value, min, max, step, display, onChange }: { label: string; value: number; min: number; max: number; step: number; display: string; onChange: (value: number) => void }) {
  return (
    <div className="field debt-range">
      <div className="debt-range__top">
        <label className="field__label" htmlFor={`range-${label}`}>
          {label}
        </label>
        <span className="debt-range__value">{display}</span>
      </div>
      <div className="debt-range__row">
        <input id={`range-${label}`} type="range" min={min} max={max} step={step} value={value} onChange={(event) => onChange(Number(event.target.value))} />
        <input
          className="input input--sm debt-range__num"
          type="number"
          inputMode="decimal"
          min={min}
          max={max}
          step={step}
          value={value}
          aria-label={`${label} value`}
          onChange={(event) => {
            const parsed = Number.parseFloat(event.target.value);
            if (Number.isFinite(parsed)) onChange(clamp(parsed, min, max));
          }}
        />
      </div>
    </div>
  );
}
