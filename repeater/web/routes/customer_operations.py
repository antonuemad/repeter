"""Tenant-safe billing, member lifecycle and operator delivery visibility."""

import re
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, update

from repeater import commerce
from repeater.activity import log_activity
from repeater.config import get_settings
from repeater.models import (
    ActivityLevel,
    BillingInvoice,
    ConnectedAccount,
    ContentPlan,
    MailOutbox,
    OAuthState,
    RotationRule,
    Source,
    User,
    Video,
    VideoStatus,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from repeater.security import verify_password
from repeater.timeutil import utcnow
from repeater.web.auth import CurrentWorkspace, hash_slot, require_owner, start_session
from repeater.web.deps import DB, Form, FormError, form_bool, form_int, form_str
from repeater.web.templating import flash, render

router = APIRouter()


def operator(ctx):
    if not ctx.user.is_platform_admin:
        raise HTTPException(403)


def reauthenticate(db, ctx, form):
    password = form_str(form, "current_password", max_len=1024, required=True)
    user = db.get(User, ctx.user.id, populate_existing=True)
    with hash_slot():
        if not user.is_active or not verify_password(user.password_hash, password):
            raise FormError("access.reauth_failed")


def current_owner(db, ctx):
    commerce.workspace_lock(db, ctx.workspace.id)
    member = db.get(WorkspaceMember, (ctx.workspace.id, ctx.user.id), populate_existing=True)
    if not member or member.role != "owner":
        raise HTTPException(403)


@router.get("/settings/billing")
def billing(request: Request, db: DB, ctx: CurrentWorkspace):
    plans = commerce.catalog(db)
    db.commit()
    current = db.get(Workspace, ctx.workspace.id)
    return render(
        request,
        "billing.html",
        {
            "nav": "settings",
            "plans": plans,
            "current": current,
            "usage": commerce.usage(db, current.id),
            "limits": current.entitlement_limits
            or next((p.limits for p in plans if p.code == current.service_plan), {}),
            "bank_details": get_settings().bank_transfer_details,
            "invoices": list(
                db.scalars(
                    select(BillingInvoice)
                    .where(BillingInvoice.workspace_id == current.id)
                    .order_by(BillingInvoice.id.desc())
                    .limit(100)
                )
            ),
        },
    )


@router.post("/settings/billing/request")
def request_invoice(request: Request, db: DB, ctx: CurrentWorkspace, form: Form):
    require_owner(ctx)
    current_owner(db, ctx)
    commerce.request_invoice(db, ctx.workspace.id, form_str(form, "plan_code", max_len=32, required=True))
    db.commit()
    flash(request, "success", "commerce.saved")
    return RedirectResponse("/settings/billing", 303)


@router.post("/settings/billing/{invoice_id}/reference")
def reference(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, invoice_id: int):
    require_owner(ctx)
    current_owner(db, ctx)
    invoice = db.scalar(
        select(BillingInvoice)
        .where(BillingInvoice.id == invoice_id, BillingInvoice.workspace_id == ctx.workspace.id)
        .with_for_update()
    )
    if not invoice:
        raise HTTPException(404)
    if invoice.status not in ("pending", "review"):
        raise HTTPException(409)
    invoice.reference = form_str(form, "reference", max_len=200, required=True)
    invoice.status = "review"
    db.commit()
    flash(request, "success", "commerce.saved")
    return RedirectResponse("/settings/billing", 303)


@router.get("/admin/billing")
def admin_billing(request: Request, db: DB, ctx: CurrentWorkspace):
    operator(ctx)
    plans = commerce.catalog(db)
    db.commit()
    return render(
        request,
        "admin_billing.html",
        {
            "nav": "operations",
            "plans": plans,
            "invoices": list(
                db.scalars(select(BillingInvoice).order_by(BillingInvoice.id.desc()).limit(100))
            ),
            "limit_keys": commerce.LIMIT_KEYS,
        },
    )


@router.post("/admin/billing/plans/{code}")
def save_plan(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, code: str):
    operator(ctx)
    try:
        reauthenticate(db, ctx, form)
        plan = db.scalar(
            select(commerce.ServicePlan).where(commerce.ServicePlan.code == code).with_for_update()
        )
        if not plan or code == "legacy":
            raise HTTPException(404)
        try:
            raw = Decimal(str(form.get("monthly_price"))) if form.get("monthly_price") else None
            if raw is not None and (
                not raw.is_finite()
                or not Decimal("0.01") <= raw <= Decimal("1000000")
                or raw.as_tuple().exponent < -2
            ):
                raise ValueError
            price = int(raw * 100) if raw is not None else None
        except (ValueError, InvalidOperation):
            raise FormError("commerce.invalid") from None
        currency = form_str(form, "currency", max_len=3).upper()
        if not re.fullmatch("[A-Z]{3}", currency):
            raise FormError("commerce.invalid")
        limits = {
            key: form_int(form, key, minimum=1, maximum=1_000_000)
            for key in commerce.LIMIT_KEYS
            if key != "storage_bytes"
        }
        limits["storage_bytes"] = form_int(form, "storage_gb", minimum=1, maximum=1_000_000) * 1024**3
        plan.monthly_price, plan.currency, plan.limits = price, currency, limits
        plan.available = form_bool(form, "available") and price is not None
        db.commit()
        flash(request, "success", "commerce.saved")
    except FormError as exc:
        db.rollback()
        flash(request, "error", exc.key)
    return RedirectResponse("/admin/billing", 303)


@router.post("/admin/billing/{invoice_id}/review")
def review_invoice(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, invoice_id: int):
    operator(ctx)
    try:
        reauthenticate(db, ctx, form)
        action = form_str(form, "action", max_len=20)
        if action not in ("approve", "reject") or (action == "approve" and not form_bool(form, "verified")):
            raise FormError("commerce.invalid")
        commerce.review_invoice(db, invoice_id, ctx.user.id, action == "approve")
        db.commit()
        flash(request, "success", "commerce.saved")
    except (FormError, commerce.LimitError) as exc:
        db.rollback()
        flash(request, "error", exc.key)
    return RedirectResponse("/admin/billing", 303)


@router.post("/settings/members/{user_id}/access")
def member_access(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, user_id: int):
    require_owner(ctx)
    try:
        reauthenticate(db, ctx, form)
        current_owner(db, ctx)
        member = db.get(WorkspaceMember, (ctx.workspace.id, user_id), populate_existing=True)
        user = db.get(User, user_id, populate_existing=True)
        if not member or not user:
            raise HTTPException(404)
        role = form_str(form, "role", max_len=16)
        owners = db.scalar(
            select(func.count())
            .select_from(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == ctx.workspace.id, WorkspaceMember.role == "owner")
        )
        if (
            role not in ("owner", "editor", "suspended")
            or user.is_platform_admin
            or (member.role == "owner" and role != "owner" and owners <= 1)
        ):
            raise FormError("lifecycle.last_owner")
        member.role = role
        user.session_version += 1
        log_activity(
            db,
            ctx.workspace.id,
            ActivityLevel.INFO,
            "auth",
            "Member access updated.",
            actor=ctx.actor,
            msg_key="lifecycle.audit_member",
            user_id=user.id,
            role=role,
        )
        db.commit()
        flash(request, "success", "lifecycle.saved")
    except FormError as exc:
        db.rollback()
        flash(request, "error", exc.key)
    return RedirectResponse("/settings/members", 303)


@router.post("/settings/sessions/revoke")
def revoke_sessions(request: Request, db: DB, ctx: CurrentWorkspace, form: Form):
    reauthenticate(db, ctx, form)
    user = db.scalar(
        select(User).where(User.id == ctx.user.id).with_for_update().execution_options(populate_existing=True)
    )
    user.session_version += 1
    db.commit()
    start_session(request, user)
    flash(request, "success", "lifecycle.saved")
    return RedirectResponse("/settings/members", 303)


@router.post("/settings/workspace/close")
def close_workspace(request: Request, db: DB, ctx: CurrentWorkspace, form: Form):
    require_owner(ctx)
    reauthenticate(db, ctx, form)
    current_owner(db, ctx)
    ws = db.get(Workspace, ctx.workspace.id)
    if ctx.user.is_platform_admin:
        raise FormError("lifecycle.last_owner")
    if form_str(form, "confirmation", max_len=200) != ws.name:
        raise FormError("calendar.invalid")
    ws.closed_at, ws.paused = utcnow(), True
    from repeater.accounts import disconnect
    from repeater.engine.people import cancel_pending

    for plan in db.scalars(select(ContentPlan).where(ContentPlan.workspace_id == ws.id)):
        cancel_pending(db, plan)
        plan.enabled = False
        plan.revision += 1
    for account in list(db.scalars(select(ConnectedAccount).where(ConnectedAccount.workspace_id == ws.id))):
        disconnect(db, account, actor=ctx.actor, revoke=False)
    db.execute(update(Source).where(Source.workspace_id == ws.id).values(enabled=False))
    db.execute(
        update(Video)
        .where(Video.workspace_id == ws.id, Video.status.in_([VideoStatus.IMPORTING, VideoStatus.PROCESSING]))
        .values(status=VideoStatus.ARCHIVED)
    )
    db.execute(update(RotationRule).where(RotationRule.workspace_id == ws.id).values(enabled=False))
    db.execute(
        update(WorkspaceInvitation)
        .where(WorkspaceInvitation.workspace_id == ws.id, WorkspaceInvitation.used_at.is_(None))
        .values(revoked_at=utcnow())
    )
    db.execute(
        update(MailOutbox)
        .where(MailOutbox.workspace_id == ws.id, MailOutbox.status == "queued")
        .values(status="expired", payload_enc=None)
    )
    from sqlalchemy import delete

    db.execute(delete(OAuthState).where(OAuthState.workspace_id == ws.id))
    for user in db.scalars(
        select(User)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(WorkspaceMember.workspace_id == ws.id)
    ):
        user.session_version += 1
    log_activity(
        db,
        ws.id,
        ActivityLevel.WARNING,
        "auth",
        "Workspace closed.",
        actor=ctx.actor,
        msg_key="lifecycle.audit_closed",
    )
    db.commit()
    request.session.clear()
    return RedirectResponse("/login", 303)


@router.post("/admin/customers/{workspace_id}/reopen")
def reopen_workspace(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, workspace_id: int):
    operator(ctx)
    reauthenticate(db, ctx, form)
    ws = commerce.workspace_lock(db, workspace_id)
    if not ws or not ws.closed_at:
        raise HTTPException(404)
    ws.closed_at, ws.paused = None, True
    log_activity(
        db,
        ws.id,
        ActivityLevel.INFO,
        "auth",
        "Workspace reopened in paused state.",
        actor=ctx.actor,
        msg_key="lifecycle.audit_reopened",
    )
    db.commit()
    flash(request, "success", "lifecycle.saved")
    return RedirectResponse("/admin/customers", 303)


@router.get("/admin/operations")
def operations(request: Request, db: DB, ctx: CurrentWorkspace):
    operator(ctx)
    from pathlib import Path

    from repeater import backups, readiness

    statuses = dict(db.execute(select(MailOutbox.status, func.count()).group_by(MailOutbox.status)).all())
    health = readiness.report(db)
    marker = get_settings().backup_status_file
    drill = (
        backups.receipt(Path(marker).parent / "last_recovery_drill_success", max_hours=24 * 8)
        if marker
        else backups.Receipt("not_configured")
    )
    return render(
        request,
        "operations.html",
        {
            "nav": "operations",
            "mail_ready": get_settings().mail_configured,
            "mail_statuses": statuses,
            "bank_ready": bool(get_settings().bank_transfer_details),
            "health": health,
            "drill": drill,
        },
    )
