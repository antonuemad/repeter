"""Calendar edits, mail delivery, manual subscriptions and workspace lifecycle.

Revision ID: 0008
Revises: 0007
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade():
    op.add_column("users", sa.Column("session_version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column(
        "workspaces", sa.Column("service_plan", sa.String(32), nullable=False, server_default="legacy")
    )
    op.add_column("workspaces", sa.Column("entitlement_limits", JSONB(), nullable=False, server_default="{}"))
    op.add_column("workspaces", sa.Column("subscription_until", sa.DateTime(timezone=True)))
    op.add_column("workspaces", sa.Column("closed_at", sa.DateTime(timezone=True)))
    op.create_table(
        "plan_slot_overrides",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("slot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("video_id", sa.Integer(), sa.ForeignKey("videos.id", ondelete="SET NULL")),
        sa.Column("replacement", sa.Boolean(), nullable=False),
        sa.Column("excluded", sa.Boolean(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("plan_id", "account_id", "slot_at", name="uq_slot_override"),
        sa.ForeignKeyConstraint(
            ["plan_id", "workspace_id"],
            ["content_plans.id", "content_plans.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "workspace_id"],
            ["connected_accounts.id", "connected_accounts.workspace_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_plan_slot_overrides_workspace_id", "plan_slot_overrides", ["workspace_id"])
    op.create_table(
        "service_plans",
        sa.Column("code", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("monthly_price", sa.Integer()),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("limits", JSONB(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        *timestamps(),
    )
    op.get_bind().exec_driver_sql(
        "INSERT INTO service_plans (code, name, currency, limits, available) VALUES ('legacy', 'Existing workspace', 'EGP', '{}', false), ('pilot', 'Invited pilot', 'EGP', '{\"people\":3,\"accounts\":10,\"members\":3,\"items\":1000,\"storage_bytes\":10737418240}', false), ('creator', 'Creator', 'EGP', '{\"people\":5,\"accounts\":30,\"members\":5,\"items\":5000,\"storage_bytes\":53687091200}', false), ('team', 'Team', 'EGP', '{\"people\":20,\"accounts\":100,\"members\":15,\"items\":20000,\"storage_bytes\":214748364800}', false)"
    )
    op.create_table(
        "billing_invoices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("plan_code", sa.String(32), sa.ForeignKey("service_plans.code"), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reference", sa.String(200), nullable=False),
        sa.Column("reviewed_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("limits", JSONB(), nullable=False),
        *timestamps(),
        sa.CheckConstraint("amount > 0", name="ck_invoice_amount"),
    )
    op.create_index("ix_billing_invoices_workspace_id", "billing_invoices", ["workspace_id"])
    op.create_table(
        "mail_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE")),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column(
            "invitation_id", sa.Integer(), sa.ForeignKey("workspace_invitations.id", ondelete="CASCADE")
        ),
        sa.Column("reset_id", sa.Integer(), sa.ForeignKey("password_resets.id", ondelete="CASCADE")),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload_enc", sa.Text()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_mail_outbox_workspace_id", "mail_outbox", ["workspace_id"])


def downgrade():
    # Never silently reopen a closed workspace or weaken edited executable schedules.
    connection = op.get_bind()
    if (
        connection.scalar(sa.text("SELECT count(*) FROM workspaces WHERE closed_at IS NOT NULL"))
        or connection.scalar(sa.text("SELECT count(*) FROM plan_slot_overrides"))
        or connection.scalar(sa.text("SELECT count(*) FROM billing_invoices"))
    ):
        raise RuntimeError(
            "Archive customer operations before downgrade; closure, calendar edits or invoices exist"
        )
    for table in ("mail_outbox", "billing_invoices", "service_plans", "plan_slot_overrides"):
        op.drop_table(table)
    for column in ("closed_at", "subscription_until", "entitlement_limits", "service_plan"):
        op.drop_column("workspaces", column)
    op.drop_column("users", "session_version")
