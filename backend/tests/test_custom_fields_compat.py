from types import SimpleNamespace

import pytest
import responses

from app.customfield_schema import CustomFieldsTemplate
from app.services import diff, netbox_client, netbox_customfields


@responses.activate
def test_connection_rejects_netbox_below_supported_floor():
    responses.get("https://netbox/api/status/", json={"netbox-version": "4.6.7"})
    result = netbox_client.test_connection("https://netbox", "token", True)
    assert result == {
        "ok": False, "netbox_version": "4.6.7",
        "detail": "NetBox 4.6.7 is unsupported; NetBox >= 4.6.8 is required.",
    }


@responses.activate
def test_connection_accepts_supported_netbox_floor():
    responses.get("https://netbox/api/status/", json={"netbox-version": "4.6.8"})
    assert netbox_client.test_connection("https://netbox", "token", True)["ok"] is True


def test_pre_4_content_types_is_rejected():
    with pytest.raises(ValueError, match="content_types is unsupported; use object_types"):
        CustomFieldsTemplate.model_validate({
            "custom_fields": [{"name": "asset_owner", "content_types": ["dcim.device"]}],
        })


def test_custom_field_scope_order_does_not_cause_drift():
    template = {"custom_fields": [{
        "name": "asset_owner", "object_types": ["dcim.rack", "dcim.device"],
    }]}
    existing = {"custom_fields": [{
        "name": "asset_owner", "object_types": ["dcim.device", "dcim.rack"],
    }]}

    result = diff.diff_custom_fields_template(template, existing)

    assert result["status"] == "in_sync"
    assert result["custom_fields"]["changed"] == []


def test_reads_netbox_4x_object_types(monkeypatch):
    field = SimpleNamespace(
        name="asset_owner", label="Asset owner", type="text",
        object_types=["dcim.device", SimpleNamespace(app_label="dcim", model="rack")],
    )
    nb = SimpleNamespace(extras=SimpleNamespace(
        custom_field_choice_sets=SimpleNamespace(all=lambda: []),
        custom_fields=SimpleNamespace(all=lambda: [field]),
    ))
    monkeypatch.setattr(netbox_customfields, "require_custom_fields_version", lambda *args: "4.7.0")
    monkeypatch.setattr(netbox_customfields, "get_client", lambda *args: nb)

    result = netbox_customfields.get_existing_custom_fields("https://netbox", "token", True)

    assert result["custom_fields"][0]["object_types"] == ["dcim.device", "dcim.rack"]
    assert "content_types" not in result["custom_fields"][0]


def test_push_writes_object_types(monkeypatch):
    created = []
    fields = SimpleNamespace(get=lambda **kwargs: None, create=lambda payload: created.append(payload))
    nb = SimpleNamespace(
        extras=SimpleNamespace(
            custom_field_choice_sets=SimpleNamespace(all=lambda: [], get=lambda **kwargs: None, create=lambda payload: None),
            custom_fields=fields,
        ),
        core=SimpleNamespace(object_types=SimpleNamespace(get=lambda **kwargs: None)),
    )
    monkeypatch.setattr(netbox_customfields, "require_custom_fields_version", lambda *args: "4.6.8")
    monkeypatch.setattr(netbox_customfields, "get_client", lambda *args: nb)

    netbox_customfields.push_custom_fields(
        "https://netbox", "token", True,
        {"custom_fields": [{"name": "asset_owner", "object_types": ["dcim.device"]}]},
    )

    assert created == [{"name": "asset_owner", "object_types": ["dcim.device"]}]


@pytest.mark.parametrize("version", ["4.6.8", "4.7.0-rc1"])
def test_custom_fields_version_accepts_supported_versions(monkeypatch, version):
    monkeypatch.setattr(
        netbox_client, "test_connection",
        lambda *args: {"ok": True, "netbox_version": version, "detail": None},
    )
    assert netbox_client.require_custom_fields_version("url", "token", True) == version


@pytest.mark.parametrize("version", ["3.7.0", "4.4.0", "4.6.7"])
def test_custom_fields_version_rejects_old_versions(monkeypatch, version):
    monkeypatch.setattr(
        netbox_client, "test_connection",
        lambda *args: {"ok": True, "netbox_version": version, "detail": None},
    )
    with pytest.raises(RuntimeError, match=r"NetBox >= 4\.6\.8 is required"):
        netbox_client.require_custom_fields_version("url", "token", True)


def test_custom_fields_version_rejects_unverifiable_instance(monkeypatch):
    monkeypatch.setattr(
        netbox_client, "test_connection",
        lambda *args: {"ok": False, "netbox_version": None, "detail": "HTTP 403"},
    )
    with pytest.raises(RuntimeError, match="Could not verify NetBox version: HTTP 403"):
        netbox_client.require_custom_fields_version("url", "token", True)
