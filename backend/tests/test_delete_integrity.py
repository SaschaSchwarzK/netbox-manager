import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import models
from app.database import _configure_sqlite_connection
from app.rbac import AccessContext
from app.routers.github import delete_target
from app.routers.instances import delete_instance


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'delete.db'}")
    event.listen(engine, "connect", _configure_sqlite_connection)
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def admin():
    return AccessContext(role="admin", scoping_active=False, app_admin=True)


def instance(name):
    return models.NetboxInstance(name=name, base_url=f"https://{name}", api_token_encrypted="encrypted")


def test_instance_delete_blocks_non_terminal_job(db):
    source, target = instance("source"), instance("target")
    db.add_all([source, target]); db.flush()
    db.add(models.MigrationJob(source_instance_id=source.id, target_instance_id=target.id, status="running"))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        delete_instance(source.id, db=db, _=admin())
    assert exc.value.status_code == 409


def test_instance_delete_removes_drift_and_preserves_terminal_job(db):
    source, target = instance("source"), instance("target")
    repo = models.GithubTarget(name="repo", repo="o/r", pat_encrypted="encrypted")
    db.add_all([source, target, repo]); db.flush()
    job = models.MigrationJob(source_instance_id=source.id, target_instance_id=target.id, status="completed")
    db.add_all([job, models.DriftRecord(instance_id=source.id, repo_target_id=repo.id,
                                        file_path="device-types/A/a.yml", status="in_sync")])
    db.commit()
    delete_instance(source.id, db=db, _=admin())
    db.refresh(job)
    assert job.source_instance_id is None
    assert db.query(models.DriftRecord).count() == 0


def test_target_delete_removes_drift_and_preserves_push_history(db):
    source = instance("source")
    repo = models.GithubTarget(name="repo", repo="o/r", pat_encrypted="encrypted")
    db.add_all([source, repo]); db.flush()
    history = models.DeviceTypePushHistory(repo_target_id=repo.id, file_path="device-types/A/a.yml",
        target_type="github", target_name="repo", status="success")
    db.add_all([history, models.DriftRecord(instance_id=source.id, repo_target_id=repo.id,
                                            file_path="device-types/A/a.yml", status="in_sync")])
    db.commit()
    delete_target(repo.id, db=db, _=admin())
    db.refresh(history)
    assert history.repo_target_id is None
    assert db.query(models.DriftRecord).count() == 0
