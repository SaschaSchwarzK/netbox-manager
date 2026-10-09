import pytest

from app.config import Settings, validate_startup_security


def settings(**overrides):
    values = {
        "secret_key": "unused-for-this-check",
        "authentication_disabled": False,
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


def test_invalid_fernet_key_fails_closed(monkeypatch):
    import importlib
    import app.config as config
    import app.crypto as crypto
    valid_key = config.settings.secret_key
    monkeypatch.setattr(config.settings, "secret_key", "not-a-fernet-key")
    with pytest.raises(RuntimeError, match="valid Fernet key"):
        importlib.reload(crypto)
    monkeypatch.setattr(config.settings, "secret_key", valid_key)
    importlib.reload(crypto)
