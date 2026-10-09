import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import responses


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify-netbox-compat.py"
SPEC = importlib.util.spec_from_file_location("verify_netbox_compat", SCRIPT)
compat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compat)


def _session(token="test-secret"):
    compat.set_redaction_secrets(token)
    session = requests.Session()
    session.headers["Authorization"] = f"Token {token}"
    return session


def _templates():
    return [
        {"id": 101, "device_type": {"id": 1}},
        {"id": 102, "device_type": {"id": 2}},
    ]


def _component_callback(absent_status=200, forbidden_single=False, token=None):
    def callback(request):
        values = parse_qs(urlparse(request.url).query).get("device_type_id", [])
        if not values:
            return 200, {}, json.dumps({"count": 2, "results": _templates()})
        if values == [compat.ABSENT_DEVICE_TYPE_ID]:
            body = {"detail": f"bad token={token}"} if absent_status != 200 else {"count": 0, "results": []}
            return absent_status, {}, json.dumps(body)
        if forbidden_single and values == ["1"]:
            return 403, {}, json.dumps({"detail": f"denied token={token}"})
        rows = [template for template in _templates() if str(template["device_type"]["id"]) in values]
        return 200, {}, json.dumps({"count": len(rows), "results": rows})

    return callback


@responses.activate
def test_absent_id_400_is_informational_and_endpoint_passes():
    url = "https://nb/api/dcim/interface-templates/"
    responses.add_callback(responses.GET, url, callback=_component_callback(absent_status=400))

    result = compat.check_component_filters(
        _session(), "https://nb", [1, 2], {"interfaces": "interface-templates"},
    )["interface-templates"]

    assert result["status"] == "pass"
    assert result["detail"] == "repeated=2 union_of_individual=2 parents_sampled=2"
    assert "HTTP 400 validation error" in result["absent_id"]["detail"]


@responses.activate
def test_repeated_filter_ignored_fails_endpoint():
    url = "https://nb/api/dcim/interface-templates/"

    def callback(request):
        values = parse_qs(urlparse(request.url).query).get("device_type_id", [])
        if not values:
            return 200, {}, json.dumps({"count": 2, "results": _templates()})
        if values == [compat.ABSENT_DEVICE_TYPE_ID]:
            return 200, {}, json.dumps({"count": 0, "results": []})
        # Emulate a backend which keeps only the final repeated query value.
        rows = [template for template in _templates() if str(template["device_type"]["id"]) == values[-1]]
        return 200, {}, json.dumps({"count": len(rows), "results": rows})

    responses.add_callback(responses.GET, url, callback=callback)
    result = compat.check_component_filters(
        _session(), "https://nb", [1, 2], {"interfaces": "interface-templates"},
    )["interface-templates"]

    assert result["status"] == "fail"
    assert result["detail"] == "repeated=1 union_of_individual=2 parents_sampled=2"


@responses.activate
def test_empty_individual_union_is_inconclusive_and_recommendation_is_false():
    url = "https://nb/api/dcim/interface-templates/"

    def callback(request):
        values = parse_qs(urlparse(request.url).query).get("device_type_id", [])
        if not values:
            return 200, {}, json.dumps({"count": 2, "results": _templates()})
        return 200, {}, json.dumps({"count": 0, "results": []})

    responses.add_callback(responses.GET, url, callback=callback)
    component = compat.check_component_filters(
        _session(), "https://nb", [1, 2], {"interfaces": "interface-templates"},
    )
    result = component["interface-templates"]

    assert result["status"] == "inconclusive"
    assert "no templates found on the sampled device types" in result["detail"]
    assert "repeated=0 union_of_individual=0 parents_sampled=2" in result["detail"]
    assert compat.recommendations(component, {})[0] is False


@responses.activate
def test_ignored_filter_guard_fails_when_single_requests_do_not_narrow_results():
    url = "https://nb/api/dcim/interface-templates/"

    def callback(request):
        values = parse_qs(urlparse(request.url).query).get("device_type_id", [])
        if values == [compat.ABSENT_DEVICE_TYPE_ID]:
            return 200, {}, json.dumps({"count": 0, "results": []})
        return 200, {}, json.dumps({"count": 2, "results": _templates()})

    responses.add_callback(responses.GET, url, callback=callback)
    result = compat.check_component_filters(
        _session(), "https://nb", [1, 2], {"interfaces": "interface-templates"},
    )["interface-templates"]

    assert result["status"] == "fail"
    assert "filter did not narrow" in result["detail"]


@responses.activate
def test_single_real_id_403_has_diagnostic_and_redacts_token():
    token = "super-secret-token-value"
    url = "https://nb/api/dcim/interface-templates/"
    responses.add_callback(
        responses.GET, url,
        callback=_component_callback(absent_status=400, forbidden_single=True, token=token),
    )

    result = compat.check_component_filters(
        _session(token), "https://nb", [1, 2], {"interfaces": "interface-templates"},
    )["interface-templates"]
    message = result["detail"]

    assert result["status"] == "fail"
    assert "HTTP 403" in message
    assert "/api/dcim/interface-templates/?limit=1000&device_type_id=1" in message
    assert "denied" in message
    assert token not in message
    assert "[REDACTED]" in message


def test_no_device_types_is_inconclusive_not_failed():
    result = compat.check_component_filters(
        _session(), "https://nb", [], {"interfaces": "interface-templates"},
    )["interface-templates"]

    assert result["status"] == "inconclusive"
    assert result["detail"] == "no device types exist"


@responses.activate
def test_changelog_since_reports_counts_and_request_query():
    url = "https://nb/api/core/object-changes/"
    since = "2026-10-07T10:00:00+02:00"

    def callback(request):
        query = parse_qs(urlparse(request.url).query)
        if query.get("ordering") == ["time"]:
            return 200, {}, json.dumps({"count": 0, "results": []})
        selected = query.get("changed_object_type", [])
        count = 2 if selected == ["dcim.devicetype"] else 0
        if len(selected) > 1:
            count = 2
        assert query.get("time_after") == [since] or len(selected) > 1
        return 200, {}, json.dumps({"count": count, "results": []})

    responses.add_callback(responses.GET, url, callback=callback)
    result = compat.check_changelog(
        _session(), "https://nb", ("dcim.devicetype", "dcim.interfacetemplate"), since,
    )

    assert result["type:dcim.devicetype"]["detail"] == f"2 changes since {since}; verified"
    assert result["type:dcim.interfacetemplate"]["detail"] == f"0 changes since {since}; not verified"


def test_recommendations_require_evidence_not_filter_acceptance():
    component = {
        "interface-templates": {
            "status": "pass",
            "detail": "repeated=2 union_of_individual=2 parents_sampled=2",
            "absent_id": compat._check(True, "informational"),
        },
    }
    changelog = {
        "endpoint": compat._check(True, "available"),
        "ordering": compat._check(True, "supported"),
        "oldest": compat._check(True, "empty"),
        "type:dcim.devicetype": compat._check(True, "filter accepted"),
        "repeated_filter": compat._check(True, "accepted"),
    }

    assert compat.recommendations(component, changelog) == (True, False)
    assert compat.recommendations(component, changelog, True) == (True, True)

    component["interface-templates"]["status"] = "inconclusive"
    assert compat.recommendations(component, changelog, True) == (False, True)


def test_since_recommendation_requires_non_zero_count_for_every_type():
    component = {
        "endpoint": {"status": "pass"},
    }
    changelog = {
        "endpoint": compat._check(True, "available"),
        "ordering": compat._check(True, "supported"),
        "oldest": compat._check(True, "empty"),
        "type:a": compat._check(True, "1 changes since 2026-01-01T00:00:00Z; verified"),
        "type:b": compat._check(True, "0 changes since 2026-01-01T00:00:00Z; not verified"),
    }
    assert compat.recommendations(component, changelog, True, "2026-01-01T00:00:00Z") == (
        True, False,
    )


@responses.activate
def test_setup_failure_diagnostic_never_prints_token(monkeypatch, capsys):
    token = "never-print-this-token"
    monkeypatch.setenv("NB_URL", "https://nb")
    monkeypatch.setenv("NB_TOKEN", token)
    responses.add(
        responses.GET, "https://nb/api/dcim/device-types/",
        status=403, body=f'{{"detail":"token={token}"}}',
    )

    assert compat.main([]) == 1
    output = capsys.readouterr().out
    assert token not in output
    assert "HTTP 403" in output
    assert "/api/dcim/device-types/?limit=200" in output
    assert "[REDACTED]" in output
