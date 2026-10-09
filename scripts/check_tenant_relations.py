#!/usr/bin/env python3
"""Compare direct Tenant foreign keys in a live NetBox Django environment.

Run this with NetBox's virtualenv and settings available, for example from the
NetBox installation directory:
  /opt/netbox/venv/bin/python /path/to/check_tenant_relations.py --registry /path/to/config/tenant-relations.yaml
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import yaml


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    args = parser.parse_args()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "netbox.settings")
    import django
    django.setup()
    from django.apps import apps
    from tenancy.models import Tenant

    relations = (yaml.safe_load(args.registry.read_text()) or {}).get("relations", {})
    discovered = {}
    for model in apps.get_models():
        for field in model._meta.get_fields():
            if getattr(field, "many_to_one", False) and getattr(field, "related_model", None) is Tenant:
                discovered[f"{model._meta.app_label}.{model._meta.model_name}"] = field.name

    errors = 0
    for object_type, field_name in sorted(discovered.items()):
        configured = relations.get(object_type, {}).get("path")
        marker = "OK" if configured == field_name else "MISMATCH"
        print(f"{marker:8} {object_type}: discovered={field_name!r} configured={configured!r}")
        errors += marker != "OK"
    for object_type, entry in sorted(relations.items()):
        if entry.get("path") == "tenant" and object_type not in discovered:
            print(f"MISSING  {object_type}: configured direct tenant FK was not discovered")
            errors += 1
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
