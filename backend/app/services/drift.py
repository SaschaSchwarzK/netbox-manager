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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from sqlalchemy.orm import Session

from app import crypto, models
from app.services import diff as diff_mod
from app.devicetype_schema import DeviceType
from app.services import github_repo, netbox_client, netbox_customfields

_MAX_WORKERS = 8


def _repo_device_types(target: models.GithubTarget) -> dict[tuple[str, str], str]:
    """
    {(manufacturer, slug): file_path} for every device-type file in this repo
    target, using each file's actual declared manufacturer/slug (not guessed
    from its path), so it matches how NetBox itself identifies a device type.
    """
    try:
        pat = crypto.decrypt(target.pat_encrypted)
        files = github_repo.list_device_types(pat, target.repo, target.branch, github_repo.base_dir_for_pattern(target.path_pattern))
    except Exception:
        return {}
    result: dict[tuple[str, str], str] = {}
    for f in files:
        try:
            payload = github_repo.get_file(pat, target.repo, target.branch, f.path)["payload"] or {}
        except Exception:
            continue
        manufacturer, slug = payload.get("manufacturer"), payload.get("slug")
        if manufacturer and slug:
            result[(manufacturer, slug)] = f.path
    return result


def _instance_device_types(instance: models.NetboxInstance) -> set[tuple[str, str]]:
    try:
        token = crypto.decrypt(instance.api_token_encrypted)
        entries = netbox_client.list_device_types_on_instance(instance.base_url, token, instance.verify_ssl)
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


def _upsert(db: Session, instance_id: str, repo_target_id: str, kind: str, file_path: str, status: str, detail: dict) -> models.DriftRecord:
    existing_record = (
        db.query(models.DriftRecord)
        .filter_by(instance_id=instance_id, repo_target_id=repo_target_id, kind=kind, file_path=file_path)
        .first()
    )
    if existing_record:
        existing_record.status = status
        existing_record.detail_json = json.dumps(detail)
        existing_record.checked_at = datetime.utcnow()
        record = existing_record
    else:
        record = models.DriftRecord(
            instance_id=instance_id, repo_target_id=repo_target_id, kind=kind, file_path=file_path,
            status=status, detail_json=json.dumps(detail),
        )
        db.add(record)
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
                instance.base_url, token, instance.verify_ssl, source["manufacturer"], source["slug"]
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
            existing = netbox_customfields.get_existing_custom_fields(instance.base_url, token, instance.verify_ssl)
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

    for instance_id, repo_target_id, file_path in device_type_pairs:
        check_device_type_pair(db, instance_id, repo_target_id, file_path)
    for instance_id, repo_target_id in cf_pairs:
        check_custom_fields_pair(db, instance_id, repo_target_id)

    return len(device_type_pairs) + len(cf_pairs)
