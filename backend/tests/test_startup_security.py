import pytest
from cryptography.fernet import Fernet
import os
import subprocess
import sys

from app.config import Settings, startup_configuration_errors, validate_startup_security


def settings(**overrides):
    values = {
        "secret_key": Fernet.generate_key().decode(),
        "authentication_disabled": False,
        "local_admin_user": "admin",
        "local_admin_password": "test-password",
        **overrides,
    }
    return Settings.model_construct(**values)


@pytest.mark.parametrize("secret", ["CHANGE_ME_GENERATE_A_RANDOM_SECRET", "too-short"])
def test_auth_enabled_rejects_default_or_short_session_secret(secret):
    with pytest.raises(RuntimeError, match="NBM_SESSION_SECRET_KEY"):
        validate_startup_security(settings(session_secret_key=secret))


def test_auth_enabled_accepts_strong_session_secret():
    validate_startup_security(settings(session_secret_key="x" * 32))


def test_auth_disabled_keeps_session_secret_escape_hatch():
    validate_startup_security(settings(authentication_disabled=True))


def test_idle_timeout_must_exceed_session_touch_interval():
    with pytest.raises(RuntimeError, match="NBM_SESSION_IDLE_TIMEOUT_MINUTES"):
        validate_startup_security(settings(
            authentication_disabled=True,
            session_idle_timeout_minutes=1,
            session_touch_interval_seconds=60,
        ))


def test_authentication_disabled_uses_unprefixed_environment(monkeypatch):
    monkeypatch.setenv("AUTHENTICATION_DISABLED", "True")
    monkeypatch.delenv("NBM_AUTHENTICATION_DISABLED", raising=False)
    assert Settings(_env_file=None).authentication_disabled is True


def test_database_path_is_not_environment_configurable(monkeypatch):
    monkeypatch.setenv("NBM_DATABASE_PATH", "/tmp/attacker-controlled.db")
    value = Settings(_env_file=None)
    assert not hasattr(value, "database_path")


def test_invalid_fernet_key_fails_closed(monkeypatch):
    import importlib
    import app.config as config
    import app.crypto as crypto
    valid_key = config.settings.secret_key
    monkeypatch.setattr(config.settings, "secret_key", "not-a-fernet-key")
    importlib.reload(crypto)
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        crypto.encrypt("secret")
    monkeypatch.setattr(config.settings, "secret_key", valid_key)
    importlib.reload(crypto)


def test_configuration_errors_report_all_problems_without_secrets():
    value = settings(
        secret_key="bad",
        session_secret_key="short",
        oidc_issuer="https://issuer.example",
        oidc_client_id="",
        oidc_client_secret="top-secret",
        local_admin_user="admin",
        local_admin_password="",
    )
    errors = startup_configuration_errors(value)
    assert len(errors) == 5
    assert any("NBM_SECRET_KEY" in error for error in errors)
    assert any("OIDC configuration is incomplete" in error for error in errors)
    assert all("top-secret" not in error for error in errors)


def test_invalid_configuration_keeps_api_process_importable():
    env = os.environ.copy()
    env.update({
        "NBM_SECRET_KEY": "invalid",
        "NBM_SESSION_SECRET_KEY": "x" * 32,
        "NBM_LOCAL_ADMIN_USER": "admin",
        "NBM_LOCAL_ADMIN_PASSWORD": "test-password",
    })
    for key in ("NBM_OIDC_ISSUER", "NBM_OIDC_CLIENT_ID", "NBM_OIDC_CLIENT_SECRET"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-c", "import app.main; print(app.main.health()['status'])"],
        cwd="backend", env=env, capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "degraded" in result.stdout
