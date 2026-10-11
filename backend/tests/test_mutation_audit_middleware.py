from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app import main


def _client(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'audit.db'}", connect_args={"check_same_thread": False}
    )
    models.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(main, "get_current_user_optional", lambda request: {
        "sub": "local:admin", "name": "local admin (admin)", "email": None, "local": True,
    })

    application = FastAPI()
    application.add_middleware(main.MutationAuditMiddleware)

    @application.api_route("/api/widgets/{widget_id}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def widget(widget_id: str, request: Request):
        if request.method == "POST":
            request.state.nbm_audit_event = {
                "action": "create_widget", "resource_type": "widget",
                "resource_id": widget_id, "detail": f"Created widget {widget_id}.",
            }
        return {"id": widget_id}

    @application.get("/api/auth/callback")
    def callback(request: Request):
        request.state.nbm_audit_actor = {
            "sub": "oidc-user", "name": "OIDC User", "email": "user@example.com",
        }
        request.state.nbm_audit_event = {
            "action": "login", "resource_type": "authentication", "detail": "Logged in.",
        }
        return {"ok": True}

    return TestClient(application), sessions


def test_only_meaningful_annotated_actions_are_audited(monkeypatch, tmp_path):
    client, sessions = _client(monkeypatch, tmp_path)
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/api/widgets/widget-1").status_code == 200
    assert client.get("/api/widgets/widget-1").status_code == 200

    with sessions() as db:
        events = db.query(models.AuditEvent).order_by(models.AuditEvent.created_at).all()
        assert [event.action for event in events] == ["create_widget"]
        assert all(event.actor_sub == "local:admin" for event in events)
        assert events[0].detail == "Created widget widget-1."


def test_oidc_callback_is_audited_with_new_identity(monkeypatch, tmp_path):
    client, sessions = _client(monkeypatch, tmp_path)
    assert client.get("/api/auth/callback").status_code == 200

    with sessions() as db:
        event = db.query(models.AuditEvent).one()
        assert event.action == "login"
        assert event.actor_sub == "oidc-user"
        assert event.actor_email == "user@example.com"
