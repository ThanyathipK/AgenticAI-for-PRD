"""remove duplicate requirement state project index

Revision ID: 0033_req_state_index
Revises: 0032_trim_req_state
"""

from alembic import op
from sqlalchemy import inspect


revision = "0033_req_state_index"
down_revision = "0032_trim_req_state"
branch_labels = None
depends_on = None


INDEX_NAME = "ix_requirement_states_project_id"


def _indexes() -> set[str]:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "requirement_states" not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes("requirement_states")}


def upgrade() -> None:
    if INDEX_NAME in _indexes():
        op.drop_index(INDEX_NAME, table_name="requirement_states")


def downgrade() -> None:
    if INDEX_NAME not in _indexes():
        op.create_index(INDEX_NAME, "requirement_states", ["project_id"], unique=False)
