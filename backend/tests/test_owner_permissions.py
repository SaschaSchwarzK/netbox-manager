import copy

import pytest

from app.owner_permissions import OwnerManager, OwnerPermissionError, render_owner_template, validate_owner_template
from app.tenant_permissions import MembersPresentError

RELATIONS = {"dcim.device": {"path": "owner__users"}, "dcim.interface": {"path": "device__owner__users"}}


def template(entries=None, name="owners", version=1):
    return {"name": name, "version": version, "description": "owner policy", "permissions": entries or []}


def test_registry_paths_token_and_action_normalization():
    value = validate_owner_template(template([
        {"object_type": "dcim.device", "owner_relation": "owner__users", "actions": ["change"]},
        {"object_type": "dcim.interface", "owner_relation": "device__owner__users", "actions": ["change"]},
    ]), RELATIONS, set())
    rendered = render_owner_template(value, "prod", "owners-rw")
    assert value["permissions"][0]["actions"] == ["view", "change"]
    assert rendered["permissions"][0]["constraints"] == {"owner__users": "$user"}
    assert rendered["permissions"][1]["constraints"] == {"device__owner__users": "$user"}


@pytest.mark.parametrize("entry", [
    {"object_type": "dcim.device", "owner_relation": "owner", "actions": ["view"]},
    {"object_type": "ipam.prefix", "owner_relation": "owner__users", "actions": ["view"]},
])
def test_unknown_or_mismatched_paths_fail_closed(entry):
    with pytest.raises(OwnerPermissionError):
        validate_owner_template(template([entry]), RELATIONS, set())


def test_identical_owner_grants_are_combined():
    relations = {"dcim.device": {"path": "owner__users"}, "dcim.site": {"path": "owner__users"}}
    value = validate_owner_template(template([
        {"object_type": "dcim.device", "owner_relation": "owner__users", "actions": ["view"]},
        {"object_type": "dcim.site", "owner_relation": "owner__users", "actions": ["view"]},
    ]), relations, set())
    assert render_owner_template(value, "prod", "owners-ro")["permissions"] == [{
        "object_types": ["dcim.device", "dcim.site"], "actions": ["view"], "constraints": {"owner__users": "$user"},
    }]


class Repo:
    def __init__(self): self.files = {"templates/owner-permissions/owners.yaml": template([{"object_type": "dcim.device", "owner_relation": "owner__users", "actions": ["change"]}])}; self.prs = []
    def read_yaml(self, path): return copy.deepcopy(self.files.get(path))
    def list_paths(self, prefix): return [p for p in self.files if p.startswith(prefix)]
    def write_pr(self, changes, title, body):
        for path, value in changes.items():
            if value is None: self.files.pop(path, None)
            else: self.files[path] = copy.deepcopy(value)
        self.prs.append(changes); return {"number": len(self.prs), "url": "test"}


class NetBox:
    def __init__(self): self.groups = {}; self.permissions = {}; self.members = {}; self.n = 1
    def group(self, name): return self.groups.get(name)
    def create_group(self, name):
        value = {"id": self.n, "name": name}; self.n += 1; self.groups[name] = value; return value
    def group_member_count(self, group_id): return self.members.get(group_id, 0)
    def reconcile(self, group, desired, key):
        old = self.permissions.get(group["id"], [])
        if old == desired: return {"created": 0, "updated": 0, "deleted": 0}
        self.permissions[group["id"]] = copy.deepcopy(desired); return {"created": len(desired), "updated": 0, "deleted": len(old)}
    def plan_reconcile(self, group, desired, key): return {"created": len(desired) if group is None else 0, "updated": 0, "deleted": 0}
    def delete_group_and_permissions(self, group, key): self.groups.pop(group["name"]); return {"deleted": 1, "detached": 0}


def test_reconcile_idempotency_collision_and_member_guard():
    repo, nb = Repo(), NetBox()
    manager = OwnerManager(nb, repo, RELATIONS, set(), "prod", "tester")
    assert manager.reconcile("owner-ro", "owner-rw", "owners")["changed"] is True
    assert manager.reconcile("owner-ro", "owner-rw", "owners")["changed"] is False
    nb.members[nb.groups["owner-ro"]["id"]] = 1
    with pytest.raises(MembersPresentError): manager.decommission()
    manager.decommission(force=True)
    assert not nb.groups


def test_plan_is_read_only_and_reports_both_shared_groups():
    repo, nb = Repo(), NetBox()
    manager = OwnerManager(nb, repo, RELATIONS, set(), "prod", "tester")
    result = manager.plan("owner-ro", "owner-rw", "owners")
    assert result["stats"]["ro"]["created"] == 1
    assert result["stats"]["rw"]["created"] == 1
    assert nb.groups == {}
    assert repo.prs == []


def test_untracked_group_collision_is_rejected_before_mutation():
    repo, nb = Repo(), NetBox(); nb.create_group("owner-ro")
    manager = OwnerManager(nb, repo, RELATIONS, set(), "prod", "tester")
    with pytest.raises(OwnerPermissionError, match="not tracked"):
        manager.reconcile("owner-ro", "owner-rw", "owners")


def test_legacy_owner_template_path_remains_readable():
    repo, nb = Repo(), NetBox()
    repo.files["owner-templates/owners.yaml"] = repo.files.pop(
        "templates/owner-permissions/owners.yaml"
    )
    manager = OwnerManager(nb, repo, RELATIONS, set(), "prod", "tester")
    assert manager.plan("owner-ro", "owner-rw", "owners")["stats"]["ro"]["created"] == 1
