import type { ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import { exportUrl } from "../api/client";
import type { WhmcsNotReady } from "../api/types";
import { EmptyState } from "./EmptyState";
import { Icon } from "./Icon";
import { SegmentedControl } from "./SegmentedControl";
import { useConfig } from "../hooks/useConfig";
import { useGlobalFilters, withGlobal } from "../hooks/useGlobalFilters";
import { useSession } from "../hooks/useSession";
import { useUrlState } from "../hooks/useUrlState";
import { calendarToday, monthStart, shiftMonth } from "../lib/dates";
import { cx } from "../lib/cx";

/** The oldest WHMCS payments are from late 2008. */
export const WHMCS_FIRST_DAY = "2008-01-01";

type Params = Record<string, string | number | boolean | null | undefined>;

export function isReady<T extends { ready: true }>(data: T | WhmcsNotReady | undefined): data is T {
  return Boolean(data && data.ready);
}

/** Brand filter kept in the URL (?brand=), separate from the ledger's business filter. */
export function useBrand(): [string, (brand: string) => void] {
  const url = useUrlState();
  const { whmcs_brands: brands } = useConfig();
  const raw = url.get("brand", "all");
  const brand = brands.includes(raw) ? raw : "all";
  return [brand, (next: string) => url.patch({ brand: next === "all" ? "" : next })];
}

export function BrandPicker({ value, onChange }: { value: string; onChange: (brand: string) => void }) {
  const { whmcs_brands: brands } = useConfig();
  return (
    <SegmentedControl
      label="Brand"
      size="sm"
      value={value}
      onChange={onChange}
      options={[{ value: "all", label: "All brands" }, ...brands.map((brand) => ({ value: brand, label: brand }))]}
    />
  );
}

/** Ranges that reach back into WHMCS history; the topbar presets only cover the ledger's months. */
export function PeriodShortcuts() {
  const filters = useGlobalFilters();
  const { data: session } = useSession();
  const today = calendarToday(session?.today);
  const month = today.slice(0, 7);
  const options = [
    { key: "ytd", label: "Year to date", start: `${today.slice(0, 4)}-01-01` },
    { key: "12m", label: "12 months", start: monthStart(shiftMonth(month, -11)) },
    { key: "3y", label: "3 years", start: monthStart(shiftMonth(month, -35)) },
    { key: "5y", label: "5 years", start: monthStart(shiftMonth(month, -59)) },
    { key: "all", label: "All history", start: WHMCS_FIRST_DAY },
  ];
  const current = options.find((item) => item.start === filters.start && filters.end === today)?.key ?? "";
  return (
    <SegmentedControl
      label="Period"
      size="sm"
      value={current}
      onChange={(key) => {
        const item = options.find((option) => option.key === key);
        if (item) filters.setRange({ start: item.start, end: today });
      }}
      options={options.map((item) => ({ value: item.key, label: item.label }))}
    />
  );
}

export function WhmcsToolbar({ children, brand, period = true }: { children?: ReactNode; brand?: { value: string; onChange: (brand: string) => void }; period?: boolean }) {
  return (
    <div className="toolbar whmcs-toolbar no-print">
      {brand ? <BrandPicker value={brand.value} onChange={brand.onChange} /> : null}
      {period ? <PeriodShortcuts /> : null}
      {children}
    </div>
  );
}

export function NotSynced() {
  return (
    <EmptyState icon="server" title="WHMCS has not been synced yet">
      Run <code>bin/hpgbooks whmcs sync</code> on the books machine. It opens the SSH tunnels, copies the billing data into the encrypted database, and closes them.
    </EmptyState>
  );
}

export function CsvLink({ table, params, label = "CSV" }: { table: string; params: Params; label?: string }) {
  return (
    <a className="btn btn--secondary btn--sm" href={exportUrl(`/export/whmcs/${table}.csv`, params)} download>
      <Icon name="download" size={15} /> {label}
    </a>
  );
}

export function WhmcsEyebrow() {
  const location = useLocation();
  return (
    <Link to={withGlobal("/whmcs", location.search)} className="link-quiet">
      <Icon name="chevronLeft" size={14} /> WHMCS billing
    </Link>
  );
}

/** Client id that opens the signed-in customer page. Aggregate reports never show names. */
export function ClientLink({ brand, id }: { brand: string | null; id: number | null }) {
  const location = useLocation();
  if (!brand || id === null || id === undefined || id === 0) return <span className="muted">—</span>;
  return (
    <Link to={withGlobal(`/whmcs/customers/${brand}/${id}`, location.search)} className="num" data-no-row-click>
      #{id}
    </Link>
  );
}

export function StatusBadgeText({ status }: { status: string }) {
  const tone = status === "Active" || status === "Paid" || status === "ok" ? "pos" : status === "Suspended" || status === "Unpaid" ? "warn" : status === "error" || status === "Fraud" ? "neg" : "muted";
  return <span className={cx("status-text", `status-text--${tone}`)}>{status || "—"}</span>;
}

export function Pct({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) return <span className="muted">—</span>;
  return <span className="num">{value.toFixed(value >= 100 ? 0 : 1)}%</span>;
}
