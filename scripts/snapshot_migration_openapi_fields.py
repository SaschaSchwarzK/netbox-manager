#!/usr/bin/env python3
"""Refresh the compact nested-field fixture from the official NetBox demo."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config/migration-registry.yaml"
OUTPUT_PATH = ROOT / "backend/tests/fixtures/netbox_openapi_nested_fields_4_7_2.json"
SCHEMA_URL = "https://demo.netbox.dev/api/schema/?format=json"


def main() -> None:
    registry = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))["types"]
    with urllib.request.urlopen(SCHEMA_URL, timeout=30) as response:  # noqa: S310 - fixed trusted URL
        schema = json.load(response)
    components = schema["components"]["schemas"]

    def ref_name(value: dict) -> str | None:
        if "$ref" in value:
            return value["$ref"].rsplit("/", 1)[-1]
        for member in value.get("allOf", []):
            if "$ref" in member:
                return member["$ref"].rsplit("/", 1)[-1]
        return None

    def resolve(value: dict) -> dict:
        name = ref_name(value)
        return components[name] if name else value

    nested_fields: dict[str, list[str]] = {}
    for type_key, spec in registry.items():
        if spec.get("out_of_scope"):
            continue
        app_name, endpoint_name = spec["endpoint"].split(".")
        path = f"/api/{app_name}/{endpoint_name.replace('_', '-')}/"
        response_schema = schema["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        paginated = resolve(response_schema)
        item_schema = resolve(paginated["properties"]["results"]["items"])
        fields = []
        for field_name, field_schema in item_schema.get("properties", {}).items():
            target = field_schema.get("items", {}) if field_schema.get("type") == "array" else field_schema
            referenced = ref_name(target)
            if referenced and not referenced.endswith(("Enum", "Request")):
                fields.append(field_name)
        nested_fields[type_key] = sorted(fields)

    payload = {
        "netbox_version": schema["info"]["version"],
        "source": SCHEMA_URL,
        "nested_fields": nested_fields,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
