"""remove lock reason from lockable artifacts

Revision ID: 0030_remove_lock_reason
Revises: 0029_read_path_indexes
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0030_remove_lock_reason"
down_revision = "0029_read_path_indexes"
branch_labels = None
depends_on = None


TARGET_TABLES = (
    "projects",
    "epics",
    "requirements",
    "user_stories",
    "acceptance_criteria",
    "clarification_questions",
    "prd_documents",
    "prd_sections",
)


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    for table_name in TARGET_TABLES:
        if "lock_reason" in _columns(table_name):
            op.drop_column(table_name, "lock_reason")


def downgrade() -> None:
    for table_name in TARGET_TABLES:
        if table_name in inspect(op.get_bind()).get_table_names() and "lock_reason" not in _columns(table_name):
            op.add_column(
                table_name,
                sa.Column("lock_reason", sa.String(length=255), nullable=True),
            )
