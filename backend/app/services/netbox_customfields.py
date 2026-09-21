from typing import Any

from app.services.netbox_client import _choice_value, get_client


def _content_type_string(ct) -> str | None:
    """Nested content-type references (related_object_type) come back as objects with app_label/model, not a plain string."""
    if ct is None:
        return None
    app_label = getattr(ct, "app_label", None)
    model = getattr(ct, "model", None)
    if app_label and model:
        return f"{app_label}.{model}"
    return str(ct)


def get_existing_custom_fields(base_url: str, token: str, verify_ssl: bool) -> dict[str, Any]:
    """Fetch the current custom fields and choice sets from a NetBox instance, reshaped to match the template YAML."""
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
            "content_types": list(getattr(cf, "content_types", None) or []),
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
