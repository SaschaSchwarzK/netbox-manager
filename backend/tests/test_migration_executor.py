import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).parent))
from test_migration_planner import FakeClient  # noqa: E402

from app import models  # noqa: E402
from app.services.migration.executor import execute_job  # noqa: E402
from app.services.migration.planner import build_plan, persist_plan  # noqa: E402
from app.services.migration.registry import load_registry  # noqa: E402

REGISTRY = load_registry()


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    session = Session(bind=engine)
    yield session
    session.close()


def _make_job(db, source_id="src", target_id="tgt"):
    job = models.MigrationJob(source_instance_id=source_id, target_instance_id=target_id, status="planned")
    db.add(job)
    db.commit()
    return job


def test_basic_execution_creates_objects_in_dependency_order(db):
    source = FakeClient({
        "dcim.sites": [{"id": 1, "slug": "ams-1", "name": "Amsterdam 1"}],
    }, read_only=True)
    target = FakeClient({})

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={},
    )
    job = _make_job(db)
    persist_plan(db, job.id, plan)

    execute_job(db, job, registry=REGISTRY, target_client=target)

    db.refresh(job)
    assert job.status == "completed"
    assert len(target.data["dcim.sites"]) == 1
    assert target.data["dcim.sites"][0]["slug"] == "ams-1"


def test_execution_resolves_fk_chain_with_real_ids_not_placeholders(db):
    source = FakeClient({
        "dcim.manufacturers": [{"id": 1, "slug": "cisco"}],
        "dcim.device_types": [{"id": 1, "manufacturer": {"id": 1}, "model": "C9300"}],
        "dcim.device_roles": [{"id": 1, "slug": "access-switch"}],
        "dcim.sites": [{"id": 1, "slug": "ams-1"}],
        "dcim.devices": [{"id": 1, "name": "core-sw-1", "site": {"id": 1}, "device_type": {"id": 1}, "role": {"id": 1}}],
    }, read_only=True)
    target = FakeClient({})

    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.device"}, tenant_filter=[], mapping_overrides={})
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    execute_job(db, job, registry=REGISTRY, target_client=target)

    db.refresh(job)
    assert job.status == "completed"
    device = target.data["dcim.devices"][0]
    site_id = target.data["dcim.sites"][0]["id"]
    assert device["site"] == site_id  # a REAL target id, never the planning-time negative placeholder
    assert device["site"] > 0


def test_deferred_fk_is_applied_via_patch_phase(db):
    source = FakeClient({
        "dcim.manufacturers": [{"id": 1, "slug": "cisco"}],
        "dcim.device_types": [{"id": 1, "manufacturer": {"id": 1}, "model": "C9300"}],
        "dcim.device_roles": [{"id": 1, "slug": "access-switch"}],
        "dcim.sites": [{"id": 1, "slug": "ams-1"}],
        "dcim.devices": [{
            "id": 1, "name": "core-sw-1", "site": {"id": 1}, "device_type": {"id": 1}, "role": {"id": 1},
            "primary_ip4": {"id": 55},
        }],
        "dcim.interfaces": [{"id": 10, "device": {"id": 1}, "name": "mgmt0"}],
        "ipam.ip_addresses": [{"id": 55, "address": "10.0.0.1/24", "assigned_object": {"id": 10}}],
    }, read_only=True)
    target = FakeClient({})

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.device", "ipam.ipaddress"}, tenant_filter=[], mapping_overrides={},
    )
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    execute_job(db, job, registry=REGISTRY, target_client=target)

    db.refresh(job)
    assert job.status == "completed"
    device = target.data["dcim.devices"][0]
    ip = target.data["ipam.ip_addresses"][0]
    assert device["primary_ip4"] == ip["id"]  # patched in after both existed


def test_crash_mid_run_then_resume_produces_no_duplicates(db):
    source = FakeClient({
        "dcim.sites": [
            {"id": 1, "slug": "ams-1", "name": "Amsterdam 1"},
            {"id": 2, "slug": "lon-1", "name": "London 1"},
            {"id": 3, "slug": "par-1", "name": "Paris 1"},
        ],
    }, read_only=True)
    target = FakeClient({})

    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={})
    job = _make_job(db)
    persist_plan(db, job.id, plan)

    # Simulate a genuine process crash (not a handled API error — the executor deliberately
    # catches MigrationApiError as a per-item, best-effort failure and keeps going, which is
    # correct for real API errors but doesn't exercise resume). An uncaught exception, as if
    # the container was killed mid-request, is what actually leaves a job "running" for the
    # app startup hook to resume — so raise something the executor does NOT catch anywhere.
    original_create = target.create
    call_count = {"n": 0}

    def flaky_create(endpoint, payload):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated container death")
        return original_create(endpoint, payload)

    target.create = flaky_create
    with pytest.raises(RuntimeError):
        execute_job(db, job, registry=REGISTRY, target_client=target)

    # First site made it; job is still "running" (not terminal) — exactly the state a real
    # crash would leave behind, ready for the app startup hook to resume.
    db.refresh(job)
    assert job.status == "running"
    assert len(target.data["dcim.sites"]) == 1

    # Resume with a healthy client (a fresh RateLimitedClient in real life, after restart).
    target.create = original_create
    execute_job(db, job, registry=REGISTRY, target_client=target)

    db.refresh(job)
    assert job.status == "completed"
    assert len(target.data["dcim.sites"]) == 3  # not 4 — no duplicate of the one that "crashed" mid-flight
    slugs = sorted(s["slug"] for s in target.data["dcim.sites"])
    assert slugs == ["ams-1", "lon-1", "par-1"]


def test_resume_does_not_duplicate_when_target_already_has_the_object_from_a_lost_commit(db):
    # The trickiest crash window: create() succeeded on the target, but the process died
    # BEFORE marking the item done locally. Simulate by pre-creating the object on the target
    # directly (bypassing the executor) and leaving the item "pending" — the safety-net
    # natural-key recheck must find it rather than creating a second one.
    source = FakeClient({"dcim.sites": [{"id": 1, "slug": "ams-1", "name": "Amsterdam 1"}]}, read_only=True)
    target = FakeClient({})

    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={})
    job = _make_job(db)
    persist_plan(db, job.id, plan)

    # Simulate the "lost commit": the object already exists on the target...
    target.data.setdefault("dcim.sites", []).append({"id": 999, "slug": "ams-1", "name": "Amsterdam 1"})
    # ...but the local item still says "pending" (as it would after a lost commit).

    execute_job(db, job, registry=REGISTRY, target_client=target)

    db.refresh(job)
    assert job.status == "completed"
    assert len(target.data["dcim.sites"]) == 1  # still just the one — recheck found it, no duplicate


def test_genuine_api_error_is_recorded_per_item_and_does_not_abort_the_job(db):
    from app.services.migration.client import MigrationApiError

    source = FakeClient({
        "dcim.sites": [{"id": 1, "slug": "ams-1"}, {"id": 2, "slug": "lon-1"}],
    }, read_only=True)
    target = FakeClient({})
    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={})
    job = _make_job(db)
    persist_plan(db, job.id, plan)

    original_create = target.create

    def failing_second_create(endpoint, payload):
        if payload.get("slug") == "lon-1":
            raise MigrationApiError("target rejected this one (e.g. validation error)")
        return original_create(endpoint, payload)

    target.create = failing_second_create
    execute_job(db, job, registry=REGISTRY, target_client=target)  # must NOT raise

    db.refresh(job)
    assert job.status == "completed_with_errors"
    assert len(target.data["dcim.sites"]) == 1  # ams-1 still got created despite lon-1 failing

    items = db.query(models.MigrationJobItem).filter_by(job_id=job.id).order_by(models.MigrationJobItem.order_index).all()
    statuses = {i.source_natural_key: i.execution_status for i in items}
    assert statuses["ams-1"] == "done"
    assert statuses["lon-1"] == "error"


def test_execute_job_is_a_noop_on_an_already_terminal_job(db):
    source = FakeClient({"dcim.sites": [{"id": 1, "slug": "ams-1"}]}, read_only=True)
    target = FakeClient({})
    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={})
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    execute_job(db, job, registry=REGISTRY, target_client=target)
    db.refresh(job)
    assert job.status == "completed"

    call_count_before = len(target.created)
    execute_job(db, job, registry=REGISTRY, target_client=target)  # should do nothing at all
    assert len(target.created) == call_count_before


def test_cancellation_stops_between_items_and_leaves_job_cancelled(db):
    source = FakeClient({
        "dcim.sites": [
            {"id": 1, "slug": "ams-1"}, {"id": 2, "slug": "lon-1"}, {"id": 3, "slug": "par-1"},
        ],
    }, read_only=True)
    target = FakeClient({})
    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={})
    job = _make_job(db)
    persist_plan(db, job.id, plan)

    # Request cancellation from the very start — with HEARTBEAT_EVERY=10 normally, force an
    # immediate check by requesting cancellation before any items run at all: the first
    # heartbeat check happens after the very first item (counter=1, 1%10==... not 0) — so to
    # make this deterministic within the test, patch HEARTBEAT_EVERY down to 1.
    import app.services.migration.executor as executor_module
    executor_module.HEARTBEAT_EVERY = 1
    job.cancel_requested = True
    db.commit()

    execute_job(db, job, registry=REGISTRY, target_client=target)

    db.refresh(job)
    assert job.status == "cancelled"
    assert len(target.data.get("dcim.sites", [])) == 0  # stopped before creating anything
