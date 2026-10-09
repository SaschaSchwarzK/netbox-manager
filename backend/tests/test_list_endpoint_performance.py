from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import crypto, models
from app.rbac import AccessContext
from app.routers import drift, fleet
from app.timeutil import utcnow


def _database():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    return engine


def test_drift_scope_is_applied_before_pagination():
    engine = _database()
    with Session(engine) as db:
        hidden = models.NetboxInstance(id="hidden", name="Hidden", base_url="https://hidden",
                                       api_token_encrypted="x", verify_ssl=True)
        visible = models.NetboxInstance(id="visible", name="Visible", base_url="https://visible",
                                        api_token_encrypted="x", verify_ssl=True)
        target = models.GithubTarget(id="target", name="Repo", repo="org/repo", branch="main",
                                     pat_encrypted="x")
        db.add_all([hidden, visible, target, models.AccessMapping(
            oidc_group="other-team", resource_type="instance", resource_id="hidden",
        )])
        db.add_all([
            models.DriftRecord(instance_id="hidden", repo_target_id="target", kind="device_type",
                               file_path="hidden.yml", status="in_sync", checked_at=utcnow()),
            models.DriftRecord(instance_id="visible", repo_target_id="target", kind="device_type",
                               file_path="visible.yml", status="in_sync", checked_at=utcnow() - timedelta(seconds=1)),
        ])
        db.commit()

        result = drift.list_drift(limit=1, offset=0, db=db,
                                  ctx=AccessContext(role="viewer", groups=["my-team"], app_admin=False))

        assert [row.instance_id for row in result] == ["visible"]


def test_fleet_results_are_cached_and_refresh_bypasses_cache(monkeypatch):
    engine = _database()
    calls = {"health": 0, "expiry": 0}
    with Session(engine) as db:
        db.add(models.NetboxInstance(id="one", name="One", base_url="https://one",
                                     api_token_encrypted=crypto.encrypt("token"), verify_ssl=True))
        db.commit()
        monkeypatch.setattr(fleet, "_cache", {})

        def health(*args):
            calls["health"] += 1
            return {"reachable": True, "netbox_version": "4.6.8", "python_version": "3.12",
                    "plugins": {}, "response_time_ms": 1, "error": None}

        def expiry(*args):
            calls["expiry"] += 1
            return {"known": True, "expires": None, "note": None}

        monkeypatch.setattr(fleet.netbox_client, "get_health", health)
        monkeypatch.setattr(fleet.netbox_client, "check_token_expiry", expiry)
        ctx = AccessContext(role="admin", scoping_active=False)

        fleet.fleet_health(limit=10, offset=0, refresh=False, db=db, ctx=ctx)
        fleet.fleet_health(limit=10, offset=0, refresh=False, db=db, ctx=ctx)
        fleet.fleet_health(limit=10, offset=0, refresh=True, db=db, ctx=ctx)

        assert calls == {"health": 2, "expiry": 2}
