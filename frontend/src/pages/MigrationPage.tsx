import { useEffect, useRef, useState } from "react";
import {
  instancesApi, migrationsApi, MigrationJobSummary, MigrationMappingOverride,
  MigrationMappingSkeletonRow, MigrationTenant, MigrationType, NetboxInstance, MigrationPreflightResult,
} from "../api/client";

type ConflictPolicy = "skip" | "update" | "update_empty_only";

function message(error: any) {
  const text = error?.message ?? "Request failed";
  try {
    const body = JSON.parse(text.replace(/^\d+:\s*/, ""));
    return typeof body.detail === "string" ? body.detail : body.detail?.message ?? text;
  } catch { return text; }
}

const TERMINAL: MigrationJobSummary["status"][] = [
  "completed", "completed_with_errors", "failed", "cancelled", "rolled_back", "rolled_back_with_errors",
];
const ROLLBACK_ELIGIBLE: MigrationJobSummary["status"][] = ["completed", "completed_with_errors", "failed", "cancelled"];

function totalsFor(job: MigrationJobSummary, action: string): number {
  return Object.values(job.totals).reduce((sum, row) => sum + (row[action] ?? 0), 0);
}

function statusColor(status: MigrationJobSummary["status"]) {
  if (status === "completed") return "var(--success)";
  if (status === "completed_with_errors" || status === "failed") return "var(--danger)";
  if (status === "rolled_back_with_errors") return "var(--danger)";
  if (status === "rolled_back") return "var(--success)";
  if (status === "running" || status === "rolling_back") return "var(--accent)";
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
  const [tenantFilter, setTenantFilter] = useState<string[]>([]);
  const [tenants, setTenants] = useState<MigrationTenant[]>([]);
  const [tenantsBusy, setTenantsBusy] = useState(false);
  const [includeUntenanted, setIncludeUntenanted] = useState(false);
  const [markerTag, setMarkerTag] = useState(true);
  const [failFast, setFailFast] = useState(false);
  const [maxRps, setMaxRps] = useState(4);
  const [selectedTypes, setSelectedTypes] = useState<Set<string>>(new Set());
  const [conflictDefault, setConflictDefault] = useState<ConflictPolicy>("skip");
  const [conflictOverrides, setConflictOverrides] = useState<Record<string, ConflictPolicy>>({});

  const [job, setJob] = useState<MigrationJobSummary | null>(null);
  const [skeleton, setSkeleton] = useState<MigrationMappingSkeletonRow[]>([]);
  const [mappingOverrides, setMappingOverrides] = useState<Record<string, MigrationMappingOverride>>({});
  const [mappingSearch, setMappingSearch] = useState("");
  const [manualType, setManualType] = useState("");
  const [manualSourceId, setManualSourceId] = useState("");
  const [manualTargetId, setManualTargetId] = useState("");
  const [manualAction, setManualAction] = useState<MigrationMappingOverride["action"]>("map");
  const [preflight, setPreflight] = useState<MigrationPreflightResult | null>(null);
  const [preflightBusy, setPreflightBusy] = useState(false);
  const [confirmRun, setConfirmRun] = useState(false);
  const [reportNonce, setReportNonce] = useState(0);
  const pollRef = useRef<number | null>(null);
  const tenantCacheRef = useRef<Record<string, MigrationTenant[]>>({});

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

  useEffect(() => {
    setTenantFilter([]);
    if (!sourceId) { setTenants([]); return; }
    const cached = tenantCacheRef.current[sourceId];
    if (cached) { setTenants(cached); return; }
    let active = true;
    setTenantsBusy(true);
    migrationsApi.tenants(sourceId).then((rows) => {
      tenantCacheRef.current[sourceId] = rows;
      if (active) setTenants(rows);
    }).catch((e) => {
      if (active) { setTenants([]); setNotice({ kind: "error", text: message(e) }); }
    }).finally(() => { if (active) setTenantsBusy(false); });
    return () => { active = false; };
  }, [sourceId]);

  // Poll summary counts while a job runs. The report itself stays stable so it can be read;
  // users can refresh it explicitly, and it reloads once on a status transition.
  useEffect(() => {
    if (!job || TERMINAL.includes(job.status)) {
      if (pollRef.current) window.clearInterval(pollRef.current);
      return;
    }
    pollRef.current = window.setInterval(async () => {
      try {
        const updated = await migrationsApi.job(job.id);
        setJob(updated);
        if (updated.status !== job.status) setReportNonce((n) => n + 1);
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
    setJob(null); setSkeleton([]); setMappingOverrides({}); setMappingSearch(""); setPreflight(null); setConfirmRun(false); setNotice(null); setSelectedTypes(new Set()); setConflictOverrides({});
  };

  useEffect(() => {
    if (!job) { setSkeleton([]); return; }
    migrationsApi.mappingSkeleton(job.id).then(setSkeleton).catch((e) => setNotice({ kind: "error", text: message(e) }));
  }, [job?.id]);

  const runPreflight = async () => {
    if (!sourceId || !targetId) return;
    setPreflightBusy(true); setPreflight(null);
    try { setPreflight(await migrationsApi.preflight(sourceId, targetId)); }
    catch (e) { setNotice({ kind: "error", text: message(e) }); }
    finally { setPreflightBusy(false); }
  };

  const planMigration = async (reuseJobId?: string, overrides = mappingOverrides) => {
    if (!sourceId || !targetId) { setNotice({ kind: "error", text: "Select a source and target instance." }); return; }
    if (sourceId === targetId) { setNotice({ kind: "error", text: "Source and target must be different instances." }); return; }
    if (!selectedTypes.size) { setNotice({ kind: "error", text: "Select at least one data type to migrate." }); return; }
    setBusy(true); setNotice(null);
    try {
      const conflict_policy: Record<string, string> = { default: conflictDefault };
      selectedTypes.forEach((type) => {
        const override = conflictOverrides[type];
        if (override && override !== conflictDefault) conflict_policy[type] = override;
      });
      const result = await migrationsApi.plan({
        source_instance_id: sourceId, target_instance_id: targetId,
        selected_types: Array.from(selectedTypes), tenant_filter: tenantFilter,
        include_untenanted: includeUntenanted, marker_tag: markerTag, max_requests_per_second: maxRps,
        fail_fast: failFast,
        conflict_policy,
        mapping_overrides: overrides,
        job_id: reuseJobId,
      });
      setJob(result); setReportNonce((n) => n + 1);
    } catch (e) { setNotice({ kind: "error", text: message(e) }); }
    finally { setBusy(false); }
  };

  const ambiguousCount = job ? totalsFor(job, "ambiguous") : 0;

  const typeGroups = types.reduce<Record<string, MigrationType[]>>((groups, type) => {
    const namespace = type.type.split(".")[0].toUpperCase();
    (groups[namespace] ??= []).push(type);
    return groups;
  }, {});

  const visibleSkeleton = skeleton.filter((row) => {
    const query = mappingSearch.trim().toLowerCase();
    return !query || `${row.override_key} ${row.source_natural_key} ${row.auto_match}`.toLowerCase().includes(query);
  });

  const addManualMapping = () => {
    const type = manualType.trim();
    const sourceId = Number(manualSourceId);
    const targetId = Number(manualTargetId);
    if (!type || !Number.isInteger(sourceId) || sourceId <= 0) {
      setNotice({ kind: "error", text: "Enter a valid object type and source ID." });
      return;
    }
    if (manualAction === "map" && (!Number.isInteger(targetId) || targetId <= 0)) {
      setNotice({ kind: "error", text: "A map action requires a valid target ID." });
      return;
    }
    const next = {
      ...mappingOverrides,
      [`${type}:${sourceId}`]: { action: manualAction, target_id: manualAction === "map" ? targetId : null },
    };
    setMappingOverrides(next);
    if (job) void planMigration(job.id, next);
  };

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

  const rollbackMigration = async () => {
    if (!job || !confirm(
      "Delete every object successfully created by this migration?\n\n" +
      "This is best-effort only. Mapped and updated objects will NOT be restored or changed, patches on surviving objects will remain, and the marker tag definition will be retained."
    )) return;
    setBusy(true); setNotice(null);
    try {
      const result = await migrationsApi.rollback(job.id);
      setJob(result.job); setReportNonce((n) => n + 1);
      setNotice({
        kind: result.failed ? "warning" : "ok",
        text: `${result.detail} Deleted ${result.deleted}; failed ${result.failed}; untouched mapped ${result.untouched_mapped}; untouched updated ${result.untouched_updated}.`,
      });
    } catch (e) { setNotice({ kind: "error", text: message(e) }); }
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
        <button onClick={runPreflight} disabled={preflightBusy || !sourceId || !targetId}>
          {preflightBusy ? "Testing…" : "Test connection"}
        </button>
        {preflight && <div style={{ marginTop: 10 }}>
          {(["source", "target"] as const).map((side) => {
            const result = preflight[side];
            return <div key={side}><span className={`status-dot ${result.token_valid ? "status-ok" : "status-error"}`} />
              {side === "source" ? "Source" : "Target"}: {result.token_valid ? `NetBox ${result.netbox_version ?? "unknown"}` : result.detail ?? "Connection failed"}
            </div>;
          })}
          <div className="field-help">Target write permission is not probed because no safe read-only permission check is available.</div>
        </div>}
        <div className="form-row">
          <label>Tenant filter (optional — leave empty to migrate all tenants)</label>
          <select multiple size={Math.min(Math.max(tenants.length, 3), 7)} value={tenantFilter} disabled={tenantsBusy}
            onChange={(e) => setTenantFilter(Array.from(e.target.selectedOptions, (option) => option.value))}>
            {tenants.map((tenant) => <option key={tenant.id} value={tenant.slug}>{tenant.name} — {tenant.slug}</option>)}
          </select>
          <div className="field-help">{tenantsBusy ? "Loading source tenants…" : `${tenants.length} source tenant(s) available. Hold Ctrl/Cmd to select multiple.`}</div>
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
          <div className="form-row">
            <label><input type="checkbox" checked={failFast} onChange={(e) => setFailFast(e.target.checked)} style={{ width: "auto", marginRight: 6 }} />Stop on first API error</label>
          </div>
        </div>
      </div>

      <div className="card">
        <h2>2. Data to migrate</h2>
        <p className="field-help" style={{ marginTop: -6, marginBottom: 12 }}>
          Required reference objects (sites, device types, manufacturers, …) are included automatically and don't need to be selected here.
        </p>
        <div className="migration-type-groups">
          {Object.entries(typeGroups).map(([namespace, group]) => (
            <section className="migration-type-group" key={namespace}>
              <h3>{namespace}</h3>
              <div className="content-type-grid">
                {group.map((t) => {
                  const selected = selectedTypes.has(t.type);
                  return <div className="migration-type-option" key={t.type} title={t.dependencies.length ? `Requires: ${t.dependencies.join(", ")}` : undefined}>
                    <label>
                      <input type="checkbox" checked={selected} onChange={() => toggleType(t.type)} />
                      <span>{t.label}</span>
                    </label>
                    {selected && <select aria-label={`${t.label} conflict policy override`}
                      value={conflictOverrides[t.type] ?? ""}
                      onChange={(e) => setConflictOverrides((previous) => {
                        const next = { ...previous };
                        const value = e.target.value as ConflictPolicy | "";
                        if (value) next[t.type] = value; else delete next[t.type];
                        return next;
                      })}>
                      <option value="">Use global default</option>
                      <option value="skip">Leave unchanged</option>
                      <option value="update">Update from source</option>
                      <option value="update_empty_only">Update empty fields only</option>
                    </select>}
                  </div>;
                })}
              </div>
            </section>
          ))}
        </div>
      </div>

      <div className="card" style={{ maxWidth: 420 }}>
        <h2>3. Conflict policy</h2>
        <div className="form-row">
          <label>When an object already exists on the target (matched by name/slug/etc.)</label>
          <select value={conflictDefault} onChange={(e) => setConflictDefault(e.target.value as ConflictPolicy)}>
            <option value="skip">Leave it unchanged (map only)</option>
            <option value="update">Update it from the source</option>
            <option value="update_empty_only">Update empty fields only</option>
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
          <thead><tr><th>Type</th><th>Create</th><th>Update</th><th>Map</th><th>Skip</th><th>Ambiguous</th><th>Errors</th><th>Rolled back</th><th>Rollback errors</th></tr></thead>
          <tbody>
            {Object.entries(job.totals).map(([type, counts]) => (
              <tr key={type}>
                <td>{types.find((t) => t.type === type)?.label ?? type}</td>
                <td>{counts.create ?? 0}</td>
                <td>{counts.update ?? 0}</td>
                <td>{counts.map ?? 0}</td>
                <td>{counts.skip ?? 0}</td>
                <td style={{ color: counts.ambiguous ? "var(--danger)" : undefined }}>{counts.ambiguous ?? 0}</td>
                <td style={{ color: counts.error ? "var(--danger)" : undefined }}>{counts.error ?? 0}</td>
                <td>{counts.rolled_back ?? 0}</td>
                <td style={{ color: counts.rollback_error ? "var(--danger)" : undefined }}>{counts.rollback_error ?? 0}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {job.status !== "planned" && <div className="field-help" style={{ marginTop: 8 }}>Counts update as objects finish; pending objects are not included.</div>}
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
        <h2>Mapping review</h2>
        <div className="mapping-toolbar">
          <input value={mappingSearch} onChange={(e) => setMappingSearch(e.target.value)} placeholder="Search type, source ID, name or status" />
          <span className="field-help">{visibleSkeleton.length} of {skeleton.length} planned objects</span>
        </div>
        {!skeleton.length && <p className="field-help">No planned objects are available for mapping review.</p>}
        {!!visibleSkeleton.length && <table>
          <thead><tr><th>Source</th><th>Auto-match</th><th>Action</th><th>Target ID</th></tr></thead>
          <tbody>{visibleSkeleton.map((row) => {
            const override = mappingOverrides[row.override_key];
            return <tr key={row.override_key} style={row.auto_match === "ambiguous" ? { color: "var(--danger)" } : undefined}>
              <td>{row.object_type} #{row.source_id} ({row.source_natural_key})<br /><small>{row.match_detail}</small></td>
              <td>{row.auto_match}{row.target_id ? ` -> ${row.target_id}` : ""}</td>
              <td><select value={override?.action ?? ""} onChange={(e) => {
                const action = e.target.value as MigrationMappingOverride["action"];
                const next = { ...mappingOverrides, [row.override_key]: { action, target_id: action === "map" ? (override?.target_id ?? row.target_id ?? null) : null } };
                setMappingOverrides(next);
                if (action !== "map" || next[row.override_key].target_id != null) void planMigration(job.id, next);
              }}><option value="">Review…</option><option value="map">Map to existing</option><option value="skip">Skip</option><option value="create">Create new</option></select></td>
              <td>{override?.action === "map" && <input type="number" value={override.target_id ?? ""} onChange={(e) => {
                const target_id = Number(e.target.value) || null;
                const next = { ...mappingOverrides, [row.override_key]: { ...override, target_id } };
                setMappingOverrides(next);
                if (target_id != null) void planMigration(job.id, next);
              }} />}</td>
            </tr>;
          })}</tbody>
        </table>}
        {!!skeleton.length && !visibleSkeleton.length && <p className="field-help">No mapping rows match the search.</p>}
        <div className="manual-mapping-editor">
          <strong>Add manual mapping</strong>
          <input value={manualType} onChange={(e) => setManualType(e.target.value)} placeholder="type.key" />
          <input value={manualSourceId} onChange={(e) => setManualSourceId(e.target.value)} placeholder="Source ID" inputMode="numeric" />
          <select value={manualAction} onChange={(e) => setManualAction(e.target.value as MigrationMappingOverride["action"])}>
            <option value="map">Map to existing</option>
            <option value="skip">Skip</option>
            <option value="create">Create new</option>
          </select>
          {manualAction === "map" && <input value={manualTargetId} onChange={(e) => setManualTargetId(e.target.value)} placeholder="Target ID" inputMode="numeric" />}
          <button disabled={busy} onClick={addManualMapping}>Add and re-plan</button>
        </div>
      </div>

      <div className="card">
        <div className="mapping-toolbar">
          <h2 style={{ margin: 0 }}>Detailed report {job.status === "running" && <span className="pill">snapshot</span>}</h2>
          <button onClick={() => setReportNonce((n) => n + 1)}>Refresh report</button>
          <button onClick={() => window.open(migrationsApi.reportUrl(job.id), "_blank")}>Open report</button>
          <a className="button-link" href={migrationsApi.reportDownloadUrl(job.id)}>Download report</a>
        </div>
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

        {(job.status === "running" || job.status === "rolling_back") && <div className="toolbar">
          <span className="pill">Status: {job.status}{job.status === "running" ? ` (${job.phase} phase)` : ""}</span>
          {job.status === "running" &&
          <button className="danger" onClick={cancelMigration}>Cancel</button>
          }
        </div>}

        {TERMINAL.includes(job.status) && <div className="toolbar">
          <span className="pill" style={{ color: statusColor(job.status) }}>Status: {job.status}</span>
          {job.status === "completed_with_errors" && <button disabled={busy} onClick={retryFailed}>Retry failed objects</button>}
          {ROLLBACK_ELIGIBLE.includes(job.status) && <button className="danger" disabled={busy} onClick={rollbackMigration}>Rollback created objects</button>}
          <button onClick={resetWizard}>Start a new migration</button>
        </div>}
      </div>
    </>}
  </div>;
}
