"""Serialized workspace quotas and auditable, manually verified bank-transfer invoices."""

from datetime import timedelta

from sqlalchemy import func, select

from repeater.config import get_settings
from repeater.messages import UserError
from repeater.models import (
    BillingInvoice,
    ConnectedAccount,
    CreatorProfile,
    ServicePlan,
    Video,
    VideoStatus,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from repeater.timeutil import utcnow

LIMIT_KEYS = ("people", "accounts", "members", "items", "storage_bytes")
CATALOG = {
    "legacy": ("Existing workspace", {}),
    "pilot": (
        "Invited pilot",
        dict(people=3, accounts=10, members=3, items=1000, storage_bytes=10 * 1024**3),
    ),
    "creator": ("Creator", dict(people=5, accounts=30, members=5, items=5000, storage_bytes=50 * 1024**3)),
    "team": ("Team", dict(people=20, accounts=100, members=15, items=20000, storage_bytes=200 * 1024**3)),
}


class LimitError(UserError):
    def __init__(self, key="commerce.limit"):
        super().__init__(
            key, "Your subscription or workspace limit does not allow this operation. Review plan usage."
        )


def work_allowed(workspace, now=None):
    return bool(
        workspace
        and not workspace.closed_at
        and (not workspace.subscription_until or workspace.subscription_until > (now or utcnow()))
    )


def catalog(db):
    # Serialize first access as create_all-based tests and a new installation may not have seeds.
    db.execute(select(func.pg_advisory_xact_lock(727008, 1)))
    for code, (name, limits) in CATALOG.items():
        if db.get(ServicePlan, code) is None:
            db.add(ServicePlan(code=code, name=name, currency="EGP", limits=limits, available=False))
    db.flush()
    return list(db.scalars(select(ServicePlan).order_by(ServicePlan.code)))


def workspace_lock(db, workspace_id):
    return db.scalar(
        select(Workspace)
        .where(Workspace.id == workspace_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )


def usage(db, workspace_id):
    counts = {
        key: db.scalar(select(func.count()).select_from(model).where(model.workspace_id == workspace_id)) or 0
        for key, model in [
            ("people", CreatorProfile),
            ("accounts", ConnectedAccount),
            ("members", WorkspaceMember),
            ("items", Video),
        ]
    }
    counts["members"] += (
        db.scalar(
            select(func.count())
            .select_from(WorkspaceInvitation)
            .where(
                WorkspaceInvitation.workspace_id == workspace_id,
                WorkspaceInvitation.used_at.is_(None),
                WorkspaceInvitation.revoked_at.is_(None),
                WorkspaceInvitation.expires_at > utcnow(),
            )
        )
        or 0
    )
    values = db.execute(
        select(Video.size_bytes, Video.encoded_size_bytes, Video.status).where(
            Video.workspace_id == workspace_id
        )
    )
    reserve = get_settings().max_upload_mb * 1024**2 * 4
    counts["storage_bytes"] = sum(
        max((a or 0) + (b or 0), reserve if status in (VideoStatus.IMPORTING, VideoStatus.PROCESSING) else 0)
        for a, b, status in values
    )
    return counts


def require_capacity(db, workspace_id, key, amount=1):
    ws = workspace_lock(db, workspace_id)
    if ws is None or ws.closed_at:
        raise LimitError("commerce.expired")
    if ws.subscription_until and ws.subscription_until <= utcnow():
        raise LimitError("commerce.expired")
    plan = db.get(ServicePlan, ws.service_plan)
    limits = ws.entitlement_limits or (plan.limits if plan else CATALOG.get(ws.service_plan, ("", {}))[1])
    maximum = limits.get(key)
    if maximum is not None and usage(db, workspace_id)[key] + amount > maximum:
        raise LimitError()


def request_invoice(db, workspace_id, plan_code):
    ws = workspace_lock(db, workspace_id)
    if not ws or ws.closed_at:
        raise LimitError("commerce.invalid")
    plan = db.get(ServicePlan, plan_code)
    if not get_settings().bank_transfer_details or not plan or not plan.available or not plan.monthly_price:
        raise LimitError("commerce.not_ready")
    existing = db.scalar(
        select(BillingInvoice).where(
            BillingInvoice.workspace_id == workspace_id,
            BillingInvoice.plan_code == plan.code,
            BillingInvoice.status.in_(["pending", "review"]),
        )
    )
    if existing:
        return existing
    invoice = BillingInvoice(
        workspace_id=workspace_id,
        plan_code=plan.code,
        amount=plan.monthly_price,
        currency=plan.currency,
        limits=dict(plan.limits),
        status="pending",
        reference="",
    )
    db.add(invoice)
    db.flush()
    return invoice


def review_invoice(db, invoice_id, actor_id, approved):
    # Same workspace-first lock order as every quota/customer mutation.
    candidate = db.get(BillingInvoice, invoice_id)
    if not candidate:
        raise LimitError("commerce.invalid")
    ws = workspace_lock(db, candidate.workspace_id)
    invoice = db.scalar(
        select(BillingInvoice)
        .where(BillingInvoice.id == invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not ws or ws.closed_at or invoice.status != "review" or not invoice.reference:
        raise LimitError("commerce.invalid")
    invoice.status = "paid" if approved else "rejected"
    invoice.reviewed_at, invoice.reviewed_by_id = utcnow(), actor_id
    if approved:
        ws.service_plan = invoice.plan_code
        ws.entitlement_limits = dict(invoice.limits)
        ws.subscription_until = max(ws.subscription_until or utcnow(), utcnow()) + timedelta(days=30)
    return invoice
