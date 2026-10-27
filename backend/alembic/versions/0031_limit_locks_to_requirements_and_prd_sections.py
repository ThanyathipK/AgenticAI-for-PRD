"""limit locks to requirements and PRD sections

Revision ID: 0031_limit_lock_scope
Revises: 0030_remove_lock_reason
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0031_limit_lock_scope"
down_revision = "0030_remove_lock_reason"
branch_labels = None
depends_on = None


TARGET_TABLES = (
    "projects",
    "epics",
    "user_stories",
    "acceptance_criteria",
    "clarification_questions",
    "prd_documents",
)

LOCK_COLUMNS = ("is_locked", "locked_by", "locked_at")


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    for table_name in TARGET_TABLES:
        columns = _columns(table_name)
        for column_name in LOCK_COLUMNS:
            if column_name in columns:
                op.drop_column(table_name, column_name)


def downgrade() -> None:
    column_types = {
        "is_locked": sa.Boolean(),
        "locked_by": sa.String(length=100),
        "locked_at": sa.DateTime(timezone=True),
    }
    for table_name in TARGET_TABLES:
        columns = _columns(table_name)
        for column_name in LOCK_COLUMNS:
            if column_name not in columns:
                kwargs = {"nullable": column_name != "is_locked"}
                if column_name == "is_locked":
                    kwargs["server_default"] = sa.false()
                op.add_column(
                    table_name,
                    sa.Column(column_name, column_types[column_name], **kwargs),
                )
