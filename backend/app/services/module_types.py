"""NetBox REST operations for community-library module types."""
from __future__ import annotations

from typing import Any

import requests

from app.module_type_schema import MODULE_COMPONENT_ENDPOINTS, MODULE_COMPONENT_EXTRA_FIELDS, ModuleType
from app.services.github_repo import BinaryRepoFile
from app.image_safety import validate_image_bytes
from app.services.netbox_client import _choice_value, get_client, _slugify


def list_on_instance(base_url: str, token: str, verify_ssl: bool) -> list[dict]:
    nb = get_client(base_url, token, verify_ssl)
    return [{
        "manufacturer": str(item.manufacturer), "model": str(item.model),
        "part_number": str(item.part_number) if getattr(item, "part_number", None) else None,
    } for item in nb.dcim.module_types.all()]


def get_existing(
    base_url: str, token: str, verify_ssl: bool, manufacturer_name: str, model: str,
) -> dict | None:
    nb = get_client(base_url, token, verify_ssl)
    manufacturer = nb.dcim.manufacturers.get(name=manufacturer_name)
    if manufacturer is None:
        return None
    module_type = nb.dcim.module_types.get(manufacturer_id=manufacturer.id, model=model)
    if module_type is None:
        return None
    profile = getattr(module_type, "profile", None)
    result: dict[str, Any] = {
        "manufacturer": manufacturer_name, "model": module_type.model,
        "part_number": getattr(module_type, "part_number", None) or None,
        "airflow": _choice_value(getattr(module_type, "airflow", None)),
        "weight": float(module_type.weight) if getattr(module_type, "weight", None) is not None else None,
        "weight_unit": _choice_value(getattr(module_type, "weight_unit", None)),
        "description": getattr(module_type, "description", None) or None,
        "comments": getattr(module_type, "comments", None) or None,
        "profile": str(profile) if profile else None,
        "attribute_data": dict(getattr(module_type, "attributes", None) or {}),
        "custom_fields": dict(getattr(module_type, "custom_fields", None) or {}),
    }
    result["_image_attachments"] = list_image_attachments(base_url, token, verify_ssl, module_type.id)
    for key, endpoint_name in MODULE_COMPONENT_ENDPOINTS.items():
        endpoint = getattr(nb.dcim, endpoint_name.replace("-", "_"))
        items = []
        for item in endpoint.filter(module_type_id=module_type.id):
            entry = {"name": item.name}
            fields = ["type", "label", "description", *MODULE_COMPONENT_EXTRA_FIELDS[key]]
            if key == "front-ports":
                fields.extend(("rear_port", "rear_port_position"))
            for field in fields:
                value = getattr(item, field, None)
                if hasattr(value, "value"):
                    value = value.value
                if field in ("power_port", "rear_port") and value is not None:
                    value = str(value)
                if value not in (None, ""):
                    entry[field] = value
            items.append(entry)
        result[key] = items
    mappings = []
    for front_port in result.get("front-ports", []):
        rear_port = front_port.pop("rear_port", None)
        if rear_port:
            mappings.append({
                "front_port": front_port["name"],
                "front_port_position": front_port.pop("rear_port_position", 1),
                "rear_port": rear_port,
                "rear_port_position": 1,
            })
    result["port-mappings"] = mappings
    return result


def push(
    base_url: str, token: str, verify_ssl: bool, payload: dict[str, Any], overwrite: bool = False,
    *, images: dict[str, BinaryRepoFile] | None = None,
) -> dict[str, Any]:
    source = ModuleType(**payload).to_internal_dict()
    nb = get_client(base_url, token, verify_ssl)
    manufacturer = nb.dcim.manufacturers.get(name=source["manufacturer"])
    if manufacturer is None:
        manufacturer = nb.dcim.manufacturers.create(
            name=source["manufacturer"], slug=_slugify(source["manufacturer"])
        )
    existing = nb.dcim.module_types.get(manufacturer_id=manufacturer.id, model=source["model"])
    if existing and not overwrite:
        return {"status": "error", "detail": f"Module type '{source['model']}' already exists. Enable overwrite to update it."}

    base_fields = {
        key: value for key, value in source.items()
        if key not in MODULE_COMPONENT_ENDPOINTS and key not in ("manufacturer", "port-mappings", "profile", "attribute_data")
    }
    base_fields["manufacturer"] = manufacturer.id
    if source.get("profile"):
        profiles = nb.dcim.module_type_profiles
        profile = profiles.get(name=source["profile"])
        if profile is None:
            profile = profiles.create(name=source["profile"], description="Created from module-type library import")
        base_fields["profile"] = profile.id
    if source.get("attribute_data"):
        base_fields["attributes"] = source["attribute_data"]
    if existing:
        existing.update(base_fields); module_type = existing
    else:
        module_type = nb.dcim.module_types.create(base_fields)

    mappings = {mapping["front_port"]: mapping for mapping in source.get("port-mappings", [])}
    for key, endpoint_name in MODULE_COMPONENT_ENDPOINTS.items():
        endpoint = getattr(nb.dcim, endpoint_name.replace("-", "_"))
        for component in source.get(key, []):
            fields = dict(component)
            fields["module_type"] = module_type.id
            if key == "front-ports" and fields.get("name") in mappings:
                mapping = mappings[fields["name"]]
                rear_port = nb.dcim.rear_port_templates.get(
                    module_type_id=module_type.id, name=mapping["rear_port"]
                )
                if rear_port is None:
                    raise ValueError(f"Port mapping references unknown rear port '{mapping['rear_port']}'")
                fields["rear_port"] = rear_port.id
                fields["rear_port_position"] = mapping["rear_port_position"]
            found = endpoint.get(module_type_id=module_type.id, name=fields["name"])
            if found:
                found.update(fields)
            else:
                endpoint.create(fields)

    warnings = []
    for side, image in (images or {}).items():
        try:
            upload_image_attachment(base_url, token, verify_ssl, module_type.id, side, image)
        except Exception as exc:
            warnings.append(f"Could not upload {side} image attachment: {exc}")
    detail = f"Pushed as module type id {module_type.id}"
    if warnings:
        detail += "; image warning(s): " + " | ".join(warnings)
    return {"status": "success", "detail": detail}


def list_image_attachments(base_url: str, token: str, verify_ssl: bool, module_type_id: int) -> list[dict]:
    response = requests.get(
        f"{base_url.rstrip('/')}/api/extras/image-attachments/",
        headers={"Authorization": f"Token {token}"},
        params={"object_type": "dcim.moduletype", "object_id": module_type_id},
        verify=verify_ssl, timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("results", payload)


def upload_image_attachment(
    base_url: str, token: str, verify_ssl: bool, module_type_id: int,
    side: str, image: BinaryRepoFile,
) -> None:
    """Create or replace a front/rear generic image attachment for a module type."""
    if side not in ("front", "rear"):
        raise ValueError(f"Unsupported module image side: {side}")
    extension = image.path.rsplit(".", 1)[-1] if "." in image.path else ""
    validate_image_bytes(image.content, extension)
    existing = next((
        item for item in list_image_attachments(base_url, token, verify_ssl, module_type_id)
        if str(item.get("name", "")).lower() == side
    ), None)
    files = {"image": (image.path.rsplit("/", 1)[-1], image.content, image.content_type)}
    data = {"object_type": "dcim.moduletype", "object_id": str(module_type_id), "name": side.title()}
    if existing:
        response = requests.patch(
            f"{base_url.rstrip('/')}/api/extras/image-attachments/{existing['id']}/",
            headers={"Authorization": f"Token {token}"}, data=data, files=files,
            verify=verify_ssl, timeout=30,
        )
    else:
        response = requests.post(
            f"{base_url.rstrip('/')}/api/extras/image-attachments/",
            headers={"Authorization": f"Token {token}"}, data=data, files=files,
            verify=verify_ssl, timeout=30,
        )
    response.raise_for_status()


def get_attachment_file(base_url: str, token: str, verify_ssl: bool, attachment: dict) -> BinaryRepoFile:
    """Download a generic NetBox image attachment for committing to GitHub."""
    image_url = attachment.get("image")
    if not image_url:
        raise ValueError("Image attachment has no downloadable image URL")
    from urllib.parse import urljoin
    response = requests.get(
        urljoin(base_url.rstrip("/") + "/", str(image_url)),
        headers={"Authorization": f"Token {token}"}, verify=verify_ssl, timeout=30,
    )
    response.raise_for_status()
    name = str(image_url).split("?", 1)[0].rsplit("/", 1)[-1] or "attachment.bin"
    return BinaryRepoFile(name, response.content, response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0])
