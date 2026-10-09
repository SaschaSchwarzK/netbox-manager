const BASE = "/api";
const inFlightGets = new Map<string, Promise<unknown>>();

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown, public rawBody: string) {
    const message = typeof detail === "string"
      ? detail
      : (detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string")
        ? detail.message
        : status === 403 ? "You don't have permission to perform this action." : `Request failed (${status}).`;
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const execute = async (): Promise<T> => {
    const resp = await fetch(`${BASE}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!resp.ok) {
      const body = await resp.text();
      let detail: unknown;
      try { detail = JSON.parse(body).detail; } catch { detail = undefined; }
      if (resp.status === 401) window.dispatchEvent(new Event("nbm:unauthorized"));
      throw new ApiError(resp.status, detail, body);
    }
    if (resp.status === 204) return undefined as T;
    return resp.json();
  };
  if (method !== "GET") return execute();
  const key = `${BASE}${path}`;
  const existing = inFlightGets.get(key);
  if (existing) return existing as Promise<T>;
  const pending = execute().finally(() => inFlightGets.delete(key));
  inFlightGets.set(key, pending);
  return pending;
}

async function requestText(path: string): Promise<string> {
  const resp = await fetch(`${BASE}${path}`);
  if (!resp.ok) {
    const body = await resp.text();
    let detail: unknown;
    try { detail = JSON.parse(body).detail; } catch { detail = undefined; }
    if (resp.status === 401) window.dispatchEvent(new Event("nbm:unauthorized"));
    throw new ApiError(resp.status, detail, body);
  }
  return resp.text();
}

// ---------- Auth ----------

export interface AuthUser {
  sub?: string;
  email?: string;
  name?: string;
  groups: string[];
}

export interface AuthMeResponse {
  auth_enabled: boolean;
  oidc_enabled: boolean;
  local_login_enabled: boolean;
  authenticated: boolean;
  user: AuthUser | null;
  role: "viewer" | "editor" | "admin";
  app_admin: boolean;
}

export const authApi = {
  me: () => request<AuthMeResponse>("/auth/me"),
  localLogin: (username: string, password: string) =>
    request<{ ok: boolean; user: AuthUser; role: Role }>("/auth/local-login", {
      method: "POST", body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>("/auth/logout", { method: "POST" }),
};

// ---------- Access control (RBAC) ----------

export type Role = "viewer" | "editor" | "admin";
export type ScopeType = "all" | "instance" | "github_target";
export const SCOPE_ALL = "*";

export interface AccessMapping {
  id: string;
  oidc_group: string;
  role: Role | null;
  resource_type: "*" | "instance" | "github_target";
  resource_id: string;
  resource_name: string;
  created_at: string;
  updated_at: string;
}

export type AccessMappingInput = Pick<AccessMapping, "oidc_group" | "role" | "resource_type" | "resource_id">;

export const accessApi = {
  me: () => request<{
    role: Role;
    groups: string[];
    scoping_active: boolean;
    app_admin: boolean;
    editable: { instance: string[]; github_target: string[] };
  }>("/access/me"),
  knownGroups: () => request<string[]>("/access/known-groups"),
  listMappings: () => request<AccessMapping[]>("/access/mappings"),
  createMapping: (data: AccessMappingInput) =>
    request<AccessMapping>("/access/mappings", { method: "POST", body: JSON.stringify(data) }),
  updateMapping: (id: string, data: AccessMappingInput) =>
    request<AccessMapping>(`/access/mappings/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteMapping: (id: string) => request<void>(`/access/mappings/${id}`, { method: "DELETE" }),
};

// ---------- Instances ----------

export interface NetboxInstance {
  id: string;
  name: string;
  base_url: string;
  verify_ssl: boolean;
  description?: string | null;
  tags: string[];
  requires_approved_pr: boolean;
  has_ca_bundle: boolean;
  created_at: string;
  updated_at: string;
}

export interface NetboxInstanceCreate {
  name: string;
  base_url: string;
  api_token: string;
  verify_ssl: boolean;
  description?: string;
  tags?: string[];
  requires_approved_pr?: boolean;
  ca_bundle_pem?: string;
}

export interface NetboxInstanceUpdate {
  name?: string;
  base_url?: string;
  api_token?: string;
  verify_ssl?: boolean;
  description?: string;
  tags?: string[];
  requires_approved_pr?: boolean;
  ca_bundle_pem?: string;
}

export const instancesApi = {
  list: () => request<NetboxInstance[]>("/instances"),
  create: (data: NetboxInstanceCreate) =>
    request<NetboxInstance>("/instances", { method: "POST", body: JSON.stringify(data) }),
  update: (id: string, data: NetboxInstanceUpdate) =>
    request<NetboxInstance>(`/instances/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  remove: (id: string) => request<void>(`/instances/${id}`, { method: "DELETE" }),
  test: (id: string) => request<{ ok: boolean; netbox_version?: string; detail?: string }>(
    `/instances/${id}/test`, { method: "POST" }
  ),
  testNew: (data: NetboxInstanceUpdate & { id?: string }) => request<{ ok: boolean; netbox_version?: string; detail?: string }>(
    "/instances/test", { method: "POST", body: JSON.stringify(data) }
  ),
};

// ---------- GitHub targets ----------

export interface GithubTarget {
  id: string;
  name: string;
  repo: string;
  branch: string;
  path_pattern: string;
  module_path_pattern: string;
  rack_path_pattern: string;
  custom_fields_path: string;
  reference_data_path: string;
  created_at: string;
}

export interface GithubTargetCreate {
  name: string;
  repo: string;
  branch: string;
  path_pattern: string;
  module_path_pattern: string;
  rack_path_pattern: string;
  custom_fields_path?: string;
  reference_data_path?: string;
  pat: string;
}

export interface GithubTargetUpdate {
  name?: string;
  repo?: string;
  branch?: string;
  path_pattern?: string;
  module_path_pattern?: string;
  rack_path_pattern?: string;
  custom_fields_path?: string;
  reference_data_path?: string;
  pat?: string;
}

export const githubApi = {
  list: () => request<GithubTarget[]>("/github-targets"),
  create: (data: GithubTargetCreate) =>
    request<GithubTarget>("/github-targets", { method: "POST", body: JSON.stringify(data) }),
  update: (id: string, data: GithubTargetUpdate) =>
    request<GithubTarget>(`/github-targets/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  remove: (id: string) => request<void>(`/github-targets/${id}`, { method: "DELETE" }),
  testNew: (data: GithubTargetUpdate & { id?: string }) => request<{ ok: boolean; detail?: string }>(
    "/github-targets/test", { method: "POST", body: JSON.stringify(data) }
  ),
  test: (id: string) => request<{ ok: boolean; detail?: string }>(
    `/github-targets/${id}/test`, { method: "POST" }
  ),
};

// ---------- Tenant permission automation ----------

export interface PermissionTemplateSummary {
  name: string;
  version: string | number;
  description: string;
  path: string;
}

export interface ManagedTenant {
  instance_id: string;
  instance: string;
  tenant_id: number;
  tenant_name: string;
  metadata: Record<string, any>;
}

export const tenantPermissionsApi = {
  templates: (targetId: string) => request<PermissionTemplateSummary[]>(`/repos/${targetId}/tenant-permissions/templates`),
  template: (targetId: string, name: string) => request<{ path: string; payload: Record<string, any> }>(
    `/repos/${targetId}/tenant-permissions/templates/${encodeURIComponent(name)}`
  ),
  validate: (targetId: string, payload: Record<string, any>) => request<{ valid: boolean; normalized: Record<string, any> }>(
    `/repos/${targetId}/tenant-permissions/validate`, { method: "POST", body: JSON.stringify({ payload }) }
  ),
  saveTemplate: (targetId: string, payload: Record<string, any>, previousName?: string) => request<any>(
    `/repos/${targetId}/tenant-permissions/templates`, {
      method: "PUT", body: JSON.stringify({ payload, previous_name: previousName || null }),
    }
  ),
  tenants: (targetId: string) => request<ManagedTenant[]>(`/repos/${targetId}/tenant-permissions/tenants`),
  availableTenants: (targetId: string, instanceId: string, query = "") => request<{ id: number; name: string }[]>(
    `/repos/${targetId}/tenant-permissions/instances/${instanceId}/available-tenants?q=${encodeURIComponent(query)}`
  ),
  onboard: (targetId: string, data: Record<string, any>) => request<any>(
    `/repos/${targetId}/tenant-permissions/onboard`, { method: "POST", body: JSON.stringify(data) }
  ),
  apply: (targetId: string, template: string) => request<any>(
    `/repos/${targetId}/tenant-permissions/apply`, { method: "POST", body: JSON.stringify({ template }) }
  ),
  planApply: (targetId: string, template: string) => request<any>(
    `/repos/${targetId}/tenant-permissions/apply/plan`, { method: "POST", body: JSON.stringify({ template }) }
  ),
  decommission: (targetId: string, instanceId: string, tenantId: number, force = false) => request<any>(
    `/repos/${targetId}/tenant-permissions/decommission`, {
      method: "POST", body: JSON.stringify({ instance_id: instanceId, tenant_id: tenantId, force }),
    }
  ),
};

// ---------- Device types (files inside a GitHub target repo) ----------

export interface DeviceTypeSummary {
  path: string;
  manufacturer?: string;
  model?: string;
  slug?: string;
  part_number?: string | null;
}

export interface DeviceTypeFile {
  repo_target_id: string;
  path: string;
  sha: string;
  payload: Record<string, any>;
  open_pr?: { number: number; url: string } | null;
  image_status: Record<string, "present" | "missing">;
}

export interface SaveResult {
  path: string;
  sha: string;
  pr_number: number;
  pr_url: string;
}

// ---------- Cross-instance search ----------

export interface SearchResultItem {
  id: number;
  name: string;
  serial?: string | null;
  type_display?: string | null;
  site?: string | null;
  status?: string | null;
  url: string;
}

export interface InstanceSearchResult {
  instance_id: string;
  instance_name: string;
  devices: SearchResultItem[];
  virtual_machines: SearchResultItem[];
  virtual_device_contexts: SearchResultItem[];
  ip_addresses: SearchResultItem[];
  prefixes: SearchResultItem[];
  mac_addresses: SearchResultItem[];
  error?: string | null;
}

export interface SearchResponse {
  query: string;
  results: InstanceSearchResult[];
}

export const searchApi = {
  search: (query: string, instanceIds?: string[]) => {
    const params = new URLSearchParams({ query });
    (instanceIds ?? []).forEach((id) => params.append("instance_ids", id));
    return request<SearchResponse>(`/search?${params.toString()}`);
  },
};

export const deviceTypesApi = {
  list: (targetId: string) => request<DeviceTypeSummary[]>(`/repos/${targetId}/device-types`),

  get: (targetId: string, path: string) =>
    request<DeviceTypeFile>(`/repos/${targetId}/device-types/file?path=${encodeURIComponent(path)}`),

  create: (targetId: string, data: {
    manufacturer: string; model: string; slug: string;
    payload?: Record<string, any>; commit_message?: string; pr_body?: string;
  }) => request<SaveResult>(`/repos/${targetId}/device-types`, { method: "POST", body: JSON.stringify(data) }),

  save: (targetId: string, path: string, data: {
    payload: Record<string, any>; sha?: string; commit_message?: string; pr_body?: string;
  }) => request<SaveResult>(
    `/repos/${targetId}/device-types/file?path=${encodeURIComponent(path)}`,
    { method: "PUT", body: JSON.stringify(data) }
  ),

  remove: (targetId: string, path: string, sha: string, commit_message?: string) =>
    request<void>(
      `/repos/${targetId}/device-types/file?path=${encodeURIComponent(path)}`,
      { method: "DELETE", body: JSON.stringify({ sha, commit_message }) }
    ),

  importYaml: (targetId: string, yaml_text: string, commit_message?: string, pr_body?: string) =>
    request<SaveResult>(`/repos/${targetId}/device-types/import`, {
      method: "POST", body: JSON.stringify({ yaml_text, commit_message, pr_body }),
    }),

  pushToNetbox: (targetId: string, path: string, instance_ids: string[], overwrite: boolean, tags: string[] = []) =>
    request<{ target: string; status: string; detail?: string }[]>(
      `/repos/${targetId}/device-types/file/push-to-netbox?path=${encodeURIComponent(path)}`,
      { method: "POST", body: JSON.stringify({ instance_ids, tags, overwrite }) }
    ),

  diffWithNetbox: (targetId: string, path: string, instance_ids: string[], tags: string[] = []) =>
    request<InstanceDiffResult[]>(
      `/repos/${targetId}/device-types/file/diff-with-netbox?path=${encodeURIComponent(path)}`,
      { method: "POST", body: JSON.stringify({ instance_ids, tags, overwrite: false }) }
    ),

  coverage: (targetId: string, path: string) =>
    request<CoverageEntry[]>(`/repos/${targetId}/device-types/file/coverage?path=${encodeURIComponent(path)}`),
  imagePreview: (targetId: string, path: string, side: "front" | "rear") =>
    request<ImagePreview>(`/repos/${targetId}/device-types/file/image-preview?path=${encodeURIComponent(path)}&side=${side}`),
  saveImage: (targetId: string, path: string, data: Record<string, any>) => request<SaveResult>(
    `/repos/${targetId}/device-types/file/image?path=${encodeURIComponent(path)}`,
    { method: "PUT", body: JSON.stringify(data) }
  ),
  removeImage: (targetId: string, path: string, side: "front" | "rear") => request<SaveResult>(
    `/repos/${targetId}/device-types/file/image?path=${encodeURIComponent(path)}`,
    { method: "DELETE", body: JSON.stringify({ side }) }
  ),
};

export interface ModuleTypeSummary {
  path: string;
  manufacturer?: string | null;
  model?: string | null;
  part_number?: string | null;
}

export interface ModuleTypeFile {
  repo_target_id: string;
  path: string;
  sha: string;
  payload: Record<string, any>;
  open_pr?: { number: number; url: string } | null;
  image_status: Record<string, "present" | "missing">;
}

export const moduleTypesApi = {
  list: (targetId: string) => request<ModuleTypeSummary[]>(`/repos/${targetId}/module-types`),
  get: (targetId: string, path: string) => request<ModuleTypeFile>(
    `/repos/${targetId}/module-types/file?path=${encodeURIComponent(path)}`
  ),
  create: (targetId: string, data: Record<string, any>) => request<SaveResult>(
    `/repos/${targetId}/module-types`, { method: "POST", body: JSON.stringify(data) }
  ),
  save: (targetId: string, path: string, data: Record<string, any>) => request<SaveResult>(
    `/repos/${targetId}/module-types/file?path=${encodeURIComponent(path)}`,
    { method: "PUT", body: JSON.stringify(data) }
  ),
  remove: (targetId: string, path: string, sha: string) => request<void>(
    `/repos/${targetId}/module-types/file?path=${encodeURIComponent(path)}`,
    { method: "DELETE", body: JSON.stringify({ sha }) }
  ),
  importYaml: (targetId: string, yaml_text: string) => request<SaveResult>(
    `/repos/${targetId}/module-types/import`, { method: "POST", body: JSON.stringify({ yaml_text }) }
  ),
  coverage: (targetId: string, path: string) => request<CoverageEntry[]>(
    `/repos/${targetId}/module-types/file/coverage?path=${encodeURIComponent(path)}`
  ),
  diff: (targetId: string, path: string, instance_ids: string[]) => request<InstanceDiffResult[]>(
    `/repos/${targetId}/module-types/file/diff-with-netbox?path=${encodeURIComponent(path)}`,
    { method: "POST", body: JSON.stringify({ instance_ids, tags: [], overwrite: false }) }
  ),
  push: (targetId: string, path: string, instance_ids: string[], overwrite: boolean) =>
    request<{ target: string; status: string; detail?: string }[]>(
      `/repos/${targetId}/module-types/file/push-to-netbox?path=${encodeURIComponent(path)}`,
      { method: "POST", body: JSON.stringify({ instance_ids, tags: [], overwrite }) }
    ),
  imagePreview: (targetId: string, path: string, side: "front" | "rear") =>
    request<ImagePreview>(`/repos/${targetId}/module-types/file/image-preview?path=${encodeURIComponent(path)}&side=${side}`),
  saveImage: (targetId: string, path: string, data: Record<string, any>) => request<SaveResult>(
    `/repos/${targetId}/module-types/file/image?path=${encodeURIComponent(path)}`,
    { method: "PUT", body: JSON.stringify(data) }
  ),
  removeImage: (targetId: string, path: string, side: "front" | "rear") => request<SaveResult>(
    `/repos/${targetId}/module-types/file/image?path=${encodeURIComponent(path)}`,
    { method: "DELETE", body: JSON.stringify({ side }) }
  ),
};

export interface RackTypeSummary {
  path: string;
  manufacturer?: string | null;
  model?: string | null;
  slug?: string | null;
  u_height?: number | null;
}

export interface RackTypeFile {
  repo_target_id: string;
  path: string;
  sha: string;
  payload: Record<string, any>;
  open_pr?: { number: number; url: string } | null;
}

export const rackTypesApi = {
  list: (targetId: string) => request<RackTypeSummary[]>(`/repos/${targetId}/rack-types`),
  get: (targetId: string, path: string) => request<RackTypeFile>(
    `/repos/${targetId}/rack-types/file?path=${encodeURIComponent(path)}`
  ),
  create: (targetId: string, data: Record<string, any>) => request<SaveResult>(
    `/repos/${targetId}/rack-types`, { method: "POST", body: JSON.stringify(data) }
  ),
  save: (targetId: string, path: string, data: Record<string, any>) => request<SaveResult>(
    `/repos/${targetId}/rack-types/file?path=${encodeURIComponent(path)}`,
    { method: "PUT", body: JSON.stringify(data) }
  ),
  remove: (targetId: string, path: string, sha: string) => request<void>(
    `/repos/${targetId}/rack-types/file?path=${encodeURIComponent(path)}`,
    { method: "DELETE", body: JSON.stringify({ sha }) }
  ),
  coverage: (targetId: string, path: string) => request<CoverageEntry[]>(
    `/repos/${targetId}/rack-types/file/coverage?path=${encodeURIComponent(path)}`
  ),
  diff: (targetId: string, path: string, instance_ids: string[]) => request<InstanceDiffResult[]>(
    `/repos/${targetId}/rack-types/file/diff-with-netbox?path=${encodeURIComponent(path)}`,
    { method: "POST", body: JSON.stringify({ instance_ids, tags: [], overwrite: false }) }
  ),
  push: (targetId: string, path: string, instance_ids: string[], overwrite: boolean) =>
    request<{ target: string; status: string; detail?: string }[]>(
      `/repos/${targetId}/rack-types/file/push-to-netbox?path=${encodeURIComponent(path)}`,
      { method: "POST", body: JSON.stringify({ instance_ids, tags: [], overwrite }) }
    ),
};

// ---------- Bulk import from a device-type library ----------

export interface BulkImportScanEntry {
  path: string;
  manufacturer_guess?: string | null;
  slug_guess?: string | null;
  model?: string | null;
  part_number?: string | null;
}

export interface DeviceTypePreview {
  manufacturer?: string | null;
  model?: string | null;
  slug?: string | null;
  part_number?: string | null;
  component_counts: Record<string, number>;
  custom_fields: Record<string, any>;
  image_status: Record<string, string>;
}

export interface BulkImportFailure {
  path: string;
  error: string;
}

export interface BulkImportResult {
  branch: string;
  pr_number?: number | null;
  pr_url?: string | null;
  imported: string[];
  skipped_existing: string[];
  failed: BulkImportFailure[];
}

export interface ImagePreview {
  side: "front" | "rear";
  filename: string;
  content_type: string;
  content_base64: string;
}

export interface SyslogSettings {
  enabled: boolean;
  protocol: string;
  host: string;
  port: number;
  facility: string;
  app_name: string;
}

export interface SyslogTestResult {
  ok: boolean;
  detail: string;
}

export const syslogApi = {
  getSettings: () => request<SyslogSettings>("/syslog/settings"),
  test: () => request<SyslogTestResult>("/syslog/test", { method: "POST" }),
};

export const bulkImportApi = {
  scan: (objectType: "device-types" | "module-types" | "rack-types", targetId: string, source_repo: string, source_branch: string, source_base_dir: string, source_pat?: string) =>
    request<BulkImportScanEntry[]>(`/repos/${targetId}/${objectType}/bulk-import/scan`, {
      method: "POST", body: JSON.stringify({ source_repo, source_branch, source_base_dir, source_pat }),
    }),

  preview: (objectType: "device-types" | "module-types" | "rack-types", targetId: string, source_repo: string, source_branch: string, path: string, source_pat?: string) =>
    request<DeviceTypePreview>(`/repos/${targetId}/${objectType}/bulk-import/preview`, {
      method: "POST", body: JSON.stringify({ source_repo, source_branch, source_pat, path }),
    }),

  imagePreview: (objectType: "device-types" | "module-types", targetId: string, data: {
    source_repo: string; source_branch: string; path: string; side: "front" | "rear"; source_pat?: string;
  }) => request<ImagePreview>(`/repos/${targetId}/${objectType}/bulk-import/image-preview`, {
    method: "POST", body: JSON.stringify(data),
  }),

  import: (objectType: "device-types" | "module-types" | "rack-types", targetId: string, data: {
    source_repo: string; source_branch: string; source_pat?: string; paths: string[];
    commit_message?: string; pr_title?: string; pr_body?: string;
  }) => request<BulkImportResult>(`/repos/${targetId}/${objectType}/bulk-import`, {
    method: "POST", body: JSON.stringify(data),
  }),
};

export interface NdxSearchEntry {
  vendor_slug: string;
  vendor_name: string;
  manufacturer: string;
  model: string;
  slug: string;
  part_number?: string | null;
  u_height?: number | null;
  source?: string | null;
}

export const ndxApi = {
  search: (targetId: string, query: string, limit = 200) => request<NdxSearchEntry[]>(
    `/repos/${targetId}/device-types/ndx/search`, { method: "POST", body: JSON.stringify({ query, limit }) }
  ),
  preview: (targetId: string, vendor_slug: string, slug: string) => request<DeviceTypePreview>(
    `/repos/${targetId}/device-types/ndx/preview`, { method: "POST", body: JSON.stringify({ vendor_slug, slug }) }
  ),
  import: (targetId: string, data: {
    selections: { vendor_slug: string; slug: string }[]; pr_title?: string;
  }) => request<BulkImportResult>(`/repos/${targetId}/device-types/ndx/import`, {
    method: "POST", body: JSON.stringify(data),
  }),
};

// ---------- Import device types from a NetBox instance into git ----------

export interface ImportFromNetboxScanEntry {
  manufacturer: string;
  model: string;
  slug: string;
  part_number?: string | null;
  u_height?: number | null;
}

export const importFromNetboxApi = {
  scan: (objectType: "device-types" | "module-types" | "rack-types", targetId: string, instance_id: string) =>
    request<ImportFromNetboxScanEntry[]>(`/repos/${targetId}/${objectType}/import-from-netbox/scan`, {
      method: "POST", body: JSON.stringify({ instance_id }),
    }),

  preview: (objectType: "device-types" | "module-types" | "rack-types", targetId: string, instance_id: string, manufacturer: string, identifier: string) =>
    request<DeviceTypePreview>(`/repos/${targetId}/${objectType}/import-from-netbox/preview`, {
      method: "POST", body: JSON.stringify(objectType === "module-types" ? { instance_id, manufacturer, model: identifier } : { instance_id, manufacturer, slug: identifier }),
    }),

  imagePreview: (objectType: "device-types" | "module-types", targetId: string, instance_id: string,
                 manufacturer: string, identifier: string, side: "front" | "rear") =>
    request<ImagePreview>(`/repos/${targetId}/${objectType}/import-from-netbox/image-preview`, {
      method: "POST",
      body: JSON.stringify(objectType === "module-types"
        ? { instance_id, manufacturer, model: identifier, side }
        : { instance_id, manufacturer, slug: identifier, side }),
    }),

  import: (objectType: "device-types" | "module-types" | "rack-types", targetId: string, data: {
    instance_id: string; selections: { manufacturer: string; slug?: string; model?: string }[];
    commit_message?: string; pr_title?: string; pr_body?: string;
  }) => request<BulkImportResult>(`/repos/${targetId}/${objectType}/import-from-netbox`, {
    method: "POST", body: JSON.stringify(data),
  }),
};

// ---------- Diff / drift ----------

export interface BaseFieldChange {
  field: string;
  source: any;
  netbox: any;
}

export interface FieldLevelChange {
  field: string;
  source: any;
  existing: any;
}

export interface ChangedItem {
  name: string;
  field_changes: FieldLevelChange[];
}

export interface ComponentChange {
  added: string[];
  removed: string[];
  changed: ChangedItem[];
}

export interface DiffResult {
  status: "in_sync" | "drift" | "missing";
  base_field_changes: BaseFieldChange[];
  component_changes: Record<string, ComponentChange>;
  image_changes: { side: string; status: string; detail?: string | null }[];
}

export interface InstanceDiffResult {
  instance_id: string;
  instance_name: string;
  diff?: DiffResult | null;
  error?: string | null;
}

export interface DriftRecord {
  id: string;
  instance_id: string;
  instance_name: string;
  repo_target_id: string;
  repo_target_name: string;
  kind: "device_type" | "custom_fields";
  file_path: string;
  status: string;
  diff?: DiffResult | CustomFieldsDiffResult | null;
  checked_at: string;
  last_full_check?: string | null;
  reused?: boolean | null;
  checks_since_full?: number | null;
}

export const driftApi = {
  list: () => request<DriftRecord[]>("/drift"),
  checkNow: () => request<DriftRecord[]>("/drift/check-now", { method: "POST" }),
};

// ---------- Audit log ----------

export interface AuditLogEntry {
  id: string;
  created_at: string;
  action_type: string; // "github" or "netbox"
  target_name: string;
  repo_target_id: string;
  repo_target_name: string;
  file_path: string;
  status: string;
  detail?: string | null;
  actor_sub?: string | null;
  actor_name?: string | null;
  actor_email?: string | null;
}

export const auditApi = {
  list: () => request<AuditLogEntry[]>("/audit"),
};

// ---------- Fleet visibility ----------

export interface TokenExpiryInfo {
  known: boolean;
  expires?: string | null;
  note?: string | null;
}

export interface InstanceHealth {
  instance_id: string;
  instance_name: string;
  reachable: boolean;
  netbox_version?: string | null;
  python_version?: string | null;
  plugins: Record<string, any>;
  response_time_ms?: number | null;
  error?: string | null;
  token_expiry: TokenExpiryInfo;
}

export interface GithubTokenStatus {
  target_id: string;
  target_name: string;
  token_expiry: TokenExpiryInfo;
}

export const fleetApi = {
  health: () => request<InstanceHealth[]>("/fleet/health"),
  githubTokenStatus: () => request<GithubTokenStatus[]>("/fleet/github-token-status"),
};

export interface CoverageEntry {
  instance_id: string;
  instance_name: string;
  status: string;
  error?: string | null;
  image_warnings: string[];
}

// ---------- Custom-fields template ----------

export interface CustomFieldsTemplateFile {
  repo_target_id: string;
  path: string;
  exists: boolean;
  sha?: string | null;
  // Custom fields use NetBox's canonical `object_types` key.
  payload: { custom_fields: Record<string, any>[]; custom_field_choice_sets: Record<string, any>[] };
  open_pr?: { number: number; url: string } | null;
}

export interface ImportCandidate {
  kind: "custom_field" | "choice_set";
  name: string;
  status: "missing" | "changed";
  payload: Record<string, any>;
  field_changes: FieldLevelChange[];
}

export interface CustomFieldsImportScanResult {
  template_exists: boolean;
  candidates: ImportCandidate[];
}

export interface NamedListDiff {
  missing_on_instance: string[];
  extra_on_instance: string[];
  changed: ChangedItem[];
}

export interface CustomFieldsDiffResult {
  status: string;
  custom_fields?: NamedListDiff | null;
  custom_field_choice_sets?: NamedListDiff | null;
}

export interface InstanceCustomFieldsDiffResult {
  instance_id: string;
  instance_name: string;
  diff?: CustomFieldsDiffResult | null;
  error?: string | null;
}

export interface ScopeTypePreview {
  object_type: string;
  raw_count: number;
  meaningful_count: number;
  sample: { id: number | string; display: string; url?: string | null; value: unknown }[];
  counting_method: "cf_empty_filter" | "client_side_pagination";
}

export interface ScopeReductionPreview {
  field_name: string;
  current_object_types: string[];
  proposed_object_types: string[];
  removed_object_types: string[];
  object_types: ScopeTypePreview[];
}

export interface InstanceScopePreview {
  instance_id: string;
  instance_name: string;
  netbox_version?: string | null;
  reductions: ScopeReductionPreview[];
  confirmation_text: string;
  backup_possible: boolean;
  backup?: Record<string, any> | null;
  confirmation_token?: string | null;
  error?: string | null;
}

export const customFieldsApi = {
  get: (targetId: string) => request<CustomFieldsTemplateFile>(`/repos/${targetId}/custom-fields/file`),

  save: (targetId: string, data: { payload: Record<string, any>; sha?: string; commit_message?: string; pr_body?: string }) =>
    request<SaveResult>(`/repos/${targetId}/custom-fields/file`, { method: "PUT", body: JSON.stringify(data) }),

  scanImport: (targetId: string, instance_id: string) =>
    request<CustomFieldsImportScanResult>(`/repos/${targetId}/custom-fields/import-scan`, {
      method: "POST", body: JSON.stringify({ instance_id }),
    }),

  importSelected: (targetId: string, data: {
    instance_id: string; selected: { kind: string; name: string }[]; commit_message?: string; pr_body?: string;
  }) => request<SaveResult>(`/repos/${targetId}/custom-fields/import`, {
    method: "POST", body: JSON.stringify(data),
  }),

  previewPush: (targetId: string, instance_ids: string[], tags: string[], overwrite: boolean, include_backup: boolean) =>
    request<InstanceScopePreview[]>(`/repos/${targetId}/custom-fields/push/preview`, {
      method: "POST", body: JSON.stringify({ instance_ids, tags, overwrite, include_backup }),
    }),

  push: (targetId: string, instance_ids: string[], tags: string[], overwrite: boolean,
    confirmations: Record<string, Record<string, any>> = {}) =>
    request<{ target: string; status: string; detail?: string; warnings?: string[] }[]>(`/repos/${targetId}/custom-fields/push`, {
      method: "POST", body: JSON.stringify({ instance_ids, tags, overwrite, confirmations }),
    }),

  restore: (targetId: string, instance_id: string, backup: Record<string, any>, dry_run: boolean) =>
    request<{ status: string; results: { object_type: string; record_id: number | string; status: string; detail?: string }[] }>(
      `/repos/${targetId}/custom-fields/restore`, {
        method: "POST", body: JSON.stringify({ instance_id, backup, dry_run }),
      }),

  diff: (targetId: string, instance_ids: string[], tags: string[]) =>
    request<InstanceCustomFieldsDiffResult[]>(`/repos/${targetId}/custom-fields/diff`, {
      method: "POST", body: JSON.stringify({ instance_ids, tags, overwrite: false }),
    }),
};

export interface ReferenceDataKind {
  label: string; fields: string[]; json_schema: Record<string, any>; file: string;
}
export interface ReferenceDataRegistry { push_order: string[]; kinds: Record<string, ReferenceDataKind>; }
export const referenceDataApi = {
  schema: (targetId: string) => request<ReferenceDataRegistry>(`/repos/${targetId}/reference-data/schema`),
  get: (targetId: string, kind: string) => request<{ exists: boolean; sha?: string; payload: { items: Record<string, any>[] } }>(`/repos/${targetId}/reference-data/${kind}/file`),
  save: (targetId: string, kind: string, data: Record<string, any>) => request<SaveResult>(`/repos/${targetId}/reference-data/${kind}/file`, { method: "PUT", body: JSON.stringify(data) }),
  push: (targetId: string, data: Record<string, any>) => request<{ target: string; status: string; detail?: string; warnings?: string[] }[]>(`/repos/${targetId}/reference-data/push`, { method: "POST", body: JSON.stringify(data) }),
};

// ---------- Data migration ----------

export interface MigrationType {
  type: string;
  label: string;
  dependencies: string[];
  optional_dependencies: string[];
  required_selectable_dependencies: string[];
  possible_optional_selectable_dependencies: string[];
}

export interface MigrationTenant {
  id: number;
  name: string;
  slug: string;
}

export interface MigrationMappingOverride {
  action: "map" | "skip" | "create";
  target_id?: number | null;
}

export interface MigrationPlanRequest {
  source_instance_id: string;
  target_instance_id: string;
  selected_types: string[];
  tenant_filter: string[];
  include_untenanted?: boolean;
  mapping_overrides?: Record<string, MigrationMappingOverride>;
  conflict_policy?: Record<string, string>;
  marker_tag?: boolean;
  fail_fast?: boolean;
  max_requests_per_second?: number;
  max_batch_size?: number;
  job_id?: string | null;
}

export type MigrationJobStatus =
  | "planning" | "planned" | "running" | "completed" | "completed_with_errors" | "failed" | "cancelled"
  | "rolling_back" | "rolled_back" | "rolled_back_with_errors";

export interface MigrationRollbackResult {
  job: MigrationJobSummary;
  detail: string;
  deleted: number;
  failed: number;
  untouched_mapped: number;
  untouched_updated: number;
}

export interface MigrationJobSummary {
  id: string;
  source_instance_id: string;
  target_instance_id: string;
  source_instance_name?: string | null;
  target_instance_name?: string | null;
  status: MigrationJobStatus;
  phase: "primary" | "patch" | "done";
  current_step?: string | null;
  tenant_filter: string[];
  selected_types: string[];
  totals: Record<string, Record<string, number>>;
  warnings: string[];
  created_at: string;
  started_at?: string | null;
  last_heartbeat_at?: string | null;
  finished_at?: string | null;
  actor_name?: string | null;
}

export interface MigrationMappingSkeletonRow {
  override_key: string;
  object_type: string;
  source_id: number;
  source_natural_key: string;
  auto_match: "matched" | "no_match" | "ambiguous";
  target_id?: number | null;
  target_natural_key?: string | null;
  match_detail?: string | null;
  action?: "map" | "skip" | "create" | null;
}

export interface MigrationTargetOption {
  id: number;
  label: string;
}

export interface MigrationPreflightSide {
  reachable: boolean;
  token_valid: boolean;
  netbox_version?: string | null;
  detail?: string | null;
  write_permission_checked?: boolean | null;
  write_permission_ok?: boolean | null;
}

export interface MigrationPreflightResult {
  source: MigrationPreflightSide;
  target: MigrationPreflightSide;
}

export const migrationsApi = {
  types: () => request<MigrationType[]>("/migrations/types"),
  tenants: (instanceId: string, query = "") => request<MigrationTenant[]>(
    `/migrations/instances/${instanceId}/tenants${query ? `?q=${encodeURIComponent(query)}` : ""}`,
  ),
  plan: (data: MigrationPlanRequest) =>
    request<MigrationJobSummary>("/migrations/plan", { method: "POST", body: JSON.stringify(data) }),
  jobs: () => request<MigrationJobSummary[]>("/migrations/jobs"),
  job: (id: string) => request<MigrationJobSummary>(`/migrations/jobs/${id}`),
  mappingSkeleton: (id: string) => request<MigrationMappingSkeletonRow[]>(`/migrations/jobs/${id}/mapping-skeleton`),
  targetOptions: (id: string, objectType: string, query = "") => request<MigrationTargetOption[]>(
    `/migrations/jobs/${id}/target-options?object_type=${encodeURIComponent(objectType)}${query ? `&q=${encodeURIComponent(query)}` : ""}`,
  ),
  preflight: (source_instance_id: string, target_instance_id: string) =>
    request<MigrationPreflightResult>("/migrations/preflight", {
      method: "POST", body: JSON.stringify({ source_instance_id, target_instance_id }),
    }),
  execute: (id: string) =>
    request<MigrationJobSummary>(`/migrations/jobs/${id}/execute`, {
      method: "POST", body: JSON.stringify({ confirm: true }),
    }),
  cancel: (id: string) => request<MigrationJobSummary>(`/migrations/jobs/${id}/cancel`, { method: "POST" }),
  retryFailed: (id: string) => request<MigrationJobSummary>(`/migrations/jobs/${id}/retry-failed`, { method: "POST" }),
  rollback: (id: string) => request<MigrationRollbackResult>(`/migrations/jobs/${id}/rollback`, {
    method: "POST", body: JSON.stringify({ confirm: true }),
  }),
  reportHtml: (id: string) => requestText(`/migrations/jobs/${id}/report`),
  reportUrl: (id: string) => `/api/migrations/jobs/${id}/report`,
  reportDownloadUrl: (id: string) => `/api/migrations/jobs/${id}/report?download=true`,
};

// ---------- Data Export ----------

export interface ExportSchemaField { key: string; label: string; }
export interface ExportCustomField { name: string; label: string; type: string; group_name: string; weight: number; }
export interface ExportSchemaType {
  key: string; label: string; fixed: ExportSchemaField[]; optional: ExportSchemaField[]; custom_fields: ExportCustomField[];
}
export interface ExportJob {
  id: string; instance_id: string | null; instance_name: string; tenant_id: number; tenant_name: string; tenant_slug: string;
  object_types: string[]; fields: Record<string, { optional: string[]; custom_fields: ExportCustomField[] }>;
  format: "csv" | "xlsx"; delimiter: string; status: "queued" | "running" | "completed" | "failed" | "cancelled" | "expired";
  progress: Record<string, { done: number; total: number }>; row_counts: Record<string, number>; error?: string | null;
  created_at: string; started_at?: string | null; finished_at?: string | null; expires_at?: string | null;
  file_name?: string | null; file_size?: number | null; download_name: string;
}
export interface ExportCreateRequest {
  instance_id: string; tenant_id: number; object_types: string[];
  fields: Record<string, { optional: string[]; custom_fields: string[] }>;
  format: "csv" | "xlsx"; delimiter: "," | ";";
}
export const exportsApi = {
  schema: (instanceId: string) => request<{ object_types: ExportSchemaType[] }>(`/exports/instances/${instanceId}/schema`),
  create: (body: ExportCreateRequest) => request<ExportJob>("/exports", { method: "POST", body: JSON.stringify(body) }),
  list: () => request<ExportJob[]>("/exports"),
  get: (id: string) => request<ExportJob>(`/exports/${id}`),
  cancel: (id: string) => request<ExportJob>(`/exports/${id}/cancel`, { method: "POST" }),
  remove: (id: string) => request<void>(`/exports/${id}`, { method: "DELETE" }),
  downloadUrl: (id: string) => `/api/exports/${id}/download`,
};
