import { useEffect, useState } from "react";
import {
  accessApi, instancesApi, NetboxInstance, NetboxInstanceCreate, NetboxInstanceUpdate,
} from "../api/client";
import { useAccess } from "../contexts/AccessContext";

const EMPTY_FORM = {
  name: "", base_url: "", api_token: "", verify_ssl: true, description: "", tags: "",
  requires_approved_pr: false,
  ca_bundle_pem: "",
};

type InstanceForm = typeof EMPTY_FORM;

function formPayload(form: InstanceForm): NetboxInstanceCreate {
  return {
    name: form.name,
    base_url: form.base_url,
    api_token: form.api_token,
    verify_ssl: form.verify_ssl,
    description: form.description || undefined,
    tags: form.tags.split(",").map((tag) => tag.trim()).filter(Boolean),
    requires_approved_pr: form.requires_approved_pr,
    ...(form.ca_bundle_pem.trim() ? { ca_bundle_pem: form.ca_bundle_pem } : {}),
  };
}

export default function InstancesPage() {
  const access = useAccess();
  const [instances, setInstances] = useState<NetboxInstance[]>([]);
  const [editableIds, setEditableIds] = useState<string[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<InstanceForm>(EMPTY_FORM);
  const [testResult, setTestResult] = useState<Record<string, { ok: boolean; detail?: string; version?: string }>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testingIds, setTestingIds] = useState<string[]>([]);

  const testExisting = async (id: string) => {
    setTestingIds((current) => current.includes(id) ? current : [...current, id]);
    try {
      const result = await instancesApi.test(id);
      setTestResult((current) => ({
        ...current,
        [id]: { ok: result.ok, detail: result.detail, version: result.netbox_version },
      }));
    } catch (err) {
      setTestResult((current) => ({
        ...current,
        [id]: { ok: false, detail: err instanceof Error ? err.message : String(err) },
      }));
    } finally {
      setTestingIds((current) => current.filter((candidate) => candidate !== id));
    }
  };

  const load = () => {
    instancesApi.list().then((rows) => {
      setInstances(rows);
      setTestResult({});
      rows.forEach((instance) => { void testExisting(instance.id); });
    }).catch((err) => setError(err instanceof Error ? err.message : String(err)));
    accessApi.me().then((result) => setEditableIds(result.editable.instance));
  };
  useEffect(() => { load(); }, []);

  const resetForm = () => {
    setForm(EMPTY_FORM);
    setEditingId(null);
    setShowForm(false);
    setError(null);
  };

  const startEdit = (instance: NetboxInstance) => {
    setEditingId(instance.id);
    setForm({
      name: instance.name,
      base_url: instance.base_url,
      api_token: "",
      verify_ssl: instance.verify_ssl,
      description: instance.description ?? "",
      tags: instance.tags.join(", "),
      requires_approved_pr: instance.requires_approved_pr,
      ca_bundle_pem: "",
    });
    setError(null);
    setShowForm(true);
  };

  const handleTestForm = async () => {
    setError(null);
    try {
      const payload = formPayload(form);
      const result = await instancesApi.testNew({
        ...(editingId ? { id: editingId } : {}),
        base_url: payload.base_url,
        verify_ssl: payload.verify_ssl,
        ...(payload.ca_bundle_pem ? { ca_bundle_pem: payload.ca_bundle_pem } : {}),
        ...(payload.api_token ? { api_token: payload.api_token } : {}),
      });
      setTestResult((current) => ({
        ...current,
        __new__: { ok: result.ok, detail: result.detail, version: result.netbox_version },
      }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const payload = formPayload(form);
      if (editingId) {
        const update: NetboxInstanceUpdate = { ...payload };
        if (!update.api_token) delete update.api_token;
        await instancesApi.update(editingId, update);
      } else {
        await instancesApi.create(payload);
      }
      resetForm();
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const handleTestExisting = async (id: string) => {
    await testExisting(id);
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Remove this NetBox instance? This cannot be undone.")) return;
    await instancesApi.remove(id);
    load();
  };

  const canSave = Boolean(
    form.name.trim() && form.base_url.trim() && (editingId || form.api_token.trim()),
  );

  return (
    <div>
      <h1>NetBox Instances</h1>
      <p className="page-subtitle">Instances you can push device types to. Tokens are encrypted at rest and never shown again.</p>

      <div className="toolbar">
        {access.appAdmin ? (
          <button className="primary" onClick={() => showForm ? resetForm() : setShowForm(true)}>
            {showForm ? "Cancel" : "+ Add instance"}
          </button>
        ) : (
          <span className="pill">Creating and removing instances requires app-level admin</span>
        )}
      </div>

      {showForm && (
        <div className="card">
          <h2>{editingId ? `Edit instance — ${form.name}` : "New instance"}</h2>
          <div className="form-row">
            <label>Name</label>
            <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="prod-dc1" />
          </div>
          <div className="form-row">
            <label>Private CA certificate (PEM, optional)</label>
            <textarea className="mono" rows={6} value={form.ca_bundle_pem}
              onChange={(event) => setForm({ ...form, ca_bundle_pem: event.target.value })}
              placeholder={editingId ? "Leave empty to keep the configured CA bundle" : "-----BEGIN CERTIFICATE-----"} />
            {editingId && <div className="field-help">The stored certificate is never returned by the API.</div>}
          </div>
          <div className="form-row">
            <label>Base URL</label>
            <input value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} placeholder="https://netbox.example.com" />
          </div>
          <div className="form-row">
            <label>API Token</label>
            <input
              type="password"
              className="mono"
              value={form.api_token}
              onChange={(event) => setForm({ ...form, api_token: event.target.value })}
              placeholder={editingId ? "leave empty to keep the current token" : ""}
            />
          </div>
          <div className="form-row">
            <label>
              <input type="checkbox" style={{ width: "auto", marginRight: 6 }} checked={form.verify_ssl}
                onChange={(event) => setForm({ ...form, verify_ssl: event.target.checked })} />
              Verify SSL certificate
            </label>
          </div>
          <div className="form-row">
            <label>Description (optional)</label>
            <input value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} />
          </div>
          <div className="form-row">
            <label>Tags (comma-separated — e.g. "prod, region:eu")</label>
            <input className="mono" value={form.tags} onChange={(event) => setForm({ ...form, tags: event.target.value })} placeholder="prod, region:eu" />
          </div>
          <div className="form-row">
            <label>
              <input type="checkbox" style={{ width: "auto", marginRight: 6 }} checked={form.requires_approved_pr}
                onChange={(event) => setForm({ ...form, requires_approved_pr: event.target.checked })} />
              Require an approved, merged PR before pushing GitHub-managed data here
            </label>
          </div>
          <div className="toolbar">
            <button onClick={handleTestForm}>Test connection</button>
            <button className="primary" disabled={saving || !canSave} onClick={handleSave}>
              {saving ? "Saving…" : "Save instance"}
            </button>
            {editingId && <button onClick={resetForm}>Cancel</button>}
          </div>
          {error && <p style={{ color: "var(--danger)", fontSize: 13 }}>{error}</p>}
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
            <tr><th>Name</th><th>Base URL</th><th>Tags</th><th>SSL</th><th title="Require an approved and merged GitHub pull request before pushes to this instance">Push approval gate</th><th>Status</th><th></th></tr>
          </thead>
          <tbody>
            {instances.map((instance) => {
              const editable = access.appAdmin || editableIds.includes(instance.id);
              return (
                <tr key={instance.id}>
                  <td>{instance.name}</td>
                  <td className="mono">{instance.base_url}</td>
                  <td>{instance.tags.map((tag) => <span key={tag} className="pill" style={{ marginRight: 4 }}>{tag}</span>)}</td>
                  <td>{instance.has_ca_bundle ? "private CA" : instance.verify_ssl ? "verified" : "insecure"}</td>
                  <td>{instance.requires_approved_pr ? "🔒 required" : "—"}</td>
                  <td>
                    {testingIds.includes(instance.id) ? <span className="pill">checking…</span> : testResult[instance.id] ? (
                      <span>
                        <span className={`status-dot ${testResult[instance.id].ok ? "status-ok" : "status-error"}`} />
                        {testResult[instance.id].ok
                          ? `v${testResult[instance.id].version}`
                          : testResult[instance.id].detail ?? "unreachable"}
                      </span>
                    ) : <span className="pill">not tested</span>}
                  </td>
                  <td>
                    <button disabled={testingIds.includes(instance.id)} onClick={() => handleTestExisting(instance.id)}>Test</button>{" "}
                    {editable && <button onClick={() => startEdit(instance)}>Edit</button>}{" "}
                    {access.appAdmin && <button className="danger" onClick={() => handleDelete(instance.id)}>Remove</button>}
                  </td>
                </tr>
              );
            })}
            {instances.length === 0 && (
              <tr><td colSpan={7} style={{ color: "var(--muted)" }}>No instances yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
