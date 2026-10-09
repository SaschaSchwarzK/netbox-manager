import sys
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.migration.registry import (
    MigrationRegistryError,
    Registry,
    load_registry,
    parse_registry,
    resolve_selection,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import check_migration_registry_consistency as consistency_check  # noqa: E402


INDEPENDENT_REFERENCE_TYPES = {
    "circuits.circuittype", "circuits.provider", "circuits.providernetwork",
    "dcim.devicerole", "dcim.devicetype", "dcim.location", "dcim.manufacturer",
    "dcim.moduletype", "dcim.platform", "dcim.powerpanel", "dcim.rack", "dcim.rackrole",
    "dcim.region", "dcim.site", "dcim.sitegroup", "ipam.rir", "ipam.role", "ipam.vlangroup",
    "tenancy.tenant", "tenancy.tenantgroup", "virtualization.cluster",
    "virtualization.clustergroup", "virtualization.clustertype",
}


def test_real_registry_loads_and_is_acyclic():
    registry = load_registry(force_reload=True)
    assert "dcim.device" in registry
    assert set(registry.topological_order) == set(registry.types)
    # Every dependency must precede its dependent in the topological order.
    position = {key: i for i, key in enumerate(registry.topological_order)}
    for key, spec in registry.types.items():
        for dep in spec.all_dependencies:
            assert position[dep] < position[key], f"{dep} must precede {key}"


def test_real_registry_matches_tenant_relations_registry():
    errors = consistency_check.find_mismatches()
    assert errors == []


def test_new_object_types_parse_and_resolve_dependency_closure():
    registry = load_registry(force_reload=True)
    expected = {
        "ipam.fhrpgroupassignment", "ipam.service", "dcim.macaddress",
        "extras.configtemplate", "ipam.vlantranslationpolicy",
        "ipam.vlantranslationrule", "ipam.asnrange",
    }
    assert expected <= set(registry.types)
    resolved = resolve_selection(expected, registry)
    assert expected <= set(resolved)
    assert resolved.index("ipam.fhrpgroup") < resolved.index("ipam.fhrpgroupassignment")
    assert resolved.index("ipam.vlantranslationpolicy") < resolved.index("ipam.vlantranslationrule")
    assert resolved.index("ipam.rir") < resolved.index("ipam.asnrange")
    assert registry["dcim.macaddress"].min_netbox_version == (4, 2, 0)


def test_resolve_selection_includes_required_and_optional_dependencies():
    registry = load_registry()
    resolved = resolve_selection({"dcim.device"}, registry)
    assert "dcim.site" in resolved
    assert "dcim.devicetype" in resolved
    assert "dcim.devicerole" in resolved
    assert "tenancy.tenant" in resolved  # optional dependency, still pulled in
    # Dependencies precede the dependent.
    assert resolved.index("dcim.site") < resolved.index("dcim.device")
    assert resolved.index("dcim.devicetype") < resolved.index("dcim.device")


def test_resolve_selection_rejects_unknown_type():
    registry = load_registry()
    with pytest.raises(MigrationRegistryError):
        resolve_selection({"not.a.real.type"}, registry)


def test_self_referential_optional_dependency_does_not_create_a_cycle():
    registry = load_registry()
    spec = registry["dcim.region"]
    assert "dcim.region" not in spec.all_dependencies
    assert spec.self_parent_field == "parent"


def test_cycle_detection_raises():
    doc = {
        "types": {
            "a": {"endpoint": "x.a", "ui_path": "x/a", "dependencies": ["b"], "field_map": {"b_ref": "b"}},
            "b": {"endpoint": "x.b", "ui_path": "x/b", "dependencies": ["a"], "field_map": {"a_ref": "a"}},
        }
    }
    with pytest.raises(MigrationRegistryError, match="Cycle detected"):
        parse_registry(doc)


def test_unknown_dependency_raises():
    doc = {
        "types": {
            "a": {
                "endpoint": "x.a", "ui_path": "x/a", "dependencies": ["nonexistent"],
                "field_map": {"some_field": "nonexistent"},
            },
        }
    }
    with pytest.raises(MigrationRegistryError, match="unknown type"):
        parse_registry(doc)


def test_field_map_missing_entry_for_dependency_raises():
    doc = {
        "types": {
            "a": {"endpoint": "x.a", "ui_path": "x/a", "dependencies": ["b"], "field_map": {}},
            "b": {"endpoint": "x.b", "ui_path": "x/b"},
        }
    }
    with pytest.raises(MigrationRegistryError, match="has no field for dependencies"):
        parse_registry(doc)


def test_field_map_extra_entry_raises():
    doc = {
        "types": {
            "a": {"endpoint": "x.a", "ui_path": "x/a", "field_map": {"b_ref": "b"}},
            "b": {"endpoint": "x.b", "ui_path": "x/b"},
        }
    }
    with pytest.raises(MigrationRegistryError, match="not listed in dependencies"):
        parse_registry(doc)


def test_field_for_returns_field_name():
    registry = load_registry()
    assert registry.field_for("dcim.device", "dcim.site") == "site"
    assert registry.field_for("dcim.device", "dcim.devicerole") == "role"


def test_match_strategy_fk_fields_must_be_subset_of_fields():
    doc = {
        "types": {
            "a": {
                "endpoint": "x.a", "ui_path": "x/a",
                "match_strategies": [{"fields": ["slug"], "fk_fields": ["manufacturer"]}],
            },
        }
    }
    with pytest.raises(MigrationRegistryError, match="fk_fields"):
        parse_registry(doc)


def test_selectable_types_excludes_out_of_scope():
    registry = load_registry()
    selectable = registry.selectable_types()
    assert "dcim.cable" in selectable
    assert "dcim.device" in selectable


def test_reference_catalog_types_are_independently_selectable():
    registry = load_registry(force_reload=True)
    assert INDEPENDENT_REFERENCE_TYPES <= set(registry.selectable_types())
    assert {
        key for key, spec in registry.in_scope_types().items() if not spec.selectable
    } == {"extras.tag"}


def test_making_reference_types_selectable_does_not_change_dependency_closure():
    registry = load_registry(force_reload=True)
    prior_selectability = Registry(
        types={
            key: replace(spec, selectable=False) if key in INDEPENDENT_REFERENCE_TYPES else spec
            for key, spec in registry.types.items()
        },
        topological_order=registry.topological_order,
    )
    assert resolve_selection({"dcim.device"}, registry) == resolve_selection(
        {"dcim.device"}, prior_selectability
    )


def test_power_feed_types_and_dependency_order_are_registered():
    registry = load_registry(force_reload=True)
    resolved = resolve_selection({"dcim.powerfeed"}, registry)
    assert resolved.index("dcim.site") < resolved.index("dcim.powerpanel") < resolved.index("dcim.powerfeed")
    assert registry["dcim.powerpanel"].tenant_relation == "site__tenant"
    assert registry["dcim.powerpanel"].tenant_filterable is False
    assert registry["dcim.powerfeed"].tenant_relation == "tenant"
    assert registry["dcim.powerfeed"].tenant_filterable is True


def test_circuit_types_and_polymorphic_termination_are_registered_in_order():
    registry = load_registry(force_reload=True)
    resolved = resolve_selection({"circuits.circuittermination"}, registry)
    assert resolved.index("circuits.provider") < resolved.index("circuits.circuit")
    assert resolved.index("circuits.circuittype") < resolved.index("circuits.circuit")
    assert resolved.index("circuits.circuit") < resolved.index("circuits.circuittermination")
    poly = registry["circuits.circuittermination"].polymorphic_field_map["termination_id"]
    assert poly.deferred is False
    assert poly.type_values["circuits.providernetwork"] == "circuits.providernetwork"


def test_l2vpn_uses_vpn_app_and_shared_inline_polymorphic_mechanism():
    registry = load_registry(force_reload=True)
    spec = registry["vpn.l2vpntermination"]
    assert spec.endpoint == "vpn.l2vpn_terminations"
    assert set(spec.polymorphic_field_map["assigned_object_id"].type_values) == {
        "dcim.interface", "virtualization.vminterface", "ipam.vlan",
    }
    assert spec.polymorphic_field_map["assigned_object_id"].deferred is False
    resolved = resolve_selection({"vpn.l2vpntermination"}, registry)
    assert resolved.index("vpn.l2vpn") < resolved.index("vpn.l2vpntermination")


def test_cable_is_ordered_after_every_supported_termination_type():
    registry = load_registry(force_reload=True)
    cable = registry["dcim.cable"]
    termination_types = {
        "dcim.interface", "dcim.consoleport", "dcim.consoleserverport", "dcim.powerport",
        "dcim.poweroutlet", "dcim.frontport", "dcim.rearport", "circuits.circuittermination",
        "dcim.powerfeed",
    }
    assert termination_types <= set(cable.dependencies)
    position = {key: index for index, key in enumerate(registry.topological_order)}
    assert all(position[key] < position["dcim.cable"] for key in termination_types)
    assert set(cable.polymorphic_list_field_map) == {"a_terminations", "b_terminations"}


def test_virtual_device_context_dependency_closure_and_interface_order_are_acyclic():
    registry = load_registry(force_reload=True)
    resolved = resolve_selection({"dcim.interface"}, registry)
    vdc = registry["dcim.virtualdevicecontext"]
    assert vdc.endpoint == "dcim.virtual_device_contexts"
    assert vdc.dependencies == ("dcim.device",)
    assert set(vdc.deferred_field_map) == {"primary_ip4", "primary_ip6"}
    assert registry["dcim.interface"].field_map["vdcs"] == "dcim.virtualdevicecontext"
    assert resolved.index("dcim.device") < resolved.index("dcim.virtualdevicecontext") < resolved.index("dcim.interface")
