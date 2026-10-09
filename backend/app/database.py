from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

# check_same_thread=False is needed for SQLite since FastAPI may use the
# connection from a different thread than the one that created it; SQLAlchemy's
# session-per-request pattern still keeps access safe.
DATABASE_URL = f"sqlite:///{settings.database_path}"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _configure_sqlite_connection(dbapi_connection, _connection_record):
    """Allow report polling to read while the migration worker is committing progress."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def optimize_database() -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA optimize")


def run_schema_migrations(bind: Engine = engine) -> None:
    """Upgrade a fresh or existing Alembic-managed database to head."""
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    with bind.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
