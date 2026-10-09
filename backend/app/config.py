from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="NBM_", extra="ignore")
    # Fernet key used to encrypt NetBox tokens / GitHub PATs at rest.
    # Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    secret_key: str = "CHANGE_ME_GENERATE_A_FERNET_KEY"
    cors_origins: list[str] = ["http://localhost:5173", "https://localhost:8443"]
    drift_check_interval_hours: int = 6
    drift_workers: int = 8
    drift_per_instance_workers: int = 2
    drift_full_recheck_every: int = 10
    drift_bulk_component_reads: bool = False
    drift_use_changelog: bool = False
    drift_changelog_margin_seconds: int = 300
    search_limit_per_type: int = 50
    search_timeout_seconds: float = 15
    fleet_cache_seconds: int = 30
    fleet_timeout_seconds: float = 15
    migration_auto_resume: bool = True
    migration_max_auto_resumes: int = 3
    enable_api_docs: bool = False
    audit_retention_days: int = 365
    log_format: str = "human"
    request_timing: bool = True
    slow_request_ms: int = 2000
    session_max_age_hours: int = 12
    session_idle_timeout_minutes: int = 60
    session_touch_interval_seconds: int = 60
    archive_max_mib: int = 48
    archive_lock_timeout_seconds: float = 120
    database_path: str = "/app/data/netbox_manager.db"
    export_dir: str = "/app/exports"
    export_retention_days: int = 7
    export_max_concurrent_jobs: int = 2
    export_max_active_jobs_per_user: int = 3
    export_max_requests_per_second: float = 8
    export_max_rows_per_type: int = 1_000_000
    export_min_free_mib: int = 200

    # Authentication is fail-closed. This deliberately has no NBM_ prefix:
    # AUTHENTICATION_DISABLED=True is the only way to disable authentication and authorization.
    authentication_disabled: bool = Field(default=False, validation_alias="AUTHENTICATION_DISABLED")

    # OIDC
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = "https://localhost:8443/api/auth/callback"
    oidc_scopes: str = "openid profile email groups"
    oidc_groups_claim: str = "groups"  # the ID token claim holding the user's group memberships
    session_secret_key: str = "CHANGE_ME_GENERATE_A_RANDOM_SECRET"
    session_cookie_secure: bool = True
    local_admin_user: str = ""
    local_admin_password: str = ""
    default_role: str = "viewer"  # role for an authenticated user whose groups match no access mapping role
    # Comma-separated OIDC group name(s) that always resolve to admin, regardless of what's in the
    # access mappings. This is the group reference that tells NetBox Manager who's allowed to
    # manage NetBox Manager itself (instances, GitHub targets, and access mappings) —
    # set it to your real admin/network-ops group and leave it set permanently. It also solves the
    # chicken-and-egg problem of getting a first admin in: without it, nobody could ever create the
    # first access mapping, since creating one requires already being admin.
    bootstrap_admin_groups: str = ""

    @field_validator("authentication_disabled", mode="before")
    @classmethod
    def authentication_is_disabled_only_by_explicit_true(cls, value) -> bool:
        # Fail closed for missing, false, malformed, or differently-cased values.
        return value is True or value == "True"

    @property
    def local_admin_enabled(self) -> bool:
        return bool(self.local_admin_user.strip() and self.local_admin_password.strip())

    @property
    def oidc_enabled(self) -> bool:
        return bool(
            self.oidc_issuer.strip()
            and self.oidc_client_id.strip()
            and self.oidc_client_secret.strip()
        )

    # Syslog forwarding of the audit log — config-file only, deliberately not
    # editable from the UI (the UI shows these values read-only plus a test button).
    syslog_enabled: bool = False
    syslog_protocol: str = "udp"  # "udp" or "tcp"
    syslog_host: str = ""
    syslog_port: int = 514
    syslog_facility: str = "local0"  # standard syslog facility keyword, see RFC 5424 / RFC 3164
    syslog_app_name: str = "netbox-manager"

settings = Settings()


def validate_startup_security(config: Settings = settings) -> None:
    if config.session_idle_timeout_minutes * 60 <= config.session_touch_interval_seconds:
        raise RuntimeError(
            "NBM_SESSION_IDLE_TIMEOUT_MINUTES must be greater than "
            "NBM_SESSION_TOUCH_INTERVAL_SECONDS."
        )
    if config.authentication_disabled:
        return
    if (
        config.session_secret_key == "CHANGE_ME_GENERATE_A_RANDOM_SECRET"
        or len(config.session_secret_key) < 32
    ):
        raise RuntimeError(
            "NBM_SESSION_SECRET_KEY must be changed from the placeholder and contain at least 32 characters."
        )
