"""Registry-driven reference-data synchronization for NetBox 4.6."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, create_model

from app.services.netbox_client import get_session

SENSITIVE_HEADER = re.compile(r"authorization|token|key|secret|cookie", re.I)
SYNC_FIELDS = {"data_source", "data_file", "data_path", "auto_sync_enabled", "data_synced"}


@dataclass(frozen=True)
class Kind:
    label: str
    endpoint: str
    fields: tuple[str, ...]
    relations: dict[str, tuple[str, bool]] = None  # field -> (kind, required)
    secret_fields: tuple[str, ...] = ()


REGISTRY: dict[str, Kind] = {
    "tags": Kind("Tags", "extras/tags", ("name", "slug", "color", "description", "object_types")),
    "manufacturers": Kind("Manufacturers", "dcim/manufacturers", ("name", "slug", "description")),
    "config-templates": Kind("Config templates", "extras/config-templates", ("name", "description", "template_code", "environment_params", "mime_type", "file_name")),
    "export-templates": Kind("Export templates", "extras/export-templates", ("name", "object_types", "description", "template_code", "mime_type", "file_extension", "as_attachment")),
    "device-roles": Kind("Device roles", "dcim/device-roles", ("name", "slug", "color", "vm_role", "description", "config_template"), {"config_template": ("config-templates", False)}),
    "platforms": Kind("Platforms", "dcim/platforms", ("name", "slug", "manufacturer", "napalm_driver", "napalm_args", "description", "config_template"), {"manufacturer": ("manufacturers", False), "config_template": ("config-templates", False)}),
    "webhooks": Kind("Webhooks", "extras/webhooks", ("name", "payload_url", "http_method", "http_content_type", "additional_headers", "body_template", "ssl_verification"), secret_fields=("secret",)),
    "event-rules": Kind("Event rules", "extras/event-rules", ("name", "object_types", "event_types", "enabled", "conditions", "action_type", "action_object", "description"), {"action_object": ("webhooks", False)}),
    "config-contexts": Kind("Config contexts", "extras/config-contexts", ("name", "weight", "description", "is_active", "data", "sites", "roles", "platforms", "device_types", "tags", "tenants"), {"sites": ("sites", False), "roles": ("device-roles", False), "platforms": ("platforms", False), "device_types": ("device-types", False), "tags": ("tags", False), "tenants": ("tenants", False)}),
}
PUSH_ORDER = tuple(REGISTRY)
EXTERNAL_ENDPOINTS = {"sites": "dcim/sites", "device-types": "dcim/device-types", "tenants": "tenancy/tenants"}


class StrictItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)


def _model_for(kind: str) -> type[BaseModel]:
    definition = REGISTRY[kind]
    optional = {name: (Any | None, None) for name in definition.fields if name != "name"}
    return create_model(f"{kind.title().replace('-', '')}Item", __base__=StrictItem, **optional)


MODELS = {kind: _model_for(kind) for kind in REGISTRY}


class ReferenceDataFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[dict[str, Any]] = []


def path_for(root: str, kind: str) -> str:
    if kind not in REGISTRY:
        raise ValueError("Unknown reference-data kind.")
    if root.startswith("/"):
        raise ValueError("Reference-data path must be a safe repository directory.")
    root = root.strip("/")
    if not root or root in {".", ".."} or ".." in root.split("/") or "\\" in root:
        raise ValueError("Reference-data path must be a safe repository directory.")
    return f"{root}/{kind}.yml"


def _validate_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query:
        raise ValueError("payload_url must not contain credentials or a query string.")


def redact_headers(value: str | None) -> tuple[str | None, list[str]]:
    warnings, safe = [], []
    for line in (value or "").splitlines():
        name, sep, _ = line.partition(":")
        if sep and SENSITIVE_HEADER.search(name):
            safe.append(f"{name}: <redacted>")
            warnings.append(f"additional_headers: redacted sensitive header '{name.strip()}'")
        else:
            safe.append(line)
    return ("\n".join(safe) if value is not None else None), warnings


def validate_file(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    wrapped = ReferenceDataFile.model_validate(payload)
    items = []
    for raw in wrapped.items:
        forbidden = (set(raw) & SYNC_FIELDS) | (set(raw) & set(REGISTRY[kind].secret_fields))
        if forbidden:
            raise ValueError(f"Forbidden field(s): {', '.join(sorted(forbidden))}")
        item = MODELS[kind].model_validate(raw).model_dump(exclude_none=True)
        if kind == "webhooks":
            _validate_url(item.get("payload_url", ""))
            headers, _ = redact_headers(item.get("additional_headers"))
            item["additional_headers"] = headers
        items.append(item)
    return {"items": items}


def schema() -> dict[str, Any]:
    return {"push_order": list(PUSH_ORDER), "kinds": {name: {
        "label": item.label, "api_path": item.endpoint, "identity_field": "name",
        "fields": list(item.fields), "relations": item.relations or {},
        "secret_fields": list(item.secret_fields), "file": f"{name}.yml",
        "json_schema": MODELS[name].model_json_schema(),
    } for name, item in REGISTRY.items()}}


def _records(session, base_url: str, endpoint: str) -> list[dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/api/{endpoint}/"
    records = []
    while url:
        response = session.get(url, params={"limit": 200} if not records else None, timeout=30)
        response.raise_for_status()
        body = response.json()
        records.extend(body.get("results", body if isinstance(body, list) else []))
        url = body.get("next") if isinstance(body, dict) else None
    return records


def _name(value: Any, *, slug: bool = False) -> Any:
    if isinstance(value, dict):
        return value.get("slug" if slug else "name") or value.get("display")
    return value


def fetch_kind(base_url: str, token: str, verify_ssl: bool, kind: str) -> dict[str, Any]:
    definition = REGISTRY[kind]
    session = get_session(base_url, token, verify_ssl)
    items = []
    for record in _records(session, base_url, definition.endpoint):
        if any(record.get(field) for field in SYNC_FIELDS):
            continue
        item = {field: record.get(field) for field in definition.fields if field in record}
        for field, (related_kind, _) in (definition.relations or {}).items():
            value = item.get(field)
            if isinstance(value, list):
                item[field] = [_name(v, slug=related_kind == "device-types") for v in value]
            elif value is not None:
                item[field] = _name(value, slug=related_kind == "device-types")
        if kind == "tags" and isinstance(item.get("object_types"), list):
            item["object_types"] = [f"{v.get('app_label')}.{v.get('model')}" if isinstance(v, dict) else v for v in item["object_types"]]
        if kind == "webhooks":
            item.pop("secret", None)
            item["additional_headers"], _ = redact_headers(item.get("additional_headers"))
        items.append(MODELS[kind].model_validate(item).model_dump(exclude_none=True))
    return {"items": items}


def diff_kind(expected: dict, actual: dict) -> dict[str, Any]:
    left = {x["name"]: x for x in expected.get("items", [])}
    right = {x["name"]: x for x in actual.get("items", [])}
    changed = []
    for name in sorted(left.keys() & right.keys()):
        fields = [{"field": key, "source": left[name].get(key), "existing": right[name].get(key)}
                  for key in sorted(set(left[name]) | set(right[name])) if left[name].get(key) != right[name].get(key)]
        if fields:
            changed.append({"name": name, "field_changes": fields})
    return {"missing_on_instance": sorted(left.keys() - right.keys()), "extra_on_instance": sorted(right.keys() - left.keys()), "changed": changed}


def _resolve(session, base_url: str, related_kind: str, value: str) -> int | None:
    endpoint = REGISTRY[related_kind].endpoint if related_kind in REGISTRY else EXTERNAL_ENDPOINTS[related_kind]
    param = "slug" if related_kind == "device-types" else "name"
    response = session.get(f"{base_url.rstrip('/')}/api/{endpoint}/", params={param: value, "limit": 2}, timeout=20)
    response.raise_for_status()
    rows = response.json().get("results", [])
    return rows[0]["id"] if len(rows) == 1 else None


def _resolve_object_types(session, base_url: str, values: list[str]) -> tuple[list[int], list[str]]:
    ids, missing = [], []
    for value in values:
        app_label, separator, model = value.partition(".")
        if not separator:
            missing.append(value)
            continue
        response = session.get(f"{base_url.rstrip('/')}/api/core/object-types/",
                               params={"app_label": app_label, "model": model, "limit": 2}, timeout=20)
        response.raise_for_status()
        rows = response.json().get("results", [])
        (ids if len(rows) == 1 else missing).append(rows[0]["id"] if len(rows) == 1 else value)
    return ids, missing


def push_all(base_url: str, token: str, verify_ssl: bool, files: dict[str, dict], *, overwrite: bool, enable_event_rules: bool = False) -> dict[str, Any]:
    session = get_session(base_url, token, verify_ssl)
    result = {"created": [], "updated": [], "skipped": [], "warnings": [], "failed": []}
    for kind in PUSH_ORDER:
        definition = REGISTRY[kind]
        for source in validate_file(kind, files.get(kind, {"items": []}))["items"]:
            payload = dict(source)
            if kind == "webhooks":
                headers = []
                for line in (payload.get("additional_headers") or "").splitlines():
                    if "<redacted>" in line:
                        result["warnings"].append(f"webhooks:{source['name']}: skipped redacted header '{line.partition(':')[0]}'")
                    else:
                        headers.append(line)
                payload["additional_headers"] = "\n".join(headers)
            if isinstance(payload.get("object_types"), list):
                payload["object_types"], missing_types = _resolve_object_types(session, base_url, payload["object_types"])
                for value in missing_types:
                    result["warnings"].append(f"{kind}:{source['name']}: object type '{value}' was not found")
            missing_required = False
            for field, (related_kind, required) in (definition.relations or {}).items():
                value = payload.get(field)
                if value is None:
                    continue
                values = value if isinstance(value, list) else [value]
                ids = [resolved for name in values if (resolved := _resolve(session, base_url, related_kind, name))]
                if len(ids) != len(values):
                    result["warnings"].append(f"{kind}:{source['name']}: referenced object for '{field}' was not found")
                    if required:
                        missing_required = True
                        break
                payload[field] = ids if isinstance(value, list) else (ids[0] if ids else None)
            if missing_required:
                result["failed"].append(f"{kind}:{source['name']}")
                continue
            url = f"{base_url.rstrip('/')}/api/{definition.endpoint}/"
            lookup = session.get(url, params={"name": source["name"], "limit": 2}, timeout=20)
            lookup.raise_for_status()
            rows = lookup.json().get("results", [])
            if rows and not overwrite:
                result["skipped"].append(f"{kind}:{source['name']}")
                continue
            if kind == "event-rules" and not rows and not enable_event_rules:
                payload["enabled"] = False
            if rows:
                response = session.patch(f"{url}{rows[0]['id']}/", json=payload, timeout=30)
                bucket = "updated"
            else:
                response = session.post(url, json=payload, timeout=30)
                bucket = "created"
            if response.ok:
                result[bucket].append(f"{kind}:{source['name']}")
            else:
                result["failed"].append(f"{kind}:{source['name']}")
                result["warnings"].append(f"{kind}:{source['name']}: NetBox returned HTTP {response.status_code}")
    result["status"] = "error" if result["failed"] else "success"
    return result
