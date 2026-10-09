import { useEffect, useRef, useState } from "react";
import { ApiError, ExportCreateRequest, ExportJob, ExportSchemaType, exportsApi, instancesApi, migrationsApi, MigrationTenant, NetboxInstance } from "../api/client";
import { usePolling } from "../hooks/usePolling";

const FINISHED = new Set(["completed", "failed", "cancelled", "expired"]);
const errorText = (error: unknown) => error instanceof ApiError || error instanceof Error ? error.message : "Request failed";
const localTime = (value?: string | null) => value ? new Date(value).toLocaleString() : "—";

export default function ExportPage() {
  const [tab, setTab] = useState<"new" | "jobs">("new");
  const [instances, setInstances] = useState<NetboxInstance[]>([]);
  const [instanceId, setInstanceId] = useState("");
  const [tenants, setTenants] = useState<MigrationTenant[]>([]);
  const [tenantId, setTenantId] = useState(0);
  const [tenantSearch, setTenantSearch] = useState("");
  const tenantCache = useRef<Record<string, MigrationTenant[]>>({});
  const [schema, setSchema] = useState<ExportSchemaType[]>([]);
  const [schemaBusy, setSchemaBusy] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [optional, setOptional] = useState<Record<string, Set<string>>>({});
  const [custom, setCustom] = useState<Record<string, Set<string>>>({});
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [format, setFormat] = useState<"csv" | "xlsx">("csv");
  const [delimiter, setDelimiter] = useState<"," | ";">(",");
  const [jobs, setJobs] = useState<ExportJob[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [pollKey, setPollKey] = useState(0);

  useEffect(() => { instancesApi.list().then((rows) => { setInstances(rows); if (rows[0]) setInstanceId(rows[0].id); }).catch((e) => setNotice({ kind: "error", text: errorText(e) })); }, []);
  useEffect(() => {
    setTenantId(0); setSelected(new Set()); setOptional({}); setCustom({}); setSchema([]);
    if (!instanceId) return;
    const cached = tenantCache.current[instanceId];
    (cached ? Promise.resolve(cached) : migrationsApi.tenants(instanceId).then((rows) => (tenantCache.current[instanceId] = rows)))
      .then(setTenants).catch((e) => setNotice({ kind: "error", text: errorText(e) }));
    setSchemaBusy(true);
    exportsApi.schema(instanceId).then((value) => setSchema(value.object_types)).catch((e) => setNotice({ kind: "error", text: errorText(e) })).finally(() => setSchemaBusy(false));
  }, [instanceId]);

  const loadJobs = () => exportsApi.list().then(setJobs).catch((e) => setNotice({ kind: "error", text: errorText(e) }));
  useEffect(() => { if (tab === "jobs") void loadJobs(); }, [tab, pollKey]);
  usePolling({ enabled: tab === "jobs" && jobs.some((job) => !FINISHED.has(job.status)), pollKey: String(pollKey), initialValue: jobs,
    poll: exportsApi.list, isTerminal: (value) => value.every((job) => FINISHED.has(job.status)), onValue: setJobs });

  const toggle = (type: string, field: string, customField = false) => {
    const setter = customField ? setCustom : setOptional;
    setter((old) => { const next = new Set(old[type] ?? []); next.has(field) ? next.delete(field) : next.add(field); return { ...old, [type]: next }; });
  };
  const start = async () => {
    const body: ExportCreateRequest = { instance_id: instanceId, tenant_id: tenantId, object_types: [...selected], format, delimiter,
      fields: Object.fromEntries([...selected].map((key) => [key, { optional: [...(optional[key] ?? [])], custom_fields: [...(custom[key] ?? [])] }])) };
    setBusy(true); setNotice(null);
    try { await exportsApi.create(body); setNotice({ kind: "ok", text: "Export job started." }); setPollKey((value) => value + 1); setTab("jobs"); }
    catch (e) { setNotice({ kind: "error", text: errorText(e) }); } finally { setBusy(false); }
  };
  const visibleTenants = tenants.filter((tenant) => `${tenant.name} ${tenant.slug}`.toLowerCase().includes(tenantSearch.toLowerCase()));
  const remove = async (job: ExportJob) => { if (!confirm("Delete this export job and its file?")) return; await exportsApi.remove(job.id); await loadJobs(); };
  const totalRows = (job: ExportJob) => Object.values(job.row_counts).reduce((sum, value) => sum + value, 0);
  const progress = (job: ExportJob) => { const rows = Object.values(job.progress); const done = rows.reduce((sum, item) => sum + item.done, 0); const total = rows.reduce((sum, item) => sum + item.total, 0); return { done, total }; };

  return <div><h1>Data Export</h1><p className="page-subtitle">Export tenant devices and virtual systems to CSV or Excel.</p>
    <div className="tabs"><button className={tab === "new" ? "active" : ""} onClick={() => setTab("new")}>New export</button><button className={tab === "jobs" ? "active" : ""} onClick={() => setTab("jobs")}>Jobs</button></div>
    {notice && <p role="alert" style={{ color: notice.kind === "error" ? "var(--danger)" : "var(--success)" }}>{notice.text}</p>}
    {tab === "new" ? <div>
      <div className="card"><div className="form-row"><label htmlFor="export-instance">NetBox instance</label><select id="export-instance" value={instanceId} onChange={(e) => setInstanceId(e.target.value)}><option value="">Select…</option>{instances.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
        <div className="form-row"><label htmlFor="export-tenant-search">Find tenant</label><input id="export-tenant-search" value={tenantSearch} onChange={(e) => setTenantSearch(e.target.value)} placeholder="Type to search" /></div>
        <div className="form-row"><label htmlFor="export-tenant">Tenant</label><select id="export-tenant" value={tenantId} onChange={(e) => setTenantId(Number(e.target.value))}><option value={0}>Select…</option>{visibleTenants.map((tenant) => <option key={tenant.id} value={tenant.id}>{tenant.name}</option>)}</select></div>
      </div>
      <div className="card"><h2>Object types</h2>{schemaBusy ? <p>Loading fields…</p> : schema.map((type) => <label key={type.key} style={{ display: "block" }}><input type="checkbox" checked={selected.has(type.key)} onChange={(e) => setSelected((old) => { const next = new Set(old); e.target.checked ? next.add(type.key) : next.delete(type.key); return next; })} /> {type.label}</label>)}</div>
      {schema.filter((type) => selected.has(type.key)).map((type) => { const filter = (filters[type.key] ?? "").toLowerCase(); const fields = type.custom_fields.filter((field) => `${field.label} ${field.name}`.toLowerCase().includes(filter)); return <div className="card" key={type.key}><h2>{type.label}</h2>
        <h3>Always included</h3>{type.fixed.map((field) => <label key={field.key} style={{ display: "block" }}><input type="checkbox" checked disabled /> {field.label}</label>)}
        <h3>Optional fields</h3>{type.optional.map((field) => <label key={field.key} style={{ display: "block" }}><input type="checkbox" checked={optional[type.key]?.has(field.key) ?? false} onChange={() => toggle(type.key, field.key)} /> {field.label}</label>)}
        <h3>Custom fields</h3><div className="toolbar"><button onClick={() => setCustom((old) => ({ ...old, [type.key]: new Set(type.custom_fields.map((field) => field.name)) }))}>Select all</button><button onClick={() => setCustom((old) => ({ ...old, [type.key]: new Set() }))}>None</button></div>
        {type.custom_fields.length > 10 && <input aria-label={`Filter ${type.label} custom fields`} value={filters[type.key] ?? ""} onChange={(e) => setFilters({ ...filters, [type.key]: e.target.value })} placeholder="Filter custom fields" />}
        {fields.map((field) => <label key={field.name} style={{ display: "block" }}><input type="checkbox" checked={custom[type.key]?.has(field.name) ?? false} onChange={() => toggle(type.key, field.name, true)} /> {field.label} <small>({field.type})</small></label>)}</div>; })}
      <div className="card"><h2>Format</h2><label><input type="radio" checked={format === "csv"} onChange={() => setFormat("csv")} /> CSV</label> <label><input type="radio" checked={format === "xlsx"} onChange={() => setFormat("xlsx")} /> Excel</label>{format === "csv" && <div className="form-row"><label htmlFor="export-delimiter">Delimiter</label><select id="export-delimiter" value={delimiter} onChange={(e) => setDelimiter(e.target.value as "," | ";")}><option value=",">Comma</option><option value=";">Semicolon</option></select></div>}</div>
      <button className="primary" disabled={busy || !instanceId || !tenantId || selected.size === 0} onClick={start}>{busy ? "Starting…" : "Start export"}</button>
    </div> : <div className="card table-scroll"><table><thead><tr><th>Created</th><th>Instance</th><th>Tenant</th><th>Objects</th><th>Format</th><th>Status</th><th>Progress</th><th>Finished</th><th>Expires at</th><th>Actions</th></tr></thead><tbody>{jobs.map((job) => { const p = progress(job); return <tr key={job.id}><td title={job.created_at}>{localTime(job.created_at)}</td><td>{job.instance_name}</td><td>{job.tenant_name}</td><td>{job.object_types.join(", ")}</td><td>{job.format.toUpperCase()}</td><td><span className="pill" style={{ color: job.status === "completed" ? "var(--success)" : job.status === "failed" ? "var(--danger)" : "var(--accent)" }}>{job.status}</span></td><td>{job.status === "running" ? <span>{p.done}/{p.total || "?"}</span> : totalRows(job)}</td><td title={job.finished_at ?? ""}>{localTime(job.finished_at)}</td><td title={job.expires_at ?? ""}>{localTime(job.expires_at)}</td><td className="list-table-actions">{job.status === "completed" && <a className="button-link" href={exportsApi.downloadUrl(job.id)}>Download</a>} {(["queued", "running"] as string[]).includes(job.status) && <button onClick={async () => { await exportsApi.cancel(job.id); await loadJobs(); }}>Cancel</button>} {FINISHED.has(job.status) && <button className="danger" onClick={() => remove(job)}>Delete</button>}</td></tr>; })}{jobs.length === 0 && <tr><td colSpan={10}>No export jobs yet.</td></tr>}</tbody></table></div>}
  </div>;
}
