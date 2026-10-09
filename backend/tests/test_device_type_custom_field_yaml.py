from types import SimpleNamespace

import pytest

from app.devicetype_schema import DeviceType
from app.services import diff, netbox_client


BASE = {"manufacturer": "Cisco", "model": "C9300-24P", "slug": "c9300-24p"}


def test_netbox_native_custom_field_yaml_round_trip_preserves_value_types():
    source = {
        **BASE,
        "cf_eol_date": "2030-12-31",
        "cf_support_tier": "gold",
        "cf_replacement_cost": 4200,
        "cf_requires_license": True,
    }

    parsed = DeviceType.model_validate(source)

    assert parsed.custom_fields == {
        "eol_date": "2030-12-31",
        "support_tier": "gold",
        "replacement_cost": 4200,
        "requires_license": True,
    }
    rendered = parsed.to_yaml_dict()
    assert "custom_fields" not in rendered
    assert rendered["cf_eol_date"] == "2030-12-31"
    assert rendered["cf_replacement_cost"] == 4200
    assert rendered["cf_requires_license"] is True


def test_legacy_nested_custom_fields_are_written_as_native_cf_keys():
    rendered = DeviceType.model_validate({
        **BASE, "custom_fields": {"eol_date": "2030-12-31"},
    }).to_yaml_dict()

    assert rendered["cf_eol_date"] == "2030-12-31"
    assert "custom_fields" not in rendered


def test_matching_native_and_legacy_values_can_coexist_during_migration():
    parsed = DeviceType.model_validate({
        **BASE, "custom_fields": {"eol_date": "2030-12-31"}, "cf_eol_date": "2030-12-31",
    })
    assert parsed.custom_fields == {"eol_date": "2030-12-31"}


def test_conflicting_native_and_legacy_values_are_rejected():
    with pytest.raises(ValueError, match="Conflicting values"):
        DeviceType.model_validate({
            **BASE, "custom_fields": {"eol_date": "2030-12-31"}, "cf_eol_date": "2031-01-01",
        })


def test_empty_cf_name_is_rejected():
    with pytest.raises(ValueError, match="include a name"):
        DeviceType.model_validate({**BASE, "cf_": "value"})


def test_rest_push_converts_cf_keys_to_nested_custom_fields(monkeypatch):
    created = []
    manufacturer = SimpleNamespace(id=9)
    nb = SimpleNamespace(dcim=SimpleNamespace(
        manufacturers=SimpleNamespace(get=lambda **kwargs: manufacturer),
        device_types=SimpleNamespace(
            get=lambda **kwargs: None,
            create=lambda payload: created.append(payload) or SimpleNamespace(id=42),
        ),
    ))
    monkeypatch.setattr(netbox_client, "get_client", lambda *args: nb)

    result = netbox_client.push_device_type(
        "https://netbox", "token", True, {**BASE, "cf_eol_date": "2030-12-31"}
    )

    assert result["status"] == "success"
    assert created[0]["custom_fields"] == {"eol_date": "2030-12-31"}
    assert "cf_eol_date" not in created[0]


def test_drift_treats_native_cf_yaml_and_rest_shape_as_equal():
    existing = DeviceType.model_validate({
        **BASE, "custom_fields": {"eol_date": "2030-12-31"},
    }).to_internal_dict()
    result = diff.diff_payloads({**BASE, "cf_eol_date": "2030-12-31"}, existing)
    assert result["status"] == "in_sync"
