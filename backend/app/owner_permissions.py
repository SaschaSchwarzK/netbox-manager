"""Owner-based NetBox ObjectPermission policy and reconciliation."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.tenant_permissions import (
    ACTIONS, MembersPresentError, NetBoxBackend, RepoBackend, SAFE_NAME_RE,
    TenantPermissionError, combine_permissions, validate_path_segment,
    validate_template_name,
)


class OwnerPermissionError(TenantPermissionError):
    pass


_HERE = Path(__file__).resolve()
ROOT = next((p for p in (_HERE.parents[2], _HERE.parents[1]) if (p / "config/owner-relations.yaml").exists()), _HERE.parents[2])
TEMPLATE_ROOT = "templates/owner-permissions"
LEGACY_TEMPLATE_ROOT = "owner-templates"
STATE_ROOT = "owner-permissions/instances"


def _yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise OwnerPermissionError(f"Cannot read YAML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OwnerPermissionError(f"{path}: top level must be a mapping")
    return value


def load_owner_policy_files(root: Path = ROOT) -> tuple[dict[str, Any], set[str]]:
    registry = _yaml(root / "config/owner-relations.yaml").get("relations")
    blocked = _yaml(root / "config/blocklist.yaml").get("blocked_object_types")
    if not isinstance(registry, dict) or not isinstance(blocked, list):
        raise OwnerPermissionError("Invalid owner-relations.yaml or blocklist.yaml")
    return registry, set(blocked)


def validate_owner_template(template: dict[str, Any], relations: dict[str, Any], blocked: set[str]) -> dict[str, Any]:
    errors: list[str] = []
    for key in ("name", "version", "description", "permissions"):
        if key not in template:
            errors.append(f"missing required field: {key}")
    if not isinstance(template.get("name"), str) or not SAFE_NAME_RE.fullmatch(template.get("name", "")):
        errors.append(f"name must match {SAFE_NAME_RE.pattern!r}")
    if not isinstance(template.get("version"), (str, int)) or isinstance(template.get("version"), bool):
        errors.append("version must be a string or integer")
    if not isinstance(template.get("description"), str):
        errors.append("description must be a string")
    if not isinstance(template.get("permissions"), list):
        errors.append("permissions must be a list")
    if errors:
        raise OwnerPermissionError("Template validation failed:\n- " + "\n- ".join(errors))

    normalized = copy.deepcopy(template)
    seen: set[str] = set()
    for index, entry in enumerate(normalized["permissions"]):
        where = f"permissions[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be a mapping")
            continue
        object_type = entry.get("object_type")
        if not isinstance(object_type, str) or not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", object_type):
            errors.append(f"{where}.object_type must be <app_label>.<model>")
            continue
        if object_type in seen:
            errors.append(f"{where}: duplicate object_type {object_type}")
        seen.add(object_type)
        if object_type in blocked:
            errors.append(f"{where}: {object_type} is blocklisted")
        registry_entry = relations.get(object_type)
        if not isinstance(registry_entry, dict) or not isinstance(registry_entry.get("path"), str):
            errors.append(f"{where}: {object_type} is absent from owner-relations registry")
            continue
        expected = registry_entry["path"]
        if entry.get("owner_relation") != expected:
            errors.append(f"{where}: owner_relation {entry.get('owner_relation')!r} does not match registry value {expected!r}")
        actions = entry.get("actions")
        if not isinstance(actions, list) or any(a not in ACTIONS for a in actions):
            errors.append(f"{where}.actions must be a list containing only {list(ACTIONS)}")
            continue
        if len(actions) != len(set(actions)):
            errors.append(f"{where}.actions contains duplicates")
        if entry.get("no_auto_view") not in (None, True, False):
            errors.append(f"{where}.no_auto_view must be boolean")
        if actions and "view" not in actions and not entry.get("no_auto_view"):
            entry["actions"] = ["view", *actions]
        entry["actions"] = [a for a in ACTIONS if a in entry["actions"]]
        entry.setdefault("no_auto_view", False)
    if errors:
        raise OwnerPermissionError("Template validation failed:\n- " + "\n- ".join(errors))
    return normalized


def derive_ro_template(template: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(template)
    value["name"] = f"{template['name']} (derived read-only)"
    for permission in value["permissions"]:
        permission["actions"] = ["view"] if permission["actions"] else []
    return value


def render_owner_template(template: dict[str, Any], instance_slug: str, group_name: str) -> dict[str, Any]:
    permissions = [{
        "object_type": entry["object_type"],
        "actions": entry["actions"],
        "constraints": {entry["owner_relation"]: "$user"},
    } for entry in template["permissions"] if entry["actions"]]
    return {
        "instance": instance_slug, "group": group_name,
        "template": {"name": template["name"], "version": template["version"]},
        "permissions": combine_permissions(permissions),
    }


@dataclass
class OwnerManager:
    netbox: NetBoxBackend
    repo: RepoBackend
    relations: dict[str, Any]
    blocked: set[str]
    instance_slug: str
    actor: str

    def __post_init__(self) -> None:
        validate_path_segment(self.instance_slug, "instance name")

    @property
    def base(self) -> str:
        return f"{STATE_ROOT}/{self.instance_slug}"

    def _template(self, name: str) -> dict[str, Any]:
        validate_template_name(name)
        for path in (
            f"{TEMPLATE_ROOT}/custom/{name}.yaml",
            f"{TEMPLATE_ROOT}/{name}.yaml",
            f"{LEGACY_TEMPLATE_ROOT}/custom/{name}.yaml",
            f"{LEGACY_TEMPLATE_ROOT}/{name}.yaml",
        ):
            value = self.repo.read_yaml(path)
            if value is not None:
                result = validate_owner_template(value, self.relations, self.blocked)
                if result["name"] != name:
                    raise OwnerPermissionError(f"{path}: name must equal {name!r}")
                return result
        raise OwnerPermissionError(f"Owner template {name!r} not found")

    def _prepare(self, ro_group: str, rw_group: str, rw_template: str, ro_template: str | None):
        if ro_group == rw_group:
            raise OwnerPermissionError("RO and RW group names must differ")
        rw = self._template(rw_template)
        ro = self._template(ro_template) if ro_template else derive_ro_template(rw)
        metadata = self.repo.read_yaml(f"{self.base}/metadata.yaml") or {}
        tracked = {metadata.get("group_ro"), metadata.get("group_rw")}
        groups = {}
        for role, name in (("ro", ro_group), ("rw", rw_group)):
            group = self.netbox.group(name)
            if group and name not in tracked:
                raise OwnerPermissionError(f"NetBox group {name!r} already exists but is not tracked by owner permissions for this instance")
            groups[role] = group
        rendered = {"ro": render_owner_template(ro, self.instance_slug, ro_group), "rw": render_owner_template(rw, self.instance_slug, rw_group)}
        return rw, ro, metadata, groups, rendered

    def reconcile(self, ro_group: str, rw_group: str, rw_template: str, ro_template: str | None = None) -> dict[str, Any]:
        rw, ro, old, existing, rendered = self._prepare(ro_group, rw_group, rw_template, ro_template)
        groups = {role: existing[role] or self.netbox.create_group(name) for role, name in (("ro", ro_group), ("rw", rw_group))}
        stats = {role: self.netbox.reconcile(groups[role], rendered[role]["permissions"], f"nbm:owner:{self.instance_slug}:{role}") for role in ("ro", "rw")}
        metadata = {
            "instance": self.instance_slug,
            "rw_template": {"name": rw_template, "version": rw["version"]},
            "ro_template": ({"name": ro_template, "version": ro["version"]} if ro_template else {"derived_from": rw_template, "version": rw["version"]}),
            "group_ro": ro_group, "group_rw": rw_group,
            "last_applied_at": datetime.now(timezone.utc).isoformat(), "last_applied_by": self.actor,
        }
        changes = {f"{self.base}/ro.resolved.yaml": rendered["ro"], f"{self.base}/rw.resolved.yaml": rendered["rw"], f"{self.base}/metadata.yaml": metadata}
        noop = all(sum(s.values()) == 0 for s in stats.values())
        stable = old and all(old.get(k) == metadata.get(k) for k in ("rw_template", "ro_template", "group_ro", "group_rw"))
        if noop and stable:
            return {"changed": False, "stats": stats}
        pr = self.repo.write_pr(changes, f"Reconcile owner permissions for {self.instance_slug}", f"Owner permission update requested by {self.actor}.")
        return {"changed": True, "stats": stats, "pr": pr}

    def plan(self, ro_group: str, rw_group: str, rw_template: str, ro_template: str | None = None) -> dict[str, Any]:
        _, _, _, groups, rendered = self._prepare(ro_group, rw_group, rw_template, ro_template)
        stats = {role: self.netbox.plan_reconcile(groups[role], rendered[role]["permissions"], f"nbm:owner:{self.instance_slug}:{role}") for role in ("ro", "rw")}
        return {"instance": self.instance_slug, "stats": stats}

    def decommission(self, force: bool = False) -> dict[str, Any]:
        metadata = self.repo.read_yaml(f"{self.base}/metadata.yaml")
        if not metadata:
            raise OwnerPermissionError(f"No tracked owner metadata for {self.instance_slug}")
        groups = [self.netbox.group(metadata[k]) for k in ("group_ro", "group_rw")]
        populated = [(g["name"], self.netbox.group_member_count(g["id"])) for g in groups if g]
        populated = [(name, count) for name, count in populated if count]
        if populated and not force:
            raise MembersPresentError("Refusing to decommission groups with members: " + ", ".join(f"{n} ({c} members)" for n, c in populated))
        cleanup = {"deleted": 0, "detached": 0}
        for group in groups:
            if group:
                result = self.netbox.delete_group_and_permissions(group, f"nbm:owner:{self.instance_slug}:")
                cleanup = {key: cleanup[key] + result.get(key, 0) for key in cleanup}
        changes = {path: None for path in self.repo.list_paths(self.base + "/")}
        pr = self.repo.write_pr(changes, f"Decommission owner permissions for {self.instance_slug}", f"Owner permission decommission requested by {self.actor}.")
        return {"changed": True, "pr": pr, "permission_cleanup": cleanup}
