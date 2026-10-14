import { useState } from "react";
import { ApiError } from "../../api/client";
import { useDebtOfferWrite, type DebtOffer, type DebtOfferInput } from "../../api/personal";
import { cx } from "../../lib/cx";
import { computedPayment, quoteCheck, type LoanOffer, type QuoteCheck } from "../../lib/debtPayoff";
import { formatDate, formatMoney, parseMoney } from "../../lib/format";
import { Badge } from "../Badge";
import { Button } from "../Button";
import { Card, CardHeader } from "../Card";
import { Modal } from "../Modal";
import { TextField } from "../Select";
import { useToast } from "../Toast";

export const CUSTOM_KEY = "custom";

export function offerKey(id: number): string {
  return `offer:${id}`;
}

/** A saved offer as the simulation's loan. Duplicate lender names get their id. */
export function toLoanOffer(offer: DebtOffer, all: DebtOffer[]): LoanOffer {
  const twins = all.filter((item) => item.lender === offer.lender).length > 1;
  return {
    key: offerKey(offer.id),
    label: twins ? `${offer.lender} (#${offer.id})` : offer.lender,
    amount: offer.amount_cents / 100,
    apr: offer.apr,
    feePct: offer.fee_pct,
    feeDeducted: offer.fee_from_proceeds,
    termMonths: offer.term_months,
    quotedPayment: offer.monthly_payment_cents === null ? null : offer.monthly_payment_cents / 100,
  };
}

export function quoteNote(quote: QuoteCheck | null): string | null {
  if (!quote || !quote.differs) return null;
  return `Quoted ${formatMoney(quote.quoted * 100, { whole: true })} vs computed ${formatMoney(quote.computed * 100, { whole: true })}: the lender may include fees or insurance, or use a different rate.`;
}

export function feeText(feePct: number, fromProceeds: boolean): string {
  if (!feePct) return "no fee";
  return `${feePct}% fee ${fromProceeds ? "from proceeds" : "added"}`;
}

export function OffersCard({
  offers,
  loading,
  error,
  custom,
  selected,
  onSelect,
  onSaveCustom,
  savingCustom,
  onSaved,
}: {
  offers: DebtOffer[];
  loading: boolean;
  error: unknown;
  custom: LoanOffer;
  selected: string[];
  onSelect: (key: string, on: boolean) => void;
  onSaveCustom: () => void;
  savingCustom: boolean;
  onSaved: (offer: DebtOffer, created: boolean) => void;
}) {
  const [editing, setEditing] = useState<DebtOffer | "new" | null>(null);
  const [deleting, setDeleting] = useState<DebtOffer | null>(null);
  const { remove } = useDebtOfferWrite();
  const toast = useToast();
  return (
    <Card padded={false} className="debt-offers-card">
      <div className="debt-table-head">
        <CardHeader
          title="Loan offers"
          subtitle="Check the offers to compare. Each pays off the checked cards (and checked loans) below, highest APR first. Saved offers stay until you delete them."
          actions={
            <Button size="sm" variant="primary" icon="plus" onClick={() => setEditing("new")}>
              Add offer
            </Button>
          }
        />
      </div>
      {error ? <p className="debt-offers__error" role="alert">Couldn't load saved offers: {error instanceof Error ? error.message : "error"}</p> : null}
      <div className="debt-table-scroll" tabIndex={0} role="region" aria-label="Offers table">
        <table className="table debt-table debt-stack">
          <caption className="sr-only">Loan offers to compare</caption>
          <thead>
            <tr>
              <th scope="col" className="debt-table__check">
                Compare
              </th>
              <th scope="col">Offer</th>
              <th scope="col" className="num">
                Amount
              </th>
              <th scope="col" className="num">
                APR
              </th>
              <th scope="col">Fee</th>
              <th scope="col" className="num">
                Term
              </th>
              <th scope="col" className="num">
                Payment
              </th>
              <th scope="col">Expires</th>
              <th scope="col">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr className="debt-offers__custom">
              <td className="debt-table__check" data-label="Compare">
                <input type="checkbox" checked={selected.includes(CUSTOM_KEY)} onChange={(event) => onSelect(CUSTOM_KEY, event.target.checked)} aria-label="Compare Custom / what-if" />
              </td>
              <td data-label="Offer">
                <span className="debt-name">Custom / what-if</span>
                <span className="debt-sub debt-sub--block">From the Custom inputs above</span>
              </td>
              <td className="num" data-label="Amount">
                {formatMoney(custom.amount * 100)}
              </td>
              <td className="num" data-label="APR">
                {custom.apr}%
              </td>
              <td data-label="Fee">{feeText(custom.feePct, custom.feeDeducted)}</td>
              <td className="num" data-label="Term">
                {custom.termMonths} mo
              </td>
              <td className="num" data-label="Payment">
                {formatMoney(computedPayment(custom) * 100)}
              </td>
              <td data-label="Expires">—</td>
              <td className="debt-offers__actions">
                <Button size="sm" variant="secondary" icon="bookmark" onClick={onSaveCustom} loading={savingCustom}>
                  Save as offer
                </Button>
              </td>
            </tr>
            {loading ? (
              <tr>
                <td colSpan={9} className="debt-sub">
                  Loading saved offers…
                </td>
              </tr>
            ) : null}
            {!loading && !error && offers.length === 0 ? (
              <tr>
                <td colSpan={9} className="debt-sub">
                  No saved offers yet. Add one, save the custom what-if, or run <code>hpgbooks personal offers add</code>.
                </td>
              </tr>
            ) : null}
            {offers.map((offer) => {
              const loan = toLoanOffer(offer, offers);
              const note = quoteNote(quoteCheck(loan));
              return (
                <tr key={offer.id} className={cx(offer.expired && "is-expired")}>
                  <td className="debt-table__check" data-label="Compare">
                    <input type="checkbox" checked={selected.includes(loan.key)} onChange={(event) => onSelect(loan.key, event.target.checked)} aria-label={`Compare ${loan.label}`} />
                  </td>
                  <td data-label="Offer">
                    <span className="debt-name">{loan.label}</span> {offer.source !== "manual" ? <Badge tone="info">{offer.source}</Badge> : null}
                    {offer.notes ? <span className="debt-sub debt-sub--block">{offer.notes}</span> : null}
                  </td>
                  <td className="num" data-label="Amount">
                    {formatMoney(offer.amount_cents)}
                  </td>
                  <td className="num" data-label="APR">
                    {offer.apr}%
                  </td>
                  <td data-label="Fee">{feeText(offer.fee_pct, offer.fee_from_proceeds)}</td>
                  <td className="num" data-label="Term">
                    {offer.term_months} mo
                  </td>
                  <td className="num" data-label="Payment">
                    {offer.monthly_payment_cents !== null ? (
                      <>
                        {formatMoney(offer.monthly_payment_cents)} <span className="debt-sub">quoted</span>
                      </>
                    ) : (
                      <>
                        {formatMoney(computedPayment(loan) * 100)} <span className="debt-sub">computed</span>
                      </>
                    )}
                    {note ? <span className="debt-sub debt-sub--block debt-quote-note">{note}</span> : null}
                  </td>
                  <td data-label="Expires" className="nowrap">
                    {offer.expires_on ? formatDate(offer.expires_on) : "—"} {offer.expired ? <Badge tone="warn">expired</Badge> : null}
                  </td>
                  <td className="debt-offers__actions">
                    <Button size="sm" variant="ghost" icon="edit" onClick={() => setEditing(offer)} aria-label={`Edit ${loan.label}`}>
                      Edit
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setDeleting(offer)} aria-label={`Delete ${loan.label}`}>
                      Delete
                    </Button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {editing ? (
        <OfferFormModal
          offer={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={(offer, created) => {
            setEditing(null);
            onSaved(offer, created);
          }}
        />
      ) : null}
      <Modal
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        title="Delete this offer?"
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              loading={remove.isPending}
              onClick={() => {
                if (!deleting) return;
                const target = deleting;
                remove.mutate(target.id, {
                  onSuccess: () => {
                    setDeleting(null);
                    toast.toast({ tone: "success", title: `Deleted the ${target.lender} offer` });
                  },
                  onError: (error) => toast.toast({ tone: "error", title: "Couldn't delete", description: error instanceof Error ? error.message : "" }),
                });
              }}
            >
              Delete offer
            </Button>
          </>
        }
      >
        {deleting ? (
          <p>
            The {formatMoney(deleting.amount_cents, { whole: true })} offer from <strong>{deleting.lender}</strong> at {deleting.apr}% for {deleting.term_months} months will be removed. This cannot be undone.
          </p>
        ) : null}
      </Modal>
    </Card>
  );
}

interface Draft {
  lender: string;
  amount: string;
  apr: string;
  fee: string;
  feeFromProceeds: boolean;
  term: string;
  payment: string;
  expires: string;
  notes: string;
}

function draftFrom(offer: DebtOffer | null): Draft {
  if (!offer) return { lender: "", amount: "", apr: "", fee: "0", feeFromProceeds: true, term: "60", payment: "", expires: "", notes: "" };
  return {
    lender: offer.lender,
    amount: (offer.amount_cents / 100).toFixed(2),
    apr: String(offer.apr),
    fee: String(offer.fee_pct),
    feeFromProceeds: offer.fee_from_proceeds,
    term: String(offer.term_months),
    payment: offer.monthly_payment_cents === null ? "" : (offer.monthly_payment_cents / 100).toFixed(2),
    expires: offer.expires_on ?? "",
    notes: offer.notes,
  };
}

/** Field errors for a draft, and the request body when there are none. */
export function validateDraft(draft: Draft): { errors: Partial<Record<keyof Draft, string>>; body: DebtOfferInput | null } {
  const errors: Partial<Record<keyof Draft, string>> = {};
  const lender = draft.lender.trim();
  if (!lender) errors.lender = "Enter the lender";
  const amount = parseMoney(draft.amount);
  if (amount === null || amount <= 0) errors.amount = "Enter an amount above $0";
  const apr = Number(draft.apr);
  if (draft.apr.trim() === "" || !Number.isFinite(apr) || apr < 0 || apr > 100) errors.apr = "APR is 0 to 100";
  const fee = draft.fee.trim() === "" ? 0 : Number(draft.fee);
  if (!Number.isFinite(fee) || fee < 0 || fee > 10) errors.fee = "Fee is 0 to 10%";
  const term = Number(draft.term);
  if (!Number.isInteger(term) || term < 1 || term > 360) errors.term = "Term is 1 to 360 months";
  const payment = draft.payment.trim() ? parseMoney(draft.payment) : null;
  if (draft.payment.trim() && (payment === null || payment <= 0)) errors.payment = "Enter a payment above $0, or leave it blank";
  if (draft.expires && !/^\d{4}-\d{2}-\d{2}$/.test(draft.expires)) errors.expires = "Use a date";
  if (Object.keys(errors).length) return { errors, body: null };
  return {
    errors,
    body: {
      lender,
      amount_cents: amount as number,
      apr,
      fee_pct: fee,
      fee_from_proceeds: draft.feeFromProceeds,
      term_months: term,
      monthly_payment_cents: payment,
      notes: draft.notes.trim(),
      expires_on: draft.expires || null,
    },
  };
}

function OfferFormModal({ offer, onClose, onSaved }: { offer: DebtOffer | null; onClose: () => void; onSaved: (offer: DebtOffer, created: boolean) => void }) {
  const [draft, setDraft] = useState<Draft>(() => draftFrom(offer));
  const [errors, setErrors] = useState<Partial<Record<keyof Draft, string>>>({});
  const [serverError, setServerError] = useState<string | null>(null);
  const { save } = useDebtOfferWrite();
  const toast = useToast();
  const set = (patch: Partial<Draft>) => setDraft((current) => ({ ...current, ...patch }));
  const submit = () => {
    const checked = validateDraft(draft);
    setErrors(checked.errors);
    if (!checked.body) return;
    setServerError(null);
    save.mutate(
      { id: offer?.id, body: checked.body },
      {
        onSuccess: (data) => {
          toast.toast({ tone: "success", title: offer ? "Offer updated" : "Offer saved" });
          onSaved(data.offer as DebtOffer, !offer);
        },
        onError: (error) => setServerError(error instanceof ApiError || error instanceof Error ? error.message : "Couldn't save"),
      },
    );
  };
  return (
    <Modal
      open
      onClose={onClose}
      title={offer ? `Edit ${offer.lender} offer` : "Add a loan offer"}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" form="debt-offer-form" loading={save.isPending}>
            {offer ? "Save changes" : "Save offer"}
          </Button>
        </>
      }
    >
      <form
        id="debt-offer-form"
        className="debt-offer-form"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <TextField label="Lender" value={draft.lender} maxLength={80} onChange={(event) => set({ lender: event.target.value })} error={errors.lender} className="debt-offer-form__wide" />
        <TextField label="Amount ($)" inputMode="decimal" value={draft.amount} onChange={(event) => set({ amount: event.target.value })} error={errors.amount} />
        <TextField label="APR (%)" inputMode="decimal" value={draft.apr} onChange={(event) => set({ apr: event.target.value })} error={errors.apr} />
        <TextField label="Origination fee (%)" inputMode="decimal" value={draft.fee} onChange={(event) => set({ fee: event.target.value })} error={errors.fee} />
        <TextField label="Term (months)" inputMode="numeric" value={draft.term} onChange={(event) => set({ term: event.target.value })} error={errors.term} />
        <label className="debt-check debt-offer-form__wide">
          <input type="checkbox" role="switch" checked={draft.feeFromProceeds} onChange={(event) => set({ feeFromProceeds: event.target.checked })} />
          <span>
            Fee taken from the proceeds
            <span className="debt-check__hint">Off: you receive the full amount and pay the fee separately.</span>
          </span>
        </label>
        <TextField label="Quoted monthly payment ($, optional)" inputMode="decimal" value={draft.payment} onChange={(event) => set({ payment: event.target.value })} error={errors.payment} />
        <TextField label="Expires (optional)" type="date" value={draft.expires} onChange={(event) => set({ expires: event.target.value })} error={errors.expires} />
        <div className="field debt-offer-form__wide">
          <label className="field__label" htmlFor="debt-offer-notes">
            Notes (optional)
          </label>
          <textarea id="debt-offer-notes" className="input debt-offer-form__notes" rows={2} maxLength={1000} value={draft.notes} onChange={(event) => set({ notes: event.target.value })} />
        </div>
        {serverError ? (
          <p className="field__error debt-offer-form__wide" role="alert">
            {serverError}
          </p>
        ) : null}
      </form>
    </Modal>
  );
}
