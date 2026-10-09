import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import * as yaml from "js-yaml";
import { CoverageEntry, githubApi, GithubTarget, rackTypesApi, RackTypeSummary } from "../api/client";

const COLORS: Record<string, string> = { in_sync: "var(--success)", drift: "var(--warning)", missing: "var(--danger)", error: "var(--danger)" };

export default function RackTypesPage() {
  const navigate = useNavigate(); const input = useRef<HTMLInputElement>(null);
  const [targets, setTargets] = useState<GithubTarget[]>([]); const [targetId, setTargetId] = useState("");
  const [rows, setRows] = useState<RackTypeSummary[]>([]); const [filter, setFilter] = useState("");
  const [showNew, setShowNew] = useState(false); const [error, setError] = useState<string>();
  const [coverage, setCoverage] = useState<Record<string, CoverageEntry[] | "loading" | "error">>({});
  const [draft, setDraft] = useState({ manufacturer: "", model: "", slug: "" });
  useEffect(() => { githubApi.list().then((items) => { setTargets(items); if (items[0]) setTargetId(items[0].id); }); }, []);
  useEffect(() => { if (targetId) rackTypesApi.list(targetId).then(setRows); }, [targetId]);
  const shown = useMemo(() => rows.filter((row) => Object.values(row).join(" ").toLowerCase().includes(filter.toLowerCase())), [rows, filter]);
  const openDraft = (payload: Record<string, any>) => navigate(`/rack-types/${targetId}/edit`, { state: { draftPayload: payload } });
  const importFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]; if (!file) return;
    try {
      const data = yaml.load(await file.text()) as Record<string, any>;
      if (!data?.manufacturer || !data?.model || !data?.slug) throw new Error("Rack YAML requires manufacturer, model, and slug.");
      openDraft(data);
    } catch (exc: any) { setError(exc.message); } finally { event.target.value = ""; }
  };
  const check = async (path: string) => {
    setCoverage((value) => ({ ...value, [path]: "loading" }));
    try { const result = await rackTypesApi.coverage(targetId, path); setCoverage((value) => ({ ...value, [path]: result })); }
    catch { setCoverage((value) => ({ ...value, [path]: "error" })); }
  };
  if (!targets.length) return <div><h1>Rack Types</h1><p className="page-subtitle">Add a GitHub target first.</p></div>;
  return <div><h1>Rack Types</h1><p className="page-subtitle">Manage community-library rack definitions and push them to NetBox.</p>
    <div className="toolbar"><select value={targetId} onChange={(e) => setTargetId(e.target.value)}>{targets.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</select>
      <button className="primary" onClick={() => setShowNew(!showNew)}>+ New from scratch</button><button onClick={() => input.current?.click()}>Import YAML file</button>
      <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter rack types" /><input ref={input} hidden type="file" accept=".yml,.yaml" onChange={importFile} /></div>
    {error && <p style={{ color: "var(--danger)" }}>{error}</p>}
    {showNew && <div className="card"><h2>New rack type</h2>{(["manufacturer", "model", "slug"] as const).map((field) => <div className="form-row" key={field}><label>{field}</label><input value={draft[field]} onChange={(e) => setDraft({ ...draft, [field]: e.target.value })} /></div>)}
      <button className="primary" disabled={!draft.manufacturer || !draft.model || !draft.slug} onClick={() => openDraft({ ...draft, form_factor: "4-post-cabinet", width: 19, u_height: 42, starting_unit: 1, desc_units: false })}>Open in editor</button></div>}
    <div className="card"><table><thead><tr><th>Manufacturer</th><th>Model</th><th>Slug</th><th>Height</th><th>Path</th><th>Coverage</th></tr></thead><tbody>
      {shown.map((row) => { const status = coverage[row.path]; return <tr key={row.path}><td>{row.manufacturer}</td><td><a style={{ color: "var(--accent)", cursor: "pointer" }} onClick={() => navigate(`/rack-types/${targetId}/edit?path=${encodeURIComponent(row.path)}`)}>{row.model}</a></td><td className="mono">{row.slug}</td><td>{row.u_height}U</td><td className="mono">{row.path}</td><td>{!status && <button onClick={() => check(row.path)}>Check coverage</button>}{status === "loading" && "Checking…"}{status === "error" && "Failed"}{Array.isArray(status) && status.map((item) => <span className="pill" key={item.instance_id} style={{ color: COLORS[item.status], marginRight: 4 }}>{item.instance_name}: {item.status.replace("_", " ")}</span>)}</td></tr>; })}
      {!shown.length && <tr><td colSpan={6}>No rack types found.</td></tr>}</tbody></table></div>
  </div>;
}
