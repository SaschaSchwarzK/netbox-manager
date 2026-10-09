from dataclasses import dataclass
from collections import OrderedDict
import logging
import hashlib
import mimetypes
import re
import shutil
import tempfile
import threading
import time
from zipfile import ZipFile

import requests
import yaml
from github import BadCredentialsException, Github, GithubException, UnknownObjectException

from app.config import settings


@dataclass
class RepoFile:
    path: str
    sha: str


@dataclass(frozen=True)
class BinaryRepoFile:
    path: str
    content: bytes
    content_type: str


class RepoAccessError(Exception):
    """Raised when the configured repo/branch can't be reached with the stored PAT."""


class ArchiveBusyError(Exception):
    """Raised when the process-wide archive scanner cannot be acquired in time."""


_metadata_cache: OrderedDict[tuple[str, str, str, str], tuple[float, list[dict]]] = OrderedDict()
_repo_cache: OrderedDict[tuple[str, str], tuple[float, object]] = OrderedDict()
_MAX_METADATA_CACHE_ENTRIES = 4
_MAX_REPO_CACHE_ENTRIES = 16
_REPO_CACHE_TTL_SECONDS = 60
_MIB = 1024 * 1024
_ARCHIVE_SPOOL_BYTES = 8 * _MIB
_MAX_YAML_BYTES = 1024 * 1024
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_cache_lock = threading.RLock()
_archive_limit_lock = threading.Lock()
_archive_scan_semaphore = threading.BoundedSemaphore(1)
_effective_archive_max_bytes: int | None = None
_archive_limit_warning_emitted = False
logger = logging.getLogger(__name__)


def _archive_limit_error(limit_bytes: int) -> ValueError:
    limit_mib = limit_bytes / _MIB
    return ValueError(
        f"GitHub archive exceeds effective {limit_mib:g} MiB limit "
        f"(configured by NBM_ARCHIVE_MAX_MIB)"
    )


def _archive_busy_error() -> ArchiveBusyError:
    return ArchiveBusyError(
        "Another GitHub archive scan is running; retry after it finishes "
        "(wait configured by NBM_ARCHIVE_LOCK_TIMEOUT_SECONDS)."
    )


def initialize_archive_limit() -> int:
    """Resolve the safe archive cap once, accounting for the writable tmpfs."""
    global _effective_archive_max_bytes, _archive_limit_warning_emitted
    with _archive_limit_lock:
        if _effective_archive_max_bytes is not None:
            return _effective_archive_max_bytes
        configured = max(0, settings.archive_max_mib) * _MIB
        free = shutil.disk_usage(tempfile.gettempdir()).free
        effective = min(configured, max(0, free - _ARCHIVE_SPOOL_BYTES))
        _effective_archive_max_bytes = effective
        if effective < configured and not _archive_limit_warning_emitted:
            logger.warning(
                "Temporary storage cannot hold the configured GitHub archive cap; "
                "lowering the effective NBM_ARCHIVE_MAX_MIB limit to %.1f MiB.",
                effective / _MIB,
            )
            _archive_limit_warning_emitted = True
        return effective


def validate_source_coordinates(repo_name: str, branch: str) -> None:
    if not _REPO_RE.fullmatch(repo_name) or any(part in (".", "..") for part in repo_name.split("/")):
        raise ValueError("Source repository must be in owner/repository form.")
    if not branch or ".." in branch or branch.startswith("/") or branch.endswith("/"):
        raise ValueError("Invalid source branch name.")


def _text(value) -> str | None:
    return None if value is None else str(value)


def _repo(pat: str, repo_name: str):
    cache_key = (hashlib.sha256(pat.encode()).hexdigest(), repo_name)
    with _cache_lock:
        cached = _repo_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < _REPO_CACHE_TTL_SECONDS:
            _repo_cache.move_to_end(cache_key)
            return cached[1]
    try:
        repo = Github(pat).get_repo(repo_name)
        with _cache_lock:
            _repo_cache[cache_key] = (time.monotonic(), repo)
            _repo_cache.move_to_end(cache_key)
            while len(_repo_cache) > _MAX_REPO_CACHE_ENTRIES:
                _repo_cache.popitem(last=False)
        return repo
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


def rate_limit_remaining(pat: str, repo_name: str) -> int | None:
    """Return GitHub core quota when PyGithub exposes it; callers degrade gracefully otherwise."""
    try:
        repo = _repo(pat, repo_name)
        github = getattr(repo, "_requester", None)
        requester = getattr(github, "requestJsonAndCheck", None)
        if requester is None:
            return None
        _headers, data = requester("GET", "/rate_limit")
        return int(data["resources"]["core"]["remaining"])
    except Exception:
        return None


def base_dir_for_pattern(path_pattern: str) -> str:
    """e.g. "device-types/{manufacturer}/{slug}.yml" -> "device-types" """
    return path_pattern.split("{")[0].rsplit("/", 1)[0] if "{" in path_pattern else path_pattern


def list_device_types(pat: str, repo_name: str, branch: str, base_dir: str) -> list[RepoFile]:
    """List every .yml/.yaml file under base_dir using the git trees API (single call, recursive)."""
    repo = _repo(pat, repo_name)
    branch_ref = repo.get_branch(branch)
    tree = repo.get_git_tree(branch_ref.commit.sha, recursive=True)
    if getattr(tree, "truncated", False):
        raise ValueError("GitHub truncated the repository tree; narrow the configured base directory or source repository")
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


def scan_repository_paths(pat: str, repo_name: str, branch: str, base_dir: str) -> list[dict]:
    """Cheap bulk-import discovery using GitHub's tree API, without downloading the repository archive.

    Detailed YAML metadata is deliberately deferred to the existing per-item preview/import calls.
    This keeps scans viable for repositories whose compressed archive exceeds the manager's memory limit.
    """
    validate_source_coordinates(repo_name, branch)
    rows = []
    for file in list_device_types(pat, repo_name, branch, base_dir):
        manufacturer, filename = guess_manufacturer_slug(file.path)
        rows.append({
            "path": file.path,
            "manufacturer": manufacturer,
            "model": None,
            "slug": filename,
            "part_number": None,
        })
    return rows


def scan_device_type_metadata(pat: str, repo_name: str, branch: str, base_dir: str) -> list[dict]:
    """Download one repository archive and read searchable YAML metadata in memory."""
    validate_source_coordinates(repo_name, branch)
    pat_hash = hashlib.sha256(pat.encode()).hexdigest()[:16]
    repo = _repo(pat, repo_name)
    head_sha = repo.get_branch(branch).commit.sha
    cache_key = (repo_name, head_sha, base_dir.strip("/"), pat_hash)
    with _cache_lock:
        cached = _metadata_cache.get(cache_key)
        if cached:
            _metadata_cache.move_to_end(cache_key)
            return cached[1]
    acquired = _archive_scan_semaphore.acquire(timeout=max(0, settings.archive_lock_timeout_seconds))
    if not acquired:
        raise _archive_busy_error()
    try:
        # A concurrent caller may have populated this entry while we waited.
        with _cache_lock:
            cached = _metadata_cache.get(cache_key)
            if cached:
                _metadata_cache.move_to_end(cache_key)
                return cached[1]
        headers = {"Accept": "application/vnd.github+json"}
        if pat:
            headers["Authorization"] = f"token {pat}"
        response = requests.get(
            f"https://api.github.com/repos/{repo_name}/zipball/{head_sha}",
            headers=headers,
            stream=True,
            timeout=90,
        )
        try:
            response.raise_for_status()
            archive_limit = initialize_archive_limit()
            content_length = response.headers.get("Content-Length")
            try:
                declared_length = int(content_length) if content_length is not None else None
            except (TypeError, ValueError):
                declared_length = None
            if declared_length is not None and declared_length > archive_limit:
                raise _archive_limit_error(archive_limit)
            base_dir = base_dir.strip("/")
            results = []
            with tempfile.SpooledTemporaryFile(max_size=_ARCHIVE_SPOOL_BYTES) as archive_file:
                downloaded = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > archive_limit:
                        raise _archive_limit_error(archive_limit)
                    try:
                        archive_file.write(chunk)
                    except OSError as exc:
                        raise _archive_limit_error(archive_limit) from exc
                archive_file.seek(0)
                with ZipFile(archive_file) as archive:
                    uncompressed_read = 0
                    for info in archive.infolist():
                        name = info.filename
                        parts = name.split("/", 1)
                        if len(parts) != 2:
                            continue
                        path = parts[1]
                        if not path.endswith((".yml", ".yaml")) or (
                            base_dir and not path.startswith(base_dir + "/")
                        ):
                            continue
                        if info.file_size > _MAX_YAML_BYTES:
                            continue
                        uncompressed_read += info.file_size
                        if uncompressed_read > archive_limit:
                            raise _archive_limit_error(archive_limit)
                        try:
                            payload = yaml.safe_load(archive.read(info)) or {}
                        except (yaml.YAMLError, UnicodeDecodeError):
                            continue
                        if not isinstance(payload, dict):
                            continue
                        results.append({
                            "path": path,
                            "manufacturer": _text(payload.get("manufacturer")),
                            "model": _text(payload.get("model")),
                            "slug": _text(payload.get("slug")),
                            "part_number": _text(payload.get("part_number")),
                        })
        finally:
            response.close()
    finally:
        _archive_scan_semaphore.release()
    with _cache_lock:
        _metadata_cache[cache_key] = (time.monotonic(), results)
        _metadata_cache.move_to_end(cache_key)
        while len(_metadata_cache) > _MAX_METADATA_CACHE_ENTRIES:
            _metadata_cache.popitem(last=False)
    return results


def _invalidate_metadata(repo_name: str) -> None:
    with _cache_lock:
        for key in [key for key in _metadata_cache if key[0] == repo_name]:
            _metadata_cache.pop(key, None)


def get_file(pat: str, repo_name: str, branch: str, path: str) -> dict:
    repo = _repo(pat, repo_name)
    content_file = repo.get_contents(path, ref=branch)
    raw = content_file.decoded_content.decode("utf-8")
    parsed = yaml.safe_load(raw) or {}
    return {"path": path, "sha": content_file.sha, "payload": parsed}


def get_binary_file(pat: str, repo_name: str, branch: str, path: str) -> BinaryRepoFile:
    """Fetch a repository blob without applying the YAML/UTF-8 text path."""
    repo = _repo(pat, repo_name)
    content_file = repo.get_contents(path, ref=branch)
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return BinaryRepoFile(path=path, content=content_file.decoded_content, content_type=content_type)


def get_elevation_image(
    pat: str, repo_name: str, branch: str, device_type_path: str, slug: str, side: str,
) -> BinaryRepoFile | None:
    """Resolve the devicetype-library's flat elevation-images/<vendor>/ convention."""
    if side not in ("front", "rear"):
        raise ValueError(f"Unsupported elevation image side: {side}")
    parts = [part for part in device_type_path.split("/") if part]
    if len(parts) < 2:
        raise ValueError(f"Cannot derive manufacturer folder from {device_type_path!r}")
    manufacturer_folder = parts[-2]
    try:
        device_types_index = parts.index("device-types")
        prefix = parts[:device_types_index]
    except ValueError:
        prefix = parts[:-2]
    image_dir = "/".join([*prefix, "elevation-images", manufacturer_folder])
    repo = _repo(pat, repo_name)
    try:
        entries = repo.get_contents(image_dir, ref=branch)
    except UnknownObjectException:
        return None
    # Canonical library names use the YAML slug. Older contributions sometimes
    # use the YAML filename/model instead. Prefer the canonical name when both
    # exist, as with Cisco N9K-C9396TX, and use the legacy name only as fallback.
    yaml_stem = parts[-1].rsplit(".", 1)[0]
    for candidate in dict.fromkeys((slug, yaml_stem)):
        expected_prefix = f"{candidate}.{side}."
        matches = [entry for entry in entries if entry.type == "file" and entry.name.startswith(expected_prefix)]
        if len(matches) > 1:
            raise ValueError(f"Multiple {side} elevation images found for {candidate} in {image_dir}")
        if matches:
            return get_binary_file(pat, repo_name, branch, matches[0].path)
    return None


def elevation_image_destination(device_type_path: str, slug: str, side: str, source_name: str) -> str:
    """Build the destination library path while preserving the source extension."""
    suffix = source_name.rsplit(".", 1)[-1].lower() if "." in source_name else "bin"
    parts = [part for part in device_type_path.split("/") if part]
    manufacturer_folder = parts[-2]
    try:
        device_types_index = parts.index("device-types")
        prefix = parts[:device_types_index]
    except ValueError:
        prefix = parts[:-2]
    return "/".join([*prefix, "elevation-images", manufacturer_folder, f"{slug}.{side}.{suffix}"])


def get_module_images(
    pat: str, repo_name: str, branch: str, module_type_path: str,
) -> dict[str, BinaryRepoFile]:
    """Return at most one front and rear image from flat module-images/<manufacturer>."""
    parts = [part for part in module_type_path.split("/") if part]
    manufacturer_folder = parts[-2]
    module_name = parts[-1].rsplit(".", 1)[0]
    try:
        module_types_index = parts.index("module-types")
        prefix = parts[:module_types_index]
    except ValueError:
        prefix = parts[:-2]
    image_dir = "/".join([*prefix, "module-images", manufacturer_folder])
    repo = _repo(pat, repo_name)
    try:
        entries = repo.get_contents(image_dir, ref=branch)
    except UnknownObjectException:
        return {}
    images = {}
    for side in ("front", "rear"):
        expected_prefix = f"{module_name}.{side}."
        matches = [entry for entry in entries if entry.type == "file" and entry.name.startswith(expected_prefix)]
        if len(matches) > 1:
            raise ValueError(f"Multiple {side} images found for module type {module_name}; maximum is one")
        if matches:
            images[side] = get_binary_file(pat, repo_name, branch, matches[0].path)
    if len(images) > 2:
        raise ValueError(f"Module type {module_name} has more than two images")
    return images


def module_image_destination(module_type_path: str, side: str, source_name: str) -> str:
    suffix = source_name.rsplit(".", 1)[-1].lower() if "." in source_name else "bin"
    parts = [part for part in module_type_path.split("/") if part]
    manufacturer_folder = parts[-2]
    module_name = parts[-1].rsplit(".", 1)[0]
    try:
        module_types_index = parts.index("module-types")
        prefix = parts[:module_types_index]
    except ValueError:
        prefix = parts[:-2]
    return "/".join([*prefix, "module-images", manufacturer_folder, f"{module_name}.{side}.{suffix}"])


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
    normalized = f"/{path}"
    if "/module-types/" in normalized:
        prefix = "module-type"
    elif "/rack-types/" in normalized:
        prefix = "rack-type"
    else:
        prefix = "device-type"
    digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:8]
    # Git refs reject whitespace, control characters and a small set of
    # punctuation. Flatten the repository path while retaining readable text;
    # the digest makes otherwise-equivalent sanitized paths unambiguous.
    name = re.sub(r"[\x00-\x20\x7f~^:?*\[\\/]+", "-", path)
    name = name.replace("@{", "-")
    while ".." in name:
        name = name.replace("..", ".")
    name = name.strip("./-")
    if name.endswith(".lock"):
        name = name[:-5].rstrip(".") + "-lock"
    suffix = f"-{digest}"
    max_name_length = 240 - len(prefix) - 1
    name = name[: max_name_length - len(suffix)].rstrip(".")
    return f"{prefix}/{name or 'file'}{suffix}"


def _ensure_branch(repo, branch: str, base_sha: str) -> None:
    """Create a branch, reusing it only when GitHub confirms it exists."""
    try:
        repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)
    except GithubException as exc:
        if exc.status != 422:
            raise
        try:
            repo.get_branch(branch)
        except GithubException:
            raise exc


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
    _ensure_branch(repo, target_branch, base_ref.commit.sha)

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

    _invalidate_metadata(repo_name)
    return {"path": path, "sha": result["content"].sha, "pr_number": pr.number, "pr_url": pr.html_url}


def save_binary_file(pat: str, repo_name: str, base_branch: str, path: str, content: bytes,
                     commit_message: str, pr_body: str | None = None,
                     feature_source_path: str | None = None) -> dict:
    """Create or replace a binary file on the type's normal feature branch and PR."""
    repo = _repo(pat, repo_name); target_branch = _feature_branch_name(feature_source_path or path)
    base_ref = repo.get_branch(base_branch)
    _ensure_branch(repo, target_branch, base_ref.commit.sha)
    try:
        current = repo.get_contents(path, ref=target_branch)
        result = repo.update_file(path, commit_message, content, current.sha, branch=target_branch)
    except UnknownObjectException:
        result = repo.create_file(path, commit_message, content, branch=target_branch)
    existing = list(repo.get_pulls(state="open", head=f"{repo.owner.login}:{target_branch}", base=base_branch))
    pr = existing[0] if existing else repo.create_pull(
        title=commit_message, body=pr_body or "Automated image update from NetBox Manager.",
        head=target_branch, base=base_branch,
    )
    _invalidate_metadata(repo_name)
    return {"path": path, "sha": result["content"].sha, "pr_number": pr.number, "pr_url": pr.html_url}


def delete_binary_file(pat: str, repo_name: str, base_branch: str, path: str,
                       commit_message: str, pr_body: str | None = None,
                       feature_source_path: str | None = None) -> dict:
    repo = _repo(pat, repo_name); target_branch = _feature_branch_name(feature_source_path or path)
    base_ref = repo.get_branch(base_branch)
    _ensure_branch(repo, target_branch, base_ref.commit.sha)
    current = repo.get_contents(path, ref=target_branch)
    repo.delete_file(path, commit_message, current.sha, branch=target_branch)
    existing = list(repo.get_pulls(state="open", head=f"{repo.owner.login}:{target_branch}", base=base_branch))
    pr = existing[0] if existing else repo.create_pull(
        title=commit_message, body=pr_body or "Automated image removal from NetBox Manager.",
        head=target_branch, base=base_branch,
    )
    _invalidate_metadata(repo_name)
    return {"path": path, "sha": current.sha, "pr_number": pr.number, "pr_url": pr.html_url}


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
    _invalidate_metadata(repo_name)


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
    rather than overwritten. Each item contains either a YAML `payload` or raw
    binary `content`.
    """
    repo = _repo(pat, repo_name)
    base_ref = repo.get_branch(base_branch)
    _ensure_branch(repo, branch_name, base_ref.commit.sha)
    branch_ref = repo.get_branch(branch_name)
    tree = repo.get_git_tree(branch_ref.commit.sha, recursive=True)
    if getattr(tree, "truncated", False):
        raise ValueError("GitHub truncated the feature-branch tree; bulk import cannot safely detect existing files")
    existing_paths = {entry.path for entry in tree.tree if entry.type == "blob"}

    created, skipped, failed = [], [], []
    for f in files:
        path = f["path"]
        if path in existing_paths:
            skipped.append(path)
            continue
        try:
            content = f.get("content")
            if content is None:
                content = yaml.dump(f["payload"], sort_keys=False, allow_unicode=True)
            repo.create_file(path, f"{commit_message_prefix}: {path}", content, branch=branch_name)
            created.append(path)
        except Exception as exc:
            failed.append({"path": path, "error": str(exc)})

    if created:
        _invalidate_metadata(repo_name)
    return {"created": created, "skipped": skipped, "failed": failed}


def open_bulk_pr(pat: str, repo_name: str, branch_name: str, base_branch: str, title: str, body: str) -> dict:
    repo = _repo(pat, repo_name)
    existing = list(repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch_name}", base=base_branch))
    if existing:
        pr = existing[0]
    else:
        pr = repo.create_pull(title=title, body=body, head=branch_name, base=base_branch)
    return {"pr_number": pr.number, "pr_url": pr.html_url}
