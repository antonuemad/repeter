"""Per-person rotation settings; existing plans retain their one-pass behavior.

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("content_plans", sa.Column("rotation_mode", sa.String(16), nullable=False, server_default="once"))
    op.add_column("content_plans", sa.Column("repeat_after_days", sa.Integer(), nullable=False, server_default="30"))
    op.create_check_constraint("ck_plan_rotation_mode", "content_plans", "rotation_mode IN ('once', 'repeat')")
    op.create_check_constraint("ck_plan_repeat_days", "content_plans", "repeat_after_days BETWEEN 1 AND 365")


def downgrade():
    op.drop_constraint("ck_plan_repeat_days", "content_plans")
    op.drop_constraint("ck_plan_rotation_mode", "content_plans")
    op.drop_column("content_plans", "repeat_after_days")
    op.drop_column("content_plans", "rotation_mode")
