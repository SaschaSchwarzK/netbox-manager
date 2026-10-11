"""Store pre-update values for reversible migration updates."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0007_migration_rollback_snapshots"
down_revision = "0006_audit_events"
branch_labels = None
depends_on = None


def _add_column(table: str) -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns(table)}
    if "previous_payload_json" not in columns:
        op.add_column(table, sa.Column("previous_payload_json", sa.Text(), nullable=True))


def upgrade():
    _add_column("migration_job_items")
    _add_column("migration_job_patches")


def downgrade():
    op.drop_column("migration_job_patches", "previous_payload_json")
    op.drop_column("migration_job_items", "previous_payload_json")
