import { useEffect, useState } from "react";
import {
  accessApi, RoleMapping, ScopeMapping, Role,
  instancesApi, NetboxInstance, githubApi, GithubTarget,
} from "../api/client";

const ROLES: Role[] = ["viewer", "editor", "admin"];

export default function AccessControlPage() {
  const [knownGroups, setKnownGroups] = useState<string[]>([]);
  const [roleMappings, setRoleMappings] = useState<RoleMapping[]>([]);
  const [scopeMappings, setScopeMappings] = useState<ScopeMapping[]>([]);
  const [instances, setInstances] = useState<NetboxInstance[]>([]);
  const [targets, setTargets] = useState<GithubTarget[]>([]);

  const [newRoleGroup, setNewRoleGroup] = useState("");
  const [newRole, setNewRole] = useState<Role>("viewer");

  const [newScopeGroup, setNewScopeGroup] = useState("");
  const [newScopeType, setNewScopeType] = useState<"instance" | "github_target">("instance");
  const [newScopeResource, setNewScopeResource] = useState("");

  const load = () => {
    accessApi.knownGroups().then(setKnownGroups);
    accessApi.listRoleMappings().then(setRoleMappings);
    accessApi.listScopeMappings().then(setScopeMappings);
    instancesApi.list().then(setInstances);
    githubApi.list().then(setTargets);
  };
  useEffect(() => { load(); }, []);

  const handleAddRole = async () => {
    if (!newRoleGroup.trim()) return;
    await accessApi.createRoleMapping(newRoleGroup.trim(), newRole);
    setNewRoleGroup("");
    setNewRole("viewer");
    load();
  };

  const handleChangeRole = async (m: RoleMapping, role: Role) => {
    await accessApi.updateRoleMapping(m.id, m.oidc_group, role);
    load();
  };

  const handleDeleteRole = async (id: string) => {
    if (!confirm("Remove this role mapping? Members of this group fall back to the default role.")) return;
    await accessApi.deleteRoleMapping(id);
    load();
  };

  const handleAddScope = async () => {
    if (!newScopeGroup.trim() || !newScopeResource) return;
    await accessApi.createScopeMapping(newScopeGroup.trim(), newScopeType, newScopeResource);
    setNewScopeGroup("");
    setNewScopeResource("");
    load();
  };

  const handleDeleteScope = async (id: string) => {
    await accessApi.deleteScopeMapping(id);
    load();
  };

  const resourceOptions = newScopeType === "instance" ? instances : targets;

  // Group scope mappings by resource for a clearer "who can see this" view.
  const scopeByResource = scopeMappings.reduce<Record<string, ScopeMapping[]>>((acc, m) => {
    const key = `${m.resource_type}:${m.resource_id}`;
    (acc[key] ??= []).push(m);
    return acc;
  }, {});

  return (
    <div>
      <h1>Access Control</h1>
      <p className="page-subtitle">
        Two independent mappings: <strong>role</strong> (what actions a group's members can take) and{" "}
        <strong>scope</strong> (which instances/GitHub targets they can even see). A resource with no scope
        mappings at all stays visible to everyone — scoping is opt-in per resource, not opt-out.
      </p>

      <div className="card">
        <h2>Role Mappings</h2>
        <p style={{ color: "var(--muted)", fontSize: 13, marginTop: -6 }}>
          A user in multiple mapped groups gets the highest role among them. Unmapped groups get the server's
          default role (configured via NBM_DEFAULT_ROLE, currently used for anyone not listed below).
        </p>
        <table>
          <thead><tr><th>OIDC Group</th><th>Role</th><th></th></tr></thead>
          <tbody>
            {roleMappings.map((m) => (
              <tr key={m.id}>
                <td className="mono">{m.oidc_group}</td>
                <td>
                  <select value={m.role} onChange={(e) => handleChangeRole(m, e.target.value as Role)}>
                    {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                </td>
                <td><button className="danger" onClick={() => handleDeleteRole(m.id)} style={{ padding: "2px 8px" }}>Remove</button></td>
              </tr>
            ))}
            {roleMappings.length === 0 && <tr><td colSpan={3} style={{ color: "var(--muted)" }}>No role mappings yet — everyone gets the default role.</td></tr>}
          </tbody>
        </table>
        <div className="toolbar" style={{ marginTop: 10 }}>
          <input list="known-groups" placeholder="oidc group name" value={newRoleGroup} onChange={(e) => setNewRoleGroup(e.target.value)} style={{ maxWidth: 240 }} />
          <select value={newRole} onChange={(e) => setNewRole(e.target.value as Role)}>
            {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
          <button className="primary" disabled={!newRoleGroup.trim()} onClick={handleAddRole}>Add</button>
        </div>
      </div>

      <div className="card">
        <h2>Scope Mappings</h2>
        <p style={{ color: "var(--muted)", fontSize: 13, marginTop: -6 }}>
          Grants a group visibility into one specific instance or GitHub target. The first mapping added for
          a resource switches it from "visible to everyone" to "visible only to explicitly listed groups."
        </p>
        <table>
          <thead><tr><th>Resource</th><th>Type</th><th>Groups with access</th><th></th></tr></thead>
          <tbody>
            {Object.entries(scopeByResource).map(([key, mappings]) => (
              <tr key={key}>
                <td>{mappings[0].resource_name}</td>
                <td>{mappings[0].resource_type === "instance" ? "NetBox instance" : "GitHub target"}</td>
                <td>
                  {mappings.map((m) => (
                    <span key={m.id} className="pill" style={{ marginRight: 4 }}>
                      {m.oidc_group} <a onClick={() => handleDeleteScope(m.id)} style={{ cursor: "pointer", color: "var(--danger)" }}>×</a>
                    </span>
                  ))}
                </td>
                <td></td>
              </tr>
            ))}
            {Object.keys(scopeByResource).length === 0 && (
              <tr><td colSpan={4} style={{ color: "var(--muted)" }}>No scope mappings yet — every instance and GitHub target is visible to everyone.</td></tr>
            )}
          </tbody>
        </table>
        <div className="toolbar" style={{ marginTop: 10 }}>
          <input list="known-groups" placeholder="oidc group name" value={newScopeGroup} onChange={(e) => setNewScopeGroup(e.target.value)} style={{ maxWidth: 200 }} />
          <select value={newScopeType} onChange={(e) => { setNewScopeType(e.target.value as any); setNewScopeResource(""); }}>
            <option value="instance">NetBox instance</option>
            <option value="github_target">GitHub target</option>
          </select>
          <select value={newScopeResource} onChange={(e) => setNewScopeResource(e.target.value)} style={{ maxWidth: 220 }}>
            <option value="">— select resource —</option>
            {resourceOptions.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
          <button className="primary" disabled={!newScopeGroup.trim() || !newScopeResource} onClick={handleAddScope}>Grant access</button>
        </div>
      </div>

      <datalist id="known-groups">
        {knownGroups.map((g) => <option key={g} value={g} />)}
      </datalist>
    </div>
  );
}
