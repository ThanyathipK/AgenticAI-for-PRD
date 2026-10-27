"""remove legacy requirement state columns

Revision ID: 0032_trim_req_state
Revises: 0031_limit_lock_scope
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0032_trim_req_state"
down_revision = "0031_limit_lock_scope"
branch_labels = None
depends_on = None


LEGACY_COLUMNS = (
    "project_name",
    "requirements",
    "user_stories",
    "acceptance_criteria",
    "clarification_questions",
)


def _columns() -> set[str]:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "requirement_states" not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns("requirement_states")}


def upgrade() -> None:
    columns = _columns()
    for column_name in LEGACY_COLUMNS:
        if column_name in columns:
            op.drop_column("requirement_states", column_name)


def downgrade() -> None:
    columns = _columns()
    definitions = {
        "project_name": sa.Column("project_name", sa.Text(), nullable=True),
        "requirements": sa.Column("requirements", sa.JSON(), nullable=False, server_default="{}"),
        "user_stories": sa.Column("user_stories", sa.JSON(), nullable=False, server_default="[]"),
        "acceptance_criteria": sa.Column("acceptance_criteria", sa.JSON(), nullable=False, server_default="[]"),
        "clarification_questions": sa.Column("clarification_questions", sa.JSON(), nullable=False, server_default="[]"),
    }
    for column_name in LEGACY_COLUMNS:
        if column_name not in columns:
            op.add_column("requirement_states", definitions[column_name])
