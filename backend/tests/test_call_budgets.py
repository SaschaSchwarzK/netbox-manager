from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bench.fake_github import FakeGithubRepo
from bench.fake_netbox import FakeNetBox
from app import crypto, models
from app.database import Base
from app.services import drift, github_repo
from app.services import netbox_client


def test_definition_listing_uses_one_branch_and_one_recursive_tree_call(monkeypatch):
    calls = {"branch": 0, "tree": 0}

    class Repo:
        def get_branch(self, branch):
            calls["branch"] += 1
            return SimpleNamespace(commit=SimpleNamespace(sha="root"))

        def get_git_tree(self, sha, recursive=False):
            calls["tree"] += 1
            assert recursive is True
            return SimpleNamespace(
                truncated=False,
                tree=[SimpleNamespace(type="blob", path="device-types/Acme/router.yaml", sha="file")],
            )

    monkeypatch.setattr(github_repo, "_repo", lambda *args: Repo())
    files = github_repo.list_device_types("pat", "org/repo", "main", "device-types")

    assert [item.path for item in files] == ["device-types/Acme/router.yaml"]
    assert calls == {"branch": 1, "tree": 1}


@pytest.fixture
def fake_netbox():
    server = FakeNetBox(latency=0).start()
    try:
        yield server
    finally:
        server.close()


def test_small_drift_read_budget_is_bounded(fake_netbox):
    repo = FakeGithubRepo(count=2, latency=0)
    with patch.object(github_repo, "_repo", return_value=repo):
        files = github_repo.list_device_types("pat", "owner/repo", "main", "device-types")
        for item in files:
            source = github_repo.get_file("pat", "owner/repo", "main", item.path)["payload"]
            netbox_client.get_existing_device_type(fake_netbox.base_url, "token", False,
                                                   source["manufacturer"], source["slug"])
    assert repo.total_requests <= 4
    assert fake_netbox.total_requests <= 40


def test_two_instance_twenty_type_drift_budgets_and_source_reuse(monkeypatch):
    servers = [FakeNetBox(latency=0).start() for _ in range(2)]
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    repo = FakeGithubRepo(count=20, latency=0)
    target = models.GithubTarget(
        name="repo", repo="owner/repo", branch="main", pat_encrypted=crypto.encrypt("pat"),
    )
    instances = [models.NetboxInstance(
        name=f"nb-{index}", base_url=server.base_url,
        api_token_encrypted=crypto.encrypt("token"), verify_ssl=False,
    ) for index, server in enumerate(servers)]
    db.add_all([target, *instances]); db.flush()
    paths = list(repo.files)
    db.add_all([
        models.DeviceTypePushHistory(
            repo_target_id=target.id, file_path=path, target_type="netbox",
            target_name=instance.name, status="success",
        )
        for instance in instances for path in paths
    ])
    db.commit()
    monkeypatch.setattr(drift, "_repo_device_types", lambda _target: {})
    monkeypatch.setattr(drift, "_instance_device_types", lambda _instance: set())
    monkeypatch.setattr(drift, "custom_fields_candidate_pairs", lambda _db: set())
    monkeypatch.setattr(drift.settings, "drift_bulk_component_reads", False)
    monkeypatch.setattr(drift.settings, "drift_use_changelog", False)
    try:
        with patch.object(github_repo, "_repo", return_value=repo):
            assert drift.run_full_check(db) == 40
            # 2 marker pages + 40 × (manufacturer, device type, 9 component endpoints).
            first_netbox_calls = sum(server.total_requests for server in servers)
            assert first_netbox_calls <= 442
            assert first_netbox_calls / 40 <= 11.05
            # One tree listing and one blob read for each of 20 shared source files.
            assert repo.total_requests <= 22

            before_netbox = [server.total_requests for server in servers]
            before_github = repo.total_requests
            assert drift.run_full_check(db) == 40
            # Unchanged pass: exactly one marker-list page per instance; no blobs/components.
            assert [server.total_requests - before for server, before in zip(servers, before_netbox)] == [1, 1]
            assert repo.counts["contents"] == 20
            assert repo.total_requests - before_github <= 2

            original_tree = repo.get_git_tree
            changed_path = paths[0]

            def changed_tree(sha, recursive=False):
                tree = original_tree(sha, recursive=recursive)
                for item in tree.tree:
                    if item.path == changed_path:
                        item.sha = "changed-sha"
                return tree

            repo.get_git_tree = changed_tree
            before_contents = repo.counts["contents"]
            before_netbox = [server.total_requests for server in servers]
            drift.run_full_check(db)
            assert repo.counts["contents"] - before_contents == 1
            # Both instance pairs for the changed file are fully checked; all others reuse markers.
            assert sum(server.total_requests - before for server, before in zip(servers, before_netbox)) <= 24
    finally:
        db.close()
        engine.dispose()
        for server in servers:
            server.close()


def test_bulk_component_read_budget_for_two_instances_twenty_types(monkeypatch):
    servers = [FakeNetBox(latency=0).start() for _ in range(2)]
    try:
        for server in servers:
            markers = netbox_client.list_device_type_markers(server.base_url, "token", False)
            netbox_client.get_existing_device_types_bulk(
                server.base_url, "token", False,
                [entry["id"] for entry in list(markers.values())[:20]],
            )
        # Per instance: one marker page + one device-type page + nine component pages.
        assert sum(server.total_requests for server in servers) <= 22, [server.counts for server in servers]
    finally:
        for server in servers:
            server.close()


def test_one_instance_diff_and_push_budget_is_bounded(fake_netbox):
    source = FakeGithubRepo(count=1, latency=0).files["device-types/Vendor/switch-0000.yml"]
    import yaml
    payload = yaml.safe_load(source)
    netbox_client.get_existing_device_type(fake_netbox.base_url, "token", False, "Vendor", "switch-0000")
    result = netbox_client.push_device_type(fake_netbox.base_url, "token", False, payload, overwrite=True)
    assert result["status"] == "success"
    assert fake_netbox.total_requests <= 32


def test_component_bulk_failure_falls_back_and_reports_only_bad_object(monkeypatch):
    updated = []

    class Endpoint:
        def create(self, payload):
            if isinstance(payload, list):
                raise RuntimeError("bulk unsupported")
            if payload["name"] == "bad":
                raise RuntimeError("invalid component")
            updated.append(payload["name"])

    summary = {"created": 0, "updated": 0, "failed": []}
    netbox_client._bulk_component_write(
        Endpoint(), [{"name": "good"}, {"name": "bad"}], "created", summary
    )

    assert updated == ["good"]
    assert summary["created"] == 1
    assert summary["failed"] == [{"name": "bad", "error": "invalid component"}]


def test_one_instance_search_budget_is_bounded(fake_netbox):
    netbox_client.search_instance(fake_netbox.base_url, "token", False, "router")
    assert fake_netbox.total_requests <= 5


@pytest.mark.parametrize("raw", [
    "aa:bb:cc:dd:ee:ff", "aa-bb-cc-dd-ee-ff", "aabb.ccdd.eeff", "aabbccddeeff",
])
def test_supported_mac_spellings_are_normalized(raw):
    assert netbox_client._normalize_mac_query(raw) == "aa:bb:cc:dd:ee:ff"


def test_non_mac_search_does_not_run_mac_endpoints(fake_netbox):
    netbox_client.search_instance(fake_netbox.base_url, "token", False, "router")
    assert all("mac-address" not in path and "interfaces" not in path
               for _method, path in fake_netbox.counts)


def test_one_instance_fleet_budget_is_two_calls(fake_netbox):
    netbox_client.get_health(fake_netbox.base_url, "token", False)
    netbox_client.check_token_expiry(fake_netbox.base_url, "token", False)
    assert fake_netbox.total_requests <= 2


def test_netbox_sessions_are_reused_without_raw_token_cache_keys():
    netbox_client.close_sessions()
    first = netbox_client.get_session("https://netbox.example", "super-secret", True)
    second = netbox_client.get_session("https://netbox.example/", "super-secret", True)

    assert first is second
    assert all("super-secret" not in part for key in netbox_client._sessions for part in key)
    assert first.headers["Authorization"] == "Token super-secret"
    assert first.adapters["https://"]._pool_maxsize == 10
    netbox_client.close_sessions()
