"""add clarification provenance and immutable answer context

Revision ID: 0026_clarification_provenance
Revises: 0025_add_prd_narrative
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0026_clarification_provenance"
down_revision = "0025_add_prd_narrative"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if "clarification_questions" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("clarification_questions")}
    if "provenance" not in columns:
        op.add_column("clarification_questions", sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"))
    if "target_requirement_id" not in columns:
        op.add_column("clarification_questions", sa.Column("target_requirement_id", sa.String(length=100), nullable=True))
    if "requirement_context" not in columns:
        op.add_column("clarification_questions", sa.Column("requirement_context", sa.Text(), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if "clarification_questions" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("clarification_questions")}
    for column in ("requirement_context", "target_requirement_id", "provenance"):
        if column in columns:
            op.drop_column("clarification_questions", column)
