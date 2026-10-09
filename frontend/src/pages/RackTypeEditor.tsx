import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import * as yaml from "js-yaml";
import { InstanceDiffResult, instancesApi, NetboxInstance, rackTypesApi } from "../api/client";
import DiffView from "../components/DiffView";

const FORM_FACTORS = ["wall-cabinet", "4-post-frame", "2-post-frame", "4-post-cabinet", "wall-frame", "wall-frame-vertical", "wall-cabinet-vertical"];

export default function RackTypeEditor() {
  const { targetId } = useParams<{ targetId: string }>(); const [params] = useSearchParams(); const path = params.get("path") ?? ""; const isNew = !path;
  const location = useLocation(); const navigate = useNavigate(); const draft = (location.state as { draftPayload?: Record<string, any> } | null)?.draftPayload;
  const [payload, setPayload] = useState<Record<string, any> | null>(isNew ? draft ?? null : null); const [sha, setSha] = useState<string>();
  const [instances, setInstances] = useState<NetboxInstance[]>([]); const [selected, setSelected] = useState<string[]>([]); const [overwrite, setOverwrite] = useState(false);
  const [diffs, setDiffs] = useState<InstanceDiffResult[]>([]); const [results, setResults] = useState<{ target: string; status: string; detail?: string }[]>([]);
  useEffect(() => { instancesApi.list().then(setInstances); }, []);
  useEffect(() => { if (targetId && path) rackTypesApi.get(targetId, path).then((file) => { setPayload(file.payload); setSha(file.sha); }); }, [targetId, path]);
  if (!payload || !targetId) return <p>{isNew ? "No draft data." : "Loading from GitHub…"}</p>;
  const update = (key: string, value: any) => setPayload({ ...payload, [key]: value });
  const save = async () => { const result = isNew ? await rackTypesApi.create(targetId, { manufacturer: payload.manufacturer, model: payload.model, slug: payload.slug, payload }) : await rackTypesApi.save(targetId, path, { payload, sha }); setSha(result.sha); if (isNew) navigate(`/rack-types/${targetId}/edit?path=${encodeURIComponent(result.path)}`, { replace: true }); };
  const numeric = ["u_height", "starting_unit", "outer_width", "outer_height", "outer_depth", "weight", "max_weight", "mounting_depth"];
  return <div><h1>{payload.manufacturer} {payload.model}</h1><p className="page-subtitle mono">{path || "not saved yet"}</p><div className="editor-layout"><div>
    <div className="card">{["manufacturer", "model", "slug", "description"].map((field) => <div className="form-row" key={field}><label>{field.replace("_", " ")}</label><input value={payload[field] ?? ""} onChange={(e) => update(field, e.target.value)} /></div>)}
      <div className="form-row"><label>Form factor</label><select value={payload.form_factor} onChange={(e) => update("form_factor", e.target.value)}>{FORM_FACTORS.map((x) => <option key={x}>{x}</option>)}</select></div>
      <div className="form-row"><label>Width</label><select value={payload.width} onChange={(e) => update("width", Number(e.target.value))}>{[10, 19, 20, 23].map((x) => <option key={x}>{x}</option>)}</select></div>
      {numeric.map((field) => <div className="form-row" key={field}><label>{field.replace(/_/g, " ")}</label><input type="number" value={payload[field] ?? ""} onChange={(e) => update(field, e.target.value === "" ? null : Number(e.target.value))} /></div>)}
      <div className="form-row"><label>Outer unit</label><select value={payload.outer_unit ?? ""} onChange={(e) => update("outer_unit", e.target.value || null)}><option value="">—</option><option>mm</option><option>in</option></select></div>
      <div className="form-row"><label>Weight unit</label><select value={payload.weight_unit ?? ""} onChange={(e) => update("weight_unit", e.target.value || null)}><option value="">—</option>{["kg", "g", "lb", "oz"].map((x) => <option key={x}>{x}</option>)}</select></div>
      <div className="form-row"><label><input type="checkbox" style={{ width: "auto", marginRight: 6 }} checked={!!payload.desc_units} onChange={(e) => update("desc_units", e.target.checked)} />Descending units</label></div>
      <div className="form-row"><label>Comments</label><textarea value={payload.comments ?? ""} onChange={(e) => update("comments", e.target.value)} /></div></div>
    {!isNew && <div className="card"><h2>Push to NetBox</h2>{instances.map((instance) => <label key={instance.id} style={{ display: "block" }}><input type="checkbox" style={{ width: "auto", marginRight: 6 }} checked={selected.includes(instance.id)} onChange={(e) => setSelected(e.target.checked ? [...selected, instance.id] : selected.filter((id) => id !== instance.id))} />{instance.name}</label>)}
      <label><input type="checkbox" style={{ width: "auto", margin: 10 }} checked={overwrite} onChange={(e) => setOverwrite(e.target.checked)} />Overwrite existing</label><div className="toolbar"><button disabled={!selected.length} onClick={async () => setDiffs(await rackTypesApi.diff(targetId, path, selected))}>Preview diff</button><button className="primary" disabled={!selected.length} onClick={async () => setResults(await rackTypesApi.push(targetId, path, selected, overwrite))}>Push</button></div>
      {diffs.map((item) => <div className="card" key={item.instance_id}><strong>{item.instance_name}</strong>{item.diff && <DiffView diff={item.diff} />}</div>)}{results.map((item) => <p key={item.target}>{item.target}: {item.detail}</p>)}</div>}
    <div className="card"><button className="primary" onClick={save}>Save (open pull request)</button></div></div><div><h2>YAML preview</h2><pre className="yaml-preview mono">{yaml.dump(payload, { sortKeys: false })}</pre></div></div></div>;
}
