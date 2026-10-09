"""Store authenticated sessions server-side."""
from alembic import op
import sqlalchemy as sa

revision = "0002_user_sessions"
down_revision = "0001_current_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("user_sessions",
        sa.Column("id_hash", sa.String(64), primary_key=True), sa.Column("sub", sa.String(512), nullable=False),
        sa.Column("name", sa.String(512)), sa.Column("email", sa.String(512)), sa.Column("username", sa.String(512)),
        sa.Column("groups_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("local", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False))
    op.create_index("ix_user_sessions_sub", "user_sessions", ["sub"])
    op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"])
    op.create_index("ix_user_sessions_last_seen_at", "user_sessions", ["last_seen_at"])


def downgrade():
    op.drop_table("user_sessions")
