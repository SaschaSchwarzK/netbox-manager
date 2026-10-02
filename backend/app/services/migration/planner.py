"""
build_plan(): reads the source (and queries the target read-only), resolves
every selected object's match against the target, and writes the complete
MigrationJobItem / MigrationJobPatch rows for a job — all without writing
anything to the target. This is BOTH the dry-run preview and the plan
`execute_plan()` (executor.py) later walks; there's exactly one code path
that decides "what should happen to this object", so a dry run's prediction
and a real run's outcome can't drift apart from having separate logic.

Ordering within a resolved type: registry.resolve_selection() already gives
type-level topological order. Within a single type, objects are ordered by
their own dependency depth where that matters (the self-referential
hierarchies: region/site group/location parent chains — see
TypeSpec.self_parent_field) so a parent is always created before its child
even though they're the same registry type.

Placeholder ids: build_plan never writes to the target, so an object that
will be CREATED has no real target id yet. To let anything depending on it
still be matched/previewed sensibly (rather than every downstream object
falling back to "can't tell, defer"), a negative placeholder id is recorded
in the id_map for every "create" action. Negative ids can never collide
with a real NetBox id, so resolve_fk_refs() treating "id_map has a value"
as resolved (regardless of sign) is always safe, and matching against a
placeholder correctly finds nothing on the real target (which is exactly
right — a device whose site doesn't exist yet on the target obviously can't
match an existing device scoped to that site either). See executor.py for
how these placeholders get replaced by real ids as execution proceeds.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.services.migration.client import RateLimitedClient
from app.services.migration.matcher import (
    IdMap,
    MappingOverride,
    MatchOutcome,
    TargetLookup,
    match_object,
)
from app.services.migration.registry import UNIVERSAL_TAG_TYPE, Registry, TypeSpec, resolve_selection
from app.services.migration.sanitize import build_preview_payload, extract_fields, resolve_fk_refs, ExtractedFields

DEFAULT_CONFLICT_POLICY = "skip"  # skip / update / update_empty_only — see ConflictPolicy


@dataclass
class PlannedItem:
    object_type: str
    source_id: int
    source_natural_key: str
    planned_action: str  # create/update/map/skip/ambiguous
    match_detail: str | None
    static_fields: dict[str, Any]
    fk_refs: dict[str, int]           # {field_name: source_fk_id}, regular dependencies — always resolvable by execution time
    deferred_fk: dict[str, int]       # {field_name: source_fk_id}, always via phase `patch`
    # Polymorphic FK refs: {field_name: {"type": dep_type_key, "id": source_fk_id}}.
    # Always patch-only. Stored verbatim from extract_fields so the executor can
    # call resolve_polymorphic_fk_refs with the real id_map at patch time.
    polymorphic_fk: dict[str, dict[str, Any]]
    preview_payload: dict[str, Any]   # best-effort payload for the report; NEVER sent to any API
    dropped_custom_fields: list[str]
    target_id: int | None            # a REAL target id (existing object); None for "create" (even though a placeholder exists in id_map during planning)
    execution_status: str            # "pending" for create/update; "done" for map/skip; "error" for ambiguous
    error_detail: str | None = None


@dataclass
class PlannedPatch:
    object_type: str
    source_id: int
    patch_fields: dict[str, int]  # {field_name: source_fk_id}, resolved later at execution time
    polymorphic_patch_fields: dict[str, dict[str, Any]]  # {field_name: {"type": dep_type_key, "id": source_fk_id}}


@dataclass
class PlanResult:
    resolved_types: list[str]
    items: list[PlannedItem] = field(default_factory=list)
    patches: list[PlannedPatch] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    totals: dict[str, dict[str, int]] = field(default_factory=dict)  # {type: {action: count}}

    def has_blocking_errors(self) -> bool:
        return any(item.planned_action == "ambiguous" for item in self.items)


class LiveTargetLookup:
    """Real TargetLookup backed by the target RateLimitedClient — one GET per candidate strategy."""

    def __init__(self, target_client: RateLimitedClient):
        self.target_client = target_client
        self._cache: dict[tuple[str, tuple], dict | None] = {}

    def find(self, type_spec: TypeSpec, scalar_filters: dict[str, Any]) -> dict[str, Any] | None:
        cache_key = (type_spec.key, tuple(sorted(scalar_filters.items())))
        if cache_key in self._cache:
            return self._cache[cache_key]
        endpoint = resolve_endpoint(self.target_client.nb, type_spec.endpoint)
        result = self.target_client.get(endpoint, **scalar_filters)
        found = dict(result) if result is not None else None
        self._cache[cache_key] = found
        return found

    def clear_cache(self) -> None:
        self._cache.clear()


def resolve_endpoint(nb: Any, dotted: str) -> Any:
    app_name, endpoint_name = dotted.split(".")
    return getattr(getattr(nb, app_name), endpoint_name)


def _natural_key_label(type_spec: TypeSpec, source_obj: dict[str, Any]) -> str:
    for candidate_field in ("name", "slug", "model", "address", "prefix", "vid", "asn"):
        if source_obj.get(candidate_field):
            return str(source_obj[candidate_field])
    return f"id={source_obj.get('id')}"


def _validate_preview_payload(
    target_client: RateLimitedClient,
    type_spec: TypeSpec,
    payload: dict[str, Any],
) -> list[str]:
    """Use NetBox OPTIONS metadata for early field warnings without blocking a plan."""
    options_method = getattr(target_client, "options", None)
    if options_method is None:
        return []
    endpoint = resolve_endpoint(target_client.nb, type_spec.endpoint)
    try:
        metadata = options_method(endpoint)
    except Exception as exc:  # noqa: BLE001 - validation metadata is advisory
        return [f"{type_spec.key}: field validation metadata unavailable: {exc}"]
    post_fields = ((metadata.get("actions") or {}).get("POST") or {})
    if not isinstance(post_fields, dict) or not post_fields:
        return [f"{type_spec.key}: NetBox did not provide POST field metadata; field validation was not performed."]
    warnings: list[str] = []
    for field_name, field_spec in post_fields.items():
        if not isinstance(field_spec, dict):
            continue
        value = payload.get(field_name)
        if field_spec.get("required") and value in (None, "", [], {}):
            warnings.append(f"{type_spec.key}: required field {field_name!r} is missing from the planned payload.")
        choices = field_spec.get("choices")
        if value is not None and choices:
            allowed = {choice.get("value") for choice in choices if isinstance(choice, dict)}
            if allowed and value not in allowed:
                warnings.append(f"{type_spec.key}: field {field_name!r} value {value!r} is not in the target choices.")
        max_length = field_spec.get("max_length")
        if isinstance(value, str) and isinstance(max_length, int) and len(value) > max_length:
            warnings.append(f"{type_spec.key}: field {field_name!r} exceeds target max_length={max_length}.")
        if value is not None and field_spec.get("read_only"):
            warnings.append(f"{type_spec.key}: field {field_name!r} is read-only on the target.")
    for field_name in payload:
        if field_name not in post_fields:
            warnings.append(f"{type_spec.key}: field {field_name!r} is not advertised by the target POST schema.")
    return warnings


def _depth(type_spec: TypeSpec, source_obj: dict[str, Any], by_id: dict[int, dict[str, Any]]) -> int:
    """Depth in a self-referential parent chain (region/site group/location); 0 for everything else."""
    if not type_spec.self_parent_field:
        return 0
    depth = 0
    current = source_obj
    seen: set[int] = set()
    while True:
        parent_ref = current.get(type_spec.self_parent_field)
        if parent_ref is None:
            return depth
        parent_id = parent_ref["id"] if isinstance(parent_ref, dict) else parent_ref
        if parent_id in seen or parent_id not in by_id:
            return depth  # malformed/external parent — stop rather than loop forever
        seen.add(parent_id)
        current = by_id[parent_id]
        depth += 1


class _PlaceholderIds:
    """Negative, strictly-decreasing ids handed out for pending "create" actions during planning. Never persisted as a real target_id."""

    def __init__(self) -> None:
        self._next = -1

    def next(self) -> int:
        value = self._next
        self._next -= 1
        return value


def build_plan(
    *,
    registry: Registry,
    source_client: RateLimitedClient,
    target_client: RateLimitedClient,
    selected_types: set[str],
    tenant_filter: list[str],
    mapping_overrides: dict[tuple[str, int], MappingOverride],
    conflict_policy: dict[str, str] | None = None,
    include_untenanted: bool = False,
) -> PlanResult:
    if not source_client.read_only:
        raise ValueError("build_plan requires a read-only source client")

    resolved_types = resolve_selection(selected_types, registry)
    if UNIVERSAL_TAG_TYPE in registry and UNIVERSAL_TAG_TYPE not in resolved_types:
        # `tags` is deliberately not a declared dependency of anything (see registry.py's
        # UNIVERSAL_TAG_FIELD) — it's universal rather than type-specific, so it can't be
        # discovered by the normal referenced-only dependency closure. Every object that
        # carries any tags needs the full tag id map available before it's processed, so
        # extras.tag is unconditionally processed first, exactly as if it were a dependency
        # of every selected type (which, in effect, it is).
        resolved_types = [UNIVERSAL_TAG_TYPE, *resolved_types]
    id_map = IdMap()
    placeholders = _PlaceholderIds()
    target_lookup = LiveTargetLookup(target_client)
    result = PlanResult(resolved_types=resolved_types)
    conflict_policy = conflict_policy or {}
    # Every type's source object ids, as they're processed — used to scope
    # a dependent, non-tenant-filterable child type (interfaces, VM disks,
    # console ports, ...) to just the parents that are actually in the plan,
    # since those endpoints have no `tenant` query param of their own.
    parent_source_ids: dict[str, set[int]] = {}

    for type_key in resolved_types:
        type_spec = registry[type_key]
        if type_spec.out_of_scope:
            continue
        source_objects = _fetch_source_objects(
            source_client, type_spec, registry=registry,
            tenant_filter=tenant_filter, include_untenanted=include_untenanted,
            parent_source_ids=parent_source_ids,
        )
        parent_source_ids[type_key] = {obj["id"] for obj in source_objects}
        by_id = {obj["id"]: obj for obj in source_objects}
        ordered_objects = sorted(source_objects, key=lambda obj: _depth(type_spec, obj, by_id))

        policy = conflict_policy.get(type_key, conflict_policy.get("default", DEFAULT_CONFLICT_POLICY))
        type_totals: dict[str, int] = {}
        for source_obj in ordered_objects:
            item = _plan_one_object(
                type_spec, source_obj, registry=registry, id_map=id_map,
                target_lookup=target_lookup, mapping_overrides=mapping_overrides,
                conflict_policy=policy, warnings=result.warnings,
            )
            result.items.append(item)
            type_totals[item.planned_action] = type_totals.get(item.planned_action, 0) + 1
            if item.planned_action in ("create", "update"):
                result.warnings.extend(_validate_preview_payload(target_client, type_spec, item.preview_payload))

            if item.planned_action in ("map", "update"):
                id_map.put(type_key, item.source_id, item.target_id)
            elif item.planned_action == "create":
                id_map.put(type_key, item.source_id, placeholders.next())
            # "skip" / "ambiguous": deliberately no id_map entry — nothing exists to reference.

            if item.deferred_fk or item.polymorphic_fk:
                result.patches.append(PlannedPatch(
                    object_type=type_key, source_id=item.source_id,
                    patch_fields=dict(item.deferred_fk),
                    polymorphic_patch_fields=dict(item.polymorphic_fk),
                ))
        result.totals[type_key] = type_totals

    return result


def _plan_one_object(
    type_spec: TypeSpec,
    source_obj: dict[str, Any],
    *,
    registry: Registry,
    id_map: IdMap,
    target_lookup: TargetLookup,
    mapping_overrides: dict[tuple[str, int], MappingOverride],
    conflict_policy: str,
    warnings: list[str],
) -> PlannedItem:
    source_id = source_obj["id"]
    natural_key = _natural_key_label(type_spec, source_obj)
    override = mapping_overrides.get((type_spec.key, source_id))

    match = match_object(
        type_spec, source_obj, registry=registry, id_map=id_map, target_lookup=target_lookup, override=override,
    )

    if match.outcome in (MatchOutcome.MAPPED_EXPLICIT, MatchOutcome.MATCHED):
        # "Mapped objects are never changed" — this holds regardless of conflict_policy for an
        # EXPLICIT mapping override (the user pointed at this object on purpose). For an
        # auto-match (natural key), conflict_policy decides whether it's left alone ("map") or
        # updated from the source ("update": all fields; "update_empty_only": only fields that
        # are currently empty on the target, where empty means None/""/[]/{}; 0 and False are
        # meaningful values and are never overwritten).
        is_explicit = match.outcome == MatchOutcome.MAPPED_EXPLICIT
        if is_explicit or conflict_policy == "skip":
            return PlannedItem(
                object_type=type_spec.key, source_id=source_id, source_natural_key=natural_key,
                planned_action="map", match_detail=match.detail,
                static_fields={}, fk_refs={}, deferred_fk={}, polymorphic_fk={}, preview_payload={},
                dropped_custom_fields=[], target_id=match.target_id, execution_status="done",
            )
        extracted = extract_fields(type_spec, source_obj)
        if extracted.dropped_custom_fields:
            warnings.append(f"{type_spec.key} id={source_id} ({natural_key}): dropped custom fields {extracted.dropped_custom_fields}")

        if conflict_policy == "update_empty_only":
            # Filter to only fields that are currently empty on the matched target object.
            # The target object is already in the LiveTargetLookup cache from the match;
            # re-fetch it by id to get the full field set (the match may have returned a
            # partial projection depending on the strategy used).
            target_obj = target_lookup.find(type_spec, {"id": match.target_id}) or {}
            _EMPTY = (None, "", [], {})
            filtered_static = {k: v for k, v in extracted.static_fields.items() if target_obj.get(k) in _EMPTY}
            filtered_fk_refs = {k: v for k, v in extracted.fk_refs.items() if target_obj.get(k) in _EMPTY}
            if not filtered_static and not filtered_fk_refs:
                # Nothing to update — treat as a plain map (no-op).
                return PlannedItem(
                    object_type=type_spec.key, source_id=source_id, source_natural_key=natural_key,
                    planned_action="map", match_detail=match.detail,
                    static_fields={}, fk_refs={}, deferred_fk={}, polymorphic_fk={}, preview_payload={},
                    dropped_custom_fields=extracted.dropped_custom_fields,
                    target_id=match.target_id, execution_status="done",
                )
            preview_payload = build_preview_payload(
                type_spec,
                ExtractedFields(filtered_static, filtered_fk_refs, {}, {}, extracted.dropped_custom_fields),
                id_map=id_map,
            )
            return PlannedItem(
                object_type=type_spec.key, source_id=source_id, source_natural_key=natural_key,
                planned_action="update", match_detail=match.detail,
                static_fields=filtered_static, fk_refs=filtered_fk_refs,
                deferred_fk={}, polymorphic_fk={}, preview_payload=preview_payload,
                dropped_custom_fields=extracted.dropped_custom_fields,
                target_id=match.target_id, execution_status="pending",
            )

        preview_payload = build_preview_payload(type_spec, extracted, id_map=id_map)
        return PlannedItem(
            object_type=type_spec.key, source_id=source_id, source_natural_key=natural_key,
            planned_action="update", match_detail=match.detail,
            static_fields=extracted.static_fields, fk_refs=extracted.fk_refs,
            deferred_fk=extracted.deferred_fk_refs, polymorphic_fk=extracted.polymorphic_fk_refs,
            preview_payload=preview_payload,
            dropped_custom_fields=extracted.dropped_custom_fields,
            target_id=match.target_id, execution_status="pending",
        )
    if match.outcome == MatchOutcome.SKIPPED_EXPLICIT:
        return PlannedItem(
            object_type=type_spec.key, source_id=source_id, source_natural_key=natural_key,
            planned_action="skip", match_detail=match.detail,
            static_fields={}, fk_refs={}, deferred_fk={}, polymorphic_fk={}, preview_payload={},
            dropped_custom_fields=[], target_id=None, execution_status="done",
        )
    if match.outcome == MatchOutcome.AMBIGUOUS:
        return PlannedItem(
            object_type=type_spec.key, source_id=source_id, source_natural_key=natural_key,
            planned_action="ambiguous", match_detail=match.detail,
            static_fields={}, fk_refs={}, deferred_fk={}, polymorphic_fk={}, preview_payload={},
            dropped_custom_fields=[], target_id=None, execution_status="error",
            error_detail=f"Ambiguous match, needs manual mapping: {match.detail}",
        )
    if match.outcome == MatchOutcome.UNRESOLVABLE:
        # Not fatal on its own: this usually means a dependency of this object was itself
        # skipped. The object is still created, just without that particular field — same
        # as any other deferred FK — but we surface it clearly since it often indicates a
        # skip decision the user didn't intend to cascade this far.
        warnings.append(
            f"{type_spec.key} id={source_id} ({natural_key}): a referenced dependency isn't "
            f"resolved (skipped, or itself ambiguous) — proceeding without confirming a match."
        )

    # NO_MATCH or UNRESOLVABLE (best-effort): create new.
    extracted = extract_fields(type_spec, source_obj)
    preview_payload = build_preview_payload(type_spec, extracted, id_map=id_map)
    if extracted.dropped_custom_fields:
        warnings.append(f"{type_spec.key} id={source_id} ({natural_key}): dropped custom fields {extracted.dropped_custom_fields}")
    return PlannedItem(
        object_type=type_spec.key, source_id=source_id, source_natural_key=natural_key,
        planned_action="create", match_detail=match.detail,
        static_fields=extracted.static_fields, fk_refs=extracted.fk_refs,
        deferred_fk=extracted.deferred_fk_refs, polymorphic_fk=extracted.polymorphic_fk_refs,
        preview_payload=preview_payload,
        dropped_custom_fields=extracted.dropped_custom_fields,
        target_id=None, execution_status="pending",
    )


# Types whose child objects should always be scoped to "whichever parents ended
# up in the plan" rather than fetched independently — these endpoints have no
# `tenant` query parameter of their own, so this is the only way a component
# type (interfaces, console ports, VM disks, ...) respects the tenant filter
# at all. Applied regardless of whether a tenant filter is active: a component
# type's dependency on its parent (dcim.device / virtualization.virtualmachine)
# is required, so the parent is always resolved first and always known.
_PARENT_SCOPE_TYPES = {"dcim.device", "virtualization.virtualmachine"}


def _fetch_source_objects(
    source_client: RateLimitedClient,
    type_spec: TypeSpec,
    *,
    registry: Registry,
    tenant_filter: list[str],
    include_untenanted: bool,
    parent_source_ids: dict[str, set[int]],
) -> list[dict[str, Any]]:
    endpoint = resolve_endpoint(source_client.nb, type_spec.endpoint)

    scope_parent_type = next((dep for dep in type_spec.dependencies if dep in _PARENT_SCOPE_TYPES), None)
    parent_scope_filters: dict[str, Any] = {}
    if scope_parent_type is not None and scope_parent_type in parent_source_ids:
        parent_ids = sorted(parent_source_ids[scope_parent_type])
        if not parent_ids:
            return []  # no parents in the plan at all — nothing of this child type can be in scope either
        field_name = registry.field_for(type_spec.key, scope_parent_type)
        parent_scope_filters[f"{field_name}_id"] = parent_ids

    if not (tenant_filter and type_spec.tenant_filterable):
        return list(source_client.paginated(endpoint, **parent_scope_filters))

    tenant_scoped = list(source_client.paginated(endpoint, tenant=tenant_filter, **parent_scope_filters))
    if not include_untenanted:
        return tenant_scoped

    # NetBox's convention for "this FK is null" filters is `<field>_id=null`.
    untenanted = list(source_client.paginated(endpoint, tenant_id="null", **parent_scope_filters))
    by_id = {obj["id"]: obj for obj in (*tenant_scoped, *untenanted)}  # de-dup, in case of any overlap
    return list(by_id.values())


def serialize_plan_item(item: PlannedItem) -> dict[str, Any]:
    return {
        "object_type": item.object_type,
        "source_id": item.source_id,
        "source_natural_key": item.source_natural_key,
        "planned_action": item.planned_action,
        "match_detail": item.match_detail,
        "payload_json": json.dumps(item.static_fields),
        "fk_refs_json": json.dumps(item.fk_refs),
        "deferred_fk_json": json.dumps(item.deferred_fk),
        "dropped_custom_fields_json": json.dumps(item.dropped_custom_fields),
        "target_id": item.target_id,
        "execution_status": item.execution_status,
        "error_detail": item.error_detail,
    }


def persist_plan(db: Any, job_id: str, plan: PlanResult) -> None:
    """
    Writes a freshly-built PlanResult into MigrationJobItem/MigrationJobPatch
    rows, replacing any previous plan for this job (re-planning — e.g. after
    the user edits the mapping table — always starts from a clean slate;
    there is nothing to preserve from a superseded plan, unlike execution
    state, which lives entirely in these same rows once a job is running).
    `order_index` is simply each list's position — plan.items and
    plan.patches are already in the order build_plan produced them in
    (topological, depth-sorted within a type), which is exactly the order
    execution must happen in.
    """
    # Local import to avoid a hard dependency of this module on the ORM at import time
    # (keeps planner.py importable/testable with plain dicts and no DB, as the tests do).
    from app.models import MigrationJobItem, MigrationJobPatch

    db.query(MigrationJobItem).filter(MigrationJobItem.job_id == job_id).delete()
    db.query(MigrationJobPatch).filter(MigrationJobPatch.job_id == job_id).delete()

    for order_index, item in enumerate(plan.items):
        serialized = serialize_plan_item(item)
        db.add(MigrationJobItem(job_id=job_id, order_index=order_index, **serialized))

    for order_index, patch in enumerate(plan.patches):
        db.add(MigrationJobPatch(
            job_id=job_id, order_index=order_index,
            object_type=patch.object_type, source_id=patch.source_id,
            patch_fields_json=json.dumps(patch.patch_fields),
            polymorphic_patch_fields_json=json.dumps(patch.polymorphic_patch_fields),
        ))

    db.commit()
