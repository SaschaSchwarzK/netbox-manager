from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.database import run_schema_migrations


def _alembic_config(engine):
    from pathlib import Path

    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    connection = engine.connect()
    config.attributes["connection"] = connection
    return config, connection


def test_fresh_database_migrates_to_head():
    engine = create_engine("sqlite:///:memory:")
    run_schema_migrations(engine)
    tables = set(inspect(engine).get_table_names())
    assert {"alembic_version", "netbox_instances", "github_targets", "migration_jobs", "export_jobs", "audit_events"} <= tables
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0007_migration_rollback_snapshots"
    assert "previous_payload_json" in {
        column["name"] for column in inspect(engine).get_columns("migration_job_items")
    }
    assert "previous_payload_json" in {
        column["name"] for column in inspect(engine).get_columns("migration_job_patches")
    }


def test_existing_database_gets_missing_audit_events_table():
    engine = create_engine("sqlite:///:memory:")
    config, connection = _alembic_config(engine)
    try:
        command.upgrade(config, "0005_export_jobs")
        # Reproduce an installation whose dynamic baseline was applied before
        # AuditEvent existed in the model metadata.
        connection.execute(text("DROP TABLE audit_events"))
        connection.commit()
        command.upgrade(config, "head")
    finally:
        connection.close()

    inspector = inspect(engine)
    assert "audit_events" in inspector.get_table_names()
    assert "ix_audit_events_created_at" in {
        index["name"] for index in inspector.get_indexes("audit_events")
    }


def test_hot_migration_query_uses_composite_index():
    engine = create_engine("sqlite:///:memory:")
    run_schema_migrations(engine)
    with engine.connect() as connection:
        plan = connection.execute(text(
            "EXPLAIN QUERY PLAN SELECT * FROM migration_job_items "
            "WHERE job_id='job' AND execution_status='pending' ORDER BY order_index"
        )).all()
    assert "ix_migration_items_execution" in " ".join(str(row) for row in plan)
