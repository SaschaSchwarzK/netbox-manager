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
from app.services.migration.registry import TypeSpec

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


@dataclass(frozen=True)
class ExtractedFields:
    static_fields: dict[str, Any]         # plain, non-FK fields — final as-is, never re-resolved
    fk_refs: dict[str, int]               # {field_name: source_fk_id}, from field_map (regular dependencies)
    deferred_fk_refs: dict[str, int]      # {field_name: source_fk_id}, from deferred_field_map (patch-only)
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
    dropped_custom_fields: list[str] = []

    for key, value in source_obj.items():
        if key in _ALWAYS_STRIP:
            continue
        if key in _ALWAYS_DROP:
            if value:
                dropped_custom_fields.append(key)
            continue
        if key in type_spec.deferred_field_map:
            if value is not None:
                deferred_fk_refs[key] = value["id"] if isinstance(value, dict) else value
            continue
        if key in type_spec.field_map:
            if value is None:
                static_fields[key] = None  # a genuinely absent optional FK — not a resolution problem
            else:
                fk_refs[key] = value["id"] if isinstance(value, dict) else value
            continue
        static_fields[key] = _choice_value(value)

    return ExtractedFields(static_fields, fk_refs, deferred_fk_refs, dropped_custom_fields)


def resolve_fk_refs(
    type_spec: TypeSpec,
    fk_refs: dict[str, int],
    *,
    id_map: IdMap,
) -> tuple[dict[str, Any], dict[str, int]]:
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
    unresolved: dict[str, int] = {}
    for field_name, source_fk_id in fk_refs.items():
        dep_type_key = fk_field_types.get(field_name)
        target_fk_id = id_map.get(dep_type_key, source_fk_id) if dep_type_key else None
        if target_fk_id is not None:
            resolved[field_name] = target_fk_id
        else:
            unresolved[field_name] = source_fk_id
    return resolved, unresolved


def build_preview_payload(
    type_spec: TypeSpec,
    extracted: ExtractedFields,
    *,
    id_map: IdMap,
) -> dict[str, Any]:
    """Best-effort payload for the dry-run report / plan preview — merges static fields with whatever resolves right now."""
    resolved_fk, _ = resolve_fk_refs(type_spec, extracted.fk_refs, id_map=id_map)
    return {**extracted.static_fields, **resolved_fk}
