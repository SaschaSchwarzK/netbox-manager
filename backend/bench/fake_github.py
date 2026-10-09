from __future__ import annotations

import base64
import time
from collections import Counter
from types import SimpleNamespace

import yaml
from github import UnknownObjectException


class FakeGithubRepo:
    """PyGithub-compatible subset used by repository helpers in the benchmark."""

    def __init__(self, count: int = 2000, interfaces: int = 48, latency: float = 0.03):
        self.latency = latency
        self.counts = Counter()
        self.owner = SimpleNamespace(login="benchmark")
        self.files = {}
        for index in range(count):
            slug = f"switch-{index:04d}"
            payload = {"manufacturer": "Vendor", "model": f"Switch {index}", "slug": slug,
                       "interfaces": [{"name": f"Ethernet{port}"} for port in range(1, interfaces + 1)]}
            self.files[f"device-types/Vendor/{slug}.yml"] = yaml.safe_dump(payload).encode()

    @property
    def total_requests(self):
        return sum(self.counts.values())

    def _call(self, name):
        time.sleep(self.latency); self.counts[name] += 1

    def get_branch(self, branch):
        self._call("branches"); return SimpleNamespace(commit=SimpleNamespace(sha=f"sha-{branch}"))

    def get_git_tree(self, _sha, recursive=False):
        self._call("git_trees")
        return SimpleNamespace(truncated=False, tree=[SimpleNamespace(type="blob", path=path, sha=f"sha-{i}")
            for i, path in enumerate(self.files)])

    def get_contents(self, path, ref=None):
        self._call("contents")
        if path not in self.files:
            raise UnknownObjectException(404, {"message": "Not Found"}, {})
        raw = self.files[path]
        return SimpleNamespace(path=path, sha="sha", decoded_content=raw,
                               content=base64.b64encode(raw).decode())

    def create_git_ref(self, **_kwargs): self._call("refs")
    def create_file(self, path, _message, content, branch=None):
        self._call("create_file"); self.files[path] = content if isinstance(content, bytes) else content.encode()
        return {"content": SimpleNamespace(sha="created")}
    def get_pulls(self, **_kwargs): self._call("pulls"); return []
    def create_pull(self, **_kwargs): self._call("create_pull"); return SimpleNamespace(number=1, html_url="https://example/pr/1")
