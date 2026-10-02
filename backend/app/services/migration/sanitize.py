"""
Turns a raw source object (as returned by RateLimitedClient.paginated —
NetBox's full nested representation) into what a create/update call against
the target needs, split into two independent steps:

1. extract_fields() — a PURE function of (type_spec, source_obj): strips
   read-only/server fields, drops custom fields (out of scope for v1),
   unwraps choice fields, and separates the result into `static_fields`
   (plain values, never touched again) and two {field_name: source_fk_id}
   maps for the FK fields (regular and deferred). This never looks at an
   id_map, so its output is a true plan-time snapshot: calling it again
   later, or after a crash, against the same source_obj always produces the
   identical result.

2. resolve_fk_refs() — resolves one of those {field_name: source_fk_id}
   maps against WHATEVER id_map is current, returning what could be
   resolved and what couldn't. This is deliberately re-run rather than
   cached, and against a DIFFERENT id_map depending on when it's called:
    - At plan time, the id_map contains real ids for matched/mapped objects
      and PLACEHOLDER (negative) ids for objects that are merely planned to
      be created (see planner.py) — good enough to produce an accurate
      preview payload and to let matching work symbolically for objects
      that depend on other not-yet-created objects, but not something that
      should ever be sent to the target's API.
    - At execution time, the id_map contains only real ids, built up
      incrementally as items are actually created/mapped in order — by the
      time a regular (non-deferred) FK field is resolved this way, its
      dependency is GUARANTEED to already be real (topological order), so
      resolution always succeeds for those. Deferred fields (see
      TypeSpec.deferred_field_map) carry no such guarantee, which is why
      they're never included in the create/update payload at all and
      always go through the phase-`patch` second pass instead.

Mirrors the payload-cleaning approach already used in
app.services.netbox_customfields (strip read-only fields, unwrap
{"value": ...} choice fields, resolve nested reference dicts to ids) but
generalized across every registry type via field_map instead of being
hand-written per field.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.migration.matcher import IdMap
from app.services.migration.registry import UNIVERSAL_TAG_FIELD, UNIVERSAL_TAG_TYPE, TypeSpec

# Present on every NetBox object's representation; never sent back on create/update.
_ALWAYS_STRIP = {
    "id", "url", "display", "display_url", "created", "last_updated",
    "_depth", "count_ipaddresses", "count_prefixes", "count_vlans", "count_devices",
    "device_count", "vm_count", "rack_count", "interface_count", "circuit_count",
}

# Fields the migration feature intentionally does not carry over (see plan: out of scope for v1).
_ALWAYS_DROP = {"custom_fields"}


def _choice_value(value: Any) -> Any:
    """NetBox choice fields serialize as {"value": ..., "label": ...}; the API wants just the value back."""
    if isinstance(value, dict) and "value" in value and "label" in value:
        return value["value"]
    return value


def _unwrap_fk_value(value: Any) -> Any:
    """Return a scalar id or list of ids from a single nested object/list-like FK reference."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("id") if "id" in value else value
    if isinstance(value, (list, tuple, set)):
        items = []
        for item in value:
            item_value = _unwrap_fk_value(item)
            if item_value is not None:
                items.append(item_value)
        return items
    return value


@dataclass(frozen=True)
class ExtractedFields:
    static_fields: dict[str, Any]         # plain, non-FK fields — final as-is, never re-resolved
    fk_refs: dict[str, int]               # {field_name: source_fk_id}, from field_map (regular dependencies)
    deferred_fk_refs: dict[str, int]      # {field_name: source_fk_id}, from deferred_field_map (patch-only)
    # Polymorphic FK fields: {field_name: {"type": dep_type_key, "id": source_fk_id}}.
    # The type is determined at extract time from the discriminator field on the source object.
    # Always patch-only (same as deferred_fk_refs) — no ordering guarantee.
    polymorphic_fk_refs: dict[str, dict[str, Any]]
    dropped_custom_fields: list[str]


def extract_fields(type_spec: TypeSpec, source_obj: dict[str, Any]) -> ExtractedFields:
    """
    Pure function of the source object: no id_map, no network, same result
    every time it's called on the same input. This is what actually gets
    persisted (as MigrationJobItem.payload_json / fk_refs_json /
    deferred_fk_json) as the plan-time snapshot.
    """
    static_fields: dict[str, Any] = {}
    fk_refs: dict[str, int] = {}
    deferred_fk_refs: dict[str, int] = {}
    polymorphic_fk_refs: dict[str, dict[str, Any]] = {}
    dropped_custom_fields: list[str] = []

    self_parent_field = type_spec.self_parent_field

    for key, value in source_obj.items():
        if key in _ALWAYS_STRIP:
            continue
        if key in _ALWAYS_DROP:
            if value:
                dropped_custom_fields.append(key)
            continue
        if key == self_parent_field:
            if value is None:
                static_fields[key] = None  # same as any optional FK, never a resolution problem
            else:
                fk_refs[key] = _unwrap_fk_value(value)
            continue
        if key == UNIVERSAL_TAG_FIELD and type_spec.key != UNIVERSAL_TAG_TYPE:
            # `tags` is a list of nested tag objects on read; NetBox's write API wants a list
            # of tag ids/slugs, not nested objects — without this, every migrated object with
            # any tag would send the raw nested dicts straight through as static_fields and
            # fail (or write garbage) on create. Resolved like any other FK list: through
            # fk_refs, against the always-pre-populated extras.tag id map (see planner.py,
            # which processes tags before every other type regardless of what's selected).
            fk_refs[key] = _unwrap_fk_value(value) if value else []
            continue
        if key in type_spec.deferred_field_map:
            if value is not None:
                deferred_fk_refs[key] = _unwrap_fk_value(value)
            continue
        if key in type_spec.polymorphic_field_map:
            if value is not None:
                poly_spec = type_spec.polymorphic_field_map[key]
                disc_value = source_obj.get(poly_spec.discriminator_field)
                dep_type_key = poly_spec.type_values.get(disc_value) if disc_value else None
                source_fk_id = _unwrap_fk_value(value)
                polymorphic_fk_refs[key] = {"type": dep_type_key, "id": source_fk_id}
            continue
        if key in type_spec.field_map:
            if value is None:
                static_fields[key] = None  # a genuinely absent optional FK — not a resolution problem
            else:
                fk_refs[key] = _unwrap_fk_value(value)
            continue
        static_fields[key] = _choice_value(value)

    return ExtractedFields(static_fields, fk_refs, deferred_fk_refs, polymorphic_fk_refs, dropped_custom_fields)


def _resolve_ref_value(source_value: Any, dep_type_key: str | None, *, id_map: IdMap) -> Any:
    if isinstance(source_value, list):
        resolved_values: list[Any] = []
        for item in source_value:
            target_value = id_map.get(dep_type_key, item) if dep_type_key is not None else None
            if target_value is None:
                return None
            resolved_values.append(target_value)
        return resolved_values
    if dep_type_key is None:
        return None
    return id_map.get(dep_type_key, source_value)


def resolve_fk_refs(
    type_spec: TypeSpec,
    fk_refs: dict[str, Any],
    *,
    id_map: IdMap,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Resolves a {field_name: source_fk_id} map (either `fk_refs` or
    `deferred_fk_refs` from extract_fields) against `id_map` as it stands
    right now. Returns (resolved, still_unresolved) — the caller decides
    what "still unresolved" means for its situation: at plan/execution time
    for regular fk_refs it indicates a genuinely skipped/ambiguous
    dependency (an error); for deferred_fk_refs during phase `primary` it's
    the expected, normal case that becomes a MigrationJobPatch.
    """
    fk_field_types = type_spec.all_field_map  # {field_name: dependency_type_key}
    resolved: dict[str, Any] = {}
    unresolved: dict[str, Any] = {}
    for field_name, source_fk_id in fk_refs.items():
        dep_type_key = fk_field_types.get(field_name)
        if dep_type_key is None and field_name == type_spec.self_parent_field:
            dep_type_key = type_spec.key
        target_fk_id = _resolve_ref_value(source_fk_id, dep_type_key, id_map=id_map)
        if target_fk_id is not None:
            resolved[field_name] = target_fk_id
        else:
            unresolved[field_name] = source_fk_id
    return resolved, unresolved


def resolve_polymorphic_fk_refs(
    polymorphic_fk_refs: dict[str, dict[str, Any]],
    *,
    id_map: IdMap,
) -> tuple[dict[str, Any], dict[str, str]]:
    """
    Resolves polymorphic FK refs (from ExtractedFields.polymorphic_fk_refs) against
    `id_map`. Each entry is {field_name: {"type": dep_type_key, "id": source_fk_id}}.

    Returns (resolved, errors) where:
    - resolved: {field_name: target_fk_id} for entries that resolved successfully.
    - errors: {field_name: reason_string} for entries that could not be resolved
      (unknown discriminator value, dependency type not in id_map, etc.).
    """
    resolved: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for field_name, ref in polymorphic_fk_refs.items():
        dep_type_key = ref.get("type")
        source_fk_id = ref.get("id")
        if dep_type_key is None:
            errors[field_name] = (
                f"assigned_object_type discriminator value is unknown or not mapped — "
                f"cannot determine which dependency type to resolve against"
            )
            continue
        target_fk_id = id_map.get(dep_type_key, source_fk_id)
        if target_fk_id is None:
            errors[field_name] = (
                f"dependency type {dep_type_key!r} (source id {source_fk_id}) was not resolved — "
                f"it may not have been selected for this migration, or was skipped/ambiguous"
            )
        else:
            resolved[field_name] = target_fk_id
    return resolved, errors


def build_preview_payload(
    type_spec: TypeSpec,
    extracted: ExtractedFields,
    *,
    id_map: IdMap,
) -> dict[str, Any]:
    """Best-effort payload for the dry-run report / plan preview — merges static fields with whatever resolves right now."""
    resolved_fk, _ = resolve_fk_refs(type_spec, extracted.fk_refs, id_map=id_map)
    return {**extracted.static_fields, **resolved_fk}
