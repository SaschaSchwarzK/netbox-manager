import os
import shutil
import tempfile
from pathlib import Path

import pytest


# pytest imports test modules during collection, before fixtures run. Set the
# database location at conftest import time so app.database can never bind its
# global engine to /app/data during a test run.
_DATABASE_DIR = Path(tempfile.mkdtemp(prefix="netbox-manager-tests-"))
_DATABASE_PATH = _DATABASE_DIR / "netbox_manager.db"
os.environ["NBM_DATABASE_PATH"] = str(_DATABASE_PATH)
_EXPORT_DIR = _DATABASE_DIR / "exports"
os.environ["NBM_EXPORT_DIR"] = str(_EXPORT_DIR)


@pytest.fixture(scope="session", autouse=True)
def isolated_database():
    from app.database import engine, run_schema_migrations
    from app.services.export import storage

    run_schema_migrations()
    storage.ensure_dirs()
    yield
    engine.dispose()
    shutil.rmtree(_DATABASE_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_shared_database(isolated_database):
    from app.database import Base, engine

    def clear():
        with engine.begin() as connection:
            for table in reversed(Base.metadata.sorted_tables):
                connection.execute(table.delete())

    clear()
    yield
    clear()
