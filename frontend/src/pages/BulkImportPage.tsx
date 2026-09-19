import { useEffect, useState } from "react";
import { githubApi, GithubTarget, bulkImportApi, BulkImportScanEntry, BulkImportResult } from "../api/client";

export default function BulkImportPage() {
  const [targets, setTargets] = useState<GithubTarget[]>([]);
  const [selectedTarget, setSelectedTarget] = useState("");

  const [sourceRepo, setSourceRepo] = useState("netbox-community/devicetype-library");
  const [sourceBranch, setSourceBranch] = useState("master");
  const [sourceBaseDir, setSourceBaseDir] = useState("device-types");
  const [sourcePat, setSourcePat] = useState("");

  const [scanning, setScanning] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);
  const [entries, setEntries] = useState<BulkImportScanEntry[] | null>(null);
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const [prTitle, setPrTitle] = useState("");
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [result, setResult] = useState<BulkImportResult | null>(null);

  useEffect(() => {
    githubApi.list().then((t) => { setTargets(t); if (t.length > 0) setSelectedTarget(t[0].id); });
  }, []);

  const handleScan = async () => {
    setScanning(true);
    setScanError(null);
    setEntries(null);
    setSelected(new Set());
    setResult(null);
    try {
      const found = await bulkImportApi.scan(selectedTarget, sourceRepo, sourceBranch, sourceBaseDir, sourcePat || undefined);
      setEntries(found);
    } catch (err: any) {
      setScanError(err.message ?? "Scan failed.");
    } finally {
      setScanning(false);
    }
  };

  const filtered = (entries ?? []).filter((e) => {
    if (!filter.trim()) return true;
    const f = filter.toLowerCase();
    return e.path.toLowerCase().includes(f) || (e.manufacturer_guess ?? "").toLowerCase().includes(f);
  });

  const toggle = (path: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path); else next.add(path);
      return next;
    });
  };
  const selectAllFiltered = () => setSelected((prev) => new Set([...prev, ...filtered.map((e) => e.path)]));
  const clearSelection = () => setSelected(new Set());

  const handleImport = async () => {
    setImporting(true);
    setImportError(null);
    try {
      const res = await bulkImportApi.import(selectedTarget, {
        source_repo: sourceRepo, source_branch: sourceBranch, source_pat: sourcePat || undefined,
        paths: Array.from(selected), pr_title: prTitle || undefined,
      });
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
        Scan an existing device-type library (e.g. a fork of netbox-community/devicetype-library), select the ones
        you want, and land them all in <strong>one pull request</strong> against your target repo — not one PR per file.
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
        <button className="primary" disabled={scanning || !sourceRepo} onClick={handleScan}>
          {scanning ? "Scanning…" : "Scan"}
        </button>
        {scanError && <p style={{ color: "var(--danger)", fontSize: 13 }}>{scanError}</p>}
      </div>

      {entries && (
        <div className="card">
          <h2>Select device types ({entries.length} found{filter ? `, ${filtered.length} shown` : ""})</h2>
          <div className="toolbar">
            <input placeholder="Filter by manufacturer or path…" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ maxWidth: 280 }} />
            <button onClick={selectAllFiltered}>Select all shown</button>
            <button onClick={clearSelection}>Clear selection</button>
            <span className="pill">{selected.size} selected</span>
          </div>
          <div style={{ maxHeight: 360, overflowY: "auto", border: "1px solid var(--border)", borderRadius: "var(--radius)" }}>
            <table>
              <thead><tr><th></th><th>Manufacturer</th><th>Slug (guessed)</th><th>Path</th></tr></thead>
              <tbody>
                {filtered.map((e) => (
                  <tr key={e.path}>
                    <td><input type="checkbox" style={{ width: "auto" }} checked={selected.has(e.path)} onChange={() => toggle(e.path)} /></td>
                    <td>{e.manufacturer_guess ?? "—"}</td>
                    <td className="mono">{e.slug_guess ?? "—"}</td>
                    <td className="mono" style={{ color: "var(--muted)" }}>{e.path}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="form-row" style={{ marginTop: 12 }}>
            <label>PR title (optional)</label>
            <input value={prTitle} onChange={(e) => setPrTitle(e.target.value)}
              placeholder={`Bulk import ${selected.size || "N"} device types from ${sourceRepo}`} />
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
