"""
Loads and validates config/migration-registry.yaml: the single source of
truth for which NetBox object types the migration feature knows about, their
pynetbox endpoints, their dependency graph, and how to match an object
against an existing one on the target instance.

Mirrors the fail-closed validation style of app.tenant_permissions
(load_policy_files / validate_template): a malformed registry raises at load
time rather than producing confusing behavior mid-migration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_HERE = Path(__file__).resolve()
ROOT = next(
    (candidate for candidate in (_HERE.parents[4], _HERE.parents[3])
     if (candidate / "config/migration-registry.yaml").exists()),
    _HERE.parents[4],
)


class MigrationRegistryError(RuntimeError):
    pass


# Every NetBox object type carries a `tags` field (a list of nested tag
# objects on read, a list of tag identifiers on write) — far too universal to
# declare per-type in field_map, and NetBox's `extras.tags` endpoint has no
# complex dependencies of its own. Handled the same way self_parent_field is:
# as an always-present, implicit entry in TypeSpec.all_field_map, so the
# existing list-aware FK resolution (resolve_fk_refs / sanitize.extract_fields)
# picks it up for free instead of needing a fourth parallel field-map concept.
UNIVERSAL_TAG_FIELD = "tags"
UNIVERSAL_TAG_TYPE = "extras.tag"


@dataclass(frozen=True)
class MatchStrategy:
    fields: tuple[str, ...]
    fk_fields: tuple[str, ...]


@dataclass(frozen=True)
class PolymorphicFieldSpec:
    """
    A single JSON field whose dependency type varies per object, determined
    at runtime by reading a sibling discriminator field on the same source
    object. Used for NetBox GenericForeignKey fields like `assigned_object`
    on ipam.ipaddress, where `assigned_object_type` (e.g. "dcim.interface"
    or "virtualization.vminterface") names the actual type.

    Always treated as deferred (patch-only), same as deferred_field_map:
    there is no ordering guarantee that the referenced type is resolved
    before this type, so it can never be resolved inline.
    """
    discriminator_field: str          # sibling field that names the type, e.g. "assigned_object_type"
    type_values: dict[str, str]       # {discriminator_value: dependency_type_key}


@dataclass(frozen=True)
class TypeSpec:
    key: str
    endpoint: str
    ui_path: str
    selectable: bool
    dependencies: tuple[str, ...]
    optional_dependencies: tuple[str, ...]
    field_map: dict[str, str]
    # FK fields deliberately EXCLUDED from the dependency graph because
    # including them would create a cycle at the type level (a device's
    # primary_ip4 points at an IP address, which may be assigned to an
    # interface, which belongs BACK to that same device). There is no
    # ordering guarantee that the referenced type is resolved by the time
    # this type is processed, so these fields are never resolved inline —
    # they always go through build_payload's "defer" path and end up as a
    # MigrationJobPatch, applied once every phase-`primary` item is done.
    deferred_field_map: dict[str, str]
    # GenericForeignKey fields whose dependency type varies per object,
    # determined by a sibling discriminator field. Always deferred (patch-
    # only), same as deferred_field_map. Stored as {field_name: PolymorphicFieldSpec}.
    polymorphic_field_map: dict[str, PolymorphicFieldSpec]
    tenant_relation: str | None
    tenant_filterable: bool
    match_strategies: tuple[MatchStrategy, ...]
    intentionally_unconverted_fields: tuple[str, ...] = ()
    min_netbox_version: tuple[int, int, int] | None = None
    out_of_scope: bool = False
    out_of_scope_reason: str | None = None
    self_parent_field: str | None = None

    @property
    def all_dependencies(self) -> tuple[str, ...]:
        """
        Every OTHER type this one references, required or optional — used for
        the type-level graph/ordering. Self-references (a region's optional
        parent region, a site group's parent group, a location's parent
        location) are intentionally excluded here: they don't affect the
        order in which types are processed relative to each other, only the
        order objects of that same type must be created in (parent before
        child) — which the planner handles separately, by sorting each
        type's objects on their parent-chain depth.
        """
        return tuple(dict.fromkeys(d for d in (*self.dependencies, *self.optional_dependencies) if d != self.key))

    @property
    def all_field_map(self) -> dict[str, str]:
        """field_map + deferred_field_map merged — every static-type FK field this type has."""
        extra_self_parent = {self.self_parent_field: self.key} if self.self_parent_field else {}
        extra_tags = {} if self.key == UNIVERSAL_TAG_TYPE else {UNIVERSAL_TAG_FIELD: UNIVERSAL_TAG_TYPE}
        return {**self.field_map, **self.deferred_field_map, **extra_self_parent, **extra_tags}


@dataclass(frozen=True)
class Registry:
    types: dict[str, TypeSpec]
    # Full topological order (dependencies before dependents) across every
    # type in the registry. A prefix-filtered view of this is used to order
    # any selected subset, so ordering is always consistent across jobs.
    topological_order: tuple[str, ...]

    def field_for(self, type_key: str, dependency_key: str) -> str:
        """
        The source JSON field name on `type_key`'s objects that references
        `dependency_key`. If more than one field maps to the same dependency
        type, the first declared (dict insertion order) is returned — fine
        for this method's callers (planner's "which field scopes this child
        type by its parent"), which only ever deal with a single, required,
        unambiguous parent relationship.
        """
        spec = self.types[type_key]
        for field_name, dep_key in spec.field_map.items():
            if dep_key == dependency_key:
                return field_name
        if dependency_key == spec.key and spec.self_parent_field:
            return spec.self_parent_field
        raise MigrationRegistryError(f"{type_key} has no field_map entry for dependency {dependency_key!r}")

    def all_field_map_for(self, type_key: str) -> dict[str, str]:
        """field_map + deferred_field_map merged — every static-type FK field this type has."""
        return self.types[type_key].all_field_map

    def __getitem__(self, key: str) -> TypeSpec:
        return self.types[key]

    def __contains__(self, key: str) -> bool:
        return key in self.types

    def selectable_types(self) -> list[str]:
        return sorted(k for k, spec in self.types.items() if spec.selectable and not spec.out_of_scope)

    def in_scope_types(self) -> dict[str, TypeSpec]:
        return {k: v for k, v in self.types.items() if not v.out_of_scope}


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise MigrationRegistryError(f"Cannot read YAML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MigrationRegistryError(f"{path}: top level must be a mapping")
    return value


def _parse_match_strategies(key: str, raw: Any) -> tuple[MatchStrategy, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise MigrationRegistryError(f"{key}.match_strategies must be a list")
    strategies = []
    for index, entry in enumerate(raw):
        where = f"{key}.match_strategies[{index}]"
        if not isinstance(entry, dict):
            raise MigrationRegistryError(f"{where} must be a mapping")
        fields = entry.get("fields")
        fk_fields = entry.get("fk_fields", [])
        if not isinstance(fields, list) or not fields or not all(isinstance(f, str) for f in fields):
            raise MigrationRegistryError(f"{where}.fields must be a non-empty list of strings")
        if not isinstance(fk_fields, list) or not all(isinstance(f, str) for f in fk_fields):
            raise MigrationRegistryError(f"{where}.fk_fields must be a list of strings")
        unknown_fk = set(fk_fields) - set(fields)
        if unknown_fk:
            raise MigrationRegistryError(f"{where}.fk_fields contains fields not in `fields`: {sorted(unknown_fk)}")
        strategies.append(MatchStrategy(fields=tuple(fields), fk_fields=tuple(fk_fields)))
    return tuple(strategies)


def _parse_type(key: str, raw: Any) -> TypeSpec:
    if not isinstance(raw, dict):
        raise MigrationRegistryError(f"{key} must be a mapping")
    for required in ("endpoint", "ui_path"):
        if not isinstance(raw.get(required), str) or not raw[required].strip():
            raise MigrationRegistryError(f"{key}.{required} must be a non-empty string")
    self_parent_field = raw.get("self_parent_field")
    if self_parent_field is not None and not isinstance(self_parent_field, str):
        raise MigrationRegistryError(f"{key}.self_parent_field must be a string or null")
    dependencies = raw.get("dependencies", [])
    optional_dependencies = raw.get("optional_dependencies", [])
    if not isinstance(dependencies, list) or not all(isinstance(d, str) for d in dependencies):
        raise MigrationRegistryError(f"{key}.dependencies must be a list of strings")
    if not isinstance(optional_dependencies, list) or not all(isinstance(d, str) for d in optional_dependencies):
        raise MigrationRegistryError(f"{key}.optional_dependencies must be a list of strings")
    tenant_relation = raw.get("tenant_relation")
    if tenant_relation is not None and not isinstance(tenant_relation, str):
        raise MigrationRegistryError(f"{key}.tenant_relation must be a string or null")
    min_netbox_version = _parse_min_netbox_version(key, raw.get("min_netbox_version"))
    intentionally_unconverted_fields = raw.get("intentionally_unconverted_fields", [])
    if not isinstance(intentionally_unconverted_fields, list) or not all(
        isinstance(value, str) and value for value in intentionally_unconverted_fields
    ):
        raise MigrationRegistryError(f"{key}.intentionally_unconverted_fields must be a list of non-empty strings")

    field_map = raw.get("field_map", {})
    if not isinstance(field_map, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in field_map.items()):
        raise MigrationRegistryError(f"{key}.field_map must be a mapping of field-name -> type-key strings")
    all_deps = set(dependencies) | set(optional_dependencies)
    field_map_dep_values = set(field_map.values())
    # A dependency type may be covered by polymorphic_field_map instead of field_map;
    # parse polymorphic_field_map first so we can exclude those types from the field_map check.
    polymorphic_field_map = _parse_polymorphic_field_map(key, raw.get("polymorphic_field_map", {}), all_deps)
    poly_covered_types = {dep for spec in polymorphic_field_map.values() for dep in spec.type_values.values()}
    missing_map_entries = all_deps - field_map_dep_values - poly_covered_types
    if missing_map_entries:
        raise MigrationRegistryError(f"{key}.field_map has no field for dependencies: {sorted(missing_map_entries)}")
    extra_map_entries = field_map_dep_values - all_deps
    if extra_map_entries:
        raise MigrationRegistryError(f"{key}.field_map references types not listed in dependencies: {sorted(extra_map_entries)}")

    deferred_field_map = raw.get("deferred_field_map", {})
    if not isinstance(deferred_field_map, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in deferred_field_map.items()
    ):
        raise MigrationRegistryError(f"{key}.deferred_field_map must be a mapping of field-name -> type-key strings")
    overlapping_fields = set(field_map) & set(deferred_field_map)
    if overlapping_fields:
        raise MigrationRegistryError(f"{key}: fields {sorted(overlapping_fields)} in both field_map and deferred_field_map")

    poly_overlap = (set(field_map) | set(deferred_field_map)) & set(polymorphic_field_map)
    if poly_overlap:
        raise MigrationRegistryError(f"{key}: fields {sorted(poly_overlap)} appear in both polymorphic_field_map and field_map/deferred_field_map")

    match_strategies = _parse_match_strategies(key, raw.get("match_strategies"))
    all_fk_field_names = set(field_map) | set(deferred_field_map) | set(polymorphic_field_map)
    if self_parent_field:
        all_fk_field_names.add(self_parent_field)
    for strategy in match_strategies:
        unknown_fk = set(strategy.fk_fields) - all_fk_field_names
        if unknown_fk:
            raise MigrationRegistryError(
                f"{key}.match_strategies references fk_fields {sorted(unknown_fk)} "
                f"not declared in field_map or deferred_field_map (keys: {sorted(all_fk_field_names)})"
            )

    return TypeSpec(
        key=key,
        endpoint=raw["endpoint"],
        ui_path=raw["ui_path"].strip("/"),
        selectable=bool(raw.get("selectable", False)),
        dependencies=tuple(dependencies),
        optional_dependencies=tuple(optional_dependencies),
        field_map=field_map,
        deferred_field_map=deferred_field_map,
        polymorphic_field_map=polymorphic_field_map,
        intentionally_unconverted_fields=tuple(intentionally_unconverted_fields),
        tenant_relation=tenant_relation,
        tenant_filterable=bool(raw.get("tenant_filterable", False)),
        match_strategies=match_strategies,
        min_netbox_version=min_netbox_version,
        out_of_scope=bool(raw.get("out_of_scope", False)),
        out_of_scope_reason=raw.get("out_of_scope_reason"),
        self_parent_field=self_parent_field,
    )


def _parse_min_netbox_version(key: str, raw: Any) -> tuple[int, int, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise MigrationRegistryError(f"{key}.min_netbox_version must be a version string")
    parts = raw.split(".")
    if len(parts) not in (2, 3) or not all(part.isdigit() for part in parts):
        raise MigrationRegistryError(f"{key}.min_netbox_version must look like '4.2' or '4.2.0'")
    values = [int(part) for part in parts]
    return tuple(values + [0] * (3 - len(values)))


def _parse_polymorphic_field_map(
    key: str,
    raw: Any,
    all_deps: set[str],
) -> dict[str, PolymorphicFieldSpec]:
    """
    Parses and validates the `polymorphic_field_map` block. Each entry:
      field_name:
        discriminator_field: <sibling field that names the type>
        type_values: {discriminator_value: dependency_type_key}
    Every dependency_type_key in type_values must appear in the type's
    dependencies or optional_dependencies (same rule as field_map).
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise MigrationRegistryError(f"{key}.polymorphic_field_map must be a mapping")
    result: dict[str, PolymorphicFieldSpec] = {}
    for field_name, spec_raw in raw.items():
        where = f"{key}.polymorphic_field_map.{field_name}"
        if not isinstance(spec_raw, dict):
            raise MigrationRegistryError(f"{where} must be a mapping")
        disc = spec_raw.get("discriminator_field")
        if not isinstance(disc, str) or not disc:
            raise MigrationRegistryError(f"{where}.discriminator_field must be a non-empty string")
        type_values = spec_raw.get("type_values")
        if not isinstance(type_values, dict) or not type_values:
            raise MigrationRegistryError(f"{where}.type_values must be a non-empty mapping")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in type_values.items()):
            raise MigrationRegistryError(f"{where}.type_values must map strings to strings")
        # Every referenced dependency type must be declared in dependencies/optional_dependencies.
        unknown_types = set(type_values.values()) - all_deps
        if unknown_types:
            raise MigrationRegistryError(
                f"{where}.type_values references types not in dependencies/optional_dependencies: {sorted(unknown_types)}"
            )
        result[field_name] = PolymorphicFieldSpec(discriminator_field=disc, type_values=dict(type_values))
    return result


def _topological_sort(types: dict[str, TypeSpec]) -> tuple[str, ...]:
    """
    Kahn's algorithm over the full registry graph (dependencies + optional
    dependencies as edges: dependency must come before dependent). Raises on
    any cycle. A stable, deterministic order is produced by always picking
    the lexicographically smallest ready node, so job plans are reproducible.
    """
    remaining_edges: dict[str, set[str]] = {k: set(spec.all_dependencies) for k, spec in types.items()}
    ordered: list[str] = []
    ready = sorted(k for k, deps in remaining_edges.items() if not deps)
    visited: set[str] = set()
    while ready:
        node = ready.pop(0)
        if node in visited:
            continue
        visited.add(node)
        ordered.append(node)
        newly_ready = []
        for k, deps in remaining_edges.items():
            if node in deps:
                deps.discard(node)
                if not deps and k not in visited:
                    newly_ready.append(k)
        ready = sorted(set(ready) | set(newly_ready))
    if len(ordered) != len(types):
        cyclic = sorted(set(types) - visited)
        raise MigrationRegistryError(f"Cycle detected in migration registry dependency graph, involving: {cyclic}")
    return tuple(ordered)


def parse_registry(doc: dict[str, Any]) -> Registry:
    raw_types = doc.get("types")
    if not isinstance(raw_types, dict) or not raw_types:
        raise MigrationRegistryError("migration-registry.yaml: `types` must be a non-empty mapping")
    types = {key: _parse_type(key, raw) for key, raw in raw_types.items()}
    for key, spec in types.items():
        for dep in spec.all_dependencies:
            if dep not in types:
                raise MigrationRegistryError(f"{key} depends on unknown type {dep!r}")
        for dep in spec.deferred_field_map.values():
            if dep not in types:
                raise MigrationRegistryError(f"{key}.deferred_field_map references unknown type {dep!r}")
        for field_name, poly_spec in spec.polymorphic_field_map.items():
            for dep in poly_spec.type_values.values():
                if dep not in types:
                    raise MigrationRegistryError(
                        f"{key}.polymorphic_field_map.{field_name} references unknown type {dep!r}"
                    )
    topo = _topological_sort(types)
    return Registry(types=types, topological_order=topo)


_cache: Registry | None = None


def load_registry(root: Path = ROOT, *, force_reload: bool = False) -> Registry:
    global _cache
    if _cache is not None and not force_reload:
        return _cache
    doc = _load_yaml(root / "config/migration-registry.yaml")
    _cache = parse_registry(doc)
    return _cache


def resolve_selection(selected: set[str], registry: Registry) -> list[str]:
    """
    Transitive closure of `selected` over both required and optional
    dependencies, returned in the registry's global topological order (so
    dependencies always precede dependents, and ordering is stable across
    runs regardless of selection order).
    """
    unknown = selected - set(registry.types)
    if unknown:
        raise MigrationRegistryError(f"Unknown type(s) selected: {sorted(unknown)}")
    closure: set[str] = set()
    frontier = set(selected)
    while frontier:
        closure |= frontier
        next_frontier: set[str] = set()
        for key in frontier:
            spec = registry[key]
            next_frontier |= set(spec.all_dependencies) - closure
        frontier = next_frontier
    return [key for key in registry.topological_order if key in closure]
