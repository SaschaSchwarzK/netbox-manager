#!/usr/bin/env python3
"""Validate owner registry lookups in a live NetBox Django environment."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import yaml


def resolve(model, path: str):
    current = model
    for segment in path.split("__"):
        field = current._meta.get_field(segment)
        current = field.related_model
        if current is None and segment != path.split("__")[-1]:
            raise ValueError(f"{segment!r} is not a relation")
    return field


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    args = parser.parse_args()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "netbox.settings")
    import django
    django.setup()
    from django.apps import apps
    from users.models import User

    relations = (yaml.safe_load(args.registry.read_text()) or {}).get("relations", {})
    errors = 0
    for object_type, entry in sorted(relations.items()):
        try:
            model = apps.get_model(object_type)
            final = resolve(model, entry["path"])
            if final.related_model is not User or not final.many_to_many:
                raise ValueError("lookup does not terminate at the users.Group.user reverse M2M relation")
            print(f"OK       {object_type}: {entry['path']}")
        except Exception as exc:  # noqa: BLE001 - report every invalid Django lookup and continue
            print(f"MISMATCH {object_type}: {entry.get('path')!r}: {exc}")
            errors += 1
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
