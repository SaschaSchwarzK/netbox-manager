from fastapi.testclient import TestClient

from app import models
from app.config import settings
from app.database import SessionLocal
from app.main import app


def test_audit_log_serializes_generic_events_without_a_repository_target(monkeypatch):
    monkeypatch.setattr(settings, "authentication_disabled", True)
    db = SessionLocal()
    try:
        db.add(models.AuditEvent(
            actor_sub="break-glass-admin",
            actor_name="Break Glass Admin",
            action="create_instance",
            resource_type="instance",
            resource_id="instance-1",
            status="success",
            detail="Added NetBox instance Branch Office.",
        ))
        db.commit()
    finally:
        db.close()

    response = TestClient(app).get("/api/audit")

    assert response.status_code == 200
    assert response.json() == [{
        "id": response.json()[0]["id"],
        "created_at": response.json()[0]["created_at"],
        "action_type": "create_instance",
        "target_name": "instance-1",
        "repo_target_id": None,
        "repo_target_name": "",
        "file_path": "",
        "status": "success",
        "detail": "Added NetBox instance Branch Office.",
        "actor_sub": "break-glass-admin",
        "actor_name": "Break Glass Admin",
        "actor_email": None,
        "resource_type": "instance",
        "resource_id": "instance-1",
        "feature": "NetBox instances",
        "activity": "Added NetBox instance Branch Office.",
    }]
