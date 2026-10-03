"""
Integration test for the migration router: calls the actual endpoint
functions (not through HTTP/TestClient — this codebase's existing tests are
all at this function level, e.g. test_tenant_permission_backends.py) against
`responses`-mocked real NetBox HTTP for both source and target, verifying
the full stack wires together: RBAC, instance lookup/token decryption,
request schema validation, background execution, and report rendering.
The service layer itself (registry/matcher/sanitize/planner/executor/report)
already has focused unit tests elsewhere; this test's job is to catch wiring
mistakes those can't see.
"""
import time

import pytest
import responses
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import crypto, models, schemas
from app.rbac import AccessContext
from app.routers import migrations

API_SRC = "https://source.example/api"
API_TGT = "https://target.example/api"


@pytest.fixture
def db(monkeypatch):
    # StaticPool: every session (including the one the background thread opens via the
    # monkeypatched SessionLocal below) shares this single connection, so they all see the
    # same in-memory database — a plain in-memory engine gives each new connection its own
    # separate, empty database instead.
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    session = TestSession()

    # execute_migration spins up a background thread that opens its OWN session via
    # app.database.SessionLocal — point that at the same in-memory engine/connection
    # pool so the test can observe what the thread does.
    monkeypatch.setattr(migrations, "SessionLocal", TestSession)

    yield session
    session.close()


@pytest.fixture
def instances(db):
    source = models.NetboxInstance(
        name="Source", base_url="https://source.example",
        api_token_encrypted=crypto.encrypt("src-token"), verify_ssl=True,
    )
    target = models.NetboxInstance(
        name="Target", base_url="https://target.example",
        api_token_encrypted=crypto.encrypt("tgt-token"), verify_ssl=True,
    )
    db.add_all([source, target])
    db.commit()
    return source, target


def _admin_ctx():
    return AccessContext(role="admin", groups=[], scoping_active=False, app_admin=True)


def _empty_list(url):
    responses.get(url, json={"count": 0, "next": None, "previous": None, "results": []})


@responses.activate
def test_list_instance_tenants_uses_read_only_paginated_migration_client(db, instances):
    source, _ = instances
    responses.get(
        f"{API_SRC}/tenancy/tenants/",
        json={
            "count": 2,
            "next": f"{API_SRC}/tenancy/tenants/?limit=50&offset=1",
            "previous": None,
            "results": [{"id": 1, "name": "Acme", "slug": "acme"}],
        },
    )
    responses.get(
        f"{API_SRC}/tenancy/tenants/?limit=50&offset=1",
        json={
            "count": 2, "next": None, "previous": None,
            "results": [{"id": 2, "name": "Globex", "slug": "globex"}],
        },
    )

    rows = migrations.list_instance_tenants(source.id, q="", db=db, ctx=_admin_ctx())

    assert rows == [
        {"id": 1, "name": "Acme", "slug": "acme"},
        {"id": 2, "name": "Globex", "slug": "globex"},
    ]


def _mock_netbox_apis():
    for base in (API_SRC, API_TGT):
        responses.get(f"{base}/status/", json={"netbox-version": "4.6.8"})
        _empty_list(f"{base}/extras/tags/")  # tags are always fetched first, regardless of selection
    # ipam.vrf (unlike dcim.site) is a user-selectable type in the registry, so it's a
    # minimal, representative choice for exercising the router end to end. It optionally
    # depends on tenancy.tenant (which itself optionally depends on tenancy.tenantgroup) —
    # auto-included by the dependency closure even though none of our VRFs reference one.
    for base in (API_SRC, API_TGT):
        _empty_list(f"{base}/tenancy/tenant-groups/")
        _empty_list(f"{base}/tenancy/tenants/")

    responses.get(
        f"{API_SRC}/ipam/vrfs/",
        json={"count": 1, "next": None, "previous": None, "results": [
            {"id": 1, "name": "customer-a", "url": f"{API_SRC}/ipam/vrfs/1/"},
        ]},
    )
    _empty_list(f"{API_TGT}/ipam/vrfs/")  # no existing match -> create
    responses.post(
        f"{API_TGT}/ipam/vrfs/",
        json=[{"id": 100, "name": "customer-a", "url": f"{API_TGT}/ipam/vrfs/100/"}],
        status=201,
    )

    # Marker tagging (on by default): create the tag on the target, then PATCH the created VRF.
    responses.post(
        f"{API_TGT}/extras/tags/",
        json={"id": 999, "slug": "migrated-from-source", "name": "migrated-from-source"},
        status=201,
    )
    responses.get(
        f"{API_TGT}/ipam/vrfs/100/",
        json={"id": 100, "name": "customer-a", "tags": [], "url": f"{API_TGT}/ipam/vrfs/100/"},
    )
    responses.patch(
        f"{API_TGT}/ipam/vrfs/100/",
        json={"id": 100, "name": "customer-a", "tags": [999], "url": f"{API_TGT}/ipam/vrfs/100/"},
    )


@responses.activate
def test_full_plan_execute_report_cycle_through_the_router(db, instances):
    source, target = instances
    _mock_netbox_apis()

    plan_request = schemas.MigrationPlanRequest(
        source_instance_id=source.id, target_instance_id=target.id,
        selected_types=["ipam.vrf"], tenant_filter=[],
    )
    summary = migrations.plan_migration(plan_request, request=_FakeRequest(), db=db, ctx=_admin_ctx())
    assert summary.status == "planned"
    assert summary.totals["ipam.vrf"]["create"] == 1

    job = db.get(models.MigrationJob, summary.id)
    assert job is not None
    item = db.query(models.MigrationJobItem).filter_by(job_id=job.id).one()
    assert item.planned_action == "create"
    assert item.execution_status == "pending"

    exec_summary = migrations.execute_migration(
        job.id, schemas.MigrationExecuteRequest(confirm=True), db=db, ctx=_admin_ctx(),
    )
    assert exec_summary.status == "running"

    # The background thread runs genuinely async; give it a moment and poll — this mirrors
    # how a real client would poll GET /jobs/{id} rather than assuming instant completion.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        db.expire_all()
        current = migrations.get_job(job.id, db=db, _=_admin_ctx())
        if current.status in ("completed", "completed_with_errors", "failed"):
            break
        time.sleep(0.05)
    assert current.status == "completed"
    assert current.totals["ipam.vrf"]["create"] == 1

    db.expire_all()
    item = db.query(models.MigrationJobItem).filter_by(job_id=job.id).one()
    assert item.execution_status == "done"
    assert item.target_id == 100

    report_html = migrations.get_report(job.id, format="html", db=db, _=_admin_ctx())
    assert b"customer-a" in report_html.body
    assert b"source.example/ipam/vrfs/1/" in report_html.body
    assert b"target.example/ipam/vrfs/100/" in report_html.body

    download = migrations.get_report(job.id, format="html", download=True, db=db, _=_admin_ctx())
    assert download.headers["content-disposition"] == f'attachment; filename="migration-{job.id}.html"'

    report_json = migrations.get_report(job.id, format="json", db=db, _=_admin_ctx())
    assert report_json["status"] == "completed"


@pytest.mark.parametrize("status", ["planned", "running"])
def test_rollback_rejects_nonterminal_job(status, db, instances):
    source, target = instances
    job = models.MigrationJob(source_instance_id=source.id, target_instance_id=target.id, status=status)
    db.add(job); db.commit()

    with pytest.raises(HTTPException) as exc_info:
        migrations.rollback_migration(
            job.id, schemas.MigrationExecuteRequest(confirm=True), db=db, ctx=_admin_ctx(),
        )

    assert exc_info.value.status_code == 409
    assert "only after execution has stopped" in exc_info.value.detail


def test_rollback_requires_target_instance_admin(db, instances):
    source, target = instances
    job = models.MigrationJob(source_instance_id=source.id, target_instance_id=target.id, status="completed")
    db.add(job); db.commit()
    editor = AccessContext(role="editor", groups=[], scoping_active=False, app_admin=False)

    with pytest.raises(HTTPException) as exc_info:
        migrations.rollback_migration(
            job.id, schemas.MigrationExecuteRequest(confirm=True), db=db, ctx=editor,
        )

    assert exc_info.value.status_code == 403


class _FakeRequest:
    """get_current_actor(request) reads request.cookies for a session cookie — absent here, same as an unauthenticated/auth-disabled request."""
    cookies: dict = {}
