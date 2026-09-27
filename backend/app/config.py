from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Fernet key used to encrypt NetBox tokens / GitHub PATs at rest.
    # Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    secret_key: str = "CHANGE_ME_GENERATE_A_FERNET_KEY"
    cors_origins: list[str] = ["http://localhost:5173", "https://localhost:8443"]
    drift_check_interval_hours: int = 6

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

    class Config:
        env_file = ".env"
        env_prefix = "NBM_"


settings = Settings()
