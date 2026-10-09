from sqlalchemy import create_engine, inspect, text

from app.database import run_schema_migrations


def test_fresh_database_migrates_to_head():
    engine = create_engine("sqlite:///:memory:")
    run_schema_migrations(engine)
    tables = set(inspect(engine).get_table_names())
    assert {"alembic_version", "netbox_instances", "github_targets", "migration_jobs", "export_jobs"} <= tables
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0005_export_jobs"


def test_hot_migration_query_uses_composite_index():
    engine = create_engine("sqlite:///:memory:")
    run_schema_migrations(engine)
    with engine.connect() as connection:
        plan = connection.execute(text(
            "EXPLAIN QUERY PLAN SELECT * FROM migration_job_items "
            "WHERE job_id='job' AND execution_status='pending' ORDER BY order_index"
        )).all()
    assert "ix_migration_items_execution" in " ".join(str(row) for row in plan)
