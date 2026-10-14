import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiPost } from "../api/client";
import { keys, useMargins } from "../api/queries";
import type { MarginMapping, MarginOverheadLine, MarginRuleType, MarginServer, MarginTarget, Margins } from "../api/types";
import { Badge } from "../components/Badge";
import { Button } from "../components/Button";
import { Card, CardHeader } from "../components/Card";
import { DataTable, type Column } from "../components/DataTable";
import { EmptyState, ErrorState } from "../components/EmptyState";
import { KpiCard } from "../components/KpiCard";
import { Drawer } from "../components/Modal";
import { Money } from "../components/MoneyCell";
import { PageHeader } from "../components/PageHeader";
import { Select, TextField } from "../components/Select";
import { SkeletonCard } from "../components/Skeleton";
import { useToast } from "../components/Toast";
import { ClientLink, NotSynced, Pct, WhmcsEyebrow, isReady } from "../components/Whmcs";
import { useConfig } from "../hooks/useConfig";
import { centsToInput, formatMoney, formatPct, formatSigned } from "../lib/format";
import { EMPTY_SCENARIO, FLAG_LABELS, KIND_LABELS, RULE_LABELS, STATUS_LABELS, whatIf, type Scenario } from "../lib/margins";

type MarginSingle = Margins["single_customers"][number];
type MarginRollup = Margins["customer_rollups"][number];
type MarginPlanGroup = Margins["plan_groups"][number];
type MarginBrand = Margins["brands"][number];
type UnmappedGroup = Margins["unmapped"]["groups"][number];
type UnmappedItem = Margins["unmapped"]["top"][number];

const FLAG_TONES = { retire_candidate: "warn", zero_revenue: "neutral", negative_margin: "neg", retired: "neutral" } as const;

export default function WhmcsMarginsPage() {
  const query = useMargins();
  const raw = query.data;
  const data = isReady<Margins>(raw) ? raw : null;
  const [editing, setEditing] = useState<number | null>(null);
  const editServer = data?.servers.find((row) => row.id === editing) ?? null;

  return (
    <div className="page">
      <PageHeader
        eyebrow={<WhmcsEyebrow />}
        title="Server margins"
        subtitle="What each server costs, the WHMCS services it carries, and the margin. Revenue updates with every WHMCS sync."
      />
      {query.isError ? <ErrorState error={query.error} onRetry={() => query.refetch()} /> : null}
      {raw && !raw.ready ? (
        <NotSynced />
      ) : data && !data.seeded ? (
        <EmptyState icon="server" title="No servers yet">
          Run <code>bin/hpgbooks margins seed</code> on the books machine. It loads servers, cost components, mapping rules, and overhead from the local seed file.
        </EmptyState>
      ) : (
        <>
          <Headline data={data} />
          <ServersCard data={data} onEdit={setEditing} />
          <WhatIfCard data={data} />
          <BrandsCard data={data} />
          <CustomersCard data={data} />
          <PlanGroupsCard data={data} />
          <UnmappedCard data={data} />
          <OverheadCard data={data} />
          {data ? (
            <ul className="muted small margin-notes">
              {data.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          ) : null}
        </>
      )}
      <ServerDrawer server={editServer} onClose={() => setEditing(null)} />
    </div>
  );
}

function Headline({ data }: { data: Margins | null }) {
  if (!data) {
    return (
      <section className="kpi-grid" aria-label="Headline">
        {[0, 1, 2, 3].map((i) => (
          <KpiCard key={i} label="" loading />
        ))}
      </section>
    );
  }
  const t = data.totals;
  return (
    <section className="kpi-grid" aria-label="Headline">
      <KpiCard label="Active MRR" cents={t.mrr_cents} footer={<span className="muted">{t.active_services.toLocaleString("en-US")} active services</span>} />
      <KpiCard label="Server cost" cents={t.server_cost_cents} footer={<span className="muted">Contribution <Money cents={t.contribution_cents} /> ({formatPct(t.contribution_pct)})</span>} />
      <KpiCard label="Shared overhead" cents={t.overhead_cents} footer={<span className="muted">Infrastructure <Money cents={t.infrastructure_cents} /></span>} />
      <KpiCard label="Blended margin" cents={t.blended_margin_cents} footer={<span className="muted">{formatPct(t.blended_margin_pct)} of MRR, after all infrastructure</span>} />
    </section>
  );
}

function Flags({ server }: { server: MarginServer }) {
  if (!server.flags.length) return null;
  return (
    <span className="margin-flags">
      {server.flags.map((flag) => (
        <Badge key={flag} tone={FLAG_TONES[flag]}>
          {FLAG_LABELS[flag]}
        </Badge>
      ))}
    </span>
  );
}

function ServersCard({ data, onEdit }: { data: Margins | null; onEdit: (id: number) => void }) {
  const columns: Column<MarginServer>[] = [
    {
      key: "label",
      header: "Server",
      accessor: (row) => row.label,
      sortable: true,
      cell: (row) => (
        <div className="margin-server">
          <span className="strong">{row.label}</span>
          <span className="muted small">
            {row.vendor}
            {row.location ? ` · ${row.location}` : ""} · {KIND_LABELS[row.kind] ?? row.kind}
          </span>
          <Flags server={row} />
        </div>
      ),
    },
    { key: "cost", header: "Cost", accessor: (row) => row.cost_cents, format: "money", sortable: true },
    { key: "services", header: "Services", accessor: (row) => row.services, format: "number", sortable: true, hideOnMobile: true },
    { key: "customers", header: "Paying customers", accessor: (row) => row.paying_customers, format: "number", sortable: true, hideOnMobile: true },
    { key: "revenue", header: "Revenue", accessor: (row) => row.revenue_cents, format: "money", sortable: true },
    { key: "margin", header: "Margin", accessor: (row) => row.margin_cents, format: "money-signed", sortable: true },
    { key: "pct", header: "Margin %", accessor: (row) => row.margin_pct, cell: (row) => <Pct value={row.margin_pct} />, sortable: true },
    {
      key: "edit",
      header: <span className="sr-only">Edit</span>,
      cell: (row) => (
        <Button size="sm" variant="ghost" icon="edit" onClick={() => onEdit(row.id)} aria-label={`Edit ${row.label}`} data-no-row-click>
          Edit
        </Button>
      ),
    },
  ];
  const t = data?.totals;
  return (
    <Card padded={false} className="table-section">
      <div className="table-section__head">
        <CardHeader title="Per-server margin" subtitle="Monthly. Cost includes the licenses billed with the server. Pool servers share cost across their services by revenue." />
      </div>
      <DataTable
        caption="Margin by server"
        columns={columns}
        rows={data?.servers ?? []}
        rowKey={(row) => String(row.id)}
        loading={!data}
        defaultSort={{ key: "revenue", dir: "desc" }}
        rowClassName={(row) => (row.status === "retired" ? "row--muted" : undefined)}
        footer={
          t ? (
            <span className="muted small">
              Unmapped revenue <Money cents={t.unmapped_cents} /> ({t.unmapped_services.toLocaleString("en-US")} services) · overhead <Money cents={t.overhead_cents} /> · total margin{" "}
              <Money cents={t.blended_margin_cents} /> ({formatPct(t.blended_margin_pct)})
            </span>
          ) : null
        }
        dense
      />
    </Card>
  );
}

function BrandsCard({ data }: { data: Margins | null }) {
  const columns: Column<MarginBrand>[] = [
    { key: "brand", header: "Brand", accessor: (row) => row.brand },
    { key: "services", header: "Services", accessor: (row) => row.services, format: "number", hideOnMobile: true },
    { key: "revenue", header: "Revenue", accessor: (row) => row.revenue_cents, format: "money" },
    { key: "cost", header: "Server cost", accessor: (row) => row.cost_cents, format: "money" },
    { key: "margin", header: "Margin", accessor: (row) => row.margin_cents, format: "money-signed" },
    { key: "pct", header: "Margin %", accessor: (row) => row.margin_pct, cell: (row) => <Pct value={row.margin_pct} /> },
  ];
  return (
    <Card padded={false} className="table-section">
      <div className="table-section__head">
        <CardHeader title="By brand" subtitle="Server cost follows the mapping, before overhead. Servers with no revenue sit outside the brand rows." />
      </div>
      <DataTable caption="Margin by brand" columns={columns} rows={data?.brands ?? []} rowKey={(row) => row.brand} loading={!data} dense />
    </Card>
  );
}

function CustomersCard({ data }: { data: Margins | null }) {
  const single: Column<MarginSingle>[] = [
    { key: "name", header: "Customer", accessor: (row) => row.name ?? "", sortable: true, cell: (row) => <span className="strong">{row.name}</span> },
    { key: "client", header: "Client", accessor: (row) => row.client_id, cell: (row) => <ClientLink brand={row.brand} id={row.client_id} />, hideOnMobile: true },
    { key: "server", header: "Server", accessor: (row) => row.server, sortable: true, hideOnMobile: true },
    { key: "cost", header: "Cost", accessor: (row) => row.cost_cents, format: "money", sortable: true },
    { key: "revenue", header: "Revenue", accessor: (row) => row.revenue_cents, format: "money", sortable: true },
    { key: "margin", header: "Margin", accessor: (row) => row.margin_cents, format: "money-signed", sortable: true },
    { key: "pct", header: "Margin %", accessor: (row) => row.margin_pct, cell: (row) => <Pct value={row.margin_pct} />, sortable: true },
  ];
  const rollup: Column<MarginRollup>[] = [
    { key: "name", header: "Customer", accessor: (row) => row.name ?? "", cell: (row) => <span className="strong">{row.name}</span> },
    { key: "client", header: "Client", accessor: (row) => row.client_id, cell: (row) => <ClientLink brand={row.brand} id={row.client_id} />, hideOnMobile: true },
    { key: "servers", header: "Servers", accessor: (row) => row.servers.join("; "), hideOnMobile: true },
    { key: "revenue", header: "Revenue", accessor: (row) => row.revenue_cents, format: "money" },
    { key: "cost", header: "Cost", accessor: (row) => row.cost_cents, format: "money" },
    { key: "margin", header: "Margin", accessor: (row) => row.margin_cents, format: "money-signed" },
    { key: "pct", header: "Margin %", accessor: (row) => row.margin_pct, cell: (row) => <Pct value={row.margin_pct} /> },
  ];
  return (
    <Card padded={false} className="table-section">
      <div className="table-section__head">
        <CardHeader title="Single-customer servers" subtitle="Revenue includes each service's addons. Before overhead." />
      </div>
      <DataTable caption="Margin by customer on single-customer servers" columns={single} rows={data?.single_customers ?? []} rowKey={(row) => String(row.server_id)} loading={!data} defaultSort={{ key: "revenue", dir: "desc" }} dense />
      {data && data.customer_rollups.length ? (
        <>
          <div className="table-section__head">
            <CardHeader title="Customers on more than one server" subtitle="Every active service; pool cost shared by revenue." />
          </div>
          <DataTable caption="Customers on more than one server" columns={rollup} rows={data.customer_rollups} rowKey={(row) => `${row.brand}:${row.client_id}`} dense />
        </>
      ) : null}
    </Card>
  );
}

function PlanGroupsCard({ data }: { data: Margins | null }) {
  const columns: Column<MarginPlanGroup>[] = [
    { key: "server", header: "Server", accessor: (row) => row.server, sortable: true },
    { key: "group", header: "Plan group", accessor: (row) => row.group, sortable: true, cell: (row) => <span className="strong">{row.group}</span> },
    { key: "services", header: "Services", accessor: (row) => row.services, format: "number", sortable: true, hideOnMobile: true },
    { key: "paying", header: "Paying", accessor: (row) => row.paying_services, format: "number", hideOnMobile: true },
    { key: "revenue", header: "Revenue", accessor: (row) => row.revenue_cents, format: "money", sortable: true },
    { key: "cost", header: "Cost (by revenue)", accessor: (row) => row.cost_cents, format: "money", hideOnMobile: true },
    { key: "margin", header: "Margin", accessor: (row) => row.margin_cents, format: "money-signed", sortable: true },
    { key: "even", header: "Margin if cost split evenly", accessor: (row) => row.even_margin_cents, format: "money-signed", sortable: true },
  ];
  return (
    <Card padded={false} className="table-section">
      <div className="table-section__head">
        <CardHeader title="Plan groups on shared servers" subtitle="Cost shared by revenue gives every group the server's margin %; the even split (cost per service) shows which plans are carried by others." />
      </div>
      <DataTable caption="Plan groups on shared servers" columns={columns} rows={data?.plan_groups ?? []} rowKey={(row) => `${row.server_id}:${row.group}`} loading={!data} pageSize={25} dense />
    </Card>
  );
}

function UnmappedCard({ data }: { data: Margins | null }) {
  const groups: Column<UnmappedGroup>[] = [
    { key: "brand", header: "Brand", accessor: (row) => row.brand },
    { key: "group", header: "Group", accessor: (row) => row.group },
    { key: "cycle", header: "Cycle", accessor: (row) => row.cycle, hideOnMobile: true },
    { key: "services", header: "Services", accessor: (row) => row.services, format: "number" },
    { key: "paying", header: "Paying", accessor: (row) => row.paying_services, format: "number", hideOnMobile: true },
    { key: "revenue", header: "MRR", accessor: (row) => row.revenue_cents, format: "money" },
  ];
  const top: Column<UnmappedItem>[] = [
    { key: "service", header: "Service", accessor: (row) => row.service_id, cell: (row) => <span className="num">#{row.service_id}{row.kind === "addon" ? " (addon)" : ""}</span> },
    { key: "customer", header: "Customer", accessor: (row) => row.name ?? "", cell: (row) => <span>{row.name} <ClientLink brand={row.brand} id={row.client_id} /></span> },
    { key: "plan", header: "Plan", accessor: (row) => row.plan, hideOnMobile: true },
    { key: "domain", header: "Domain", accessor: (row) => row.domain ?? "", hideOnMobile: true },
    { key: "why", header: "Why", accessor: (row) => row.reason, hideOnMobile: true },
    { key: "revenue", header: "MRR", accessor: (row) => row.revenue_cents, format: "money" },
  ];
  return (
    <Card padded={false} className="table-section">
      <div className="table-section__head">
        <CardHeader
          title="Active revenue with no server"
          subtitle={data ? `${data.totals.unmapped_services.toLocaleString("en-US")} services, ${formatMoney(data.totals.unmapped_cents)} a month. Add a mapping on a server to place them.` : undefined}
        />
      </div>
      <DataTable caption="Unmapped revenue by group" columns={groups} rows={data?.unmapped.groups ?? []} rowKey={(row) => `${row.brand}:${row.group}:${row.cycle}`} loading={!data} dense />
      {data && data.unmapped.top.length ? (
        <>
          <div className="table-section__head">
            <CardHeader title="Largest unmapped services" />
          </div>
          <DataTable caption="Largest unmapped services" columns={top} rows={data.unmapped.top} rowKey={(row) => `${row.brand}:${row.kind}:${row.service_id}`} dense />
        </>
      ) : null}
    </Card>
  );
}

// --- edits ------------------------------------------------------------------------------------

function useMarginEdit(success: string) {
  const client = useQueryClient();
  const { toast } = useToast();
  return useMutation({
    mutationFn: ({ path, body }: { path: string; body: Record<string, unknown> }) => apiPost<{ ok: true }>(path, body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.margins });
      void client.invalidateQueries({ queryKey: ["audit"] });
      toast({ tone: "success", title: success });
    },
    onError: (error) => toast({ tone: "error", title: "Couldn't save", description: error instanceof Error ? error.message : String(error) }),
  });
}

function OverheadCard({ data }: { data: Margins | null }) {
  const save = useMarginEdit("Overhead saved");
  const [label, setLabel] = useState("");
  const [vendor, setVendor] = useState("");
  const [amount, setAmount] = useState("");
  const columns: Column<MarginOverheadLine>[] = [
    { key: "label", header: "Item", accessor: (row) => row.label, sortable: true, cell: (row) => <span className={row.kind === "shared" ? "strong" : "muted"}>{row.label}</span> },
    { key: "vendor", header: "Vendor", accessor: (row) => row.vendor, sortable: true, hideOnMobile: true },
    { key: "cost", header: "$/month", accessor: (row) => row.monthly_cost_cents, format: "money", sortable: true },
    {
      key: "kind",
      header: "Counts as",
      cell: (row) => (
        <Select
          label={`Counts as, ${row.label}`}
          hideLabel
          size="sm"
          value={row.kind}
          disabled={save.isPending}
          onChange={(kind) => save.mutate({ path: `/margins/overhead/${row.id}`, body: { kind } })}
          options={[
            { value: "shared", label: "Shared overhead" },
            { value: "not_this_business", label: "Not this business" },
          ]}
        />
      ),
    },
    { key: "note", header: "Note", accessor: (row) => row.note, hideOnMobile: true },
  ];
  return (
    <Card padded={false} className="table-section">
      <div className="table-section__head">
        <CardHeader
          title="Overhead"
          subtitle={data ? `Spread across all revenue: ${formatMoney(data.totals.overhead_cents)} a month${data.totals.overhead_excluded_cents ? `; ${formatMoney(data.totals.overhead_excluded_cents)} marked not this business is left out` : ""}.` : undefined}
        />
      </div>
      <DataTable caption="Overhead lines" columns={columns} rows={data?.overhead ?? []} rowKey={(row) => String(row.id)} loading={!data} defaultSort={{ key: "cost", dir: "desc" }} dense />
      <form
        className="margin-form no-print"
        aria-label="Add an overhead line"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate(
            { path: "/margins/overhead", body: { label, vendor, monthly_cost: amount, kind: "shared" } },
            {
              onSuccess: () => {
                setLabel("");
                setVendor("");
                setAmount("");
              },
            },
          );
        }}
      >
        <TextField label="New overhead item" inputSize="sm" value={label} maxLength={200} onChange={(event) => setLabel(event.target.value)} required />
        <TextField label="Vendor" inputSize="sm" value={vendor} maxLength={80} onChange={(event) => setVendor(event.target.value)} />
        <TextField label="$/month" inputSize="sm" inputMode="decimal" value={amount} onChange={(event) => setAmount(event.target.value)} required />
        <Button type="submit" size="sm" variant="primary" icon="plus" loading={save.isPending}>
          Add overhead
        </Button>
      </form>
    </Card>
  );
}

function ServerDrawer({ server, onClose }: { server: MarginServer | null; onClose: () => void }) {
  return (
    <Drawer open={server !== null} onClose={onClose} title={server ? server.label : "Server"} description="Changes are saved to the encrypted database and written to the audit log.">
      {server ? <ServerEditor key={server.id} server={server} /> : null}
    </Drawer>
  );
}

function ServerEditor({ server }: { server: MarginServer }) {
  const save = useMarginEdit("Saved");
  const [status, setStatus] = useState<string>(server.status);
  const [notes, setNotes] = useState(server.notes);
  const [component, setComponent] = useState("");
  const [amount, setAmount] = useState("");
  const [ruleType, setRuleType] = useState<MarginRuleType>("service_id");
  const [ruleValue, setRuleValue] = useState("");
  const { whmcs_brands: brands } = useConfig();
  const [scope, setScope] = useState<string>(brands[0] ?? "");
  const [allocation, setAllocation] = useState<string>(server.kind === "pool" ? "by_revenue" : "direct");

  return (
    <div className="stack">
      <form
        className="form-stack"
        aria-label="Server status"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate({ path: `/margins/servers/${server.id}`, body: { status, notes } });
        }}
      >
        <Select label="Status" value={status} onChange={setStatus} options={Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label }))} />
        <TextField label="Notes" value={notes} maxLength={1000} onChange={(event) => setNotes(event.target.value)} />
        <div>
          <Button type="submit" variant="primary" size="sm" loading={save.isPending}>
            Save status
          </Button>
        </div>
      </form>

      <section aria-label="Cost components">
        <h3 className="section-title">Cost components · {formatMoney(server.cost_cents)}/month</h3>
        <ul className="margin-list">
          {server.components.map((item) => (
            <ComponentRow key={item.id} item={item} />
          ))}
        </ul>
        <form
          className="margin-form"
          aria-label="Add a cost component"
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate(
              { path: `/margins/servers/${server.id}/costs`, body: { component, monthly_cost: amount } },
              {
                onSuccess: () => {
                  setComponent("");
                  setAmount("");
                },
              },
            );
          }}
        >
          <TextField label="Component" inputSize="sm" placeholder="server, licence, ip block…" value={component} maxLength={80} onChange={(event) => setComponent(event.target.value)} required />
          <TextField label="$/month" inputSize="sm" inputMode="decimal" value={amount} onChange={(event) => setAmount(event.target.value)} required />
          <Button type="submit" size="sm" icon="plus" loading={save.isPending}>
            Add cost
          </Button>
        </form>
      </section>

      <section aria-label="Mapping rules">
        <h3 className="section-title">Which services run here</h3>
        <ul className="margin-list">
          {server.mappings.length ? server.mappings.map((rule) => <MappingRow key={rule.id} rule={rule} />) : <li className="muted small">No rules. Revenue from this server's services shows as unmapped.</li>}
        </ul>
        <form
          className="margin-form"
          aria-label="Add a mapping rule"
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate(
              { path: "/margins/mappings", body: { server_id: server.id, rule_type: ruleType, rule_value: ruleValue, brand_scope: scope, allocation } },
              { onSuccess: () => setRuleValue("") },
            );
          }}
        >
          <Select label="Rule" size="sm" value={ruleType} onChange={(value) => setRuleType(value as MarginRuleType)} options={Object.entries(RULE_LABELS).map(([value, label]) => ({ value, label }))} />
          <TextField
            label="Value"
            inputSize="sm"
            value={ruleValue}
            maxLength={200}
            placeholder={ruleType === "service_id" ? "1234 or addon:123" : ruleType === "domain" ? "example.com" : ruleType === "brand" ? scope || brands[0] || "" : ""}
            onChange={(event) => setRuleValue(event.target.value)}
            required
          />
          <Select label="Brand" size="sm" value={scope} onChange={setScope} options={[{ value: "", label: "Every brand" }, ...brands.map((brand) => ({ value: brand, label: brand }))]} />
          <Select
            label="Cost"
            size="sm"
            value={allocation}
            onChange={setAllocation}
            options={[
              { value: "direct", label: "Direct" },
              { value: "by_revenue", label: "Shared by revenue" },
            ]}
          />
          <Button type="submit" size="sm" icon="plus" loading={save.isPending}>
            Add rule
          </Button>
        </form>
        <p className="muted small">Rules apply in this order: service, domain, WHMCS server, product group, brand. Addons follow their parent service.</p>
      </section>
    </div>
  );
}

function ComponentRow({ item }: { item: MarginServer["components"][number] }) {
  const save = useMarginEdit("Cost saved");
  const remove = useMarginEdit("Cost removed");
  const [amount, setAmount] = useState(centsToInput(item.monthly_cost_cents));
  return (
    <li className="margin-list__row">
      <span className="strong">{item.component}</span>
      <form
        className="margin-inline"
        aria-label={`Edit ${item.component}`}
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate({ path: `/margins/costs/${item.id}`, body: { monthly_cost: amount } });
        }}
      >
        <TextField label={`${item.component} $/month`} hideLabel inputSize="sm" inputMode="decimal" value={amount} onChange={(event) => setAmount(event.target.value)} />
        <Button type="submit" size="sm" loading={save.isPending} disabled={amount === centsToInput(item.monthly_cost_cents)}>
          Save
        </Button>
        <Button size="sm" variant="ghost" loading={remove.isPending} onClick={() => remove.mutate({ path: `/margins/costs/${item.id}/delete`, body: {} })} aria-label={`Remove ${item.component}`}>
          Remove
        </Button>
      </form>
    </li>
  );
}

function MappingRow({ rule }: { rule: MarginMapping }) {
  const remove = useMarginEdit("Rule removed");
  const text = `${RULE_LABELS[rule.rule_type]} ${rule.rule_value}`;
  return (
    <li className="margin-list__row">
      <span>
        <span className="strong">{text}</span>
        <span className="muted small"> · {rule.brand_scope || "every brand"} · {rule.allocation === "by_revenue" ? "shared by revenue" : "direct"}</span>
      </span>
      <Button size="sm" variant="ghost" loading={remove.isPending} onClick={() => remove.mutate({ path: `/margins/mappings/${rule.id}/delete`, body: {} })} aria-label={`Remove rule ${text}`}>
        Remove
      </Button>
    </li>
  );
}

// --- what-if ------------------------------------------------------------------------------------

function WhatIfCard({ data }: { data: Margins | null }) {
  const [scenario, setScenario] = useState<Scenario | null>(null);
  const [targetKind, setTargetKind] = useState<"customer" | "plan">("customer");
  const [targetKey, setTargetKey] = useState("");
  const [mode, setMode] = useState<"pct" | "flat">("pct");
  const [amount, setAmount] = useState("");
  const current: Scenario = scenario ?? { ...EMPTY_SCENARIO, merge: data?.whatif.merge_default ?? null };

  const targets: MarginTarget[] = data ? (targetKind === "customer" ? data.whatif.customers : data.whatif.plans) : [];
  const target = targets.find((item) => item.key === targetKey) ?? null;
  const amountNumber = Number(amount);
  const increase = target && amount && Number.isFinite(amountNumber) && amountNumber > 0 ? { target, mode, amount: mode === "pct" ? amountNumber : Math.round(amountNumber * 100) } : null;
  if (!data) return <SkeletonCard height={200} />;
  const result = whatIf(data, { ...current, increase });

  const active = data.servers.filter((row) => row.status !== "retired");
  const serverOptions = active.map((row) => ({ value: String(row.id), label: `${row.label} (${formatMoney(row.cost_cents)})` }));
  const update = (patch: Partial<Scenario>) => setScenario({ ...current, ...patch });
  const targetLabel = (item: MarginTarget) =>
    item.plan !== undefined ? `${item.plan} · ${item.brand} (${formatMoney(item.revenue_cents)})` : `${item.name ?? `#${item.client_id}`} · ${item.brand} (${formatMoney(item.revenue_cents)})`;

  return (
    <Card>
      <CardHeader title="What if" subtitle="Try changes here; nothing is saved. Effects combine." actions={<Button size="sm" variant="ghost" icon="undo" onClick={() => { setScenario(EMPTY_SCENARIO); setAmount(""); setTargetKey(""); }}>Clear</Button>} />
      <div className="whatif-grid">
        <fieldset className="whatif-box">
          <legend>Merge servers</legend>
          <Select
            label="Move services from"
            size="sm"
            value={current.merge ? String(current.merge[0]) : ""}
            onChange={(value) => update({ merge: value ? [Number(value), current.merge?.[1] ?? Number(serverOptions.find((o) => o.value !== value)?.value)] : null })}
            options={[{ value: "", label: "No merge" }, ...serverOptions]}
          />
          <Select
            label="Onto"
            size="sm"
            value={current.merge ? String(current.merge[1]) : ""}
            disabled={!current.merge}
            onChange={(value) => current.merge && update({ merge: [current.merge[0], Number(value)] })}
            options={serverOptions}
          />
        </fieldset>
        <fieldset className="whatif-box">
          <legend>Retire a server</legend>
          <Select
            label="Retire"
            size="sm"
            value={current.retire === null ? "" : String(current.retire)}
            onChange={(value) => update({ retire: value ? Number(value) : null })}
            options={[{ value: "", label: "None" }, ...serverOptions]}
            hint="Its revenue goes too. To keep the customers, merge instead."
          />
        </fieldset>
        <fieldset className="whatif-box">
          <legend>Raise a price</legend>
          <Select
            label="For"
            size="sm"
            value={targetKind}
            onChange={(value) => {
              setTargetKind(value as "customer" | "plan");
              setTargetKey("");
            }}
            options={[
              { value: "customer", label: "A customer" },
              { value: "plan", label: "A plan" },
            ]}
          />
          <Select label={targetKind === "customer" ? "Customer" : "Plan"} size="sm" value={targetKey} onChange={setTargetKey} options={[{ value: "", label: "Choose…" }, ...targets.map((item) => ({ value: item.key, label: targetLabel(item) }))]} />
          <div className="form-row">
            <Select
              label="By"
              size="sm"
              value={mode}
              onChange={(value) => setMode(value as "pct" | "flat")}
              options={[
                { value: "pct", label: "Percent" },
                { value: "flat", label: "$ per service" },
              ]}
            />
            <TextField label={mode === "pct" ? "Percent" : "$ a month"} inputSize="sm" inputMode="decimal" value={amount} onChange={(event) => setAmount(event.target.value)} />
          </div>
        </fieldset>
        <fieldset className="whatif-box">
          <legend>Overhead that is not this business</legend>
          <div className="whatif-checks">
            {data.overhead
              .filter((line) => line.kind === "shared")
              .map((line) => (
                <label key={line.id} className="check">
                  <input
                    type="checkbox"
                    checked={current.excluded.includes(line.id)}
                    onChange={(event) => update({ excluded: event.target.checked ? [...current.excluded, line.id] : current.excluded.filter((id) => id !== line.id) })}
                  />
                  <span>
                    {line.label} <span className="muted">({formatMoney(line.monthly_cost_cents)})</span>
                  </span>
                </label>
              ))}
          </div>
        </fieldset>
      </div>
      <div className="whatif-result" aria-live="polite">
          {result.error ? <p className="field__error">{result.error}</p> : null}
          <section className="kpi-grid" aria-label="What-if result">
            <KpiCard label="Blended margin" cents={result.blended_margin_cents} footer={<span className="muted">{formatPct(result.blended_margin_pct)} of MRR (now {formatPct(data.totals.blended_margin_pct)})</span>} />
            <KpiCard label="Change per month" display={formatSigned(result.blended_change_cents)} footer={<span className="muted">{formatSigned(result.blended_change_cents * 12)} a year</span>} />
            <KpiCard label="Cost saved" cents={result.cost_saved_cents} footer={<span className="muted">Server and overhead</span>} />
            <KpiCard label="Revenue added" cents={result.revenue_added_cents} footer={<span className="muted">MRR <Money cents={result.mrr_cents} /></span>} />
          </section>
          {result.servers.length ? (
            <DataTable
              caption="Servers changed by the what-if"
              columns={[
                { key: "label", header: "Server", accessor: (row) => row.label, cell: (row) => <span className="strong">{row.label}{row.removed ? <> <Badge tone="warn">Removed</Badge></> : null}</span> },
                { key: "cost", header: "Cost after", accessor: (row) => row.cost_after_cents, format: "money" },
                { key: "revenue", header: "Revenue after", accessor: (row) => row.revenue_after_cents, format: "money" },
                { key: "before", header: "Margin before", accessor: (row) => row.margin_before_cents, format: "money-signed", hideOnMobile: true },
                { key: "after", header: "Margin after", accessor: (row) => row.margin_after_cents, format: "money-signed" },
                { key: "pct", header: "Margin %", accessor: (row) => row.margin_after_pct, cell: (row) => <Pct value={row.margin_after_pct} /> },
              ]}
              rows={result.servers}
              rowKey={(row) => String(row.id)}
              dense
            />
          ) : null}
      </div>
    </Card>
  );
}
