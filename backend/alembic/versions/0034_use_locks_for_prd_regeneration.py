"""use locks as the PRD regeneration protection mechanism

Revision ID: 0034_prd_lock_contract
Revises: 0033_req_state_index
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0034_prd_lock_contract"
down_revision = "0033_req_state_index"
branch_labels = None
depends_on = None


def _has_prd_sections() -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return (
        "prd_sections" in inspector.get_table_names()
        and {"section_key", "ai_generatable"}.issubset(
            {column["name"] for column in inspector.get_columns("prd_sections")}
        )
    )


def upgrade() -> None:
    if not _has_prd_sections():
        return
    # Manual edits used to set ai_generatable=false permanently. Restore the
    # canonical contract for all AI-managed parts, including locked ones: the
    # lock itself protects them now and unlocking should allow regeneration.
    op.execute(
        sa.text(
            "UPDATE prd_sections SET ai_generatable = true "
            "WHERE section_key NOT IN ('reviews', 'contents')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE prd_sections SET ai_generatable = false "
            "WHERE section_key IN ('reviews', 'contents')"
        )
    )


def downgrade() -> None:
    if not _has_prd_sections():
        return
    # Best-effort restoration of the former ownership rule. Historical data
    # cannot distinguish every explicit override, so only human-authored rows
    # are returned to the previous non-generatable state.
    op.execute(
        sa.text(
            "UPDATE prd_sections SET ai_generatable = false "
            "WHERE content_source = 'human'"
        )
    )
