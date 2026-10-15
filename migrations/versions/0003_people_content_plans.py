"""People, isolated libraries and draft content plans.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def timestamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)]


def upgrade():
    op.create_table("creator_profiles", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False), sa.Column("name", sa.String(200), nullable=False), *timestamps(), sa.UniqueConstraint("id", "workspace_id", name="uq_profile_workspace"))
    op.create_index("ix_creator_profiles_workspace_id", "creator_profiles", ["workspace_id"])
    for table, prefix in [("connected_accounts", "account"), ("sources", "source"), ("videos", "video")]:
        op.add_column(table, sa.Column("profile_id", sa.Integer(), nullable=True))
        op.create_index(f"ix_{table}_profile_id", table, ["profile_id"])
        op.create_foreign_key(f"fk_{prefix}_profile", table, "creator_profiles", ["profile_id", "workspace_id"], ["id", "workspace_id"])
    op.create_unique_constraint("uq_account_workspace", "connected_accounts", ["id", "workspace_id"])
    op.create_table("content_plans", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False), sa.Column("profile_id", sa.Integer(), nullable=False), sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id", ondelete="SET NULL")), sa.Column("timezone", sa.String(64), nullable=False), sa.Column("start_date", sa.Date(), nullable=False), sa.Column("weekdays", postgresql.JSONB(), nullable=False), sa.Column("times", postgresql.JSONB(), nullable=False), sa.Column("ordering", sa.String(16), nullable=False), sa.Column("revision", sa.Integer(), nullable=False), *timestamps(), sa.UniqueConstraint("id", "workspace_id", name="uq_plan_workspace"), sa.UniqueConstraint("profile_id", name="uq_plan_profile"), sa.ForeignKeyConstraint(["profile_id", "workspace_id"], ["creator_profiles.id", "creator_profiles.workspace_id"], name="fk_plan_profile", ondelete="CASCADE"))
    op.create_index("ix_content_plans_workspace_id", "content_plans", ["workspace_id"])
    op.create_table("plan_targets", sa.Column("plan_id", sa.Integer(), primary_key=True), sa.Column("account_id", sa.Integer(), primary_key=True), sa.Column("workspace_id", sa.Integer(), nullable=False), sa.ForeignKeyConstraint(["plan_id", "workspace_id"], ["content_plans.id", "content_plans.workspace_id"], name="fk_target_plan", ondelete="CASCADE"), sa.ForeignKeyConstraint(["account_id", "workspace_id"], ["connected_accounts.id", "connected_accounts.workspace_id"], name="fk_target_account", ondelete="CASCADE"))
    op.add_column("oauth_states", sa.Column("return_path", sa.String(200)))
    op.drop_index("uq_video_source_external", table_name="videos")
    op.create_index("uq_video_source_external", "videos", ["workspace_id", sa.text("COALESCE(profile_id, 0)"), "source_platform", "external_id"], unique=True, postgresql_where=sa.text("external_id IS NOT NULL"))


def downgrade():
    # Never silently discard person libraries to satisfy the old global unique index.
    op.drop_index("uq_video_source_external", table_name="videos")
    op.create_index("uq_video_source_external", "videos", ["workspace_id", "source_platform", "external_id"], unique=True, postgresql_where=sa.text("external_id IS NOT NULL"))
    op.drop_column("oauth_states", "return_path")
    op.drop_table("plan_targets")
    op.drop_table("content_plans")
    op.drop_constraint("uq_account_workspace", "connected_accounts")
    for table, prefix in [("videos", "video"), ("sources", "source"), ("connected_accounts", "account")]:
        op.drop_constraint(f"fk_{prefix}_profile", table)
        op.drop_index(f"ix_{table}_profile_id", table_name=table)
        op.drop_column(table, "profile_id")
    op.drop_table("creator_profiles")
