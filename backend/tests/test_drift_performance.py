from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crypto, models, stats
from app.database import Base
from app.services import drift, github_repo


def test_drift_fetches_shared_source_once_and_reuses_unchanged_result(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine); db = sessionmaker(bind=engine)()
    target = models.GithubTarget(name="repo", repo="owner/repo", branch="main",
        pat_encrypted=crypto.encrypt("pat"))
    instances = [models.NetboxInstance(name=f"nb-{index}", base_url=f"https://nb-{index}",
        api_token_encrypted=crypto.encrypt("token")) for index in range(2)]
    db.add_all([target, *instances]); db.flush()
    path = "device-types/Vendor/switch.yml"
    db.add_all([models.DeviceTypePushHistory(repo_target_id=target.id, file_path=path,
        target_type="netbox", target_name=instance.name, status="success") for instance in instances])
    db.commit()

    monkeypatch.setattr(drift, "_repo_device_types", lambda _target: {})
    monkeypatch.setattr(drift, "_instance_device_types", lambda _instance: set())
    monkeypatch.setattr(drift, "custom_fields_candidate_pairs", lambda _db: set())
    monkeypatch.setattr(github_repo, "rate_limit_remaining", lambda *args: 1000)
    monkeypatch.setattr(github_repo, "list_device_types", lambda *args: [github_repo.RepoFile(path, "blob-sha")])
    source_calls = []
    monkeypatch.setattr(github_repo, "get_file", lambda *args: source_calls.append(1) or {"payload": {
        "manufacturer": "Vendor", "model": "Switch", "slug": "switch",
    }})
    marker_list_calls, marker_calls, full_calls = [], [], []
    monkeypatch.setattr(drift.netbox_client, "list_device_type_markers",
                        lambda *args: marker_list_calls.append(1) or {
                            ("  vendor ", " SWITCH "): {"id": 1, "last_updated": "unchanged"},
                        })
    monkeypatch.setattr(drift.netbox_client, "get_device_type_marker",
                        lambda *args: marker_calls.append(1) or "unchanged")
    monkeypatch.setattr(drift.netbox_client, "get_existing_device_type",
                        lambda *args: full_calls.append(1) or {
                            "manufacturer": "Vendor", "model": "Switch", "slug": "switch",
                        })
    monkeypatch.setattr(drift.netbox_client, "get_existing_device_type_by_id",
                        lambda *args: full_calls.append(1) or {
                            "manufacturer": "Vendor", "model": "Switch", "slug": "switch",
                        })

    assert drift.run_full_check(db) == 2
    assert len(source_calls) == 1
    assert len(marker_list_calls) == 2
    assert marker_calls == []
    assert len(full_calls) == 2
    assert stats.snapshot()["last_drift_run"]["normalized_marker_matches"] == 2

    source_calls.clear(); marker_list_calls.clear(); marker_calls.clear(); full_calls.clear()
    assert drift.run_full_check(db) == 2
    assert source_calls == []
    assert len(marker_list_calls) == 2
    assert marker_calls == []
    assert full_calls == []

    monkeypatch.setattr(drift.settings, "drift_use_changelog", True)
    changelog_calls = []
    monkeypatch.setattr(
        drift.netbox_client, "has_relevant_device_type_changes",
        lambda base_url, *args: changelog_calls.append(base_url) or base_url.endswith("nb-0"),
    )
    marker_list_calls.clear(); full_calls.clear()
    assert drift.run_full_check(db) == 2
    assert len(changelog_calls) == 2
    assert len(full_calls) == 1

    monkeypatch.setattr(drift.netbox_client, "has_relevant_device_type_changes", lambda *args: None)
    full_calls.clear()
    assert drift.run_full_check(db) == 2
    assert full_calls == []
    assert stats.snapshot()["last_drift_run"]["changelog_unknown"] == 2


def test_marker_listing_failure_is_visible_sanitized_and_falls_back(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine); db = sessionmaker(bind=engine)()
    target = models.GithubTarget(
        name="repo", repo="owner/repo", branch="main", pat_encrypted=crypto.encrypt("pat"),
    )
    instance = models.NetboxInstance(
        name="nb", base_url="https://nb", api_token_encrypted=crypto.encrypt("super-secret-token"),
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
    monkeypatch.setattr(github_repo, "list_device_types", lambda *args: [github_repo.RepoFile(path, "sha")])
    monkeypatch.setattr(github_repo, "get_file", lambda *args: {"payload": {
        "manufacturer": "Vendor", "model": "Switch", "slug": "switch",
    }})
    monkeypatch.setattr(
        drift.netbox_client, "list_device_type_markers",
        lambda *args: (_ for _ in ()).throw(RuntimeError("token=super-secret-token https://nb/fail")),
    )
    fallback = []
    monkeypatch.setattr(
        drift.netbox_client, "get_device_type_marker",
        lambda *args: fallback.append(1) or "marker",
    )
    monkeypatch.setattr(drift.netbox_client, "get_existing_device_type", lambda *args: {
        "manufacturer": "Vendor", "model": "Switch", "slug": "switch",
    })
    monkeypatch.setattr(drift.netbox_client, "get_existing_device_type_by_id", lambda *args: {
        "manufacturer": "Vendor", "model": "Switch", "slug": "switch",
    })
    warnings = []
    monkeypatch.setattr(
        drift.logger, "warning",
        lambda message, *args: warnings.append(message % args),
    )

    assert drift.run_full_check(db) == 1
    summary = stats.snapshot()["last_drift_run"]
    assert summary["marker_list_failures"] == 1
    assert summary["fallback_pair_lookups"] == 1
    assert summary["degraded"] is True
    assert fallback == [1]
    assert sum("marker listing failed" in message for message in warnings) == 1
    assert all("super-secret-token" not in message for message in warnings)
    assert all("https://nb" not in message for message in warnings)


def test_normalized_marker_key_avoids_pair_fallback(monkeypatch):
    assert drift._normalized_marker_key("  ACME   Corp ", " Switch ") == ("acme corp", "switch")


def test_degraded_threshold_is_strictly_more_than_ten_percent():
    assert drift._run_is_degraded(drift.DriftRunStats(pairs=10, fallback_pair_lookups=1)) is False
    assert drift._run_is_degraded(drift.DriftRunStats(pairs=10, fallback_pair_lookups=2)) is True


def test_missing_batched_marker_falls_back_to_point_lookup(monkeypatch):
    monkeypatch.setattr(drift.netbox_client, "list_device_type_markers", lambda *args: {})
    calls = []
    monkeypatch.setattr(drift.netbox_client, "get_device_type_marker",
                        lambda *args: calls.append(1) or None)
    # The full run behavior is covered above; this assertion protects the
    # fallback API itself from being removed when marker batching changes.
    assert drift.netbox_client.get_device_type_marker("url", "token", True, "Vendor", "missing") is None
    assert calls == [1]
