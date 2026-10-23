"""add indexes verified for bounded timeline reads

Revision ID: 0029_read_path_indexes
Revises: 0028_unique_prd_version
"""

from alembic import op
from sqlalchemy import inspect


revision = "0029_read_path_indexes"
down_revision = "0028_unique_prd_version"
branch_labels = None
depends_on = None


INDEX_NAME = "idx_conversation_messages_project_created_id"


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if "conversation_messages" not in inspector.get_table_names():
        return
    existing = {item["name"] for item in inspector.get_indexes("conversation_messages")}
    if INDEX_NAME not in existing:
        op.create_index(
            INDEX_NAME,
            "conversation_messages",
            ["project_id", "created_at", "id"],
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if "conversation_messages" not in inspector.get_table_names():
        return
    existing = {item["name"] for item in inspector.get_indexes("conversation_messages")}
    if INDEX_NAME in existing:
        op.drop_index(INDEX_NAME, table_name="conversation_messages")
