"""Concrete NetBox REST and GitHub backends for tenant permission automation."""

from __future__ import annotations

import uuid
from typing import Any

import requests
import yaml
from github import GithubException, UnknownObjectException

from app.services.github_repo import _repo
from app.tenant_permissions import TenantPermissionError, permission_name


class RequestsNetBox:
    def __init__(self, base_url: str, token: str, verify_ssl: bool):
        self.base = base_url.rstrip("/") + "/api"
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Token {token}", "Content-Type": "application/json"})
        self.session.verify = verify_ssl

    def _request(self, method: str, path: str, **kwargs):
        response = self.session.request(method, self.base + path, timeout=30, **kwargs)
        if not response.ok:
            raise TenantPermissionError(f"NetBox {method} {path} failed: HTTP {response.status_code}: {response.text[:500]}")
        return response.json() if response.content else None

    def tenants(self, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if query.strip():
            params["q"] = query.strip()
        return self._request("GET", "/tenancy/tenants/", params=params)["results"]

    def tenant(self, tenant_id: int) -> dict[str, Any]:
        try:
            return self._request("GET", f"/tenancy/tenants/{tenant_id}/")
        except TenantPermissionError as exc:
            raise TenantPermissionError(f"Tenant ID {tenant_id} was not found") from exc

    def group(self, name: str) -> dict[str, Any] | None:
        rows = self._request("GET", "/users/groups/", params={"name": name, "limit": 2})["results"]
        exact = [row for row in rows if row["name"] == name]
        return exact[0] if exact else None

    def create_group(self, name: str) -> dict[str, Any]:
        return self._request("POST", "/users/groups/", json={"name": name})

    def group_member_count(self, group_id: int) -> int:
        value = self._request("GET", f"/users/groups/{group_id}/")
        users = value.get("user_count", value.get("users"))
        if isinstance(users, int):
            return users
        if isinstance(users, list):
            return len(users)
        # NetBox supports group_id filtering on users; limit=1 keeps this cheap.
        return int(self._request("GET", "/users/users/", params={"group_id": group_id, "limit": 1})["count"])

    @staticmethod
    def _ids(values) -> list[int]:
        return sorted(int(v["id"] if isinstance(v, dict) else v) for v in (values or []))

    def reconcile(self, group: dict[str, Any], desired: list[dict[str, Any]], key: str) -> dict[str, int]:
        rows = self._request("GET", "/users/permissions/", params={"group_id": group["id"], "limit": 1000})["results"]
        managed = {row["name"]: row for row in rows if row["name"].startswith(key + ":")}
        wanted = {}
        for item in desired:
            name = permission_name(key, item["object_type"])
            wanted[name] = {
                "name": name, "enabled": True, "object_types": [item["object_type"]],
                "groups": [group["id"]], "actions": item["actions"], "constraints": item["constraints"],
            }
        created = updated = deleted = 0
        # Add/update before delete, avoiding a fully-unpermissioned transition.
        for name, payload in wanted.items():
            current = managed.get(name)
            if current is None:
                self._request("POST", "/users/permissions/", json=payload); created += 1
                continue
            comparable = {"name": current["name"], "enabled": current.get("enabled", True),
                          "object_types": sorted(current.get("object_types", [])),
                          "groups": self._ids(current.get("groups")),
                          "actions": sorted(current.get("actions", [])), "constraints": current.get("constraints") or {}}
            compare_payload = {**payload, "object_types": sorted(payload["object_types"]),
                               "groups": sorted(payload["groups"]), "actions": sorted(payload["actions"])}
            if comparable != compare_payload:
                self._request("PATCH", f"/users/permissions/{current['id']}/", json=payload); updated += 1
        for name, current in managed.items():
            if name not in wanted:
                self._request("DELETE", f"/users/permissions/{current['id']}/"); deleted += 1
        return {"created": created, "updated": updated, "deleted": deleted}

    def plan_reconcile(self, group: dict[str, Any] | None, desired: list[dict[str, Any]], key: str) -> dict[str, int]:
        if group is None:
            return {"created": len(desired), "updated": 0, "deleted": 0}
        rows = self._request("GET", "/users/permissions/", params={"group_id": group["id"], "limit": 1000})["results"]
        managed = {row["name"]: row for row in rows if row["name"].startswith(key + ":")}
        wanted = {}
        for item in desired:
            name = permission_name(key, item["object_type"])
            wanted[name] = {
                "name": name, "enabled": True, "object_types": sorted([item["object_type"]]),
                "groups": [int(group["id"])], "actions": sorted(item["actions"]),
                "constraints": item["constraints"],
            }
        updated = 0
        for name, payload in wanted.items():
            current = managed.get(name)
            if current is None:
                continue
            comparable = {
                "name": current["name"], "enabled": current.get("enabled", True),
                "object_types": sorted(current.get("object_types", [])), "groups": self._ids(current.get("groups")),
                "actions": sorted(current.get("actions", [])), "constraints": current.get("constraints") or {},
            }
            if comparable != payload:
                updated += 1
        return {
            "created": len(set(wanted) - set(managed)),
            "updated": updated,
            "deleted": len(set(managed) - set(wanted)),
        }

    def delete_group_and_permissions(self, group: dict[str, Any], key_prefix: str) -> dict[str, int]:
        rows = self._request("GET", "/users/permissions/", params={"group_id": group["id"], "limit": 1000})["results"]
        deleted = detached = 0
        for row in rows:
            group_ids = self._ids(row.get("groups"))
            user_ids = self._ids(row.get("users"))
            remaining_groups = [value for value in group_ids if value != int(group["id"])]
            owned = row["name"].startswith(key_prefix)
            if owned and not remaining_groups and not user_ids:
                self._request("DELETE", f"/users/permissions/{row['id']}/")
                deleted += 1
            else:
                # Preserve foreign/shared permissions; remove only this group association.
                self._request("PATCH", f"/users/permissions/{row['id']}/", json={"groups": remaining_groups})
                detached += 1
        self._request("DELETE", f"/users/groups/{group['id']}/")
        return {"deleted": deleted, "detached": detached}


class GithubPermissionRepo:
    """Multi-file PR writer using one Git tree/commit, never the base branch."""
    def __init__(self, pat: str, repo_name: str, base_branch: str):
        self.repo = _repo(pat, repo_name)
        self.base_branch = base_branch

    def read_yaml(self, path: str) -> dict[str, Any] | None:
        try:
            content = self.repo.get_contents(path, ref=self.base_branch)
        except UnknownObjectException:
            return None
        return yaml.safe_load(content.decoded_content.decode()) or {}

    def list_paths(self, prefix: str) -> list[str]:
        tree = self.repo.get_git_tree(self.repo.get_branch(self.base_branch).commit.sha, recursive=True)
        return [item.path for item in tree.tree if item.type == "blob" and item.path.startswith(prefix)]

    def write_pr(self, changes: dict[str, dict[str, Any] | None], title: str, body: str) -> dict[str, Any]:
        base_commit = self.repo.get_branch(self.base_branch).commit
        base_tree = self.repo.get_git_tree(base_commit.sha)
        elements = []
        from github import InputGitTreeElement
        for path, payload in changes.items():
            content = None if payload is None else yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
            if payload is None:
                elements.append(InputGitTreeElement(path=path, mode="100644", type="blob", sha=None))
            else:
                elements.append(InputGitTreeElement(path=path, mode="100644", type="blob", content=content))
        tree = self.repo.create_git_tree(elements, base_tree)
        commit = self.repo.create_git_commit(title, tree, [self.repo.get_git_commit(base_commit.sha)])
        slug = "-".join(title.lower().split())[:40].strip("-") or "change"
        branch = f"tenant-permissions/{slug}-{uuid.uuid4().hex[:8]}"
        self.repo.create_git_ref(f"refs/heads/{branch}", commit.sha)
        pr = self.repo.create_pull(title=title, body=body, head=branch, base=self.base_branch)
        return {"number": pr.number, "url": pr.html_url, "branch": branch}
