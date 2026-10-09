from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import responses

from bench.fake_netbox import FakeNetBox
from app.devicetype_schema import COMPONENT_ENDPOINTS
from app.services import netbox_client


URL = "https://netbox.example/api/core/object-changes/"
SINCE = datetime(2026, 10, 7, 10, 0, 0)


def _content_type(endpoint: str) -> str:
    return f"dcim.{endpoint.removesuffix('s').replace('-', '')}"


def test_drift_change_types_are_derived_from_component_endpoints():
    assert set(netbox_client.DRIFT_CHANGE_TYPES) == {
        "dcim.devicetype", *(_content_type(value) for value in COMPONENT_ENDPOINTS.values()),
    }


def _callback(changed_type=None, oldest="2026-01-01T00:00:00+00:00", fail_type=None):
    def respond(request):
        query = parse_qs(urlparse(request.url).query)
        if query.get("ordering") == ["time"]:
            if oldest is None:
                return 200, {}, '{"count": 0, "results": []}'
            return 200, {}, f'{{"count": 1, "results": [{{"time": "{oldest}"}}]}}'
        object_type = query.get("changed_object_type", [None])[-1]
        if object_type == fail_type:
            return 500, {}, '{"detail": "failed"}'
        count = 1 if object_type == changed_type else 0
        return 200, {}, f'{{"count": {count}, "results": []}}'
    return respond


@responses.activate
def test_changelog_requests_one_type_each_with_aware_margin(monkeypatch):
    monkeypatch.setattr(netbox_client.settings, "drift_changelog_margin_seconds", 300)
    responses.add_callback(responses.GET, URL, callback=_callback())

    assert netbox_client.has_relevant_device_type_changes(
        "https://netbox.example", "secret", True, SINCE,
    ) is False

    filtered = [call for call in responses.calls if "changed_object_type" in call.request.url]
    assert len(filtered) == len(netbox_client.DRIFT_CHANGE_TYPES)
    seen = set()
    for call in filtered:
        query = parse_qs(urlparse(call.request.url).query)
        assert len(query["changed_object_type"]) == 1
        assert query["time_after"] == ["2026-10-07T09:55:00+00:00"]
        seen.add(query["changed_object_type"][0])
    assert seen == set(netbox_client.DRIFT_CHANGE_TYPES)


@responses.activate
def test_single_valued_filter_detects_non_last_type_and_short_circuits(monkeypatch):
    changed = "dcim.interfacetemplate"
    responses.add_callback(responses.GET, URL, callback=_callback(changed_type=changed))
    session = netbox_client.get_session("https://netbox.example", "secret", True)

    # Demonstrate the old request shape against last-value-wins behavior.
    old_response = session.get(URL, params=[
        ("time_after", SINCE.replace(tzinfo=UTC).isoformat()),
        *(("changed_object_type", item) for item in netbox_client.DRIFT_CHANGE_TYPES),
    ])
    assert old_response.json()["count"] == 0

    assert netbox_client.has_relevant_device_type_changes(
        "https://netbox.example", "secret", True, SINCE,
    ) is True
    filtered = [call for call in responses.calls[1:] if "changed_object_type" in call.request.url]
    assert any(changed in call.request.url for call in filtered)
    assert len(filtered) < len(netbox_client.DRIFT_CHANGE_TYPES)


@responses.activate
def test_changelog_any_filter_failure_is_unknown():
    responses.add_callback(
        responses.GET, URL,
        callback=_callback(fail_type="dcim.powerporttemplate"),
    )
    assert netbox_client.has_relevant_device_type_changes(
        "https://netbox.example", "secret", True, SINCE,
    ) is None

    responses.reset()
    responses.add_callback(responses.GET, URL, callback=_callback(oldest=None))
    assert netbox_client.has_relevant_device_type_changes(
        "https://netbox.example", "secret", True, SINCE,
    ) is None


@responses.activate
def test_changelog_truncated_or_empty_history_is_unknown():
    responses.add_callback(
        responses.GET, URL,
        callback=_callback(oldest="2026-10-07T09:59:00+00:00"),
    )
    assert netbox_client.has_relevant_device_type_changes(
        "https://netbox.example", "secret", True, SINCE,
    ) is None


def test_single_valued_fake_regresses_old_multi_value_shape(monkeypatch):
    fake = FakeNetBox(latency=0).start()
    fake.object_changes = [
        {"id": 1, "time": "2020-01-01T00:00:00+00:00", "changed_object_type": "dcim.devicetype"},
        {"id": 2, "time": "2099-01-01T00:00:00+00:00", "changed_object_type": "dcim.interfacetemplate"},
    ]
    monkeypatch.setattr(netbox_client.settings, "drift_changelog_margin_seconds", 0)
    try:
        session = netbox_client.get_session(fake.base_url, "token", False)
        old = session.get(
            f"{fake.base_url}/api/core/object-changes/",
            params=[
                ("time_after", SINCE.replace(tzinfo=UTC).isoformat()),
                *(("changed_object_type", item) for item in netbox_client.DRIFT_CHANGE_TYPES),
            ],
        )
        assert old.json()["count"] == 0
        assert netbox_client.has_relevant_device_type_changes(
            fake.base_url, "token", False, SINCE,
        ) is True
    finally:
        fake.close()
