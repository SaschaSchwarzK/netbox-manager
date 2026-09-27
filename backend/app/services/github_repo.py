from dataclasses import dataclass

import requests
import yaml
from github import BadCredentialsException, Github, GithubException, UnknownObjectException


@dataclass
class RepoFile:
    path: str
    sha: str


class RepoAccessError(Exception):
    """Raised when the configured repo/branch can't be reached with the stored PAT."""


def _repo(pat: str, repo_name: str):
    try:
        return Github(pat).get_repo(repo_name)
    except BadCredentialsException as exc:
        raise RepoAccessError(
            "GitHub rejected this token (bad credentials). Check the PAT hasn't expired or been revoked."
        ) from exc
    except UnknownObjectException as exc:
        raise RepoAccessError(
            f"GitHub returned 404 for repo '{repo_name}'. This means either: the repo name isn't "
            f"exactly 'owner/repo' (no https://, no trailing slash), the PAT isn't scoped to this "
            f"repo, or — if the repo belongs to an org with SSO enforced — the token hasn't been "
            f"authorized for that org yet (GitHub Settings > Developer settings > your token > "
            f"'Configure SSO')."
        ) from exc


def check_token_expiry(pat: str) -> dict:
    """
    GitHub sets a `github-authentication-token-expiration` header on authenticated
    API responses when the token (fine-grained PAT, or a classic PAT with an
    expiration set) has one. We hit the cheap rate_limit endpoint just to read it.
    """
    try:
        resp = requests.get(
            "https://api.github.com/rate_limit",
            headers={"Authorization": f"token {pat}", "Accept": "application/vnd.github+json"},
            timeout=10,
        )
        if resp.status_code != 200:
            return {"known": False, "expires": None, "note": f"Can't check (HTTP {resp.status_code})."}
        header = resp.headers.get("github-authentication-token-expiration")
        if not header:
            return {"known": True, "expires": None, "note": "No expiration set on this token."}
        return {"known": True, "expires": header, "note": None}
    except requests.RequestException as exc:
        return {"known": False, "expires": None, "note": str(exc)}


def base_dir_for_pattern(path_pattern: str) -> str:
    """e.g. "device-types/{manufacturer}/{slug}.yml" -> "device-types" """
    return path_pattern.split("{")[0].rsplit("/", 1)[0] if "{" in path_pattern else path_pattern


def list_device_types(pat: str, repo_name: str, branch: str, base_dir: str) -> list[RepoFile]:
    """List every .yml/.yaml file under base_dir using the git trees API (single call, recursive)."""
    repo = _repo(pat, repo_name)
    branch_ref = repo.get_branch(branch)
    tree = repo.get_git_tree(branch_ref.commit.sha, recursive=True)
    base_dir = base_dir.strip("/")
    files = []
    for entry in tree.tree:
        if entry.type != "blob":
            continue
        if base_dir and not entry.path.startswith(base_dir + "/"):
            continue
        if entry.path.endswith((".yml", ".yaml")):
            files.append(RepoFile(path=entry.path, sha=entry.sha))
    return files


def get_file(pat: str, repo_name: str, branch: str, path: str) -> dict:
    repo = _repo(pat, repo_name)
    content_file = repo.get_contents(path, ref=branch)
    raw = content_file.decoded_content.decode("utf-8")
    parsed = yaml.safe_load(raw) or {}
    return {"path": path, "sha": content_file.sha, "payload": parsed}


def test_connection(pat: str, repo_name: str, branch: str) -> dict:
    try:
        repo = _repo(pat, repo_name)
        repo.get_branch(branch)
        return {"ok": True, "detail": f"Connected to {repo_name} ({'private' if repo.private else 'public'})"}
    except RepoAccessError as exc:
        return {"ok": False, "detail": str(exc)}
    except GithubException as exc:
        if exc.status == 404:
            return {"ok": False, "detail": f"Branch '{branch}' not found in {repo_name}."}
        detail = exc.data.get("message", str(exc)) if isinstance(exc.data, dict) else str(exc)
        return {"ok": False, "detail": f"GitHub API error: {detail}"}


def file_exists(pat: str, repo_name: str, branch: str, path: str) -> bool:
    repo = _repo(pat, repo_name)
    try:
        repo.get_contents(path, ref=branch)
        return True
    except UnknownObjectException:
        return False


def resolve_working_branch(pat: str, repo_name: str, base_branch: str, path: str) -> str:
    """
    If this file already has an open PR branch (from a previous save), return
    that branch so edits continue on the same PR instead of forking a second one.
    Otherwise return the base branch.
    """
    repo = _repo(pat, repo_name)
    feature_branch = _feature_branch_name(path)
    try:
        repo.get_branch(feature_branch)
        return feature_branch
    except GithubException:
        return base_branch


def get_open_pr(pat: str, repo_name: str, base_branch: str, path: str) -> dict | None:
    repo = _repo(pat, repo_name)
    feature_branch = _feature_branch_name(path)
    prs = list(repo.get_pulls(state="open", head=f"{repo.owner.login}:{feature_branch}", base=base_branch))
    if prs:
        return {"number": prs[0].number, "url": prs[0].html_url}
    return None


def _feature_branch_name(path: str) -> str:
    return f"device-type/{path.replace('/', '-')}"


def save_file(
    pat: str,
    repo_name: str,
    branch: str,
    path: str,
    payload: dict,
    commit_message: str,
    sha: str | None = None,
    pr_body: str | None = None,
) -> dict:
    """
    Direct commits to the base branch are not supported: every device-type
    change goes to a per-file feature branch and opens (or reuses, if one is
    already open for this file) a pull request against `branch` for review.
    `sha` is GitHub's optimistic-concurrency check, required when updating a
    file that already exists on the feature branch.
    """
    repo = _repo(pat, repo_name)
    yaml_text = yaml.dump(payload, sort_keys=False, allow_unicode=True)

    target_branch = _feature_branch_name(path)
    base_ref = repo.get_branch(branch)
    try:
        repo.create_git_ref(ref=f"refs/heads/{target_branch}", sha=base_ref.commit.sha)
    except GithubException as exc:
        if exc.status != 422:  # 422 = branch already exists, fine to reuse
            raise

    try:
        if sha:
            result = repo.update_file(path, commit_message, yaml_text, sha, branch=target_branch)
        else:
            result = repo.create_file(path, commit_message, yaml_text, branch=target_branch)
    except GithubException as exc:
        if exc.status == 409:
            raise ValueError(
                "This file changed since you loaded it (on the open PR branch). Reload before "
                "saving to avoid overwriting someone else's edit."
            ) from exc
        raise

    existing = list(repo.get_pulls(state="open", head=f"{repo.owner.login}:{target_branch}", base=branch))
    if existing:
        pr = existing[0]
    else:
        pr = repo.create_pull(
            title=commit_message,
            body=pr_body or "Automated device-type update from NetBox Manager.",
            head=target_branch,
            base=branch,
        )

    return {"path": path, "sha": result["content"].sha, "pr_number": pr.number, "pr_url": pr.html_url}


def get_merged_pr_approval(pat: str, repo_name: str, branch: str, path: str) -> dict:
    """
    Finds the most recent commit that touched `path` on `branch`, looks up the
    pull request(s) associated with that commit, and reports whether a merged
    PR with at least one approving review produced the current file content.
    Used to gate pushes to instances that require reviewed changes.
    """
    repo = _repo(pat, repo_name)
    commits = list(repo.get_commits(path=path, sha=branch))
    if not commits:
        return {"found": False, "approved": False, "pr_number": None, "pr_url": None}

    latest_sha = commits[0].sha
    headers = {"Authorization": f"token {pat}", "Accept": "application/vnd.github+json"}
    resp = requests.get(
        f"https://api.github.com/repos/{repo_name}/commits/{latest_sha}/pulls", headers=headers, timeout=15
    )
    resp.raise_for_status()
    prs = [p for p in resp.json() if p.get("merged_at")]
    if not prs:
        return {"found": False, "approved": False, "pr_number": None, "pr_url": None}

    pr = prs[0]
    reviews_resp = requests.get(
        f"https://api.github.com/repos/{repo_name}/pulls/{pr['number']}/reviews", headers=headers, timeout=15
    )
    reviews_resp.raise_for_status()
    approved = any(r["state"] == "APPROVED" for r in reviews_resp.json())

    return {"found": True, "approved": approved, "pr_number": pr["number"], "pr_url": pr["html_url"]}


def delete_file(pat: str, repo_name: str, branch: str, path: str, sha: str, commit_message: str) -> None:
    repo = _repo(pat, repo_name)
    repo.delete_file(path, commit_message, sha, branch=branch)


def guess_manufacturer_slug(path: str) -> tuple[str | None, str | None]:
    """
    Cheap heuristic for the scan/preview list, before we've fetched any file
    content: the community devicetype-library layout is
    device-types/{Manufacturer}/{slug}.yml, so infer from path segments alone
    rather than fetching every file (which would mean thousands of API calls
    against a repo the size of devicetype-library just to list it).
    """
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None, None
    manufacturer = parts[-2]
    slug = parts[-1].rsplit(".", 1)[0]
    return manufacturer, slug


def bulk_create_files(
    pat: str, repo_name: str, base_branch: str, branch_name: str, files: list[dict], commit_message_prefix: str
) -> dict:
    """
    Commits many files to a single shared branch — one commit per file (GitHub's
    Contents API doesn't support atomic multi-file commits without dropping to
    the low-level Git Data API), but still just ONE branch and, afterward, ONE
    pull request, so a bulk import doesn't flood the repo with dozens of
    separate PRs. Files already present at their destination path are skipped
    rather than overwritten. `files` is a list of {"path": str, "payload": dict}.
    """
    repo = _repo(pat, repo_name)
    base_ref = repo.get_branch(base_branch)
    try:
        repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_ref.commit.sha)
    except GithubException as exc:
        if exc.status != 422:  # 422 = branch already exists, fine to reuse (e.g. resuming a partial import)
            raise

    created, skipped, failed = [], [], []
    for f in files:
        path = f["path"]
        try:
            repo.get_contents(path, ref=branch_name)
            skipped.append(path)
            continue
        except UnknownObjectException:
            pass
        try:
            yaml_text = yaml.dump(f["payload"], sort_keys=False, allow_unicode=True)
            repo.create_file(path, f"{commit_message_prefix}: {path}", yaml_text, branch=branch_name)
            created.append(path)
        except Exception as exc:
            failed.append({"path": path, "error": str(exc)})

    return {"created": created, "skipped": skipped, "failed": failed}


def open_bulk_pr(pat: str, repo_name: str, branch_name: str, base_branch: str, title: str, body: str) -> dict:
    repo = _repo(pat, repo_name)
    existing = list(repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch_name}", base=base_branch))
    if existing:
        pr = existing[0]
    else:
        pr = repo.create_pull(title=title, body=body, head=branch_name, base=base_branch)
    return {"pr_number": pr.number, "pr_url": pr.html_url}
