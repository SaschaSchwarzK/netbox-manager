import { useEffect, useState } from "react";
import * as yaml from "js-yaml";
import {
  ApiError, GithubTarget, ManagedOwnerPermissions, NetboxInstance,
  PermissionTemplateSummary, githubApi, instancesApi, ownerPermissionsApi,
} from "../api/client";

const EMPTY = { name: "new-owner-template", version: 1, description: "", permissions: [] };
const errorMessage = (error: any) => error instanceof ApiError ? error.message : error?.message ?? "Request failed";

export default function OwnerPermissionsPage() {
  const [targets, setTargets] = useState<GithubTarget[]>([]); const [targetId, setTargetId] = useState("");
  const [instances, setInstances] = useState<NetboxInstance[]>([]); const [templates, setTemplates] = useState<PermissionTemplateSummary[]>([]);
  const [managed, setManaged] = useState<ManagedOwnerPermissions[]>([]); const [tab, setTab] = useState<"reconcile"|"templates"|"managed">("reconcile");
  const [form, setForm] = useState({ instance_id: "", group_ro: "", group_rw: "", rw_template: "", ro_template: "" });
  const [yamlText, setYamlText] = useState(yaml.dump(EMPTY)); const [editing, setEditing] = useState<string|undefined>();
  const [notice, setNotice] = useState(""); const [busy, setBusy] = useState(false); const [plan, setPlan] = useState<any>(null);
  useEffect(() => { githubApi.list().then(v => { setTargets(v); if(v[0]) setTargetId(v[0].id); }); instancesApi.list().then(v => { setInstances(v); if(v[0]) setForm(f => ({...f, instance_id:v[0].id})); }); }, []);
  const reload = async () => { if(!targetId) return; const [t,m] = await Promise.all([ownerPermissionsApi.templates(targetId), ownerPermissionsApi.managed(targetId)]); setTemplates(t); setManaged(m); if(!form.rw_template && t[0]) setForm(f => ({...f, rw_template:t[0].name})); };
  useEffect(() => { reload().catch(e => setNotice(errorMessage(e))); }, [targetId]);
  const run = async (fn:()=>Promise<any>, ok:string) => { setBusy(true); setNotice(""); try { await fn(); setNotice(ok); await reload(); } catch(e) { setNotice(errorMessage(e)); } finally { setBusy(false); } };
  const parsed = () => { const value = yaml.load(yamlText); if(!value || typeof value !== "object") throw new Error("Template must be a YAML mapping."); return value as Record<string,any>; };
  if(!targets.length) return <div><h1>Owner Permissions</h1><p className="page-subtitle">Add a GitHub target first.</p></div>;
  return <div><h1>Owner Permissions</h1><p className="page-subtitle">Manage shared per-instance read-only/read-write groups. Membership and OIDC mapping remain outside this application.</p>
    <div className="toolbar"><select value={targetId} onChange={e=>setTargetId(e.target.value)}>{targets.map(t=><option key={t.id} value={t.id}>{t.name} ({t.repo}@{t.branch})</option>)}</select></div>
    {notice && <div className="card">{notice}</div>}
    <div className="tabs"><button className={tab==="reconcile"?"active":""} onClick={()=>setTab("reconcile")}>Reconcile</button><button className={tab==="templates"?"active":""} onClick={()=>setTab("templates")}>Templates ({templates.length})</button><button className={tab==="managed"?"active":""} onClick={()=>setTab("managed")}>Managed instances ({managed.length})</button></div>
    {tab==="reconcile" && <div className="card" style={{maxWidth:720}}><h2>Reconcile owner groups</h2><div className="field-row-2col">
      <div className="form-row"><label>NetBox instance</label><select value={form.instance_id} onChange={e=>setForm({...form,instance_id:e.target.value})}>{instances.map(i=><option key={i.id} value={i.id}>{i.name}</option>)}</select></div>
      <div/><div className="form-row"><label>Read-only group</label><input value={form.group_ro} onChange={e=>setForm({...form,group_ro:e.target.value})}/></div><div className="form-row"><label>Read-write group</label><input value={form.group_rw} onChange={e=>setForm({...form,group_rw:e.target.value})}/></div>
      <div className="form-row"><label>Read-write template</label><select value={form.rw_template} onChange={e=>setForm({...form,rw_template:e.target.value})}><option value="">Select…</option>{templates.map(t=><option key={t.path}>{t.name}</option>)}</select></div>
      <div className="form-row"><label>Read-only template</label><select value={form.ro_template} onChange={e=>setForm({...form,ro_template:e.target.value})}><option value="">Derive from RW</option>{templates.map(t=><option key={t.path}>{t.name}</option>)}</select></div></div>
      <button className="primary" disabled={busy||!form.instance_id||!form.group_ro||!form.group_rw||!form.rw_template} onClick={()=>run(()=>ownerPermissionsApi.reconcile(targetId,{...form,ro_template:form.ro_template||null}),"Owner permissions reconciled and PR opened.")}>Reconcile and open PR</button></div>}
    {tab==="templates" && <div className="editor-layout"><div><button className="primary" onClick={()=>{setEditing(undefined);setYamlText(yaml.dump(EMPTY));}}>+ New owner template</button><div className="card"><table><thead><tr><th>Name</th><th>Version</th><th>Description</th><th/></tr></thead><tbody>{templates.map(t=><tr key={t.path}><td>{t.name}</td><td>{t.version}</td><td>{t.description}</td><td><button onClick={async()=>{const f=await ownerPermissionsApi.template(targetId,t.name);setEditing(t.name);setYamlText(yaml.dump(f.payload));}}>Edit</button> <button onClick={async()=>setPlan({template:t.name,data:await ownerPermissionsApi.planApply(targetId,t.name)})}>Plan fleet apply</button></td></tr>)}</tbody></table></div></div>
      <div className="card"><h2>{editing?`Edit ${editing}`:"New template"}</h2><textarea className="yaml" style={{minHeight:420}} value={yamlText} onChange={e=>setYamlText(e.target.value)}/><div className="toolbar"><button onClick={async()=>{try{const r=await ownerPermissionsApi.validate(targetId,parsed());setYamlText(yaml.dump(r.normalized));setNotice("Template is valid.");}catch(e){setNotice(errorMessage(e));}}}>Validate</button><button className="primary" disabled={busy} onClick={()=>run(()=>ownerPermissionsApi.saveTemplate(targetId,parsed(),editing),"Owner template saved through a PR.")}>Validate, save, and open PR</button></div></div>
      {plan&&<div className="card" style={{gridColumn:"1 / -1"}}><h2>Fleet plan: {plan.template}</h2><p>{plan.data.matched} instance(s) matched.</p><pre>{JSON.stringify(plan.data.results,null,2)}</pre><button onClick={()=>setPlan(null)}>Cancel</button> <button className="primary" disabled={busy||plan.data.results.some((r:any)=>r.status!=="success")} onClick={()=>run(()=>ownerPermissionsApi.apply(targetId,plan.template),"Fleet apply complete.").then(()=>setPlan(null))}>Confirm fleet apply</button></div>}</div>}
    {tab==="managed" && <div className="card"><table><thead><tr><th>Instance</th><th>RO group</th><th>RW group</th><th>Template</th><th/></tr></thead><tbody>{managed.map(m=><tr key={m.instance}><td>{m.instance}</td><td>{m.metadata.group_ro}</td><td>{m.metadata.group_rw}</td><td>{m.metadata.rw_template?.name}</td><td><button className="danger" disabled={busy} onClick={()=>run(async()=>{try{return await ownerPermissionsApi.decommission(targetId,m.instance_id,false);}catch(e:any){if(e instanceof ApiError&&e.status===409&&confirm(`${errorMessage(e)} Force deletion?`)) return ownerPermissionsApi.decommission(targetId,m.instance_id,true);throw e;}},"Owner groups decommissioned and cleanup PR opened.")}>Decommission</button></td></tr>)}</tbody></table></div>}
  </div>;
}
