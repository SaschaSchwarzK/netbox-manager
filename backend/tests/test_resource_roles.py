from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import models
from app.rbac import AccessContext, require_resource_role, role_for_resource


class FakeDb:
    def __init__(self, resources):
        self.resources = resources

    def get(self, model, resource_id):
        return self.resources.get((model, resource_id))

    def query(self, model):
        mappings = []
        for context in _CONTEXTS:
            mappings.extend(context.mappings)
        return FakeQuery(mappings)


class FakeQuery:
    def __init__(self, values): self.values = values
    def filter(self, *args): return self
    def all(self): return self.values


def mapping(group, role, resource_type, resource_id):
    return SimpleNamespace(oidc_group=group, role=role, resource_type=resource_type, resource_id=resource_id)


_CONTEXTS = []


def test_editor_role_is_limited_to_its_mapped_resource(monkeypatch):
    ctx = AccessContext(role="editor", groups=["team"], app_admin=False, mappings=[
        mapping("team", "viewer", "*", "*"),
        mapping("team", "editor", "instance", "A"),
    ])
    _CONTEXTS[:] = [ctx]
    resources = {(models.NetboxInstance, name): SimpleNamespace(id=name) for name in ("A", "B")}
    db = FakeDb(resources)
    assert role_for_resource(ctx, "instance", "A") == "editor"
    require_resource_role(ctx, "instance", "A", "editor", db)
    with pytest.raises(HTTPException) as exc:
        require_resource_role(ctx, "instance", "B", "editor", db)
    assert exc.value.status_code == 403


def test_app_admin_can_edit_any_visible_resource():
    ctx = AccessContext(role="admin", app_admin=True, scoping_active=False)
    _CONTEXTS[:] = [ctx]
    db = FakeDb({(models.NetboxInstance, "B"): SimpleNamespace(id="B")})
    require_resource_role(ctx, "instance", "B", "editor", db)
