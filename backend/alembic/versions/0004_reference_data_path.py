"""Add the reference-data repository directory."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0004_reference_data_path"
down_revision = "0003_performance_indexes"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("github_targets")}
    if "reference_data_path" not in columns:
        op.add_column("github_targets", sa.Column("reference_data_path", sa.String(256), nullable=False, server_default="reference-data"))


def downgrade():
    op.drop_column("github_targets", "reference_data_path")
