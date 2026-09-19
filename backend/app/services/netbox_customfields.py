from typing import Any

from app.services.netbox_client import _choice_value, get_client


def get_existing_custom_fields(base_url: str, token: str, verify_ssl: bool) -> dict[str, Any]:
    """Fetch the current custom fields and choice sets from a NetBox instance, reshaped to match the template YAML."""
    nb = get_client(base_url, token, verify_ssl)

    choice_sets = []
    for cs in nb.extras.custom_field_choice_sets.all():
        choice_sets.append({
            "name": cs.name,
            "description": getattr(cs, "description", None) or None,
            "extra_choices": [list(c) for c in (getattr(cs, "extra_choices", None) or [])],
            "order_alphabetically": bool(getattr(cs, "order_alphabetically", False)),
        })

    fields = []
    for cf in nb.extras.custom_fields.all():
        choice_set = getattr(cf, "choice_set", None)
        fields.append({
            "name": cf.name,
            "label": getattr(cf, "label", None) or None,
            "type": _choice_value(getattr(cf, "type", None)) or "text",
            "content_types": list(getattr(cf, "content_types", None) or []),
            "description": getattr(cf, "description", None) or None,
            "required": bool(getattr(cf, "required", False)),
            "default": getattr(cf, "default", None),
            "choice_set": str(choice_set) if choice_set else None,
            "filter_logic": _choice_value(getattr(cf, "filter_logic", None)),
            "weight": getattr(cf, "weight", None),
            "group_name": getattr(cf, "group_name", None) or None,
            "ui_visible": _choice_value(getattr(cf, "ui_visible", None)),
            "ui_editable": _choice_value(getattr(cf, "ui_editable", None)),
            "is_cloneable": bool(getattr(cf, "is_cloneable", False)),
            "validation_minimum": getattr(cf, "validation_minimum", None),
            "validation_maximum": getattr(cf, "validation_maximum", None),
            "validation_regex": getattr(cf, "validation_regex", None) or None,
        })

    return {"custom_fields": fields, "custom_field_choice_sets": choice_sets}


def push_custom_fields(base_url: str, token: str, verify_ssl: bool, template: dict, overwrite: bool = False) -> dict:
    """
    Creates missing custom fields/choice sets on the instance. Existing ones
    (matched by name) are only updated if `overwrite` is set. Choice sets are
    pushed first since custom fields can reference them by name.
    """
    nb = get_client(base_url, token, verify_ssl)
    created, updated, skipped = [], [], []

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
