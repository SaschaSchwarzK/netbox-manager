import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

import requests

from app.services.netbox_client import _choice_value, get_client, require_custom_fields_version

SCOPE_SAMPLE_SIZE = 10
BACKUP_MAX_RECORDS = 100_000
BACKUP_MAX_BYTES = 25 * 1024 * 1024


class ScopeInspectionError(RuntimeError):
    pass


def _content_type_string(ct) -> str | None:
    """Nested content-type references (related_object_type) come back as objects with app_label/model, not a plain string."""
    if ct is None:
        return None
    app_label = getattr(ct, "app_label", None)
    model = getattr(ct, "model", None)
    if app_label and model:
        return f"{app_label}.{model}"
    return str(ct)


def _object_type_strings(values) -> list[str]:
    return [value for item in (values or []) if (value := _content_type_string(item))]


def _record_dict(record) -> dict[str, Any]:
    if hasattr(record, "serialize"):
        return record.serialize()
    return dict(record) if isinstance(record, dict) else dict(vars(record))


def _api_session(token: str, verify_ssl: bool) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Authorization": f"Token {token}", "Accept": "application/json"})
    session.verify = verify_ssl
    return session


def _model_spellings(model: str) -> set[str]:
    model = re.sub(r"[^a-z0-9]", "", model.lower())
    return {model, model + "s", model + "es", model[:-1] + "ies" if model.endswith("y") else ""}


def _resolve_list_url(session: requests.Session, base_url: str, object_type: str) -> str:
    app_label, dot, model = object_type.partition(".")
    if not dot or not app_label or not model:
        raise ScopeInspectionError(f"Invalid object type {object_type!r}.")
    response = session.get(f"{base_url.rstrip('/')}/api/{app_label}/", timeout=20)
    response.raise_for_status()
    roots = response.json()
    spellings = _model_spellings(model)
    matches = [url for name, url in roots.items() if re.sub(r"[^a-z0-9]", "", name.lower()) in spellings]
    if len(matches) != 1:
        raise ScopeInspectionError(
            f"Could not resolve a unique REST endpoint for {object_type}; found {len(matches)} candidates."
        )
    return matches[0]


def _page_records(session: requests.Session, url: str, params: dict[str, Any] | None = None):
    next_url, first_params = url, params
    while next_url:
        response = session.get(next_url, params=first_params, timeout=30)
        response.raise_for_status()
        data = response.json()
        for record in data.get("results", []):
            yield record
        next_url, first_params = data.get("next"), None


def _inspect_removed_type(
    session: requests.Session, base_url: str, object_type: str, field_name: str, default: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    list_url = _resolve_list_url(session, base_url, object_type)
    raw_response = session.get(list_url, params={"limit": 1}, timeout=20)
    raw_response.raise_for_status()
    raw_count = int(raw_response.json().get("count", 0))
    params = {"limit": 200, "fields": "id,display,url,custom_fields", f"cf_{field_name}__empty": "false"}
    counting_method = "cf_empty_filter"
    try:
        records = list(_page_records(session, list_url, params))
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code not in (400, 404):
            raise
        counting_method = "client_side_pagination"
        records = list(_page_records(session, list_url, {"limit": 200, "fields": "id,display,url,custom_fields"}))

    affected, samples = [], []
    for record in records:
        value = (record.get("custom_fields") or {}).get(field_name)
        if value is None or value == default:
            continue
        entry = {
            "object_type": object_type,
            "record_id": record.get("id"),
            "url": record.get("url"),
            "display": record.get("display") or str(record.get("id")),
            "affected_custom_fields": [{"name": field_name, "value": value}],
        }
        affected.append(entry)
        if len(samples) < SCOPE_SAMPLE_SIZE:
            samples.append({
                "id": entry["record_id"], "display": entry["display"], "url": entry["url"], "value": value,
            })
    return {
        "object_type": object_type, "raw_count": raw_count, "meaningful_count": len(affected),
        "sample": samples, "counting_method": counting_method,
    }, affected


def _records_sha256(records: list[dict[str, Any]]) -> str:
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def preview_scope_reductions(
    base_url: str, token: str, verify_ssl: bool, template: dict, *, instance: dict[str, Any],
    actor: dict[str, Any], include_backup: bool = True,
) -> dict[str, Any]:
    """Inspect destructive object-type removals without changing NetBox."""
    version = require_custom_fields_version(base_url, token, verify_ssl)
    nb = get_client(base_url, token, verify_ssl)
    session = _api_session(token, verify_ssl)
    reductions, backup_records = [], []
    snapshots = []
    for proposed in template.get("custom_fields", []):
        current = nb.extras.custom_fields.get(name=proposed["name"])
        if not current:
            continue
        current_types = set(_object_type_strings(getattr(current, "object_types", None)))
        proposed_types = set(proposed.get("object_types", []))
        removed = sorted(current_types - proposed_types)
        if not removed:
            continue
        inspections = []
        for object_type in removed:
            inspected, records = _inspect_removed_type(
                session, base_url, object_type, proposed["name"], getattr(current, "default", None)
            )
            inspections.append(inspected)
            backup_records.extend(records)
        snapshot = _record_dict(current)
        snapshots.append({
            "name": proposed["name"], "current_object_types": sorted(current_types),
            "proposed_object_types": sorted(proposed_types), "definition": snapshot,
        })
        reductions.append({
            "field_name": proposed["name"], "current_object_types": sorted(current_types),
            "proposed_object_types": sorted(proposed_types), "removed_object_types": removed,
            "object_types": inspections,
        })

    confirmation_text = ",".join(sorted(item["field_name"] for item in reductions))
    result = {
        "netbox_version": version, "reductions": reductions, "confirmation_text": confirmation_text,
        "backup_possible": True, "backup": None,
    }
    if reductions and include_backup:
        if len(backup_records) > BACKUP_MAX_RECORDS:
            raise ScopeInspectionError(f"Backup exceeds the {BACKUP_MAX_RECORDS} record safety limit.")
        backup = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "netbox_version": version,
            "instance": instance,
            "actor": actor,
            "custom_field_change": {"fields": snapshots},
            "records": backup_records,
            "sha256": _records_sha256(backup_records),
        }
        if len(json.dumps(backup, ensure_ascii=False).encode()) > BACKUP_MAX_BYTES:
            raise ScopeInspectionError(f"Backup exceeds the {BACKUP_MAX_BYTES // (1024 * 1024)} MiB safety limit.")
        result["backup"] = backup
    return result


def scope_preview_fingerprint(preview: dict[str, Any]) -> str:
    bound = {
        "reductions": [{
            "field_name": item["field_name"],
            "removed_object_types": item["removed_object_types"],
            "counts": [{"object_type": row["object_type"], "raw_count": row["raw_count"],
                        "meaningful_count": row["meaningful_count"]} for row in item["object_types"]],
        } for item in preview["reductions"]],
    }
    return hashlib.sha256(json.dumps(bound, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def restore_custom_fields_backup(
    base_url: str, token: str, verify_ssl: bool, backup: dict[str, Any], *, dry_run: bool,
) -> dict[str, Any]:
    version = require_custom_fields_version(base_url, token, verify_ssl)
    records = backup.get("records")
    if not isinstance(records, list) or backup.get("sha256") != _records_sha256(records):
        raise ScopeInspectionError("Backup record hash is invalid.")
    nb = get_client(base_url, token, verify_ssl)
    results = []
    if not dry_run:
        for field in backup.get("custom_field_change", {}).get("fields", []):
            current = nb.extras.custom_fields.get(name=field["name"])
            if not current:
                raise ScopeInspectionError(f"Custom field {field['name']!r} no longer exists.")
            merged = sorted(set(_object_type_strings(getattr(current, "object_types", None))) | set(field["current_object_types"]))
            current.update({"object_types": merged})
    session = _api_session(token, verify_ssl)
    endpoint_cache: dict[str, str] = {}
    for item in records:
        object_type, record_id = item.get("object_type"), item.get("record_id")
        try:
            list_url = endpoint_cache.get(object_type)
            if not list_url:
                list_url = _resolve_list_url(session, base_url, object_type)
                endpoint_cache[object_type] = list_url
            record_url = f"{list_url.rstrip('/')}/{record_id}/"
            response = session.get(record_url, params={"fields": "id,custom_fields"}, timeout=20)
            if response.status_code == 404:
                results.append({"object_type": object_type, "record_id": record_id, "status": "skipped", "detail": "Record no longer exists."})
                continue
            response.raise_for_status()
            values = dict(response.json().get("custom_fields") or {})
            for affected in item.get("affected_custom_fields", []):
                values[affected["name"]] = affected.get("value")
            if not dry_run:
                patched = session.patch(record_url, json={"custom_fields": values}, timeout=20)
                patched.raise_for_status()
            results.append({"object_type": object_type, "record_id": record_id, "status": "would_restore" if dry_run else "restored"})
        except Exception as exc:
            results.append({"object_type": object_type, "record_id": record_id, "status": "error", "detail": str(exc)})
    return {"status": "dry_run" if dry_run else "complete", "netbox_version": version, "results": results}


def get_existing_custom_fields(base_url: str, token: str, verify_ssl: bool) -> dict[str, Any]:
    """Fetch the current custom fields and choice sets from a NetBox instance, reshaped to match the template YAML."""
    require_custom_fields_version(base_url, token, verify_ssl)
    nb = get_client(base_url, token, verify_ssl)

    choice_sets = []
    for cs in nb.extras.custom_field_choice_sets.all():
        choice_sets.append({
            "name": cs.name,
            "description": getattr(cs, "description", None) or None,
            "extra_choices": [list(c) for c in (getattr(cs, "extra_choices", None) or [])],
            "base_choices": _choice_value(getattr(cs, "base_choices", None)),
            "order_alphabetically": bool(getattr(cs, "order_alphabetically", False)),
        })

    fields = []
    for cf in nb.extras.custom_fields.all():
        choice_set = getattr(cf, "choice_set", None)
        fields.append({
            "name": cf.name,
            "label": getattr(cf, "label", None) or None,
            "type": _choice_value(getattr(cf, "type", None)) or "text",
            "object_types": _object_type_strings(getattr(cf, "object_types", None)),
            "description": getattr(cf, "description", None) or None,
            "required": bool(getattr(cf, "required", False)),
            "unique": bool(getattr(cf, "unique", False)),
            "search_weight": getattr(cf, "search_weight", None),
            "default": getattr(cf, "default", None),
            "choice_set": str(choice_set) if choice_set else None,
            "related_object_type": _content_type_string(getattr(cf, "related_object_type", None)),
            "related_object_filter": getattr(cf, "related_object_filter", None),
            "filter_logic": _choice_value(getattr(cf, "filter_logic", None)),
            "weight": getattr(cf, "weight", None),
            "group_name": getattr(cf, "group_name", None) or None,
            "ui_visible": _choice_value(getattr(cf, "ui_visible", None)),
            "ui_editable": _choice_value(getattr(cf, "ui_editable", None)),
            "is_cloneable": bool(getattr(cf, "is_cloneable", False)),
            "validation_minimum": getattr(cf, "validation_minimum", None),
            "validation_maximum": getattr(cf, "validation_maximum", None),
            "validation_regex": getattr(cf, "validation_regex", None) or None,
            "comments": getattr(cf, "comments", None) or None,
        })

    return {"custom_fields": fields, "custom_field_choice_sets": choice_sets}


def push_custom_fields(
    base_url: str, token: str, verify_ssl: bool, template: dict, overwrite: bool = False,
    scope_reductions_confirmed: bool = False,
) -> dict:
    """
    Creates missing custom fields/choice sets on the instance. Existing ones
    (matched by name) are only updated if `overwrite` is set. Choice sets are
    pushed first since custom fields can reference them by name.
    """
    require_custom_fields_version(base_url, token, verify_ssl)
    nb = get_client(base_url, token, verify_ssl)
    created, updated, skipped = [], [], []

    # Preflight before even updating choice sets: direct service callers must
    # not be able to bypass the router's destructive-change confirmation.
    if overwrite and not scope_reductions_confirmed:
        for proposed in template.get("custom_fields", []):
            existing = nb.extras.custom_fields.get(name=proposed["name"])
            if not existing:
                continue
            current_types = set(_object_type_strings(getattr(existing, "object_types", None)))
            proposed_types = set(proposed.get("object_types", []))
            removed = sorted(current_types - proposed_types)
            if removed:
                raise ScopeInspectionError(
                    f"Refusing to reduce scope for custom field {proposed['name']!r} without validated confirmation: {', '.join(removed)}"
                )

    for cs in template.get("custom_field_choice_sets", []):
        existing = nb.extras.custom_field_choice_sets.get(name=cs["name"])
        if existing:
            if overwrite:
                existing.update(cs)
                updated.append(f"choice-set:{cs['name']}")
            else:
                skipped.append(f"choice-set:{cs['name']}")
        else:
            nb.extras.custom_field_choice_sets.create(cs)
            created.append(f"choice-set:{cs['name']}")

    for cf in template.get("custom_fields", []):
        payload = dict(cf)
        if payload.get("choice_set"):
            cs_obj = nb.extras.custom_field_choice_sets.get(name=payload["choice_set"])
            if cs_obj:
                payload["choice_set"] = cs_obj.id
            else:
                payload.pop("choice_set", None)  # referenced choice set doesn't exist here; drop rather than fail the whole push

        if payload.get("related_object_type"):
            app_label, _, model = payload["related_object_type"].partition(".")
            ct_obj = nb.core.object_types.get(app_label=app_label, model=model) if app_label and model else None
            if ct_obj:
                payload["related_object_type"] = ct_obj.id
            else:
                payload.pop("related_object_type", None)  # unknown/unavailable content type on this instance; drop rather than fail

        existing = nb.extras.custom_fields.get(name=cf["name"])
        if existing:
            if overwrite:
                existing.update(payload)
                updated.append(f"field:{cf['name']}")
            else:
                skipped.append(f"field:{cf['name']}")
        else:
            nb.extras.custom_fields.create(payload)
            created.append(f"field:{cf['name']}")

    detail = f"Created {len(created)}, updated {len(updated)}, skipped {len(skipped)} (already exist, overwrite off)."
    return {"status": "success", "detail": detail}
