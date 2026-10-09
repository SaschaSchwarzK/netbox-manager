import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crypto, models
from app.database import Base
from app.devicetype_schema import DeviceType
from app.services import drift, github_repo


def _setup(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    target = models.GithubTarget(
        name="repo", repo="owner/repo", branch="main", pat_encrypted=crypto.encrypt("pat"),
    )
    instance = models.NetboxInstance(
        name="nb", base_url="https://nb", api_token_encrypted=crypto.encrypt("token"),
    )
    db.add_all([target, instance]); db.flush()
    path = "device-types/Vendor/switch.yml"
    db.add(models.DeviceTypePushHistory(
        repo_target_id=target.id, file_path=path, target_type="netbox",
        target_name=instance.name, status="success",
    ))
    db.commit()
    monkeypatch.setattr(drift, "_repo_device_types", lambda _target: {})
    monkeypatch.setattr(drift, "_instance_device_types", lambda _instance: set())
    monkeypatch.setattr(drift, "custom_fields_candidate_pairs", lambda _db: set())
    monkeypatch.setattr(github_repo, "rate_limit_remaining", lambda *args: 1000)
    monkeypatch.setattr(
        github_repo, "list_device_types", lambda *args: [github_repo.RepoFile(path, "same-sha")],
    )
    return engine, db, target, instance, path


def _record(db, instance, target, path):
    return db.query(models.DriftRecord).filter_by(
        instance_id=instance.id, repo_target_id=target.id, file_path=path,
    ).one()


def _existing(**updates):
    result = DeviceType(
        manufacturer="Vendor", model="Switch", slug="switch",
    ).to_internal_dict()
    result.update(updates)
    return result


def _existing_with_interface(label=""):
    result = DeviceType(
        manufacturer="Vendor", model="Switch", slug="switch",
        interfaces=[{"name": "Ethernet1", "type": "1000base-t"}],
    ).to_internal_dict()
    if label:
        result["interfaces"][0]["label"] = label
    return result


def test_marker_change_with_unchanged_repo_fetches_real_source_and_recovers(monkeypatch):
    engine, db, target, instance, path = _setup(monkeypatch)
    state = {"marker": "m1", "u_height": 2}
    source_calls = []
    monkeypatch.setattr(github_repo, "get_file", lambda *args: source_calls.append(1) or {"payload": {
        "manufacturer": "Vendor", "model": "Switch", "slug": "switch", "u_height": 1,
    }})
    monkeypatch.setattr(drift.netbox_client, "list_device_type_markers", lambda *args: {
        ("Vendor", "switch"): {"id": 1, "last_updated": state["marker"]},
    })
    monkeypatch.setattr(
        drift.netbox_client, "get_existing_device_type_by_id",
        lambda *args: _existing(u_height=state["u_height"]),
    )
    try:
        drift.run_full_check(db)
        assert _record(db, instance, target, path).status == "drift"
        source_calls.clear()

        state.update(marker="m2", u_height=3)
        for _ in range(3):
            drift.run_full_check(db)
            record = _record(db, instance, target, path)
            assert record.status == "drift"
            assert "validation error" not in record.detail_json
        assert source_calls == [1]
    finally:
        db.close(); engine.dispose()


def test_failed_source_record_retries_and_recovers_next_pass(monkeypatch):
    engine, db, target, instance, path = _setup(monkeypatch)
    attempts = {"count": 0}

    def source(*args):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary GitHub failure")
        return {"payload": {"manufacturer": "Vendor", "model": "Switch", "slug": "switch"}}

    monkeypatch.setattr(github_repo, "get_file", source)
    monkeypatch.setattr(drift.netbox_client, "list_device_type_markers", lambda *args: {
        ("Vendor", "switch"): {"id": 1, "last_updated": "m1"},
    })
    monkeypatch.setattr(drift.netbox_client, "get_existing_device_type_by_id", lambda *args: _existing())
    try:
        drift.run_full_check(db)
        assert _record(db, instance, target, path).status == "error"
        drift.run_full_check(db)
        assert _record(db, instance, target, path).status == "in_sync"
    finally:
        db.close(); engine.dispose()


def test_on_demand_source_failure_clears_sha_then_recovers(monkeypatch):
    engine, db, target, instance, path = _setup(monkeypatch)
    source_payload = {"payload": {
        "manufacturer": "Vendor", "model": "Switch", "slug": "switch",
    }}
    monkeypatch.setattr(github_repo, "get_file", lambda *args: source_payload)
    monkeypatch.setattr(drift.netbox_client, "list_device_type_markers", lambda *args: {
        ("Vendor", "switch"): {"id": 1, "last_updated": "m1"},
    })
    monkeypatch.setattr(drift.netbox_client, "get_existing_device_type_by_id", lambda *args: _existing())
    try:
        drift.run_full_check(db)

        class FlippingMarkers(dict):
            calls = 0

            def get(self, key, default=None):
                value = super().get(key, default)
                if value and key == ("Vendor", "switch"):
                    self.calls += 1
                    return {**value, "last_updated": "m1" if self.calls == 1 else "m2"}
                return value

        monkeypatch.setattr(drift.netbox_client, "list_device_type_markers", lambda *args: FlippingMarkers({
            ("Vendor", "switch"): {"id": 1, "last_updated": "m1"},
        }))
        monkeypatch.setattr(
            github_repo, "get_file",
            lambda *args: (_ for _ in ()).throw(RuntimeError("on-demand failure")),
        )
        drift.run_full_check(db)
        record = _record(db, instance, target, path)
        assert record.status == "error"
        assert json.loads(record.detail_json)["_drift_meta"]["source_sha"] is None

        monkeypatch.setattr(github_repo, "get_file", lambda *args: source_payload)
        monkeypatch.setattr(drift.netbox_client, "list_device_type_markers", lambda *args: {
            ("Vendor", "switch"): {"id": 1, "last_updated": "m2"},
        })
        drift.run_full_check(db)
        assert _record(db, instance, target, path).status == "in_sync"
    finally:
        db.close(); engine.dispose()


def test_component_only_change_waits_for_forced_full_recheck(monkeypatch):
    engine, db, target, instance, path = _setup(monkeypatch)
    state = {"label": ""}
    monkeypatch.setattr(drift.settings, "drift_full_recheck_every", 2)
    source_calls = []
    monkeypatch.setattr(github_repo, "get_file", lambda *args: source_calls.append(1) or {"payload": {
        "manufacturer": "Vendor", "model": "Switch", "slug": "switch",
        "interfaces": [{"name": "Ethernet1", "type": "1000base-t"}],
    }})
    monkeypatch.setattr(drift.netbox_client, "list_device_type_markers", lambda *args: {
        ("Vendor", "switch"): {"id": 1, "last_updated": "unchanged-marker"},
    })
    monkeypatch.setattr(
        drift.netbox_client, "get_existing_device_type_by_id",
        lambda *args: _existing_with_interface(state["label"]),
    )
    try:
        drift.run_full_check(db)
        state["label"] = "edited in NetBox"
        source_calls.clear()
        drift.run_full_check(db)
        assert _record(db, instance, target, path).status == "in_sync"
        assert source_calls == []
        drift.run_full_check(db)
        assert _record(db, instance, target, path).status == "drift"
        assert source_calls == [1]
    finally:
        db.close(); engine.dispose()


def test_normalized_marker_match_reads_device_type_by_matched_id(monkeypatch):
    engine, db, target, instance, path = _setup(monkeypatch)
    monkeypatch.setattr(github_repo, "get_file", lambda *args: {"payload": {
        "manufacturer": "Vendor", "model": "Switch", "slug": "switch",
    }})
    monkeypatch.setattr(drift.netbox_client, "list_device_type_markers", lambda *args: {
        (" vendor ", " SWITCH "): {"id": 42, "last_updated": "m1"},
    })
    ids = []
    monkeypatch.setattr(
        drift.netbox_client, "get_existing_device_type",
        lambda *args: (_ for _ in ()).throw(AssertionError("exact-name lookup must not run")),
    )
    monkeypatch.setattr(
        drift.netbox_client, "get_existing_device_type_by_id",
        lambda _url, _token, _verify, device_type_id: ids.append(device_type_id) or _existing(
            manufacturer="vendor", slug="SWITCH",
        ),
    )
    try:
        drift.run_full_check(db)
        assert ids == [42]
        assert _record(db, instance, target, path).status == "in_sync"
    finally:
        db.close(); engine.dispose()
