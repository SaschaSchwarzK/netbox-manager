"""Offline guard against nested NetBox API fields leaking into write payloads.

The fixture is a compact snapshot of nested-object fields from NetBox's
OpenAPI schema. Refresh it deliberately with:

    python scripts/snapshot_migration_openapi_fields.py

Review every resulting difference: add a field/deferred/polymorphic mapping,
or explicitly document it as intentionally unconverted in the registry.
"""
import json
from pathlib import Path

from app.services.migration.registry import load_registry


FIXTURE = Path(__file__).parent / "fixtures" / "netbox_openapi_nested_fields_4_7_2.json"


def test_every_nested_openapi_field_is_mapped_or_explicitly_unconverted():
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    registry = load_registry(force_reload=True)
    in_scope = registry.in_scope_types()
    assert set(snapshot["nested_fields"]) == set(in_scope), (
        "The registry type set changed; refresh the OpenAPI nested-field fixture and review the diff."
    )

    uncovered: list[str] = []
    for type_key, expected_fields in snapshot["nested_fields"].items():
        spec = registry[type_key]
        accounted = (
            set(spec.all_field_map)
            | set(spec.polymorphic_field_map)
            | set(spec.intentionally_unconverted_fields)
        )
        uncovered.extend(
            f"{type_key}.{field_name}"
            for field_name in expected_fields
            if field_name not in accounted
        )

    assert uncovered == [], "Nested API fields are not accounted for: " + ", ".join(uncovered)
