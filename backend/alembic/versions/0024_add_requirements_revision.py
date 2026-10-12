"""Separate requirement-work revisions from PRD release versions.

Revision ID: 0024_add_req_revision
Revises: 0023_remove_req_state_name
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "0024_add_req_revision"
down_revision: Union[str, None] = "0023_remove_req_state_name"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "requirement_states" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("requirement_states")}
    if "requirements_revision" not in columns:
        op.add_column(
            "requirement_states",
            sa.Column("requirements_revision", sa.Integer(), nullable=False, server_default="1"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "requirement_states" in inspector.get_table_names() and "requirements_revision" in {
        column["name"] for column in inspector.get_columns("requirement_states")
    }:
        op.drop_column("requirement_states", "requirements_revision")
