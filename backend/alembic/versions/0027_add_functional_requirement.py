"""add requirement-level functional requirement text

Revision ID: 0027_functional_requirement
Revises: 0026_clarification_provenance
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0027_functional_requirement"
down_revision = "0026_clarification_provenance"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if "requirements" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("requirements")}
    if "functional_requirement" not in columns:
        op.add_column("requirements", sa.Column("functional_requirement", sa.Text(), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if "requirements" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("requirements")}
    if "functional_requirement" in columns:
        op.drop_column("requirements", "functional_requirement")
