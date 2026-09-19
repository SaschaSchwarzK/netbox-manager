import { useEffect, useState } from "react";
import { githubApi, GithubTarget } from "../api/client";

export default function GithubTargetsPage() {
  const [targets, setTargets] = useState<GithubTarget[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({
    name: "", repo: "", branch: "main",
    path_pattern: "device-types/{manufacturer}/{slug}.yml",
    custom_fields_path: "custom-fields/template.yml",
    pat: "",
  });
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState<Record<string, { ok: boolean; detail?: string }>>({});

  const load = () => githubApi.list().then(setTargets);
  useEffect(() => { load(); }, []);

  const handleTestNew = async () => {
    const result = await githubApi.testNew(form);
    setTestResult((r) => ({ ...r, __new__: result }));
  };

  const handleTestExisting = async (id: string) => {
    const result = await githubApi.test(id);
    setTestResult((r) => ({ ...r, [id]: result }));
  };

  const handleCreate = async () => {
    setSaving(true);
    try {
      await githubApi.create(form);
      setForm({
        name: "", repo: "", branch: "main",
        path_pattern: "device-types/{manufacturer}/{slug}.yml",
        custom_fields_path: "custom-fields/template.yml",
        pat: "",
      });
      setShowForm(false);
      load();
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Remove this GitHub target?")) return;
    await githubApi.remove(id);
    load();
  };

  return (
    <div>
      <h1>GitHub Targets</h1>
      <p className="page-subtitle">Repos device-type YAML can be committed to, e.g. a fork of netbox-community/devicetype-library.</p>

      <div className="toolbar">
        <button className="primary" onClick={() => setShowForm((s) => !s)}>
          {showForm ? "Cancel" : "+ Add target"}
        </button>
      </div>

      {showForm && (
        <div className="card">
          <h2>New target</h2>
          <div className="form-row">
            <label>Name</label>
            <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="internal-library" />
          </div>
          <div className="form-row">
            <label>Repository (org/repo)</label>
            <input value={form.repo} onChange={(e) => setForm({ ...form, repo: e.target.value })} placeholder="myorg/devicetype-library" />
          </div>
          <div className="form-row">
            <label>Branch</label>
            <input value={form.branch} onChange={(e) => setForm({ ...form, branch: e.target.value })} />
          </div>
          <div className="form-row">
            <label>Device-type path pattern</label>
            <input className="mono" value={form.path_pattern} onChange={(e) => setForm({ ...form, path_pattern: e.target.value })} />
          </div>
          <div className="form-row">
            <label>Custom-fields template path</label>
            <input className="mono" value={form.custom_fields_path} onChange={(e) => setForm({ ...form, custom_fields_path: e.target.value })} />
          </div>
          <div className="form-row">
            <label>Personal Access Token (repo scope)</label>
            <input type="password" className="mono" value={form.pat} onChange={(e) => setForm({ ...form, pat: e.target.value })} />
          </div>
          <button className="primary" disabled={saving || !form.name || !form.repo || !form.pat} onClick={handleCreate}>
            {saving ? "Saving…" : "Save target"}
          </button>
          {" "}
          <button onClick={handleTestNew} disabled={!form.repo || !form.pat}>Test connection</button>
          {testResult.__new__ && (
            <p style={{ fontSize: 13 }}>
              <span className={`status-dot ${testResult.__new__.ok ? "status-ok" : "status-error"}`} />
              {testResult.__new__.detail}
            </p>
          )}
        </div>
      )}

      <div className="card">
        <table>
          <thead>
            <tr><th>Name</th><th>Repo</th><th>Branch</th><th>Device-type path</th><th>Custom-fields path</th><th>Status</th><th></th></tr>
          </thead>
          <tbody>
            {targets.map((t) => (
              <tr key={t.id}>
                <td>{t.name}</td>
                <td className="mono">{t.repo}</td>
                <td>{t.branch}</td>
                <td className="mono">{t.path_pattern}</td>
                <td className="mono">{t.custom_fields_path}</td>
                <td>
                  {testResult[t.id] ? (
                    <span>
                      <span className={`status-dot ${testResult[t.id].ok ? "status-ok" : "status-error"}`} />
                      {testResult[t.id].ok ? "ok" : "failed"}
                    </span>
                  ) : (
                    <span className="pill">not tested</span>
                  )}
                </td>
                <td>
                  <button onClick={() => handleTestExisting(t.id)}>Test</button>{" "}
                  <button className="danger" onClick={() => handleDelete(t.id)}>Remove</button>
                </td>
              </tr>
            ))}
            {targets.length === 0 && (
              <tr><td colSpan={7} style={{ color: "var(--muted)" }}>No GitHub targets yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
