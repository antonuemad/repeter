"""One official platform source per connected account in each person's library.

Revision ID: 0005
Revises: 0004
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "uq_person_platform_source",
        "sources",
        ["profile_id", "connected_account_id"],
        unique=True,
        postgresql_where=sa.text("profile_id IS NOT NULL AND kind = 'platform_api'"),
    )


def downgrade():
    op.drop_index("uq_person_platform_source", table_name="sources")
