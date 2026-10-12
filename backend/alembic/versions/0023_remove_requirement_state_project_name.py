"""Remove the denormalized requirement-state project name.

Revision ID: 0023_remove_req_state_name
Revises: 0022_add_generation_jobs
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# ``alembic_version.version_num`` is VARCHAR(32) in deployed databases.
# Keep every revision identifier at or below that limit.
revision: str = "0023_remove_req_state_name"
down_revision: Union[str, None] = "0022_add_generation_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if (
        "requirement_states" in inspector.get_table_names()
        and "project_name" in {column["name"] for column in inspector.get_columns("requirement_states")}
    ):
        op.drop_column("requirement_states", "project_name")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if (
        "requirement_states" in inspector.get_table_names()
        and "project_name" not in {column["name"] for column in inspector.get_columns("requirement_states")}
    ):
        op.add_column(
            "requirement_states",
            sa.Column("project_name", sa.String(length=255), nullable=False, server_default="Default Project"),
        )
