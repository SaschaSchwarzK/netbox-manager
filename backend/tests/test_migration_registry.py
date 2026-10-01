import sys
from pathlib import Path

import pytest

from app.services.migration.registry import (
    MigrationRegistryError,
    load_registry,
    parse_registry,
    resolve_selection,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import check_migration_registry_consistency as consistency_check  # noqa: E402


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
    assert "dcim.cable" not in selectable
    assert "dcim.device" in selectable
