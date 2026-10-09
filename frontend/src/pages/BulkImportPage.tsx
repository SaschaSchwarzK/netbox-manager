import { Fragment, useEffect, useState } from "react";
import {
  githubApi, GithubTarget, instancesApi, NetboxInstance,
  bulkImportApi, importFromNetboxApi, BulkImportResult, DeviceTypePreview,
  ndxApi,
} from "../api/client";
import ImagePreviewModal, { ActiveImagePreview } from "../components/ImagePreviewModal";

type SourceType = "ndx" | "github" | "netbox";
type ObjectType = "device-types" | "module-types" | "rack-types";
const OBJECT_LABELS: Record<ObjectType, string> = {
  "device-types": "Device Types", "module-types": "Module Types", "rack-types": "Rack Types",
};

interface Candidate {
  key: string; // unique per candidate; for github this is the source path, for netbox "manufacturer/slug"
  manufacturer?: string;
  model?: string;
  slug?: string;
  partNumber?: string;
  vendorSlug?: string; // NDX only
  path?: string; // github only
}

const COMPONENT_COUNT_LABELS: Record<string, string> = {
  "interfaces": "interfaces",
  "console-ports": "console ports",
  "console-server-ports": "console server ports",
  "power-ports": "power ports",
  "power-outlets": "power outlets",
  "rear-ports": "rear ports",
  "front-ports": "front ports",
  "device-bays": "device bays",
  "module-bays": "module bays",
};

function DeviceTypePreviewDetail({ preview, onPreviewImage }: {
  preview: DeviceTypePreview | "loading" | "error" | undefined;
  onPreviewImage: (side: "front" | "rear") => void;
}) {
  if (!preview || preview === "loading") return <p style={{ fontSize: 13, color: "var(--muted)" }}>Loading preview…</p>;
  if (preview === "error") return <p style={{ fontSize: 13, color: "var(--danger)" }}>Could not load a preview for this one.</p>;

  const componentEntries = Object.entries(preview.component_counts ?? {});
  const customFieldEntries = Object.entries(preview.custom_fields ?? {});
  const imageEntries = Object.entries(preview.image_status ?? {});

  return (
    <div style={{ fontSize: 13 }}>
      <p style={{ marginBottom: 6 }}>
        <strong>{preview.manufacturer ?? "?"} {preview.model ?? "?"}</strong>{" "}
        <span className="mono" style={{ color: "var(--muted)" }}>{preview.slug}</span>
        {preview.part_number && <> · Part number <span className="mono">{preview.part_number}</span></>}
      </p>
      {componentEntries.length > 0 && (
        <p style={{ color: "var(--muted)", marginBottom: 6 }}>
          {componentEntries.map(([key, count]) => `${count} ${COMPONENT_COUNT_LABELS[key] ?? key}`).join(", ")}
        </p>
      )}
      {imageEntries.length > 0 && <div style={{ color: "var(--muted)", marginBottom: 6 }}>
        Images: {imageEntries.map(([side, status]) => <span key={side} style={{ marginLeft: 6 }}>
          {side} ({status}) {status === "present" && (side === "front" || side === "rear") &&
            <button style={{ padding: "2px 8px" }} onClick={() => onPreviewImage(side)}>Preview {side}</button>}
        </span>)}
      </div>}
      {customFieldEntries.length > 0 ? (
        <div>
          <div style={{ color: "var(--muted)", marginBottom: 2 }}>Custom fields:</div>
          <table>
            <tbody>
              {customFieldEntries.map(([name, value]) => (
                <tr key={name}>
                  <td className="mono">{name}</td>
                  <td className="mono">{typeof value === "string" ? value : JSON.stringify(value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p style={{ color: "var(--muted)" }}>No custom field values set.</p>
      )}
    </div>
  );
}

export default function BulkImportPage() {
  const [targets, setTargets] = useState<GithubTarget[]>([]);
  const [selectedTarget, setSelectedTarget] = useState("");
  const [instances, setInstances] = useState<NetboxInstance[]>([]);

  const [sourceType, setSourceType] = useState<SourceType>("ndx");
  const [objectType, setObjectType] = useState<ObjectType>("device-types");
  const [ndxQuery, setNdxQuery] = useState("");

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

  const [expandedPreview, setExpandedPreview] = useState<string | null>(null);
  const [previews, setPreviews] = useState<Record<string, DeviceTypePreview | "loading" | "error">>({});

  const [prTitle, setPrTitle] = useState("");
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [result, setResult] = useState<BulkImportResult | null>(null);
  const [activeImage, setActiveImage] = useState<ActiveImagePreview>(null);

  useEffect(() => {
    githubApi.list().then((t) => { setTargets(t); if (t.length > 0) setSelectedTarget(t[0].id); });
    instancesApi.list().then(setInstances);
  }, []);

  const resetResults = () => {
    setCandidates(null);
    setSelected(new Set());
    setResult(null);
    setImportError(null);
    setExpandedPreview(null);
    setPreviews({});
    setActiveImage(null);
  };

  const selectObjectType = (next: ObjectType) => {
    setObjectType(next);
    setSourceBaseDir(next);
    if (next !== "device-types" && sourceType === "ndx") setSourceType("github");
    resetResults();
  };

  const handleScan = async () => {
    setScanning(true);
    setScanError(null);
    resetResults();
    try {
      if (sourceType === "ndx") {
        const found = await ndxApi.search(selectedTarget, ndxQuery);
        setCandidates(found.map((entry) => ({
          key: `${entry.vendor_slug}/${entry.slug}`, vendorSlug: entry.vendor_slug,
          manufacturer: entry.manufacturer, model: entry.model, slug: entry.slug,
          partNumber: entry.part_number ?? undefined,
        })));
      } else if (sourceType === "github") {
        const found = await bulkImportApi.scan(objectType, selectedTarget, sourceRepo, sourceBranch, sourceBaseDir, sourcePat || undefined);
        setCandidates(found.map((e) => ({
          key: e.path, path: e.path, manufacturer: e.manufacturer_guess ?? undefined,
          model: e.model ?? undefined, slug: e.slug_guess ?? undefined, partNumber: e.part_number ?? undefined,
        })));
      } else {
        if (!sourceInstance) return;
        const found = await importFromNetboxApi.scan(objectType, selectedTarget, sourceInstance);
        setCandidates(found.map((e) => ({ key: `${e.manufacturer}/${objectType === "module-types" ? e.model : e.slug}`, manufacturer: e.manufacturer, model: e.model, slug: e.slug, partNumber: e.part_number ?? undefined })));
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
    return (c.path ?? "").toLowerCase().includes(f) || (c.manufacturer ?? "").toLowerCase().includes(f)
      || (c.model ?? "").toLowerCase().includes(f) || (c.partNumber ?? "").toLowerCase().includes(f);
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

  const handlePreview = async (c: Candidate) => {
    if (expandedPreview === c.key) { setExpandedPreview(null); return; }
    setExpandedPreview(c.key);
    if (previews[c.key] && previews[c.key] !== "error") return; // already fetched (or in flight)
    setPreviews((prev) => ({ ...prev, [c.key]: "loading" }));
    try {
      const preview = sourceType === "ndx"
        ? await ndxApi.preview(selectedTarget, c.vendorSlug!, c.slug!)
        : sourceType === "github"
          ? await bulkImportApi.preview(objectType, selectedTarget, sourceRepo, sourceBranch, c.path!, sourcePat || undefined)
          : await importFromNetboxApi.preview(objectType, selectedTarget, sourceInstance, c.manufacturer!, objectType === "module-types" ? c.model! : c.slug!);
      setPreviews((prev) => ({ ...prev, [c.key]: preview }));
    } catch {
      setPreviews((prev) => ({ ...prev, [c.key]: "error" }));
    }
  };

  const handleImagePreview = async (candidate: Candidate, side: "front" | "rear") => {
    if (objectType === "rack-types" || sourceType === "ndx") return;
    setActiveImage("loading");
    try {
      const image = sourceType === "github"
        ? await bulkImportApi.imagePreview(objectType, selectedTarget, {
            source_repo: sourceRepo, source_branch: sourceBranch, source_pat: sourcePat || undefined,
            path: candidate.path!, side,
          })
        : await importFromNetboxApi.imagePreview(
            objectType, selectedTarget, sourceInstance, candidate.manufacturer!,
            objectType === "module-types" ? candidate.model! : candidate.slug!, side,
          );
      setActiveImage({ ...image, title: `${candidate.manufacturer} ${candidate.model} — ${side}` });
    } catch { setActiveImage("error"); }
  };

  const handleImport = async () => {
    setImporting(true);
    setImportError(null);
    try {
      const chosen = (candidates ?? []).filter((c) => selected.has(c.key));
      let res: BulkImportResult;
      if (sourceType === "ndx") {
        res = await ndxApi.import(selectedTarget, {
          selections: chosen.map((c) => ({ vendor_slug: c.vendorSlug!, slug: c.slug! })),
          pr_title: prTitle || undefined,
        });
      } else if (sourceType === "github") {
        res = await bulkImportApi.import(objectType, selectedTarget, {
          source_repo: sourceRepo, source_branch: sourceBranch, source_pat: sourcePat || undefined,
          paths: chosen.map((c) => c.path!), pr_title: prTitle || undefined,
        });
      } else {
        res = await importFromNetboxApi.import(objectType, selectedTarget, {
          instance_id: sourceInstance,
          selections: chosen.map((c) => objectType === "module-types"
            ? { manufacturer: c.manufacturer!, model: c.model! }
            : { manufacturer: c.manufacturer!, slug: c.slug! }),
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
        <p className="page-subtitle">Add a GitHub target first — imported type definitions and images land in that repo.</p>
      </div>
    );
  }

  return (
    <div>
      <h1>Bulk Import</h1>
      <p className="page-subtitle">
        Import device, module, or rack types from GitHub or a NetBox instance; select the
        ones you want, and land them all in <strong>one pull request</strong> — not one PR per file. Once that PR is
        reviewed and merged, propagate them to other instances from the corresponding type page. Device and module images are copied with their definitions.
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
          {(Object.keys(OBJECT_LABELS) as ObjectType[]).map((kind) => <button key={kind}
            className={objectType === kind ? "active" : ""} onClick={() => selectObjectType(kind)}>{OBJECT_LABELS[kind]}</button>)}
        </div>
        <div className="tabs" style={{ marginBottom: 12 }}>
          <button disabled={objectType !== "device-types"} title={objectType !== "device-types" ? "NDX provides device types only" : undefined}
            className={sourceType === "ndx" ? "active" : ""} onClick={() => { setSourceType("ndx"); resetResults(); }}>
            NetBox Data Exchange
          </button>
          <button className={sourceType === "github" ? "active" : ""} onClick={() => { setSourceType("github"); resetResults(); }}>
            GitHub library
          </button>
          <button className={sourceType === "netbox" ? "active" : ""} onClick={() => { setSourceType("netbox"); resetResults(); }}>
            NetBox instance
          </button>
        </div>

        {sourceType === "ndx" ? (
          <div className="form-row">
            <label>Search NDX by manufacturer, model/name, or part number</label>
            <input value={ndxQuery} onChange={(e) => setNdxQuery(e.target.value)} placeholder="e.g. Cisco C9300 or AX1000" />
            <div className="field-help">Uses the public NDX catalog; up to 200 matching device types are shown.</div>
          </div>
        ) : sourceType === "github" ? (
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

        <button className="primary" disabled={scanning || (sourceType === "github" ? !sourceRepo : sourceType === "netbox" ? !sourceInstance : !ndxQuery.trim())} onClick={handleScan}>
          {scanning ? "Searching…" : sourceType === "ndx" ? "Search NDX" : "Scan"}
        </button>
        {scanError && <p style={{ color: "var(--danger)", fontSize: 13 }}>{scanError}</p>}
      </div>

      {candidates && (
        <div className="card">
          <h2>Select {OBJECT_LABELS[objectType].toLowerCase()} ({candidates.length} found{filter ? `, ${filtered.length} shown` : ""})</h2>
          <div className="toolbar">
            <input placeholder="Filter manufacturer, model, part number…" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ maxWidth: 320 }} />
            <button onClick={selectAllFiltered}>Select all shown</button>
            <button onClick={clearSelection}>Clear selection</button>
            <span className="pill">{selected.size} selected</span>
          </div>
          <div style={{ maxHeight: 360, overflowY: "auto", border: "1px solid var(--border)", borderRadius: "var(--radius)" }}>
            <table>
              <thead>
                <tr>
                  <th></th><th>Manufacturer</th><th>Model</th><th>Part number</th>
                  <th>{sourceType === "github" ? "Path" : "Slug"}</th><th></th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((c) => (
                  <Fragment key={c.key}>
                    <tr>
                      <td><input type="checkbox" style={{ width: "auto" }} checked={selected.has(c.key)} onChange={() => toggle(c.key)} /></td>
                      <td>{c.manufacturer ?? "—"}</td>
                      <td>{c.model ?? (sourceType === "github" ? c.slug : "—")}</td>
                      <td className="mono">{c.partNumber ?? "—"}</td>
                      <td className="mono" style={{ color: "var(--muted)" }}>{(sourceType === "github" ? c.path : c.slug) ?? "—"}</td>
                      <td>
                        <button onClick={() => handlePreview(c)} style={{ padding: "2px 8px" }}>
                          {expandedPreview === c.key ? "Hide" : "Preview"}
                        </button>
                      </td>
                    </tr>
                    {expandedPreview === c.key && (
                      <tr>
                        <td colSpan={6} style={{ background: "var(--panel-raised)" }}>
                          <DeviceTypePreviewDetail preview={previews[c.key]} onPreviewImage={(side) => handleImagePreview(c, side)} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>

          <div className="form-row" style={{ marginTop: 12 }}>
            <label>PR title (optional)</label>
            <input value={prTitle} onChange={(e) => setPrTitle(e.target.value)}
              placeholder={`Import ${selected.size || "N"} ${OBJECT_LABELS[objectType].toLowerCase()}`} />
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
          {result.imported.length > 0 && <div>
            <p style={{ color: "var(--muted)", fontSize: 12 }}>Created files (including images):</p>
            {result.imported.map((path) => <p key={path} className="mono" style={{ fontSize: 12 }}>{path}</p>)}
          </div>}
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
      <ImagePreviewModal image={activeImage} onClose={() => setActiveImage(null)} />
    </div>
  );
}
