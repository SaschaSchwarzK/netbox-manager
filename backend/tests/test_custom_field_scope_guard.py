from types import SimpleNamespace

import pytest

from app import schemas
from app.routers import custom_fields as custom_fields_router
from app.services import netbox_customfields


class FakeField:
    def __init__(self, object_types, default=None):
        self.name = "owner"
        self.object_types = object_types
        self.default = default
        self.updated = []

    def serialize(self):
        return {"name": self.name, "object_types": self.object_types, "default": self.default}

    def update(self, payload):
        self.updated.append(payload)
        self.object_types = payload["object_types"]


def _patch_netbox(monkeypatch, field):
    nb = SimpleNamespace(extras=SimpleNamespace(custom_fields=SimpleNamespace(get=lambda **kwargs: field)))
    monkeypatch.setattr(netbox_customfields, "require_custom_fields_version", lambda *args: "4.7.0")
    monkeypatch.setattr(netbox_customfields, "get_client", lambda *args: nb)
    monkeypatch.setattr(netbox_customfields, "_api_session", lambda *args: object())
    return nb


def _preview(monkeypatch, field, proposed, include_backup=True):
    _patch_netbox(monkeypatch, field)
    return netbox_customfields.preview_scope_reductions(
        "https://netbox", "token", True,
        {"custom_fields": [{"name": "owner", "object_types": proposed}]},
        instance={"id": "i1", "name": "lab", "base_url": "https://netbox"},
        actor={"sub": "user-1", "name": "User", "email": None}, include_backup=include_backup,
    )


def test_no_scope_shrink_needs_no_prompt(monkeypatch):
    preview = _preview(monkeypatch, FakeField(["dcim.device"]), ["dcim.device", "dcim.rack"])
    assert preview["reductions"] == []
    assert preview["backup"] is None


def test_scope_shrink_with_zero_values(monkeypatch):
    monkeypatch.setattr(netbox_customfields, "_inspect_removed_type", lambda *args: ({
        "object_type": "dcim.device", "raw_count": 12, "meaningful_count": 0,
        "sample": [], "counting_method": "cf_empty_filter",
    }, []))
    preview = _preview(monkeypatch, FakeField(["dcim.device", "dcim.rack"]), ["dcim.rack"])
    assert preview["reductions"][0]["removed_object_types"] == ["dcim.device"]
    assert preview["reductions"][0]["object_types"][0]["meaningful_count"] == 0


def test_scope_shrink_with_values_builds_hashed_backup(monkeypatch):
    record = {
        "object_type": "dcim.device", "record_id": 7, "url": "https://netbox/api/dcim/devices/7/",
        "display": "edge-1", "affected_custom_fields": [{"name": "owner", "value": "network"}],
    }
    monkeypatch.setattr(netbox_customfields, "_inspect_removed_type", lambda *args: ({
        "object_type": "dcim.device", "raw_count": 20, "meaningful_count": 1,
        "sample": [{"id": 7, "display": "edge-1", "url": record["url"], "value": "network"}],
        "counting_method": "cf_empty_filter",
    }, [record]))
    preview = _preview(monkeypatch, FakeField(["dcim.device"]), [])
    assert preview["backup"]["records"] == [record]
    assert preview["backup"]["sha256"] == netbox_customfields._records_sha256([record])


def test_backup_limit_failure_aborts_preview(monkeypatch):
    monkeypatch.setattr(netbox_customfields, "BACKUP_MAX_RECORDS", 0)
    monkeypatch.setattr(netbox_customfields, "_inspect_removed_type", lambda *args: ({
        "object_type": "dcim.device", "raw_count": 1, "meaningful_count": 1,
        "sample": [], "counting_method": "client_side_pagination",
    }, [{"record_id": 1}]))
    with pytest.raises(netbox_customfields.ScopeInspectionError, match="record safety limit"):
        _preview(monkeypatch, FakeField(["dcim.device"]), [])


def test_low_level_push_rejects_unconfirmed_scope_reduction(monkeypatch):
    field = FakeField(["dcim.device", "dcim.rack"])
    nb = _patch_netbox(monkeypatch, field)
    nb.extras.custom_field_choice_sets = SimpleNamespace(get=lambda **kwargs: None, create=lambda payload: None)
    nb.core = SimpleNamespace(object_types=SimpleNamespace(get=lambda **kwargs: None))
    with pytest.raises(netbox_customfields.ScopeInspectionError, match="without validated confirmation"):
        netbox_customfields.push_custom_fields(
            "https://netbox", "token", True,
            {"custom_fields": [{"name": "owner", "object_types": ["dcim.rack"]}]}, overwrite=True,
        )
    assert field.updated == []


def test_changed_counts_reject_confirmation():
    original = {"reductions": [{
        "field_name": "owner", "removed_object_types": ["dcim.device"],
        "object_types": [{"object_type": "dcim.device", "raw_count": 2, "meaningful_count": 1}],
    }], "confirmation_text": "owner"}
    token = custom_fields_router._scope_serializer.dumps({
        "instance_id": "i1", "fingerprint": netbox_customfields.scope_preview_fingerprint(original),
        "backup_requested": True,
    })
    changed = {**original, "reductions": [{**original["reductions"][0], "object_types": [
        {"object_type": "dcim.device", "raw_count": 2, "meaningful_count": 2}
    ]}]}
    confirmation = schemas.CustomFieldScopeConfirmation(
        token=token, typed_field_names="owner", backup_acknowledged=True,
    )
    with pytest.raises(ValueError, match="counts changed"):
        custom_fields_router._validate_scope_confirmation("i1", changed, confirmation)


def test_restore_round_trip_readds_scope_and_patches_value(monkeypatch):
    field = FakeField(["dcim.rack"])
    _patch_netbox(monkeypatch, field)
    record = {
        "object_type": "dcim.device", "record_id": 7, "url": "https://netbox/api/dcim/devices/7/",
        "display": "edge-1", "affected_custom_fields": [{"name": "owner", "value": "network"}],
    }
    backup = {
        "custom_field_change": {"fields": [{
            "name": "owner", "current_object_types": ["dcim.device", "dcim.rack"],
            "proposed_object_types": ["dcim.rack"], "definition": {},
        }]},
        "records": [record], "sha256": netbox_customfields._records_sha256([record]),
    }

    class Response:
        status_code = 200
        def json(self): return {"custom_fields": {"other": "keep"}}
        def raise_for_status(self): return None

    class Session:
        def __init__(self): self.patches = []
        def get(self, *args, **kwargs): return Response()
        def patch(self, url, json, timeout): self.patches.append((url, json)); return Response()

    session = Session()
    monkeypatch.setattr(netbox_customfields, "_api_session", lambda *args: session)
    monkeypatch.setattr(netbox_customfields, "_resolve_list_url", lambda *args: "https://netbox/api/dcim/devices/")
    result = netbox_customfields.restore_custom_fields_backup("https://netbox", "token", True, backup, dry_run=False)

    assert field.updated == [{"object_types": ["dcim.device", "dcim.rack"]}]
    assert session.patches == [("https://netbox/api/dcim/devices/7/", {"custom_fields": {"other": "keep", "owner": "network"}})]
    assert result["results"][0]["status"] == "restored"
