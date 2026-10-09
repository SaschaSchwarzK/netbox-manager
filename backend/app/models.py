import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.timeutil import utcnow


def gen_uuid() -> str:
    return str(uuid.uuid4())


SCOPE_ALL = "*"
RESOURCE_TYPES = ("*", "instance", "github_target")

# MigrationJob.status
MIGRATION_JOB_STATUSES = (
    "planning", "planned", "running", "cancelling", "completed", "completed_with_errors", "failed", "cancelled",
    "rolling_back", "rolled_back", "rolled_back_with_errors",
)
# MigrationJob.phase
MIGRATION_JOB_PHASES = ("primary", "patch", "done")
# MigrationJobItem.planned_action / MigrationJobPatch have no "planned_action" of their own
MIGRATION_ITEM_ACTIONS = ("create", "update", "map", "skip", "ambiguous")
# MigrationJobItem.execution_status / MigrationJobPatch.execution_status
MIGRATION_EXECUTION_STATUSES = ("pending", "done", "error", "rolled_back", "rollback_error")


class AccessMapping(Base):
    """One row = one OIDC group -> (role, scope) grant.

    resource_type/resource_id == "*" means the grant applies to every resource ("global").
    role may be NULL for a visibility-only grant: the group may see the resource but gains no
    role from this row (it keeps the app default role).
    """
    __tablename__ = "access_mappings"
    __table_args__ = (
        UniqueConstraint(
            "oidc_group", "resource_type", "resource_id", name="uq_access_mapping"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    oidc_group: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False, default=SCOPE_ALL)
    resource_id: Mapped[str] = mapped_column(String(36), nullable=False, default=SCOPE_ALL)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class SeenOidcGroup(Base):
    """Every OIDC group observed at login, purely so the admin UI can autocomplete group names rather than guess-typing."""
    __tablename__ = "seen_oidc_groups"

    name: Mapped[str] = mapped_column(String(256), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class UserSession(Base):
    __tablename__ = "user_sessions"
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    sub: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    email: Mapped[str | None] = mapped_column(String(512), nullable=True)
    username: Mapped[str | None] = mapped_column(String(512), nullable=True)
    groups_json: Mapped[str] = mapped_column(Text, default="[]")
    local: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class NetboxInstance(Base):
    __tablename__ = "netbox_instances"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    ca_bundle_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags_csv: Mapped[str] = mapped_column(String(512), default="")
    requires_approved_pr: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    @property
    def tags(self) -> list[str]:
        return [t.strip() for t in self.tags_csv.split(",") if t.strip()]

    @tags.setter
    def tags(self, value: list[str]) -> None:
        self.tags_csv = ",".join(t.strip() for t in value if t.strip())

    @property
    def has_ca_bundle(self) -> bool:
        return bool(self.ca_bundle_pem)


class GithubTarget(Base):
    __tablename__ = "github_targets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    repo: Mapped[str] = mapped_column(String(256), nullable=False)  # "org/repo"
    branch: Mapped[str] = mapped_column(String(128), default="main")
    path_pattern: Mapped[str] = mapped_column(
        String(256), default="device-types/{manufacturer}/{slug}.yml"
    )
    module_path_pattern: Mapped[str] = mapped_column(
        String(256), default="module-types/{manufacturer}/{model}.yaml"
    )
    rack_path_pattern: Mapped[str] = mapped_column(
        String(256), default="rack-types/{manufacturer}/{model}.yaml"
    )
    custom_fields_path: Mapped[str] = mapped_column(String(256), default="custom-fields/template.yml")
    reference_data_path: Mapped[str] = mapped_column(String(256), default="reference-data")
    pat_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DriftRecord(Base):
    __tablename__ = "drift_records"
    __table_args__ = (Index("ix_drift_pair_path", "instance_id", "repo_target_id", "kind", "file_path"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    instance_id: Mapped[str] = mapped_column(String(36), ForeignKey("netbox_instances.id", ondelete="CASCADE"), nullable=False)
    repo_target_id: Mapped[str] = mapped_column(String(36), ForeignKey("github_targets.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="device_type")  # "device_type" or "custom_fields"
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)  # device-type file path, or the target's custom-fields template path
    status: Mapped[str] = mapped_column(String(32))  # in_sync / drift / missing / error
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DeviceTypePushHistory(Base):
    __tablename__ = "push_history"
    __table_args__ = (
        Index("ix_push_history_target_created", "repo_target_id", "created_at"),
        Index("ix_push_history_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    repo_target_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("github_targets.id", ondelete="SET NULL"), nullable=True
    )
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32))  # "netbox" or "github"
    target_name: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32))  # success/error
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class ExportJob(Base):
    __tablename__ = "export_jobs"
    __table_args__ = (
        Index("ix_export_jobs_owner_created", "owner_sub", "created_at"),
        Index("ix_export_jobs_status", "status"),
        Index("ix_export_jobs_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    instance_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("netbox_instances.id", ondelete="SET NULL"), nullable=True
    )
    instance_name: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tenant_name: Mapped[str] = mapped_column(String(255), nullable=False)
    tenant_slug: Mapped[str] = mapped_column(String(255), nullable=False)
    object_types_json: Mapped[str] = mapped_column(Text, default="[]")
    fields_json: Mapped[str] = mapped_column(Text, default="{}")
    format: Mapped[str] = mapped_column(String(8), nullable=False)
    delimiter: Mapped[str] = mapped_column(String(1), default=",")
    status: Mapped[str] = mapped_column(String(16), default="queued")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    progress_json: Mapped[str] = mapped_column(Text, default="{}")
    row_counts_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    download_name: Mapped[str] = mapped_column(String(255), nullable=False)


class MigrationJob(Base):
    """
    One instance-to-instance data migration run (plan, dry run, or real
    execution — all three share this same row and code path; see
    services/migration/planner.py and executor.py).

    Serves as this feature's own audit trail (who ran what, against which
    instances, when) rather than being folded into DeviceTypePushHistory:
    that table's repo_target_id is a required FK to github_targets, which a
    migration job has no equivalent of, and this app has no schema-migration
    tool to safely relax that constraint on already-deployed databases.
    Completion is still forwarded to syslog via services.syslog_client
    directly (see executor.py), for parity with the rest of the app's audit
    trail.
    """
    __tablename__ = "migration_jobs"
    __table_args__ = (Index("ix_migration_jobs_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    source_instance_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("netbox_instances.id", ondelete="SET NULL"), nullable=True
    )
    target_instance_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("netbox_instances.id", ondelete="SET NULL"), nullable=True
    )

    tenant_filter_json: Mapped[str] = mapped_column(Text, default="[]")  # list[str] of tenant slugs
    selected_types_json: Mapped[str] = mapped_column(Text, default="[]")  # list[str], as chosen by the user
    resolved_types_json: Mapped[str] = mapped_column(Text, default="[]")  # list[str], after dependency closure, in order
    conflict_policy_json: Mapped[str] = mapped_column(Text, default="{}")  # {"default": "skip", "dcim.device": "update", ...}
    mapping_json: Mapped[str] = mapped_column(Text, default="{}")  # {"dcim.site:12": {"action": "map", "target_id": 3}, ...}
    options_json: Mapped[str] = mapped_column(Text, default="{}")  # marker tag on/off, include-untenanted, fail-fast, page size, rps, ...

    status: Mapped[str] = mapped_column(String(32), default="planned")
    phase: Mapped[str] = mapped_column(String(16), default="primary")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    current_step: Mapped[str | None] = mapped_column(String(512), nullable=True)

    totals_json: Mapped[str] = mapped_column(Text, default="{}")  # per-type create/update/map/skip/error counts, refreshed as it runs
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")  # list[str], collected at plan time (dropped custom fields, version notes, ...)
    api_stats_json: Mapped[str] = mapped_column(Text, default="{}")  # RateLimitStats snapshot for source + target, for the report

    actor_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resume_count: Mapped[int] = mapped_column(Integer, default=0)
    last_resumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MigrationJobItem(Base):
    """
    One row per source object considered by a migration job, written in
    full at plan time (including `payload_json`, a SNAPSHOT of the sanitized
    create/update payload as of planning — see the plan discussion on
    snapshot-at-plan-time semantics). `execution_status`/`target_id` start
    filled in for skip/map rows (there's nothing left to execute) and get
    filled in during execution for create/update rows.

    This table IS the id map: rebuilding source-id -> target-id for a given
    type is just "every row of that type with execution_status='done'".
    Resuming a crashed job means reloading that map and continuing from the
    first still-`pending` row in `order_index` order — see executor.py.
    """
    __tablename__ = "migration_job_items"
    __table_args__ = (
        UniqueConstraint("job_id", "object_type", "source_id", name="uq_migration_job_item"),
        Index("ix_migration_items_execution", "job_id", "execution_status", "order_index"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("migration_jobs.id"), nullable=False, index=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)  # fixed at plan time; topological + depth order

    object_type: Mapped[str] = mapped_column(String(64), nullable=False)  # registry key, e.g. "dcim.device"
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_natural_key: Mapped[str] = mapped_column(String(512), default="")  # human-readable, for the report (e.g. "core-sw-1 @ AMS-1")

    planned_action: Mapped[str] = mapped_column(String(16), nullable=False)  # create/update/map/skip/ambiguous
    match_detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # matcher's `detail`, e.g. ambiguity explanation

    # Plan-time snapshot, split so FK resolution can be safely redone at
    # execution time against real ids rather than the placeholder ids
    # build_plan() uses for "will be created" dependencies (see planner.py's
    # module docstring). payload_json holds only non-FK, non-custom fields —
    # final as-is, safe to reuse verbatim. fk_refs_json is {field_name:
    # source_fk_id} for `field_map` (required/optional, non-deferred)
    # dependencies; the executor re-resolves this against the REAL id_map
    # immediately before the create/update call, which is always possible by
    # then (topological order guarantees the dependency was itself already
    # processed for real). deferred_fk_json is the same shape but for
    # `deferred_field_map` fields (primary_ip4, virtual chassis master, ...)
    # — these are NEVER attempted at create/update time, always via a
    # MigrationJobPatch instead.
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    fk_refs_json: Mapped[str] = mapped_column(Text, default="{}")
    deferred_fk_json: Mapped[str] = mapped_column(Text, default="{}")
    dropped_custom_fields_json: Mapped[str] = mapped_column(Text, default="[]")  # list[str], for the report's warnings

    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # known immediately for map; filled in on create
    target_natural_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    execution_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/done/error/rolled_back/rollback_error
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MigrationJobPatch(Base):
    """
    One second-pass FK fix-up, for the fields build_payload() had to defer
    because the object they reference hadn't been created/mapped yet at the
    time this row's parent MigrationJobItem was processed (the circular-
    reference case: a device's primary_ip4, a virtual chassis' master, ...).
    Executed in phase `patch`, after every MigrationJobItem in phase
    `primary` has reached a terminal execution_status.
    """
    __tablename__ = "migration_job_patches"
    __table_args__ = (Index("ix_migration_patches_execution", "job_id", "execution_status", "order_index"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("migration_jobs.id"), nullable=False, index=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)

    object_type: Mapped[str] = mapped_column(String(64), nullable=False)  # the type being patched, e.g. "dcim.device"
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)  # source object whose target counterpart gets patched

    patch_fields_json: Mapped[str] = mapped_column(Text, default="{}")  # {field_name: source_fk_id}, resolved through the id map at execution time
    # Polymorphic FK fields: {field_name: {"type": dep_type_key, "id": source_fk_id}}.
    # The type is determined at plan time from the discriminator field; resolution happens at patch time.
    polymorphic_patch_fields_json: Mapped[str] = mapped_column(Text, default="{}")

    execution_status: Mapped[str] = mapped_column(String(16), default="pending")
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
