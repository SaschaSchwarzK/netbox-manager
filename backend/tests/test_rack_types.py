from types import SimpleNamespace

import pytest

from app.rack_type_schema import RackType
from app.routers import rack_types as router
from app.services import diff, github_repo, rack_types


BASE = {
    "manufacturer": "APC", "model": "AR3100", "slug": "apc-ar3100",
    "form_factor": "4-post-cabinet", "width": 19, "u_height": 42, "starting_unit": 1,
}


def test_real_library_rack_shape_round_trips():
    payload = {**BASE, "outer_height": 1991, "outer_depth": 1070, "outer_unit": "mm",
               "weight": 125, "max_weight": 1701, "weight_unit": "kg",
               "mounting_depth": 190, "desc_units": False}
    assert RackType(**payload).to_yaml_dict() == payload


def test_rack_schema_rejects_invalid_library_values():
    with pytest.raises(ValueError):
        RackType(**{**BASE, "slug": "APC AR3100"})
    with pytest.raises(ValueError):
        RackType(**{**BASE, "width": 21})
    with pytest.raises(ValueError):
        RackType(**{**BASE, "weight": 1.005})


def test_rack_paths_get_a_distinct_feature_branch():
    branch = github_repo._feature_branch_name("rack-types/APC/AR3100.yaml")
    assert branch.startswith("rack-type/rack-types-APC-AR3100.yaml-")


def test_push_creates_manufacturer_and_rack_type(monkeypatch):
    created = {}
    manufacturers = SimpleNamespace(
        get=lambda **kwargs: None,
        create=lambda **payload: SimpleNamespace(id=3),
    )
    endpoint = SimpleNamespace(
        get=lambda **kwargs: None,
        create=lambda payload: created.update(payload) or SimpleNamespace(id=8),
    )
    monkeypatch.setattr(rack_types, "get_client", lambda *args: SimpleNamespace(
        dcim=SimpleNamespace(manufacturers=manufacturers, rack_types=endpoint)
    ))
    result = rack_types.push("https://netbox", "token", True, BASE)
    assert result["status"] == "success"
    assert created["manufacturer"] == 3
    assert created["slug"] == "apc-ar3100"


def test_rack_diff_has_no_component_or_image_changes():
    existing = RackType(**BASE).to_internal_dict()
    result = diff.diff_rack_type({**BASE, "u_height": 48}, existing)
    assert result["status"] == "drift"
    assert result["base_field_changes"] == [{"field": "u_height", "source": 48, "netbox": 42}]
    assert result["component_changes"] == {}
    assert result["image_changes"] == []


def test_router_exposes_full_rack_type_workflow():
    paths = {(route.path, method) for route in router.router.routes for method in route.methods}
    expected = {
        ("/api/repos/{target_id}/rack-types", "GET"),
        ("/api/repos/{target_id}/rack-types", "POST"),
        ("/api/repos/{target_id}/rack-types/file", "GET"),
        ("/api/repos/{target_id}/rack-types/file", "PUT"),
        ("/api/repos/{target_id}/rack-types/file", "DELETE"),
        ("/api/repos/{target_id}/rack-types/import", "POST"),
        ("/api/repos/{target_id}/rack-types/file/coverage", "GET"),
        ("/api/repos/{target_id}/rack-types/bulk-import", "POST"),
        ("/api/repos/{target_id}/rack-types/file/diff-with-netbox", "POST"),
        ("/api/repos/{target_id}/rack-types/file/push-to-netbox", "POST"),
        ("/api/repos/{target_id}/rack-types/import-from-netbox/scan", "POST"),
        ("/api/repos/{target_id}/rack-types/import-from-netbox/preview", "POST"),
        ("/api/repos/{target_id}/rack-types/import-from-netbox", "POST"),
    }
    assert expected <= paths
