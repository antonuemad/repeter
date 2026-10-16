"""Explicit person execution and durable schedule provenance.

Revision ID: 0007
Revises: 0006
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("content_plans", sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("scheduled_posts", sa.Column("plan_id", sa.Integer(), nullable=True))
    op.add_column("scheduled_posts", sa.Column("plan_revision", sa.Integer(), nullable=True))
    op.add_column("scheduled_posts", sa.Column("slot_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_post_plan", "scheduled_posts", "content_plans", ["plan_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_scheduled_posts_plan_id", "scheduled_posts", ["plan_id"])
    op.create_unique_constraint(
        "uq_plan_slot", "scheduled_posts", ["plan_id", "plan_revision", "connected_account_id", "slot_at"]
    )


def downgrade():
    # Older workers must not see executable person work after losing its plan provenance.
    op.execute(
        "UPDATE scheduled_posts SET status='canceled' WHERE created_by='plan' AND status IN ('scheduled','failed')"
    )
    op.drop_constraint("uq_plan_slot", "scheduled_posts", type_="unique")
    op.drop_index("ix_scheduled_posts_plan_id", table_name="scheduled_posts")
    op.drop_constraint("fk_post_plan", "scheduled_posts", type_="foreignkey")
    for column in ("slot_at", "plan_revision", "plan_id"):
        op.drop_column("scheduled_posts", column)
    op.drop_column("content_plans", "enabled")
