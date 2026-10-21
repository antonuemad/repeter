"""Distinguish a reviewed schedule from untouched daily defaults.

Revision ID: 0010
Revises: 0009
"""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("content_plans", sa.Column("schedule_reviewed_at", sa.DateTime(timezone=True), nullable=True))
    # Preserve evidence of explicit schedule saves. No schedule or execution state is changed.
    op.execute("""
        UPDATE content_plans AS p SET schedule_reviewed_at = (
            SELECT max(a.created_at) FROM activity_log AS a
            WHERE a.workspace_id = p.workspace_id AND a.category = 'plans'
              AND a.data->>'section' = 'schedule' AND a.data->>'plan_id' = p.id::text
        )
    """)


def downgrade():
    op.drop_column("content_plans", "schedule_reviewed_at")
