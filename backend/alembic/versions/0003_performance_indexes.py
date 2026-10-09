"""Add indexes for drift, history, and migration hot paths."""
from alembic import op
from sqlalchemy import inspect

revision = "0003_performance_indexes"
down_revision = "0002_user_sessions"
branch_labels = None
depends_on = None


def upgrade():
    statements = (
        ("drift_records", {"instance_id", "repo_target_id", "kind", "file_path"}, "CREATE INDEX IF NOT EXISTS ix_drift_pair_path ON drift_records (instance_id, repo_target_id, kind, file_path)"),
        ("push_history", {"repo_target_id", "created_at"}, "CREATE INDEX IF NOT EXISTS ix_push_history_target_created ON push_history (repo_target_id, created_at)"),
        ("push_history", {"created_at"}, "CREATE INDEX IF NOT EXISTS ix_push_history_created ON push_history (created_at)"),
        ("migration_job_items", {"job_id", "execution_status", "order_index"}, "CREATE INDEX IF NOT EXISTS ix_migration_items_execution ON migration_job_items (job_id, execution_status, order_index)"),
        ("migration_job_patches", {"job_id", "execution_status", "order_index"}, "CREATE INDEX IF NOT EXISTS ix_migration_patches_execution ON migration_job_patches (job_id, execution_status, order_index)"),
        ("migration_jobs", {"created_at"}, "CREATE INDEX IF NOT EXISTS ix_migration_jobs_created ON migration_jobs (created_at)"),
    )
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table, required_columns, statement in statements:
        columns = {item["name"] for item in inspector.get_columns(table)} if table in tables else set()
        if required_columns <= columns:
            op.execute(statement)


def downgrade():
    op.drop_index("ix_migration_jobs_created", table_name="migration_jobs")
    op.drop_index("ix_migration_patches_execution", table_name="migration_job_patches")
    op.drop_index("ix_migration_items_execution", table_name="migration_job_items")
    op.drop_index("ix_push_history_created", table_name="push_history")
    op.drop_index("ix_push_history_target_created", table_name="push_history")
    op.drop_index("ix_drift_pair_path", table_name="drift_records")
