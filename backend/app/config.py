from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:////app/data/netbox_manager.db"
    # Fernet key used to encrypt NetBox tokens / GitHub PATs at rest.
    # Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    secret_key: str = "CHANGE_ME_GENERATE_A_FERNET_KEY"
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080"]
    drift_check_interval_hours: int = 6

    # OIDC — leave oidc_issuer empty to run with auth disabled (local/dev only).
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = "http://localhost:8080/api/auth/callback"
    oidc_scopes: str = "openid profile email groups"
    oidc_groups_claim: str = "groups"  # the ID token claim holding the user's group memberships
    session_secret_key: str = "CHANGE_ME_GENERATE_A_RANDOM_SECRET"
    session_cookie_secure: bool = False  # set True once served over HTTPS

    class Config:
        env_file = ".env"
        env_prefix = "NBM_"


settings = Settings()
