import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).parent))
from test_migration_planner import FakeClient  # noqa: E402

from app import models  # noqa: E402
from app.services.migration.executor import execute_job  # noqa: E402
from app.services.migration.client import MigrationApiError  # noqa: E402
from app.services.migration.planner import build_plan, persist_plan  # noqa: E402
from app.services.migration.registry import load_registry  # noqa: E402
from app.services.migration.rollback import rollback_job  # noqa: E402

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
        "ipam.ip_addresses": [{"id": 55, "address": "10.0.0.1/24", "assigned_object_type": "dcim.interface", "assigned_object_id": 10, "assigned_object": {"id": 10}}],
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


def test_virtualchassis_master_is_patched_after_both_chassis_and_device_exist(db):
    source = FakeClient({
        "dcim.manufacturers": [{"id": 1, "slug": "acme"}],
        "dcim.device_types": [{"id": 2, "manufacturer": {"id": 1}, "model": "R1"}],
        "dcim.device_roles": [{"id": 3, "slug": "router"}],
        "dcim.sites": [{"id": 4, "slug": "ams"}],
        "dcim.virtual_chassis": [{"id": 5, "name": "stack-1", "master": {"id": 6}}],
        "dcim.devices": [{
            "id": 6, "name": "r1", "site": {"id": 4}, "device_type": {"id": 2},
            "role": {"id": 3}, "virtual_chassis": {"id": 5}, "vc_position": 1,
        }],
    }, read_only=True)
    target = FakeClient({})
    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.device", "dcim.virtualchassis"}, tenant_filter=[], mapping_overrides={},
    )
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    execute_job(db, job, registry=REGISTRY, target_client=target)

    chassis = target.data["dcim.virtual_chassis"][0]
    device = target.data["dcim.devices"][0]
    assert device["virtual_chassis"] == chassis["id"]
    assert chassis["master"] == device["id"]


def test_device_with_both_primary_ip4_and_primary_ip6_deferred_resolves_independently(db):
    source = FakeClient({
        "dcim.manufacturers": [{"id": 1, "slug": "acme"}],
        "dcim.device_types": [{"id": 2, "manufacturer": {"id": 1}, "model": "R1"}],
        "dcim.device_roles": [{"id": 3, "slug": "router"}],
        "dcim.sites": [{"id": 4, "slug": "ams"}],
        "dcim.devices": [{
            "id": 5, "name": "r1", "site": {"id": 4}, "device_type": {"id": 2}, "role": {"id": 3},
            "primary_ip4": {"id": 20}, "primary_ip6": {"id": 21},
        }],
        "dcim.interfaces": [
            {"id": 10, "device": {"id": 5}, "name": "eth0"},
            {"id": 11, "device": {"id": 5}, "name": "eth1"},
        ],
        "ipam.ip_addresses": [
            {"id": 20, "address": "192.0.2.1/24", "assigned_object_type": "dcim.interface", "assigned_object_id": 10},
            {"id": 21, "address": "2001:db8::1/64", "assigned_object_type": "dcim.interface", "assigned_object_id": 11},
        ],
    }, read_only=True)
    target = FakeClient({})
    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.device", "dcim.interface", "ipam.ipaddress"}, tenant_filter=[], mapping_overrides={},
    )
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    execute_job(db, job, registry=REGISTRY, target_client=target)

    device = target.data["dcim.devices"][0]
    ips = {row["address"]: row["id"] for row in target.data["ipam.ip_addresses"]}
    assert device["primary_ip4"] == ips["192.0.2.1/24"]
    assert device["primary_ip6"] == ips["2001:db8::1/64"]


def test_device_primary_ip4_patch_failure_does_not_block_primary_ip6_patch(db):
    source = FakeClient({
        "dcim.manufacturers": [{"id": 1, "slug": "acme"}],
        "dcim.device_types": [{"id": 2, "manufacturer": {"id": 1}, "model": "R1"}],
        "dcim.device_roles": [{"id": 3, "slug": "router"}],
        "dcim.sites": [{"id": 4, "slug": "ams"}],
        "dcim.devices": [{
            "id": 5, "name": "r1", "site": {"id": 4}, "device_type": {"id": 2}, "role": {"id": 3},
            "primary_ip4": {"id": 20}, "primary_ip6": {"id": 21},
        }],
        "ipam.ip_addresses": [{"id": 21, "address": "2001:db8::1/64"}],
    }, read_only=True)
    target = FakeClient({})
    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.device", "ipam.ipaddress"}, tenant_filter=[], mapping_overrides={},
    )
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    execute_job(db, job, registry=REGISTRY, target_client=target)

    device = target.data["dcim.devices"][0]
    assert "primary_ip4" not in device
    assert device["primary_ip6"] == target.data["ipam.ip_addresses"][0]["id"]
    patch = db.query(models.MigrationJobPatch).filter_by(job_id=job.id, object_type="dcim.device").one()
    assert patch.execution_status == "error"
    assert "primary_ip4" in patch.error_detail


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
    assert json.loads(job.totals_json)["dcim.site"] == {"create": 1, "error": 1}


def test_fail_fast_stops_before_the_next_pending_item(db):
    source = FakeClient({
        "dcim.sites": [
            {"id": 1, "slug": "ams-1"}, {"id": 2, "slug": "lon-1"}, {"id": 3, "slug": "par-1"},
        ],
    }, read_only=True)
    target = FakeClient({})
    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={})
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    original_create = target.create

    def fail_second(endpoint, payload):
        if payload.get("slug") == "lon-1":
            raise MigrationApiError("stop here")
        return original_create(endpoint, payload)

    target.create = fail_second
    execute_job(db, job, registry=REGISTRY, target_client=target, fail_fast=True)
    items = db.query(models.MigrationJobItem).filter_by(job_id=job.id).order_by(models.MigrationJobItem.order_index).all()
    assert [item.execution_status for item in items] == ["done", "error", "pending"]
    assert job.status == "completed_with_errors"
    assert job.phase == "primary"


def test_fail_fast_resume_processes_remaining_pending_items(db):
    source = FakeClient({"dcim.sites": [{"id": 1, "slug": "ams-1"}, {"id": 2, "slug": "lon-1"}, {"id": 3, "slug": "par-1"}]}, read_only=True)
    target = FakeClient({})
    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={})
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    original_create = target.create
    target.create = lambda endpoint, payload: (_ for _ in ()).throw(MigrationApiError("stop")) if payload.get("slug") == "lon-1" else original_create(endpoint, payload)
    execute_job(db, job, registry=REGISTRY, target_client=target, fail_fast=True)
    target.create = original_create
    db.query(models.MigrationJobItem).filter_by(job_id=job.id, execution_status="error").update(
        {"execution_status": "pending", "error_detail": None}, synchronize_session=False,
    )
    job.status = "running"
    db.commit()
    db.refresh(job)
    execute_job(db, job, registry=REGISTRY, target_client=target, fail_fast=True)
    assert job.status == "completed"
    assert len(target.data["dcim.sites"]) == 3


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


# ── Fix 1: polymorphic assigned_object end-to-end ────────────────────────────

def test_ip_assigned_to_device_interface_resolves_via_patch_phase(db):
    source = FakeClient({
        "dcim.manufacturers": [{"id": 1, "slug": "cisco"}],
        "dcim.device_types": [{"id": 1, "manufacturer": {"id": 1}, "model": "C9300"}],
        "dcim.device_roles": [{"id": 1, "slug": "access-switch"}],
        "dcim.sites": [{"id": 1, "slug": "ams-1"}],
        "dcim.devices": [{"id": 1, "name": "sw-1", "site": {"id": 1}, "device_type": {"id": 1}, "role": {"id": 1}}],
        "dcim.interfaces": [{"id": 10, "device": {"id": 1}, "name": "mgmt0"}],
        "ipam.ip_addresses": [{
            "id": 55, "address": "10.0.0.1/24",
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": 10,
            "assigned_object": {"id": 10},
        }],
    }, read_only=True)
    target = FakeClient({})

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.device", "dcim.interface", "ipam.ipaddress"},
        tenant_filter=[], mapping_overrides={},
    )
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    execute_job(db, job, registry=REGISTRY, target_client=target)

    db.refresh(job)
    assert job.status == "completed"
    iface = target.data["dcim.interfaces"][0]
    ip = target.data["ipam.ip_addresses"][0]
    # The IP's assigned_object must point at the newly-created target interface
    assert ip["assigned_object_id"] == iface["id"]


def test_ip_assigned_to_vm_interface_resolves_via_patch_phase(db):
    source = FakeClient({
        "virtualization.cluster_types": [{"id": 1, "slug": "vmware"}],
        "virtualization.clusters": [{"id": 1, "name": "prod", "type": {"id": 1}}],
        "virtualization.virtual_machines": [{"id": 1, "name": "vm-1", "cluster": {"id": 1}}],
        "virtualization.interfaces": [{"id": 20, "virtual_machine": {"id": 1}, "name": "eth0"}],
        "ipam.ip_addresses": [{
            "id": 66, "address": "192.168.1.1/24",
            "assigned_object_type": "virtualization.vminterface",
            "assigned_object_id": 20,
        }],
    }, read_only=True)
    target = FakeClient({})

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"virtualization.virtualmachine", "virtualization.vminterface", "ipam.ipaddress"},
        tenant_filter=[], mapping_overrides={},
    )
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    execute_job(db, job, registry=REGISTRY, target_client=target)

    db.refresh(job)
    assert job.status == "completed"
    vmif = target.data["virtualization.interfaces"][0]
    ip = target.data["ipam.ip_addresses"][0]
    assert ip["assigned_object_id"] == vmif["id"]


def test_ip_assigned_to_vminterface_not_selected_produces_patch_error(db):
    # vminterface is NOT in selected_types — its id map will be empty.
    # The patch must fail with a clear, specific error, not silently succeed or
    # be confused with the dcim.interface case.
    source = FakeClient({
        "ipam.ip_addresses": [{
            "id": 66, "address": "192.168.1.1/24",
            "assigned_object_type": "virtualization.vminterface",
            "assigned_object_id": 20,
        }],
    }, read_only=True)
    target = FakeClient({})

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"ipam.ipaddress"},  # vminterface deliberately excluded
        tenant_filter=[], mapping_overrides={},
    )
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    execute_job(db, job, registry=REGISTRY, target_client=target)

    db.refresh(job)
    assert job.status == "completed_with_errors"

    from app import models as _models
    patches = db.query(_models.MigrationJobPatch).filter_by(job_id=job.id).all()
    ip_patch = next(p for p in patches if p.object_type == "ipam.ipaddress")
    assert ip_patch.execution_status == "error"
    # Error must mention the vminterface type specifically
    assert "virtualization.vminterface" in ip_patch.error_detail


def test_executor_creates_all_new_types_and_resolves_polymorphic_fields(db):
    source = FakeClient({
        "extras.config_templates": [{"id": 70, "name": "router", "template_code": "hostname {{ device.name }}"}],
        "dcim.manufacturers": [{"id": 1, "slug": "acme"}],
        "dcim.device_types": [{"id": 2, "manufacturer": {"id": 1}, "model": "R1"}],
        "dcim.device_roles": [{"id": 3, "slug": "router"}],
        "dcim.sites": [{"id": 4, "slug": "ams"}],
        "dcim.devices": [{
            "id": 5, "name": "r1", "site": {"id": 4}, "device_type": {"id": 2},
            "role": {"id": 3}, "config_template": {"id": 70},
        }],
        "ipam.vlan_translation_policies": [{"id": 80, "name": "edge"}],
        "ipam.vlan_translation_rules": [{"id": 81, "policy": {"id": 80}, "local_vid": 100, "remote_vid": 200}],
        "dcim.interfaces": [{
            "id": 6, "device": {"id": 5}, "name": "eth0",
            "vlan_translation_policy": {"id": 80},
        }],
        "ipam.fhrp_groups": [{"id": 7, "protocol": "vrrp2", "group_id": 10}],
        "ipam.fhrp_group_assignments": [{
            "id": 8, "group": {"id": 7}, "interface_type": "dcim.interface",
            "interface_id": 6, "interface": {"id": 6}, "priority": 100,
        }],
        "ipam.services": [{
            "id": 9, "parent_object_type": "ipam.fhrpgroup", "parent_object_id": 7,
            "parent": {"id": 7}, "name": "dns", "protocol": "udp", "ports": [53],
        }],
        "dcim.mac_addresses": [{
            "id": 10, "mac_address": "00:11:22:33:44:55",
            "assigned_object_type": "dcim.interface", "assigned_object_id": 6,
            "assigned_object": {"id": 6},
        }],
        "ipam.rirs": [{"id": 11, "slug": "arin", "name": "ARIN"}],
        "ipam.asn_ranges": [{
            "id": 12, "name": "private", "slug": "private", "rir": {"id": 11},
            "start": 64512, "end": 65534,
        }],
    }, read_only=True)
    target = FakeClient({})
    selected = {
        "extras.configtemplate", "ipam.vlantranslationpolicy", "ipam.vlantranslationrule",
        "ipam.fhrpgroupassignment", "ipam.service", "dcim.macaddress", "ipam.asnrange",
    }
    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types=selected, tenant_filter=[], mapping_overrides={}, source_netbox_version="4.6.8",
    )
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    execute_job(db, job, registry=REGISTRY, target_client=target)

    db.refresh(job)
    assert job.status == "completed"
    expected_endpoints = {
        "extras.config_templates", "ipam.vlan_translation_policies", "ipam.vlan_translation_rules",
        "ipam.fhrp_group_assignments", "ipam.services", "dcim.mac_addresses", "ipam.asn_ranges",
    }
    assert expected_endpoints <= set(target.data)
    interface_id = target.data["dcim.interfaces"][0]["id"]
    group_id = target.data["ipam.fhrp_groups"][0]["id"]
    assert target.data["ipam.fhrp_group_assignments"][0]["interface_id"] == interface_id
    assert target.data["ipam.services"][0]["parent_object_id"] == group_id
    assert target.data["dcim.mac_addresses"][0]["assigned_object_id"] == interface_id


def _executed_site_device_interface_job(db):
    source = FakeClient({
        "dcim.manufacturers": [{"id": 1, "slug": "acme"}],
        "dcim.device_types": [{"id": 2, "manufacturer": {"id": 1}, "model": "R1"}],
        "dcim.device_roles": [{"id": 3, "slug": "router"}],
        "dcim.sites": [{"id": 4, "slug": "ams"}],
        "dcim.devices": [{"id": 5, "name": "r1", "site": {"id": 4}, "device_type": {"id": 2}, "role": {"id": 3}}],
        "dcim.interfaces": [{"id": 6, "device": {"id": 5}, "name": "eth0"}],
    }, read_only=True)
    target = FakeClient({})
    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.interface"}, tenant_filter=[], mapping_overrides={},
    )
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    execute_job(db, job, registry=REGISTRY, target_client=target)
    return job, target


def test_rollback_deletes_created_dependency_chain_in_reverse_order(db):
    job, target = _executed_site_device_interface_job(db)

    rollback_job(db, job, registry=REGISTRY, target_client=target)

    assert job.status == "rolled_back"
    deleted_types = [endpoint for endpoint, _ in target.deleted]
    assert deleted_types.index("dcim.interfaces") < deleted_types.index("dcim.devices")
    assert deleted_types.index("dcim.devices") < deleted_types.index("dcim.sites")


def test_rollback_never_touches_mapped_or_updated_objects(db):
    job = _make_job(db)
    job.status = "completed"
    items = [
        models.MigrationJobItem(job_id=job.id, order_index=0, object_type="dcim.site", source_id=1, planned_action="create", execution_status="done", target_id=101),
        models.MigrationJobItem(job_id=job.id, order_index=1, object_type="dcim.site", source_id=2, planned_action="map", execution_status="done", target_id=102),
        models.MigrationJobItem(job_id=job.id, order_index=2, object_type="dcim.site", source_id=3, planned_action="update", execution_status="done", target_id=103),
    ]
    db.add_all(items); db.commit()
    target = FakeClient({"dcim.sites": [{"id": 101}, {"id": 102}, {"id": 103}]})

    rollback_job(db, job, registry=REGISTRY, target_client=target)

    assert target.deleted == [("dcim.sites", 101)]
    assert {row["id"] for row in target.data["dcim.sites"]} == {102, 103}


def test_rollback_crash_can_resume_without_double_delete(db):
    job, target = _executed_site_device_interface_job(db)
    original_delete = target.delete_by_id
    calls = {"count": 0}

    def crash_on_second_delete(endpoint, id_):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated rollback process death")
        return original_delete(endpoint, id_)

    target.delete_by_id = crash_on_second_delete
    with pytest.raises(RuntimeError, match="simulated rollback"):
        rollback_job(db, job, registry=REGISTRY, target_client=target)
    first_deleted = target.deleted[0]
    assert job.status == "rolling_back"

    target.delete_by_id = original_delete
    rollback_job(db, job, registry=REGISTRY, target_client=target)

    assert job.status == "rolled_back"
    assert target.deleted.count(first_deleted) == 1


def test_rollback_records_delete_failure_and_continues(db):
    job, target = _executed_site_device_interface_job(db)
    original_delete = target.delete_by_id

    def fail_device(endpoint, id_):
        if endpoint.key == "dcim.devices":
            raise MigrationApiError("still referenced")
        return original_delete(endpoint, id_)

    target.delete_by_id = fail_device
    rollback_job(db, job, registry=REGISTRY, target_client=target)

    assert job.status == "rolled_back_with_errors"
    failed = db.query(models.MigrationJobItem).filter_by(job_id=job.id, execution_status="rollback_error").one()
    assert failed.object_type == "dcim.device"
    assert "still referenced" in failed.error_detail
    assert any(endpoint == "dcim.sites" for endpoint, _ in target.deleted)


def test_tags_are_resolved_to_target_ids_not_sent_as_raw_nested_objects(db):
    source = FakeClient({
        "extras.tags": [{"id": 1, "slug": "production", "name": "Production"}],
        "dcim.sites": [{"id": 1, "slug": "ams-1", "name": "Amsterdam 1", "tags": [{"id": 1, "slug": "production", "name": "Production"}]}],
    }, read_only=True)
    target = FakeClient({})

    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={})
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    execute_job(db, job, registry=REGISTRY, target_client=target)

    db.refresh(job)
    assert job.status == "completed"
    created_tag = target.data["extras.tags"][0]
    created_site = target.data["dcim.sites"][0]
    # The site's tags field must be the TARGET tag's id, never the raw nested source object.
    assert created_site["tags"] == [created_tag["id"]]
    assert isinstance(created_site["tags"][0], int)


def test_tags_reuse_an_existing_target_tag_by_slug_instead_of_duplicating(db):
    source = FakeClient({
        "extras.tags": [{"id": 1, "slug": "production", "name": "Production"}],
        "dcim.sites": [{"id": 1, "slug": "ams-1", "name": "Amsterdam 1", "tags": [{"id": 1, "slug": "production"}]}],
    }, read_only=True)
    target = FakeClient({"extras.tags": [{"id": 500, "slug": "production", "name": "Production"}]})

    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={})
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    execute_job(db, job, registry=REGISTRY, target_client=target)

    db.refresh(job)
    assert job.status == "completed"
    assert len(target.data["extras.tags"]) == 1  # no duplicate tag created
    created_site = target.data["dcim.sites"][0]
    assert created_site["tags"] == [500]


def test_interface_lag_is_patched_after_both_interfaces_exist(db):
    source = FakeClient({
        "dcim.manufacturers": [{"id": 1, "slug": "cisco"}],
        "dcim.device_types": [{"id": 1, "manufacturer": {"id": 1}, "model": "C9300"}],
        "dcim.device_roles": [{"id": 1, "slug": "access-switch"}],
        "dcim.sites": [{"id": 1, "slug": "ams-1"}],
        "dcim.devices": [{"id": 1, "name": "core-sw-1", "site": {"id": 1}, "device_type": {"id": 1}, "role": {"id": 1}}],
        "dcim.interfaces": [
            {"id": 10, "device": {"id": 1}, "name": "Port-channel1"},
            {"id": 11, "device": {"id": 1}, "name": "Gi0/1", "lag": {"id": 10}},
        ],
    }, read_only=True)
    target = FakeClient({})

    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.device", "dcim.interface"}, tenant_filter=[], mapping_overrides={})
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    execute_job(db, job, registry=REGISTRY, target_client=target)

    db.refresh(job)
    assert job.status == "completed"
    interfaces = {i["name"]: i for i in target.data["dcim.interfaces"]}
    assert interfaces["Gi0/1"]["lag"] == interfaces["Port-channel1"]["id"]


def test_devicebay_installed_device_is_patched_after_both_devices_exist(db):
    source = FakeClient({
        "dcim.manufacturers": [{"id": 1, "slug": "cisco"}],
        "dcim.device_types": [{"id": 1, "manufacturer": {"id": 1}, "model": "C9300"}],
        "dcim.device_roles": [{"id": 1, "slug": "access-switch"}],
        "dcim.sites": [{"id": 1, "slug": "ams-1"}],
        "dcim.devices": [
            {"id": 1, "name": "chassis-1", "site": {"id": 1}, "device_type": {"id": 1}, "role": {"id": 1}},
            {"id": 2, "name": "blade-1", "site": {"id": 1}, "device_type": {"id": 1}, "role": {"id": 1}},
        ],
        "dcim.device_bays": [{"id": 20, "device": {"id": 1}, "name": "Bay 1", "installed_device": {"id": 2}}],
    }, read_only=True)
    target = FakeClient({})

    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.device", "dcim.devicebay"}, tenant_filter=[], mapping_overrides={})
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    execute_job(db, job, registry=REGISTRY, target_client=target)

    db.refresh(job)
    assert job.status == "completed"
    bay = target.data["dcim.device_bays"][0]
    blade = next(d for d in target.data["dcim.devices"] if d["name"] == "blade-1")
    assert bay["installed_device"] == blade["id"]


def test_marker_tag_applied_to_created_objects_not_mapped_ones(db):
    source = FakeClient({
        "dcim.sites": [
            {"id": 1, "slug": "ams-1", "name": "Amsterdam 1"},   # will be mapped (exists on target)
            {"id": 2, "slug": "lon-1", "name": "London 1"},      # will be created
        ],
    }, read_only=True)
    target = FakeClient({"dcim.sites": [{"id": 100, "slug": "ams-1", "name": "Amsterdam 1"}]})
    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={})
    job = _make_job(db)
    persist_plan(db, job.id, plan)

    execute_job(db, job, registry=REGISTRY, target_client=target, marker_tag_slug="migrated-from-test")

    db.refresh(job)
    assert job.status == "completed"
    tag = target.data["extras.tags"][0]
    assert tag["slug"] == "migrated-from-test"
    sites = {s["slug"]: s for s in target.data["dcim.sites"]}
    assert sites["lon-1"]["tags"] == [tag["id"]]          # created object: tagged
    assert sites["ams-1"].get("tags") in (None, [])       # mapped (pre-existing) object: untouched


def test_marker_tag_reuses_existing_tag_and_is_idempotent_across_two_runs(db):
    source = FakeClient({"dcim.sites": [{"id": 1, "slug": "lon-1", "name": "London 1"}]}, read_only=True)
    target = FakeClient({"extras.tags": [{"id": 999, "slug": "migrated-from-test", "name": "migrated-from-test"}]})
    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={})
    job = _make_job(db)
    persist_plan(db, job.id, plan)

    execute_job(db, job, registry=REGISTRY, target_client=target, marker_tag_slug="migrated-from-test")
    db.refresh(job)
    assert job.status == "completed"
    assert len(target.data["extras.tags"]) == 1  # reused, not duplicated
    site = target.data["dcim.sites"][0]
    assert site["tags"] == [999]
    assert target.update_many_calls == [("dcim.sites", [{"id": site["id"], "tags": [999]}])]

    # Re-running marker tagging again (e.g. a resumed job re-doing this best-effort step) must
    # not duplicate the tag on the object either.
    from app.services.migration.executor import _apply_marker_tag
    from app.services.migration.matcher import IdMap
    id_map = IdMap()
    id_map.put("dcim.site", 1, site["id"])
    _apply_marker_tag(db, job, registry=REGISTRY, target_client=target, id_map=id_map, tag_slug="migrated-from-test")
    assert target.data["dcim.sites"][0]["tags"] == [999]  # still just one entry, not [999, 999]


def test_marker_tag_failure_is_recorded_as_a_warning_and_does_not_block_completion(db):
    source = FakeClient({"dcim.sites": [{"id": 1, "slug": "lon-1", "name": "London 1"}]}, read_only=True)
    target = FakeClient({})
    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={})
    job = _make_job(db)
    persist_plan(db, job.id, plan)

    def failing_create(endpoint, payload):
        raise RuntimeError("target rejected tag creation")
    original_create = target.create
    target.create = lambda endpoint, payload: failing_create(endpoint, payload) if "tags" in endpoint.key else original_create(endpoint, payload)

    execute_job(db, job, registry=REGISTRY, target_client=target, marker_tag_slug="migrated-from-test")

    db.refresh(job)
    assert job.status == "completed"  # marker tagging must never prevent a terminal status
    assert any("Marker tagging failed" in w for w in __import__("json").loads(job.warnings_json))
    assert len(target.data["dcim.sites"]) == 1  # the site itself still migrated fine


def test_no_marker_tag_when_slug_is_none(db):
    source = FakeClient({"dcim.sites": [{"id": 1, "slug": "lon-1", "name": "London 1"}]}, read_only=True)
    target = FakeClient({})
    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={})
    job = _make_job(db)
    persist_plan(db, job.id, plan)

    execute_job(db, job, registry=REGISTRY, target_client=target, marker_tag_slug=None)

    db.refresh(job)
    assert job.status == "completed"
    assert "extras.tags" not in target.data or not target.data["extras.tags"]


def test_execution_current_step_distinguishes_create_patch_and_marker_phases(db):
    source = FakeClient({"dcim.sites": [{"id": 1, "slug": "lon-1", "name": "London"}]}, read_only=True)
    target = FakeClient({})
    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={},
    )
    job = _make_job(db)
    persist_plan(db, job.id, plan)
    db.add(models.MigrationJobPatch(
        job_id=job.id, order_index=0, object_type="dcim.site", source_id=1,
        patch_fields_json="{}", polymorphic_patch_fields_json="{}",
    ))
    db.commit()

    steps: list[str] = []
    original_commit = db.commit
    def recording_commit():
        if job.current_step:
            steps.append(job.current_step)
        original_commit()
    db.commit = recording_commit

    execute_job(db, job, registry=REGISTRY, target_client=target, marker_tag_slug="migrated-from-test")

    assert any(step.startswith("Creating Site (1/1)") for step in steps)
    assert any(step.startswith("Applying deferred patches (1/1)") for step in steps)
    assert "Applying marker tag" in steps
    assert job.current_step == "Completed"


def test_conflict_policy_update_actually_executes_the_update(db):
    """
    Regression test: the bulk-create batching refactor (_execute_create_batch)
    routed "create" items to a new code path but left "update" items falling
    through to _execute_one_item, which had its "update" branch deleted and
    replaced with an unconditional `else: raise AssertionError(...)`. Any
    conflict_policy="update" migration crashed the entire job the instant it
    reached a matched-and-to-be-updated object. This must never regress again.
    """
    source = FakeClient({"dcim.sites": [{"id": 1, "slug": "ams-1", "name": "Amsterdam 1 RENAMED"}]}, read_only=True)
    target = FakeClient({"dcim.sites": [{"id": 100, "slug": "ams-1", "name": "Amsterdam 1"}]})
    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={},
        conflict_policy={"dcim.site": "update"},
    )
    job = _make_job(db)
    persist_plan(db, job.id, plan)

    execute_job(db, job, registry=REGISTRY, target_client=target)  # must NOT raise

    db.refresh(job)
    assert job.status == "completed"
    item = db.query(models.MigrationJobItem).filter_by(job_id=job.id).one()
    assert item.execution_status == "done"
    assert item.target_id == 100  # the existing object, not a new one
    updated_site = next(s for s in target.data["dcim.sites"] if s["id"] == 100)
    assert updated_site["name"] == "Amsterdam 1 RENAMED"
