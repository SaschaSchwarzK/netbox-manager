from app.services.migration.matcher import (
    AmbiguousTargetLookup,
    IdMap,
    MappingAction,
    MappingOverride,
    MatchOutcome,
    match_object,
    natural_key_label,
)
from app.services.migration.registry import load_registry


class FakeTargetLookup:
    """
    Holds a list of (type_key, filters, target_object) triples. `find`
    returns the first target_object whose stored filters are a subset match
    of the requested filters for the same type — good enough to emulate a
    real `?field=value` GET without any HTTP.
    """

    def __init__(self):
        self.entries: list[tuple[str, dict, dict]] = []
        self.calls: list[dict] = []

    def add(self, type_key: str, filters: dict, target_object: dict):
        self.entries.append((type_key, filters, target_object))

    def find(self, type_spec, scalar_filters):
        self.calls.append(scalar_filters)
        for type_key, filters, target_object in self.entries:
            if type_key == type_spec.key and filters == scalar_filters:
                return target_object
        return None


REGISTRY = load_registry()


def test_component_labels_include_device_and_module_context():
    interface = {"name": "eth0", "device": {"name": "router-01"}}
    module = {
        "device": {"name": "router-01"},
        "module_bay": {"name": "Bay 1"},
        "module_type": {"model": "Line Card 4x10G"},
    }
    assert natural_key_label(interface, type_spec=REGISTRY["dcim.interface"]) == "router-01 · eth0"
    assert natural_key_label(module, type_spec=REGISTRY["dcim.module"]) == (
        "router-01 · Bay 1 · Line Card 4x10G"
    )


def test_explicit_mapping_map_short_circuits_everything():
    result = match_object(
        REGISTRY["dcim.site"], {"slug": "ams-1"},
        registry=REGISTRY, id_map=IdMap(), target_lookup=FakeTargetLookup(),
        override=MappingOverride(action=MappingAction.MAP, target_id=99),
    )
    assert result.outcome == MatchOutcome.MAPPED_EXPLICIT
    assert result.target_id == 99


def test_explicit_mapping_skip():
    result = match_object(
        REGISTRY["dcim.site"], {"slug": "ams-1"},
        registry=REGISTRY, id_map=IdMap(), target_lookup=FakeTargetLookup(),
        override=MappingOverride(action=MappingAction.SKIP),
    )
    assert result.outcome == MatchOutcome.SKIPPED_EXPLICIT


def test_explicit_mapping_create_bypasses_auto_match():
    lookup = FakeTargetLookup()
    lookup.add("dcim.site", {"slug": "ams-1"}, {"id": 5})  # would auto-match if tried
    result = match_object(
        REGISTRY["dcim.site"], {"slug": "ams-1"},
        registry=REGISTRY, id_map=IdMap(), target_lookup=lookup,
        override=MappingOverride(action=MappingAction.CREATE),
    )
    assert result.outcome == MatchOutcome.NO_MATCH
    assert lookup.calls == []  # never even queried the target


def test_auto_match_by_slug():
    lookup = FakeTargetLookup()
    lookup.add("dcim.site", {"slug": "ams-1"}, {"id": 5, "slug": "ams-1"})
    result = match_object(
        REGISTRY["dcim.site"], {"slug": "ams-1", "name": "Amsterdam 1"},
        registry=REGISTRY, id_map=IdMap(), target_lookup=lookup, override=None,
    )
    assert result.outcome == MatchOutcome.MATCHED
    assert result.target_id == 5
    assert result.target_natural_key == "ams-1"


def test_auto_match_without_a_readable_label_keeps_id_fallback_available():
    lookup = FakeTargetLookup()
    lookup.add("dcim.site", {"slug": "ams-1"}, {"id": 5})
    result = match_object(
        REGISTRY["dcim.site"], {"slug": "ams-1"}, registry=REGISTRY,
        id_map=IdMap(), target_lookup=lookup, override=None,
    )
    assert result.target_id == 5
    assert result.target_natural_key is None


def test_auto_match_uses_netbox_display_when_canonical_label_fields_are_absent():
    lookup = FakeTargetLookup()
    lookup.add("dcim.site", {"slug": "ams-1"}, {"id": 5, "display": "Amsterdam Campus"})
    result = match_object(
        REGISTRY["dcim.site"], {"slug": "ams-1"}, registry=REGISTRY,
        id_map=IdMap(), target_lookup=lookup, override=None,
    )
    assert result.target_natural_key == "Amsterdam Campus"


def test_no_match_means_create():
    result = match_object(
        REGISTRY["dcim.site"], {"slug": "new-site", "name": "New Site"},
        registry=REGISTRY, id_map=IdMap(), target_lookup=FakeTargetLookup(), override=None,
    )
    assert result.outcome == MatchOutcome.NO_MATCH


def test_devicetype_matches_by_manufacturer_and_part_number_via_id_map():
    id_map = IdMap()
    id_map.put("dcim.manufacturer", 7, 70)  # source manufacturer 7 -> target manufacturer 70
    lookup = FakeTargetLookup()
    lookup.add("dcim.devicetype", {"manufacturer_id": 70, "part_number": "C9300-24T"}, {"id": 500})
    source_obj = {
        "manufacturer": {"id": 7, "slug": "cisco"},
        "part_number": "C9300-24T",
        "model": "Catalyst 9300 24-port",
        "slug": "cisco-c9300-24t",
    }
    result = match_object(
        REGISTRY["dcim.devicetype"], source_obj,
        registry=REGISTRY, id_map=id_map, target_lookup=lookup, override=None,
    )
    assert result.outcome == MatchOutcome.MATCHED
    assert result.target_id == 500
    assert result.matched_strategy_fields == ("manufacturer", "part_number")


def test_devicetype_falls_back_to_manufacturer_and_model_when_part_number_absent():
    id_map = IdMap()
    id_map.put("dcim.manufacturer", 7, 70)
    lookup = FakeTargetLookup()
    lookup.add("dcim.devicetype", {"manufacturer_id": 70, "model": "Catalyst 9300 24-port"}, {"id": 500})
    source_obj = {
        "manufacturer": {"id": 7},
        "part_number": "",  # falsy but present — treated as absent since NetBox often leaves this blank
        "model": "Catalyst 9300 24-port",
    }
    result = match_object(
        REGISTRY["dcim.devicetype"], source_obj,
        registry=REGISTRY, id_map=id_map, target_lookup=lookup, override=None,
    )
    assert result.outcome == MatchOutcome.MATCHED
    assert result.target_id == 500
    assert result.matched_strategy_fields == ("manufacturer", "model")
    assert {"manufacturer_id": 70, "part_number": ""} not in lookup.calls


def test_devicetype_ambiguous_when_part_number_and_model_disagree():
    id_map = IdMap()
    id_map.put("dcim.manufacturer", 7, 70)
    lookup = FakeTargetLookup()
    # Part number points at one existing target device type...
    lookup.add("dcim.devicetype", {"manufacturer_id": 70, "part_number": "QFX5120-48Y-32C"}, {"id": 500})
    # ...but the model string matches a DIFFERENT existing target device type.
    lookup.add("dcim.devicetype", {"manufacturer_id": 70, "model": "QFX5120-48Y"}, {"id": 600})
    source_obj = {
        "manufacturer": {"id": 7},
        "part_number": "QFX5120-48Y-32C",
        "model": "QFX5120-48Y",
    }
    result = match_object(
        REGISTRY["dcim.devicetype"], source_obj,
        registry=REGISTRY, id_map=id_map, target_lookup=lookup, override=None,
    )
    assert result.outcome == MatchOutcome.AMBIGUOUS
    assert "500" in result.detail and "600" in result.detail


def test_ambiguous_match_detail_includes_candidate_labels_and_ids():
    id_map = IdMap()
    id_map.put("dcim.manufacturer", 7, 70)
    lookup = FakeTargetLookup()
    lookup.add("dcim.devicetype", {"manufacturer_id": 70, "part_number": "P1"}, {"id": 500, "model": "Model One"})
    lookup.add("dcim.devicetype", {"manufacturer_id": 70, "model": "Source Model"}, {"id": 600, "model": "Model Two"})
    result = match_object(
        REGISTRY["dcim.devicetype"],
        {"manufacturer": {"id": 7}, "part_number": "P1", "model": "Source Model"},
        registry=REGISTRY, id_map=id_map, target_lookup=lookup, override=None,
    )
    assert result.outcome == MatchOutcome.AMBIGUOUS
    assert "Model One (#500)" in result.detail
    assert "Model Two (#600)" in result.detail


def test_devicetype_agreeing_strategies_are_not_ambiguous():
    id_map = IdMap()
    id_map.put("dcim.manufacturer", 7, 70)
    lookup = FakeTargetLookup()
    lookup.add("dcim.devicetype", {"manufacturer_id": 70, "part_number": "C9300-24T"}, {"id": 500})
    lookup.add("dcim.devicetype", {"manufacturer_id": 70, "model": "Catalyst 9300 24-port"}, {"id": 500})
    source_obj = {"manufacturer": {"id": 7}, "part_number": "C9300-24T", "model": "Catalyst 9300 24-port"}
    result = match_object(
        REGISTRY["dcim.devicetype"], source_obj,
        registry=REGISTRY, id_map=id_map, target_lookup=lookup, override=None,
    )
    assert result.outcome == MatchOutcome.MATCHED
    assert result.target_id == 500


def test_ambiguous_broad_strategy_can_be_resolved_by_later_specific_strategy():
    class BroadThenSpecificLookup(FakeTargetLookup):
        def find(self, type_spec, scalar_filters):
            self.calls.append(scalar_filters)
            if scalar_filters == {"vid": 300, "group_id": "null"}:
                raise AmbiguousTargetLookup("broad VLAN lookup matched multiple targets")
            if scalar_filters == {"vid": 300, "site_id": 110}:
                return {"id": 32, "vid": 300, "name": "Wireless"}
            return None

    id_map = IdMap()
    id_map.put("dcim.site", 10, 110)
    result = match_object(
        REGISTRY["ipam.vlan"],
        {"vid": 300, "group": None, "site": {"id": 10}, "name": "Wireless"},
        registry=REGISTRY, id_map=id_map, target_lookup=BroadThenSpecificLookup(), override=None,
    )
    assert result.outcome == MatchOutcome.MATCHED
    assert result.target_id == 32
    assert result.matched_strategy_fields == ("vid", "site")


def test_unresolved_dependency_yields_unresolvable_not_a_false_no_match():
    # Manufacturer hasn't been migrated/mapped yet (not in id_map) — the
    # matcher must not silently treat this as "no match" (which would try to
    # create a duplicate); it must report it can't safely determine an answer.
    lookup = FakeTargetLookup()
    lookup.add("dcim.devicetype", {"manufacturer_id": 70, "part_number": "C9300-24T"}, {"id": 500})
    source_obj = {"manufacturer": {"id": 7}, "part_number": "C9300-24T", "model": "Catalyst 9300"}
    result = match_object(
        REGISTRY["dcim.devicetype"], source_obj,
        registry=REGISTRY, id_map=IdMap(), target_lookup=lookup, override=None,
    )
    assert result.outcome == MatchOutcome.UNRESOLVABLE


def test_nullable_fk_in_natural_key_matches_explicit_null_instead_of_being_unresolvable():
    lookup = FakeTargetLookup()
    lookup.add("ipam.prefix", {"prefix": "10.112.0.0/15", "vrf_id": "null"}, {"id": 500})
    result = match_object(
        REGISTRY["ipam.prefix"], {"prefix": "10.112.0.0/15", "vrf": None},
        registry=REGISTRY, id_map=IdMap(), target_lookup=lookup, override=None,
    )
    assert result.outcome == MatchOutcome.MATCHED
    assert result.target_id == 500


def test_device_matches_by_name_and_site_fk():
    id_map = IdMap()
    id_map.put("dcim.site", 1, 10)
    lookup = FakeTargetLookup()
    lookup.add("dcim.device", {"name": "core-sw-1", "site_id": 10}, {"id": 999})
    source_obj = {"name": "core-sw-1", "site": {"id": 1, "slug": "ams-1"}}
    result = match_object(
        REGISTRY["dcim.device"], source_obj,
        registry=REGISTRY, id_map=id_map, target_lookup=lookup, override=None,
    )
    assert result.outcome == MatchOutcome.MATCHED
    assert result.target_id == 999
