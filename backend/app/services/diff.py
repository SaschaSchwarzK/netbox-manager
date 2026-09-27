"""
Compares a device-type payload from GitHub (the source of truth) against the
current state of that device type on a NetBox instance. Used for both
diff-before-push and scheduled drift detection, so the two features always
agree on what "in sync" means.
"""
from app.devicetype_schema import COMPONENT_ENDPOINTS

BASE_FIELDS = [
    "part_number", "u_height", "is_full_depth", "subdevice_role",
    "airflow", "weight", "weight_unit", "comments", "custom_fields",
]


def field_level_diff(source_item: dict, existing_item: dict) -> list[dict]:
    """
    Compares every attribute of two same-named items (a component template,
    a custom field, a choice set) and returns exactly which fields differ,
    not just that they differ — e.g. {"field": "maximum_draw", "source": 60,
    "existing": 30} rather than a bare "changed" flag on the item.
    """
    keys = (set(source_item.keys()) | set(existing_item.keys())) - {"name"}
    changes = []
    for key in sorted(keys):
        source_value = _normalize(source_item.get(key))
        existing_value = _normalize(existing_item.get(key))
        if source_value != existing_value:
            changes.append({"field": key, "source": source_item.get(key), "existing": existing_item.get(key)})
    return changes


def diff_payloads(source: dict, existing: dict | None) -> dict:
    """
    `source` is the payload as stored in GitHub. `existing` is the same shape
    as produced by netbox_client.get_existing_device_type(), or None if the
    device type doesn't exist on that instance at all.
    """
    if existing is None:
        return {"status": "missing", "base_field_changes": [], "component_changes": {}}

    base_field_changes = []
    for field in BASE_FIELDS:
        source_value = source.get(field)
        existing_value = existing.get(field)
        if _normalize(source_value) != _normalize(existing_value):
            base_field_changes.append({"field": field, "source": source_value, "netbox": existing_value})

    component_changes = {}
    for key in COMPONENT_ENDPOINTS:
        source_items = {c.get("name"): c for c in (source.get(key) or []) if c.get("name")}
        existing_items = {c.get("name"): c for c in (existing.get(key) or []) if c.get("name")}

        added = sorted(set(source_items) - set(existing_items))
        removed = sorted(set(existing_items) - set(source_items))
        changed = []
        for name in sorted(set(source_items) & set(existing_items)):
            field_changes = field_level_diff(source_items[name], existing_items[name])
            if field_changes:
                changed.append({"name": name, "field_changes": field_changes})

        if added or removed or changed:
            component_changes[key] = {"added": added, "removed": removed, "changed": changed}

    status = "in_sync" if not base_field_changes and not component_changes else "drift"
    return {"status": status, "base_field_changes": base_field_changes, "component_changes": component_changes}


def _normalize(value):
    """Loose equality: None/""/0-vs-missing shouldn't all read as 'changed'."""
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items() if v not in (None, "")}
    if value in (None, ""):
        return None
    return value


def diff_named_list(source_items: list[dict], existing_items: list[dict]) -> dict:
    """
    Generic "by name" diff for lists of dicts (custom fields, choice sets):
    what's in `source_items` but missing from `existing_items` (needs pushing),
    what's in `existing_items` but not in `source_items` (exists on the
    instance but isn't in the template), and — for items present in both —
    exactly which fields differ, via field_level_diff().
    """
    source_map = {i.get("name"): i for i in source_items if i.get("name")}
    existing_map = {i.get("name"): i for i in existing_items if i.get("name")}

    missing_on_instance = sorted(set(source_map) - set(existing_map))
    extra_on_instance = sorted(set(existing_map) - set(source_map))
    changed = []
    for name in sorted(set(source_map) & set(existing_map)):
        field_changes = field_level_diff(source_map[name], existing_map[name])
        if field_changes:
            changed.append({"name": name, "field_changes": field_changes})
    return {"missing_on_instance": missing_on_instance, "extra_on_instance": extra_on_instance, "changed": changed}


def diff_custom_fields_template(template: dict, existing: dict | None) -> dict:
    """
    Compares the custom-fields template against what's actually on a NetBox
    instance. `existing` comes from netbox_customfields.get_existing_custom_fields().
    """
    if existing is None:
        return {"status": "error", "custom_fields": None, "custom_field_choice_sets": None}

    cf_diff = diff_named_list(template.get("custom_fields", []), existing.get("custom_fields", []))
    cs_diff = diff_named_list(
        template.get("custom_field_choice_sets", []), existing.get("custom_field_choice_sets", [])
    )
    in_sync = not any(cf_diff.values()) and not any(cs_diff.values())
    return {
        "status": "in_sync" if in_sync else "drift",
        "custom_fields": cf_diff,
        "custom_field_choice_sets": cs_diff,
    }
