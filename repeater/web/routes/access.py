"""Invite-only onboarding and operator-mediated recovery (manual link delivery)."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from repeater import access
from repeater.activity import log_activity
from repeater.models import (
    ActivityLevel,
    RecoveryRequest,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from repeater.security import hash_password, verify_password
from repeater.timeutil import utcnow
from repeater.web import consent, i18n
from repeater.web.auth import CurrentWorkspace, HashingBusy, client_ip, hash_slot, require_owner, session_user
from repeater.web.deps import DB, Form, form_bool
from repeater.web.templating import flash, render

router = APIRouter()
PROOF_SECONDS = 600


def _private(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _page(request: Request, template: str, context: dict | None = None, *, status: int = 200) -> Response:
    return _private(render(request, template, context, status_code=status))


def _operator(ctx) -> None:
    if not ctx.user.is_platform_admin:
        raise HTTPException(403)


def _audit(db, ctx, event: str, **data) -> None:
    log_activity(
        db,
        ctx.workspace.id,
        ActivityLevel.INFO,
        "auth",
        event,
        actor=ctx.actor,
        event=event,
        msg_key="access.audit_" + event,
        **data,
    )


def _limit(db, request: Request, scope: str, maximum: int = 20) -> None:
    allowed = access.allow_attempt(db, scope, client_ip(request), maximum, timedelta(minutes=15))
    db.commit()
    if not allowed:
        raise HTTPException(429)


def _proof(request: Request, kind: str) -> str:
    value = request.session.get("access_" + kind)
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("until"), (int, float))
        or value["until"] <= utcnow().timestamp()
    ):
        raise access.AccessError()
    token_hash = value.get("digest")
    if not isinstance(token_hash, str) or len(token_hash) != 64:
        raise access.AccessError()
    return token_hash


def _find(db, kind, token_hash):
    return access.invitation_for(db, token_hash) if kind == "invite" else access.reset_for(db, token_hash)


def _not_signed_in(request, db) -> None:
    if session_user(request, db) is not None:
        raise access.AccessError("access.sign_out_first")


@router.get("/invite")
@router.get("/reset-password")
def open_link(request: Request) -> Response:
    kind = "invite" if request.url.path == "/invite" else "reset-password"
    return _page(request, "access_open.html", {"kind": kind, "error": None})


@router.post("/{kind}/open")
def verify_link(request: Request, db: DB, form: Form, kind: Literal["invite", "reset-password"]) -> Response:
    _limit(db, request, "open-link")
    try:
        _not_signed_in(request, db)
        token_hash = access.digest(str(form.get("token", "")))
        _find(db, kind, token_hash)
        request.session["access_" + kind] = {
            "digest": token_hash,
            "until": utcnow().timestamp() + PROOF_SECONDS,
        }
        target = "/invite/accept" if kind == "invite" else "/reset-password/confirm"
        return _private(RedirectResponse(target, 303))
    except access.AccessError as exc:
        return _page(request, "access_open.html", {"kind": kind, "error": exc.key}, status=400)


def _password_page(request, db, kind, error=None, status=200):
    try:
        _not_signed_in(request, db)
        row = _find(db, kind, _proof(request, kind))
        workspace_name = db.get(Workspace, row.workspace_id).name if kind == "invite" else None
        return _page(
            request,
            "access_password.html",
            {
                "kind": kind,
                "error": error,
                "invited_email": row.email if kind == "invite" else None,
                "workspace_name": workspace_name,
                "legal_version": consent.LEGAL_VERSION,
            },
            status=status,
        )
    except access.AccessError as exc:
        request.session.pop("access_" + kind, None)
        return _page(request, "access_open.html", {"kind": kind, "error": exc.key}, status=400)


@router.get("/invite/accept")
def invite_form(request: Request, db: DB) -> Response:
    return _password_page(request, db, "invite")


@router.get("/reset-password/confirm")
def reset_form(request: Request, db: DB) -> Response:
    return _password_page(request, db, "reset-password")


def _redeem(request, db, form, kind):
    _limit(db, request, "set-password")
    try:
        _not_signed_in(request, db)
        token_hash = _proof(request, kind)
        _find(db, kind, token_hash)
        password = str(form.get("password", ""))  # Spaces/Unicode are intentional; never strip.
        access.validate_password(password, str(form.get("confirmation", "")))
        if kind == "invite" and (
            not form_bool(form, "accept") or form.get("legal_version") != consent.LEGAL_VERSION
        ):
            raise access.AccessError("access.accept_terms")
        db.rollback()  # No DB connection/row lock held during memory-expensive hashing.
        with hash_slot():
            hashed = hash_password(password)
        if kind == "invite":
            user = access.redeem_invitation(db, token_hash, hashed)
            consent.record_consent(request.session, db, user)
        else:
            user = access.redeem_reset(db, token_hash, hashed)
        membership = db.execute(
            select(WorkspaceMember).where(WorkspaceMember.user_id == user.id).limit(1)
        ).scalar_one_or_none()
        event = "invitation_accepted" if kind == "invite" else "password_reset"
        log_activity(
            db,
            membership.workspace_id if membership else None,
            ActivityLevel.INFO,
            "auth",
            event,
            actor=user.email[:64],
            event=event,
            user_id=user.id,
            msg_key="access.audit_" + event,
        )
        db.commit()
        request.session.clear()
        flash(request, "success", "access.password_saved")
        return _private(RedirectResponse("/login", 303))
    except access.AccessError as exc:
        db.rollback()
        return _password_page(request, db, kind, exc.key, 400)
    except IntegrityError:
        db.rollback()
        return _password_page(request, db, kind, "access.email_unavailable", 400)
    except HashingBusy:
        db.rollback()
        return _password_page(request, db, kind, "access.busy", 503)


@router.post("/invite/accept")
def accept_invite(request: Request, db: DB, form: Form) -> Response:
    return _redeem(request, db, form, "invite")


@router.post("/reset-password/confirm")
def accept_reset(request: Request, db: DB, form: Form) -> Response:
    return _redeem(request, db, form, "reset-password")


@router.get("/forgot-password")
def forgot_form(request: Request) -> Response:
    return _page(request, "access_forgot.html", {"submitted": False, "error": None})


@router.post("/forgot-password")
def forgot(request: Request, db: DB, form: Form) -> Response:
    try:
        email = access.email_address(str(form.get("email", "")))
    except access.AccessError as exc:
        return _page(request, "access_forgot.html", {"submitted": False, "error": exc.key}, status=400)
    # Both checks run for every address, including an unknown one. Commit rejected attempts too.
    ip_ok = access.allow_attempt(db, "recovery-ip", client_ip(request), 10, timedelta(hours=1))
    email_ok = access.allow_attempt(db, "recovery-email", email, 3, timedelta(hours=1))
    if ip_ok and email_ok:
        access.request_recovery(db, email)
    db.commit()
    return _page(request, "access_forgot.html", {"submitted": True, "error": None})


@router.get("/settings/members")
def members(request: Request, db: DB, ctx: CurrentWorkspace) -> Response:
    people = db.execute(
        select(User.email, WorkspaceMember.role)
        .join(WorkspaceMember, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == ctx.workspace.id)
        .order_by(User.id)
        .limit(100)
    ).all()
    invitations = (
        db.execute(
            select(WorkspaceInvitation)
            .where(
                WorkspaceInvitation.workspace_id == ctx.workspace.id,
                WorkspaceInvitation.used_at.is_(None),
                WorkspaceInvitation.revoked_at.is_(None),
                WorkspaceInvitation.expires_at > utcnow(),
            )
            .order_by(WorkspaceInvitation.id.desc())
            .limit(100)
        )
        .scalars()
        .all()
        if ctx.is_owner
        else []
    )
    return _page(
        request,
        "settings_members.html",
        {"nav": "settings", "people": people, "invitations": invitations, "can_invite": ctx.is_owner},
    )


def _issued(request, url, destination, kind):
    return _page(
        request,
        "access_issued.html",
        {
            "nav": "customers" if destination.startswith("/admin/") else "settings",
            "issued_link": url,
            "destination": destination,
            "kind": kind,
        },
    )


@router.post("/settings/members/invite")
def invite_member(request: Request, db: DB, ctx: CurrentWorkspace, form: Form) -> Response:
    require_owner(ctx)
    _limit(db, request, "issue-invite", 20)
    try:
        row, url = access.issue_invitation(
            db, ctx.workspace.id, str(form.get("email", "")), "editor", ctx.user.id
        )
        _audit(db, ctx, "invitation_created", invitation_id=row.id)
        db.commit()
        return _issued(request, url, "/settings/members", "invite")
    except access.AccessError as exc:
        db.rollback()
        flash(request, "error", exc.key)
        return RedirectResponse("/settings/members", 303)


@router.post("/settings/members/{invitation_id}/revoke")
def revoke(request: Request, db: DB, ctx: CurrentWorkspace, invitation_id: int) -> Response:
    require_owner(ctx)
    if not access.revoke_invitation(db, ctx.workspace.id, invitation_id):
        raise HTTPException(404)
    _audit(db, ctx, "invitation_revoked", invitation_id=invitation_id)
    db.commit()
    flash(request, "success", "access.revoked")
    return RedirectResponse("/settings/members", 303)


@router.get("/admin/customers")
def customers(request: Request, db: DB, ctx: CurrentWorkspace, before: int = 0) -> Response:
    _operator(ctx)
    query = select(Workspace).order_by(Workspace.id.desc()).limit(51)
    if before > 0:
        query = query.where(Workspace.id < before)
    rows = db.execute(query).scalars().all()
    return _page(
        request,
        "admin_customers.html",
        {
            "nav": "customers",
            "customers": rows[:50],
            "next_before": rows[49].id if len(rows) > 50 else None,
            "invitations": db.execute(
                select(WorkspaceInvitation)
                .where(
                    WorkspaceInvitation.used_at.is_(None),
                    WorkspaceInvitation.revoked_at.is_(None),
                    WorkspaceInvitation.expires_at > utcnow(),
                )
                .order_by(WorkspaceInvitation.id.desc())
                .limit(100)
            )
            .scalars()
            .all(),
        },
    )


@router.post("/admin/customers")
def create_customer(request: Request, db: DB, ctx: CurrentWorkspace, form: Form) -> Response:
    _operator(ctx)
    _limit(db, request, "create-customer", 20)
    try:
        name = str(form.get("name", "")).strip()
        if not name or len(name) > 200:
            raise access.AccessError("access.workspace_name")
        locale = str(form.get("locale", "ar"))
        if locale not in i18n.SUPPORTED:
            raise access.AccessError("access.invalid_locale")
        workspace = Workspace(name=name, locale=locale, paused=True)
        db.add(workspace)
        db.flush()
        row, url = access.issue_invitation(db, workspace.id, str(form.get("email", "")), "owner", ctx.user.id)
        _audit(db, ctx, "customer_created", customer_workspace_id=workspace.id, invitation_id=row.id)
        db.commit()
        return _issued(request, url, "/admin/customers", "invite")
    except access.AccessError as exc:
        db.rollback()
        flash(request, "error", exc.key)
        return RedirectResponse("/admin/customers", 303)


@router.get("/admin/recovery")
def recovery_queue(request: Request, db: DB, ctx: CurrentWorkspace) -> Response:
    _operator(ctx)
    rows = (
        db.execute(
            select(RecoveryRequest)
            .where(RecoveryRequest.status == "pending")
            .order_by(RecoveryRequest.id)
            .limit(50)
        )
        .scalars()
        .all()
    )
    return _page(
        request,
        "admin_recovery.html",
        {"nav": "customers", "requests": [(r, access.recovery_email(r)) for r in rows]},
    )


@router.post("/admin/recovery/{request_id}")
def handle_recovery(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, request_id: int) -> Response:
    _operator(ctx)
    _limit(db, request, "issue-recovery", 10)
    if str(form.get("action")) == "close":
        row = db.execute(
            select(RecoveryRequest).where(RecoveryRequest.id == request_id).with_for_update()
        ).scalar_one_or_none()
        if row is None or row.status != "pending":
            raise HTTPException(404)
        row.status, row.handled_at, row.handled_by_id = "closed", utcnow(), ctx.user.id
        _audit(db, ctx, "recovery_closed", recovery_request_id=row.id)
        db.commit()
        return RedirectResponse("/admin/recovery", 303)
    try:
        if not form_bool(form, "verified"):
            raise access.AccessError("access.verify_identity")
        password = str(form.get("current_password", ""))
        if not 1 <= len(password) <= 1024:
            raise access.AccessError("access.reauth_failed")
        actor_id, actor_hash = ctx.user.id, ctx.user.password_hash
        db.rollback()
        with hash_slot():
            valid = verify_password(actor_hash, password)
        actor = db.get(User, actor_id, populate_existing=True)
        if (
            not valid
            or not actor.is_active
            or not actor.is_platform_admin
            or actor.password_hash != actor_hash
        ):
            raise access.AccessError("access.reauth_failed")
        row = db.execute(
            select(RecoveryRequest).where(RecoveryRequest.id == request_id).with_for_update()
        ).scalar_one_or_none()
        if row is None or row.status != "pending":
            raise HTTPException(404)
        user = db.execute(
            select(User).where(func.lower(User.email) == access.recovery_email(row))
        ).scalar_one_or_none()
        if user is None or not user.is_active or user.is_platform_admin:
            raise access.AccessError("access.recovery_unavailable")
        _, url = access.issue_reset(db, user.id, actor_id)
        row.status, row.handled_at, row.handled_by_id = "issued", utcnow(), actor_id
        _audit(db, ctx, "recovery_issued", recovery_request_id=row.id, user_id=user.id)
        db.commit()
        return _issued(request, url, "/admin/recovery", "reset-password")
    except access.AccessError as exc:
        db.rollback()
        flash(request, "error", exc.key)
        return RedirectResponse("/admin/recovery", 303)
    except HashingBusy:
        db.rollback()
        flash(request, "error", "access.busy")
        return RedirectResponse("/admin/recovery", 303)


@router.post("/admin/customers/{workspace_id}/invite")
def invite_customer_owner(
    request: Request, db: DB, ctx: CurrentWorkspace, form: Form, workspace_id: int
) -> Response:
    _operator(ctx)
    _limit(db, request, "issue-invite", 20)
    try:
        row, url = access.issue_invitation(db, workspace_id, str(form.get("email", "")), "owner", ctx.user.id)
        _audit(db, ctx, "invitation_created", invitation_id=row.id, customer_workspace_id=workspace_id)
        db.commit()
        return _issued(request, url, "/admin/customers", "invite")
    except access.AccessError as exc:
        db.rollback()
        flash(request, "error", exc.key)
        return RedirectResponse("/admin/customers", 303)


@router.post("/admin/customers/{workspace_id}/invitations/{invitation_id}/revoke")
def revoke_customer_invitation(
    request: Request, db: DB, ctx: CurrentWorkspace, workspace_id: int, invitation_id: int
) -> Response:
    _operator(ctx)
    if not access.revoke_invitation(db, workspace_id, invitation_id):
        raise HTTPException(404)
    _audit(db, ctx, "invitation_revoked", invitation_id=invitation_id, customer_workspace_id=workspace_id)
    db.commit()
    return RedirectResponse("/admin/customers", 303)
