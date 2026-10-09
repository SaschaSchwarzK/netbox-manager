"""Baseline the current application schema."""
from alembic import op

from app.database import Base
from app import models  # noqa: F401

revision = "0001_current_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    for table in Base.metadata.sorted_tables:
        if table.name != "user_sessions":
            table.create(bind=bind, checkfirst=True)


def downgrade():
    Base.metadata.drop_all(bind=op.get_bind())
