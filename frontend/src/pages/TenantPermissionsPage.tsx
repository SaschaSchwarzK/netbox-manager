import { useEffect, useMemo, useRef, useState } from "react";
import yaml from "js-yaml";
import {
  githubApi, GithubTarget, instancesApi, ManagedTenant, NetboxInstance,
  PermissionTemplateSummary, tenantPermissionsApi,
} from "../api/client";

const EMPTY_TEMPLATE = {
  name: "new-template",
  version: 1,
  description: "",
  permissions: [],
};

function message(error: any) {
  const text = error?.message ?? "Request failed";
  try {
    const body = JSON.parse(text.replace(/^\d+:\s*/, ""));
    return typeof body.detail === "string" ? body.detail : body.detail?.message ?? text;
  } catch { return text; }
}

export default function TenantPermissionsPage() {
  const [targets, setTargets] = useState<GithubTarget[]>([]);
  const [targetId, setTargetId] = useState("");
  const [instances, setInstances] = useState<NetboxInstance[]>([]);
  const [templates, setTemplates] = useState<PermissionTemplateSummary[]>([]);
  const [tenants, setTenants] = useState<ManagedTenant[]>([]);
  const [availableTenants, setAvailableTenants] = useState<{ id: number; name: string }[]>([]);
  const [tenantSearch, setTenantSearch] = useState("");
  const [tenantSearching, setTenantSearching] = useState(false);
  const [tab, setTab] = useState<"onboard" | "templates" | "managed">("onboard");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ kind: "ok" | "error" | "warning"; text: string } | null>(null);

  const [form, setForm] = useState({ instance_id: "", tenant_id: "", oidc_group_ro: "", oidc_group_rw: "", rw_template: "", ro_template: "" });
  const [editingName, setEditingName] = useState<string | undefined>();
  const [editorOpen, setEditorOpen] = useState(false);
  const [yamlText, setYamlText] = useState(yaml.dump(EMPTY_TEMPLATE, { sortKeys: false }));
  const [validation, setValidation] = useState<string | null>(null);
  const [applyPlan, setApplyPlan] = useState<{ template: string; data: any } | null>(null);
  const editorRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    githubApi.list().then((rows) => { setTargets(rows); if (rows[0]) setTargetId(rows[0].id); });
    instancesApi.list().then((rows) => { setInstances(rows); if (rows[0]) setForm((f) => ({ ...f, instance_id: rows[0].id })); });
  }, []);

  const reload = async () => {
    if (!targetId) return;
    const [templateRows, tenantRows] = await Promise.all([
      tenantPermissionsApi.templates(targetId), tenantPermissionsApi.tenants(targetId),
    ]);
    setTemplates(templateRows); setTenants(tenantRows);
    if (!form.rw_template && templateRows[0]) setForm((f) => ({ ...f, rw_template: templateRows[0].name }));
  };
  useEffect(() => { reload().catch((e) => setNotice({ kind: "error", text: message(e) })); }, [targetId]);
  useEffect(() => {
    if (!targetId || !form.instance_id) { setAvailableTenants([]); return; }
    setTenantSearching(true);
    let active = true;
    const timer = window.setTimeout(() => {
      tenantPermissionsApi.availableTenants(targetId, form.instance_id, tenantSearch).then((rows) => {
        if (active) setAvailableTenants(rows);
      }).catch((e) => { if (active) setNotice({ kind: "error", text: message(e) }); })
        .finally(() => { if (active) setTenantSearching(false); });
    }, 250);
    return () => { active = false; window.clearTimeout(timer); };
  }, [targetId, form.instance_id, tenantSearch]);

  const unscoped = useMemo(() => {
    try {
      const parsed: any = yaml.load(yamlText);
      return (parsed?.permissions ?? []).filter((p: any) => p.tenant_relation === "unsupported" && p.accept_unscoped && p.actions?.length);
    } catch { return []; }
  }, [yamlText]);

  const run = async (action: () => Promise<any>, success: string) => {
    setBusy(true); setNotice(null);
    try {
      const result = await action();
      const grants = result?.unscoped_grants ?? [];
      setNotice({ kind: grants.length ? "warning" : "ok", text: grants.length ? `${success} WARNING: ${grants.length} unscoped grant(s) applied.` : success });
      await reload();
    } catch (e) { setNotice({ kind: "error", text: message(e) }); }
    finally { setBusy(false); }
  };

  const parseEditor = () => {
    const parsed = yaml.load(yamlText);
    if (!parsed || typeof parsed !== "object") throw new Error("Template must be a YAML mapping.");
    return parsed as Record<string, any>;
  };

  const editTemplate = async (name?: string) => {
    setValidation(null); setEditingName(name); setEditorOpen(true);
    if (!name) {
      setYamlText(yaml.dump(EMPTY_TEMPLATE, { sortKeys: false }));
      window.setTimeout(() => editorRef.current?.focus(), 0);
      return;
    }
    try {
      const file = await tenantPermissionsApi.template(targetId, name);
      setYamlText(yaml.dump(file.payload, { sortKeys: false, noRefs: true }));
      window.setTimeout(() => editorRef.current?.focus(), 0);
    } catch (e) { setNotice({ kind: "error", text: message(e) }); }
  };

  const validateEditor = async () => {
    try {
      const result = await tenantPermissionsApi.validate(targetId, parseEditor());
      setYamlText(yaml.dump(result.normalized, { sortKeys: false, noRefs: true }));
      setValidation("Valid. Actions were normalized, including automatic view access where required.");
    } catch (e) { setValidation(message(e)); }
  };

  const planApply = async (template: string) => {
    setBusy(true); setNotice(null); setApplyPlan(null);
    try { setApplyPlan({ template, data: await tenantPermissionsApi.planApply(targetId, template) }); }
    catch (e) { setNotice({ kind: "error", text: message(e) }); }
    finally { setBusy(false); }
  };

  if (!targets.length) return <div><h1>Tenant Permissions</h1><p className="page-subtitle">Add a GitHub target first.</p></div>;

  return <div>
    <h1>Tenant Permissions</h1>
    <p className="page-subtitle">Manage tenant-scoped NetBox groups and permissions. Group membership remains controlled by the existing OIDC login automation.</p>
    <div className="toolbar">
      <select value={targetId} onChange={(e) => setTargetId(e.target.value)} style={{ maxWidth: 340 }}>
        {targets.map((t) => <option key={t.id} value={t.id}>{t.name} ({t.repo}@{t.branch})</option>)}
      </select>
    </div>
    {notice && <div className="card" style={{ borderColor: notice.kind === "error" ? "var(--danger)" : notice.kind === "warning" ? "var(--warning)" : "var(--success)" }}>{notice.text}</div>}
    <div className="tabs">
      <button className={tab === "onboard" ? "active" : ""} onClick={() => setTab("onboard")}>Onboard tenant</button>
      <button className={tab === "templates" ? "active" : ""} onClick={() => setTab("templates")}>Templates ({templates.length})</button>
      <button className={tab === "managed" ? "active" : ""} onClick={() => setTab("managed")}>Managed tenants ({tenants.length})</button>
    </div>

    {tab === "onboard" && <div className="card" style={{ maxWidth: 720 }}>
      <h2>Onboard tenant</h2>
      <div className="field-row-2col">
        <div className="form-row"><label>NetBox instance</label><select value={form.instance_id} onChange={(e) => { setForm({ ...form, instance_id: e.target.value, tenant_id: "" }); setTenantSearch(""); }}>{instances.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}</select></div>
        <div className="form-row">
          <label>Tenant</label>
          <input value={tenantSearch} onChange={(e) => { setTenantSearch(e.target.value); setForm({ ...form, tenant_id: "" }); }} placeholder="Search tenants by name…" />
          <select value={form.tenant_id} onChange={(e) => setForm({ ...form, tenant_id: e.target.value })} style={{ marginTop: 6 }}>
            <option value="">{tenantSearching ? "Searching…" : availableTenants.length ? "Select a matching tenant…" : "No matching tenants"}</option>
            {availableTenants.map((tenant) => <option key={tenant.id} value={tenant.id}>{tenant.name} — ID {tenant.id}</option>)}
          </select>
          <div className="field-help">Results are searched on the selected NetBox instance; at most 50 matches are shown.</div>
        </div>
        <div className="form-row"><label>OIDC read-only group (verbatim)</label><input value={form.oidc_group_ro} onChange={(e) => setForm({ ...form, oidc_group_ro: e.target.value })} /></div>
        <div className="form-row"><label>OIDC read-write group (verbatim)</label><input value={form.oidc_group_rw} onChange={(e) => setForm({ ...form, oidc_group_rw: e.target.value })} /></div>
        <div className="form-row"><label>Read-write template</label><select value={form.rw_template} onChange={(e) => setForm({ ...form, rw_template: e.target.value })}><option value="">Select…</option>{templates.map((t) => <option key={t.path} value={t.name}>{t.name} v{t.version}</option>)}</select></div>
        <div className="form-row"><label>Read-only template</label><select value={form.ro_template} onChange={(e) => setForm({ ...form, ro_template: e.target.value })}><option value="">Derive automatically from RW</option>{templates.map((t) => <option key={t.path} value={t.name}>{t.name} v{t.version}</option>)}</select></div>
      </div>
      <button className="primary" disabled={busy || !form.instance_id || !form.tenant_id || !form.oidc_group_ro || !form.oidc_group_rw || !form.rw_template}
        onClick={() => run(() => tenantPermissionsApi.onboard(targetId, { ...form, tenant_id: Number(form.tenant_id), ro_template: form.ro_template || null }), "Tenant reconciled and repository PR opened.")}>Reconcile and open PR</button>
    </div>}

    {tab === "templates" && <div className="editor-layout">
      <div>
        <div className="toolbar"><button className="primary" onClick={() => editTemplate()}>+ New custom template</button></div>
        <div className="card"><table><thead><tr><th>Name</th><th>Version</th><th>Description</th><th></th></tr></thead><tbody>
          {templates.map((t) => <tr key={t.path}><td className="mono">{t.name}</td><td>{t.version}</td><td>{t.description}</td><td className="list-table-actions"><button onClick={() => editTemplate(t.name)}>Edit</button> <button onClick={() => planApply(t.name)}>Plan fleet apply</button></td></tr>)}
          {!templates.length && <tr><td colSpan={4} style={{ color: "var(--muted)" }}>No templates in this repository.</td></tr>}
        </tbody></table></div>
      </div>
      {editorOpen && <div className="card">
        <h2>{editingName ? `Edit ${editingName}` : "New template"}</h2>
        <textarea ref={editorRef} className="yaml" value={yamlText} onChange={(e) => { setYamlText(e.target.value); setValidation(null); }} style={{ minHeight: 420 }} />
        {unscoped.length > 0 && <p style={{ color: "var(--warning)", fontWeight: 600 }}>WARNING: {unscoped.length} explicitly unscoped grant(s). These apply across all tenants.</p>}
        {validation && <p style={{ color: validation.startsWith("Valid") ? "var(--success)" : "var(--danger)", whiteSpace: "pre-wrap" }}>{validation}</p>}
        <div className="toolbar"><button onClick={validateEditor}>Validate</button><button className="primary" disabled={busy} onClick={() => run(() => tenantPermissionsApi.saveTemplate(targetId, parseEditor(), editingName), "Template saved through a pull request.")}>Validate, save, and open PR</button></div>
      </div>}
      {applyPlan && <div className="card" style={{ gridColumn: "1 / -1", borderColor: "var(--warning)" }}>
        <h2>Fleet apply plan: {applyPlan.template}</h2>
        <p>{applyPlan.data.matched} managed tenant(s) matched. Review every row before applying.</p>
        <table><thead><tr><th>Instance</th><th>Tenant ID</th><th>Status</th><th>Changes</th><th>Unscoped grants</th></tr></thead><tbody>
          {(applyPlan.data.results ?? []).map((row: any, index: number) => {
            const roles = row.plan?.stats ?? {};
            const total = (key: string) => Object.values(roles).reduce((sum: number, value: any) => sum + (value?.[key] ?? 0), 0);
            return <tr key={`${row.instance}/${row.tenant_id}/${index}`}><td>{row.instance}</td><td>{row.tenant_id}</td><td>{row.status}</td>
              <td>{row.status === "success" ? `create ${total("created")}, update ${total("updated")}, delete ${total("deleted")}` : row.error}</td>
              <td style={{ color: row.plan?.unscoped_grants?.length ? "var(--warning)" : undefined }}>{row.plan?.unscoped_grants?.length ?? 0}</td></tr>;
          })}
        </tbody></table>
        <div className="toolbar" style={{ marginTop: 12 }}><button onClick={() => setApplyPlan(null)}>Cancel</button><button className="primary" disabled={busy || (applyPlan.data.results ?? []).some((row: any) => row.status !== "success")} onClick={() => {
          if (confirm(`Apply template ${applyPlan.template} to ${applyPlan.data.matched} managed tenant(s)?`)) {
            run(() => tenantPermissionsApi.apply(targetId, applyPlan.template), `Applied ${applyPlan.template} fleet-wide.`).then(() => setApplyPlan(null));
          }
        }}>Confirm fleet apply</button></div>
      </div>}
    </div>}

    {tab === "managed" && <div className="card"><table><thead><tr><th>Instance</th><th>Tenant</th><th>RO group</th><th>RW group</th><th>Template</th><th></th></tr></thead><tbody>
      {tenants.map((t) => <tr key={`${t.instance}/${t.tenant_id}`}><td>{t.instance}</td><td>{t.tenant_name} <span className="pill">ID {t.tenant_id}</span></td><td>{t.metadata.oidc_group_ro}</td><td>{t.metadata.oidc_group_rw}</td><td>{t.metadata.rw_template?.name} v{t.metadata.rw_template?.version}</td><td><button className="danger" disabled={busy} onClick={async () => {
        const force = confirm("Decommission this tenant? The first attempt refuses if either group has members. Select OK to continue without force.");
        if (!force) return;
        await run(async () => {
          try { return await tenantPermissionsApi.decommission(targetId, t.instance_id, t.tenant_id, false); }
          catch (e: any) {
            let code: string | undefined;
            try { code = JSON.parse((e?.message ?? "").replace(/^\d+:\s*/, "")).detail?.code; } catch { /* not a structured guard error */ }
            if (code !== "group_has_members" || !confirm(`${message(e)}\n\nForce deletion anyway?`)) throw e;
            return tenantPermissionsApi.decommission(targetId, t.instance_id, t.tenant_id, true);
          }
        }, "Tenant groups deleted and repository cleanup PR opened.");
      }}>Decommission</button></td></tr>)}
      {!tenants.length && <tr><td colSpan={6} style={{ color: "var(--muted)" }}>No managed tenants.</td></tr>}
    </tbody></table></div>}
  </div>;
}
