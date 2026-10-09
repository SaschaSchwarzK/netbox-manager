import { useEffect, useState } from "react";
import { dump, load } from "js-yaml";
import { ApiError, githubApi, instancesApi, NetboxInstance, referenceDataApi, ReferenceDataRegistry } from "../api/client";
import { useAccess } from "../contexts/AccessContext";

export default function ReferenceDataPage() {
  const access = useAccess();
  const [targets, setTargets] = useState<{ id: string; name: string }[]>([]);
  const [target, setTarget] = useState("");
  const [registry, setRegistry] = useState<ReferenceDataRegistry | null>(null);
  const [kind, setKind] = useState("");
  const [items, setItems] = useState<Record<string, any>[]>([]);
  const [sha, setSha] = useState<string | undefined>();
  const [yamlMode, setYamlMode] = useState(false);
  const [yaml, setYaml] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState("");
  const [instances, setInstances] = useState<NetboxInstance[]>([]);
  const [selectedInstances, setSelectedInstances] = useState<string[]>([]);
  const [overwrite, setOverwrite] = useState(false);
  const [enableEventRules, setEnableEventRules] = useState(false);
  const [importInstance, setImportInstance] = useState("");
  const [importCandidates, setImportCandidates] = useState<{ name: string; status: "missing" | "changed"; payload: Record<string, any> }[] | null>(null);
  const [selectedImports, setSelectedImports] = useState<Set<string>>(new Set());
  const [scanningImport, setScanningImport] = useState(false);

  useEffect(() => { githubApi.list().then((rows) => { setTargets(rows); if (rows[0]) setTarget(rows[0].id); }).catch(showError); }, []);
  useEffect(() => { instancesApi.list().then(setInstances).catch(showError); }, []);
  useEffect(() => { if (!target) return; referenceDataApi.schema(target).then((value) => { setRegistry(value); setKind((old) => old || value.push_order[0]); }).catch(showError); }, [target]);
  useEffect(() => { if (!target || !kind) return; setImportCandidates(null); setSelectedImports(new Set()); referenceDataApi.get(target, kind).then((file) => { setItems(file.payload.items); setSha(file.sha); setYaml(dump(file.payload)); setError(""); }).catch(showError); }, [target, kind]);
  function showError(value: unknown) { setError(value instanceof ApiError || value instanceof Error ? value.message : String(value)); }
  const fields = registry?.kinds[kind]?.fields ?? [];
  const update = (index: number, field: string, value: string) => setItems((old) => old.map((item, i) => i === index ? { ...item, [field]: value } : item));
  const save = async () => {
    try {
      const payload = yamlMode ? load(yaml) as Record<string, any> : { items };
      const saved = await referenceDataApi.save(target, kind, { payload, sha });
      setResult(`Saved in PR #${saved.pr_number}`); setError("");
    } catch (e) { showError(e); }
  };
  const push = async () => {
    try {
      const rows = await referenceDataApi.push(target, { instance_ids: selectedInstances, overwrite, enable_event_rules: enableEventRules });
      setResult(rows.map((row) => `${row.target}: ${row.detail}${row.warnings?.length ? ` — ${row.warnings.join("; ")}` : ""}`).join("\n"));
    } catch (e) { showError(e); }
  };
  const scanImport = async () => {
    if (!importInstance) return;
    setScanningImport(true); setError(""); setResult("");
    try {
      const scan = await referenceDataApi.importScan(target, kind, importInstance);
      const missing = new Set(scan.diff.extra_on_instance);
      const changed = new Set(scan.diff.changed.map((row) => row.name));
      const candidates = scan.payload.items
        .filter((item) => missing.has(item.name) || changed.has(item.name))
        .map((item) => ({ name: String(item.name), status: missing.has(item.name) ? "missing" as const : "changed" as const, payload: item }));
      setImportCandidates(candidates); setSelectedImports(new Set());
    } catch (e) { showError(e); }
    finally { setScanningImport(false); }
  };
  const applyImports = () => {
    if (!importCandidates) return;
    const chosen = importCandidates.filter((candidate) => selectedImports.has(candidate.name));
    const replacements = new Map(chosen.map((candidate) => [candidate.name, candidate.payload]));
    const existingNames = new Set(items.map((item) => String(item.name)));
    const merged = items.map((item) => replacements.get(String(item.name)) ?? item);
    for (const candidate of chosen) if (!existingNames.has(candidate.name)) merged.push(candidate.payload);
    setItems(merged); setYaml(dump({ items: merged })); setImportCandidates(null); setSelectedImports(new Set());
    setResult(`Added ${chosen.length} imported item(s) to the draft. Review and save to open a pull request.`);
  };
  return <div><h1>Reference Data</h1><p className="page-subtitle">Manage shared NetBox reference objects as reviewed YAML.</p>
    <div className="form-row"><label>GitHub target</label><select value={target} onChange={(e) => setTarget(e.target.value)}>{targets.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</select></div>
    <div className="toolbar" role="tablist">{registry?.push_order.map((name) => <button role="tab" aria-selected={kind === name} className={kind === name ? "primary" : ""} key={name} onClick={() => setKind(name)}>{registry.kinds[name].label}</button>)}</div>
    <div className="toolbar"><button onClick={() => { setYamlMode(!yamlMode); if (!yamlMode) setYaml(dump({ items })); }}>{yamlMode ? "Form view" : "YAML view"}</button></div>
    {yamlMode ? <textarea aria-label="Reference data YAML" className="mono" rows={28} value={yaml} onChange={(e) => setYaml(e.target.value)} /> : <div>{items.map((item, index) => <div className="card" key={`${item.name}-${index}`}><div className="form-grid">{fields.map((field) => <div className="form-row" key={field}><label>{field}</label><input value={typeof item[field] === "object" ? JSON.stringify(item[field]) : item[field] ?? ""} onChange={(e) => update(index, field, e.target.value)} /></div>)}</div><button onClick={() => setItems((old) => old.filter((_, i) => i !== index))}>Remove</button></div>)}<button onClick={() => setItems((old) => [...old, { name: "" }])}>Add {registry?.kinds[kind]?.label}</button></div>}
    {access.can("editor") && <button className="primary" onClick={save}>Save (open pull request)</button>}
    {access.can("editor") && <div className="card"><h2>Import from NetBox</h2>
      <p>Scan the selected instance for {registry?.kinds[kind]?.label.toLowerCase()} that are missing from or differ from this GitHub template.</p>
      <div className="form-row"><label htmlFor="reference-import-instance">Source instance</label><select id="reference-import-instance" value={importInstance} onChange={(e) => setImportInstance(e.target.value)}><option value="">— select instance —</option>{instances.map((instance) => <option key={instance.id} value={instance.id}>{instance.name}</option>)}</select></div>
      <button disabled={!importInstance || scanningImport} onClick={scanImport}>{scanningImport ? "Scanning…" : "Scan for import"}</button>
      {importCandidates && <div><h3>Select items ({importCandidates.length} found)</h3>
        {importCandidates.length === 0 && <p>No missing or changed items were found.</p>}
        {importCandidates.map((candidate) => <label key={candidate.name} style={{ display: "block" }}><input type="checkbox" checked={selectedImports.has(candidate.name)} onChange={(event) => setSelectedImports((old) => { const next = new Set(old); event.target.checked ? next.add(candidate.name) : next.delete(candidate.name); return next; })} /> {candidate.name} <span className="pill">{candidate.status}</span></label>)}
        <button className="primary" disabled={selectedImports.size === 0} onClick={applyImports}>Import selected into draft</button>
      </div>}
    </div>}
    <div className="card"><h2>Push to instances</h2>{instances.map((instance) => <label key={instance.id} style={{ display: "block" }}><input type="checkbox" checked={selectedInstances.includes(instance.id)} onChange={(e) => setSelectedInstances((old) => e.target.checked ? [...old, instance.id] : old.filter((id) => id !== instance.id))} /> {instance.name}</label>)}
      <label style={{ display: "block" }}><input type="checkbox" checked={overwrite} onChange={(e) => setOverwrite(e.target.checked)} /> Overwrite existing objects</label>
      <label style={{ display: "block", color: "var(--danger)" }}><input type="checkbox" checked={enableEventRules} onChange={(e) => setEnableEventRules(e.target.checked)} /> Enable newly-created event rules (may trigger automation)</label>
      {access.can("editor") && <button className="primary" disabled={!selectedInstances.length} onClick={push}>Push reference data</button>}
    </div>
    {error && <p style={{ color: "var(--danger)" }}>{error}</p>}{result && <pre className="mono">{result}</pre>}
  </div>;
}
