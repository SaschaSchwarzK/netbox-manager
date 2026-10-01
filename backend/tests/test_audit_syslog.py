from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models
from app.routers.device_types import _log_action
from app.services import syslog_client


def _actor():
    return {"sub": "user-1", "name": "Test User", "email": "test@example.com"}


def test_log_action_persists_requested_action_type(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    sent = []
    monkeypatch.setattr(syslog_client, "send_audit_entry", sent.append)

    with Session(engine) as db:
        target = models.GithubTarget(
            id="target-1", name="repo", repo="org/repo", branch="main",
            path_pattern="device-types/{manufacturer}/{slug}.yml",
            custom_fields_path="custom-fields/template.yml", pat_encrypted="secret",
        )
        db.add(target)
        db.commit()

        _log_action(
            db, repo_target_id=target.id, file_path="custom-fields/template.yml",
            target_name="netbox-prod", status="success", detail="pushed",
            actor=_actor(), action_type="netbox",
        )

        row = db.query(models.DeviceTypePushHistory).one()
        assert row.target_type == "netbox"
        assert row.target_name == "netbox-prod"
        assert row.actor_email == "test@example.com"
        assert sent == [{
            "action_type": "netbox",
            "target_name": "netbox-prod",
            "file_path": "custom-fields/template.yml",
            "status": "success",
            "detail": "pushed",
            "actor_name": "Test User",
            "actor_email": "test@example.com",
        }]


def test_syslog_forwarding_failure_never_breaks_audit_commit(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    monkeypatch.setattr(syslog_client.settings, "syslog_enabled", True)
    monkeypatch.setattr(syslog_client.settings, "syslog_host", "syslog.invalid")
    monkeypatch.setattr(syslog_client, "send_raw", lambda message: (_ for _ in ()).throw(OSError("down")))

    with Session(engine) as db:
        target = models.GithubTarget(
            id="target-2", name="repo", repo="org/repo", branch="main",
            path_pattern="device-types/{manufacturer}/{slug}.yml",
            custom_fields_path="custom-fields/template.yml", pat_encrypted="secret",
        )
        db.add(target)
        db.commit()

        _log_action(
            db, repo_target_id=target.id, file_path="device-types/acme/router.yml",
            target_name="repo", status="success", detail="saved", actor=_actor(),
        )

        assert db.query(models.DeviceTypePushHistory).count() == 1
