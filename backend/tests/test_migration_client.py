import pynetbox
import pytest
import responses

from app.services.migration.client import (
    MigrationApiError,
    ReadOnlyViolation,
    build_client,
)
from app.services.netbox_client import check_migration_version_compatibility

BASE = "https://netbox.example"
API = f"{BASE}/api"


def _client(read_only=False):
    client = build_client(BASE, "token", True, read_only=read_only)
    # Don't actually sleep in tests, but keep the counters accurate.
    client._sleep = lambda seconds: None
    return client


@responses.activate
def test_create_succeeds_on_first_try_and_counts_one_request():
    responses.post(f"{API}/dcim/sites/", json={"id": 1, "name": "AMS-1"}, status=201)
    client = _client()
    result = client.create(client.nb.dcim.sites, {"name": "AMS-1", "slug": "ams-1"})
    assert result.id == 1
    assert client.stats.requests_made == 1
    assert client.stats.retries == 0


@responses.activate
def test_429_retries_honoring_retry_after_then_succeeds():
    responses.post(f"{API}/dcim/sites/", status=429, headers={"Retry-After": "2"})
    responses.post(f"{API}/dcim/sites/", json={"id": 2, "name": "LON-1"}, status=201)
    client = _client()
    result = client.create(client.nb.dcim.sites, {"name": "LON-1", "slug": "lon-1"})
    assert result.id == 2
    assert client.stats.retries == 1
    assert client.stats.total_retry_sleep_seconds == 2.0


@responses.activate
def test_429_exhausting_retries_raises_migration_api_error():
    for _ in range(10):
        responses.post(f"{API}/dcim/sites/", status=429)
    client = _client()
    client.max_retries = 3
    with pytest.raises(MigrationApiError):
        client.create(client.nb.dcim.sites, {"name": "X", "slug": "x"})
    assert client.stats.retries == 3


@responses.activate
def test_5xx_retries_with_backoff_then_succeeds():
    responses.post(f"{API}/dcim/sites/", status=502)
    responses.post(f"{API}/dcim/sites/", status=503)
    responses.post(f"{API}/dcim/sites/", json={"id": 3, "name": "PAR-1"}, status=201)
    client = _client()
    result = client.create(client.nb.dcim.sites, {"name": "PAR-1", "slug": "par-1"})
    assert result.id == 3
    assert client.stats.retries == 2


@responses.activate
def test_4xx_validation_error_is_not_retried():
    responses.post(f"{API}/dcim/sites/", json={"name": ["This field is required."]}, status=400)
    client = _client()
    with pytest.raises(pynetbox.RequestError):
        client.create(client.nb.dcim.sites, {"slug": "no-name"})
    assert client.stats.retries == 0
    assert client.stats.requests_made == 1


@responses.activate
def test_read_only_client_refuses_create_without_any_http_call():
    client = _client(read_only=True)
    with pytest.raises(ReadOnlyViolation):
        client.create(client.nb.dcim.sites, {"name": "X", "slug": "x"})
    assert len(responses.calls) == 0


@responses.activate
def test_read_only_client_refuses_update_without_any_http_call():
    responses.get(f"{API}/dcim/sites/", json={
        "count": 1, "next": None, "previous": None,
        "results": [{"id": 1, "name": "AMS-1", "slug": "ams-1", "url": f"{API}/dcim/sites/1/"}],
    })
    client = _client(read_only=True)
    site = client.get(client.nb.dcim.sites, slug="ams-1")
    with pytest.raises(ReadOnlyViolation):
        client.update(site, {"name": "renamed"})


@responses.activate
def test_paginated_retries_a_failing_page_then_continues():
    responses.get(f"{API}/dcim/sites/", status=503)
    responses.get(f"{API}/dcim/sites/", json={
        "count": 2, "next": f"{API}/dcim/sites/?offset=1", "previous": None,
        "results": [{"id": 1, "name": "AMS-1", "slug": "ams-1", "url": f"{API}/dcim/sites/1/"}],
    })
    responses.get(f"{API}/dcim/sites/?offset=1", json={
        "count": 2, "next": None, "previous": None,
        "results": [{"id": 2, "name": "LON-1", "slug": "lon-1", "url": f"{API}/dcim/sites/2/"}],
    })
    client = _client()
    names = [site["name"] for site in client.paginated(client.nb.dcim.sites)]
    assert names == ["AMS-1", "LON-1"]
    assert client.stats.retries == 1


@responses.activate
def test_throttle_enforces_minimum_interval_between_requests():
    responses.post(f"{API}/dcim/sites/", json={"id": 1, "name": "A"}, status=201)
    responses.post(f"{API}/dcim/sites/", json={"id": 2, "name": "B"}, status=201)
    client = _client()
    client.max_requests_per_second = 2.0  # min interval 0.5s
    slept = []
    client._sleep = slept.append
    fake_time = [0.0]
    client._now = lambda: fake_time[0]
    client.create(client.nb.dcim.sites, {"name": "A", "slug": "a"})
    fake_time[0] = 0.1  # only 0.1s elapsed, need to wait ~0.4s more
    client.create(client.nb.dcim.sites, {"name": "B", "slug": "b"})
    assert client.stats.throttle_sleeps == 1
    assert slept and abs(slept[0] - 0.4) < 1e-9


@responses.activate
def test_migration_versions_same_major_minor_have_no_warning():
    responses.get(f"{BASE}/source/api/status/", json={"netbox-version": "4.6.8"})
    responses.get(f"{BASE}/target/api/status/", json={"netbox-version": "4.6.9"})
    source, target, warnings = check_migration_version_compatibility(
        f"{BASE}/source", "source-token", True, f"{BASE}/target", "target-token", True,
    )
    assert source["ok"] and target["ok"]
    assert warnings == []


@responses.activate
def test_migration_versions_different_minor_warn():
    responses.get(f"{BASE}/source/api/status/", json={"netbox-version": "4.6.8"})
    responses.get(f"{BASE}/target/api/status/", json={"netbox-version": "4.7.0"})
    _, _, warnings = check_migration_version_compatibility(
        f"{BASE}/source", "source-token", True, f"{BASE}/target", "target-token", True,
    )
    assert len(warnings) == 1


@responses.activate
def test_migration_versions_different_major_raise():
    responses.get(f"{BASE}/source/api/status/", json={"netbox-version": "4.6.8"})
    responses.get(f"{BASE}/target/api/status/", json={"netbox-version": "5.0.0"})
    with pytest.raises(RuntimeError, match="major versions"):
        check_migration_version_compatibility(
            f"{BASE}/source", "source-token", True, f"{BASE}/target", "target-token", True,
        )


@responses.activate
def test_paginated_chunks_large_id_filters_and_deduplicates_results():
    for result_id in (1, 101, 201):
        responses.get(
            f"{API}/dcim/sites/",
            json={"count": 1, "next": None, "previous": None, "results": [{"id": result_id}]},
        )
    client = _client()
    results = list(client.paginated(client.nb.dcim.sites, device_id=list(range(205))))
    assert [result["id"] for result in results] == [1, 101, 201]
    assert len(responses.calls) == 3
    assert all(call.request.url.count("device_id=") == 100 for call in responses.calls[:2])
    assert responses.calls[2].request.url.count("device_id=") == 5


@responses.activate
def test_paginated_chunks_large_scalar_filters_for_bulk_matching():
    for result_id in (1, 101, 201):
        responses.get(
            f"{API}/dcim/sites/",
            json={"count": 1, "next": None, "previous": None, "results": [{"id": result_id}]},
        )
    client = _client()
    results = list(client.paginated(client.nb.dcim.sites, slug=[f"site-{i}" for i in range(205)]))

    assert [result["id"] for result in results] == [1, 101, 201]
    assert len(responses.calls) == 3
    assert responses.calls[0].request.url.count("slug=") == 100
    assert responses.calls[1].request.url.count("slug=") == 100
    assert responses.calls[2].request.url.count("slug=") == 5


@responses.activate
def test_create_many_requires_one_record_per_payload():
    responses.post(f"{API}/dcim/sites/", json=[{"id": 1}, {"id": 2}], status=201)
    client = _client()
    records = client.create_many(client.nb.dcim.sites, [{"name": "A"}, {"name": "B"}])
    assert [record.id for record in records] == [1, 2]


@responses.activate
def test_update_many_uses_one_bulk_patch_without_getting_each_record_first():
    responses.patch(
        f"{API}/dcim/interfaces/",
        json=[{"id": 10, "name": "eth0"}, {"id": 11, "name": "eth1"}],
    )
    client = _client()
    result = client.update_many(client.nb.dcim.interfaces, [
        {"id": 10, "tags": [99]}, {"id": 11, "tags": [99]},
    ])
    assert [row["id"] for row in result] == [10, 11]
    assert len(responses.calls) == 1
    assert responses.calls[0].request.method == "PATCH"


@responses.activate
def test_options_uses_canonical_url_and_caches_forbidden_response():
    responses.options(f"{API}/dcim/interfaces/", status=403)
    client = _client()

    with pytest.raises(MigrationApiError):
        client.options(client.nb.dcim.interfaces)
    with pytest.raises(MigrationApiError):
        client.options(client.nb.dcim.interfaces)
    with pytest.raises(MigrationApiError):
        client.options(client.nb.dcim.devices)

    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == f"{API}/dcim/interfaces/"
