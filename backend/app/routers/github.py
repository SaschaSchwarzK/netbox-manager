import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.database import get_db
from app.rbac import (
    AccessContext,
    filter_scoped,
    get_access_context,
    require_app_admin,
    require_resource_admin,
    require_visible,
)
from app.services import github_repo

router = APIRouter(prefix="/api/github-targets", tags=["github"])


@router.get("", response_model=list[schemas.GithubTargetOut])
def list_targets(db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    targets = db.query(models.GithubTarget).order_by(models.GithubTarget.name).all()
    return filter_scoped(targets, "github_target", ctx, db)


@router.post("/test", response_model=schemas.ConnectionTestResult)
def test_new_target(
    payload: schemas.GithubTargetTestRequest,
    db: Session = Depends(get_db),
    ctx: AccessContext = Depends(get_access_context),
):
    """Test a repo/branch/PAT combination before saving it."""
    target = None
    if payload.id is not None:
        target = db.get(models.GithubTarget, payload.id)
        if target is None:
            raise HTTPException(404, "Target not found.")
        require_resource_admin(ctx, "github_target", payload.id, db)
    elif not ctx.app_admin:
        raise HTTPException(
            403,
            "Managing NetBox Manager itself requires app-level admin — a mapping with role "
            "'admin' on 'All resources', or NBM_BOOTSTRAP_ADMIN_GROUPS.",
        )

    repo = payload.repo.strip() if payload.repo else (target.repo if target else "")
    branch = payload.branch.strip() if payload.branch else (target.branch if target else "")
    if payload.pat is not None and payload.pat.strip():
        pat = payload.pat.strip()
    elif target is not None:
        pat = crypto.decrypt(target.pat_encrypted)
    else:
        pat = ""
    if not repo or not branch or not pat:
        raise HTTPException(400, "Repo, branch, and PAT are required when testing a new target.")
    result = github_repo.test_connection(pat, repo, branch)
    return {"ok": result["ok"], "netbox_version": None, "detail": result["detail"]}


@router.post("/{target_id}/test", response_model=schemas.ConnectionTestResult)
def test_existing_target(
    target_id: str,
    db: Session = Depends(get_db),
    ctx: AccessContext = Depends(get_access_context),
):
    target = db.get(models.GithubTarget, target_id)
    if not target:
        raise HTTPException(404, "Target not found.")
    db.query(models.DriftRecord).filter(models.DriftRecord.repo_target_id == target_id).delete(
        synchronize_session=False
    )
    require_visible("github_target", target_id, ctx, db)
    pat = crypto.decrypt(target.pat_encrypted)
    result = github_repo.test_connection(pat, target.repo, target.branch)
    return {"ok": result["ok"], "netbox_version": None, "detail": result["detail"]}


@router.post("", response_model=schemas.GithubTargetOut, status_code=201)
def create_target(
    payload: schemas.GithubTargetCreate, db: Session = Depends(get_db),
    _: AccessContext = Depends(require_app_admin()),
):
    if db.query(models.GithubTarget).filter_by(name=payload.name).first():
        raise HTTPException(400, "A GitHub target with that name already exists.")
    target = models.GithubTarget(
        name=payload.name,
        repo=payload.repo,
        branch=payload.branch,
        path_pattern=payload.path_pattern,
        module_path_pattern=payload.module_path_pattern,
        rack_path_pattern=payload.rack_path_pattern,
        custom_fields_path=payload.custom_fields_path,
        reference_data_path=payload.reference_data_path,
        pat_encrypted=crypto.encrypt(payload.pat),
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    return target


@router.patch("/{target_id}", response_model=schemas.GithubTargetOut)
def update_target(
    target_id: str,
    payload: schemas.GithubTargetUpdate,
    db: Session = Depends(get_db),
    ctx: AccessContext = Depends(get_access_context),
):
    target = db.get(models.GithubTarget, target_id)
    if target is None:
        raise HTTPException(404, "Target not found.")
    require_resource_admin(ctx, "github_target", target_id, db)
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(400, "GitHub target name must not be empty.")
        duplicate = (
            db.query(models.GithubTarget)
            .filter(models.GithubTarget.name == name, models.GithubTarget.id != target_id)
            .first()
        )
        if duplicate:
            raise HTTPException(400, "A GitHub target with that name already exists.")
        target.name = name
    if payload.repo is not None:
        repo = payload.repo.strip()
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
            raise HTTPException(400, "Repository must use the 'owner/repo' format.")
        target.repo = repo
    if payload.branch is not None:
        branch = payload.branch.strip()
        if not branch:
            raise HTTPException(400, "Branch must not be empty.")
        target.branch = branch
    if payload.path_pattern is not None:
        target.path_pattern = payload.path_pattern
    if payload.module_path_pattern is not None:
        target.module_path_pattern = payload.module_path_pattern
    if payload.rack_path_pattern is not None:
        target.rack_path_pattern = payload.rack_path_pattern
    if payload.custom_fields_path is not None:
        target.custom_fields_path = payload.custom_fields_path
    if payload.reference_data_path is not None:
        target.reference_data_path = payload.reference_data_path
    if payload.pat is not None and payload.pat.strip():
        target.pat_encrypted = crypto.encrypt(payload.pat.strip())
    db.commit()
    db.refresh(target)
    return target


@router.delete("/{target_id}", status_code=204)
def delete_target(
    target_id: str, db: Session = Depends(get_db), _: AccessContext = Depends(require_app_admin())
):
    target = db.get(models.GithubTarget, target_id)
    if not target:
        raise HTTPException(404, "Target not found.")
    db.query(models.AccessMapping).filter_by(
        resource_type="github_target", resource_id=target_id
    ).delete(synchronize_session=False)
    db.delete(target)
    db.commit()
