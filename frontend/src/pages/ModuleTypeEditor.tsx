import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import * as yaml from "js-yaml";
import { instancesApi, InstanceDiffResult, moduleTypesApi, NetboxInstance } from "../api/client";
import ComponentGrid, { FieldDef } from "../components/ComponentGrid";
import DiffView from "../components/DiffView";
import ImagePreviewModal, { ActiveImagePreview } from "../components/ImagePreviewModal";

const TABS: { key: string; label: string; fields: FieldDef[] }[] = [
  { key: "interfaces", label: "Interfaces", fields: [{ key: "name", label: "Name" }, { key: "type", label: "Type" }, { key: "label", label: "Label" }] },
  { key: "console-ports", label: "Console Ports", fields: [{ key: "name", label: "Name" }, { key: "type", label: "Type" }] },
  { key: "console-server-ports", label: "Console Server Ports", fields: [{ key: "name", label: "Name" }, { key: "type", label: "Type" }] },
  { key: "power-ports", label: "Power Ports", fields: [{ key: "name", label: "Name" }, { key: "type", label: "Type" }, { key: "maximum_draw", label: "Max draw", type: "number" }] },
  { key: "power-outlets", label: "Power Outlets", fields: [{ key: "name", label: "Name" }, { key: "type", label: "Type" }, { key: "power_port", label: "Power port" }] },
  { key: "rear-ports", label: "Rear Ports", fields: [{ key: "name", label: "Name" }, { key: "type", label: "Type" }, { key: "positions", label: "Positions", type: "number" }] },
  { key: "front-ports", label: "Front Ports", fields: [{ key: "name", label: "Name" }, { key: "type", label: "Type" }, { key: "positions", label: "Positions", type: "number" }] },
  { key: "module-bays", label: "Module Bays", fields: [{ key: "name", label: "Name" }, { key: "position", label: "Position" }] },
];

export default function ModuleTypeEditorPage() {
  const { targetId } = useParams<{ targetId: string }>(); const [params] = useSearchParams();
  const path = params.get("path") ?? ""; const isNew = !path; const navigate = useNavigate(); const location = useLocation();
  const draft = (location.state as { draftPayload?: Record<string, any> } | null)?.draftPayload;
  const [payload, setPayload] = useState<Record<string, any> | null>(isNew ? draft ?? null : null);
  const [sha, setSha] = useState<string>(); const [images, setImages] = useState<Record<string, string>>({});
  const [tab, setTab] = useState("basic"); const [instances, setInstances] = useState<NetboxInstance[]>([]);
  const [selected, setSelected] = useState<string[]>([]); const [overwrite, setOverwrite] = useState(false);
  const [diffs, setDiffs] = useState<InstanceDiffResult[]>([]); const [results, setResults] = useState<{ target: string; status: string; detail?: string }[]>([]);
  const [activeImage, setActiveImage] = useState<ActiveImagePreview>(null); const [imageError, setImageError] = useState<string | null>(null);
  useEffect(() => { instancesApi.list().then(setInstances); }, []);
  useEffect(() => { if (targetId && path) moduleTypesApi.get(targetId, path).then((file) => { setPayload(file.payload); setSha(file.sha); setImages(file.image_status); }); }, [targetId, path]);
  if (!payload || !targetId) return <p>Loading module type…</p>;
  const update = (key: string, value: any) => setPayload({ ...payload, [key]: value });
  const save = async () => {
    const result = isNew
      ? await moduleTypesApi.create(targetId, { manufacturer: payload.manufacturer, model: payload.model, payload })
      : await moduleTypesApi.save(targetId, path, { payload, sha });
    setSha(result.sha); if (isNew) navigate(`/module-types/${targetId}/edit?path=${encodeURIComponent(result.path)}`, { replace: true });
  };
  const previewImage = async (side: "front" | "rear") => {
    if (isNew) return; setActiveImage("loading"); setImageError(null);
    try { setActiveImage({ ...(await moduleTypesApi.imagePreview(targetId, path, side)), title: `${payload.manufacturer} ${payload.model} — ${side}` }); }
    catch (error: any) { setActiveImage("error"); setImageError(error.message ?? "Image preview failed."); }
  };
  const uploadImage = async (side: "front" | "rear", file?: File) => {
    if (isNew || !file) return; setImageError(null);
    const content_base64 = await new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onerror = () => reject(reader.error); reader.onload = () => resolve(String(reader.result).split(",", 2)[1]); reader.readAsDataURL(file); });
    try { await moduleTypesApi.saveImage(targetId, path, { side, filename: file.name, content_type: file.type, content_base64 }); const refreshed = await moduleTypesApi.get(targetId, path); setImages(refreshed.image_status); }
    catch (error: any) { setImageError(error.message ?? "Image upload failed."); }
  };
  const removeImage = async (side: "front" | "rear") => {
    if (isNew) return; setImageError(null);
    try { await moduleTypesApi.removeImage(targetId, path, side); const refreshed = await moduleTypesApi.get(targetId, path); setImages(refreshed.image_status); }
    catch (error: any) { setImageError(error.message ?? "Image removal failed."); }
  };
  return <div><h1>{payload.manufacturer} {payload.model}</h1><p className="page-subtitle mono">{path || "not saved yet"}</p>
    <div className="editor-layout"><div><div className="tabs">
      <button className={tab === "basic" ? "active" : ""} onClick={() => setTab("basic")}>Basic Info</button>
      {TABS.map((item) => <button key={item.key} className={tab === item.key ? "active" : ""} onClick={() => setTab(item.key)}>{item.label}</button>)}
      <button className={tab === "publish" ? "active" : ""} onClick={() => setTab("publish")}>Push to NetBox</button>
    </div>
    {tab === "basic" && <div className="card">
      {(["manufacturer", "model", "part_number", "description", "comments"] as const).map((field) => <div className="form-row" key={field}><label>{field.replace(/_/g, " ")}</label>
        <input value={payload[field] ?? ""} onChange={(e) => update(field, e.target.value)} /></div>)}
      <div className="form-row"><label>Profile</label><select value={payload.profile ?? ""} onChange={(e) => update("profile", e.target.value || null)}>
        <option value="">— none —</option>{["CPU", "Fan", "GPU", "Hard disk", "Memory", "Power supply", "Expansion card"].map((x) => <option key={x}>{x}</option>)}</select></div>
      <div className="form-row"><label>Images</label>{(["front", "rear"] as const).map((side) => <div className="toolbar" key={side}>
        <span className="pill">{side}: {images[side] ?? "not present"}</span>
        <label className={`button-link file-button${isNew ? " disabled" : ""}`}>{images[side] ? "Replace" : "Attach"}<input hidden disabled={isNew} type="file" accept="image/png,image/jpeg,image/webp" onChange={(e) => { uploadImage(side, e.target.files?.[0]); e.target.value = ""; }} /></label>
        <button disabled={isNew || !images[side]} onClick={() => previewImage(side)}>Preview</button>
        <button className="danger" disabled={isNew || !images[side]} onClick={() => removeImage(side)}>Remove</button>
      </div>)}
        <div className="field-help">Images are stored flat under module-images/&lt;manufacturer&gt;. Save a new module type before attaching images.</div>{imageError && <p style={{ color: "var(--danger)" }}>{imageError}</p>}</div>
    </div>}
    {TABS.filter((item) => item.key === tab).map((item) => <div className="card" key={item.key}><ComponentGrid rows={payload[item.key] ?? []} fields={item.fields}
      onChange={(rows) => update(item.key, rows)} /></div>)}
    {tab === "publish" && <div className="card"><h2>Push to NetBox instance(s)</h2>
      {instances.map((instance) => <label key={instance.id} style={{ display: "block" }}><input type="checkbox" style={{ width: "auto", marginRight: 6 }} checked={selected.includes(instance.id)}
        onChange={(e) => setSelected(e.target.checked ? [...selected, instance.id] : selected.filter((id) => id !== instance.id))} />{instance.name}</label>)}
      <label><input type="checkbox" style={{ width: "auto", margin: 10 }} checked={overwrite} onChange={(e) => setOverwrite(e.target.checked)} />Overwrite existing</label>
      <div className="toolbar"><button onClick={async () => setDiffs(await moduleTypesApi.diff(targetId, path, selected))}>Preview diff</button>
        <button className="primary" onClick={async () => setResults(await moduleTypesApi.push(targetId, path, selected, overwrite))}>Push</button></div>
      {diffs.map((item) => <div className="card" key={item.instance_id}><strong>{item.instance_name}</strong>{item.diff && <DiffView diff={item.diff} />}</div>)}
      {results.map((item) => <p key={item.target}><span className={`status-dot ${item.status === "success" ? "status-ok" : "status-error"}`} />{item.target}: {item.detail}</p>)}
    </div>}
    <div className="card"><button className="primary" onClick={save}>Save (open pull request)</button></div></div>
    <div><h2>YAML preview</h2><pre className="yaml-preview mono">{yaml.dump(payload, { sortKeys: false })}</pre></div></div><ImagePreviewModal image={activeImage} onClose={() => setActiveImage(null)} />
  </div>;
}
