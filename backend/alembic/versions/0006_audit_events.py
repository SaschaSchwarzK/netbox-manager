"""Add the generic mutation audit log.

The model was added after the dynamic baseline migration had already been
applied by existing installations.  Fresh databases therefore received this
table through 0001, while upgraded databases did not.  Keep this migration
idempotent so it works for both cases.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0006_audit_events"
down_revision = "0005_export_jobs"
branch_labels = None
depends_on = None


def upgrade():
    if "audit_events" in inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("actor_sub", sa.String(255)),
        sa.Column("actor_name", sa.String(255)),
        sa.Column("actor_email", sa.String(255)),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(255)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("detail", sa.Text()),
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])


def downgrade():
    op.drop_table("audit_events")
