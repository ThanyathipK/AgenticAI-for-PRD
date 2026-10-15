"""Persist confidence-scored PRD narrative facts.

Revision ID: 0025_add_prd_narrative
Revises: 0024_add_req_revision
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "0025_add_prd_narrative"
down_revision: Union[str, None] = "0024_add_req_revision"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "requirement_states" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("requirement_states")}
    if "prd_narrative" not in columns:
        op.add_column(
            "requirement_states",
            sa.Column("prd_narrative", sa.JSON(), nullable=False, server_default="{}"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "requirement_states" in inspector.get_table_names() and "prd_narrative" in {
        column["name"] for column in inspector.get_columns("requirement_states")
    }:
        op.drop_column("requirement_states", "prd_narrative")
