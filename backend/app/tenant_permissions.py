"""Tenant group and object-permission automation core."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import yaml


ACTIONS = ("view", "add", "change", "delete")
UNSUPPORTED = "unsupported"
SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SAFE_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HERE = Path(__file__).resolve()
ROOT = next(
    (candidate for candidate in (_HERE.parents[2], _HERE.parents[1])
     if (candidate / "config/tenant-relations.yaml").exists()),
    _HERE.parents[2],
)


class TenantPermissionError(RuntimeError):
    pass


class MembersPresentError(TenantPermissionError):
    pass


def validate_path_segment(value: str, label: str) -> str:
    if not isinstance(value, str) or not SAFE_PATH_SEGMENT_RE.fullmatch(value):
        raise TenantPermissionError(
            f"{label} must match {SAFE_PATH_SEGMENT_RE.pattern!r}; got {value!r}"
        )
    return value


def validate_template_name(value: str) -> str:
    if not isinstance(value, str) or not SAFE_NAME_RE.fullmatch(value):
        raise TenantPermissionError(f"template name must match {SAFE_NAME_RE.pattern!r}; got {value!r}")
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise TenantPermissionError(f"Cannot read YAML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TenantPermissionError(f"{path}: top level must be a mapping")
    return value


def load_policy_files(root: Path = ROOT) -> tuple[dict[str, Any], set[str]]:
    registry_doc = _load_yaml(root / "config/tenant-relations.yaml")
    blocklist_doc = _load_yaml(root / "config/blocklist.yaml")
    relations = registry_doc.get("relations")
    blocked = blocklist_doc.get("blocked_object_types")
    if not isinstance(relations, dict) or not isinstance(blocked, list):
        raise TenantPermissionError("Invalid tenant-relations.yaml or blocklist.yaml")
    return relations, set(blocked)


def validate_template(
    template: dict[str, Any], relations: dict[str, Any], blocked: set[str]
) -> dict[str, Any]:
    """Validate and normalize a permission template, failing closed."""
    errors: list[str] = []
    for key in ("name", "version", "description", "permissions"):
        if key not in template:
            errors.append(f"missing required field: {key}")
    if not isinstance(template.get("name"), str) or not template.get("name", "").strip():
        errors.append("name must be a non-empty string")
    elif not SAFE_NAME_RE.fullmatch(template["name"]):
        errors.append(f"name must match {SAFE_NAME_RE.pattern!r}")
    if not isinstance(template.get("version"), (str, int)) or isinstance(template.get("version"), bool):
        errors.append("version must be a string or integer")
    if not isinstance(template.get("description"), str):
        errors.append("description must be a string")
    if not isinstance(template.get("permissions"), list):
        errors.append("permissions must be a list")
    if errors:
        raise TenantPermissionError("Template validation failed:\n- " + "\n- ".join(errors))

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
        # Blocklist is deliberately checked regardless of actions/relation.
        if object_type in blocked:
            errors.append(f"{where}: {object_type} is blocklisted")
        registry_entry = relations.get(object_type)
        if registry_entry is None:
            errors.append(f"{where}: {object_type} is absent from tenant-relations registry")
            continue
        expected_path = registry_entry.get("path") if isinstance(registry_entry, dict) else registry_entry
        expected = UNSUPPORTED if expected_path is None else expected_path
        actual = entry.get("tenant_relation")
        if actual != expected:
            errors.append(
                f"{where}: tenant_relation {actual!r} does not match registry value {expected!r}"
            )
        actions = entry.get("actions")
        if not isinstance(actions, list) or any(a not in ACTIONS for a in actions):
            errors.append(f"{where}.actions must be a list containing only {list(ACTIONS)}")
            continue
        if len(actions) != len(set(actions)):
            errors.append(f"{where}.actions contains duplicates")
        if expected == UNSUPPORTED and actions:
            if entry.get("accept_unscoped") is not True:
                errors.append(
                    f"{where}: unsupported tenant relation with actions requires accept_unscoped: true"
                )
        if entry.get("accept_unscoped") is True and (
            not isinstance(entry.get("note"), str) or not entry["note"].strip()
        ):
            errors.append(f"{where}: accept_unscoped: true requires a non-empty note")
        if entry.get("accept_unscoped") not in (None, True, False):
            errors.append(f"{where}.accept_unscoped must be boolean")
        if entry.get("no_auto_view") not in (None, True, False):
            errors.append(f"{where}.no_auto_view must be boolean")
        if actions and "view" not in actions and not entry.get("no_auto_view"):
            entry["actions"] = ["view", *actions]
        entry["actions"] = [action for action in ACTIONS if action in entry["actions"]]
        entry.setdefault("accept_unscoped", False)
        entry.setdefault("no_auto_view", False)
    if errors:
        raise TenantPermissionError("Template validation failed:\n- " + "\n- ".join(errors))
    return normalized


def derive_ro_template(rw: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(rw)
    result["name"] = f"{rw['name']} (derived read-only)"
    for entry in result["permissions"]:
        entry["actions"] = ["view"] if entry["actions"] else []
    return result


def render_template(template: dict[str, Any], tenant_id: int, tenant_name: str,
                    instance_slug: str, group_name: str) -> dict[str, Any]:
    permissions: list[dict[str, Any]] = []
    unscoped: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for entry in template["permissions"]:
        if not entry["actions"]:
            continue
        relation = entry["tenant_relation"]
        if relation == UNSUPPORTED:
            if entry.get("accept_unscoped") is not True:
                skipped.append({"object_type": entry["object_type"], "reason": "unsupported tenant relation"})
                continue
            constraints: dict[str, Any] = {}
            unscoped.append({
                "object_type": entry["object_type"],
                "actions": entry["actions"],
                "note": entry["note"],
            })
        else:
            constraints = {f"{relation}__id": tenant_id}
        permissions.append({
            "object_type": entry["object_type"],
            "actions": entry["actions"],
            "constraints": constraints,
        })
    return {
        "instance": instance_slug,
        "tenant": {"id": tenant_id, "name": tenant_name},
        "group": group_name,
        "template": {"name": template["name"], "version": template["version"]},
        "permissions": combine_permissions(permissions),
        "unscoped_grants": unscoped,
        "skipped_unsupported": skipped,
    }


class NetBoxBackend(Protocol):
    def tenant(self, tenant_id: int) -> dict[str, Any]: ...
    def group(self, name: str) -> dict[str, Any] | None: ...
    def create_group(self, name: str) -> dict[str, Any]: ...
    def group_member_count(self, group_id: int) -> int: ...
    def reconcile(self, group: dict[str, Any], desired: list[dict[str, Any]], key: str) -> dict[str, int]: ...
    def plan_reconcile(self, group: dict[str, Any] | None, desired: list[dict[str, Any]], key: str) -> dict[str, int]: ...
    def delete_group_and_permissions(self, group: dict[str, Any], key_prefix: str) -> dict[str, int]: ...


class RepoBackend(Protocol):
    def read_yaml(self, path: str) -> dict[str, Any] | None: ...
    def write_pr(self, changes: dict[str, dict[str, Any] | None], title: str, body: str) -> dict[str, Any]: ...
    def list_paths(self, prefix: str) -> list[str]: ...


@dataclass
class TenantManager:
    netbox: NetBoxBackend
    repo: RepoBackend
    relations: dict[str, Any]
    blocked: set[str]
    instance_slug: str
    actor: str

    def __post_init__(self) -> None:
        validate_path_segment(self.instance_slug, "instance name")

    def _template(self, name: str) -> dict[str, Any]:
        validate_template_name(name)
        for path in (f"templates/custom/{name}.yaml", f"templates/{name}.yaml"):
            value = self.repo.read_yaml(path)
            if value is not None:
                validated = validate_template(value, self.relations, self.blocked)
                if validated["name"] != name:
                    raise TenantPermissionError(f"{path}: name must equal {name!r}")
                return validated
        raise TenantPermissionError(f"Template {name!r} not found")

    def onboard(self, tenant_id: int, oidc_ro: str, oidc_rw: str,
                rw_template_name: str, ro_template_name: str | None = None) -> dict[str, Any]:
        if oidc_ro == oidc_rw:
            raise TenantPermissionError("RO and RW OIDC group names must differ")
        tenant = self.netbox.tenant(tenant_id)
        tenant_name = str(tenant["name"])
        tenant_key = str(tenant["id"])
        base = f"instances/{self.instance_slug}/tenants/{tenant_key}"
        old_meta = self.repo.read_yaml(f"{base}/metadata.yaml")
        tracked_names = set()
        if old_meta:
            tracked_names = {old_meta.get("oidc_group_ro"), old_meta.get("oidc_group_rw")}

        # Validate all source material and collision checks before mutating NetBox.
        rw = self._template(rw_template_name)
        ro = self._template(ro_template_name) if ro_template_name else derive_ro_template(rw)
        existing_groups: dict[str, dict[str, Any] | None] = {}
        for role, name in (("ro", oidc_ro), ("rw", oidc_rw)):
            existing = self.netbox.group(name)
            if existing and name not in tracked_names:
                raise TenantPermissionError(
                    f"NetBox group {name!r} already exists but is not tracked for this tenant"
                )
            existing_groups[role] = existing

        groups = {
            role: existing_groups[role] or self.netbox.create_group(name)
            for role, name in (("ro", oidc_ro), ("rw", oidc_rw))
        }
        rendered = {
            "ro": render_template(ro, int(tenant["id"]), tenant_name, self.instance_slug, oidc_ro),
            "rw": render_template(rw, int(tenant["id"]), tenant_name, self.instance_slug, oidc_rw),
        }
        stats = {}
        # Each group is reconciled additions/updates first, deletions last in the backend.
        for role in ("ro", "rw"):
            key = f"nbm:{self.instance_slug}:{tenant_key}:{role}"
            stats[role] = self.netbox.reconcile(groups[role], rendered[role]["permissions"], key)

        metadata = {
            "tenant_id": int(tenant["id"]),
            "tenant_name": tenant_name,
            "rw_template": {"name": rw_template_name, "version": rw["version"]},
            "ro_template": ({"name": ro_template_name, "version": ro["version"]}
                            if ro_template_name else {"derived_from": rw_template_name, "version": rw["version"]}),
            "oidc_group_ro": oidc_ro,
            "oidc_group_rw": oidc_rw,
            "last_applied_at": datetime.now(timezone.utc).isoformat(),
            "last_applied_by": self.actor,
        }
        changes = {f"{base}/ro.resolved.yaml": rendered["ro"],
                   f"{base}/rw.resolved.yaml": rendered["rw"],
                   f"{base}/metadata.yaml": metadata}
        unchanged = all(self.repo.read_yaml(p) == v for p, v in changes.items())
        permission_noop = all(sum(s.values()) == 0 for s in stats.values())
        # Timestamp alone must not destroy idempotency.
        if unchanged or (old_meta and permission_noop and all(
            old_meta.get(k) == metadata.get(k) for k in ("rw_template", "ro_template", "oidc_group_ro", "oidc_group_rw")
        )):
            return {"changed": False, "stats": stats, "unscoped_grants": _all_unscoped(rendered)}
        pr = self.repo.write_pr(changes, f"Onboard tenant {tenant_name}",
                                f"Automated tenant permission update requested by {self.actor}.")
        return {"changed": True, "stats": stats, "pr": pr, "unscoped_grants": _all_unscoped(rendered)}

    def plan(self, tenant_id: int, oidc_ro: str, oidc_rw: str,
             rw_template_name: str, ro_template_name: str | None = None) -> dict[str, Any]:
        """Render and diff an onboarding/apply operation without mutating NetBox or GitHub."""
        if oidc_ro == oidc_rw:
            raise TenantPermissionError("RO and RW OIDC group names must differ")
        tenant = self.netbox.tenant(tenant_id)
        tenant_key = str(tenant["id"])
        rw = self._template(rw_template_name)
        ro = self._template(ro_template_name) if ro_template_name else derive_ro_template(rw)
        rendered = {
            "ro": render_template(ro, int(tenant["id"]), str(tenant["name"]), self.instance_slug, oidc_ro),
            "rw": render_template(rw, int(tenant["id"]), str(tenant["name"]), self.instance_slug, oidc_rw),
        }
        stats = {}
        for role, name in (("ro", oidc_ro), ("rw", oidc_rw)):
            group = self.netbox.group(name)
            stats[role] = self.netbox.plan_reconcile(
                group, rendered[role]["permissions"], f"nbm:{self.instance_slug}:{tenant_key}:{role}"
            )
        return {
            "instance": self.instance_slug, "tenant_id": int(tenant["id"]),
            "tenant_name": str(tenant["name"]), "stats": stats,
            "unscoped_grants": _all_unscoped(rendered),
        }

    def decommission(self, tenant_id: int, force: bool = False) -> dict[str, Any]:
        tenant_key = str(tenant_id)
        base = f"instances/{self.instance_slug}/tenants/{tenant_key}"
        meta = self.repo.read_yaml(f"{base}/metadata.yaml")
        if not meta:
            raise TenantPermissionError(f"No tracked metadata for {self.instance_slug}/tenant ID {tenant_id}")
        groups = [self.netbox.group(meta[k]) for k in ("oidc_group_ro", "oidc_group_rw")]
        populated = [(g["name"], self.netbox.group_member_count(g["id"])) for g in groups if g]
        populated = [(name, count) for name, count in populated if count]
        if populated and not force:
            detail = ", ".join(f"{name} ({count} members)" for name, count in populated)
            raise MembersPresentError(f"Refusing to decommission groups with members: {detail}")
        cleanup = {"deleted": 0, "detached": 0}
        for g in groups:
            if g:
                result = self.netbox.delete_group_and_permissions(g, f"nbm:{self.instance_slug}:{tenant_key}:")
                cleanup = {key: cleanup[key] + result.get(key, 0) for key in cleanup}
        changes = {path: None for path in self.repo.list_paths(base + "/")}
        pr = self.repo.write_pr(changes, f"Decommission tenant {meta.get('tenant_name', tenant_id)}",
                                f"Automated tenant decommission requested by {self.actor}.")
        return {"changed": True, "pr": pr, "permission_cleanup": cleanup}


def _all_unscoped(rendered: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"role": role, **grant} for role, doc in rendered.items() for grant in doc["unscoped_grants"]]


def combine_permissions(permissions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Combine object types only when their effective actions and constraints are identical."""
    grouped: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}
    order: list[tuple[tuple[str, ...], str]] = []
    for permission in permissions:
        actions = tuple(permission["actions"])
        constraints = permission.get("constraints") or {}
        group_key = (actions, json.dumps(constraints, sort_keys=True, separators=(",", ":")))
        if group_key not in grouped:
            grouped[group_key] = {
                "object_types": [], "actions": list(actions), "constraints": constraints,
            }
            order.append(group_key)
        object_types = permission.get("object_types") or [permission["object_type"]]
        for object_type in object_types:
            if object_type not in grouped[group_key]["object_types"]:
                grouped[group_key]["object_types"].append(object_type)

    result: list[dict[str, Any]] = []
    for group_key in order:
        permission = grouped[group_key]
        object_types = sorted(permission.pop("object_types"))
        if len(object_types) == 1:
            permission["object_type"] = object_types[0]
        else:
            permission["object_types"] = object_types
        result.append(permission)
    return result


def permission_name(key: str, object_types: str | list[str]) -> str:
    """Stable <=100 character ownership marker used to track only our permissions."""
    normalized = [object_types] if isinstance(object_types, str) else sorted(set(object_types))
    identity = ",".join(normalized)
    digest = hashlib.sha256(f"{key}:{identity}".encode()).hexdigest()[:12]
    label = normalized[0] if len(normalized) == 1 else f"combined-{len(normalized)}"
    return f"{key}:{label}:{digest}"[:100]
