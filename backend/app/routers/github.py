from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.database import get_db
from app.rbac import AccessContext, filter_scoped, get_access_context, require_role
from app.services import github_repo

router = APIRouter(prefix="/api/github-targets", tags=["github"])


@router.get("", response_model=list[schemas.GithubTargetOut])
def list_targets(db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    targets = db.query(models.GithubTarget).order_by(models.GithubTarget.name).all()
    return filter_scoped(targets, "github_target", ctx, db)


@router.post("/test", response_model=schemas.ConnectionTestResult)
def test_new_target(payload: schemas.GithubTargetCreate, _: AccessContext = Depends(require_role("admin"))):
    """Test a repo/branch/PAT combination before saving it."""
    result = github_repo.test_connection(payload.pat, payload.repo, payload.branch)
    return {"ok": result["ok"], "netbox_version": None, "detail": result["detail"]}


@router.post("/{target_id}/test", response_model=schemas.ConnectionTestResult)
def test_existing_target(target_id: str, db: Session = Depends(get_db)):
    target = db.get(models.GithubTarget, target_id)
    if not target:
        raise HTTPException(404, "Target not found.")
    pat = crypto.decrypt(target.pat_encrypted)
    result = github_repo.test_connection(pat, target.repo, target.branch)
    return {"ok": result["ok"], "netbox_version": None, "detail": result["detail"]}


@router.post("", response_model=schemas.GithubTargetOut, status_code=201)
def create_target(
    payload: schemas.GithubTargetCreate, db: Session = Depends(get_db),
    _: AccessContext = Depends(require_role("admin")),
):
    if db.query(models.GithubTarget).filter_by(name=payload.name).first():
        raise HTTPException(400, "A GitHub target with that name already exists.")
    target = models.GithubTarget(
        name=payload.name,
        repo=payload.repo,
        branch=payload.branch,
        path_pattern=payload.path_pattern,
        custom_fields_path=payload.custom_fields_path,
        pat_encrypted=crypto.encrypt(payload.pat),
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    return target


@router.delete("/{target_id}", status_code=204)
def delete_target(
    target_id: str, db: Session = Depends(get_db), _: AccessContext = Depends(require_role("admin"))
):
    target = db.get(models.GithubTarget, target_id)
    if not target:
        raise HTTPException(404, "Target not found.")
    db.delete(target)
    db.commit()
