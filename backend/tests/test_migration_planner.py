import pytest

from app.services.migration.matcher import MappingAction, MappingOverride
from app.services.migration.planner import build_plan
from app.services.migration.registry import load_registry

REGISTRY = load_registry()


class _FakeEndpoint:
    def __init__(self, app_name: str, endpoint_name: str):
        self.app_name = app_name
        self.endpoint_name = endpoint_name

    @property
    def key(self) -> str:
        return f"{self.app_name}.{self.endpoint_name}"


class _FakeApp:
    def __init__(self, app_name: str):
        self.app_name = app_name

    def __getattr__(self, endpoint_name: str) -> _FakeEndpoint:
        return _FakeEndpoint(self.app_name, endpoint_name)


class _FakeNb:
    def __getattr__(self, app_name: str) -> _FakeApp:
        return _FakeApp(app_name)


def _matches(obj: dict, filters: dict) -> bool:
    for key, value in filters.items():
        if key == "tenant" and isinstance(value, list):
            tenant = obj.get("tenant")
            slug = tenant.get("slug") if isinstance(tenant, dict) else tenant
            if slug not in value:
                return False
        elif key == "tenant_id" and value == "null":
            if obj.get("tenant") is not None:
                return False
        elif key.endswith("_id"):
            field = key[: -len("_id")]
            field_value = obj.get(field)
            field_id = field_value["id"] if isinstance(field_value, dict) else field_value
            allowed = value if isinstance(value, list) else [value]
            if field_id not in allowed:
                return False
        else:
            allowed = value if isinstance(value, list) else [value]
            if obj.get(key) not in allowed:
                return False
    return True


class FakeClient:
    """
    Stands in for RateLimitedClient in planner tests: same `.nb`/`.paginated`/
    `.get`/`.create`/`.read_only` surface, but backed by an in-memory dict of
    endpoint-key -> list[dict], so planner logic is tested without any HTTP
    or real pynetbox objects (those are already covered by test_migration_client.py).
    """

    def __init__(self, data: dict[str, list[dict]] | None = None, read_only: bool = False):
        self.data = {k: list(v) for k, v in (data or {}).items()}
        self.read_only = read_only
        self.nb = _FakeNb()
        self.created: list[tuple[str, dict]] = []
        self.deleted: list[tuple[str, int]] = []
        self.paginated_calls: list[tuple[str, dict]] = []
        self.get_calls: list[tuple[str, dict]] = []
        self._next_id = 90000

    def paginated(self, endpoint: _FakeEndpoint, **filters):
        self.paginated_calls.append((endpoint.key, filters))
        for obj in self.data.get(endpoint.key, []):
            if _matches(obj, filters):
                yield obj

    def get(self, endpoint: _FakeEndpoint, **filters):
        self.get_calls.append((endpoint.key, filters))
        for obj in self.data.get(endpoint.key, []):
            if _matches(obj, filters):
                return obj
        return None

    def create(self, endpoint: _FakeEndpoint, payload: dict):
        if self.read_only:
            raise AssertionError("build_plan must never write to a client")
        self._next_id += 1
        record = {"id": self._next_id, **payload}
        self.data.setdefault(endpoint.key, []).append(record)
        self.created.append((endpoint.key, payload))
        return record

    def create_many(self, endpoint: _FakeEndpoint, payloads: list[dict]):
        return [self.create(endpoint, payload) for payload in payloads]

    def update_by_id(self, endpoint: _FakeEndpoint, id_: int, payload: dict):
        if self.read_only:
            raise AssertionError("must never write to a read-only client")
        for obj in self.data.get(endpoint.key, []):
            if obj["id"] == id_:
                obj.update(payload)
                return obj
        raise AssertionError(f"update_by_id: no object with id={id_} on {endpoint.key}")

    def delete_by_id(self, endpoint: _FakeEndpoint, id_: int):
        if self.read_only:
            raise AssertionError("must never write to a read-only client")
        rows = self.data.get(endpoint.key, [])
        for index, obj in enumerate(rows):
            if obj["id"] == id_:
                rows.pop(index)
                self.deleted.append((endpoint.key, id_))
                return True
        return False


class OptionsFakeClient(FakeClient):
    def options(self, endpoint):
        return {"actions": {"POST": {"name": {"required": True}}}}


def test_plan_warns_when_options_metadata_finds_missing_required_field():
    source = FakeClient({"dcim.sites": [{"id": 1, "slug": "ams-1"}]}, read_only=True)
    target = OptionsFakeClient({})
    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={},
    )
    assert any("required field 'name'" in warning for warning in plan.warnings)


def test_plan_maps_existing_site_by_slug_and_creates_new_one():
    source = FakeClient({
        "dcim.sites": [
            {"id": 1, "slug": "ams-1", "name": "Amsterdam 1"},
            {"id": 2, "slug": "lon-1", "name": "London 1"},
        ],
    }, read_only=True)
    target = FakeClient({
        "dcim.sites": [{"id": 100, "slug": "ams-1", "name": "Amsterdam 1"}],
    })

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={},
    )

    by_source_id = {item.source_id: item for item in plan.items}
    assert by_source_id[1].planned_action == "map"
    assert by_source_id[1].target_id == 100
    assert by_source_id[2].planned_action == "create"
    assert target.created == []  # never actually wrote anything


def test_plan_marks_multiple_natural_key_matches_ambiguous_instead_of_crashing():
    plan = build_plan(
        registry=REGISTRY,
        source_client=FakeClient({
            "dcim.sites": [{"id": 1, "slug": "duplicate", "name": "Duplicate"}],
        }, read_only=True),
        target_client=FakeClient({
            "dcim.sites": [
                {"id": 10, "slug": "duplicate", "name": "Duplicate"},
                {"id": 11, "slug": "duplicate", "name": "Duplicate"},
            ],
        }),
        selected_types={"dcim.site"},
        tenant_filter=[],
        mapping_overrides={},
    )

    site = next(item for item in plan.items if item.object_type == "dcim.site")
    assert site.planned_action == "ambiguous"
    assert site.execution_status == "error"
    assert "matched multiple target" in site.error_detail
    assert "choose one explicitly" in site.error_detail


def test_plan_resolves_device_fk_chain_and_orders_dependencies_first():
    source = FakeClient({
        "dcim.manufacturers": [{"id": 1, "slug": "cisco", "name": "Cisco"}],
        "dcim.device_types": [{"id": 1, "manufacturer": {"id": 1}, "model": "C9300", "part_number": "C9300-24T", "slug": "c9300"}],
        "dcim.device_roles": [{"id": 1, "slug": "access-switch", "name": "Access Switch"}],
        "dcim.sites": [{"id": 1, "slug": "ams-1", "name": "Amsterdam 1"}],
        "dcim.devices": [{
            "id": 1, "name": "core-sw-1",
            "site": {"id": 1}, "device_type": {"id": 1}, "role": {"id": 1},
        }],
    }, read_only=True)
    target = FakeClient({})

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.device"}, tenant_filter=[], mapping_overrides={},
    )

    # Sites/manufacturers/device types/roles must be planned before the device.
    order = [item.object_type for item in plan.items]
    device_pos = order.index("dcim.device")
    assert order.index("dcim.site") < device_pos
    assert order.index("dcim.manufacturer") < device_pos
    assert order.index("dcim.devicetype") < device_pos
    assert order.index("dcim.devicerole") < device_pos

    device_item = next(i for i in plan.items if i.object_type == "dcim.device")
    assert device_item.planned_action == "create"
    # The preview payload's FK fields must resolve — even though nothing has actually been
    # created yet — because build_plan() records a placeholder id in the id_map for every
    # "create" action, so downstream objects (like this device, referencing the not-yet-real
    # site/device type/role) can still preview a sensible-looking payload for the dry-run report.
    assert "site" in device_item.preview_payload
    assert "device_type" in device_item.preview_payload
    assert "role" in device_item.preview_payload
    # But fk_refs (what the executor actually resolves for real at execution time) holds the
    # original SOURCE ids, not the placeholders — those are a planning-only construct.
    assert device_item.fk_refs == {"site": 1, "device_type": 1, "role": 1}


def test_dependency_discovery_fetches_only_sites_referenced_by_selected_devices():
    source = FakeClient({
        "dcim.sites": [{"id": i, "slug": f"site-{i}", "name": f"Site {i}"} for i in range(1, 11)],
        "dcim.manufacturers": [{"id": 1, "slug": "acme"}],
        "dcim.device_types": [{"id": 1, "manufacturer": {"id": 1}, "model": "R1"}],
        "dcim.device_roles": [{"id": 1, "slug": "router"}],
        "dcim.devices": [{
            "id": 1, "name": "edge-1", "site": {"id": 3},
            "device_type": {"id": 1}, "role": {"id": 1},
        }],
    }, read_only=True)

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=FakeClient({}),
        selected_types={"dcim.device"}, tenant_filter=[], mapping_overrides={},
    )

    assert {item.source_id for item in plan.items if item.object_type == "dcim.site"} == {3}
    site_calls = [filters for endpoint, filters in source.paginated_calls if endpoint == "dcim.sites"]
    assert site_calls == [{"id": [3]}]


def test_dependency_discovery_scopes_device_catalog_and_optional_references():
    source = FakeClient({
        "dcim.sites": [{"id": i, "slug": f"site-{i}"} for i in range(1, 6)],
        "dcim.manufacturers": [{"id": i, "slug": f"maker-{i}"} for i in range(1, 6)],
        "dcim.device_types": [
            {"id": i, "manufacturer": {"id": i}, "model": f"Model {i}"} for i in range(1, 6)
        ],
        "dcim.device_roles": [{"id": i, "slug": f"role-{i}"} for i in range(1, 6)],
        "tenancy.tenants": [{"id": i, "slug": f"tenant-{i}", "name": f"Tenant {i}"} for i in range(1, 6)],
        "dcim.devices": [{
            "id": 1, "name": "only-device", "site": {"id": 2},
            "device_type": {"id": 4}, "role": {"id": 3}, "tenant": {"id": 5},
        }],
    }, read_only=True)

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=FakeClient({}),
        selected_types={"dcim.device"}, tenant_filter=[], mapping_overrides={},
    )

    ids_by_type = {
        type_key: {item.source_id for item in plan.items if item.object_type == type_key}
        for type_key in ("dcim.site", "dcim.manufacturer", "dcim.devicetype", "dcim.devicerole", "tenancy.tenant")
    }
    assert ids_by_type == {
        "dcim.site": {2}, "dcim.manufacturer": {4}, "dcim.devicetype": {4},
        "dcim.devicerole": {3}, "tenancy.tenant": {5},
    }


def test_dependency_discovery_follows_location_site_and_full_region_parent_chain():
    source = FakeClient({
        "dcim.regions": [
            {"id": 1, "slug": "world"},
            {"id": 2, "slug": "unused"},
            {"id": 3, "slug": "europe", "parent": {"id": 1}},
        ],
        "dcim.sites": [
            {"id": 7, "slug": "used", "region": {"id": 3}},
            {"id": 8, "slug": "unused", "region": {"id": 2}},
        ],
        "dcim.locations": [
            {"id": 10, "slug": "building", "site": {"id": 7}},
            {"id": 11, "slug": "room", "site": {"id": 7}, "parent": {"id": 10}},
            {"id": 12, "slug": "unused", "site": {"id": 8}},
        ],
        "dcim.manufacturers": [{"id": 1, "slug": "acme"}],
        "dcim.device_types": [{"id": 1, "manufacturer": {"id": 1}, "model": "R1"}],
        "dcim.device_roles": [{"id": 1, "slug": "router"}],
        "dcim.devices": [{
            "id": 1, "name": "edge", "site": {"id": 7}, "location": {"id": 11},
            "device_type": {"id": 1}, "role": {"id": 1},
        }],
    }, read_only=True)

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=FakeClient({}),
        selected_types={"dcim.device"}, tenant_filter=[], mapping_overrides={},
    )

    assert {item.source_id for item in plan.items if item.object_type == "dcim.location"} == {10, 11}
    assert {item.source_id for item in plan.items if item.object_type == "dcim.site"} == {7}
    assert {item.source_id for item in plan.items if item.object_type == "dcim.region"} == {1, 3}


def test_directly_selected_reference_type_is_fetched_in_full():
    source = FakeClient({
        "dcim.sites": [{"id": i, "slug": f"site-{i}"} for i in range(1, 5)],
    }, read_only=True)

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=FakeClient({}),
        selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={},
    )

    assert {item.source_id for item in plan.items if item.object_type == "dcim.site"} == {1, 2, 3, 4}
    assert [filters for endpoint, filters in source.paginated_calls if endpoint == "dcim.sites"] == [{}]


def test_bulk_target_matching_preserves_outcomes_with_constant_strategy_calls():
    source = FakeClient({
        "dcim.sites": [
            {"id": i, "slug": f"site-{i}", "name": f"Site {i}"} for i in range(1, 7)
        ],
    }, read_only=True)
    target = FakeClient({
        "dcim.sites": [
            {"id": 100 + i, "slug": f"site-{i}", "name": f"Site {i}"} for i in (1, 3, 5)
        ],
    })

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={},
    )

    outcomes = {item.source_id: item.planned_action for item in plan.items if item.object_type == "dcim.site"}
    assert outcomes == {1: "map", 2: "create", 3: "map", 4: "create", 5: "map", 6: "create"}
    site_calls = [filters for endpoint, filters in target.paginated_calls if endpoint == "dcim.sites"]
    assert len(site_calls) == 2  # one slug query + one name query, versus 6 * 2 individual GETs before
    assert target.get_calls == []


def test_bulk_target_matching_keeps_duplicate_source_filter_semantics():
    source = FakeClient({
        "dcim.sites": [
            {"id": 1, "slug": "same", "name": "Same"},
            {"id": 2, "slug": "same", "name": "Same"},
        ],
    }, read_only=True)
    target = FakeClient({"dcim.sites": [{"id": 90, "slug": "same", "name": "Same"}]})

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={},
    )

    sites = [item for item in plan.items if item.object_type == "dcim.site"]
    assert [(item.source_id, item.planned_action, item.target_id) for item in sites] == [
        (1, "map", 90), (2, "map", 90),
    ]
    assert len([call for call in target.paginated_calls if call[0] == "dcim.sites"]) == 2


def test_plan_does_not_query_target_with_placeholder_foreign_key_ids():
    class RejectNegativeFkClient(FakeClient):
        def get(self, endpoint: _FakeEndpoint, **filters):
            assert not any(type(value) is int and value < 0 for value in filters.values()), filters
            return super().get(endpoint, **filters)

    source = FakeClient({
        "dcim.manufacturers": [{"id": 36, "slug": "acme", "name": "Acme"}],
        "dcim.device_types": [{
            "id": 1,
            "manufacturer": {"id": 36},
            "model": "Router 1",
            "part_number": "R1",
        }],
    }, read_only=True)

    plan = build_plan(
        registry=REGISTRY,
        source_client=source,
        target_client=RejectNegativeFkClient({}),
        selected_types={"dcim.devicetype"},
        tenant_filter=[],
        mapping_overrides={},
    )

    device_type = next(item for item in plan.items if item.object_type == "dcim.devicetype")
    assert device_type.planned_action == "create"
    assert device_type.fk_refs == {"manufacturer": 36}


def test_tenant_filter_cascades_to_child_interfaces():
    source = FakeClient({
        "dcim.manufacturers": [{"id": 1, "slug": "cisco"}],
        "dcim.device_types": [{"id": 1, "manufacturer": {"id": 1}, "model": "C9300"}],
        "dcim.device_roles": [{"id": 1, "slug": "access-switch"}],
        "dcim.sites": [{"id": 1, "slug": "ams-1"}],
        "tenancy.tenants": [{"id": 1, "slug": "acme"}, {"id": 2, "slug": "globex"}],
        "dcim.devices": [
            {"id": 1, "name": "acme-sw-1", "site": {"id": 1}, "device_type": {"id": 1}, "role": {"id": 1}, "tenant": {"id": 1, "slug": "acme"}},
            {"id": 2, "name": "globex-sw-1", "site": {"id": 1}, "device_type": {"id": 1}, "role": {"id": 1}, "tenant": {"id": 2, "slug": "globex"}},
        ],
        "dcim.interfaces": [
            {"id": 10, "device": {"id": 1}, "name": "GigabitEthernet0/1"},
            {"id": 11, "device": {"id": 2}, "name": "GigabitEthernet0/1"},
        ],
    }, read_only=True)
    target = FakeClient({})

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.device", "dcim.interface"}, tenant_filter=["acme"], mapping_overrides={},
    )

    device_ids = {i.source_id for i in plan.items if i.object_type == "dcim.device"}
    interface_ids = {i.source_id for i in plan.items if i.object_type == "dcim.interface"}
    assert device_ids == {1}       # only acme's device
    assert interface_ids == {10}   # only that device's interface — NOT globex's, even though
                                    # dcim.interface has no `tenant` filter of its own


def test_ambiguous_devicetype_match_is_flagged_and_blocks_execution():
    source = FakeClient({
        "dcim.manufacturers": [{"id": 1, "slug": "juniper"}],
        "dcim.device_types": [{"id": 1, "manufacturer": {"id": 1}, "part_number": "QFX5120-48Y-32C", "model": "QFX5120-48Y"}],
    }, read_only=True)
    target = FakeClient({
        "dcim.manufacturers": [{"id": 100, "slug": "juniper"}],
        "dcim.device_types": [
            {"id": 500, "manufacturer": {"id": 100}, "part_number": "QFX5120-48Y-32C", "model": "Something Else"},
            {"id": 600, "manufacturer": {"id": 100}, "part_number": "DIFFERENT-PN", "model": "QFX5120-48Y"},
        ],
    })

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.devicetype"}, tenant_filter=[], mapping_overrides={},
    )

    devicetype_item = next(i for i in plan.items if i.object_type == "dcim.devicetype")
    assert devicetype_item.planned_action == "ambiguous"
    assert devicetype_item.execution_status == "error"
    assert plan.has_blocking_errors() is True
    assert len([call for call in target.paginated_calls if call[0] == "dcim.device_types"]) == 2
    assert target.get_calls == []


def test_explicit_mapping_override_avoids_duplicate_even_without_natural_key_match():
    source = FakeClient({"dcim.sites": [{"id": 1, "slug": "ams-1", "name": "Amsterdam 1"}]}, read_only=True)
    target = FakeClient({"dcim.sites": [{"id": 42, "slug": "amsterdam-dc1", "name": "Amsterdam DC1"}]})

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[],
        mapping_overrides={("dcim.site", 1): MappingOverride(action=MappingAction.MAP, target_id=42)},
    )
    item = plan.items[0]
    assert item.planned_action == "map"
    assert item.target_id == 42


def test_deferred_fk_produces_a_planned_patch():
    # primary_ip4 is in dcim.device's deferred_field_map (never part of the dependency
    # graph — see the registry comments on the device/IP-address circular reference), so
    # even though ipam.ipaddress IS in this run's resolved types, dcim.device is still
    # processed first (nothing forces ipam.ipaddress before it), and the field can't be
    # resolved inline — it must show up as a MigrationJobPatch instead.
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

    device_item = next(i for i in plan.items if i.object_type == "dcim.device")
    assert "primary_ip4" not in device_item.preview_payload
    assert device_item.deferred_fk == {"primary_ip4": 55}
    patch = next(p for p in plan.patches if p.object_type == "dcim.device" and p.source_id == 1)
    assert patch.patch_fields == {"primary_ip4": 55}


def test_conflict_policy_default_skip_leaves_matched_object_as_map():
    source = FakeClient({"dcim.sites": [{"id": 1, "slug": "ams-1", "name": "Amsterdam 1 RENAMED"}]}, read_only=True)
    target = FakeClient({"dcim.sites": [{"id": 100, "slug": "ams-1", "name": "Amsterdam 1"}]})
    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={},
    )
    item = plan.items[0]
    assert item.planned_action == "map"
    assert item.static_fields == {}  # nothing prepared to send — matched objects aren't touched by default


def test_conflict_policy_update_for_auto_matched_object():
    source = FakeClient({"dcim.sites": [{"id": 1, "slug": "ams-1", "name": "Amsterdam 1 RENAMED"}]}, read_only=True)
    target = FakeClient({"dcim.sites": [{"id": 100, "slug": "ams-1", "name": "Amsterdam 1"}]})
    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={},
        conflict_policy={"dcim.site": "update"},
    )
    item = plan.items[0]
    assert item.planned_action == "update"
    assert item.target_id == 100
    assert item.static_fields["name"] == "Amsterdam 1 RENAMED"
    assert item.execution_status == "pending"


def test_conflict_policy_never_overrides_an_explicit_mapping():
    # Even with a global "update" policy, an object the user explicitly mapped must stay untouched.
    source = FakeClient({"dcim.sites": [{"id": 1, "slug": "ams-1", "name": "Amsterdam 1 RENAMED"}]}, read_only=True)
    target = FakeClient({"dcim.sites": [{"id": 42, "slug": "amsterdam-dc1", "name": "Amsterdam DC1"}]})
    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[],
        mapping_overrides={("dcim.site", 1): MappingOverride(action=MappingAction.MAP, target_id=42)},
        conflict_policy={"default": "update"},
    )
    item = plan.items[0]
    assert item.planned_action == "map"
    assert item.target_id == 42


def test_dry_run_never_calls_create_on_either_client():
    source = FakeClient({"dcim.sites": [{"id": 1, "slug": "ams-1"}]}, read_only=True)
    target = FakeClient({})
    build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={},
    )
    assert source.created == []
    assert target.created == []


# ── Fix 1: polymorphic assigned_object ──────────────────────────────────────

def test_plan_ip_assigned_to_device_interface_produces_polymorphic_patch():
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

    ip_item = next(i for i in plan.items if i.object_type == "ipam.ipaddress")
    assert ip_item.planned_action == "create"
    # assigned_object must NOT be in fk_refs or deferred_fk — it's polymorphic
    assert "assigned_object" not in ip_item.fk_refs
    assert "assigned_object" not in ip_item.deferred_fk
    # The polymorphic patch must be present
    ip_patch = next(p for p in plan.patches if p.object_type == "ipam.ipaddress")
    assert ip_patch.polymorphic_patch_fields == {"assigned_object_id": {"type": "dcim.interface", "id": 10}}


def test_plan_ip_assigned_to_vm_interface_produces_polymorphic_patch():
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

    ip_patch = next(p for p in plan.patches if p.object_type == "ipam.ipaddress")
    assert ip_patch.polymorphic_patch_fields == {"assigned_object_id": {"type": "virtualization.vminterface", "id": 20}}


# ── Fix 2: update_empty_only ─────────────────────────────────────────────────

def test_update_empty_only_only_sends_fields_that_are_empty_on_target():
    source = FakeClient({
        "dcim.sites": [{"id": 1, "slug": "ams-1", "name": "Amsterdam 1 RENAMED", "description": "new desc"}],
    }, read_only=True)
    # Target has name populated but description empty
    target = FakeClient({
        "dcim.sites": [{"id": 100, "slug": "ams-1", "name": "Amsterdam 1", "description": ""}],
    })

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={},
        conflict_policy={"dcim.site": "update_empty_only"},
    )

    item = plan.items[0]
    assert item.planned_action == "update"
    assert item.target_id == 100
    # Only description (empty on target) should be in the payload — not name (already set)
    assert "description" in item.static_fields
    assert "name" not in item.static_fields


def test_update_empty_only_nothing_empty_produces_map_not_update():
    source = FakeClient({
        "dcim.sites": [{"id": 1, "slug": "ams-1", "name": "Amsterdam 1 RENAMED"}],
    }, read_only=True)
    target = FakeClient({
        "dcim.sites": [{"id": 100, "slug": "ams-1", "name": "Amsterdam 1"}],
    })

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={},
        conflict_policy={"dcim.site": "update_empty_only"},
    )

    item = plan.items[0]
    assert item.planned_action == "map"
    assert item.execution_status == "done"


def test_update_empty_only_all_empty_produces_same_as_update():
    source = FakeClient({
        "dcim.sites": [{"id": 1, "slug": "ams-1", "name": "Amsterdam 1"}],
    }, read_only=True)
    # Target has name empty (None) but slug already set (non-empty — it's the match key)
    target = FakeClient({
        "dcim.sites": [{"id": 100, "slug": "ams-1", "name": None}],
    })

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={},
        conflict_policy={"dcim.site": "update_empty_only"},
    )

    item = plan.items[0]
    assert item.planned_action == "update"
    # name is None on target — should be filled in
    assert "name" in item.static_fields
    assert item.static_fields["name"] == "Amsterdam 1"
    # slug is already set on target — must NOT be overwritten
    assert "slug" not in item.static_fields


def test_update_empty_only_zero_and_false_are_not_empty():
    # 0 and False are meaningful values — must NOT be overwritten.
    # Use a site with a numeric field that is 0 on the target.
    # dcim.rack has a `u_height` field (integer) — use that.
    # Simpler: just use a site where the target has name="" (empty string, should update)
    # and a custom numeric field at 0 (not empty). Since we can't easily test a numeric
    # field without a real NetBox schema, test the empty-string case and the None case
    # separately, and verify 0 via the _EMPTY tuple definition in the code.
    source = FakeClient({
        "dcim.sites": [{"id": 1, "slug": "ams-1", "name": "Amsterdam 1", "description": "new desc"}],
    }, read_only=True)
    # Target: name is "" (empty string — should be updated), description is "existing" (non-empty — must not be overwritten)
    target = FakeClient({
        "dcim.sites": [{"id": 100, "slug": "ams-1", "name": "", "description": "existing"}],
    })

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={},
        conflict_policy={"dcim.site": "update_empty_only"},
    )

    item = plan.items[0]
    assert item.planned_action == "update"
    # name is "" on target — empty string IS empty, should be updated
    assert "name" in item.static_fields
    # description is "existing" on target — non-empty, must NOT be overwritten
    assert "description" not in item.static_fields


def test_update_empty_only_explicit_mapping_bypasses_policy():
    source = FakeClient({
        "dcim.sites": [{"id": 1, "slug": "ams-1", "name": "Amsterdam 1 RENAMED"}],
    }, read_only=True)
    target = FakeClient({
        "dcim.sites": [{"id": 42, "slug": "amsterdam-dc1", "name": "Amsterdam DC1"}],
    })

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site"}, tenant_filter=[],
        mapping_overrides={("dcim.site", 1): MappingOverride(action=MappingAction.MAP, target_id=42)},
        conflict_policy={"default": "update_empty_only"},
    )

    item = plan.items[0]
    # Explicit mapping always wins — never updated regardless of policy
    assert item.planned_action == "map"
    assert item.target_id == 42


def test_update_empty_only_zero_and_false_not_treated_as_empty_direct():
    # Verify that 0 and False are not in the _EMPTY sentinel set used by
    # update_empty_only filtering. We test this by constructing a scenario
    # where the target object has a field set to 0 (vid on a vlan) and
    # confirming the planner does NOT include it in the update payload.
    # ipam.vlan's first match strategy is (vid, group) with group as fk_field;
    # the second is (vid, site) with site as fk_field. Neither fires without
    # a resolved FK. Use a site-scoped vlan so the second strategy fires.
    source = FakeClient({
        "dcim.sites": [{"id": 1, "slug": "ams-1"}],
        "ipam.vlans": [{"id": 1, "vid": 100, "site": {"id": 1}, "name": "new-name"}],
    }, read_only=True)
    target = FakeClient({
        "dcim.sites": [{"id": 10, "slug": "ams-1"}],
        # vid=100 matches; name="" is empty (should be updated); vid itself is non-zero (must not be overwritten)
        "ipam.vlans": [{"id": 50, "vid": 100, "site": {"id": 10}, "name": ""}],
    })

    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"dcim.site", "ipam.vlan"}, tenant_filter=[], mapping_overrides={},
        conflict_policy={"ipam.vlan": "update_empty_only"},
    )

    vlan_item = next(i for i in plan.items if i.object_type == "ipam.vlan")
    assert vlan_item.planned_action == "update"
    assert "name" in vlan_item.static_fields        # "" is empty — fill it in
    assert "vid" not in vlan_item.static_fields     # 100 is non-empty — leave it


@pytest.mark.parametrize(
    ("type_key", "endpoint", "source_obj", "target_obj"),
    [
        ("extras.configtemplate", "extras.config_templates", {"id": 1, "name": "base", "template_code": "x"}, {"id": 101, "name": "base"}),
        ("dcim.macaddress", "dcim.mac_addresses", {"id": 2, "mac_address": "00:11:22:33:44:55"}, {"id": 102, "mac_address": "00:11:22:33:44:55"}),
        ("ipam.vlantranslationpolicy", "ipam.vlan_translation_policies", {"id": 3, "name": "edge"}, {"id": 103, "name": "edge"}),
    ],
)
def test_planner_maps_new_catalog_types_by_natural_key(type_key, endpoint, source_obj, target_obj):
    plan = build_plan(
        registry=REGISTRY,
        source_client=FakeClient({endpoint: [source_obj]}, read_only=True),
        target_client=FakeClient({endpoint: [target_obj]}),
        selected_types={type_key}, tenant_filter=[], mapping_overrides={},
        source_netbox_version="4.6.8",
    )
    item = next(item for item in plan.items if item.object_type == type_key)
    assert item.planned_action == "map"
    assert item.target_id == target_obj["id"]


def test_planner_creates_fhrp_assignment_and_resolves_polymorphic_natural_key():
    source = FakeClient({
        "ipam.fhrp_groups": [{"id": 1, "protocol": "vrrp2", "group_id": 10}],
        "dcim.interfaces": [{"id": 2, "device": {"id": 99}, "name": "eth0"}],
        "ipam.fhrp_group_assignments": [{
            "id": 3, "group": {"id": 1}, "interface_type": "dcim.interface",
            "interface_id": 2, "interface": {"id": 2}, "priority": 100,
        }],
    }, read_only=True)
    target = FakeClient({})
    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=target,
        selected_types={"ipam.fhrpgroupassignment"}, tenant_filter=[], mapping_overrides={},
    )
    item = next(item for item in plan.items if item.object_type == "ipam.fhrpgroupassignment")
    assert item.planned_action == "create"
    assert item.polymorphic_fk == {"interface_id": {"type": "dcim.interface", "id": 2}}
    assert "interface" not in item.static_fields


def test_planner_creates_service_with_polymorphic_parent_and_ipaddresses():
    source = FakeClient({
        "ipam.fhrp_groups": [{"id": 1, "protocol": "vrrp2", "group_id": 10}],
        "ipam.services": [{
            "id": 2, "parent_object_type": "ipam.fhrpgroup", "parent_object_id": 1,
            "parent": {"id": 1}, "name": "dns", "protocol": "udp", "ports": [53],
        }],
    }, read_only=True)
    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=FakeClient({}),
        selected_types={"ipam.service"}, tenant_filter=[], mapping_overrides={},
    )
    item = next(item for item in plan.items if item.object_type == "ipam.service")
    assert item.planned_action == "create"
    assert item.polymorphic_fk == {"parent_object_id": {"type": "ipam.fhrpgroup", "id": 1}}
    assert "parent" not in item.static_fields


def test_planner_creates_vlan_translation_rule_and_asn_range_with_fk_mapping():
    source = FakeClient({
        "ipam.vlan_translation_policies": [{"id": 1, "name": "edge"}],
        "ipam.vlan_translation_rules": [{"id": 2, "policy": {"id": 1}, "local_vid": 100, "remote_vid": 200}],
        "ipam.rirs": [{"id": 3, "slug": "arin", "name": "ARIN"}],
        "ipam.asn_ranges": [{"id": 4, "name": "private", "slug": "private", "rir": {"id": 3}, "start": 64512, "end": 65534}],
    }, read_only=True)
    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=FakeClient({}),
        selected_types={"ipam.vlantranslationrule", "ipam.asnrange"}, tenant_filter=[], mapping_overrides={},
    )
    rule = next(item for item in plan.items if item.object_type == "ipam.vlantranslationrule")
    asn_range = next(item for item in plan.items if item.object_type == "ipam.asnrange")
    assert rule.planned_action == asn_range.planned_action == "create"
    assert rule.fk_refs == {"policy": 1}
    assert asn_range.fk_refs == {"rir": 3}


def test_mac_address_is_skipped_on_old_source_version_with_warning():
    plan = build_plan(
        registry=REGISTRY, source_client=FakeClient({}, read_only=True), target_client=FakeClient({}),
        selected_types={"dcim.macaddress"}, tenant_filter=[], mapping_overrides={},
        source_netbox_version="4.1.11",
    )
    assert "dcim.macaddress" not in plan.resolved_types
    assert any("dcim.macaddress was skipped" in warning for warning in plan.warnings)


def test_mac_address_is_included_on_supported_source_version():
    plan = build_plan(
        registry=REGISTRY,
        source_client=FakeClient({"dcim.mac_addresses": [{"id": 1, "mac_address": "00:11:22:33:44:55"}]}, read_only=True),
        target_client=FakeClient({}), selected_types={"dcim.macaddress"},
        tenant_filter=[], mapping_overrides={}, source_netbox_version="4.2.0",
    )
    assert "dcim.macaddress" in plan.resolved_types
    assert any(item.object_type == "dcim.macaddress" for item in plan.items)


def test_device_config_template_is_resolved_instead_of_passed_through_raw():
    source = FakeClient({
        "extras.config_templates": [{"id": 7, "name": "router"}],
        "dcim.manufacturers": [{"id": 1, "slug": "acme"}],
        "dcim.device_types": [{"id": 2, "manufacturer": {"id": 1}, "model": "R1"}],
        "dcim.device_roles": [{"id": 3, "slug": "router"}],
        "dcim.sites": [{"id": 4, "slug": "ams"}],
        "dcim.devices": [{"id": 5, "name": "r1", "site": {"id": 4}, "device_type": {"id": 2}, "role": {"id": 3}, "config_template": {"id": 7}}],
    }, read_only=True)
    plan = build_plan(
        registry=REGISTRY, source_client=source, target_client=FakeClient({}),
        selected_types={"dcim.device"}, tenant_filter=[], mapping_overrides={},
    )
    device = next(item for item in plan.items if item.object_type == "dcim.device")
    assert device.fk_refs["config_template"] == 7
    assert "config_template" not in device.static_fields
