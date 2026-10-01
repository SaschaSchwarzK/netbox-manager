const BASE = "/api";

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`${resp.status}: ${body}`);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json();
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
}

export interface NetboxInstanceUpdate {
  name?: string;
  base_url?: string;
  api_token?: string;
  verify_ssl?: boolean;
  description?: string;
  tags?: string[];
  requires_approved_pr?: boolean;
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
  custom_fields_path: string;
  created_at: string;
}

export interface GithubTargetCreate {
  name: string;
  repo: string;
  branch: string;
  path_pattern: string;
  custom_fields_path?: string;
  pat: string;
}

export interface GithubTargetUpdate {
  name?: string;
  repo?: string;
  branch?: string;
  path_pattern?: string;
  custom_fields_path?: string;
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
  scan: (targetId: string, source_repo: string, source_branch: string, source_base_dir: string, source_pat?: string) =>
    request<BulkImportScanEntry[]>(`/repos/${targetId}/device-types/bulk-import/scan`, {
      method: "POST", body: JSON.stringify({ source_repo, source_branch, source_base_dir, source_pat }),
    }),

  preview: (targetId: string, source_repo: string, source_branch: string, path: string, source_pat?: string) =>
    request<DeviceTypePreview>(`/repos/${targetId}/device-types/bulk-import/preview`, {
      method: "POST", body: JSON.stringify({ source_repo, source_branch, source_pat, path }),
    }),

  import: (targetId: string, data: {
    source_repo: string; source_branch: string; source_pat?: string; paths: string[];
    commit_message?: string; pr_title?: string; pr_body?: string;
  }) => request<BulkImportResult>(`/repos/${targetId}/device-types/bulk-import`, {
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
  scan: (targetId: string, instance_id: string) =>
    request<ImportFromNetboxScanEntry[]>(`/repos/${targetId}/device-types/import-from-netbox/scan`, {
      method: "POST", body: JSON.stringify({ instance_id }),
    }),

  preview: (targetId: string, instance_id: string, manufacturer: string, slug: string) =>
    request<DeviceTypePreview>(`/repos/${targetId}/device-types/import-from-netbox/preview`, {
      method: "POST", body: JSON.stringify({ instance_id, manufacturer, slug }),
    }),

  import: (targetId: string, data: {
    instance_id: string; selections: { manufacturer: string; slug: string }[];
    commit_message?: string; pr_title?: string; pr_body?: string;
  }) => request<BulkImportResult>(`/repos/${targetId}/device-types/import-from-netbox`, {
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
    request<{ target: string; status: string; detail?: string }[]>(`/repos/${targetId}/custom-fields/push`, {
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

// ---------- Data migration ----------

export interface MigrationType {
  type: string;
  label: string;
  dependencies: string[];
  optional_dependencies: string[];
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
  max_requests_per_second?: number;
  job_id?: string | null;
}

export type MigrationJobStatus =
  | "planned" | "running" | "completed" | "completed_with_errors" | "failed" | "cancelled";

export interface MigrationJobSummary {
  id: string;
  source_instance_id: string;
  target_instance_id: string;
  source_instance_name?: string | null;
  target_instance_name?: string | null;
  status: MigrationJobStatus;
  phase: "primary" | "patch" | "done";
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

export const migrationsApi = {
  types: () => request<MigrationType[]>("/migrations/types"),
  plan: (data: MigrationPlanRequest) =>
    request<MigrationJobSummary>("/migrations/plan", { method: "POST", body: JSON.stringify(data) }),
  jobs: () => request<MigrationJobSummary[]>("/migrations/jobs"),
  job: (id: string) => request<MigrationJobSummary>(`/migrations/jobs/${id}`),
  execute: (id: string) =>
    request<MigrationJobSummary>(`/migrations/jobs/${id}/execute`, {
      method: "POST", body: JSON.stringify({ confirm: true }),
    }),
  cancel: (id: string) => request<MigrationJobSummary>(`/migrations/jobs/${id}/cancel`, { method: "POST" }),
  retryFailed: (id: string) => request<MigrationJobSummary>(`/migrations/jobs/${id}/retry-failed`, { method: "POST" }),
  reportUrl: (id: string) => `/api/migrations/jobs/${id}/report`,
};
