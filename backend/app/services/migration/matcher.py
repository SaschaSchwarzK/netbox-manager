"""
Matches a source object against the target instance using the registry's
per-type natural-key strategies, so migrating twice — or resuming a crashed
job — never creates duplicates.

Precedence, per object: explicit mapping override > natural-key auto-match >
create new. "Skip" is also an explicit mapping outcome (see MappingOverride).

FK fields inside a match strategy are resolved through the IdMap (built from
already-completed, earlier-phase items — dependencies are always ordered
before dependents, so by the time a type is matched, everything it can
reference via a required or optional dependency has already been resolved).
If an FK field's source value can't be resolved yet (dependency was skipped,
or is itself ambiguous), the strategy is skipped and the next one is tried;
if no strategy can be evaluated at all, the object is reported as
"unresolvable" rather than silently guessed at.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from app.services.migration.registry import MatchStrategy, Registry, TypeSpec


class IdMap:
    """
    source object type + source id -> target id, accumulated as items are
    resolved (mapped or created). Backed by a plain dict at the planner/
    executor layer; MigrationJobItem rows are the durable form of this same
    data (see services/migration/executor.py), so rebuilding an IdMap from
    completed rows is how a resumed job recovers its state.
    """

    def __init__(self) -> None:
        self._map: dict[tuple[str, int], int] = {}

    def put(self, type_key: str, source_id: int, target_id: int) -> None:
        self._map[(type_key, source_id)] = target_id

    def get(self, type_key: str, source_id: int) -> int | None:
        return self._map.get((type_key, source_id))

    def __len__(self) -> int:
        return len(self._map)


class AmbiguousTargetLookup(RuntimeError):
    """A supposedly unique natural-key query matched multiple target objects."""


class MappingAction(str, Enum):
    MAP = "map"
    CREATE = "create"
    SKIP = "skip"


@dataclass(frozen=True)
class MappingOverride:
    """An explicit, user-reviewed decision for one source object, from the mapping file/table."""
    action: MappingAction
    target_id: int | None = None  # required when action == MAP


class TargetLookup(Protocol):
    """
    What the matcher needs from the target instance: "does an object with
    these field values already exist?" Implemented against the real
    RateLimitedClient in planner.py; faked in tests so matcher logic can be
    tested without any HTTP.
    """

    def find(self, type_spec: TypeSpec, scalar_filters: dict[str, Any]) -> dict[str, Any] | None:
        """Returns the target object (as a dict with at least `id`) matching the given scalar field filters, or None."""


class MatchOutcome(str, Enum):
    MAPPED_EXPLICIT = "mapped_explicit"      # explicit mapping override, action=map
    SKIPPED_EXPLICIT = "skipped_explicit"    # explicit mapping override, action=skip
    MATCHED = "matched"                      # auto-matched by natural key
    AMBIGUOUS = "ambiguous"                  # two strategies matched different targets
    UNRESOLVABLE = "unresolvable"            # a required FK dependency hasn't been resolved yet
    NO_MATCH = "no_match"                    # no existing target object found; will create


@dataclass(frozen=True)
class MatchResult:
    outcome: MatchOutcome
    target_id: int | None = None
    target_natural_key: str | None = None
    detail: str | None = None
    matched_strategy_fields: tuple[str, ...] | None = None


def match_object(
    type_spec: TypeSpec,
    source_obj: dict[str, Any],
    *,
    registry: Registry,
    id_map: IdMap,
    target_lookup: TargetLookup,
    override: MappingOverride | None,
) -> MatchResult:
    if override is not None:
        if override.action == MappingAction.SKIP:
            return MatchResult(MatchOutcome.SKIPPED_EXPLICIT, detail="skipped via explicit mapping")
        if override.action == MappingAction.MAP:
            # Manual mappings contain only an id. Resolve it once here so review/report
            # surfaces are just as useful as auto-matches; LiveTargetLookup caches this
            # id lookup, so later conflict-policy work does not add another API request.
            found = target_lookup.find(type_spec, {"id": override.target_id}) if override.target_id is not None else None
            return MatchResult(
                MatchOutcome.MAPPED_EXPLICIT,
                target_id=override.target_id,
                target_natural_key=natural_key_label(found, type_spec=type_spec) if found else None,
            )
        # action == CREATE falls through to the normal auto-match/create flow below,
        # since "create" is also the default outcome of finding no match — an
        # explicit "create" override just means "don't even try to auto-match".
        return _attempt_create_only(type_spec)

    # Every strategy that CAN be evaluated is checked (not just the first),
    # because ambiguity — e.g. a device type whose part number matches one
    # target object but whose model matches a different one — can only be
    # detected by comparing what different strategies find, never by
    # stopping at the first hit.
    hits: list[tuple[MatchStrategy, int, str | None]] = []
    ambiguous_details: list[str] = []
    tried_any_strategy = False
    for strategy in type_spec.match_strategies:
        resolved = _resolve_strategy(type_spec, strategy, source_obj, registry=registry, id_map=id_map)
        if resolved is None:
            continue  # this strategy's fields aren't present, or an FK isn't resolved yet — try the next one
        tried_any_strategy = True
        try:
            found = target_lookup.find(type_spec, resolved)
        except AmbiguousTargetLookup as exc:
            # A broad strategy can legitimately be ambiguous while a later,
            # more specific strategy identifies one object (VLANs are the
            # common case: vid+no-group may collide, while vid+site does not).
            # Keep evaluating instead of forcing manual mapping immediately.
            disambiguate = getattr(target_lookup, "disambiguate", None)
            found = disambiguate(type_spec, source_obj, resolved) if disambiguate else None
            if found is None:
                ambiguous_details.append(str(exc))
                continue
        if found is not None:
            hits.append((strategy, found["id"], natural_key_label(found, type_spec=type_spec)))

    if not tried_any_strategy and type_spec.match_strategies:
        return MatchResult(
            MatchOutcome.UNRESOLVABLE,
            detail="none of this type's match strategies could be evaluated "
                   "(missing fields, or a referenced dependency hasn't been resolved yet)",
        )
    distinct_ids = {target_id for _, target_id, _ in hits}
    if len(distinct_ids) > 1:
        detail = "; ".join(
            f"{'+'.join(s.fields)} -> {label or f'#{tid}'} (#{tid})" if label else
            f"{'+'.join(s.fields)} -> #{tid}"
            for s, tid, label in hits
        )
        return MatchResult(MatchOutcome.AMBIGUOUS, detail=f"strategies disagree: {detail}")
    if hits:
        best_strategy, target_id, target_label = hits[0]  # strategies are declared in priority order
        return MatchResult(
            MatchOutcome.MATCHED, target_id=target_id, target_natural_key=target_label,
            matched_strategy_fields=best_strategy.fields,
        )
    if ambiguous_details:
        return MatchResult(MatchOutcome.AMBIGUOUS, detail="; ".join(ambiguous_details))
    return MatchResult(MatchOutcome.NO_MATCH)


def _attempt_create_only(type_spec: TypeSpec) -> MatchResult:
    return MatchResult(MatchOutcome.NO_MATCH, detail="explicit override: create new (auto-match skipped)")


def natural_key_label(obj: dict[str, Any] | None, *, type_spec: TypeSpec | None = None) -> str | None:
    """Return a compact label, including the owning device/VM for component objects."""
    if not obj:
        return None
    own_label: str | None = None
    for candidate_field in ("name", "slug", "model", "address", "prefix", "vid", "asn"):
        if obj.get(candidate_field) not in (None, ""):
            own_label = str(obj[candidate_field])
            break
    # Some NetBox endpoints expose only their computed display string in list
    # responses. It is less stable than a true natural key, but still preferable
    # to showing an unexplained numeric id when the canonical fields are absent.
    if own_label is None and obj.get("display") not in (None, ""):
        own_label = str(obj["display"])

    if type_spec is None:
        return own_label
    context_parts: list[str] = []
    for field_name in ("device", "virtual_machine"):
        parent_label = natural_key_label(obj.get(field_name)) if isinstance(obj.get(field_name), dict) else None
        if parent_label:
            context_parts.append(parent_label)
    if type_spec.key == "dcim.module":
        for field_name in ("module_bay", "module_type"):
            part = natural_key_label(obj.get(field_name)) if isinstance(obj.get(field_name), dict) else None
            if part and part not in context_parts:
                context_parts.append(part)
    if own_label and own_label not in context_parts:
        context_parts.append(own_label)
    return " · ".join(context_parts) or None


def _resolve_strategy(
    type_spec: TypeSpec,
    strategy: MatchStrategy,
    source_obj: dict[str, Any],
    *,
    registry: Registry,
    id_map: IdMap,
) -> dict[str, Any] | None:
    """
    Builds the concrete {field: value} filter to look up on the target for
    one strategy, or None if this strategy can't be evaluated for this
    object (a field is absent/null on the source, or an fk_field's
    dependency hasn't been resolved in the id map yet).
    """
    resolved: dict[str, Any] = {}
    for field_name in strategy.fields:
        if field_name not in source_obj:
            return None
        raw_value = source_obj[field_name]
        if field_name in strategy.fk_fields:
            # A nullable FK is still part of a valid natural key. NetBox
            # expresses an explicit null FK filter as `<field>_id=null`.
            # This is distinct from a non-null reference missing from id_map,
            # which remains genuinely unresolvable.
            if raw_value is None:
                resolved[field_name if field_name.endswith("_id") else f"{field_name}_id"] = "null"
                continue
            list_spec = type_spec.polymorphic_list_field_map.get(field_name)
            if list_spec:
                resolved_entries = []
                for entry in raw_value:
                    discriminator = entry.get(list_spec.discriminator_field)
                    dep_type_key = list_spec.type_values.get(discriminator)
                    target_id = id_map.get(dep_type_key, entry.get(list_spec.object_id_field)) if dep_type_key else None
                    if target_id is None:
                        return None
                    resolved_entries.append({
                        list_spec.discriminator_field: discriminator,
                        list_spec.object_id_field: target_id,
                    })
                resolved[field_name] = resolved_entries
                continue
            poly_spec = type_spec.polymorphic_field_map.get(field_name)
            dep_type_key = (
                poly_spec.type_values.get(source_obj.get(poly_spec.discriminator_field))
                if poly_spec else _dependency_type_for_field(type_spec, field_name)
            )
            if dep_type_key is None:
                return None
            source_fk_id = raw_value["id"] if isinstance(raw_value, dict) else raw_value
            target_fk_id = id_map.get(dep_type_key, source_fk_id)
            if target_fk_id is None:
                return None  # dependency not resolved yet (e.g. it was skipped, or is itself ambiguous)
            # NetBox's REST filtering convention for a numeric FK lookup is
            # `<field>_id=<id>` — plain `<field>=<value>` instead filters by
            # that related object's slug/name, which target_fk_id is not.
            resolved[field_name if field_name.endswith("_id") else f"{field_name}_id"] = target_fk_id
        else:
            # Empty strings are NetBox's normal representation for an unset
            # optional identifier (notably DeviceType.part_number).  They do
            # not identify an object and must not turn a strategy such as
            # manufacturer+part_number into a broad "all blank part numbers"
            # match.  Fall through to a later, meaningful strategy instead.
            if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
                return None
            resolved[field_name] = raw_value
    return resolved


def _dependency_type_for_field(type_spec: TypeSpec, field_name: str) -> str | None:
    return type_spec.all_field_map.get(field_name)
