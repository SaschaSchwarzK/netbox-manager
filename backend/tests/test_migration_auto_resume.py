from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main, models


def run_resume(tmp_path, monkeypatch, *, count, enabled=True, limit=3):
    engine = create_engine(f"sqlite:///{tmp_path / 'resume.db'}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    job = models.MigrationJob(source_instance_id="source", target_instance_id="target", status="running",
                              resume_count=count)
    db.add(job); db.commit(); job_id = job.id; db.close()
    started = []
    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(main.settings, "migration_auto_resume", enabled)
    monkeypatch.setattr(main.settings, "migration_max_auto_resumes", limit)
    monkeypatch.setattr("app.routers.migrations.start_job_worker",
                        lambda job_id, worker, args=(): started.append(job_id))
    monkeypatch.setattr(main.syslog_client, "send_audit_entry", lambda event: None)
    main.resume_orphaned_migration_jobs()
    db = Session(); refreshed = db.get(models.MigrationJob, job_id)
    return refreshed, started, db


def test_orphan_below_limit_resumes(tmp_path, monkeypatch):
    job, started, db = run_resume(tmp_path, monkeypatch, count=1)
    assert job.resume_count == 2 and job.last_resumed_at is not None
    assert started == [job.id]
    db.close()


def test_orphan_at_limit_fails(tmp_path, monkeypatch):
    job, started, db = run_resume(tmp_path, monkeypatch, count=3)
    assert job.status == "failed" and "limit" in job.current_step
    assert started == []
    db.close()


def test_orphan_fails_when_auto_resume_disabled(tmp_path, monkeypatch):
    job, started, db = run_resume(tmp_path, monkeypatch, count=0, enabled=False)
    assert job.status == "failed" and "disabled" in job.current_step
    assert started == []
    db.close()
