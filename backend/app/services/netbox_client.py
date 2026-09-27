import time
from typing import Any

import pynetbox
import requests

from app.devicetype_schema import COMPONENT_ENDPOINTS, COMPONENT_EXTRA_FIELDS


def get_client(base_url: str, token: str, verify_ssl: bool) -> pynetbox.api:
    client = pynetbox.api(base_url, token=token)
    client.http_session.verify = verify_ssl
    return client


def test_connection(base_url: str, token: str, verify_ssl: bool) -> dict[str, Any]:
    """Hit NetBox's /api/status/ endpoint to validate URL + token."""
    url = base_url.rstrip("/") + "/api/status/"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Token {token}"},
            verify=verify_ssl,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {"ok": True, "netbox_version": data.get("netbox-version"), "detail": None}
        return {
            "ok": False,
            "netbox_version": None,
            "detail": f"HTTP {resp.status_code}: {resp.text[:200]}",
        }
    except requests.RequestException as exc:
        return {"ok": False, "netbox_version": None, "detail": str(exc)}


def search_instance(base_url: str, token: str, verify_ssl: bool, query: str) -> dict[str, Any]:
    """
    Search devices, virtual machines, virtual device contexts (VDCs), IP
    addresses, prefixes, and MAC addresses on one NetBox instance, using
    NetBox's built-in `q` quick-search filter where available (name, serial,
    asset tag, address, etc.) and an exact `mac_address` filter for MACs,
    since NetBox's interface filter doesn't support partial MAC matching.
    """
    nb = get_client(base_url, token, verify_ssl)
    web_base = base_url.rstrip("/")

    devices = []
    for d in nb.dcim.devices.filter(q=query):
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
    for v in nb.virtualization.virtual_machines.filter(q=query):
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
    for c in nb.dcim.virtual_device_contexts.filter(q=query):
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
    for ip in nb.ipam.ip_addresses.filter(q=query):
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
    for p in nb.ipam.prefixes.filter(q=query):
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
        for iface in nb.dcim.interfaces.filter(mac_address=query):
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
        for iface in nb.virtualization.interfaces.filter(mac_address=query):
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
        for m in nb.dcim.mac_addresses.filter(mac_address=query):
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

    return {
        "devices": devices, "virtual_machines": vms, "virtual_device_contexts": vdcs,
        "ip_addresses": ip_addresses, "prefixes": prefixes, "mac_addresses": mac_addresses,
    }


def get_health(base_url: str, token: str, verify_ssl: bool) -> dict[str, Any]:
    """Reachability + NetBox/plugin versions, from the /api/status/ endpoint."""
    url = base_url.rstrip("/") + "/api/status/"
    start = time.monotonic()
    try:
        resp = requests.get(url, headers={"Authorization": f"Token {token}"}, verify=verify_ssl, timeout=10)
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
        resp = requests.get(url, headers={"Authorization": f"Token {token}"}, verify=verify_ssl, timeout=10)
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
            "model": dt.model,
            "slug": dt.slug,
            "u_height": float(dt.u_height) if dt.u_height is not None else None,
        })
    return results


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

    result: dict[str, Any] = {
        "manufacturer": manufacturer_name,
        "model": dt.model,
        "slug": dt.slug,
        "part_number": dt.part_number or None,
        "u_height": float(dt.u_height) if dt.u_height is not None else None,
        "is_full_depth": dt.is_full_depth,
        "subdevice_role": _choice_value(getattr(dt, "subdevice_role", None)),
        "weight": float(dt.weight) if dt.weight is not None else None,
        "weight_unit": _choice_value(getattr(dt, "weight_unit", None)),
        "comments": dt.comments or None,
        "custom_fields": dict(getattr(dt, "custom_fields", None) or {}),
    }

    for key, endpoint_name in COMPONENT_ENDPOINTS.items():
        endpoint = getattr(nb.dcim, endpoint_name.replace("-", "_"))
        items = []
        for item in endpoint.filter(device_type_id=dt.id):
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
                    value = str(value)  # nested object reference -> its name, matching the source YAML
                elif hasattr(value, "value"):
                    value = value.value  # choice field (e.g. poe_mode, poe_type) -> raw stored string
                if value not in (None, ""):
                    entry[extra_field] = value
            items.append(entry)
        result[key] = items

    return result


def push_device_type(    base_url: str,
    token: str,
    verify_ssl: bool,
    device_type: dict[str, Any],
    overwrite: bool = False,
) -> dict[str, Any]:
    """
    Create (or update) a device-type and its component templates on a
    NetBox instance. `device_type` is expected in the hyphenated
    devicetype-library YAML shape (as produced by DeviceType.to_yaml_dict()).
    """
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
        if k not in COMPONENT_ENDPOINTS and k != "manufacturer"
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
    for key, endpoint_name in COMPONENT_ENDPOINTS.items():
        components = device_type.get(key, [])
        if not components:
            continue
        endpoint = getattr(nb.dcim, endpoint_name.replace("-", "_"))
        for component in components:
            comp_fields = dict(component)
            comp_fields["device_type"] = dt.id
            found = endpoint.get(device_type_id=dt.id, name=comp_fields.get("name"))
            if found:
                found.update(comp_fields)
            else:
                endpoint.create(comp_fields)

    return {"status": "success", "detail": f"Pushed as device type id {dt.id}"}


def _slugify(value: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in value).strip("-")
