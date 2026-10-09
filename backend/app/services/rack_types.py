"""NetBox REST operations for community-library rack types."""
from typing import Any

from app.rack_type_schema import RackType
from app.services.netbox_client import _choice_value, _slugify, get_client


def list_on_instance(base_url: str, token: str, verify_ssl: bool) -> list[dict]:
    nb = get_client(base_url, token, verify_ssl)
    return [{
        "manufacturer": str(item.manufacturer), "model": str(item.model), "slug": item.slug,
    } for item in nb.dcim.rack_types.all()]


def get_existing(base_url: str, token: str, verify_ssl: bool,
                 manufacturer_name: str, slug: str) -> dict | None:
    nb = get_client(base_url, token, verify_ssl)
    manufacturer = nb.dcim.manufacturers.get(name=manufacturer_name)
    if manufacturer is None:
        return None
    rack_type = nb.dcim.rack_types.get(manufacturer_id=manufacturer.id, slug=slug)
    if rack_type is None:
        return None
    result = {
        "manufacturer": manufacturer_name,
        "model": rack_type.model,
        "slug": rack_type.slug,
    }
    choice_fields = {"form_factor", "outer_unit", "weight_unit"}
    for field in RackType.model_fields:
        if field in result or field == "manufacturer":
            continue
        value = getattr(rack_type, field, None)
        if field in choice_fields:
            value = _choice_value(value)
        if value not in (None, ""):
            result[field] = value
    return result


def push(base_url: str, token: str, verify_ssl: bool,
         payload: dict[str, Any], overwrite: bool = False) -> dict[str, Any]:
    source = RackType(**payload).to_internal_dict()
    nb = get_client(base_url, token, verify_ssl)
    manufacturer = nb.dcim.manufacturers.get(name=source["manufacturer"])
    if manufacturer is None:
        manufacturer = nb.dcim.manufacturers.create(
            name=source["manufacturer"], slug=_slugify(source["manufacturer"])
        )
    existing = nb.dcim.rack_types.get(manufacturer_id=manufacturer.id, slug=source["slug"])
    if existing and not overwrite:
        return {"status": "error", "detail": f"Rack type '{source['slug']}' already exists. Enable overwrite to update it."}
    fields = {key: value for key, value in source.items() if key != "manufacturer"}
    fields["manufacturer"] = manufacturer.id
    if existing:
        existing.update(fields)
        rack_type = existing
    else:
        rack_type = nb.dcim.rack_types.create(fields)
    return {"status": "success", "detail": f"Pushed as rack type id {rack_type.id}"}
