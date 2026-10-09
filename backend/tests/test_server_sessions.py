from datetime import timedelta
from contextlib import contextmanager
from types import SimpleNamespace

from sqlalchemy import event

from app import models
from app.auth import (
    MAX_OIDC_GROUPS, create_session_cookie, delete_session_cookie,
    get_current_user_optional, normalize_groups,
)
from app.config import settings
from app.database import SessionLocal, engine, run_schema_migrations
from app.timeutil import utcnow


def request_with(token, *, state=False):
    request = SimpleNamespace(cookies={"nbm_session": token})
    if state:
        request.state = SimpleNamespace()
    return request


@contextmanager
def counted_statements():
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


run_schema_migrations()


def test_session_cookie_contains_only_random_identifier_and_can_be_revoked():
    token = create_session_cookie({"sub": "user-1", "name": "Person", "groups": ["ops"]})
    assert "user-1" not in token and "ops" not in token
    assert get_current_user_optional(request_with(token))["groups"] == ["ops"]
    db = SessionLocal()
    db.query(models.UserSession).filter(models.UserSession.sub == "user-1").delete()
    db.commit(); db.close()
    assert get_current_user_optional(request_with(token)) is None


def test_expired_session_is_rejected():
    token = create_session_cookie({"sub": "expired", "groups": []})
    db = SessionLocal(); row = db.query(models.UserSession).filter_by(sub="expired").one()
    row.expires_at = utcnow() - timedelta(seconds=1); db.commit(); db.close()
    assert get_current_user_optional(request_with(token)) is None


def test_oidc_group_claim_accepts_300_groups_and_caps_abusive_claims():
    groups = [f"group-{index}" for index in range(600)] + [17, None]
    normalized = normalize_groups(groups)
    assert normalized[:300] == groups[:300]
    assert len(normalized) == MAX_OIDC_GROUPS


def test_session_touch_is_written_at_most_once_inside_interval(monkeypatch):
    monkeypatch.setattr(settings, "session_touch_interval_seconds", 60)
    token = create_session_cookie({"sub": "touch-once", "groups": []})
    db = SessionLocal()
    row = db.query(models.UserSession).filter_by(sub="touch-once").one()
    row.last_seen_at = utcnow() - timedelta(seconds=61)
    db.commit(); db.close()

    with counted_statements() as statements:
        assert get_current_user_optional(request_with(token)) is not None
        assert get_current_user_optional(request_with(token)) is not None

    assert sum(statement.startswith("update user_sessions") for statement in statements) == 1


def test_session_is_touched_again_after_interval(monkeypatch):
    monkeypatch.setattr(settings, "session_touch_interval_seconds", 60)
    token = create_session_cookie({"sub": "touch-later", "groups": []})
    db = SessionLocal()
    row = db.query(models.UserSession).filter_by(sub="touch-later").one()
    row.last_seen_at = utcnow() - timedelta(seconds=61)
    db.commit(); db.close()

    with counted_statements() as statements:
        assert get_current_user_optional(request_with(token)) is not None

    assert sum(statement.startswith("update user_sessions") for statement in statements) == 1


def test_idle_timeout_and_logout_remain_immediate(monkeypatch):
    monkeypatch.setattr(settings, "session_idle_timeout_minutes", 1)
    token = create_session_cookie({"sub": "idle", "groups": []})
    db = SessionLocal()
    row = db.query(models.UserSession).filter_by(sub="idle").one()
    row.last_seen_at = utcnow() - timedelta(minutes=2)
    db.commit(); db.close()
    assert get_current_user_optional(request_with(token)) is None

    token = create_session_cookie({"sub": "logout", "groups": []})
    assert get_current_user_optional(request_with(token)) is not None
    delete_session_cookie(token)
    assert get_current_user_optional(request_with(token)) is None


def test_request_state_reuses_one_session_validation():
    token = create_session_cookie({"sub": "one-select", "groups": []})
    request = request_with(token, state=True)

    with counted_statements() as statements:
        assert get_current_user_optional(request) is not None  # middleware
        assert get_current_user_optional(request) is not None  # dependency
        assert get_current_user_optional(request) is not None  # actor helper

    session_selects = [
        statement for statement in statements
        if statement.startswith("select") and "from user_sessions" in statement
    ]
    assert len(session_selects) == 1
