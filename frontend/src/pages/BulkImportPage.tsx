import { useEffect, useState } from "react";
import {
  githubApi, GithubTarget, instancesApi, NetboxInstance,
  bulkImportApi, importFromNetboxApi, BulkImportResult,
} from "../api/client";

type SourceType = "github" | "netbox";

interface Candidate {
  key: string; // unique per candidate; for github this is the source path, for netbox "manufacturer/slug"
  manufacturer?: string;
  model?: string;
  slug?: string;
  path?: string; // github only
}

export default function BulkImportPage() {
  const [targets, setTargets] = useState<GithubTarget[]>([]);
  const [selectedTarget, setSelectedTarget] = useState("");
  const [instances, setInstances] = useState<NetboxInstance[]>([]);

  const [sourceType, setSourceType] = useState<SourceType>("github");

  // GitHub-library source config
  const [sourceRepo, setSourceRepo] = useState("netbox-community/devicetype-library");
  const [sourceBranch, setSourceBranch] = useState("master");
  const [sourceBaseDir, setSourceBaseDir] = useState("device-types");
  const [sourcePat, setSourcePat] = useState("");

  // NetBox-instance source config
  const [sourceInstance, setSourceInstance] = useState("");

  const [scanning, setScanning] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<Candidate[] | null>(null);
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const [prTitle, setPrTitle] = useState("");
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [result, setResult] = useState<BulkImportResult | null>(null);

  useEffect(() => {
    githubApi.list().then((t) => { setTargets(t); if (t.length > 0) setSelectedTarget(t[0].id); });
    instancesApi.list().then(setInstances);
  }, []);

  const resetResults = () => {
    setCandidates(null);
    setSelected(new Set());
    setResult(null);
    setImportError(null);
  };

  const handleScan = async () => {
    setScanning(true);
    setScanError(null);
    resetResults();
    try {
      if (sourceType === "github") {
        const found = await bulkImportApi.scan(selectedTarget, sourceRepo, sourceBranch, sourceBaseDir, sourcePat || undefined);
        setCandidates(found.map((e) => ({ key: e.path, path: e.path, manufacturer: e.manufacturer_guess ?? undefined, slug: e.slug_guess ?? undefined })));
      } else {
        if (!sourceInstance) return;
        const found = await importFromNetboxApi.scan(selectedTarget, sourceInstance);
        setCandidates(found.map((e) => ({ key: `${e.manufacturer}/${e.slug}`, manufacturer: e.manufacturer, model: e.model, slug: e.slug })));
      }
    } catch (err: any) {
      setScanError(err.message ?? "Scan failed.");
    } finally {
      setScanning(false);
    }
  };

  const filtered = (candidates ?? []).filter((c) => {
    if (!filter.trim()) return true;
    const f = filter.toLowerCase();
    return (c.path ?? "").toLowerCase().includes(f) || (c.manufacturer ?? "").toLowerCase().includes(f) || (c.model ?? "").toLowerCase().includes(f);
  });

  const toggle = (key: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  };
  const selectAllFiltered = () => setSelected((prev) => new Set([...prev, ...filtered.map((c) => c.key)]));
  const clearSelection = () => setSelected(new Set());

  const handleImport = async () => {
    setImporting(true);
    setImportError(null);
    try {
      const chosen = (candidates ?? []).filter((c) => selected.has(c.key));
      let res: BulkImportResult;
      if (sourceType === "github") {
        res = await bulkImportApi.import(selectedTarget, {
          source_repo: sourceRepo, source_branch: sourceBranch, source_pat: sourcePat || undefined,
          paths: chosen.map((c) => c.path!), pr_title: prTitle || undefined,
        });
      } else {
        res = await importFromNetboxApi.import(selectedTarget, {
          instance_id: sourceInstance,
          selections: chosen.map((c) => ({ manufacturer: c.manufacturer!, slug: c.slug! })),
          pr_title: prTitle || undefined,
        });
      }
      setResult(res);
      setSelected(new Set());
    } catch (err: any) {
      setImportError(err.message ?? "Import failed.");
    } finally {
      setImporting(false);
    }
  };

  if (targets.length === 0) {
    return (
      <div>
        <h1>Bulk Import</h1>
        <p className="page-subtitle">Add a GitHub target first — imported device types land in that repo.</p>
      </div>
    );
  }

  return (
    <div>
      <h1>Bulk Import</h1>
      <p className="page-subtitle">
        Import device types from either a GitHub device-type library or directly from a NetBox instance, select the
        ones you want, and land them all in <strong>one pull request</strong> — not one PR per file. Once that PR is
        reviewed and merged, propagate them to other instances the normal way, from the Device Types page.
      </p>

      <div className="card">
        <h2>Destination</h2>
        <div className="form-row">
          <label>Target repo</label>
          <select value={selectedTarget} onChange={(e) => setSelectedTarget(e.target.value)} style={{ maxWidth: 320 }}>
            {targets.map((t) => <option key={t.id} value={t.id}>{t.name} ({t.repo}@{t.branch})</option>)}
          </select>
        </div>
      </div>

      <div className="card">
        <h2>Source</h2>
        <div className="tabs" style={{ marginBottom: 12 }}>
          <button className={sourceType === "github" ? "active" : ""} onClick={() => { setSourceType("github"); resetResults(); }}>
            GitHub library
          </button>
          <button className={sourceType === "netbox" ? "active" : ""} onClick={() => { setSourceType("netbox"); resetResults(); }}>
            NetBox instance
          </button>
        </div>

        {sourceType === "github" ? (
          <>
            <div className="form-row">
              <label>Source repo (owner/repo)</label>
              <input className="mono" value={sourceRepo} onChange={(e) => setSourceRepo(e.target.value)} />
            </div>
            <div className="form-row">
              <label>Branch</label>
              <input value={sourceBranch} onChange={(e) => setSourceBranch(e.target.value)} style={{ maxWidth: 160 }} />
            </div>
            <div className="form-row">
              <label>Base directory</label>
              <input className="mono" value={sourceBaseDir} onChange={(e) => setSourceBaseDir(e.target.value)} style={{ maxWidth: 240 }} />
            </div>
            <div className="form-row">
              <label>Source PAT (optional — leave blank to use the target repo's own token)</label>
              <input type="password" className="mono" value={sourcePat} onChange={(e) => setSourcePat(e.target.value)}
                placeholder="only needed if the source is private and the target's token can't read it" />
            </div>
          </>
        ) : (
          <div className="form-row">
            <label>NetBox instance</label>
            <select value={sourceInstance} onChange={(e) => setSourceInstance(e.target.value)} style={{ maxWidth: 320 }}>
              <option value="">— select instance —</option>
              {instances.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
            </select>
          </div>
        )}

        <button className="primary" disabled={scanning || (sourceType === "github" ? !sourceRepo : !sourceInstance)} onClick={handleScan}>
          {scanning ? "Scanning…" : "Scan"}
        </button>
        {scanError && <p style={{ color: "var(--danger)", fontSize: 13 }}>{scanError}</p>}
      </div>

      {candidates && (
        <div className="card">
          <h2>Select device types ({candidates.length} found{filter ? `, ${filtered.length} shown` : ""})</h2>
          <div className="toolbar">
            <input placeholder="Filter by manufacturer or model…" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ maxWidth: 280 }} />
            <button onClick={selectAllFiltered}>Select all shown</button>
            <button onClick={clearSelection}>Clear selection</button>
            <span className="pill">{selected.size} selected</span>
          </div>
          <div style={{ maxHeight: 360, overflowY: "auto", border: "1px solid var(--border)", borderRadius: "var(--radius)" }}>
            <table>
              <thead>
                <tr>
                  <th></th><th>Manufacturer</th><th>{sourceType === "github" ? "Slug (guessed)" : "Model"}</th>
                  <th>{sourceType === "github" ? "Path" : "Slug"}</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((c) => (
                  <tr key={c.key}>
                    <td><input type="checkbox" style={{ width: "auto" }} checked={selected.has(c.key)} onChange={() => toggle(c.key)} /></td>
                    <td>{c.manufacturer ?? "—"}</td>
                    <td className="mono">{(sourceType === "github" ? c.slug : c.model) ?? "—"}</td>
                    <td className="mono" style={{ color: "var(--muted)" }}>{(sourceType === "github" ? c.path : c.slug) ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="form-row" style={{ marginTop: 12 }}>
            <label>PR title (optional)</label>
            <input value={prTitle} onChange={(e) => setPrTitle(e.target.value)}
              placeholder={`Import ${selected.size || "N"} device types`} />
          </div>
          <button className="primary" disabled={selected.size === 0 || importing} onClick={handleImport}>
            {importing ? "Importing…" : `Import ${selected.size || ""} selected & open PR`}
          </button>
          {importError && <p style={{ color: "var(--danger)", fontSize: 13 }}>{importError}</p>}
        </div>
      )}

      {result && (
        <div className="card">
          <h2>Result</h2>
          <p style={{ fontSize: 13 }}>
            <span className="status-dot status-ok" /> Imported {result.imported.length}
            {result.skipped_existing.length > 0 && `, skipped ${result.skipped_existing.length} (already exist)`}
            {result.failed.length > 0 && `, failed ${result.failed.length}`}.
          </p>
          {result.pr_url && (
            <p style={{ fontSize: 13 }}>
              <a href={result.pr_url} target="_blank" rel="noreferrer" style={{ color: "var(--accent)" }}>
                View PR #{result.pr_number}
              </a>
            </p>
          )}
          {result.failed.length > 0 && (
            <div>
              <p style={{ color: "var(--muted)", fontSize: 12 }}>Failed:</p>
              {result.failed.map((f) => (
                <p key={f.path} style={{ fontSize: 12, color: "var(--danger)" }}>{f.path}: {f.error}</p>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
