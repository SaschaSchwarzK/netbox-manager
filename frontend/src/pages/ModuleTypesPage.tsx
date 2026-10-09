import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import * as yaml from "js-yaml";
import { CoverageEntry, githubApi, GithubTarget, moduleTypesApi, ModuleTypeSummary } from "../api/client";

const STATUS_COLOR: Record<string, string> = {
  in_sync: "var(--success)", drift: "var(--warning)", missing: "var(--danger)", error: "var(--danger)",
};

export default function ModuleTypesPage() {
  const navigate = useNavigate();
  const [targets, setTargets] = useState<GithubTarget[]>([]);
  const [targetId, setTargetId] = useState("");
  const [rows, setRows] = useState<ModuleTypeSummary[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [draft, setDraft] = useState({ manufacturer: "", model: "" });
  const [showNew, setShowNew] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [coverage, setCoverage] = useState<Record<string, CoverageEntry[] | "loading" | "error">>({});
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => { githubApi.list().then((items) => { setTargets(items); if (items[0]) setTargetId(items[0].id); }); }, []);
  useEffect(() => {
    if (!targetId) return;
    setLoading(true); moduleTypesApi.list(targetId).then(setRows).finally(() => setLoading(false));
  }, [targetId]);
  const shown = useMemo(() => rows.filter((row) =>
    `${row.manufacturer} ${row.model} ${row.part_number} ${row.path}`.toLowerCase().includes(search.toLowerCase())), [rows, search]);
  const openDraft = (draftPayload: Record<string, any>) => navigate(`/module-types/${targetId}/edit`, { state: { draftPayload } });
  const importYaml = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const parsed = yaml.load(await file.text()) as Record<string, any>;
      if (!parsed || typeof parsed !== "object" || !parsed.manufacturer || !parsed.model) {
        throw new Error("File is missing manufacturer/model — is this a module-type YAML file?");
      }
      openDraft(parsed);
    } catch (error: any) {
      setImportError(error.message ?? "Could not parse that YAML file.");
    } finally { event.target.value = ""; }
  };
  const checkCoverage = async (path: string) => {
    setCoverage((current) => ({ ...current, [path]: "loading" }));
    try {
      const result = await moduleTypesApi.coverage(targetId, path);
      setCoverage((current) => ({ ...current, [path]: result }));
    }
    catch { setCoverage((current) => ({ ...current, [path]: "error" })); }
  };

  return <div>
    <h1>Module Types</h1>
    <p className="page-subtitle">Manage module-type YAML and module images, then push reviewed definitions to NetBox instances.</p>
    <div className="toolbar">
      <select value={targetId} onChange={(e) => setTargetId(e.target.value)}>
        {targets.map((target) => <option key={target.id} value={target.id}>{target.name}</option>)}
      </select>
      <button className="primary" onClick={() => setShowNew((value) => !value)}>+ New from scratch</button>
      <button onClick={() => fileInput.current?.click()}>Import YAML file</button>
      <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search module types" />
      <input ref={fileInput} type="file" accept=".yml,.yaml" style={{ display: "none" }} onChange={importYaml} />
    </div>
    {importError && <p style={{ color: "var(--danger)" }}>{importError}</p>}
    {showNew && <div className="card">
      <h2>New module type</h2>
      <div className="field-row-2col">
        <div className="form-row"><label>Manufacturer</label><input value={draft.manufacturer} onChange={(e) => setDraft({ ...draft, manufacturer: e.target.value })} /></div>
        <div className="form-row"><label>Model</label><input value={draft.model} onChange={(e) => setDraft({ ...draft, model: e.target.value })} /></div>
      </div>
      <button className="primary" disabled={!targetId || !draft.manufacturer || !draft.model} onClick={() => openDraft({ ...draft })}>Open in editor</button>
    </div>}
    <div className="card"><table><thead><tr><th>Manufacturer</th><th>Model</th><th>Part number</th><th>Path</th><th>Coverage</th></tr></thead>
      <tbody>{shown.map((row) => { const status = coverage[row.path]; return <tr key={row.path}>
        <td>{row.manufacturer ?? "—"}</td><td><a style={{ cursor: "pointer", color: "var(--accent)" }} onClick={() => navigate(
          `/module-types/${targetId}/edit?path=${encodeURIComponent(row.path)}`
        )}>{row.model ?? row.path}</a></td><td>{row.part_number ?? "—"}</td><td className="mono">{row.path}</td><td>
          {!status && <button onClick={() => checkCoverage(row.path)}>Check coverage</button>}
          {status === "loading" && "Checking…"}{status === "error" && "Failed to check"}
          {Array.isArray(status) && status.map((entry) => <span key={entry.instance_id} className="pill" style={{ color: STATUS_COLOR[entry.status], marginRight: 4 }}>{entry.instance_name}: {entry.status.replace("_", " ")}</span>)}
        </td>
      </tr>})}
      {loading && <tr><td colSpan={5}>Loading…</td></tr>}
      {!loading && !shown.length && <tr><td colSpan={5} style={{ color: "var(--muted)" }}>No module types found.</td></tr>}</tbody>
    </table></div>
  </div>;
}
