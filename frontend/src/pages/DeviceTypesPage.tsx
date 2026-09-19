import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import yaml from "js-yaml";
import { deviceTypesApi, DeviceTypeSummary, githubApi, GithubTarget, CoverageEntry } from "../api/client";

const STATUS_LABEL: Record<string, string> = {
  in_sync: "in sync", drift: "drift", missing: "missing", error: "error",
};
const STATUS_COLOR: Record<string, string> = {
  in_sync: "var(--success)", drift: "var(--warning)", missing: "var(--danger)", error: "var(--danger)",
};

export default function DeviceTypesPage() {
  const [targets, setTargets] = useState<GithubTarget[]>([]);
  const [selectedTarget, setSelectedTarget] = useState("");
  const [files, setFiles] = useState<DeviceTypeSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [showNewForm, setShowNewForm] = useState(false);
  const [newForm, setNewForm] = useState({ manufacturer: "", model: "", slug: "" });
  const [importError, setImportError] = useState<string | null>(null);
  const [coverage, setCoverage] = useState<Record<string, CoverageEntry[] | "loading" | "error">>({});
  const fileInput = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  useEffect(() => {
    githubApi.list().then((t) => {
      setTargets(t);
      if (t.length > 0) setSelectedTarget(t[0].id);
    });
  }, []);

  const loadFiles = () => {
    if (!selectedTarget) return;
    setLoading(true);
    setCoverage({});
    deviceTypesApi.list(selectedTarget).then(setFiles).finally(() => setLoading(false));
  };
  useEffect(() => { loadFiles(); }, [selectedTarget]);

  const openEditor = (path: string) => {
    navigate(`/device-types/${selectedTarget}/edit?path=${encodeURIComponent(path)}`);
  };

  // "New" and "Import" only stage a draft in memory (router state) — nothing
  // is written to GitHub until the user hits Save in the editor.
  const openNewDraft = (payload: Record<string, any>) => {
    navigate(`/device-types/${selectedTarget}/edit`, { state: { draftPayload: payload } });
  };

  const handleCreateScratch = () => {
    openNewDraft({ ...newForm, is_full_depth: true, u_height: 1 });
  };

  const handleImportFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setImportError(null);
    try {
      const text = await file.text();
      const parsed = yaml.load(text) as Record<string, any>;
      if (!parsed || typeof parsed !== "object" || !parsed.manufacturer || !parsed.model || !parsed.slug) {
        throw new Error("File is missing manufacturer/model/slug — is this a device-type YAML file?");
      }
      openNewDraft(parsed);
    } catch (err: any) {
      setImportError(err.message ?? "Could not parse that file as YAML.");
    } finally {
      e.target.value = "";
    }
  };

  const checkCoverage = async (path: string) => {
    setCoverage((c) => ({ ...c, [path]: "loading" }));
    try {
      const entries = await deviceTypesApi.coverage(selectedTarget, path);
      setCoverage((c) => ({ ...c, [path]: entries }));
    } catch {
      setCoverage((c) => ({ ...c, [path]: "error" }));
    }
  };

  if (targets.length === 0) {
    return (
      <div>
        <h1>Device Types</h1>
        <p className="page-subtitle">
          Device types are stored as YAML files in a GitHub repo — add a repo under
          "GitHub Targets" first, then come back here.
        </p>
      </div>
    );
  }

  return (
    <div>
      <h1>Device Types</h1>
      <p className="page-subtitle">Device-type YAML files live directly in the selected repo — every save is a commit.</p>

      <div className="toolbar">
        <select value={selectedTarget} onChange={(e) => setSelectedTarget(e.target.value)} style={{ maxWidth: 280 }}>
          {targets.map((t) => <option key={t.id} value={t.id}>{t.name} ({t.repo}@{t.branch})</option>)}
        </select>
        <button className="primary" onClick={() => setShowNewForm((s) => !s)}>+ New from scratch</button>
        <button onClick={() => fileInput.current?.click()}>Import YAML file</button>
        <input ref={fileInput} type="file" accept=".yml,.yaml" style={{ display: "none" }} onChange={handleImportFile} />
      </div>
      {importError && <p style={{ color: "var(--danger)", fontSize: 13 }}>{importError}</p>}

      {showNewForm && (
        <div className="card">
          <h2>New device type</h2>
          <p style={{ color: "var(--muted)", fontSize: 13, marginTop: -4 }}>
            This opens the editor with nothing committed yet — it's saved to GitHub only when you click Save there.
          </p>
          <div className="form-row">
            <label>Manufacturer</label>
            <input value={newForm.manufacturer} onChange={(e) => setNewForm({ ...newForm, manufacturer: e.target.value })} placeholder="Cisco" />
          </div>
          <div className="form-row">
            <label>Model</label>
            <input value={newForm.model} onChange={(e) => setNewForm({ ...newForm, model: e.target.value })} placeholder="Catalyst 9300-48P" />
          </div>
          <div className="form-row">
            <label>Slug</label>
            <input className="mono" value={newForm.slug} onChange={(e) => setNewForm({ ...newForm, slug: e.target.value })} placeholder="catalyst-9300-48p" />
          </div>
          <button className="primary" disabled={!newForm.manufacturer || !newForm.model || !newForm.slug} onClick={handleCreateScratch}>
            Open in editor
          </button>
        </div>
      )}

      <div className="card">
        <table>
          <thead>
            <tr><th>Manufacturer</th><th>Model</th><th>Slug</th><th>Path</th><th>Coverage (which instances have it)</th></tr>
          </thead>
          <tbody>
            {files.map((f) => {
              const cov = coverage[f.path];
              return (
                <tr key={f.path}>
                  <td>{f.manufacturer ?? "—"}</td>
                  <td><a onClick={() => openEditor(f.path)} style={{ cursor: "pointer", color: "var(--accent)" }}>{f.model ?? f.path}</a></td>
                  <td className="mono">{f.slug ?? "—"}</td>
                  <td className="mono" style={{ color: "var(--muted)" }}>{f.path}</td>
                  <td>
                    {!cov && <button onClick={() => checkCoverage(f.path)} style={{ padding: "2px 8px", fontSize: 12 }}>Check coverage</button>}
                    {cov === "loading" && <span style={{ color: "var(--muted)", fontSize: 12 }}>Checking…</span>}
                    {cov === "error" && <span style={{ color: "var(--danger)", fontSize: 12 }}>Failed to check</span>}
                    {Array.isArray(cov) && (
                      <span style={{ fontSize: 12 }}>
                        {cov.length === 0 ? (
                          <span style={{ color: "var(--muted)" }}>No instances configured</span>
                        ) : (
                          cov.map((entry) => (
                            <span key={entry.instance_id} className="pill" style={{ marginRight: 4, color: STATUS_COLOR[entry.status] }} title={entry.error ?? undefined}>
                              {entry.instance_name}: {STATUS_LABEL[entry.status] ?? entry.status}
                            </span>
                          ))
                        )}
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
            {!loading && files.length === 0 && (
              <tr><td colSpan={5} style={{ color: "var(--muted)" }}>No device types in this repo yet.</td></tr>
            )}
            {loading && (
              <tr><td colSpan={5} style={{ color: "var(--muted)" }}>Loading from GitHub…</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
