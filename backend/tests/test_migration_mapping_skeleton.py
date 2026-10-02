from app import models
from app.services.migration.mapping_skeleton import build_mapping_skeleton
from app.services.migration.registry import load_registry


REGISTRY = load_registry()


def _item(object_type, source_id, key, action, target_id=None, detail=None):
    return models.MigrationJobItem(
        object_type=object_type,
        source_id=source_id,
        source_natural_key=key,
        planned_action=action,
        target_id=target_id,
        match_detail=detail,
    )


def test_mapping_skeleton_contains_reference_and_problem_rows_only():
    rows = build_mapping_skeleton([
        _item("dcim.site", 1, "ams-1", "map", 100),
        _item("dcim.device", 2, "core-1", "map", 200),
        _item("dcim.device", 3, "core-2", "create"),
        _item("dcim.device", 4, "core-3", "ambiguous", detail="strategies disagree: name -> target id 10; slug -> target id 11"),
    ], REGISTRY)
    assert [(row.override_key, row.auto_match, row.target_id) for row in rows] == [
        ("dcim.site:1", "matched", 100),
        ("dcim.device:2", "matched", 200),
        ("dcim.device:3", "no_match", None),
        ("dcim.device:4", "ambiguous", None),
    ]
    assert rows[-1].match_detail.startswith("strategies disagree")
