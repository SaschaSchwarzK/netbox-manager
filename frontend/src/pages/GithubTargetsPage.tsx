import { useEffect, useState } from "react";
import { accessApi, githubApi, GithubTarget, GithubTargetCreate, GithubTargetUpdate } from "../api/client";
import { useAccess } from "../contexts/AccessContext";

const EMPTY_FORM = {
  name: "", repo: "", branch: "main",
  path_pattern: "device-types/{manufacturer}/{slug}.yml",
  custom_fields_path: "custom-fields/template.yml",
  pat: "",
};

type TargetForm = typeof EMPTY_FORM;

export default function GithubTargetsPage() {
  const access = useAccess();
  const [targets, setTargets] = useState<GithubTarget[]>([]);
  const [editableIds, setEditableIds] = useState<string[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<TargetForm>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, { ok: boolean; detail?: string }>>({});

  const load = () => {
    githubApi.list().then(setTargets);
    accessApi.me().then((result) => setEditableIds(result.editable.github_target));
  };
  useEffect(() => { load(); }, []);

  const resetForm = () => {
    setForm(EMPTY_FORM);
    setEditingId(null);
    setShowForm(false);
    setError(null);
  };

  const startEdit = (target: GithubTarget) => {
    setEditingId(target.id);
    setForm({
      name: target.name,
      repo: target.repo,
      branch: target.branch,
      path_pattern: target.path_pattern,
      custom_fields_path: target.custom_fields_path,
      pat: "",
    });
    setError(null);
    setShowForm(true);
  };

  const handleTestForm = async () => {
    setError(null);
    try {
      const result = await githubApi.testNew({
        ...(editingId ? { id: editingId } : {}),
        repo: form.repo,
        branch: form.branch,
        ...(form.pat ? { pat: form.pat } : {}),
      });
      setTestResult((current) => ({ ...current, __new__: result }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      if (editingId) {
        const update: GithubTargetUpdate = { ...form };
        if (!update.pat) delete update.pat;
        await githubApi.update(editingId, update);
      } else {
        await githubApi.create(form as GithubTargetCreate);
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
    const result = await githubApi.test(id);
    setTestResult((current) => ({ ...current, [id]: result }));
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Remove this GitHub target?")) return;
    await githubApi.remove(id);
    load();
  };

  const canSave = Boolean(form.name.trim() && form.repo.trim() && form.branch.trim() && (editingId || form.pat.trim()));

  return (
    <div>
      <h1>GitHub Targets</h1>
      <p className="page-subtitle">Repos device-type YAML can be committed to, e.g. a fork of netbox-community/devicetype-library.</p>

      <div className="toolbar">
        {access.appAdmin ? (
          <button className="primary" onClick={() => showForm ? resetForm() : setShowForm(true)}>
            {showForm ? "Cancel" : "+ Add target"}
          </button>
        ) : (
          <span className="pill">Creating and removing targets requires app-level admin</span>
        )}
      </div>

      {showForm && (
        <div className="card">
          <h2>{editingId ? `Edit target — ${form.name}` : "New target"}</h2>
          <div className="form-row">
            <label>Name</label>
            <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="internal-library" />
          </div>
          <div className="form-row">
            <label>Repository (org/repo)</label>
            <input value={form.repo} onChange={(event) => setForm({ ...form, repo: event.target.value })} placeholder="myorg/devicetype-library" />
          </div>
          <div className="form-row">
            <label>Branch</label>
            <input value={form.branch} onChange={(event) => setForm({ ...form, branch: event.target.value })} />
          </div>
          <div className="form-row">
            <label>Device-type path pattern</label>
            <input className="mono" value={form.path_pattern} onChange={(event) => setForm({ ...form, path_pattern: event.target.value })} />
          </div>
          <div className="form-row">
            <label>Custom-fields template path</label>
            <input className="mono" value={form.custom_fields_path} onChange={(event) => setForm({ ...form, custom_fields_path: event.target.value })} />
          </div>
          <div className="form-row">
            <label>Personal Access Token (repo scope)</label>
            <input
              type="password"
              className="mono"
              value={form.pat}
              onChange={(event) => setForm({ ...form, pat: event.target.value })}
              placeholder={editingId ? "leave empty to keep the current PAT" : ""}
            />
          </div>
          {editingId && (
            <p style={{ color: "var(--muted)", fontSize: 12 }}>
              Changing the repo, branch, or paths changes where YAML is read and written; existing files are not moved.
            </p>
          )}
          <div className="toolbar">
            <button className="primary" disabled={saving || !canSave} onClick={handleSave}>
              {saving ? "Saving…" : "Save target"}
            </button>
            <button onClick={handleTestForm} disabled={!form.repo || !form.branch || (!editingId && !form.pat)}>
              Test connection
            </button>
            {editingId && <button onClick={resetForm}>Cancel</button>}
          </div>
          {error && <p style={{ color: "var(--danger)", fontSize: 13 }}>{error}</p>}
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
            {targets.map((target) => {
              const editable = access.appAdmin || editableIds.includes(target.id);
              return (
                <tr key={target.id}>
                  <td>{target.name}</td>
                  <td className="mono">{target.repo}</td>
                  <td>{target.branch}</td>
                  <td className="mono">{target.path_pattern}</td>
                  <td className="mono">{target.custom_fields_path}</td>
                  <td>
                    {testResult[target.id] ? (
                      <span>
                        <span className={`status-dot ${testResult[target.id].ok ? "status-ok" : "status-error"}`} />
                        {testResult[target.id].ok ? "ok" : "failed"}
                      </span>
                    ) : <span className="pill">not tested</span>}
                  </td>
                  <td>
                    <button onClick={() => handleTestExisting(target.id)}>Test</button>{" "}
                    {editable && <button onClick={() => startEdit(target)}>Edit</button>}{" "}
                    {access.appAdmin && <button className="danger" onClick={() => handleDelete(target.id)}>Remove</button>}
                  </td>
                </tr>
              );
            })}
            {targets.length === 0 && (
              <tr><td colSpan={7} style={{ color: "var(--muted)" }}>No GitHub targets yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
