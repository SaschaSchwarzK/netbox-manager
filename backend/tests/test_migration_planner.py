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
            if obj.get(key) != value:
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
        self._next_id = 90000

    def paginated(self, endpoint: _FakeEndpoint, **filters):
        for obj in self.data.get(endpoint.key, []):
            if _matches(obj, filters):
                yield obj

    def get(self, endpoint: _FakeEndpoint, **filters):
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

    def update_by_id(self, endpoint: _FakeEndpoint, id_: int, payload: dict):
        if self.read_only:
            raise AssertionError("must never write to a read-only client")
        for obj in self.data.get(endpoint.key, []):
            if obj["id"] == id_:
                obj.update(payload)
                return obj
        raise AssertionError(f"update_by_id: no object with id={id_} on {endpoint.key}")


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
        "ipam.ip_addresses": [{"id": 55, "address": "10.0.0.1/24", "assigned_object": {"id": 10}}],
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
