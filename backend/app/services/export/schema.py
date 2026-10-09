from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

from app.services.netbox_customfields import _object_type_strings

TYPES = OrderedDict({
    "device": {
        "label": "Devices", "content_type": "dcim.device", "endpoint": ("dcim", "devices"),
        "fixed": (("name", "Name"), ("primary_ip", "Primary IP"), ("serial", "Serial")),
        "optional": (("site", "Site", "site"), ("region", "Region", "site.region"),
                     ("device_type", "Device type", "device_type"),
                     ("manufacturer", "Manufacturer", "device_type.manufacturer"),
                     ("virtual_chassis", "Virtual chassis", "virtual_chassis")),
        "sheet": "Devices", "csv": "devices.csv",
    },
    "virtualdevicecontext": {
        "label": "Virtual Device Contexts", "content_type": "dcim.virtualdevicecontext",
        "endpoint": ("dcim", "virtual_device_contexts"),
        "fixed": (("name", "Name"), ("primary_ip", "Primary IP")),
        "optional": (("primary_device", "Primary device", "device"),),
        "sheet": "VirtualDeviceContexts", "csv": "virtual-device-contexts.csv",
    },
    "virtualmachine": {
        "label": "Virtual Machines", "content_type": "virtualization.virtualmachine",
        "endpoint": ("virtualization", "virtual_machines"),
        "fixed": (("name", "Name"), ("primary_ip", "Primary IP")),
        "optional": (("site", "Site", "site"), ("region", "Region", "site.region"),
                     ("cluster", "Cluster", "cluster")),
        "sheet": "VirtualMachines", "csv": "virtual-machines.csv",
    },
})

_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_lock = threading.Lock()


def _endpoint(client, definition):
    return getattr(getattr(client.nb, definition["endpoint"][0]), definition["endpoint"][1])


def _choice_value(value):
    return value.get("value") if isinstance(value, dict) else getattr(value, "value", value)


def fetch_schema(client) -> dict[str, Any]:
    custom_fields = list(client.paginated(client.nb.extras.custom_fields, limit=1000))
    available = set(TYPES)
    try:
        client.call(lambda: list(client.nb.dcim.virtual_device_contexts.filter(limit=1)))
    except Exception as exc:
        response = getattr(exc, "response", None) or getattr(exc, "req", None)
        if getattr(response, "status_code", None) == 404 or "404" in str(exc):
            available.discard("virtualdevicecontext")
        else:
            raise

    result = []
    for key, definition in TYPES.items():
        if key not in available:
            continue
        relevant = []
        for field in custom_fields:
            object_types = field.get("object_types", field.get("content_types", []))
            if definition["content_type"] not in _object_type_strings(object_types):
                continue
            relevant.append({
                "name": str(field["name"]), "label": str(field.get("label") or field["name"]),
                "type": str(_choice_value(field.get("type")) or "text"),
                "group_name": str(field.get("group_name") or ""), "weight": int(field.get("weight") or 0),
            })
        relevant.sort(key=lambda item: (not bool(item["group_name"]), item["group_name"].lower(), item["weight"], item["label"].lower()))
        result.append({
            "key": key, "label": definition["label"],
            "fixed": [{"key": item[0], "label": item[1]} for item in definition["fixed"]],
            "optional": [{"key": item[0], "label": item[1]} for item in definition["optional"]],
            "custom_fields": relevant,
        })
    return {"object_types": result}


def cached_schema(instance_id: str, client) -> dict[str, Any]:
    now = time.monotonic()
    with _lock:
        cached = _cache.get(instance_id)
        if cached and now - cached[0] < 60:
            _cache.move_to_end(instance_id)
            return cached[1]
    value = fetch_schema(client)
    with _lock:
        _cache[instance_id] = (now, value)
        _cache.move_to_end(instance_id)
        while len(_cache) > 32:
            _cache.popitem(last=False)
    return value


def clear_cache() -> None:
    with _lock:
        _cache.clear()
