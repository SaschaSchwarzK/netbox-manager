"""
Drift detection, tracked as DriftRecord rows distinguished by `kind`:

  - "device_type": a device-type YAML file vs. the matching device type on a
    NetBox instance.
  - "custom_fields": a repo target's custom-fields template vs. what's
    actually defined on a NetBox instance.

Candidate (instance, repo_target, file_path) pairs for device types come from
two sources, so drift catches both:

  1. Known pairs — device types NetBox Manager itself has successfully pushed
     before (from DeviceTypePushHistory). This also catches one that's since
     been deleted from the instance ("missing").
  2. Discovered pairs — device types that exist on an instance and share a
     (manufacturer, slug) with a file in the repo, whether or not NetBox
     Manager ever put it there. This is what catches a device type created
     directly in NetBox by hand: as long as its manufacturer + slug match a
     file already committed to the repo, it gets checked like any other.

Custom-fields candidates are simpler: every (instance, repo target)
combination where the repo target actually has a custom-fields template
committed, checked against every configured instance regardless of push
history (there's no "push" concept to key off of for the template as a
whole — a field added straight to NetBox is exactly what this should catch).
"""
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app import crypto, models, stats
from app.config import settings
from app.services import diff as diff_mod
from app.devicetype_schema import DeviceType
from app.services import github_repo, netbox_client, netbox_customfields
from app.timeutil import as_utc_aware, utcnow

_MAX_WORKERS = 8
logger = logging.getLogger(__name__)


@dataclass
class DriftRunStats:
    instances: int = 0
    pairs: int = 0
    reused: int = 0
    full_checks: int = 0
    marker_list_failures: int = 0
    bulk_read_failures: int = 0
    changelog_unknown: int = 0
    fallback_pair_lookups: int = 0
    normalized_marker_matches: int = 0
    source_fetches: int = 0
    rate_limited_targets: int = 0
    degraded: bool = False


def _normalized_marker_key(manufacturer: str, slug: str) -> tuple[str, str]:
    normalize = lambda value: " ".join(value.split()).casefold()
    return normalize(manufacturer), normalize(slug)


def _safe_exception_message(exc: Exception) -> str:
    message = str(exc).splitlines()[0][:240]
    message = re.sub(r"https?://[^\s]+", "<url>", message)
    message = re.sub(r"(?i)(authorization|token|pat)\s*[:=]\s*\S+", r"\1=<redacted>", message)
    return message or "operation failed"


def _warn_instance_failure(kind: str, instance, exc: Exception) -> None:
    logger.warning(
        "Drift %s failed for instance %s (%s): %s: %s",
        kind, instance.id, getattr(instance, "name", instance.id),
        type(exc).__name__, _safe_exception_message(exc),
    )


def _run_is_degraded(run_stats: DriftRunStats) -> bool:
    return bool(
        run_stats.marker_list_failures or run_stats.bulk_read_failures or run_stats.changelog_unknown
        or run_stats.fallback_pair_lookups > run_stats.pairs * 0.10
    )


def _repo_device_types(target: models.GithubTarget) -> dict[tuple[str, str], str]:
    """
    {(manufacturer, slug): file_path} for every device-type file in this repo
    target, using each file's actual declared manufacturer/slug (not guessed
    from its path), so it matches how NetBox itself identifies a device type.
    """
    try:
        pat = crypto.decrypt(target.pat_encrypted)
        remaining = github_repo.rate_limit_remaining(pat, target.repo)
        if remaining is not None and remaining < 100:
            return {}
        base_dir = github_repo.base_dir_for_pattern(target.path_pattern)
        metadata = github_repo.scan_device_type_metadata(pat, target.repo, target.branch, base_dir)
        return {(item["manufacturer"], item["slug"]): item["path"] for item in metadata
                if item.get("manufacturer") and item.get("slug")}
    except Exception:
        try:
            files = github_repo.list_device_types(
                pat, target.repo, target.branch, github_repo.base_dir_for_pattern(target.path_pattern)
            )
        except Exception:
            return {}
    result: dict[tuple[str, str], str] = {}
    for f in files:
        manufacturer, slug = github_repo.guess_manufacturer_slug(f.path)
        if manufacturer and slug:
            result[(manufacturer, slug)] = f.path
    return result


def _instance_device_types(instance: models.NetboxInstance) -> set[tuple[str, str]]:
    try:
        token = crypto.decrypt(instance.api_token_encrypted)
        entries = netbox_client.list_device_types_on_instance(instance.base_url, token, netbox_client.verify_for_instance(instance))
    except Exception:
        return set()
    return {(e["manufacturer"], e["slug"]) for e in entries if e.get("manufacturer") and e.get("slug")}


def _has_custom_fields_template(target: models.GithubTarget) -> bool:
    try:
        pat = crypto.decrypt(target.pat_encrypted)
        return github_repo.file_exists(pat, target.repo, target.branch, target.custom_fields_path)
    except Exception:
        return False


def device_type_candidate_pairs(db: Session) -> set[tuple[str, str, str]]:
    """Returns distinct (instance_id, repo_target_id, file_path) tuples worth checking."""
    pairs: set[tuple[str, str, str]] = set()

    instances = db.query(models.NetboxInstance).all()
    targets = db.query(models.GithubTarget).all()
    instances_by_name = {i.name: i for i in instances}

    # 1) Known pairs, from push history.
    rows = (
        db.query(models.DeviceTypePushHistory.target_name, models.DeviceTypePushHistory.repo_target_id,
                  models.DeviceTypePushHistory.file_path)
        .filter(models.DeviceTypePushHistory.target_type == "netbox", models.DeviceTypePushHistory.status == "success")
        .distinct()
        .all()
    )
    for target_name, repo_target_id, file_path in rows:
        instance = instances_by_name.get(target_name)
        if instance:
            pairs.add((instance.id, repo_target_id, file_path))

    # 2) Discovered pairs: match what's actually in each repo against what's
    #    actually on each instance. Both lookups are parallelized since they're
    #    independent, cheap-ish (one git-tree call + one get_file per file for
    #    a repo target; one API call for an instance) network calls.
    if instances and targets:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
            repo_futures = {executor.submit(_repo_device_types, t): t for t in targets}
            instance_futures = {executor.submit(_instance_device_types, i): i for i in instances}
            repo_maps = {repo_futures[f]: f.result() for f in as_completed(repo_futures)}
            instance_sets = {instance_futures[f]: f.result() for f in as_completed(instance_futures)}

        for target, repo_map in repo_maps.items():
            if not repo_map:
                continue
            for instance, present in instance_sets.items():
                for key, file_path in repo_map.items():
                    if key in present:
                        pairs.add((instance.id, target.id, file_path))

    return pairs


def custom_fields_candidate_pairs(db: Session) -> set[tuple[str, str]]:
    """Every (instance_id, repo_target_id) where the repo target has a custom-fields template committed."""
    instances = db.query(models.NetboxInstance).all()
    targets = db.query(models.GithubTarget).all()
    if not instances or not targets:
        return set()

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {executor.submit(_has_custom_fields_template, t): t for t in targets}
        targets_with_template = [futures[f] for f in as_completed(futures) if f.result()]

    return {(i.id, t.id) for i in instances for t in targets_with_template}


def _upsert(db: Session, instance_id: str, repo_target_id: str, kind: str, file_path: str,
            status: str, detail: dict, commit: bool = True) -> models.DriftRecord:
    existing_record = (
        db.query(models.DriftRecord)
        .filter_by(instance_id=instance_id, repo_target_id=repo_target_id, kind=kind, file_path=file_path)
        .first()
    )
    if existing_record:
        existing_record.status = status
        existing_record.detail_json = json.dumps(detail)
        existing_record.checked_at = utcnow()
        record = existing_record
    else:
        record = models.DriftRecord(
            instance_id=instance_id, repo_target_id=repo_target_id, kind=kind, file_path=file_path,
            status=status, detail_json=json.dumps(detail),
        )
        db.add(record)
    if commit:
        db.commit()
        db.refresh(record)
    return record


def check_device_type_pair(db: Session, instance_id: str, repo_target_id: str, file_path: str) -> models.DriftRecord:
    instance = db.get(models.NetboxInstance, instance_id)
    target = db.get(models.GithubTarget, repo_target_id)

    status = "error"
    detail: dict = {}

    if not instance or not target:
        detail = {"error": "Instance or GitHub target no longer exists."}
    else:
        try:
            pat = crypto.decrypt(target.pat_encrypted)
            source = DeviceType(**github_repo.get_file(pat, target.repo, target.branch, file_path)["payload"]).to_internal_dict()
            token = crypto.decrypt(instance.api_token_encrypted)
            existing = netbox_client.get_existing_device_type(
                instance.base_url, token, netbox_client.verify_for_instance(instance), source["manufacturer"], source["slug"]
            )
            result = diff_mod.diff_payloads(source, existing)
            status = result["status"]
            detail = result
        except Exception as exc:
            status = "error"
            detail = {"error": str(exc)}

    return _upsert(db, instance_id, repo_target_id, "device_type", file_path, status, detail)


def check_custom_fields_pair(db: Session, instance_id: str, repo_target_id: str) -> models.DriftRecord:
    instance = db.get(models.NetboxInstance, instance_id)
    target = db.get(models.GithubTarget, repo_target_id)

    status = "error"
    detail: dict = {}
    file_path = target.custom_fields_path if target else "(custom-fields template)"

    if not instance or not target:
        detail = {"error": "Instance or GitHub target no longer exists."}
    else:
        try:
            pat = crypto.decrypt(target.pat_encrypted)
            if github_repo.file_exists(pat, target.repo, target.branch, target.custom_fields_path):
                template = github_repo.get_file(pat, target.repo, target.branch, target.custom_fields_path)["payload"] or {}
            else:
                template = {"custom_fields": [], "custom_field_choice_sets": []}
            token = crypto.decrypt(instance.api_token_encrypted)
            existing = netbox_customfields.get_existing_custom_fields(instance.base_url, token, netbox_client.verify_for_instance(instance))
            result = diff_mod.diff_custom_fields_template(template, existing)
            status = result["status"]
            detail = result
        except Exception as exc:
            status = "error"
            detail = {"error": str(exc)}

    return _upsert(db, instance_id, repo_target_id, "custom_fields", file_path, status, detail)


def run_full_check(db: Session) -> int:
    """
    Checks every known device-type pair and every custom-fields
    template/instance pair. Returns how many were checked.

    Only the read-only discovery step above (candidate_pairs) is
    parallelized — it doesn't touch the database. The actual checks below
    share this one Session, which SQLAlchemy Sessions aren't safe to use
    concurrently from multiple threads, so those run one at a time.
    """
    device_type_pairs = device_type_candidate_pairs(db)
    cf_pairs = custom_fields_candidate_pairs(db)
    run_stats = DriftRunStats(pairs=len(device_type_pairs))
    stats_lock = threading.Lock()

    instances = {item.id: SimpleNamespace(id=item.id, name=item.name, base_url=item.base_url,
        api_token_encrypted=item.api_token_encrypted, verify_ssl=item.verify_ssl,
        ca_bundle_pem=item.ca_bundle_pem) for item in db.query(models.NetboxInstance).all()}
    run_stats.instances = len(instances)
    targets = {item.id: SimpleNamespace(id=item.id, repo=item.repo, branch=item.branch,
        path_pattern=item.path_pattern, pat_encrypted=item.pat_encrypted)
        for item in db.query(models.GithubTarget).all()}
    previous = {(item.instance_id, item.repo_target_id, item.file_path):
                (item.status, json.loads(item.detail_json or "{}"), item.checked_at)
                for item in db.query(models.DriftRecord).filter_by(kind="device_type").all()}

    # One paginated device-type listing per instance replaces two marker calls
    # per pair. Missing entries still use the point lookup below so a partial
    # or stale listing can never incorrectly skip a full drift check.
    marker_maps = {}
    normalized_marker_maps = {}

    def fetch_markers(instance_id, instance):
        token = crypto.decrypt(instance.api_token_encrypted)
        return instance_id, netbox_client.list_device_type_markers(
            instance.base_url, token, netbox_client.verify_for_instance(instance),
        )

    with ThreadPoolExecutor(max_workers=max(1, settings.drift_workers)) as executor:
        futures = {executor.submit(fetch_markers, *item): item[0] for item in instances.items()}
        for future in as_completed(futures):
            try:
                instance_id, markers = future.result()
                marker_maps[instance_id] = markers
                normalized = {}
                ambiguous = set()
                for key, value in markers.items():
                    normalized_key = _normalized_marker_key(*key)
                    if normalized_key in normalized:
                        ambiguous.add(normalized_key)
                        normalized.pop(normalized_key, None)
                    elif normalized_key not in ambiguous:
                        normalized[normalized_key] = value
                normalized_marker_maps[instance_id] = normalized
            except Exception as exc:
                instance_id = futures[future]
                run_stats.marker_list_failures += 1
                _warn_instance_failure("marker listing", instances[instance_id], exc)

    changelog_changed: dict[str, bool | None] = {}
    if settings.drift_use_changelog:
        previous_times: dict[str, list] = {}
        for (instance_id, _target_id, _path), (_status, _detail, checked_at) in previous.items():
            previous_times.setdefault(instance_id, []).append(checked_at)

        def fetch_changelog(instance_id, checked_times):
            instance = instances[instance_id]
            token = crypto.decrypt(instance.api_token_encrypted)
            return instance_id, netbox_client.has_relevant_device_type_changes(
                instance.base_url, token, netbox_client.verify_for_instance(instance),
                min(as_utc_aware(value) for value in checked_times),
            )

        with ThreadPoolExecutor(max_workers=max(1, settings.drift_workers)) as executor:
            futures = {executor.submit(fetch_changelog, *item): item[0] for item in previous_times.items()}
            for future in as_completed(futures):
                try:
                    instance_id, changed = future.result()
                    changelog_changed[instance_id] = changed
                    if changed is None:
                        run_stats.changelog_unknown += 1
                        _warn_instance_failure(
                            "changelog query", instances[instance_id],
                            RuntimeError("result is unknown or history is truncated"),
                        )
                except Exception as exc:
                    instance_id = futures[future]
                    changelog_changed[instance_id] = None
                    run_stats.changelog_unknown += 1
                    _warn_instance_failure("changelog query", instances[instance_id], exc)

    # Resolve blob SHAs once per target, then fetch each unique source blob once for this run.
    file_shas = {}
    rate_limited_targets = set()
    for target_id in {pair[1] for pair in device_type_pairs}:
        target = targets.get(target_id)
        if not target:
            continue
        try:
            pat = crypto.decrypt(target.pat_encrypted)
            remaining = github_repo.rate_limit_remaining(pat, target.repo)
            if remaining is not None and remaining < 100:
                rate_limited_targets.add(target_id)
                continue
            files = github_repo.list_device_types(
                pat, target.repo, target.branch, github_repo.base_dir_for_pattern(target.path_pattern)
            )
            file_shas.update({(target_id, item.path): item.sha for item in files})
        except Exception:
            continue
    run_stats.rate_limited_targets = len(rate_limited_targets)

    source_cache = {}
    unique_sources = {(target_id, path, file_shas.get((target_id, path)))
                      for _, target_id, path in device_type_pairs}

    def marker_for(instance_id, source, *, count_normalized=True):
        exact = marker_maps.get(instance_id, {}).get((source["manufacturer"], source["slug"]))
        if exact:
            return exact
        normalized = normalized_marker_maps.get(instance_id, {}).get(
            _normalized_marker_key(source["manufacturer"], source["slug"])
        )
        if normalized and count_normalized:
            with stats_lock:
                run_stats.normalized_marker_matches += 1
            logger.info(
                "Drift marker matched normalized identity for instance %s: manufacturer=%r slug=%r",
                instance_id, source["manufacturer"], source["slug"],
            )
        return normalized

    def pair_can_reuse_without_source(pair, sha):
        instance_id, _target_id, _path = pair
        old = previous.get(pair)
        if not old or old[0] == "error" or changelog_changed.get(instance_id) is True:
            return False
        old_meta = old[1].get("_drift_meta", {})
        since_full = int(old_meta.get("checks_since_full", 0))
        stored_marker = old_meta.get("netbox_marker")
        if stored_marker is None:
            return False
        current_marker = marker_for(instance_id, old_meta, count_normalized=False)
        return bool(
            old_meta.get("manufacturer") and old_meta.get("slug")
            and old_meta.get("source_sha") == sha
            and current_marker is not None
            and current_marker.get("last_updated") == stored_marker
            and since_full < max(1, settings.drift_full_recheck_every) - 1
        )

    sources_to_fetch = set()
    for target_id, path, sha in unique_sources:
        source_pairs = [pair for pair in device_type_pairs if pair[1] == target_id and pair[2] == path]
        if not source_pairs or not all(pair_can_reuse_without_source(pair, sha) for pair in source_pairs):
            sources_to_fetch.add((target_id, path, sha))

    def fetch_source(target_id, path, sha):
        target = targets[target_id]
        pat = crypto.decrypt(target.pat_encrypted)
        payload = github_repo.get_file(pat, target.repo, target.branch, path)["payload"]
        return (target_id, sha or path), DeviceType(**payload).to_internal_dict()

    source_fetch_locks = {item: threading.Lock() for item in unique_sources}

    def load_real_source(target_id, path, sha):
        key = (target_id, sha or path)
        lock = source_fetch_locks.setdefault((target_id, path, sha), threading.Lock())
        with lock:
            cached = source_cache.get(key)
            if cached and not cached.get("_stub"):
                return cached
            fetched_key, source = fetch_source(target_id, path, sha)
            source_cache[fetched_key] = source
            with stats_lock:
                run_stats.source_fetches += 1
            return source

    with ThreadPoolExecutor(max_workers=max(1, settings.drift_workers)) as executor:
        futures = [executor.submit(fetch_source, *item) for item in sources_to_fetch if item[0] in targets]
        for future in as_completed(futures):
            try:
                key, source = future.result()
                source_cache[key] = source
                run_stats.source_fetches += 1
            except Exception:
                continue

    def source_for_pair(pair, sha):
        _instance_id, target_id, path = pair
        source = source_cache.get((target_id, sha or path))
        if source:
            return source
        old = previous.get(pair)
        old_meta = old[1].get("_drift_meta", {}) if old else {}
        manufacturer, slug = old_meta.get("manufacturer"), old_meta.get("slug")
        if manufacturer and slug:
            return {"manufacturer": manufacturer, "slug": slug, "_stub": True}
        return None

    bulk_existing = {}
    if settings.drift_bulk_component_reads:
        ids_by_instance: dict[str, list[int]] = {}
        for pair in device_type_pairs:
            instance_id, target_id, path = pair
            sha = file_shas.get((target_id, path))
            source = source_for_pair(pair, sha)
            if not source:
                continue
            marker_entry = marker_for(instance_id, source, count_normalized=False)
            if not marker_entry:
                continue
            old = previous.get(pair)
            old_meta = old[1].get("_drift_meta", {}) if old else {}
            since_full = int(old_meta.get("checks_since_full", 0))
            full_due = since_full >= max(1, settings.drift_full_recheck_every) - 1
            reusable = (
                old and not full_due and changelog_changed.get(instance_id) is not True
                and old_meta.get("source_sha") == sha
                and old_meta.get("netbox_marker") == marker_entry["last_updated"]
            )
            if not reusable:
                ids_by_instance.setdefault(instance_id, []).append(marker_entry["id"])

        def fetch_existing_bulk(instance_id, device_type_ids):
            instance = instances[instance_id]
            token = crypto.decrypt(instance.api_token_encrypted)
            return instance_id, netbox_client.get_existing_device_types_bulk(
                instance.base_url, token, netbox_client.verify_for_instance(instance), device_type_ids,
            )

        with ThreadPoolExecutor(max_workers=max(1, settings.drift_workers)) as executor:
            futures = {executor.submit(fetch_existing_bulk, *item): item[0]
                       for item in ids_by_instance.items()}
            for future in as_completed(futures):
                try:
                    instance_id, existing = future.result()
                    bulk_existing[instance_id] = existing
                except Exception as exc:
                    instance_id = futures[future]
                    run_stats.bulk_read_failures += 1
                    _warn_instance_failure("bulk component read", instances[instance_id], exc)


    per_instance = {instance_id: threading.Semaphore(max(1, settings.drift_per_instance_workers))
                    for instance_id in instances}

    def check_pair(pair):
        instance_id, target_id, path = pair
        if target_id in rate_limited_targets:
            return pair, "error", {"error": "GitHub rate limited; drift check deferred."}
        instance, target = instances.get(instance_id), targets.get(target_id)
        sha = file_shas.get((target_id, path))
        source = source_for_pair(pair, sha)
        if not instance or not target or not source:
            return pair, "error", {"error": "Instance, target, or source file is unavailable."}
        token = crypto.decrypt(instance.api_token_encrypted)
        verify = netbox_client.verify_for_instance(instance)
        old = previous.get(pair)
        try:
            with per_instance[instance_id]:
                marker_entry = marker_for(instance_id, source)
                if marker_entry:
                    marker = marker_entry["last_updated"]
                else:
                    with stats_lock:
                        run_stats.fallback_pair_lookups += 1
                    marker = netbox_client.get_device_type_marker(
                        instance.base_url, token, verify, source["manufacturer"], source["slug"]
                    )
                old_detail = old[1] if old else {}
                old_meta = old_detail.get("_drift_meta", {})
                since_full = int(old_meta.get("checks_since_full", 0))
                full_due = since_full >= max(1, settings.drift_full_recheck_every) - 1
                changelog_requires_full = changelog_changed.get(instance_id) is True
                if (old and not full_due and not changelog_requires_full and old_meta.get("source_sha") == sha
                        and old_meta.get("netbox_marker") == marker):
                    detail = dict(old_detail)
                    detail["_drift_meta"] = {"source_sha": sha, "netbox_marker": marker,
                                             "manufacturer": source["manufacturer"],
                                             "slug": source["slug"],
                                             "checks_since_full": since_full + 1, "reused": True,
                                             "last_full_check": old_meta.get("last_full_check")}
                    with stats_lock:
                        run_stats.reused += 1
                    return pair, old[0], detail
                if source.get("_stub"):
                    try:
                        source = load_real_source(target_id, path, sha)
                    except Exception as exc:
                        return pair, "error", {"error": str(exc), "_drift_meta": {
                            "source_sha": None, "manufacturer": source.get("manufacturer"),
                            "slug": source.get("slug"), "reused": False,
                        }}
                existing = None
                if marker_entry:
                    existing = bulk_existing.get(instance_id, {}).get(marker_entry["id"])
                if existing is None:
                    if marker_entry:
                        existing = netbox_client.get_existing_device_type_by_id(
                            instance.base_url, token, verify, marker_entry["id"],
                        )
                    else:
                        existing = netbox_client.get_existing_device_type(
                            instance.base_url, token, verify, source["manufacturer"], source["slug"]
                        )
            result = diff_mod.diff_payloads(source, existing)
            result["_drift_meta"] = {"source_sha": sha, "netbox_marker": marker,
                                     "manufacturer": source["manufacturer"], "slug": source["slug"],
                                     "checks_since_full": 0, "reused": False,
                                     "last_full_check": utcnow().isoformat()}
            with stats_lock:
                run_stats.full_checks += 1
            return pair, result["status"], result
        except Exception as exc:
            return pair, "error", {"error": str(exc), "_drift_meta": {
                "source_sha": sha, "manufacturer": source.get("manufacturer"), "slug": source.get("slug"),
            }}

    pending = []
    with ThreadPoolExecutor(max_workers=max(1, settings.drift_workers)) as executor:
        futures = [executor.submit(check_pair, pair) for pair in device_type_pairs]
        for future in as_completed(futures):
            pending.append(future.result())
            if len(pending) >= 50:
                for (instance_id, target_id, path), status, detail in pending:
                    _upsert(db, instance_id, target_id, "device_type", path, status, detail, commit=False)
                db.commit()
                pending.clear()
    for (instance_id, target_id, path), status, detail in pending:
        _upsert(db, instance_id, target_id, "device_type", path, status, detail, commit=False)
    if pending:
        db.commit()
    for instance_id, repo_target_id in cf_pairs:
        check_custom_fields_pair(db, instance_id, repo_target_id)

    run_stats.degraded = _run_is_degraded(run_stats)
    summary = asdict(run_stats)
    stats.record_drift_run(summary)
    logger.info("Drift run summary: %s", summary)
    return len(device_type_pairs) + len(cf_pairs)
