from app.services.migration.matcher import IdMap
from app.services.migration.registry import load_registry
from app.services.migration.sanitize import build_preview_payload, extract_fields, resolve_fk_refs

REGISTRY = load_registry()


def test_strips_readonly_and_server_fields():
    source_obj = {
        "id": 1, "url": "https://x/api/dcim/sites/1/", "display": "AMS-1",
        "created": "2024-01-01", "last_updated": "2024-01-02",
        "name": "AMS-1", "slug": "ams-1",
    }
    extracted = extract_fields(REGISTRY["dcim.site"], source_obj)
    assert extracted.static_fields == {"name": "AMS-1", "slug": "ams-1"}
    assert extracted.fk_refs == {}
    assert extracted.dropped_custom_fields == []


def test_drops_custom_fields_and_reports_them():
    source_obj = {"name": "AMS-1", "slug": "ams-1", "custom_fields": {"circuit_id": "ABC123"}}
    extracted = extract_fields(REGISTRY["dcim.site"], source_obj)
    assert "custom_fields" not in extracted.static_fields
    assert extracted.dropped_custom_fields == ["custom_fields"]


def test_empty_custom_fields_not_reported_as_dropped():
    source_obj = {"name": "AMS-1", "slug": "ams-1", "custom_fields": {}}
    extracted = extract_fields(REGISTRY["dcim.site"], source_obj)
    assert extracted.dropped_custom_fields == []


def test_unwraps_choice_field():
    source_obj = {"name": "AMS-1", "slug": "ams-1", "status": {"value": "active", "label": "Active"}}
    extracted = extract_fields(REGISTRY["dcim.site"], source_obj)
    assert extracted.static_fields["status"] == "active"


def test_fk_fields_extracted_as_source_ids_not_resolved():
    source_obj = {
        "name": "core-sw-1",
        "site": {"id": 1, "slug": "ams-1"},
        "device_type": {"id": 2},
        "role": {"id": 3},
    }
    extracted = extract_fields(REGISTRY["dcim.device"], source_obj)
    assert extracted.fk_refs == {"site": 1, "device_type": 2, "role": 3}
    assert "site" not in extracted.static_fields  # never mixed into static_fields


def test_null_fk_field_goes_to_static_fields_as_null():
    source_obj = {
        "name": "core-sw-1", "site": {"id": 1}, "device_type": {"id": 2}, "role": {"id": 3},
        "tenant": None,
    }
    extracted = extract_fields(REGISTRY["dcim.device"], source_obj)
    assert extracted.static_fields["tenant"] is None
    assert "tenant" not in extracted.fk_refs


def test_deferred_fk_field_kept_separate_from_regular_fk_refs():
    source_obj = {
        "name": "core-sw-1", "site": {"id": 1}, "device_type": {"id": 2}, "role": {"id": 3},
        "primary_ip4": {"id": 55},
    }
    extracted = extract_fields(REGISTRY["dcim.device"], source_obj)
    assert extracted.deferred_fk_refs == {"primary_ip4": 55}
    assert "primary_ip4" not in extracted.fk_refs
    assert "primary_ip4" not in extracted.static_fields


def test_extract_fields_is_pure_and_id_map_independent():
    # Same input -> same output, called twice, no id_map involved at all.
    source_obj = {"name": "core-sw-1", "site": {"id": 1}, "device_type": {"id": 2}, "role": {"id": 3}}
    a = extract_fields(REGISTRY["dcim.device"], source_obj)
    b = extract_fields(REGISTRY["dcim.device"], source_obj)
    assert a == b


def test_resolve_fk_refs_resolves_against_current_id_map():
    id_map = IdMap()
    fk_refs = {"site": 1, "device_type": 2, "role": 3}
    resolved, unresolved = resolve_fk_refs(REGISTRY["dcim.device"], fk_refs, id_map=id_map)
    assert resolved == {}
    assert unresolved == fk_refs

    id_map.put("dcim.site", 1, 100)
    id_map.put("dcim.devicetype", 2, 200)
    id_map.put("dcim.devicerole", 3, 300)
    resolved, unresolved = resolve_fk_refs(REGISTRY["dcim.device"], fk_refs, id_map=id_map)
    assert resolved == {"site": 100, "device_type": 200, "role": 300}
    assert unresolved == {}


def test_resolve_fk_refs_treats_negative_placeholder_ids_as_resolved():
    # Planner uses negative placeholder ids for "will be created" dependencies;
    # resolve_fk_refs must not treat -5 as falsy/unresolved — only None is "unresolved".
    id_map = IdMap()
    id_map.put("dcim.site", 1, -5)
    resolved, unresolved = resolve_fk_refs(REGISTRY["dcim.device"], {"site": 1}, id_map=id_map)
    assert resolved == {"site": -5}
    assert unresolved == {}


def test_build_preview_payload_merges_static_and_resolved_fk():
    source_obj = {"name": "core-sw-1", "site": {"id": 1}, "device_type": {"id": 2}, "role": {"id": 3}}
    extracted = extract_fields(REGISTRY["dcim.device"], source_obj)
    id_map = IdMap()
    id_map.put("dcim.site", 1, 100)
    id_map.put("dcim.devicetype", 2, 200)
    id_map.put("dcim.devicerole", 3, 300)
    payload = build_preview_payload(REGISTRY["dcim.device"], extracted, id_map=id_map)
    assert payload == {"name": "core-sw-1", "site": 100, "device_type": 200, "role": 300}


def test_build_preview_payload_omits_still_unresolved_fk():
    source_obj = {"name": "core-sw-1", "site": {"id": 1}, "device_type": {"id": 2}, "role": {"id": 3}}
    extracted = extract_fields(REGISTRY["dcim.device"], source_obj)
    payload = build_preview_payload(REGISTRY["dcim.device"], extracted, id_map=IdMap())
    assert payload == {"name": "core-sw-1"}
