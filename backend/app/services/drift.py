"""
Drift detection. We don't maintain an explicit "this instance runs this
device type" mapping — instead we infer candidate (instance, file_path) pairs
from successful past pushes logged in DeviceTypePushHistory. For each pair we
re-fetch the source from GitHub and the current state from NetBox, diff them,
and upsert a DriftRecord.
"""
import json
from datetime import datetime

from sqlalchemy.orm import Session

from app import crypto, models
from app.services import diff as diff_mod
from app.services import github_repo, netbox_client


def candidate_pairs(db: Session) -> list[tuple[str, str, str]]:
    """Returns distinct (instance_id, repo_target_id, file_path) tuples worth checking."""
    rows = (
        db.query(models.DeviceTypePushHistory.target_name, models.DeviceTypePushHistory.repo_target_id,
                  models.DeviceTypePushHistory.file_path)
        .filter(models.DeviceTypePushHistory.target_type == "netbox", models.DeviceTypePushHistory.status == "success")
        .distinct()
        .all()
    )
    pairs = []
    instances_by_name = {i.name: i.id for i in db.query(models.NetboxInstance).all()}
    for target_name, repo_target_id, file_path in rows:
        instance_id = instances_by_name.get(target_name)
        if instance_id:
            pairs.append((instance_id, repo_target_id, file_path))
    return pairs


def check_pair(db: Session, instance_id: str, repo_target_id: str, file_path: str) -> models.DriftRecord:
    instance = db.get(models.NetboxInstance, instance_id)
    target = db.get(models.GithubTarget, repo_target_id)

    status = "error"
    detail: dict = {}

    if not instance or not target:
        detail = {"error": "Instance or GitHub target no longer exists."}
    else:
        try:
            pat = crypto.decrypt(target.pat_encrypted)
            source = github_repo.get_file(pat, target.repo, target.branch, file_path)["payload"]
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

    existing_record = (
        db.query(models.DriftRecord)
        .filter_by(instance_id=instance_id, repo_target_id=repo_target_id, file_path=file_path)
        .first()
    )
    if existing_record:
        existing_record.status = status
        existing_record.detail_json = json.dumps(detail)
        existing_record.checked_at = datetime.utcnow()
        record = existing_record
    else:
        record = models.DriftRecord(
            instance_id=instance_id, repo_target_id=repo_target_id, file_path=file_path,
            status=status, detail_json=json.dumps(detail),
        )
        db.add(record)
    db.commit()
    db.refresh(record)
    return record


def run_full_check(db: Session) -> int:
    """Checks every known (instance, device-type) pair. Returns how many were checked."""
    count = 0
    for instance_id, repo_target_id, file_path in candidate_pairs(db):
        check_pair(db, instance_id, repo_target_id, file_path)
        count += 1
    return count
