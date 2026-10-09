#!/usr/bin/env python3
"""
Cross-checks config/migration-registry.yaml against config/tenant-relations.yaml:
for every object type present in both files, `tenant_relation` (migration
registry) must equal `path` (tenant relations registry) exactly. Pure static
YAML comparison — no live NetBox needed, unlike check_tenant_relations.py.

Run as part of the test suite (backend/tests/test_migration_registry.py) and
standalone:
  python3 scripts/check_migration_registry_consistency.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_relations(root: Path = ROOT) -> dict[str, object]:
    doc = yaml.safe_load((root / "config/tenant-relations.yaml").read_text(encoding="utf-8")) or {}
    return doc.get("relations", {})


def load_migration_types(root: Path = ROOT) -> dict[str, object]:
    doc = yaml.safe_load((root / "config/migration-registry.yaml").read_text(encoding="utf-8")) or {}
    return doc.get("types", {})


def find_mismatches(root: Path = ROOT) -> list[str]:
    relations = load_relations(root)
    migration_types = load_migration_types(root)
    errors = []
    for object_type, migration_entry in sorted(migration_types.items()):
        if object_type not in relations:
            continue  # tenant-relations.yaml doesn't have to cover every migratable type
        relation_entry = relations[object_type]
        expected = relation_entry.get("path") if isinstance(relation_entry, dict) else relation_entry
        actual = migration_entry.get("tenant_relation") if isinstance(migration_entry, dict) else None
        if actual != expected:
            errors.append(
                f"{object_type}: migration-registry.yaml tenant_relation={actual!r} "
                f"!= tenant-relations.yaml path={expected!r}"
            )
    return errors


def main() -> int:
    errors = find_mismatches()
    for error in errors:
        print(f"MISMATCH  {error}")
    if not errors:
        print("OK: migration-registry.yaml and tenant-relations.yaml agree on every shared object type.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
