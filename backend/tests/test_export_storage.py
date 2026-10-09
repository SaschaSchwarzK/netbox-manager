import uuid

import pytest

from app.config import settings
from app.services.export import storage


def test_export_storage_creates_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "export_dir", str(tmp_path / "exports"))
    assert storage.ensure_dirs()
    assert (tmp_path / "exports/files").is_dir()
    assert (tmp_path / "exports/tmp").is_dir()


@pytest.mark.parametrize("job_id,ext", [("../bad", "csv"), (str(uuid.uuid4()), "exe"), ("bad", "xlsx")])
def test_export_paths_reject_bad_values(tmp_path, monkeypatch, job_id, ext):
    monkeypatch.setattr(settings, "export_dir", str(tmp_path))
    with pytest.raises(ValueError):
        storage.final_path(job_id, ext)


def test_export_storage_unwritable(monkeypatch):
    monkeypatch.setattr(settings, "export_dir", "/dev/null/exports")
    assert storage.ensure_dirs() is False
    assert storage.available is False
