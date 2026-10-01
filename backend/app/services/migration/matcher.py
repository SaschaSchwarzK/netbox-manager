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
            return MatchResult(MatchOutcome.MAPPED_EXPLICIT, target_id=override.target_id)
        # action == CREATE falls through to the normal auto-match/create flow below,
        # since "create" is also the default outcome of finding no match — an
        # explicit "create" override just means "don't even try to auto-match".
        return _attempt_create_only(type_spec)

    # Every strategy that CAN be evaluated is checked (not just the first),
    # because ambiguity — e.g. a device type whose part number matches one
    # target object but whose model matches a different one — can only be
    # detected by comparing what different strategies find, never by
    # stopping at the first hit.
    hits: list[tuple[MatchStrategy, int]] = []
    tried_any_strategy = False
    for strategy in type_spec.match_strategies:
        resolved = _resolve_strategy(type_spec, strategy, source_obj, registry=registry, id_map=id_map)
        if resolved is None:
            continue  # this strategy's fields aren't present, or an FK isn't resolved yet — try the next one
        tried_any_strategy = True
        found = target_lookup.find(type_spec, resolved)
        if found is not None:
            hits.append((strategy, found["id"]))

    if not tried_any_strategy and type_spec.match_strategies:
        return MatchResult(
            MatchOutcome.UNRESOLVABLE,
            detail="none of this type's match strategies could be evaluated "
                   "(missing fields, or a referenced dependency hasn't been resolved yet)",
        )
    distinct_ids = {target_id for _, target_id in hits}
    if len(distinct_ids) > 1:
        detail = "; ".join(f"{'+'.join(s.fields)} -> target id {tid}" for s, tid in hits)
        return MatchResult(MatchOutcome.AMBIGUOUS, detail=f"strategies disagree: {detail}")
    if hits:
        best_strategy, target_id = hits[0]  # strategies are declared in priority order
        return MatchResult(MatchOutcome.MATCHED, target_id=target_id, matched_strategy_fields=best_strategy.fields)
    return MatchResult(MatchOutcome.NO_MATCH)


def _attempt_create_only(type_spec: TypeSpec) -> MatchResult:
    return MatchResult(MatchOutcome.NO_MATCH, detail="explicit override: create new (auto-match skipped)")


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
        raw_value = source_obj.get(field_name)
        if raw_value is None:
            return None
        if field_name in strategy.fk_fields:
            dep_type_key = _dependency_type_for_field(type_spec, field_name)
            if dep_type_key is None:
                return None
            source_fk_id = raw_value["id"] if isinstance(raw_value, dict) else raw_value
            target_fk_id = id_map.get(dep_type_key, source_fk_id)
            if target_fk_id is None:
                return None  # dependency not resolved yet (e.g. it was skipped, or is itself ambiguous)
            # NetBox's REST filtering convention for a numeric FK lookup is
            # `<field>_id=<id>` — plain `<field>=<value>` instead filters by
            # that related object's slug/name, which target_fk_id is not.
            resolved[f"{field_name}_id"] = target_fk_id
        else:
            resolved[field_name] = raw_value
    return resolved


def _dependency_type_for_field(type_spec: TypeSpec, field_name: str) -> str | None:
    return type_spec.all_field_map.get(field_name)
