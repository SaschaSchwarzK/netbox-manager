import { useEffect, useRef, useState } from "react";
import {
  ApiError, instancesApi, migrationsApi, MigrationJobSummary, MigrationMappingOverride,
  MigrationMappingSkeletonRow, MigrationTargetOption, MigrationTenant, MigrationType, NetboxInstance, MigrationPreflightResult,
} from "../api/client";
import { usePolling } from "../hooks/usePolling";

type ConflictPolicy = "skip" | "update" | "update_empty_only";

function message(error: any) {
  return error instanceof ApiError ? error.message : error?.message ?? "Request failed";
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
  if (status === "planning" || status === "running" || status === "rolling_back") return "var(--accent)";
  return "var(--muted)";
}

function targetOptionText(option: MigrationTargetOption) {
  return option.label === `#${option.id}` ? option.label : `${option.label} (#${option.id})`;
}

function MigrationReport({ jobId, nonce }: { jobId: string; nonce: number }) {
  const [html, setHtml] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setHtml("");
    setError("");
    migrationsApi.reportHtml(jobId).then((content) => {
      if (active) setHtml(content);
    }).catch((reason) => {
      if (active) setError(message(reason));
    });
    return () => { active = false; };
  }, [jobId, nonce]);

  if (error) return <p role="alert" style={{ color: "var(--danger)" }}>Could not load report: {error}</p>;
  if (!html) return <p className="field-help">Loading report…</p>;
  return <iframe
    title="Migration report"
    srcDoc={html}
    sandbox="allow-popups"
    style={{ width: "100%", height: 480, border: "1px solid var(--border)", borderRadius: "var(--radius)", background: "white" }}
  />;
}

const TYPE_SECTION_ORDER: Record<string, string[]> = {
  DCIM: ["Organization", "Racks", "Devices", "Device Components", "Connections", "Power"],
  IPAM: ["IP Addresses", "VLANs", "Services", "ASN and RIR"],
  VIRTUALIZATION: ["Clusters", "Virtual Machines"],
  CIRCUITS: ["Providers", "Circuits"],
  TENANCY: ["Tenants"],
  VPN: ["L2VPN"],
  EXTRAS: ["Configuration"],
};

function migrationTypeSection(type: string): string {
  const [namespace, model] = type.split(".");
  if (namespace === "dcim") {
    if (["region", "sitegroup", "site", "location"].includes(model)) return "Organization";
    if (["rackrole", "rack"].includes(model)) return "Racks";
    if (["manufacturer", "devicetype", "moduletype", "devicerole", "platform", "device",
      "module", "inventoryitem", "virtualchassis", "virtualdevicecontext"].includes(model)) return "Devices";
    if (["devicebay", "modulebay"].includes(model)) return "Device Components";
    if (["interface", "consoleport", "consoleserverport", "powerport", "poweroutlet",
      "frontport", "rearport", "macaddress", "cable"].includes(model)) return "Connections";
    if (["powerpanel", "powerfeed"].includes(model)) return "Power";
  }
  if (namespace === "ipam") {
    if (["ipaddress", "iprange", "prefix", "aggregate", "vrf", "role"].includes(model)) return "IP Addresses";
    if (["vlan", "vlangroup", "vlantranslationpolicy", "vlantranslationrule"].includes(model)) return "VLANs";
    if (["service", "fhrpgroup", "fhrpgroupassignment"].includes(model)) return "Services";
    if (["asn", "asnrange", "rir"].includes(model)) return "ASN and RIR";
  }
  if (namespace === "virtualization") return model.startsWith("cluster") ? "Clusters" : "Virtual Machines";
  if (namespace === "circuits") return model.startsWith("provider") ? "Providers" : "Circuits";
  if (namespace === "tenancy") return "Tenants";
  if (namespace === "vpn") return "L2VPN";
  if (namespace === "extras") return "Configuration";
  return "Other";
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
  const [maxBatchSize, setMaxBatchSize] = useState(100);
  const [selectedTypes, setSelectedTypes] = useState<Set<string>>(new Set());
  const [conflictDefault, setConflictDefault] = useState<ConflictPolicy>("skip");
  const [conflictOverrides, setConflictOverrides] = useState<Record<string, ConflictPolicy>>({});

  const [job, setJob] = useState<MigrationJobSummary | null>(null);
  const [skeleton, setSkeleton] = useState<MigrationMappingSkeletonRow[]>([]);
  const [mappingOverrides, setMappingOverrides] = useState<Record<string, MigrationMappingOverride>>({});
  const [mappingDirty, setMappingDirty] = useState(false);
  const [targetOptions, setTargetOptions] = useState<Record<string, MigrationTargetOption[]>>({});
  const [targetOptionsBusy, setTargetOptionsBusy] = useState<Record<string, boolean>>({});
  const [mappingSearch, setMappingSearch] = useState("");
  const [manualType, setManualType] = useState("");
  const [manualSourceId, setManualSourceId] = useState("");
  const [manualTargetId, setManualTargetId] = useState("");
  const [manualAction, setManualAction] = useState<MigrationMappingOverride["action"]>("map");
  const [preflight, setPreflight] = useState<MigrationPreflightResult | null>(null);
  const [preflightBusy, setPreflightBusy] = useState(false);
  const [confirmRun, setConfirmRun] = useState(false);
  const [reportNonce, setReportNonce] = useState(0);
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
  usePolling({
    enabled: !!job && !TERMINAL.includes(job.status),
    pollKey: job?.id ?? "",
    initialValue: job as MigrationJobSummary,
    poll: () => migrationsApi.job(job!.id),
    isTerminal: (updated) => TERMINAL.includes(updated.status),
    onValue: (updated, previous) => {
      setJob(updated);
      if (updated.status !== previous.status) {
        setReportNonce((n) => n + 1);
        if (previous.status === "planning" && updated.status === "planned") setMappingDirty(false);
      }
    },
  });

  const typeByKey = new Map(types.map((type) => [type.type, type]));
  const requiredBy = new Map<string, string[]>();
  selectedTypes.forEach((selectedType) => {
    typeByKey.get(selectedType)?.required_selectable_dependencies.forEach((dependency) => {
      const dependents = requiredBy.get(dependency) ?? [];
      dependents.push(selectedType);
      requiredBy.set(dependency, dependents);
    });
  });
  const autoIncludedTypes = new Set(requiredBy.keys());

  const toggleType = (type: string) => {
    const next = new Set(selectedTypes);
    if (next.has(type)) {
      next.delete(type);
      const dependents = requiredBy.get(type);
      if (dependents?.length) {
        const label = typeByKey.get(type)?.label ?? type;
        const dependentLabels = dependents.map((key) => typeByKey.get(key)?.label ?? key);
        setNotice({ kind: "warning", text: `${label} remains selected because it is required by ${dependentLabels.join(", ")}.` });
      }
    } else {
      next.add(type);
    }
    setSelectedTypes(next);
  };

  const resetWizard = () => {
    setJob(null); setSkeleton([]); setMappingOverrides({}); setMappingDirty(false); setTargetOptions({}); setMappingSearch(""); setPreflight(null); setConfirmRun(false); setNotice(null); setSelectedTypes(new Set()); setConflictOverrides({});
  };

  useEffect(() => {
    if (!job || job.status === "planning") { setSkeleton([]); return; }
    migrationsApi.mappingSkeleton(job.id).then(setSkeleton).catch((e) => setNotice({ kind: "error", text: message(e) }));
  }, [job?.id, job?.status]);

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
        include_untenanted: includeUntenanted, marker_tag: markerTag,
        max_requests_per_second: maxRps, max_batch_size: maxBatchSize,
        fail_fast: failFast,
        conflict_policy,
        mapping_overrides: overrides,
        job_id: reuseJobId,
      });
      if (!reuseJobId) setMappingDirty(false);
      setJob(result); setReportNonce((n) => n + 1);
    } catch (e) { setNotice({ kind: "error", text: message(e) }); }
    finally { setBusy(false); }
  };

  const ambiguousCount = job ? totalsFor(job, "ambiguous") : 0;

  const typeGroups = types.reduce<Record<string, Record<string, MigrationType[]>>>((groups, type) => {
    const namespace = type.type.split(".")[0].toUpperCase();
    const section = migrationTypeSection(type.type);
    ((groups[namespace] ??= {})[section] ??= []).push(type);
    return groups;
  }, {});

  const visibleSkeleton = skeleton.filter((row) => {
    const query = mappingSearch.trim().toLowerCase();
    return !query || `${row.override_key} ${row.source_natural_key} ${row.target_natural_key ?? ""} ${row.auto_match}`.toLowerCase().includes(query);
  });
  const mappingObjectTypes = Array.from(new Set(skeleton.map((row) => row.object_type))).sort();

  const loadTargetOptions = async (objectType: string) => {
    if (!job || !objectType || targetOptions[objectType] || targetOptionsBusy[objectType]) return;
    setTargetOptionsBusy((previous) => ({ ...previous, [objectType]: true }));
    try {
      const rows = await migrationsApi.targetOptions(job.id, objectType);
      setTargetOptions((previous) => ({ ...previous, [objectType]: rows.filter((row) => row.id > 0) }));
    } catch (e) {
      setNotice({ kind: "error", text: message(e) });
    } finally {
      setTargetOptionsBusy((previous) => ({ ...previous, [objectType]: false }));
    }
  };

  const markMappingDirty = () => {
    setMappingDirty(true);
    setConfirmRun(false);
  };

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
    markMappingDirty();
    setManualSourceId("");
    setManualTargetId("");
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
      "This is best-effort only. Mapped and updated objects will NOT be restored or changed, patches on surviving objects will remain, and the marker tag definitions will be retained."
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

  const jobControls = job && <div className="card">
    {job.status === "planned" && <>
      <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
        <input type="checkbox" checked={confirmRun} onChange={(e) => setConfirmRun(e.target.checked)} style={{ width: "auto" }} />
        I've reviewed the plan below and want to write these changes to the target instance.
      </label>
      <div className="toolbar">
        <button onClick={resetWizard}>Start over</button>
        <button disabled={busy || !mappingDirty} onClick={() => planMigration(job.id)}>Re-plan mapping changes</button>
        <button className="primary" disabled={busy || mappingDirty || !confirmRun || ambiguousCount > 0} onClick={runMigration}>
          Run migration
        </button>
      </div>
    </>}

    {(job.status === "running" || job.status === "rolling_back") && <div className="toolbar">
      <span className="pill">Status: {job.status}</span>
      <strong>{job.current_step ?? "Working…"}</strong>
      {job.status === "running" && <button className="danger" onClick={cancelMigration}>Cancel</button>}
    </div>}

    {TERMINAL.includes(job.status) && <div className="toolbar">
      <span className="pill" style={{ color: statusColor(job.status) }}>Status: {job.status}</span>
      {job.status === "completed_with_errors" && <button disabled={busy} onClick={retryFailed}>Retry failed objects</button>}
      {ROLLBACK_ELIGIBLE.includes(job.status) && (job.status !== "failed" || job.started_at) && <button className="danger" disabled={busy} onClick={rollbackMigration}>Rollback created objects</button>}
      <button onClick={resetWizard}>Start a new migration</button>
    </div>}
  </div>;

  return <div>
    <h1>Data Migration</h1>
    <p className="page-subtitle">
      Migrate devices, virtual machines, interfaces, and IPAM data from one NetBox instance to another.
      Existing sites, device types, and other reference objects are mapped automatically where possible to avoid duplicates.
    </p>

    <div className="card migration-disclaimer" role="alert">
      <strong>Development software — use at your own risk.</strong>
      <p>
        NetBox Manager is still under active development, and data migration is still under testing.
        Migration writes directly to the target and rollback cannot restore updated objects.
      </p>
      <p>
        Before continuing, create and verify current backups of the source and target NetBox instances,
        confirm that you can restore them, and test the migration in a non-production environment.
      </p>
    </div>

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
            <label>Maximum create batch size (0 disables)</label>
            <input type="number" min={0} max={100} value={maxBatchSize}
              onChange={(e) => setMaxBatchSize(Number(e.target.value))} />
          </div>
          <div className="form-row">
            <label><input type="checkbox" checked={failFast} onChange={(e) => setFailFast(e.target.checked)} style={{ width: "auto", marginRight: 6 }} />Stop on first API error</label>
          </div>
        </div>
      </div>

      <div className="card">
        <h2>2. Data to migrate</h2>
        <p className="field-help" style={{ marginTop: -6, marginBottom: 12 }}>
          Reference objects (sites, device types, manufacturers, …) can be migrated independently.
          They are still included automatically when required by other selected data.
        </p>
        <div className="migration-type-groups">
          {Object.entries(typeGroups).map(([namespace, sections]) => (
            <section className="migration-type-group" key={namespace}>
              <h3>{namespace}</h3>
              <div className="migration-type-sections">
              {[...(TYPE_SECTION_ORDER[namespace] ?? []),
                ...Object.keys(sections).filter((name) => !(TYPE_SECTION_ORDER[namespace] ?? []).includes(name)),
              ].filter((name) => sections[name]?.length).map((sectionName) => (
                <section className="migration-type-section" key={sectionName}>
                <h4>{sectionName}</h4>
                <div className="content-type-grid">
                {sections[sectionName].map((t) => {
                  const manuallySelected = selectedTypes.has(t.type);
                  const autoIncluded = autoIncludedTypes.has(t.type);
                  const selected = manuallySelected || autoIncluded;
                  const dependentLabels = (requiredBy.get(t.type) ?? []).map((key) => typeByKey.get(key)?.label ?? key);
                  const optionalLabels = t.possible_optional_selectable_dependencies.map((key) => typeByKey.get(key)?.label ?? key);
                  return <div className={`migration-type-option${autoIncluded && !manuallySelected ? " dependency-required" : ""}`} key={t.type}>
                    <label>
                      <input type="checkbox" checked={selected} disabled={autoIncluded && !manuallySelected} onChange={() => toggleType(t.type)} />
                      <span>{t.label}</span>
                    </label>
                    {autoIncluded && <div className="migration-dependency-note" title={`Required by: ${dependentLabels.join(", ")}`}>
                      Required by: {dependentLabels.join(", ")}
                    </div>}
                    {selected && optionalLabels.length > 0 && <div className="migration-optional-note">
                      May also include: {optionalLabels.join(", ")} (if referenced)
                    </div>}
                    {manuallySelected && <select aria-label={`${t.label} conflict policy override`}
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
      {jobControls}
      <div className="card">
        <h2>Plan summary</h2>
        {(job.status === "planning" || job.status === "running" || job.status === "rolling_back") && (
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
            <progress aria-label={`${job.status} in progress`} style={{ width: 72 }} />
            <strong>{job.current_step ?? "Working…"}</strong>
          </div>
        )}
        {job.status !== "planning" && <table>
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
        </table>}
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

      {job.status !== "planning" && <><div className="card">
        <h2>Mapping review</h2>
        <div className="mapping-toolbar">
          <input value={mappingSearch} onChange={(e) => setMappingSearch(e.target.value)} placeholder="Search type, source ID, name or status" />
          <span className="field-help">{visibleSkeleton.length} of {skeleton.length} planned objects</span>
        </div>
        {!skeleton.length && <p className="field-help">No planned objects are available for mapping review.</p>}
        {!!visibleSkeleton.length && <table>
          <thead><tr><th>Source</th><th>Matched target</th><th>Action</th><th>Manual target</th></tr></thead>
          <tbody>{visibleSkeleton.map((row) => {
            const override = mappingOverrides[row.override_key];
            const choices = [...(targetOptions[row.object_type] ?? [])];
            if (row.target_id && row.target_id > 0 && !choices.some((choice) => choice.id === row.target_id)) {
              choices.unshift({ id: row.target_id, label: row.target_natural_key ?? `#${row.target_id}` });
            }
            return <tr key={row.override_key} style={row.auto_match === "ambiguous" ? { color: "var(--danger)" } : undefined}>
              <td>{row.object_type} #{row.source_id} ({row.source_natural_key})<br /><small>{row.match_detail}</small></td>
              <td>{row.target_id ? <>
                <strong>{row.target_natural_key ?? `#${row.target_id}`}</strong><br />
                <small>{row.auto_match} · target ID #{row.target_id}</small>
              </> : row.auto_match}</td>
              <td><select value={override?.action ?? ""} onChange={(e) => {
                const action = e.target.value as MigrationMappingOverride["action"] | "";
                const next = { ...mappingOverrides };
                if (!action) delete next[row.override_key];
                else next[row.override_key] = {
                  action,
                  target_id: action === "map" ? (override?.target_id ?? (row.target_id && row.target_id > 0 ? row.target_id : null)) : null,
                };
                setMappingOverrides(next);
                markMappingDirty();
                if (action === "map") void loadTargetOptions(row.object_type);
              }}><option value="">Review…</option><option value="map">Map to existing</option><option value="skip">Skip</option><option value="create">Create new</option></select></td>
              <td>{override?.action === "map" && <select
                value={override.target_id && override.target_id > 0 ? override.target_id : ""}
                onFocus={() => void loadTargetOptions(row.object_type)}
                onChange={(e) => {
                const target_id = Number(e.target.value) || null;
                const next = { ...mappingOverrides, [row.override_key]: { ...override, target_id } };
                setMappingOverrides(next);
                markMappingDirty();
              }}>
                <option value="">{targetOptionsBusy[row.object_type] ? "Loading targets…" : "Choose target…"}</option>
                {choices.filter((choice) => choice.id > 0).map((choice) => (
                  <option key={choice.id} value={choice.id}>{targetOptionText(choice)}</option>
                ))}
              </select>}</td>
            </tr>;
          })}</tbody>
        </table>}
        {!!skeleton.length && !visibleSkeleton.length && <p className="field-help">No mapping rows match the search.</p>}
        <div className="manual-mapping-editor">
          <strong>Add manual mapping</strong>
          <select value={manualType} onChange={(e) => {
            setManualType(e.target.value); setManualTargetId("");
            if (e.target.value) void loadTargetOptions(e.target.value);
          }}>
            <option value="">Choose object type…</option>
            {mappingObjectTypes.map((type) => <option key={type} value={type}>{type}</option>)}
          </select>
          <input value={manualSourceId} onChange={(e) => setManualSourceId(e.target.value)} placeholder="Source ID" inputMode="numeric" />
          <select value={manualAction} onChange={(e) => setManualAction(e.target.value as MigrationMappingOverride["action"])}>
            <option value="map">Map to existing</option>
            <option value="skip">Skip</option>
            <option value="create">Create new</option>
          </select>
          {manualAction === "map" && <select value={manualTargetId} onFocus={() => void loadTargetOptions(manualType)} onChange={(e) => setManualTargetId(e.target.value)}>
            <option value="">{targetOptionsBusy[manualType] ? "Loading targets…" : "Choose target…"}</option>
            {(targetOptions[manualType] ?? []).filter((choice) => choice.id > 0).map((choice) => (
              <option key={choice.id} value={choice.id}>{targetOptionText(choice)}</option>
            ))}
          </select>}
          <button disabled={busy} onClick={addManualMapping}>Add mapping</button>
        </div>
        {mappingDirty && <p style={{ color: "var(--warning)", fontWeight: 600 }}>
          Mapping changes are pending. Re-plan to apply them before starting the migration.
        </p>}
      </div>

      <div className="card">
        <div className="mapping-toolbar">
          <h2 style={{ margin: 0 }}>Detailed report {job.status === "running" && <span className="pill">snapshot</span>}</h2>
          <button onClick={() => setReportNonce((n) => n + 1)}>Refresh report</button>
          <button onClick={() => window.open(migrationsApi.reportUrl(job.id), "_blank")}>Open report</button>
          <a className="button-link" href={migrationsApi.reportDownloadUrl(job.id)}>Download report</a>
        </div>
        <MigrationReport jobId={job.id} nonce={reportNonce} />
      </div>

      </>}
    </>}
  </div>;
}
