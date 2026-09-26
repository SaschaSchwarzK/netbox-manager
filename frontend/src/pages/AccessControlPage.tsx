import { useEffect, useState } from "react";
import {
  accessApi, AccessMapping, Role, ScopeType, SCOPE_ALL,
  instancesApi, NetboxInstance, githubApi, GithubTarget,
} from "../api/client";

const ROLES: Role[] = ["viewer", "editor", "admin"];

export default function AccessControlPage() {
  const [knownGroups, setKnownGroups] = useState<string[]>([]);
  const [mappings, setMappings] = useState<AccessMapping[]>([]);
  const [instances, setInstances] = useState<NetboxInstance[]>([]);
  const [targets, setTargets] = useState<GithubTarget[]>([]);
  const [newGroup, setNewGroup] = useState("");
  const [newRole, setNewRole] = useState<Role>("viewer");
  const [newScopeType, setNewScopeType] = useState<ScopeType>("all");
  const [newResource, setNewResource] = useState("");

  const load = () => {
    accessApi.knownGroups().then(setKnownGroups);
    accessApi.listMappings().then(setMappings);
    instancesApi.list().then(setInstances);
    githubApi.list().then(setTargets);
  };
  useEffect(() => { load(); }, []);

  const sortedMappings = [...mappings].sort((a, b) => {
    const groupOrder = a.oidc_group.localeCompare(b.oidc_group);
    if (groupOrder !== 0) return groupOrder;
    if (a.resource_type === SCOPE_ALL && b.resource_type !== SCOPE_ALL) return -1;
    if (b.resource_type === SCOPE_ALL && a.resource_type !== SCOPE_ALL) return 1;
    return `${a.resource_type}:${a.resource_id}`.localeCompare(`${b.resource_type}:${b.resource_id}`);
  });

  const handleChangeRole = async (mapping: AccessMapping, role: Role | null) => {
    await accessApi.updateMapping(mapping.id, {
      oidc_group: mapping.oidc_group,
      role,
      resource_type: mapping.resource_type,
      resource_id: mapping.resource_id,
    });
    load();
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Remove this access mapping?")) return;
    await accessApi.deleteMapping(id);
    load();
  };

  const handleAdd = async () => {
    const group = newGroup.trim();
    if (!group || (newScopeType !== "all" && !newResource)) return;
    await accessApi.createMapping({
      oidc_group: group,
      role: newRole,
      resource_type: newScopeType === "all" ? SCOPE_ALL : newScopeType,
      resource_id: newScopeType === "all" ? SCOPE_ALL : newResource,
    });
    setNewGroup("");
    setNewRole("viewer");
    setNewScopeType("all");
    setNewResource("");
    load();
  };

  const resourceOptions = newScopeType === "instance" ? instances : targets;

  return (
    <div>
      <h1>Access Control</h1>
      <p className="page-subtitle">
        Each mapping grants an OIDC group a role on every resource or on one specific NetBox instance or
        GitHub target. A resource with no mappings stays visible to everyone, so scoping is opt-in. Role and
        visibility are independent: a role on all resources does not reveal a resource scoped to other groups.
      </p>

      <div className="card">
        <h2>Access Mappings</h2>
        <table>
          <thead><tr><th>OIDC Group</th><th>Role</th><th>Scope</th><th></th></tr></thead>
          <tbody>
            {sortedMappings.map((mapping) => (
              <tr key={mapping.id}>
                <td className="mono">{mapping.oidc_group}</td>
                <td>
                  <select
                    value={mapping.role ?? ""}
                    onChange={(event) => handleChangeRole(
                      mapping,
                      event.target.value === "" ? null : event.target.value as Role,
                    )}
                  >
                    {mapping.resource_type !== SCOPE_ALL && (
                      <option value="">— use default role —</option>
                    )}
                    {ROLES.map((role) => <option key={role} value={role}>{role}</option>)}
                  </select>
                </td>
                <td>{mapping.resource_type === SCOPE_ALL ? "All resources" : mapping.resource_name}</td>
                <td>
                  <button className="danger" onClick={() => handleDelete(mapping.id)} style={{ padding: "2px 8px" }}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
            {mappings.length === 0 && (
              <tr><td colSpan={4} style={{ color: "var(--muted)" }}>No access mappings yet.</td></tr>
            )}
          </tbody>
        </table>

        <div className="toolbar" style={{ marginTop: 10 }}>
          <input
            list="known-groups"
            placeholder="oidc group name"
            value={newGroup}
            onChange={(event) => setNewGroup(event.target.value)}
            style={{ maxWidth: 220 }}
          />
          <select value={newRole} onChange={(event) => setNewRole(event.target.value as Role)}>
            {ROLES.map((role) => <option key={role} value={role}>{role}</option>)}
          </select>
          <select
            value={newScopeType}
            onChange={(event) => {
              setNewScopeType(event.target.value as ScopeType);
              setNewResource("");
            }}
          >
            <option value="all">All resources</option>
            <option value="instance">NetBox instance</option>
            <option value="github_target">GitHub target</option>
          </select>
          {newScopeType !== "all" && (
            <select
              value={newResource}
              onChange={(event) => setNewResource(event.target.value)}
              style={{ maxWidth: 220 }}
            >
              <option value="">— select resource —</option>
              {resourceOptions.map((resource) => (
                <option key={resource.id} value={resource.id}>{resource.name}</option>
              ))}
            </select>
          )}
          <button
            className="primary"
            disabled={!newGroup.trim() || (newScopeType !== "all" && !newResource)}
            onClick={handleAdd}
          >
            Add
          </button>
        </div>
      </div>

      <datalist id="known-groups">
        {knownGroups.map((group) => <option key={group} value={group} />)}
      </datalist>
    </div>
  );
}
