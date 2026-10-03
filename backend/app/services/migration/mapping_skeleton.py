"""Build the small, review-focused mapping view from a persisted migration plan."""
from __future__ import annotations

from dataclasses import dataclass
from app.models import MigrationJobItem
from app.services.migration.registry import Registry


@dataclass(frozen=True)
class MappingSkeletonRow:
    override_key: str
    object_type: str
    source_id: int
    source_natural_key: str
    auto_match: str
    target_id: int | None
    target_natural_key: str | None
    match_detail: str | None
    action: str | None = None


def build_mapping_skeleton(items: list[MigrationJobItem], registry: Registry) -> list[MappingSkeletonRow]:
    """Return every planned object so automatic mappings can be reviewed or replaced."""
    rows: list[MappingSkeletonRow] = []
    for item in items:
        auto_match = "ambiguous" if item.planned_action == "ambiguous" else (
            "matched" if item.planned_action in ("map", "update") else "no_match"
        )
        rows.append(MappingSkeletonRow(
            override_key=f"{item.object_type}:{item.source_id}",
            object_type=item.object_type,
            source_id=item.source_id,
            source_natural_key=item.source_natural_key,
            auto_match=auto_match,
            target_id=item.target_id,
            target_natural_key=item.target_natural_key,
            match_detail=item.match_detail,
        ))
    return rows
