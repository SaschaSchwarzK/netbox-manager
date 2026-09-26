from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


# ---------- NetBox instances ----------

class NetboxInstanceCreate(BaseModel):
    name: str
    base_url: str
    api_token: str
    verify_ssl: bool = True
    description: Optional[str] = None
    tags: list[str] = []
    requires_approved_pr: bool = False


class NetboxInstanceUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_token: Optional[str] = None  # only sent when the user wants to rotate it
    verify_ssl: Optional[bool] = None
    description: Optional[str] = None
    tags: Optional[list[str]] = None
    requires_approved_pr: Optional[bool] = None


class InstanceTestRequest(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_token: Optional[str] = None
    verify_ssl: Optional[bool] = None
    description: Optional[str] = None
    tags: Optional[list[str]] = None
    requires_approved_pr: Optional[bool] = None


class NetboxInstanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    base_url: str
    verify_ssl: bool
    description: Optional[str] = None
    tags: list[str] = []
    requires_approved_pr: bool = False
    created_at: datetime
    updated_at: datetime
    # api_token is intentionally never returned


class ConnectionTestResult(BaseModel):
    ok: bool
    netbox_version: Optional[str] = None
    detail: Optional[str] = None


# ---------- GitHub targets ----------

class GithubTargetCreate(BaseModel):
    name: str
    repo: str
    branch: str = "main"
    path_pattern: str = "device-types/{manufacturer}/{slug}.yml"
    custom_fields_path: str = "custom-fields/template.yml"
    pat: str


class GithubTargetUpdate(BaseModel):
    name: Optional[str] = None
    repo: Optional[str] = None
    branch: Optional[str] = None
    path_pattern: Optional[str] = None
    custom_fields_path: Optional[str] = None
    pat: Optional[str] = None


class GithubTargetTestRequest(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    repo: Optional[str] = None
    branch: Optional[str] = None
    path_pattern: Optional[str] = None
    custom_fields_path: Optional[str] = None
    pat: Optional[str] = None


class GithubTargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    repo: str
    branch: str
    path_pattern: str
    custom_fields_path: str
    created_at: datetime


# ---------- Device types (stored as YAML files in a GitHub repo) ----------

class OpenPrInfo(BaseModel):
    number: int
    url: str


class DeviceTypeFileOut(BaseModel):
    repo_target_id: str
    path: str
    sha: str
    payload: dict[str, Any]
    open_pr: Optional[OpenPrInfo] = None


class DeviceTypeSummary(BaseModel):
    path: str
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    slug: Optional[str] = None


class CreateDeviceTypeRequest(BaseModel):
    manufacturer: str
    model: str
    slug: str
    payload: dict[str, Any] = {}
    commit_message: Optional[str] = None
    pr_body: Optional[str] = None


class SaveDeviceTypeRequest(BaseModel):
    payload: dict[str, Any]
    sha: Optional[str] = None
    commit_message: Optional[str] = None
    pr_body: Optional[str] = None


class ImportYamlRequest(BaseModel):
    yaml_text: str
    commit_message: Optional[str] = None
    pr_body: Optional[str] = None


class DeleteDeviceTypeRequest(BaseModel):
    sha: str
    commit_message: Optional[str] = None


class PushToNetboxRequest(BaseModel):
    instance_ids: list[str] = []
    tags: list[str] = []  # bulk-select: also push to every instance carrying any of these tags
    overwrite: bool = False


class PushResultItem(BaseModel):
    target: str
    status: str
    detail: Optional[str] = None


# ---------- Diff / drift ----------

class BaseFieldChange(BaseModel):
    field: str
    source: Any = None
    netbox: Any = None


class FieldLevelChange(BaseModel):
    field: str
    source: Any = None
    existing: Any = None


class ChangedItem(BaseModel):
    name: str
    field_changes: list[FieldLevelChange] = []


class ComponentChange(BaseModel):
    added: list[str] = []
    removed: list[str] = []
    changed: list[ChangedItem] = []


class DiffResult(BaseModel):
    status: str  # in_sync / drift / missing
    base_field_changes: list[BaseFieldChange] = []
    component_changes: dict[str, ComponentChange] = {}


class InstanceDiffResult(BaseModel):
    instance_id: str
    instance_name: str
    diff: Optional[DiffResult] = None
    error: Optional[str] = None


class DriftRecordOut(BaseModel):
    id: str
    instance_id: str
    instance_name: str
    repo_target_id: str
    repo_target_name: str
    file_path: str
    status: str
    diff: Optional[DiffResult] = None
    checked_at: datetime


# ---------- Audit log ----------

class AuditLogEntryOut(BaseModel):
    id: str
    created_at: datetime
    action_type: str  # "netbox" or "github"
    target_name: str
    repo_target_id: str
    repo_target_name: str
    file_path: str
    status: str
    detail: Optional[str] = None
    actor_sub: Optional[str] = None
    actor_name: Optional[str] = None
    actor_email: Optional[str] = None


# ---------- Fleet visibility ----------

class TokenExpiryInfo(BaseModel):
    known: bool
    expires: Optional[str] = None
    note: Optional[str] = None


class InstanceHealthOut(BaseModel):
    instance_id: str
    instance_name: str
    reachable: bool
    netbox_version: Optional[str] = None
    python_version: Optional[str] = None
    plugins: dict[str, Any] = {}
    response_time_ms: Optional[int] = None
    error: Optional[str] = None
    token_expiry: TokenExpiryInfo


class GithubTokenStatusOut(BaseModel):
    target_id: str
    target_name: str
    token_expiry: TokenExpiryInfo


class CoverageEntry(BaseModel):
    instance_id: str
    instance_name: str
    status: str  # in_sync / drift / missing / error
    error: Optional[str] = None


# ---------- Bulk import from a device-type library ----------

class BulkImportScanRequest(BaseModel):
    source_repo: str  # "owner/repo", e.g. netbox-community/devicetype-library
    source_branch: str = "main"
    source_base_dir: str = "device-types"
    source_pat: Optional[str] = None  # falls back to this target's own PAT if omitted


class BulkImportScanEntry(BaseModel):
    path: str
    manufacturer_guess: Optional[str] = None
    slug_guess: Optional[str] = None


class BulkImportRequest(BaseModel):
    source_repo: str
    source_branch: str = "main"
    source_pat: Optional[str] = None
    paths: list[str]  # source paths selected to import
    commit_message: Optional[str] = None
    pr_title: Optional[str] = None
    pr_body: Optional[str] = None


class BulkImportFailure(BaseModel):
    path: str
    error: str


class BulkImportResult(BaseModel):
    branch: str
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    imported: list[str] = []
    skipped_existing: list[str] = []
    failed: list[BulkImportFailure] = []


# ---------- Import device types from a NetBox instance into git ----------

class ImportFromNetboxScanRequest(BaseModel):
    instance_id: str


class ImportFromNetboxScanEntry(BaseModel):
    manufacturer: str
    model: str
    slug: str
    u_height: Optional[float] = None


class DeviceTypeKey(BaseModel):
    manufacturer: str
    slug: str


class ImportFromNetboxRequest(BaseModel):
    instance_id: str
    selections: list[DeviceTypeKey]
    commit_message: Optional[str] = None
    pr_title: Optional[str] = None
    pr_body: Optional[str] = None


# ---------- Syslog forwarding (read-only, config-file driven) ----------

class SyslogSettingsOut(BaseModel):
    enabled: bool
    protocol: str
    host: str
    port: int
    facility: str
    app_name: str


class SyslogTestResult(BaseModel):
    ok: bool
    detail: str


# ---------- Access control (RBAC) ----------

class LocalLoginRequest(BaseModel):
    username: str
    password: str


class AccessMappingCreate(BaseModel):
    oidc_group: str
    role: Optional[str] = None
    resource_type: str = "*"
    resource_id: str = "*"


class AccessMappingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    oidc_group: str
    role: Optional[str] = None
    resource_type: str
    resource_id: str
    resource_name: str  # resolved for display, since the UI shouldn't have to cross-reference IDs itself
    created_at: datetime
    updated_at: datetime


class CurrentAccessOut(BaseModel):
    role: str
    groups: list[str]
    scoping_active: bool
    app_admin: bool
    editable: dict[str, list[str]]


# ---------- Custom-fields template ----------

class CustomFieldsTemplateOut(BaseModel):
    repo_target_id: str
    path: str
    exists: bool  # False when the file hasn't been created in the repo yet
    sha: Optional[str] = None
    payload: dict[str, Any]
    open_pr: Optional[OpenPrInfo] = None


class SaveCustomFieldsRequest(BaseModel):
    payload: dict[str, Any]
    sha: Optional[str] = None
    commit_message: Optional[str] = None
    pr_body: Optional[str] = None


class CustomFieldsImportScanRequest(BaseModel):
    instance_id: str


class ImportCandidate(BaseModel):
    kind: str  # "custom_field" or "choice_set"
    name: str
    status: str  # "missing" (not in template at all) or "changed" (in template but differs)
    payload: dict[str, Any]  # the instance's current definition, used if this item is selected for import
    field_changes: list[FieldLevelChange] = []  # only populated when status == "changed"


class CustomFieldsImportScanResult(BaseModel):
    template_exists: bool
    candidates: list[ImportCandidate]


class ImportCandidateKey(BaseModel):
    kind: str
    name: str


class ImportCustomFieldsSelectionRequest(BaseModel):
    instance_id: str
    selected: list[ImportCandidateKey]
    commit_message: Optional[str] = None
    pr_body: Optional[str] = None


class PushCustomFieldsRequest(BaseModel):
    instance_ids: list[str] = []
    tags: list[str] = []
    overwrite: bool = False


class NamedListDiff(BaseModel):
    missing_on_instance: list[str] = []
    extra_on_instance: list[str] = []
    changed: list[ChangedItem] = []


class CustomFieldsDiffResult(BaseModel):
    status: str  # in_sync / drift / error
    custom_fields: Optional[NamedListDiff] = None
    custom_field_choice_sets: Optional[NamedListDiff] = None


class InstanceCustomFieldsDiffResult(BaseModel):
    instance_id: str
    instance_name: str
    diff: Optional[CustomFieldsDiffResult] = None
    error: Optional[str] = None


# ---------- Cross-instance search ----------

class SearchResultItem(BaseModel):
    id: int
    name: str
    serial: Optional[str] = None
    type_display: Optional[str] = None
    site: Optional[str] = None
    status: Optional[str] = None
    url: str


class InstanceSearchResult(BaseModel):
    instance_id: str
    instance_name: str
    devices: list[SearchResultItem] = []
    virtual_machines: list[SearchResultItem] = []
    virtual_device_contexts: list[SearchResultItem] = []
    ip_addresses: list[SearchResultItem] = []
    prefixes: list[SearchResultItem] = []
    mac_addresses: list[SearchResultItem] = []
    error: Optional[str] = None


class SearchResponse(BaseModel):
    query: str
    results: list[InstanceSearchResult]


class SaveResult(BaseModel):
    path: str
    sha: str
    pr_number: int
    pr_url: str
