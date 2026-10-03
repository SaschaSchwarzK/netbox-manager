from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

# check_same_thread=False is needed for SQLite since FastAPI may use the
# connection from a different thread than the one that created it; SQLAlchemy's
# session-per-request pattern still keeps access safe.
DATABASE_URL = "sqlite:////app/data/netbox_manager.db"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _configure_sqlite_connection(dbapi_connection, _connection_record):
    """Allow report polling to read while the migration worker is committing progress."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def upgrade_existing_schema(bind: Engine) -> None:
    """Apply the small additive upgrades needed by pre-migration-tool databases."""
    inspector = inspect(bind)
    if "migration_job_patches" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("migration_job_patches")}
    if "polymorphic_patch_fields_json" not in columns:
        with bind.begin() as connection:
            connection.execute(text(
                "ALTER TABLE migration_job_patches "
                "ADD COLUMN polymorphic_patch_fields_json TEXT NOT NULL DEFAULT '{}'"
            ))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
