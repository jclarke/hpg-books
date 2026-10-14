import type { MarginTarget, Margins } from "../api/types";

/** What-if inputs. Everything is computed in the browser; nothing is saved. */
export interface Scenario {
  /** Move server A's services onto server B: A's cost goes away, its revenue moves to B. */
  merge: [number, number] | null;
  /** Drop a server with its revenue. */
  retire: number | null;
  /** Reprice one customer or plan: a percentage, or flat cents per paying service a month. */
  increase: { target: MarginTarget; mode: "pct" | "flat"; amount: number } | null;
  /** Overhead line ids treated as "not this business". */
  excluded: number[];
}

export interface ServerChange {
  id: number;
  label: string;
  removed: boolean;
  cost_before_cents: number;
  cost_after_cents: number;
  revenue_before_cents: number;
  revenue_after_cents: number;
  margin_before_cents: number;
  margin_after_cents: number;
  margin_after_pct: number | null;
}

export interface WhatIfResult {
  mrr_cents: number;
  server_cost_cents: number;
  overhead_cents: number;
  cost_saved_cents: number;
  revenue_added_cents: number;
  blended_margin_cents: number;
  blended_margin_pct: number | null;
  blended_change_cents: number;
  servers: ServerChange[];
  error: string | null;
}

export const EMPTY_SCENARIO: Scenario = { merge: null, retire: null, increase: null, excluded: [] };

function pct(part: number, whole: number): number | null {
  if (!whole) return null;
  return Math.round((part * 1000) / whole) / 10;
}

/** The same arithmetic as hpgbooks.margins.whatif on the server. */
export function whatIf(report: Margins, scenario: Scenario): WhatIfResult {
  const servers = new Map<number, { label: string; cost: number; revenue: number }>();
  for (const row of report.servers) {
    if (row.status !== "retired") servers.set(row.id, { label: row.label, cost: row.cost_cents, revenue: row.revenue_cents });
  }
  const before = new Map([...servers].map(([id, row]) => [id, { ...row }]));
  const gone = new Set<number>();
  let unmapped = report.totals.unmapped_cents;
  let error: string | null = null;
  let merge = scenario.merge;
  let retire = scenario.retire;
  if (merge && (merge[0] === merge[1] || !servers.has(merge[0]) || !servers.has(merge[1]))) {
    error = "Pick two different active servers to merge.";
    merge = null;
  }
  if (retire !== null && merge && (retire === merge[0] || retire === merge[1])) {
    error = "A merged server cannot also be retired.";
    retire = null;
  }
  if (retire !== null && !servers.has(retire)) retire = null;
  if (merge) {
    const [a, b] = merge;
    servers.get(b)!.revenue += servers.get(a)!.revenue;
    Object.assign(servers.get(a)!, { revenue: 0, cost: 0 });
    gone.add(a);
  }
  if (retire !== null) {
    Object.assign(servers.get(retire)!, { revenue: 0, cost: 0 });
    gone.add(retire);
  }
  let added = 0;
  const inc = scenario.increase;
  if (inc && inc.amount > 0) {
    const total = inc.target.revenue_cents;
    const extra = inc.mode === "pct" ? (total * inc.amount) / 100 : inc.amount * inc.target.paying_services;
    for (const [key, part] of Object.entries(inc.target.by_server)) {
      const share = total ? (extra * part) / total : 0;
      if (key === "unmapped") {
        unmapped += share;
        added += share;
        continue;
      }
      let id = Number(key);
      if (merge && id === merge[0]) id = merge[1];
      const row = servers.get(id);
      if (!row || gone.has(id)) continue;
      row.revenue += share;
      added += share;
    }
  }
  const excluded = new Set(scenario.excluded);
  const overhead = report.overhead.filter((line) => line.kind === "shared" && !excluded.has(line.id)).reduce((sum, line) => sum + line.monthly_cost_cents, 0);
  let mrr = unmapped;
  let cost = 0;
  for (const row of servers.values()) {
    mrr += row.revenue;
    cost += row.cost;
  }
  const blended = mrr - cost - overhead;
  const base = report.totals;
  const changes: ServerChange[] = [];
  for (const [id, row] of servers) {
    const old = before.get(id)!;
    if (row.cost === old.cost && row.revenue === old.revenue) continue;
    changes.push({
      id,
      label: row.label,
      removed: gone.has(id),
      cost_before_cents: old.cost,
      cost_after_cents: row.cost,
      revenue_before_cents: old.revenue,
      revenue_after_cents: Math.round(row.revenue),
      margin_before_cents: old.revenue - old.cost,
      margin_after_cents: Math.round(row.revenue - row.cost),
      margin_after_pct: pct(row.revenue - row.cost, row.revenue),
    });
  }
  return {
    mrr_cents: Math.round(mrr),
    server_cost_cents: cost,
    overhead_cents: overhead,
    cost_saved_cents: base.server_cost_cents + base.overhead_cents - cost - overhead,
    revenue_added_cents: Math.round(added),
    blended_margin_cents: Math.round(blended),
    blended_margin_pct: pct(blended, mrr),
    blended_change_cents: Math.round(blended) - base.blended_margin_cents,
    servers: changes,
    error,
  };
}

export const KIND_LABELS: Record<string, string> = { dedicated: "Dedicated", cloud_vm: "Cloud VM", pool: "Shared pool", overhead: "Overhead" };
export const STATUS_LABELS: Record<string, string> = { active: "Active", retire_candidate: "Retire candidate", retired: "Retired" };
export const FLAG_LABELS: Record<string, string> = { retire_candidate: "Retire candidate", zero_revenue: "No revenue", negative_margin: "Loses money", retired: "Retired" };
export const RULE_LABELS: Record<string, string> = { service_id: "Service #", domain: "Domain", whmcs_server_id: "WHMCS server #", product_group: "Product group", brand: "Whole brand" };
