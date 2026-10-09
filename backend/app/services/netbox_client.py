import time
import re
import os
import json
import hashlib
import threading
from datetime import datetime, timedelta
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import pynetbox
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.devicetype_schema import COMPONENT_ENDPOINTS, COMPONENT_EXTRA_FIELDS, DeviceType
from app.image_safety import validate_image_bytes
from app.services.github_repo import BinaryRepoFile
from app.timeutil import as_utc_aware
from app.config import settings


def verify_for_instance(instance) -> bool | str:
    if not getattr(instance, "ca_bundle_pem", None):
        return instance.verify_ssl
    path = Path(f"/tmp/netbox-manager-ca-{instance.id}.pem")
    content = instance.ca_bundle_pem.encode()
    if not path.exists() or path.read_bytes() != content:
        path.write_bytes(content)
        os.chmod(path, 0o600)
    return str(path)


class _TimeoutSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", 30)
        return super().request(method, url, **kwargs)


_sessions: OrderedDict[tuple[str, str, str], requests.Session] = OrderedDict()
_sessions_lock = threading.RLock()
_SESSION_CACHE_SIZE = 32


def get_session(base_url: str, token: str, verify_ssl: bool | str) -> requests.Session:
    """Return a bounded, thread-safe pooled session without retaining tokens in keys."""
    key = (base_url.rstrip("/"), hashlib.sha256(token.encode()).hexdigest(), str(verify_ssl))
    with _sessions_lock:
        session = _sessions.pop(key, None)
        if session is not None:
            _sessions[key] = session
            return session
        session = _TimeoutSession()
        session.headers.update({"Authorization": f"Token {token}", "Accept": "application/json"})
        session.verify = verify_ssl
        retry = Retry(total=2, backoff_factor=0.3, status_forcelist=[502, 503, 504],
                      allowed_methods=["GET", "HEAD"])
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _sessions[key] = session
        while len(_sessions) > _SESSION_CACHE_SIZE:
            _sessions.popitem(last=False)[1].close()
        return session


def close_sessions() -> None:
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        session.close()


def get_client(base_url: str, token: str, verify_ssl: bool | str) -> pynetbox.api:
    client = pynetbox.api(base_url, token=token)
    client.http_session.close()
    client.http_session = get_session(base_url, token, verify_ssl)
    return client


def test_connection(base_url: str, token: str, verify_ssl: bool) -> dict[str, Any]:
    """Hit NetBox's /api/status/ endpoint to validate URL + token."""
    url = base_url.rstrip("/") + "/api/status/"
    try:
        resp = get_session(base_url, token, verify_ssl).get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            version = data.get("netbox-version")
            parsed = _parse_netbox_version(version)
            if parsed is None:
                return {"ok": False, "netbox_version": version, "detail": f"Could not parse NetBox version {version!r}; NetBox >= 4.6.8 is required."}
            if parsed < MIN_CUSTOM_FIELDS_VERSION:
                return {"ok": False, "netbox_version": version, "detail": f"NetBox {version} is unsupported; NetBox >= 4.6.8 is required."}
            return {"ok": True, "netbox_version": version, "detail": None}
        if resp.status_code == 401:
            return {
                "ok": False, "netbox_version": None,
                "detail": "Authentication failed: NetBox rejected the API token (HTTP 401).",
            }
        if resp.status_code == 403:
            return {
                "ok": False, "netbox_version": None,
                "detail": "Authorization failed: the API token cannot access NetBox status (HTTP 403).",
            }
        return {
            "ok": False,
            "netbox_version": None,
            "detail": f"HTTP {resp.status_code}: {resp.text[:200]}",
        }
    except requests.RequestException as exc:
        return {"ok": False, "netbox_version": None, "detail": str(exc)}


MIN_CUSTOM_FIELDS_VERSION = (4, 6, 8)


def _parse_netbox_version(version: Any) -> tuple[int, int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", str(version or ""))
    return tuple(int(part or 0) for part in match.groups()) if match else None


def require_custom_fields_version(base_url: str, token: str, verify_ssl: bool) -> str:
    """Fail clearly unless the instance meets the supported NetBox floor."""
    result = test_connection(base_url, token, verify_ssl)
    if not result["ok"]:
        raise RuntimeError(f"Could not verify NetBox version: {result['detail']}")
    version = str(result.get("netbox_version") or "")
    parsed = _parse_netbox_version(version)
    if parsed is None:
        raise RuntimeError(f"Could not parse NetBox version {version!r}; NetBox >= 4.6.8 is required.")
    if parsed < MIN_CUSTOM_FIELDS_VERSION:
        raise RuntimeError(
            f"NetBox {version} is unsupported; NetBox >= 4.6.8 is required."
        )
    return version


def check_migration_version_compatibility(
    source_url: str, source_token: str, source_verify_ssl: bool,
    target_url: str, target_token: str, target_verify_ssl: bool,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Read both status endpoints and return results plus non-blocking warnings."""
    source = test_connection(source_url, source_token, source_verify_ssl)
    target = test_connection(target_url, target_token, target_verify_ssl)
    if not source["ok"] or not target["ok"]:
        failed = "source" if not source["ok"] else "target"
        detail = (source if failed == "source" else target).get("detail")
        raise RuntimeError(f"Could not determine NetBox version for {failed}: {detail}")
    source_version = _parse_netbox_version(source.get("netbox_version"))
    target_version = _parse_netbox_version(target.get("netbox_version"))
    if source_version is None or target_version is None:
        raise RuntimeError("Could not determine NetBox version for source or target.")
    if source_version[0] != target_version[0]:
        raise RuntimeError(
            f"NetBox major versions are incompatible: source={source['netbox_version']}, "
            f"target={target['netbox_version']}."
        )
    warnings: list[str] = []
    if source_version[:2] != target_version[:2]:
        warnings.append(
            f"Source and target use different NetBox minor versions "
            f"({source['netbox_version']} vs {target['netbox_version']}); verify type compatibility."
        )
    return source, target, warnings


def _normalize_mac_query(query: str) -> str | None:
    compact = re.sub(r"[:.\-]", "", query.strip())
    if not re.fullmatch(r"[0-9a-fA-F]{12}", compact):
        return None
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2)).lower()


def search_instance(base_url: str, token: str, verify_ssl: bool, query: str,
                    limit: int = 50) -> dict[str, Any]:
    """
    Search devices, virtual machines, virtual device contexts (VDCs), IP
    addresses, prefixes, and MAC addresses on one NetBox instance, using
    NetBox's built-in `q` quick-search filter where available (name, serial,
    asset tag, address, etc.) and an exact `mac_address` filter for MACs,
    since NetBox's interface filter doesn't support partial MAC matching.
    """
    nb = get_client(base_url, token, verify_ssl)
    web_base = base_url.rstrip("/")
    fetch_limit = limit + 1
    mac_query = _normalize_mac_query(query)

    queries = {
        "devices": lambda: nb.dcim.devices.filter(q=query, limit=fetch_limit),
        "vms": lambda: nb.virtualization.virtual_machines.filter(q=query, limit=fetch_limit),
        "vdcs": lambda: nb.dcim.virtual_device_contexts.filter(q=query, limit=fetch_limit),
        "ips": lambda: nb.ipam.ip_addresses.filter(q=query, limit=fetch_limit),
        "prefixes": lambda: nb.ipam.prefixes.filter(q=query, limit=fetch_limit),
    }
    if mac_query:
        queries.update({
            "dcim_macs": lambda: nb.dcim.interfaces.filter(mac_address=mac_query, limit=fetch_limit),
            "vm_macs": lambda: nb.virtualization.interfaces.filter(mac_address=mac_query, limit=fetch_limit),
            "macs": lambda: nb.dcim.mac_addresses.filter(mac_address=mac_query, limit=fetch_limit),
        })

    def fetch(search):
        try:
            return list(search())
        except pynetbox.RequestError:
            return []

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {key: executor.submit(fetch, search) for key, search in queries.items()}
        records = {key: future.result() for key, future in futures.items()}

    devices = []
    for d in records["devices"]:
        devices.append({
            "id": d.id,
            "name": d.name or f"(unnamed device #{d.id})",
            "serial": getattr(d, "serial", None) or None,
            "type_display": str(d.device_type) if getattr(d, "device_type", None) else None,
            "site": str(d.site) if getattr(d, "site", None) else None,
            "status": str(d.status) if getattr(d, "status", None) else None,
            "url": f"{web_base}/dcim/devices/{d.id}/",
        })

    vms = []
    for v in records["vms"]:
        vms.append({
            "id": v.id,
            "name": v.name or f"(unnamed VM #{v.id})",
            "serial": None,  # virtual machines have no serial field in NetBox
            "type_display": str(v.role) if getattr(v, "role", None) else None,
            "site": str(v.site) if getattr(v, "site", None) else None,
            "status": str(v.status) if getattr(v, "status", None) else None,
            "url": f"{web_base}/virtualization/virtual-machines/{v.id}/",
        })

    vdcs = []
    for c in records["vdcs"]:
        vdcs.append({
            "id": c.id,
            "name": c.name,
            "serial": None,
            "type_display": str(c.device) if getattr(c, "device", None) else None,
            "site": None,
            "status": str(c.status) if getattr(c, "status", None) else None,
            "url": f"{web_base}/dcim/virtual-device-contexts/{c.id}/",
        })

    ip_addresses = []
    for ip in records["ips"]:
        assigned = getattr(ip, "assigned_object", None)
        ip_addresses.append({
            "id": ip.id,
            "name": str(ip.address),
            "serial": None,
            "type_display": str(assigned) if assigned else "(unassigned)",
            "site": None,
            "status": str(ip.status) if getattr(ip, "status", None) else None,
            "url": f"{web_base}/ipam/ip-addresses/{ip.id}/",
        })

    prefixes = []
    for p in records["prefixes"]:
        prefixes.append({
            "id": p.id,
            "name": str(p.prefix),
            "serial": None,
            "type_display": str(p.role) if getattr(p, "role", None) else None,
            "site": str(p.site) if getattr(p, "site", None) else None,
            "status": str(p.status) if getattr(p, "status", None) else None,
            "url": f"{web_base}/ipam/prefixes/{p.id}/",
        })

    mac_addresses = []
    try:
        for iface in records.get("dcim_macs", []):
            device = getattr(iface, "device", None)
            mac_addresses.append({
                "id": iface.id,
                "name": str(getattr(iface, "mac_address", query)),
                "serial": None,
                "type_display": f"Interface: {device}/{iface.name}" if device else f"Interface: {iface.name}",
                "site": None,
                "status": None,
                "url": f"{web_base}/dcim/interfaces/{iface.id}/",
            })
    except pynetbox.RequestError:
        pass  # query isn't a valid MAC address format; NetBox rejects it rather than returning empty

    try:
        for iface in records.get("vm_macs", []):
            vm = getattr(iface, "virtual_machine", None)
            mac_addresses.append({
                "id": iface.id,
                "name": str(getattr(iface, "mac_address", query)),
                "serial": None,
                "type_display": f"VM interface: {vm}/{iface.name}" if vm else f"VM interface: {iface.name}",
                "site": None,
                "status": None,
                "url": f"{web_base}/virtualization/interfaces/{iface.id}/",
            })
    except pynetbox.RequestError:
        pass

    try:
        # Dedicated MAC Address objects (an interface can have several).
        for m in records.get("macs", []):
            assigned = getattr(m, "assigned_object", None)
            mac_addresses.append({
                "id": m.id,
                "name": str(m.mac_address),
                "serial": None,
                "type_display": f"Assigned to: {assigned}" if assigned else "(unassigned)",
                "site": None,
                "status": None,
                "url": f"{web_base}/dcim/mac-addresses/{m.id}/",
            })
    except pynetbox.RequestError:
        pass  # the free-form search query is not a valid MAC address

    collections = {"devices": devices, "virtual_machines": vms, "virtual_device_contexts": vdcs,
                   "ip_addresses": ip_addresses, "prefixes": prefixes, "mac_addresses": mac_addresses}
    truncated = {key: len(value) > limit for key, value in collections.items()}
    return {**{key: value[:limit] for key, value in collections.items()}, "truncated": truncated}


def get_health(base_url: str, token: str, verify_ssl: bool) -> dict[str, Any]:
    """Reachability + NetBox/plugin versions, from the /api/status/ endpoint."""
    url = base_url.rstrip("/") + "/api/status/"
    start = time.monotonic()
    try:
        resp = get_session(base_url, token, verify_ssl).get(url, timeout=10)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "reachable": True,
                "netbox_version": data.get("netbox-version"),
                "python_version": data.get("python-version"),
                "plugins": data.get("plugins", {}),
                "response_time_ms": elapsed_ms,
                "error": None,
            }
        return {
            "reachable": False, "netbox_version": None, "python_version": None, "plugins": {},
            "response_time_ms": elapsed_ms, "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
        }
    except requests.RequestException as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "reachable": False, "netbox_version": None, "python_version": None, "plugins": {},
            "response_time_ms": elapsed_ms, "error": str(exc),
        }


def check_token_expiry(base_url: str, token: str, verify_ssl: bool) -> dict[str, Any]:
    """
    Best-effort: NetBox's own /api/users/tokens/ only shows tokens the caller
    has permission to view. A typical single-purpose service account token can
    usually see just itself; if more than one comes back we genuinely can't
    tell which is the active one, so we say so rather than guessing.
    """
    url = base_url.rstrip("/") + "/api/users/tokens/"
    try:
        resp = get_session(base_url, token, verify_ssl).get(url, timeout=10)
        if resp.status_code != 200:
            return {"known": False, "expires": None, "note": f"Can't check (HTTP {resp.status_code}) — token likely lacks permission to view tokens."}
        results = resp.json().get("results", [])
        if len(results) == 1:
            return {"known": True, "expires": results[0].get("expires"), "note": None}
        if len(results) == 0:
            return {"known": False, "expires": None, "note": "No tokens visible to this token."}
        return {"known": False, "expires": None, "note": f"{len(results)} tokens visible — can't tell which one is active."}
    except requests.RequestException as exc:
        return {"known": False, "expires": None, "note": str(exc)}


def list_device_types_on_instance(base_url: str, token: str, verify_ssl: bool) -> list[dict]:
    """Lightweight list of every device type on an instance, for a selection UI (not the full component definitions)."""
    nb = get_client(base_url, token, verify_ssl)
    results = []
    for dt in nb.dcim.device_types.all():
        results.append({
            "manufacturer": str(dt.manufacturer),
            "model": str(dt.model),
            "slug": str(dt.slug),
            "part_number": str(dt.part_number) if getattr(dt, "part_number", None) is not None else None,
            "u_height": float(dt.u_height) if dt.u_height is not None else None,
        })
    return results


def list_device_type_markers(base_url: str, token: str, verify_ssl: bool) -> dict[tuple[str, str], dict]:
    """Fetch all cheap drift markers for one instance in a single paginated listing."""
    nb = get_client(base_url, token, verify_ssl)
    markers = {}
    for device_type in nb.dcim.device_types.all():
        manufacturer = getattr(device_type, "manufacturer", None)
        manufacturer_name = getattr(manufacturer, "name", None) or str(manufacturer)
        markers[(manufacturer_name, str(device_type.slug))] = {
            "id": int(device_type.id),
            "last_updated": str(
                getattr(device_type, "last_updated", None) or "present-without-last-updated"
            ),
        }
    return markers


def _choice_value(field) -> str | None:
    """
    NetBox choice fields (status, subdevice_role, weight_unit, port `type`, ...)
    come back from pynetbox as objects whose .value is the raw stored string
    (e.g. "parent") and str() is the display label (e.g. "Parent"). We need the
    raw value to compare against the source YAML, which always uses raw values.
    """
    if field is None:
        return None
    return field.value if hasattr(field, "value") else str(field)


def _device_type_result(device_type, manufacturer_name: str) -> dict[str, Any]:
    image_urls = {
        side: getattr(device_type, f"{side}_image", None) or None
        for side in ("front", "rear")
    }
    result: dict[str, Any] = {
        "manufacturer": manufacturer_name,
        "model": device_type.model,
        "slug": device_type.slug,
        "part_number": device_type.part_number or None,
        "u_height": float(device_type.u_height) if device_type.u_height is not None else None,
        "is_full_depth": device_type.is_full_depth,
        "subdevice_role": _choice_value(getattr(device_type, "subdevice_role", None)),
        "weight": float(device_type.weight) if device_type.weight is not None else None,
        "weight_unit": _choice_value(getattr(device_type, "weight_unit", None)),
        "comments": device_type.comments or None,
        "custom_fields": dict(getattr(device_type, "custom_fields", None) or {}),
        "front_image": bool(image_urls["front"]) or None,
        "rear_image": bool(image_urls["rear"]) or None,
        "_image_urls": image_urls,
    }
    for key in COMPONENT_ENDPOINTS:
        result[key] = []
    return result


def _component_result(item, key: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": item.name}
    item_type = _choice_value(getattr(item, "type", None))
    if item_type:
        entry["type"] = item_type
    if getattr(item, "label", None):
        entry["label"] = item.label
    if getattr(item, "description", None):
        entry["description"] = item.description
    for extra_field in COMPONENT_EXTRA_FIELDS.get(key, []):
        value = getattr(item, extra_field, None)
        if extra_field in ("power_port", "rear_port") and value is not None:
            value = str(value)
        elif hasattr(value, "value"):
            value = value.value
        if value not in (None, ""):
            entry[extra_field] = value
    return entry


def get_existing_device_type(base_url: str, token: str, verify_ssl: bool, manufacturer_name: str, slug: str) -> dict | None:
    """
    Fetch a device type and its component templates back from NetBox, reshaped
    into the same hyphenated-key structure as the GitHub source YAML, so it can
    be diffed directly with diff.diff_payloads(). Returns None if the
    manufacturer or device type doesn't exist on this instance at all.
    """
    nb = get_client(base_url, token, verify_ssl)

    manufacturer = nb.dcim.manufacturers.get(name=manufacturer_name)
    if manufacturer is None:
        return None
    dt = nb.dcim.device_types.get(manufacturer_id=manufacturer.id, slug=slug)
    if dt is None:
        return None

    return _existing_device_type_result(nb, dt)


def _existing_device_type_result(nb, device_type) -> dict[str, Any]:
    manufacturer = getattr(device_type, "manufacturer", None)
    manufacturer_name = getattr(manufacturer, "name", None) or str(manufacturer)
    result = _device_type_result(device_type, manufacturer_name)

    for key, endpoint_name in COMPONENT_ENDPOINTS.items():
        endpoint = getattr(nb.dcim, endpoint_name.replace("-", "_"))
        for item in endpoint.filter(device_type_id=device_type.id):
            result[key].append(_component_result(item, key))

    return result


def get_existing_device_type_by_id(
    base_url: str, token: str, verify_ssl: bool, device_type_id: int,
) -> dict | None:
    """Read a device type and its components by the marker listing's matched id."""
    nb = get_client(base_url, token, verify_ssl)
    device_type = nb.dcim.device_types.get(id=device_type_id)
    return _existing_device_type_result(nb, device_type) if device_type is not None else None


def get_existing_device_types_bulk(
    base_url: str, token: str, verify_ssl: bool, device_type_ids: list[int], batch_size: int = 50,
) -> dict[int, dict[str, Any]]:
    """Load device types and all component templates in bounded multi-value batches.

    This optimization is setting-gated because multi-value ``device_type_id``
    compatibility still needs verification against every supported NetBox version.
    """
    nb = get_client(base_url, token, verify_ssl)
    results: dict[int, dict[str, Any]] = {}
    unique_ids = list(dict.fromkeys(int(item) for item in device_type_ids))
    for start in range(0, len(unique_ids), batch_size):
        batch = unique_ids[start:start + batch_size]
        if not batch:
            continue
        for device_type in nb.dcim.device_types.filter(id=batch):
            manufacturer = getattr(device_type, "manufacturer", None)
            manufacturer_name = getattr(manufacturer, "name", None) or str(manufacturer)
            results[int(device_type.id)] = _device_type_result(device_type, manufacturer_name)
        for key, endpoint_name in COMPONENT_ENDPOINTS.items():
            endpoint = getattr(nb.dcim, endpoint_name.replace("-", "_"))
            for item in endpoint.filter(device_type_id=batch):
                parent = getattr(item, "device_type", None)
                parent_id = getattr(parent, "id", parent)
                if parent_id is not None and int(parent_id) in results:
                    results[int(parent_id)][key].append(_component_result(item, key))
    return results


def get_device_type_marker(base_url: str, token: str, verify_ssl: bool,
                           manufacturer_name: str, slug: str) -> str | None:
    """Cheap marker used to skip unchanged drift pairs without loading components."""
    nb = get_client(base_url, token, verify_ssl)
    manufacturer = nb.dcim.manufacturers.get(name=manufacturer_name)
    if manufacturer is None:
        return None
    device_type = nb.dcim.device_types.get(manufacturer_id=manufacturer.id, slug=slug)
    if device_type is None:
        return None
    return str(getattr(device_type, "last_updated", None) or "present-without-last-updated")


DRIFT_CHANGE_TYPES = (
    "dcim.devicetype",
    *(f"dcim.{endpoint.removesuffix('s').replace('-', '')}" for endpoint in COMPONENT_ENDPOINTS.values()),
)


def _change_count(session, url: str, params: list[tuple[str, str]]) -> bool | None:
    try:
        response = session.get(url, params=params, timeout=30)
        if response.status_code in (400, 403, 404):
            return None
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
            return None
        count = payload.get("count", len(payload["results"]))
        if not isinstance(count, int):
            return None
        return count > 0
    except (requests.RequestException, TypeError, ValueError):
        return None


def has_relevant_device_type_changes(
    base_url: str, token: str, verify_ssl: bool, since,
) -> bool | None:
    """Return whether NetBox logged relevant changes, or None when the shortcut is unavailable."""
    session = get_session(base_url, token, verify_ssl)
    url = f"{base_url.rstrip('/')}/api/core/object-changes/"
    aware_since = as_utc_aware(since) - timedelta(seconds=max(0, settings.drift_changelog_margin_seconds))
    since_value = aware_since.isoformat()

    # Prove that retention reaches the requested point before treating an empty
    # filtered result as authoritative.
    try:
        response = session.get(url, params={"limit": "1", "ordering": "time"}, timeout=30)
        if response.status_code in (400, 403, 404):
            return None
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results:
            return None
        oldest_value = results[0].get("time") if isinstance(results[0], dict) else None
        if not isinstance(oldest_value, str):
            return None
        oldest = datetime.fromisoformat(oldest_value.replace("Z", "+00:00"))
        if as_utc_aware(oldest) > aware_since:
            return None
    except (requests.RequestException, TypeError, ValueError):
        return None

    def check(object_type: str) -> bool | None:
        return _change_count(session, url, [
            ("time_after", since_value), ("limit", "1"),
            ("changed_object_type", object_type),
        ])

    unknown = False
    object_types = iter(DRIFT_CHANGE_TYPES)
    while batch := list(item for _, item in zip(range(4), object_types)):
        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            results = list(executor.map(check, batch))
        if any(result is True for result in results):
            return True
        if any(result is None for result in results):
            unknown = True
    return None if unknown else False


def push_device_type(
    base_url: str,
    token: str,
    verify_ssl: bool,
    device_type: dict[str, Any],
    overwrite: bool = False,
    images: dict[str, BinaryRepoFile] | None = None,
    image_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """
    Create (or update) a device-type and its component templates on a
    NetBox instance. `device_type` is expected in the hyphenated
    devicetype-library YAML shape (as produced by DeviceType.to_yaml_dict()).
    """
    # Repository files use NetBox's cf_<name> bulk-import columns, whereas
    # its REST API expects one nested custom_fields object.
    device_type = DeviceType(**device_type).to_internal_dict()
    nb = get_client(base_url, token, verify_ssl)

    manufacturer_name = device_type["manufacturer"]
    manufacturer = nb.dcim.manufacturers.get(name=manufacturer_name)
    if manufacturer is None:
        manufacturer = nb.dcim.manufacturers.create(
            name=manufacturer_name, slug=_slugify(manufacturer_name)
        )

    base_fields = {
        k: v
        for k, v in device_type.items()
        if k not in COMPONENT_ENDPOINTS and k not in ("manufacturer", "front_image", "rear_image")
    }
    base_fields["manufacturer"] = manufacturer.id

    existing = nb.dcim.device_types.get(
        manufacturer_id=manufacturer.id, slug=device_type["slug"]
    )

    if existing and not overwrite:
        return {
            "status": "error",
            "detail": (
                f"Device type '{device_type['slug']}' already exists for "
                f"manufacturer '{manufacturer_name}'. Enable overwrite to update it."
            ),
        }

    if existing:
        existing.update(base_fields)
        dt = existing
    else:
        dt = nb.dcim.device_types.create(base_fields)

    # Push component templates. On overwrite, this only adds/updates by name;
    # it does not delete templates removed from the draft (safer default).
    component_summary = {}
    for key, endpoint_name in COMPONENT_ENDPOINTS.items():
        components = device_type.get(key, [])
        if not components:
            continue
        endpoint = getattr(nb.dcim, endpoint_name.replace("-", "_"))
        summary = {"created": 0, "updated": 0, "failed": []}
        component_summary[key] = summary
        existing_components = {item.name: item for item in endpoint.filter(device_type_id=dt.id)}
        creates = []
        updates = []
        for component in components:
            comp_fields = dict(component)
            comp_fields["device_type"] = dt.id
            found = existing_components.get(comp_fields.get("name"))
            if found:
                updates.append({"id": found.id, **comp_fields})
            else:
                creates.append(comp_fields)
        _bulk_component_write(endpoint, creates, "created", summary)
        _bulk_component_write(endpoint, updates, "updated", summary)

    warnings = list(image_warnings or [])
    for side, image in (images or {}).items():
        try:
            upload_device_type_image(base_url, token, verify_ssl, dt.id, side, image)
        except Exception as exc:  # Image transfer is deliberately non-fatal to the device-type push.
            warnings.append(f"Could not upload {side} image: {exc}")

    failed_components = sum(len(item["failed"]) for item in component_summary.values())
    detail = f"Pushed as device type id {dt.id}; components={json.dumps(component_summary, separators=(',', ':'))}"
    if warnings:
        detail += "; image warning(s): " + " | ".join(warnings)
    return {"status": "error" if failed_components else "success", "detail": detail}


def _bulk_component_write(endpoint, payloads: list[dict[str, Any]], counter: str,
                          summary: dict[str, Any], batch_size: int = 100) -> None:
    """Use NetBox bulk writes, falling back per object when a batch is rejected."""
    for start in range(0, len(payloads), batch_size):
        batch = payloads[start:start + batch_size]
        if not batch:
            continue
        try:
            if counter == "created":
                endpoint.create(batch)
            else:
                endpoint.update(batch)
            summary[counter] += len(batch)
            continue
        except Exception:
            pass
        for fields in batch:
            try:
                if counter == "created":
                    endpoint.create(fields)
                else:
                    item = endpoint.get(fields["id"])
                    if item is None:
                        raise RuntimeError(f"Component id {fields['id']} disappeared during update")
                    item.update({key: value for key, value in fields.items() if key != "id"})
                summary[counter] += 1
            except Exception as exc:
                summary["failed"].append({"name": fields.get("name"), "error": str(exc)})


def upload_device_type_image(
    base_url: str, token: str, verify_ssl: bool, device_type_id: int, side: str, image: BinaryRepoFile,
) -> None:
    """Upload one DeviceType ImageField using NetBox's multipart PATCH contract."""
    if side not in ("front", "rear"):
        raise ValueError(f"Unsupported elevation image side: {side}")
    extension = image.path.rsplit(".", 1)[-1] if "." in image.path else ""
    validate_image_bytes(image.content, extension)
    field = f"{side}_image"
    response = get_session(base_url, token, verify_ssl).patch(
        f"{base_url.rstrip('/')}/api/dcim/device-types/{device_type_id}/",
        files={field: (image.path.rsplit("/", 1)[-1], image.content, image.content_type)},
        timeout=30,
    )
    response.raise_for_status()


_MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _download_image(url: str, base_url: str, token: str, verify_ssl: bool):
    resolved = urljoin(base_url.rstrip("/") + "/", url)
    expected = urlparse(base_url)
    actual = urlparse(resolved)
    if (actual.scheme.lower(), actual.hostname, actual.port) != (
        expected.scheme.lower(), expected.hostname, expected.port
    ):
        raise ValueError("Image URL must use the configured NetBox instance host.")
    response = get_session(base_url, token, verify_ssl).get(
        resolved, timeout=30,
        stream=True, allow_redirects=False,
    )
    if 300 <= response.status_code < 400:
        raise ValueError("NetBox image redirects are not allowed.")
    response.raise_for_status()
    content_length = response.headers.get("Content-Length")
    if content_length and int(content_length) > _MAX_IMAGE_BYTES:
        raise ValueError("NetBox image exceeds the 10 MiB limit.")
    chunks = []
    size = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        size += len(chunk)
        if size > _MAX_IMAGE_BYTES:
            raise ValueError("NetBox image exceeds the 10 MiB limit.")
        chunks.append(chunk)
    return resolved, response, b"".join(chunks)


def get_image_bytes(url: str, token: str, verify_ssl: bool, base_url: str) -> bytes:
    """Download a NetBox-hosted image for content drift comparison."""
    return _download_image(url, base_url, token, verify_ssl)[2]


def get_image_file(url: str, token: str, verify_ssl: bool, base_url: str) -> BinaryRepoFile:
    """Download an image from NetBox, retaining its filename and media type."""
    resolved, response, content = _download_image(url, base_url, token, verify_ssl)
    name = urlparse(resolved).path.rsplit("/", 1)[-1] or "image.bin"
    content_type = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0]
    return BinaryRepoFile(path=name, content=content, content_type=content_type)


def _slugify(value: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in value).strip("-")
