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
    tables = set(inspector.get_table_names())
    patch_columns = (
        {column["name"] for column in inspector.get_columns("migration_job_patches")}
        if "migration_job_patches" in tables else set()
    )
    if "migration_job_patches" in tables and "polymorphic_patch_fields_json" not in patch_columns:
        with bind.begin() as connection:
            connection.execute(text(
                "ALTER TABLE migration_job_patches "
                "ADD COLUMN polymorphic_patch_fields_json TEXT NOT NULL DEFAULT '{}'"
            ))
    additive_columns = {
        "migration_jobs": ("current_step", "VARCHAR(512)"),
        "migration_job_items": ("target_natural_key", "VARCHAR(512)"),
    }
    for table_name, (column_name, column_type) in additive_columns.items():
        if table_name not in tables:
            continue
        existing = {column["name"] for column in inspect(bind).get_columns(table_name)}
        if column_name not in existing:
            with bind.begin() as connection:
                connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
