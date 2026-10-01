import { useEffect, useRef, useState } from "react";
import {
  instancesApi, migrationsApi, MigrationJobSummary, MigrationType, NetboxInstance,
} from "../api/client";

function message(error: any) {
  const text = error?.message ?? "Request failed";
  try {
    const body = JSON.parse(text.replace(/^\d+:\s*/, ""));
    return typeof body.detail === "string" ? body.detail : body.detail?.message ?? text;
  } catch { return text; }
}

const TERMINAL: MigrationJobSummary["status"][] = ["completed", "completed_with_errors", "failed", "cancelled"];

function totalsFor(job: MigrationJobSummary, action: string): number {
  return Object.values(job.totals).reduce((sum, row) => sum + (row[action] ?? 0), 0);
}

function statusColor(status: MigrationJobSummary["status"]) {
  if (status === "completed") return "var(--success)";
  if (status === "completed_with_errors" || status === "failed") return "var(--danger)";
  if (status === "running") return "var(--accent)";
  return "var(--muted)";
}

export default function MigrationPage() {
  const [tab, setTab] = useState<"new" | "history">("new");
  const [instances, setInstances] = useState<NetboxInstance[]>([]);
  const [types, setTypes] = useState<MigrationType[]>([]);
  const [jobs, setJobs] = useState<MigrationJobSummary[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ kind: "ok" | "error" | "warning"; text: string } | null>(null);

  const [sourceId, setSourceId] = useState("");
  const [targetId, setTargetId] = useState("");
  const [tenantFilter, setTenantFilter] = useState("");
  const [includeUntenanted, setIncludeUntenanted] = useState(false);
  const [markerTag, setMarkerTag] = useState(true);
  const [maxRps, setMaxRps] = useState(4);
  const [selectedTypes, setSelectedTypes] = useState<Set<string>>(new Set());
  const [conflictDefault, setConflictDefault] = useState<"skip" | "update">("skip");

  const [job, setJob] = useState<MigrationJobSummary | null>(null);
  const [confirmRun, setConfirmRun] = useState(false);
  const [reportNonce, setReportNonce] = useState(0);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    instancesApi.list().then((rows) => {
      setInstances(rows);
      if (rows[0]) setSourceId(rows[0].id);
      if (rows[1]) setTargetId(rows[1].id);
    });
    migrationsApi.types().then(setTypes);
  }, []);

  const loadJobs = () => migrationsApi.jobs().then(setJobs).catch((e) => setNotice({ kind: "error", text: message(e) }));
  useEffect(() => { if (tab === "history") loadJobs(); }, [tab]);

  // Poll the active job while it's running, and bump reportNonce so the embedded report
  // iframe reloads and shows live per-object progress (execution_status is read fresh from
  // the DB on every report request — see services/migration/report.py).
  useEffect(() => {
    if (!job || TERMINAL.includes(job.status)) {
      if (pollRef.current) window.clearInterval(pollRef.current);
      return;
    }
    pollRef.current = window.setInterval(async () => {
      try {
        const updated = await migrationsApi.job(job.id);
        setJob(updated);
        setReportNonce((n) => n + 1);
      } catch { /* transient — next tick will retry */ }
    }, 2000);
    return () => { if (pollRef.current) window.clearInterval(pollRef.current); };
  }, [job?.id, job?.status]);

  const toggleType = (type: string) => {
    setSelectedTypes((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type); else next.add(type);
      return next;
    });
  };

  const resetWizard = () => {
    setJob(null); setConfirmRun(false); setNotice(null); setSelectedTypes(new Set());
  };

  const planMigration = async (reuseJobId?: string) => {
    if (!sourceId || !targetId) { setNotice({ kind: "error", text: "Select a source and target instance." }); return; }
    if (sourceId === targetId) { setNotice({ kind: "error", text: "Source and target must be different instances." }); return; }
    if (!selectedTypes.size) { setNotice({ kind: "error", text: "Select at least one data type to migrate." }); return; }
    setBusy(true); setNotice(null);
    try {
      const tenants = tenantFilter.split(",").map((t) => t.trim()).filter(Boolean);
      const result = await migrationsApi.plan({
        source_instance_id: sourceId, target_instance_id: targetId,
        selected_types: Array.from(selectedTypes), tenant_filter: tenants,
        include_untenanted: includeUntenanted, marker_tag: markerTag, max_requests_per_second: maxRps,
        conflict_policy: { default: conflictDefault },
        job_id: reuseJobId,
      });
      setJob(result); setReportNonce((n) => n + 1);
    } catch (e) { setNotice({ kind: "error", text: message(e) }); }
    finally { setBusy(false); }
  };

  const ambiguousCount = job ? totalsFor(job, "ambiguous") : 0;

  const runMigration = async () => {
    if (!job) return;
    setBusy(true); setNotice(null);
    try {
      const updated = await migrationsApi.execute(job.id);
      setJob(updated); setReportNonce((n) => n + 1);
    } catch (e) { setNotice({ kind: "error", text: message(e) }); }
    finally { setBusy(false); }
  };

  const cancelMigration = async () => {
    if (!job) return;
    try { setJob(await migrationsApi.cancel(job.id)); } catch (e) { setNotice({ kind: "error", text: message(e) }); }
  };

  const retryFailed = async () => {
    if (!job) return;
    setBusy(true);
    try { setJob(await migrationsApi.retryFailed(job.id)); setReportNonce((n) => n + 1); }
    catch (e) { setNotice({ kind: "error", text: message(e) }); }
    finally { setBusy(false); }
  };

  return <div>
    <h1>Data Migration</h1>
    <p className="page-subtitle">
      Migrate devices, virtual machines, interfaces, and IPAM data from one NetBox instance to another.
      Existing sites, device types, and other reference objects are mapped automatically where possible to avoid duplicates.
    </p>

    <div className="tabs">
      <button className={tab === "new" ? "active" : ""} onClick={() => setTab("new")}>New migration</button>
      <button className={tab === "history" ? "active" : ""} onClick={() => setTab("history")}>History ({jobs.length || ""})</button>
    </div>

    {notice && (
      <div className="card" style={{ borderColor: notice.kind === "error" ? "var(--danger)" : notice.kind === "warning" ? "var(--warning)" : "var(--success)" }}>
        {notice.text}
      </div>
    )}

    {tab === "history" && <div className="card">
      <table>
        <thead><tr><th>Source</th><th>Target</th><th>Status</th><th>Created</th><th>Actor</th><th></th></tr></thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.id}>
              <td>{j.source_instance_name ?? j.source_instance_id}</td>
              <td>{j.target_instance_name ?? j.target_instance_id}</td>
              <td><span className="status-dot" style={{ background: statusColor(j.status) }} />{j.status}</td>
              <td>{new Date(j.created_at).toLocaleString()}</td>
              <td>{j.actor_name ?? "—"}</td>
              <td className="list-table-actions">
                <button onClick={() => window.open(migrationsApi.reportUrl(j.id), "_blank")}>View report</button>
              </td>
            </tr>
          ))}
          {!jobs.length && <tr><td colSpan={6} style={{ color: "var(--muted)" }}>No migrations yet.</td></tr>}
        </tbody>
      </table>
    </div>}

    {tab === "new" && !job && <>
      <div className="card" style={{ maxWidth: 720 }}>
        <h2>1. Source and target</h2>
        <div className="field-row-2col">
          <div className="form-row">
            <label>Source instance (read-only)</label>
            <select value={sourceId} onChange={(e) => setSourceId(e.target.value)}>
              {instances.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
            </select>
          </div>
          <div className="form-row">
            <label>Target instance</label>
            <select value={targetId} onChange={(e) => setTargetId(e.target.value)}>
              {instances.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
            </select>
          </div>
        </div>
        <div className="form-row">
          <label>Tenant filter (comma-separated slugs, optional — leave blank to migrate all tenants)</label>
          <input value={tenantFilter} onChange={(e) => setTenantFilter(e.target.value)} placeholder="acme, globex" />
          <div className="field-help">Devices, VMs, IPAM data, and their components are scoped to these tenants. Reference objects (sites, device types, …) are only included if actually referenced.</div>
        </div>
        <div className="field-row-3col">
          <div className="form-row">
            <label><input type="checkbox" checked={includeUntenanted} onChange={(e) => setIncludeUntenanted(e.target.checked)} style={{ width: "auto", marginRight: 6 }} />Also include untenanted objects</label>
          </div>
          <div className="form-row">
            <label><input type="checkbox" checked={markerTag} onChange={(e) => setMarkerTag(e.target.checked)} style={{ width: "auto", marginRight: 6 }} />Tag migrated objects</label>
          </div>
          <div className="form-row">
            <label>Max requests/sec (per instance)</label>
            <input type="number" min={0.5} step={0.5} value={maxRps} onChange={(e) => setMaxRps(Number(e.target.value))} />
          </div>
        </div>
      </div>

      <div className="card">
        <h2>2. Data to migrate</h2>
        <p className="field-help" style={{ marginTop: -6, marginBottom: 12 }}>
          Required reference objects (sites, device types, manufacturers, …) are included automatically and don't need to be selected here.
        </p>
        <div className="content-type-grid" style={{ gridTemplateColumns: "repeat(3, 1fr)", maxHeight: 280 }}>
          {types.map((t) => (
            <label key={t.type} title={t.dependencies.length ? `Requires: ${t.dependencies.join(", ")}` : undefined}>
              <input type="checkbox" checked={selectedTypes.has(t.type)} onChange={() => toggleType(t.type)} />
              {t.label}
            </label>
          ))}
        </div>
      </div>

      <div className="card" style={{ maxWidth: 420 }}>
        <h2>3. Conflict policy</h2>
        <div className="form-row">
          <label>When an object already exists on the target (matched by name/slug/etc.)</label>
          <select value={conflictDefault} onChange={(e) => setConflictDefault(e.target.value as any)}>
            <option value="skip">Leave it unchanged (map only)</option>
            <option value="update">Update it from the source</option>
          </select>
          <div className="field-help">Objects you explicitly map yourself are never changed, regardless of this setting.</div>
        </div>
      </div>

      <div className="toolbar">
        <button className="primary" disabled={busy} onClick={() => planMigration()}>
          {busy ? "Planning…" : "Preview migration (dry run)"}
        </button>
      </div>
    </>}

    {tab === "new" && job && <>
      <div className="card">
        <h2>Plan summary</h2>
        <table>
          <thead><tr><th>Type</th><th>Create</th><th>Update</th><th>Map</th><th>Skip</th><th>Ambiguous</th></tr></thead>
          <tbody>
            {Object.entries(job.totals).map(([type, counts]) => (
              <tr key={type}>
                <td>{types.find((t) => t.type === type)?.label ?? type}</td>
                <td>{counts.create ?? 0}</td>
                <td>{counts.update ?? 0}</td>
                <td>{counts.map ?? 0}</td>
                <td>{counts.skip ?? 0}</td>
                <td style={{ color: counts.ambiguous ? "var(--danger)" : undefined }}>{counts.ambiguous ?? 0}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {job.warnings.length > 0 && (
          <div style={{ marginTop: 14 }}>
            <div className="modal-section-title">Warnings ({job.warnings.length})</div>
            <ul style={{ color: "var(--warning)", fontSize: 13, margin: 0, paddingLeft: 18 }}>
              {job.warnings.slice(0, 20).map((w, i) => <li key={i}>{w}</li>)}
            </ul>
          </div>
        )}
        {ambiguousCount > 0 && (
          <p style={{ color: "var(--danger)", fontWeight: 600 }}>
            {ambiguousCount} object(s) have an ambiguous match and need manual mapping before this migration can run.
            See the detailed report below for which objects and why.
          </p>
        )}
      </div>

      <div className="card">
        <h2>Detailed report {job.status === "running" && <span className="pill">live</span>}</h2>
        <iframe
          key={reportNonce}
          src={`${migrationsApi.reportUrl(job.id)}${reportNonce ? `?_=${reportNonce}` : ""}`}
          style={{ width: "100%", height: 480, border: "1px solid var(--border)", borderRadius: "var(--radius)", background: "white" }}
        />
      </div>

      <div className="card">
        {job.status === "planned" && <>
          <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
            <input type="checkbox" checked={confirmRun} onChange={(e) => setConfirmRun(e.target.checked)} style={{ width: "auto" }} />
            I've reviewed the plan above and want to write these changes to the target instance.
          </label>
          <div className="toolbar">
            <button onClick={resetWizard}>Start over</button>
            <button disabled={busy} onClick={() => planMigration(job.id)}>Re-plan</button>
            <button className="primary" disabled={busy || !confirmRun || ambiguousCount > 0} onClick={runMigration}>
              Run migration
            </button>
          </div>
        </>}

        {job.status === "running" && <div className="toolbar">
          <span className="pill">Status: running ({job.phase} phase)</span>
          <button className="danger" onClick={cancelMigration}>Cancel</button>
        </div>}

        {TERMINAL.includes(job.status) && <div className="toolbar">
          <span className="pill" style={{ color: statusColor(job.status) }}>Status: {job.status}</span>
          {job.status === "completed_with_errors" && <button disabled={busy} onClick={retryFailed}>Retry failed objects</button>}
          <button onClick={resetWizard}>Start a new migration</button>
        </div>}
      </div>
    </>}
  </div>;
}
