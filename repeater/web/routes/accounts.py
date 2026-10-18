"""Accounts: connect buttons, account status, reconnect and disconnect (owners only)."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from repeater import accounts as account_service
from repeater.config import get_settings
from repeater.crypto import CryptoError
from repeater.jobs import kinds
from repeater.models import (
    PROVIDER_FOR_PLATFORM,
    AccountStatus,
    ConnectedAccount,
    ContentPlan,
    CreatorProfile,
    Platform,
)
from repeater.platforms import provider_name_for_account, tiktok
from repeater.platforms.base import AuthError, PlatformError
from repeater.timeutil import utcnow
from repeater.web.auth import CurrentWorkspace, WorkspaceContext, require_owner
from repeater.web.deps import DB, Form, form_str
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
BULK_CONFIRM_SECONDS = 15 * 60


def _bulk_signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="account-bulk-disconnect-v1")


def _bulk_scope(value: str) -> str:
    if value != "all" and value not in {p.value for p in PLATFORM_ORDER}:
        raise HTTPException(400)
    return value


def _bulk_accounts(db: Session, workspace_id: int, scope: str) -> list[ConnectedAccount]:
    query = select(ConnectedAccount).where(
        ConnectedAccount.workspace_id == workspace_id,
        or_(ConnectedAccount.status != AccountStatus.DISABLED, ConnectedAccount.token_enc.isnot(None)),
    )
    if scope != "all":
        query = query.where(ConnectedAccount.platform == Platform(scope))
    return list(db.scalars(query.order_by(ConnectedAccount.id)))


def _bulk_digest(accounts: list[ConnectedAccount]) -> str:
    # Never expose account tokens in the signed (readable) confirmation payload. Include their
    # ciphertext in the digest so a reconnect after review requires a fresh confirmation too.
    snapshot = [(a.id, a.platform, a.status, a.token_enc, a.updated_at.isoformat()) for a in accounts]
    return hashlib.sha256(json.dumps(snapshot).encode()).hexdigest()


def _bulk_confirmation(
    request: Request,
    ctx: WorkspaceContext,
    rows: list[ConnectedAccount],
    scope: str,
    *,
    changed: bool = False,
) -> Response:
    response = render(
        request,
        "accounts_disconnect.html",
        {
            "nav": "accounts",
            "accounts": rows,
            "scope": scope,
            "changed": changed,
            "confirmation": _bulk_signer().dumps([ctx.workspace.id, ctx.user.id, scope, _bulk_digest(rows)]),
            "has_meta": any(provider_name_for_account(a) == "meta" for a in rows),
            "has_instagram": any(provider_name_for_account(a) == "instagram" for a in rows),
            "has_google": any(a.platform in (Platform.YOUTUBE, Platform.GDRIVE) for a in rows),
        },
        status_code=409 if changed else 200,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


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
        "disconnect_count": sum(a.status != AccountStatus.DISABLED or bool(a.token_enc) for a in rows),
        "disconnect_platforms": [
            (
                p.value,
                sum(
                    a.platform == p and (a.status != AccountStatus.DISABLED or bool(a.token_enc))
                    for a in rows
                ),
            )
            for p in PLATFORM_ORDER
        ],
        "people": {
            row.id: row
            for row in db.execute(
                select(CreatorProfile.id, CreatorProfile.name, ContentPlan.id.label("plan_id"))
                .join(ContentPlan, ContentPlan.profile_id == CreatorProfile.id)
                .where(CreatorProfile.workspace_id == ctx.workspace.id)
            )
        },
    }
    return render(request, "accounts.html", context)


@router.get("/accounts/disconnect")
def confirm_bulk_disconnect(request: Request, db: DB, ctx: CurrentWorkspace, scope: str = "all") -> Response:
    require_owner(ctx)
    scope = _bulk_scope(scope)
    return _bulk_confirmation(request, ctx, _bulk_accounts(db, ctx.workspace.id, scope), scope)


@router.post("/accounts/disconnect")
def bulk_disconnect(request: Request, db: DB, ctx: CurrentWorkspace, form: Form) -> Response:
    require_owner(ctx)
    try:
        payload = _bulk_signer().loads(
            form_str(form, "confirmation", max_len=2000), max_age=BULK_CONFIRM_SECONDS
        )
    except BadSignature:
        payload = None
    if (
        not isinstance(payload, list)
        or len(payload) != 4
        or payload[:2] != [ctx.workspace.id, ctx.user.id]
        or not isinstance(payload[2], str)
    ):
        flash(request, "warning", "accounts.bulk_expired")
        return RedirectResponse("/accounts", status_code=303)
    scope = _bulk_scope(payload[2])
    rows = _bulk_accounts(db, ctx.workspace.id, scope)
    if payload[3] != _bulk_digest(rows):
        return _bulk_confirmation(request, ctx, rows, scope, changed=True)
    if not rows:
        return RedirectResponse("/accounts", status_code=303)
    for account in rows:
        account_service.disconnect(db, account, actor=ctx.actor, revoke=not shares_grant(db, account))
        db.flush()  # The next shared-grant check must see tokens already removed in this batch.
    kinds.enqueue_plan_workspace(db, ctx.workspace.id)
    db.commit()
    flash(request, "success", "accounts.bulk_disconnected", count=len(rows))
    return RedirectResponse("/accounts", status_code=303)


def _get_account(db: Session, ctx: WorkspaceContext, account_id: int) -> ConnectedAccount:
    account = db.get(ConnectedAccount, account_id)
    if account is None or account.workspace_id != ctx.workspace.id:
        raise HTTPException(status_code=404)
    return account


def _tiktok_link(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        valid = parsed.scheme == "https" and (host == "tiktok.com" or host.endswith(".tiktok.com"))
        return value if valid and not parsed.username and not parsed.password else None
    except ValueError:
        return None


@router.get("/accounts/{account_id}/videos")
def tiktok_videos(request: Request, db: DB, ctx: CurrentWorkspace, account_id: int) -> Response:
    account = _get_account(db, ctx, account_id)
    if account.platform != Platform.TIKTOK:
        raise HTTPException(404)
    context: dict[str, Any] = {
        "nav": "accounts",
        "account": account,
        "videos": [],
        "checked_at": None,
        "error": None,
        "reconnect": False,
        "can_reconnect": ctx.is_owner,
        "profile_name": account.display_name,
        "plan": db.scalar(
            select(ContentPlan).where(
                ContentPlan.workspace_id == ctx.workspace.id,
                ContentPlan.profile_id == account.profile_id,
            )
        )
        if account.profile_id
        else None,
    }
    if not get_settings().platform_configured("tiktok"):
        context["error"] = "tiktok_read.not_configured"
    elif account.status == AccountStatus.DISABLED or not account.token_enc:
        context.update(error="tiktok_read.reconnect", reconnect=True)
    elif not {"user.info.basic", "video.list"}.issubset(set(account.scopes.split())):
        context.update(error="tiktok_read.scopes", reconnect=True)
    else:
        try:
            authorized = account_service.fresh_context(db, account)
            db.commit()  # Persist a rotated refresh token before making the read requests.
            profile = tiktok.read_profile(authorized)
            videos = tiktok.lister.list_recent(authorized, limit=20)
            context.update(
                profile_name=profile.get("display_name") or account.display_name,
                videos=[{"item": video, "link": _tiktok_link(video.permalink)} for video in videos],
                checked_at=utcnow(),
            )
        except account_service.AccountUnavailable:
            db.commit()
            context.update(error="tiktok_read.reconnect", reconnect=True)
        except (AuthError, CryptoError):
            db.rollback()
            context.update(error="tiktok_read.reconnect", reconnect=True)
        except PlatformError:
            db.rollback()
            context["error"] = "tiktok_read.unavailable"
    response = render(request, "account_tiktok_videos.html", context)
    response.headers["Cache-Control"] = "no-store"
    return response


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
