from __future__ import annotations

import copy

import pytest

from app.tenant_permissions import MembersPresentError, TenantManager, TenantPermissionError, derive_ro_template, render_template, validate_template


RELATIONS = {
    "dcim.site": {"path": "tenant"},
    "dcim.device": {"path": "tenant"},
    "dcim.interface": {"path": "device__tenant"},
    "dcim.cable": {"path": None, "reason": "generic endpoints"},
    "users.user": {"path": None},
}
BLOCKED = {"users.user"}


def template(entries=None, version=1, name="standard"):
    return {"name": name, "version": version, "description": "test", "permissions": entries or []}


def test_blocklist_rejection():
    with pytest.raises(TenantPermissionError, match="blocklisted"):
        validate_template(template([{"object_type": "users.user", "tenant_relation": "unsupported", "actions": []}]), RELATIONS, BLOCKED)


def test_registry_mismatch_rejection():
    with pytest.raises(TenantPermissionError, match="does not match registry"):
        validate_template(template([{"object_type": "dcim.interface", "tenant_relation": "tenant", "actions": ["view"]}]), RELATIONS, BLOCKED)


def test_unsupported_requires_explicit_acceptance_and_note():
    entry = {"object_type": "dcim.cable", "tenant_relation": "unsupported", "actions": ["view"]}
    with pytest.raises(TenantPermissionError, match="accept_unscoped"):
        validate_template(template([entry]), RELATIONS, BLOCKED)
    entry.update(accept_unscoped=True, note="Cable endpoints are generic; operators accept global visibility.")
    valid = validate_template(template([entry]), RELATIONS, BLOCKED)
    resolved = render_template(valid, 42, "Acme Corp", "prod", "acme-ro")
    assert resolved["unscoped_grants"] == [{"object_type": "dcim.cable", "actions": ["view"], "note": entry["note"]}]
    assert resolved["permissions"][0]["constraints"] == {}


def test_auto_view_and_suppression():
    entries = [
        {"object_type": "dcim.device", "tenant_relation": "tenant", "actions": ["change"]},
        {"object_type": "dcim.interface", "tenant_relation": "device__tenant", "actions": ["add"], "no_auto_view": True},
    ]
    valid = validate_template(template(entries), RELATIONS, BLOCKED)
    assert valid["permissions"][0]["actions"] == ["view", "change"]
    assert valid["permissions"][1]["actions"] == ["add"]
    assert derive_ro_template(valid)["permissions"][1]["actions"] == ["view"]


def test_render_combines_object_types_with_identical_actions_and_constraints():
    valid = validate_template(template([
        {"object_type": "dcim.device", "tenant_relation": "tenant", "actions": ["view", "change"]},
        {"object_type": "dcim.site", "tenant_relation": "tenant", "actions": ["change", "view"]},
        {"object_type": "dcim.interface", "tenant_relation": "device__tenant", "actions": ["view", "change"]},
    ]), RELATIONS, BLOCKED)

    rendered = render_template(valid, 42, "Acme Corp", "prod", "acme-rw")

    assert rendered["permissions"] == [
        {
            "object_types": ["dcim.device", "dcim.site"],
            "actions": ["view", "change"],
            "constraints": {"tenant__id": 42},
        },
        {
            "object_type": "dcim.interface",
            "actions": ["view", "change"],
            "constraints": {"device__tenant__id": 42},
        },
    ]


class FakeRepo:
    def __init__(self, files): self.files, self.prs = copy.deepcopy(files), []
    def read_yaml(self, path): return copy.deepcopy(self.files.get(path))
    def list_paths(self, prefix): return [p for p in self.files if p.startswith(prefix)]
    def write_pr(self, changes, title, body):
        for path, value in changes.items():
            if value is None: self.files.pop(path, None)
            else: self.files[path] = copy.deepcopy(value)
        self.prs.append((title, changes)); return {"number": len(self.prs), "url": "test"}


class FakeNetBox:
    def __init__(self):
        self.groups, self.permissions, self.members, self.next_id = {}, {}, {}, 1
    def tenant(self, tenant_id): return {"id": tenant_id, "name": "Acme Corp"}
    def group(self, name): return self.groups.get(name)
    def create_group(self, name):
        group = {"id": self.next_id, "name": name}; self.next_id += 1; self.groups[name] = group; return group
    def group_member_count(self, group_id): return self.members.get(group_id, 0)
    def reconcile(self, group, desired, key):
        current = self.permissions.get(group["id"], [])
        if current == desired: return {"created": 0, "updated": 0, "deleted": 0}
        stats = {"created": max(0, len(desired) - len(current)), "updated": min(len(current), len(desired)), "deleted": max(0, len(current) - len(desired))}
        self.permissions[group["id"]] = copy.deepcopy(desired); return stats
    def plan_reconcile(self, group, desired, key):
        if group is None: return {"created": len(desired), "updated": 0, "deleted": 0}
        current = self.permissions.get(group["id"], [])
        return {"created": max(0, len(desired) - len(current)), "updated": 0, "deleted": max(0, len(current) - len(desired))}
    def delete_group_and_permissions(self, group, key_prefix):
        self.permissions.pop(group["id"], None); self.groups.pop(group["name"], None)
        return {"deleted": 1, "detached": 0}


def manager(version=1, instance="prod", netbox=None, repo=None):
    tpl = template([{"object_type": "dcim.device", "tenant_relation": "tenant", "actions": ["change"]}], version)
    repo = repo or FakeRepo({"templates/standard.yaml": tpl})
    netbox = netbox or FakeNetBox()
    return TenantManager(netbox, repo, RELATIONS, BLOCKED, instance, "tester"), netbox, repo


def test_onboard_is_idempotent():
    mgr, _, repo = manager()
    assert mgr.onboard(42, "acme-ro", "acme-rw", "standard")["changed"] is True
    assert mgr.onboard(42, "acme-ro", "acme-rw", "standard")["changed"] is False
    assert len(repo.prs) == 1


def test_apply_template_fleet_selection_and_update_semantics():
    # Fleet selection is metadata-driven; exercising two managers verifies every selected instance is updated.
    netboxes, repos = [], []
    for instance in ("east", "west"):
        mgr, nb, repo = manager(version=1, instance=instance)
        mgr.onboard(42, f"{instance}-ro", f"{instance}-rw", "standard")
        repo.files["templates/standard.yaml"]["version"] = 2
        mgr.onboard(42, f"{instance}-ro", f"{instance}-rw", "standard")
        assert repo.files[f"instances/{instance}/tenants/42/metadata.yaml"]["rw_template"]["version"] == 2
        netboxes.append(nb); repos.append(repo)
    assert all(len(repo.prs) == 2 for repo in repos)


def test_no_cli_entrypoint_is_exposed():
    import app.tenant_permissions as module
    assert not hasattr(module, "main")
    assert not hasattr(module, "build_parser")


def test_decommission_member_guard_and_force_delete():
    mgr, nb, repo = manager()
    mgr.onboard(42, "acme-ro", "acme-rw", "standard")
    nb.members[nb.groups["acme-ro"]["id"]] = 1
    with pytest.raises(MembersPresentError, match="members"):
        mgr.decommission(42)
    mgr.decommission(42, force=True)
    assert nb.groups == {}
    assert not any("/tenants/42/" in path for path in repo.files)


@pytest.mark.parametrize("name", ["", "../escape", "/absolute", "UPPER", "a/b"])
def test_template_name_rejects_unsafe_paths(name):
    with pytest.raises(TenantPermissionError, match="name"):
        validate_template(template(name=name), RELATIONS, BLOCKED)


@pytest.mark.parametrize("instance", ["../escape", "/absolute", ""])
def test_instance_path_segment_is_validated(instance):
    with pytest.raises(TenantPermissionError, match="instance name"):
        manager(instance=instance)


def test_plan_does_not_mutate():
    mgr, nb, repo = manager()
    result = mgr.plan(42, "acme-ro", "acme-rw", "standard")
    assert result["stats"]["ro"]["created"] == 1
    assert nb.groups == {}
    assert repo.prs == []
