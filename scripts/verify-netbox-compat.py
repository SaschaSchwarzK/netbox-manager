#!/usr/bin/env python3
"""Read-only verification of drift-related behavior on a staging NetBox."""

import argparse
import os
import re
import sys
from datetime import datetime
from urllib.parse import urlsplit

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

from app.devicetype_schema import COMPONENT_ENDPOINTS  # noqa: E402
from app.services.netbox_client import DRIFT_CHANGE_TYPES  # noqa: E402

ABSENT_DEVICE_TYPE_ID = "2147483647"
_REDACTION_SECRETS = set()
_TOKEN_VALUE = re.compile(
    r"(?i)(\b(?:authorization|token|api[_-]?key|access[_-]?token)\b\s*[\"']?\s*[:=]\s*[\"']?)"
    r"([^\s\"',;}]+)"
)


class CheckError(RuntimeError):
    """A request or response failure with safe, actionable context."""


def set_redaction_secrets(*secrets):
    _REDACTION_SECRETS.clear()
    _REDACTION_SECRETS.update(value for value in secrets if value)


def _redact(value):
    text = str(value)
    for secret in sorted(_REDACTION_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return _TOKEN_VALUE.sub(r"\1[REDACTED]", text)


def _request_path(url):
    parsed = urlsplit(url)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _http_error(response, reason=None):
    detail = (
        f"HTTP {response.status_code} GET {_request_path(response.url)}; "
        f"body={_redact(response.text[:200])!r}"
    )
    if reason:
        detail += f"; {reason}"
    return CheckError(detail)


def _get(session, url, _trace=None, **kwargs):
    try:
        response = session.get(url, timeout=30, **kwargs)
    except requests.RequestException as exc:
        request_url = getattr(getattr(exc, "request", None), "url", url)
        raise CheckError(
            f"HTTP status unavailable GET {_request_path(request_url)}; "
            f"body unavailable; {_redact(type(exc).__name__)}"
        ) from exc
    if not 200 <= response.status_code < 300:
        raise _http_error(response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise _http_error(response, "invalid JSON response") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise _http_error(response, "unexpected paginated response")
    if _trace is not None:
        _trace.append(
            f"HTTP {response.status_code} GET {_request_path(response.url)}; "
            f"body={_redact(response.text[:200])!r}"
        )
    return payload


def _all_results(session, url, params, trace=None):
    rows = []
    next_url = url
    next_params = params
    while next_url:
        page = _get(session, next_url, params=next_params, _trace=trace)
        rows.extend(page["results"])
        next_url = page.get("next")
        next_params = None
    return rows


def _check(ok, detail):
    return {"ok": ok, "detail": detail}


def _device_type_id(template):
    parent = template.get("device_type", template.get("device_type_id"))
    if isinstance(parent, dict):
        parent = parent.get("id")
    return parent


def _component_result(status, detail, absent_check=None):
    return {
        "status": status,
        "detail": detail,
        "absent_id": absent_check or _check(None, "not attempted"),
    }


def _check_absent_id(session, url):
    try:
        response = session.get(
            url, timeout=30,
            params={"limit": "1", "device_type_id": ABSENT_DEVICE_TYPE_ID},
        )
        if response.status_code == 400:
            return _check(True, "handled as HTTP 400 validation error")
        if response.status_code == 200:
            try:
                payload = response.json()
                count = payload.get("count", len(payload.get("results", [])))
                detail = (
                    "handled as HTTP 200 with zero results" if count == 0
                    else f"HTTP 200 returned {count} result(s); informational only"
                )
                return _check(True, detail)
            except (TypeError, ValueError):
                return _check(True, f"informational: {_http_error(response, 'invalid JSON response')}")
        return _check(True, f"informational: {_http_error(response)}")
    except requests.RequestException as exc:
        return _check(True, f"informational: {_redact(type(exc).__name__)}")


def check_component_filters(session, base_url, device_type_ids, endpoints=COMPONENT_ENDPOINTS):
    results = {}
    for endpoint_name in endpoints.values():
        url = f"{base_url}/api/dcim/{endpoint_name}/"
        if not device_type_ids:
            results[endpoint_name] = _component_result(
                "inconclusive", "no device types exist",
            )
            continue

        try:
            unfiltered = _get(session, url, params={"limit": "100"})
            unfiltered_total = unfiltered.get("count", len(unfiltered["results"]))
            candidate_ids = set(device_type_ids)
            parent_ids = []
            for template in unfiltered["results"]:
                parent_id = _device_type_id(template)
                if parent_id in candidate_ids and parent_id not in parent_ids:
                    parent_ids.append(parent_id)
                if len(parent_ids) == 10:
                    break
        except (CheckError, TypeError, ValueError, KeyError) as exc:
            results[endpoint_name] = _component_result("fail", _redact(exc))
            continue

        absent_check = _check_absent_id(session, url)
        parents_sampled = len(parent_ids)
        if parents_sampled < 2:
            results[endpoint_name] = _component_result(
                "inconclusive",
                f"fewer than 2 distinct parents have templates; parents_sampled={parents_sampled}",
                absent_check,
            )
            continue

        try:
            repeated = _all_results(session, url, [
                ("limit", "1000"),
                *(("device_type_id", str(value)) for value in parent_ids),
            ])
            individual_ids = set()
            individual_counts = []
            for device_type_id in parent_ids:
                individual = _all_results(session, url, {
                    "limit": "1000", "device_type_id": str(device_type_id),
                })
                individual_counts.append(len(individual))
                individual_ids.update(item["id"] for item in individual)
            repeated_ids = {item["id"] for item in repeated}
        except (CheckError, TypeError, ValueError, KeyError) as exc:
            results[endpoint_name] = _component_result("fail", _redact(exc), absent_check)
            continue

        counts = (
            f"repeated={len(repeated_ids)} union_of_individual={len(individual_ids)} "
            f"parents_sampled={parents_sampled}"
        )
        if not individual_ids:
            results[endpoint_name] = _component_result(
                "inconclusive",
                f"no templates found on the sampled device types; {counts}",
                absent_check,
            )
        elif repeated_ids != individual_ids:
            results[endpoint_name] = _component_result("fail", counts, absent_check)
        elif not any(count < unfiltered_total for count in individual_counts):
            results[endpoint_name] = _component_result(
                "fail", f"filter did not narrow the unfiltered result; {counts}", absent_check,
            )
        else:
            results[endpoint_name] = _component_result("pass", counts, absent_check)
    return results


def check_changelog(session, base_url, change_types=DRIFT_CHANGE_TYPES, since=None):
    url = f"{base_url}/api/core/object-changes/"
    results = {}
    try:
        oldest = _get(session, url, params={"limit": "1", "ordering": "time"})
        results["endpoint"] = _check(True, "available")
        results["ordering"] = _check(True, "supported")
        oldest_time = oldest["results"][0].get("time") if oldest["results"] else None
        results["oldest"] = _check(True, oldest_time or "log is empty")
    except (CheckError, TypeError, ValueError, KeyError) as exc:
        detail = _redact(exc)
        return {name: _check(False, detail) for name in ("endpoint", "ordering", "oldest")}

    individual_counts = {}
    for content_type in change_types:
        params = {"limit": "1", "changed_object_type": content_type}
        if since is not None:
            params["time_after"] = since
        try:
            filtered = _get(session, url, params=params)
            count = filtered.get("count", len(filtered["results"]))
            individual_counts[content_type] = count
            detail = (
                "filter accepted (not proof that edits are detected)" if since is None
                else f"{count} changes since {since}; {'verified' if count else 'not verified'}"
            )
            results[f"type:{content_type}"] = _check(True, detail)
        except (CheckError, TypeError, ValueError, KeyError) as exc:
            results[f"type:{content_type}"] = _check(False, _redact(exc))

    try:
        repeated = _get(session, url, params=[
            ("limit", "1"),
            ("changed_object_type", change_types[0]),
            ("changed_object_type", change_types[-1]),
        ])
        repeated_count = repeated.get("count", len(repeated["results"]))
        first_count = individual_counts.get(change_types[0])
        last_count = individual_counts.get(change_types[-1])
        if first_count != last_count and repeated_count == last_count:
            behavior = "only the last value appears effective"
        elif repeated_count == first_count + last_count:
            behavior = "repeated values appear to be honored"
        else:
            behavior = "accepted but behavior is inconclusive"
        results["repeated_filter"] = _check(True, behavior)
    except (CheckError, TypeError, ValueError, KeyError) as exc:
        results["repeated_filter"] = _check(False, _redact(exc))
    return results


def run_checks(session, base_url, since=None):
    device_types = _get(session, f"{base_url}/api/dcim/device-types/", params={"limit": "200"})
    ids = [item["id"] for item in device_types["results"][:200]]
    return (
        check_component_filters(session, base_url, ids),
        check_changelog(session, base_url, since=since),
    )


def _parse_since(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a valid ISO8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("must include a timezone (for example Z or +00:00)")
    return value


def recommendations(component, changelog, staging_edit_verified=False, since=None):
    bulk_ok = bool(component) and all(value["status"] == "pass" for value in component.values())
    changelog_ok = all(
        value["ok"] for name, value in changelog.items() if name != "repeated_filter"
    )
    if since is not None:
        type_checks = [value for name, value in changelog.items() if name.startswith("type:")]
        changelog_ok = changelog_ok and bool(type_checks) and all(
            value["detail"].endswith("; verified")
            for value in type_checks
        )
    return bulk_ok, staging_edit_verified and changelog_ok


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    parser.add_argument("--ca-bundle", help="PEM CA bundle used to verify NetBox")
    parser.add_argument("--since", type=_parse_since, help="ISO8601 start time used to count edits per content type")
    parser.add_argument(
        "--staging-edit-verified", action="store_true",
        help="I edited a template of every component type and confirmed non-zero counts",
    )
    args = parser.parse_args(argv)
    if args.insecure and args.ca_bundle:
        parser.error("--insecure and --ca-bundle are mutually exclusive")
    base_url = os.environ.get("NB_URL", "").rstrip("/")
    token = os.environ.get("NB_TOKEN", "")
    if not base_url or not token:
        parser.error("NB_URL and NB_TOKEN must be set")
    set_redaction_secrets(token)

    session = requests.Session()
    session.headers.update({"Authorization": f"Token {token}", "Accept": "application/json"})
    session.verify = False if args.insecure else (args.ca_bundle or True)
    try:
        component, changelog = run_checks(session, base_url, args.since)
    except (CheckError, TypeError, ValueError, KeyError) as exc:
        print(f"FAIL setup: {_redact(exc)}")
        return 1

    failed = False
    for endpoint, result in component.items():
        print(f"{result['status'].upper()} component {endpoint}: {result['detail']}")
        print(f"  INFO absent_id: {result['absent_id']['detail']}")
        failed = failed or result["status"] == "fail"
    for name, result in changelog.items():
        print(f"{'PASS' if result['ok'] else 'FAIL'} changelog {name}: {result['detail']}")
        failed = failed or (name != "repeated_filter" and not result["ok"])

    if args.since is not None:
        verified = [
            name.removeprefix("type:") for name, result in changelog.items()
            if name.startswith("type:") and result["ok"]
            and result["detail"].endswith("; verified")
        ]
        print(f"Verified by non-zero count since {args.since}: {', '.join(verified) if verified else 'none'}")

    bulk_ok, changelog_ok = recommendations(
        component, changelog, args.staging_edit_verified, args.since,
    )
    if bulk_ok:
        print("Recommended NBM_DRIFT_BULK_COMPONENT_READS=true")
    else:
        blockers = ", ".join(
            f"{name} ({result['status']})"
            for name, result in component.items() if result["status"] != "pass"
        )
        print(f"Recommended NBM_DRIFT_BULK_COMPONENT_READS=false: {blockers}")
    print(f"Recommended NBM_DRIFT_USE_CHANGELOG={'true' if changelog_ok else 'false'}")
    if not args.staging_edit_verified:
        print(
            "Changelog filter acceptance is not proof of detection. Pass --staging-edit-verified only "
            "after editing a template of every component type and confirming non-zero counts."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
