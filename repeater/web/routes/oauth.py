"""OAuth connect flow: /connect/{purpose} -> provider consent -> /oauth/{provider}/callback."""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from repeater.accounts import upsert_connected_accounts
from repeater.config import get_settings
from repeater.engine import rules as engine_rules
from repeater.jobs import kinds
from repeater.models import ConnectedAccount, ContentPlan, OAuthState, Platform, RotationRule
from repeater.platforms import get_oauth_provider
from repeater.platforms.base import ConnectedAccountData, PlatformError
from repeater.timeutil import ensure_aware, utcnow
from repeater.web import i18n
from repeater.web.auth import CurrentWorkspace, WorkspaceContext
from repeater.web.deps import DB
from repeater.web.templating import flash

log = logging.getLogger("repeater.web.oauth")
router = APIRouter()

# Button the user clicked -> OAuth provider that serves it.
PURPOSE_PROVIDERS = {
    "tiktok": "tiktok",
    "youtube": "google",
    "gdrive": "google",
    "meta": "meta",
    "instagram": "instagram",
}
# Purpose used to (re)connect an account of each platform.
PLATFORM_PURPOSES = {
    Platform.TIKTOK: "tiktok",
    Platform.YOUTUBE: "youtube",
    Platform.GDRIVE: "gdrive",
    Platform.INSTAGRAM: "meta",
    Platform.FACEBOOK: "meta",
}
STATE_TTL = timedelta(minutes=15)
CANCEL_ERRORS = frozenset({"access_denied", "user_denied", "user_cancelled", "consent_required"})
MAX_PROVIDER_TEXT = 300


def pkce_pair() -> tuple[str, str]:
    """RFC 7636: a 64-character verifier from the unreserved alphabet and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=")
    return verifier, challenge.decode("ascii")


def _purge_expired_states(db: Session) -> None:
    db.execute(delete(OAuthState).where(OAuthState.created_at < utcnow() - STATE_TTL))


@router.get("/connect/{purpose}")
def connect(request: Request, db: DB, ctx: CurrentWorkspace, purpose: str, return_to: str = "") -> Response:
    return_path = _plan_return(db, ctx, return_to)
    provider_name = PURPOSE_PROVIDERS.get(purpose)
    if provider_name is None:
        raise HTTPException(status_code=404)
    settings = get_settings()
    if not settings.platform_configured(provider_name):
        flash(request, "error", "oauth.not_configured", platform=_purpose_label(purpose))
        return RedirectResponse(return_path or "/accounts", status_code=303)
    provider = get_oauth_provider(provider_name)
    verifier, challenge = pkce_pair() if provider.uses_pkce else (None, None)
    state = secrets.token_urlsafe(32)
    try:
        url = provider.authorize_url(
            state=state,
            redirect_uri=settings.oauth_redirect_uri(provider_name),
            code_challenge=challenge,
            purpose=purpose,
        )
    except (PlatformError, ValueError) as exc:
        log.warning("cannot build %s authorize URL: %s", provider_name, type(exc).__name__)
        flash(request, "error", "oauth.start_failed", platform=_purpose_label(purpose))
        return RedirectResponse(return_path or "/accounts", status_code=303)
    _purge_expired_states(db)
    db.add(
        OAuthState(
            state=state,
            workspace_id=ctx.workspace.id,
            user_id=ctx.user.id,
            provider=provider_name,
            purpose=purpose,
            code_verifier=verifier,
            return_path=return_path,
        )
    )
    db.commit()
    return RedirectResponse(url, status_code=303)


@router.get("/oauth/{provider}/callback")
def callback(request: Request, db: DB, ctx: CurrentWorkspace, provider: str) -> Response:
    params = request.query_params
    record = _consume_state(db, ctx, provider, params.get("state"))
    if isinstance(record, str):
        flash(request, "error", record)
        return RedirectResponse("/accounts", status_code=303)
    purpose = record.purpose
    return_path = _plan_return(db, ctx, record.return_path or "")
    error = params.get("error")
    if error:
        _flash_provider_error(request, purpose, error, params.get("error_description"))
        return RedirectResponse(return_path or "/accounts", status_code=303)
    code = params.get("code")
    if not code:
        flash(request, "error", "oauth.no_code", platform=_purpose_label(purpose))
        return RedirectResponse(return_path or "/accounts", status_code=303)
    try:
        items = get_oauth_provider(provider).exchange_code(
            code=code,
            redirect_uri=get_settings().oauth_redirect_uri(provider),
            code_verifier=record.code_verifier,
            purpose=purpose,
        )
    except PlatformError as exc:
        code_key = f"oauth.code.{exc.code}" if exc.code else ""
        if code_key and i18n.has_key(code_key):
            # A known failure: a translated explanation, with the platform's own text as detail.
            flash(request, "error", code_key, text=exc.message, platform=_purpose_label(purpose))
        else:
            flash(
                request, "error", "oauth.exchange_failed", text=exc.message, platform=_purpose_label(purpose)
            )
        return RedirectResponse(return_path or "/accounts", status_code=303)
    except Exception as exc:  # noqa: BLE001 - never show internals; tokens are never in our messages
        log.error("OAuth exchange with %s failed: %s", provider, type(exc).__name__)
        flash(request, "error", "oauth.unexpected", platform=_purpose_label(purpose))
        return RedirectResponse(return_path or "/accounts", status_code=303)
    return _save_accounts(request, db, ctx, items, purpose, return_path=return_path)


def _consume_state(db: Session, ctx: WorkspaceContext, provider: str, state: str | None) -> OAuthState | str:
    """Validate and delete (single use) the state. Returns the record or an i18n error key."""
    if not state or len(state) > 128:
        return "oauth.state_invalid"
    record = db.execute(
        select(OAuthState).where(OAuthState.state == state).with_for_update()
    ).scalar_one_or_none()
    if record is None or record.user_id != ctx.user.id or record.workspace_id != ctx.workspace.id:
        db.rollback()
        return "oauth.state_invalid"
    db.delete(record)
    db.commit()
    if ensure_aware(record.created_at) < utcnow() - STATE_TTL:
        return "oauth.state_expired"
    if record.provider != provider:
        return "oauth.state_invalid"
    return record


def _flash_provider_error(request: Request, purpose: str, error: str, description: str | None) -> None:
    if error in CANCEL_ERRORS:
        flash(request, "warning", "oauth.cancelled", platform=_purpose_label(purpose))
        return
    detail = (description or error)[:MAX_PROVIDER_TEXT]
    flash(request, "error", "oauth.provider_error", text=detail, platform=_purpose_label(purpose))


def _save_accounts(
    request: Request,
    db: Session,
    ctx: WorkspaceContext,
    items: list[ConnectedAccountData],
    purpose: str,
    *,
    return_path: str | None = None,
) -> Response:
    existing = {
        (p, e)
        for p, e in db.execute(
            select(ConnectedAccount.platform, ConnectedAccount.external_id).where(
                ConnectedAccount.workspace_id == ctx.workspace.id
            )
        ).all()
    }
    try:
        accounts = upsert_connected_accounts(db, ctx.workspace.id, items, actor=ctx.actor)
    except PlatformError:
        db.rollback()
        flash(request, "error", "oauth.login_method_conflict")
        return RedirectResponse(return_path or "/accounts", status_code=303)
    # New rules start OFF (engine.rules.ensure_rule): nothing is published before the user has
    # reviewed the account's settings and turned publishing on.
    needs_setup: list[ConnectedAccount] = []
    for account in accounts:
        rule = engine_rules.ensure_rule(db, account)
        is_new = (account.platform, account.external_id) not in existing
        if rule is not None and _needs_setup(rule, is_new):
            needs_setup.append(account)
    if not return_path:
        kinds.enqueue_plan_workspace(db, ctx.workspace.id)
    db.commit()
    names = ", ".join(a.display_name or a.external_id for a in accounts)
    flash(
        request,
        "success",
        "oauth.connected",
        count=len(accounts),
        names=names,
        platform=_purpose_label(purpose),
    )
    if return_path:
        return RedirectResponse(return_path, status_code=303)
    if not needs_setup:
        return RedirectResponse("/accounts", status_code=303)
    if purpose == "tiktok":
        flash(request, "info", "oauth.tiktok_setup")
    else:
        flash(
            request,
            "info",
            "oauth.publishing_off",
            count=len(needs_setup),
            names=", ".join(a.display_name or a.external_id for a in needs_setup),
        )
    # One new account: straight to its settings. Several (a Meta login returns every Page and its
    # Instagram account): the rules list, where each one is reviewed and turned on separately.
    target = f"/rules/{needs_setup[0].id}" if len(needs_setup) == 1 else "/rules"
    return RedirectResponse(target, status_code=303)


def _needs_setup(rule: RotationRule, is_new: bool) -> bool:
    """A new account always needs its settings reviewed; a reconnected one only when its rule is on
    but cannot publish yet."""
    return is_new or (bool(rule.enabled) and bool(engine_rules.rule_problems(rule)))


def _purpose_label(purpose: str) -> str:
    return {
        "tiktok": "TikTok",
        "youtube": "YouTube",
        "gdrive": "Google Drive",
        "meta": "Facebook / Instagram",
        "instagram": "Instagram",
    }.get(purpose, purpose)


def _plan_return(db: Session, ctx: WorkspaceContext, value: str) -> str | None:
    match = re.fullmatch(r"/plans/([1-9][0-9]{0,9})/(?:edit|setup/(?:accounts|content))", value)
    if match and db.scalar(
        select(ContentPlan.id).where(
            ContentPlan.id == int(match[1]), ContentPlan.workspace_id == ctx.workspace.id
        )
    ):
        return value
    return None
