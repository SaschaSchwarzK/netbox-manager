from datetime import timedelta
from types import SimpleNamespace

from fastapi.responses import FileResponse

from app.routers import exports
from app.timeutil import utcnow


class FailingAuditSession:
    def add(self, _value):
        pass

    def commit(self):
        raise RuntimeError("audit database unavailable")

    def rollback(self):
        self.rolled_back = True


def test_download_still_returns_file_when_audit_write_fails(monkeypatch, tmp_path):
    job_id = "24c4719c-5d36-4c9b-8b68-d531f28921d9"
    exported = tmp_path / f"{job_id}.csv"
    exported.write_text("name\nrouter-1\n", encoding="utf-8")
    job = SimpleNamespace(
        id=job_id,
        status="completed",
        expires_at=utcnow() + timedelta(days=1),
        file_name=exported.name,
        download_name="export_test.csv",
    )
    db = FailingAuditSession()
    monkeypatch.setattr(exports.storage, "available", True)
    monkeypatch.setattr(exports.storage, "final_path", lambda _job_id, _ext: exported)
    monkeypatch.setattr(exports, "_owner", lambda _request: ("user-1", {"sub": "user-1"}))
    monkeypatch.setattr(exports, "_owned_job", lambda _job_id, _owner, _db: job)

    response = exports.download_export(job_id, object(), db, object())

    assert isinstance(response, FileResponse)
    assert response.filename == "export_test.csv"
    assert db.rolled_back is True
