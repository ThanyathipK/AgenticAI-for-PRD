"""enforce one PRD snapshot per project version

Revision ID: 0028_unique_prd_version
Revises: 0027_functional_requirement
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0028_unique_prd_version"
down_revision = "0027_functional_requirement"
branch_labels = None
depends_on = None


CONSTRAINT_NAME = "uq_prd_versions_project_version"


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if "prd_versions" not in inspector.get_table_names():
        return
    existing = {item["name"] for item in inspector.get_unique_constraints("prd_versions")}
    if CONSTRAINT_NAME in existing:
        return

    duplicate = bind.execute(sa.text("""
        SELECT project_id, version_number, COUNT(*) AS duplicate_count
        FROM prd_versions
        GROUP BY project_id, version_number
        HAVING COUNT(*) > 1
        LIMIT 1
    """)).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot enforce unique PRD versions: duplicate "
            f"(project_id={duplicate.project_id}, version_number={duplicate.version_number}) "
            "rows already exist. Resolve the conflicting audit snapshots before retrying."
        )

    with op.batch_alter_table("prd_versions") as batch_op:
        batch_op.create_unique_constraint(
            CONSTRAINT_NAME,
            ["project_id", "version_number"],
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if "prd_versions" not in inspector.get_table_names():
        return
    existing = {item["name"] for item in inspector.get_unique_constraints("prd_versions")}
    if CONSTRAINT_NAME not in existing:
        return
    with op.batch_alter_table("prd_versions") as batch_op:
        batch_op.drop_constraint(CONSTRAINT_NAME, type_="unique")
