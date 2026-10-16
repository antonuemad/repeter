"""Accounts: connect buttons, account status, reconnect and disconnect (owners only)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from repeater import accounts as account_service
from repeater.config import get_settings
from repeater.jobs import kinds
from repeater.models import PROVIDER_FOR_PLATFORM, AccountStatus, ConnectedAccount, Platform
from repeater.platforms import provider_name_for_account
from repeater.web.auth import CurrentWorkspace, WorkspaceContext, require_owner
from repeater.web.deps import DB, Form
from repeater.web.routes.oauth import PLATFORM_PURPOSES, PURPOSE_PROVIDERS
from repeater.web.templating import flash, render

router = APIRouter()

# Connect buttons in display order: one Meta login covers Instagram and Facebook.
CONNECT_BUTTONS = (
    ("tiktok", ("tiktok",)),
    ("youtube", ("youtube",)),
    ("instagram", ("instagram",)),
    ("meta", ("instagram", "facebook")),
    ("gdrive", ("gdrive",)),
)
PLATFORM_ORDER = (Platform.TIKTOK, Platform.YOUTUBE, Platform.INSTAGRAM, Platform.FACEBOOK, Platform.GDRIVE)


@router.get("/accounts")
def accounts_page(request: Request, db: DB, ctx: CurrentWorkspace) -> Response:
    settings = get_settings()
    rows = list(
        db.execute(
            select(ConnectedAccount)
            .where(ConnectedAccount.workspace_id == ctx.workspace.id)
            .order_by(ConnectedAccount.display_name, ConnectedAccount.id)
        ).scalars()
    )
    rows.sort(key=lambda a: (PLATFORM_ORDER.index(Platform(a.platform)), a.status == AccountStatus.DISABLED))
    buttons = [
        {
            "purpose": purpose,
            "platforms": platforms,
            "configured": settings.platform_configured(PURPOSE_PROVIDERS[purpose]),
        }
        for purpose, platforms in CONNECT_BUTTONS
    ]
    context: dict[str, Any] = {
        "nav": "accounts",
        "accounts": rows,
        "buttons": buttons,
        "purposes": {
            a.id: "instagram"
            if provider_name_for_account(a) == "instagram"
            else PLATFORM_PURPOSES[Platform(a.platform)]
            for a in rows
        },
        "configured": {a.id: settings.platform_configured(provider_name_for_account(a)) for a in rows},
        "can_disconnect": ctx.is_owner,
    }
    return render(request, "accounts.html", context)


def _get_account(db: Session, ctx: WorkspaceContext, account_id: int) -> ConnectedAccount:
    account = db.get(ConnectedAccount, account_id)
    if account is None or account.workspace_id != ctx.workspace.id:
        raise HTTPException(status_code=404)
    return account


@router.get("/accounts/{account_id}/disconnect")
def confirm_disconnect(request: Request, db: DB, ctx: CurrentWorkspace, account_id: int) -> Response:
    require_owner(ctx)
    account = _get_account(db, ctx, account_id)
    return render(request, "account_disconnect.html", {"nav": "accounts", "account": account})


@router.post("/accounts/{account_id}/disconnect")
def disconnect(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, account_id: int) -> Response:
    require_owner(ctx)
    account = _get_account(db, ctx, account_id)
    if account.status == AccountStatus.DISABLED and not account.token_enc:
        flash(request, "info", "accounts.already_disconnected", name=account.display_name)
        return RedirectResponse("/accounts", status_code=303)
    account_service.disconnect(db, account, actor=ctx.actor, revoke=not shares_grant(db, account))
    kinds.enqueue_plan_workspace(db, ctx.workspace.id)
    db.commit()
    key = (
        "accounts.disconnected_meta"
        if account.platform in (Platform.INSTAGRAM, Platform.FACEBOOK)
        else "accounts.disconnected"
    )
    if provider_name_for_account(account) == "instagram":
        key = "accounts.disconnected_instagram"
    flash(request, "success", key, name=account.display_name)
    return RedirectResponse("/accounts", status_code=303)


def shares_grant(db: Session, account: ConnectedAccount) -> bool:
    """True when another connected account holds the same authorization at the platform, so that
    revoking it remotely would disconnect that account too and only the local tokens are deleted.

    Google: revoking a token removes every scope the user granted to the project (YouTube and
    Drive, in any workspace), whatever refresh token is revoked, and every connect issues a new
    refresh token. So the decision is about the Google identity (extra["google_sub"], stored for
    YouTube and Drive accounts), not about token strings. Accounts connected before the identity
    was stored cannot be told apart: while any other active Google account exists the remote
    revoke is skipped (the local tokens are deleted either way).
    TikTok: revoking removes the app from the user's "Manage app permissions", i.e. the whole
    user-to-app authorization, and the same open_id can be connected in several workspaces."""
    if not account.token_enc:
        return False
    if Platform(account.platform) == Platform.TIKTOK:
        return _tiktok_user_connected_elsewhere(db, account)
    if PROVIDER_FOR_PLATFORM.get(Platform(account.platform)) != "google":
        return False
    google_platforms = [p for p, provider in PROVIDER_FOR_PLATFORM.items() if provider == "google"]
    others = list(
        db.execute(
            select(ConnectedAccount).where(
                ConnectedAccount.id != account.id,
                ConnectedAccount.platform.in_(google_platforms),
                ConnectedAccount.status != AccountStatus.DISABLED,
                ConnectedAccount.token_enc.isnot(None),
            )
        ).scalars()
    )
    sub = _google_sub(account)
    for other in others:
        other_sub = _google_sub(other)
        if sub is None or other_sub is None or sub == other_sub:
            return True  # the same user, or an identity that cannot be ruled out
    return False


def _google_sub(account: ConnectedAccount) -> str | None:
    value = (account.extra or {}).get("google_sub")
    return str(value) if value else None


def _tiktok_user_connected_elsewhere(db: Session, account: ConnectedAccount) -> bool:
    other = db.execute(
        select(ConnectedAccount.id)
        .where(
            ConnectedAccount.id != account.id,
            ConnectedAccount.platform == Platform.TIKTOK,
            ConnectedAccount.external_id == account.external_id,
            ConnectedAccount.status != AccountStatus.DISABLED,
            ConnectedAccount.token_enc.isnot(None),
        )
        .limit(1)
    ).first()
    return other is not None
