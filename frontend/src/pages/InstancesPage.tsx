import { useEffect, useState } from "react";
import { instancesApi, NetboxInstance, NetboxInstanceCreate } from "../api/client";

function toCreatePayload(form: {
  name: string; base_url: string; api_token: string; verify_ssl: boolean; description: string;
  tags: string; requires_approved_pr: boolean;
}): NetboxInstanceCreate {
  return {
    name: form.name,
    base_url: form.base_url,
    api_token: form.api_token,
    verify_ssl: form.verify_ssl,
    description: form.description || undefined,
    tags: form.tags.split(",").map((t) => t.trim()).filter(Boolean),
    requires_approved_pr: form.requires_approved_pr,
  };
}

export default function InstancesPage() {
  const [instances, setInstances] = useState<NetboxInstance[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: "", base_url: "", api_token: "", verify_ssl: true, description: "", tags: "", requires_approved_pr: false });
  const [testResult, setTestResult] = useState<Record<string, { ok: boolean; detail?: string; version?: string }>>({});
  const [saving, setSaving] = useState(false);

  const load = () => instancesApi.list().then(setInstances);
  useEffect(() => { load(); }, []);

  const handleTestNew = async () => {
    const result = await instancesApi.testNew(toCreatePayload(form));
    setTestResult((r) => ({ ...r, __new__: { ok: result.ok, detail: result.detail, version: result.netbox_version } }));
  };

  const handleCreate = async () => {
    setSaving(true);
    try {
      await instancesApi.create(toCreatePayload(form));
      setForm({ name: "", base_url: "", api_token: "", verify_ssl: true, description: "", tags: "", requires_approved_pr: false });
      setShowForm(false);
      load();
    } finally {
      setSaving(false);
    }
  };

  const handleTestExisting = async (id: string) => {
    const result = await instancesApi.test(id);
    setTestResult((r) => ({ ...r, [id]: { ok: result.ok, detail: result.detail, version: result.netbox_version } }));
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Remove this NetBox instance? This cannot be undone.")) return;
    await instancesApi.remove(id);
    load();
  };

  return (
    <div>
      <h1>NetBox Instances</h1>
      <p className="page-subtitle">Instances you can push device types to. Tokens are encrypted at rest and never shown again.</p>

      <div className="toolbar">
        <button className="primary" onClick={() => setShowForm((s) => !s)}>
          {showForm ? "Cancel" : "+ Add instance"}
        </button>
      </div>

      {showForm && (
        <div className="card">
          <h2>New instance</h2>
          <div className="form-row">
            <label>Name</label>
            <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="prod-dc1" />
          </div>
          <div className="form-row">
            <label>Base URL</label>
            <input value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} placeholder="https://netbox.example.com" />
          </div>
          <div className="form-row">
            <label>API Token</label>
            <input type="password" className="mono" value={form.api_token} onChange={(e) => setForm({ ...form, api_token: e.target.value })} />
          </div>
          <div className="form-row">
            <label>
              <input type="checkbox" style={{ width: "auto", marginRight: 6 }} checked={form.verify_ssl}
                onChange={(e) => setForm({ ...form, verify_ssl: e.target.checked })} />
              Verify SSL certificate
            </label>
          </div>
          <div className="form-row">
            <label>Description (optional)</label>
            <input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </div>
          <div className="form-row">
            <label>Tags (comma-separated — e.g. "prod, region:eu")</label>
            <input className="mono" value={form.tags} onChange={(e) => setForm({ ...form, tags: e.target.value })} placeholder="prod, region:eu" />
          </div>
          <div className="form-row">
            <label>
              <input type="checkbox" style={{ width: "auto", marginRight: 6 }} checked={form.requires_approved_pr}
                onChange={(e) => setForm({ ...form, requires_approved_pr: e.target.checked })} />
              Require an approved, merged PR before pushing device types here
            </label>
          </div>
          <div className="toolbar">
            <button onClick={handleTestNew}>Test connection</button>
            <button className="primary" disabled={saving || !form.name || !form.base_url || !form.api_token} onClick={handleCreate}>
              {saving ? "Saving…" : "Save instance"}
            </button>
          </div>
          {testResult.__new__ && (
            <p style={{ fontSize: 13 }}>
              <span className={`status-dot ${testResult.__new__.ok ? "status-ok" : "status-error"}`} />
              {testResult.__new__.ok
                ? `Connected — NetBox v${testResult.__new__.version}`
                : `Failed: ${testResult.__new__.detail}`}
            </p>
          )}
        </div>
      )}

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Base URL</th>
              <th>Tags</th>
              <th>SSL</th>
              <th>Approval</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {instances.map((inst) => (
              <tr key={inst.id}>
                <td>{inst.name}</td>
                <td className="mono">{inst.base_url}</td>
                <td>{inst.tags.map((t) => <span key={t} className="pill" style={{ marginRight: 4 }}>{t}</span>)}</td>
                <td>{inst.verify_ssl ? "verified" : "insecure"}</td>
                <td>{inst.requires_approved_pr ? "🔒 required" : "—"}</td>
                <td>
                  {testResult[inst.id] ? (
                    <span>
                      <span className={`status-dot ${testResult[inst.id].ok ? "status-ok" : "status-error"}`} />
                      {testResult[inst.id].ok ? `v${testResult[inst.id].version}` : "unreachable"}
                    </span>
                  ) : (
                    <span className="pill">not tested</span>
                  )}
                </td>
                <td>
                  <button onClick={() => handleTestExisting(inst.id)}>Test</button>{" "}
                  <button className="danger" onClick={() => handleDelete(inst.id)}>Remove</button>
                </td>
              </tr>
            ))}
            {instances.length === 0 && (
              <tr><td colSpan={7} style={{ color: "var(--muted)" }}>No instances yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
