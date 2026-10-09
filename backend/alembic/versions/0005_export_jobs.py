"""Add Data Export jobs."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0005_export_jobs"
down_revision = "0004_reference_data_path"
branch_labels = None
depends_on = None


def upgrade():
    if "export_jobs" in inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "export_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("instance_id", sa.String(36), sa.ForeignKey("netbox_instances.id", ondelete="SET NULL"), nullable=True),
        sa.Column("instance_name", sa.String(128), nullable=False),
        sa.Column("owner_sub", sa.String(255), nullable=False),
        sa.Column("actor_name", sa.String(255)), sa.Column("actor_email", sa.String(255)),
        sa.Column("tenant_id", sa.Integer(), nullable=False), sa.Column("tenant_name", sa.String(255), nullable=False),
        sa.Column("tenant_slug", sa.String(255), nullable=False), sa.Column("object_types_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("fields_json", sa.Text(), nullable=False, server_default="{}"), sa.Column("format", sa.String(8), nullable=False),
        sa.Column("delimiter", sa.String(1), nullable=False, server_default=","), sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("progress_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("row_counts_json", sa.Text(), nullable=False, server_default="{}"), sa.Column("error", sa.String(512)),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("started_at", sa.DateTime()), sa.Column("finished_at", sa.DateTime()),
        sa.Column("expires_at", sa.DateTime()), sa.Column("file_name", sa.String(64)), sa.Column("file_size", sa.BigInteger()),
        sa.Column("download_name", sa.String(255), nullable=False),
    )
    op.create_index("ix_export_jobs_owner_created", "export_jobs", ["owner_sub", "created_at"])
    op.create_index("ix_export_jobs_status", "export_jobs", ["status"])
    op.create_index("ix_export_jobs_expires_at", "export_jobs", ["expires_at"])


def downgrade():
    op.drop_table("export_jobs")
