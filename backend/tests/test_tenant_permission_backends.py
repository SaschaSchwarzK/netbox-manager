from types import SimpleNamespace

import responses

from app.services.tenant_permission_backends import GithubPermissionRepo, RequestsNetBox
from app.tenant_permissions import permission_name


@responses.activate
def test_reconcile_creates_updates_then_deletes_managed_permissions():
    base = "https://netbox.example/api"
    responses.get(f"{base}/users/permissions/", json={"results": [
        {"id": 1, "name": permission_name("nbm:prod:42:rw", "dcim.device"), "enabled": True,
         "object_types": ["dcim.device"], "groups": [{"id": 7}], "actions": ["view"], "constraints": {"tenant__id": 1}},
        {"id": 2, "name": "nbm:prod:42:rw:stale:x", "groups": [{"id": 7}], "actions": ["view"]},
    ]})
    responses.post(f"{base}/users/permissions/", json={"id": 3})
    responses.patch(f"{base}/users/permissions/1/", json={"id": 1})
    responses.delete(f"{base}/users/permissions/2/", status=204)
    client = RequestsNetBox("https://netbox.example", "secret", True)
    desired = [
        {"object_type": "dcim.device", "actions": ["view", "change"], "constraints": {"tenant__id": 42}},
        {"object_type": "ipam.prefix", "actions": ["view"], "constraints": {"tenant__id": 42}},
    ]
    result = client.reconcile({"id": 7}, desired, "nbm:prod:42:rw")
    assert result == {"created": 1, "updated": 1, "deleted": 1}
    methods = [call.request.method for call in responses.calls]
    assert methods.index("POST") < methods.index("DELETE")


@responses.activate
def test_delete_group_deletes_owned_exclusive_and_detaches_foreign_or_shared():
    base = "https://netbox.example/api"
    responses.get(f"{base}/users/permissions/", json={"results": [
        {"id": 1, "name": "nbm:prod:42:ro:owned", "groups": [{"id": 7}], "users": []},
        {"id": 2, "name": "foreign", "groups": [{"id": 7}], "users": []},
        {"id": 3, "name": "nbm:prod:42:ro:shared", "groups": [{"id": 7}, {"id": 8}], "users": []},
        {"id": 4, "name": "nbm:prod:42:ro:user-shared", "groups": [{"id": 7}], "users": [{"id": 9}]},
    ]})
    responses.delete(f"{base}/users/permissions/1/", status=204)
    for permission_id in (2, 3, 4):
        responses.patch(f"{base}/users/permissions/{permission_id}/", json={})
    responses.delete(f"{base}/users/groups/7/", status=204)
    client = RequestsNetBox("https://netbox.example", "secret", True)
    result = client.delete_group_and_permissions({"id": 7}, "nbm:prod:42:")
    assert result == {"deleted": 1, "detached": 3}
    patches = [call.request for call in responses.calls if call.request.method == "PATCH"]
    assert len(patches) == 3
    assert b'"groups": []' in patches[0].body
    assert b'"groups": [8]' in patches[1].body


def test_github_permission_repo_reads_base_and_creates_unique_branch(monkeypatch):
    class FakeContent:
        decoded_content = b"name: standard\n"
    class FakeRepo:
        owner = SimpleNamespace(login="org")
        def __init__(self): self.refs = []
        def get_contents(self, path, ref): assert ref == "main"; return FakeContent()
        def get_branch(self, branch): assert branch == "main"; return SimpleNamespace(commit=SimpleNamespace(sha="base"))
        def get_git_tree(self, sha, recursive=False): return SimpleNamespace(tree=[])
        def create_git_tree(self, elements, base): return SimpleNamespace(sha="tree")
        def get_git_commit(self, sha): return SimpleNamespace(sha=sha)
        def create_git_commit(self, title, tree, parents): return SimpleNamespace(sha="commit")
        def create_git_ref(self, ref, sha): self.refs.append(ref)
        def create_pull(self, **kwargs): return SimpleNamespace(number=1, html_url="url")
    fake = FakeRepo()
    monkeypatch.setattr("app.services.tenant_permission_backends._repo", lambda *_: fake)
    repo = GithubPermissionRepo("pat", "org/repo", "main")
    assert repo.read_yaml("templates/standard.yaml") == {"name": "standard"}
    first = repo.write_pr({"x.yaml": {"a": 1}}, "Same title", "body")
    second = repo.write_pr({"x.yaml": {"a": 2}}, "Same title", "body")
    assert first["branch"] != second["branch"]
    assert all(ref.startswith("refs/heads/tenant-permissions/") for ref in fake.refs)
