"""Sources: where new videos come from (official API, unofficial yt-dlp, Google Drive)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import FormData

from repeater.activity import log_activity
from repeater.jobs import kinds
from repeater.models import (
    AccountStatus,
    ActivityLevel,
    ConnectedAccount,
    ContentPlan,
    Platform,
    Source,
    SourceKind,
)
from repeater.platforms.base import PlatformError
from repeater.sources.drive import parse_folder_id
from repeater.timeutil import utcnow
from repeater.web.auth import CurrentWorkspace, WorkspaceContext
from repeater.web.deps import DB, Form, FormError, form_bool, form_int, form_str
from repeater.web.templating import flash, render

router = APIRouter()

EDITABLE_KINDS = (SourceKind.PLATFORM_API, SourceKind.YTDLP, SourceKind.DRIVE)
KIND_ACCOUNT_PLATFORMS: dict[SourceKind, tuple[Platform, ...]] = {
    SourceKind.PLATFORM_API: (Platform.INSTAGRAM, Platform.FACEBOOK),
    SourceKind.YTDLP: (Platform.TIKTOK, Platform.YOUTUBE),
    SourceKind.DRIVE: (Platform.GDRIVE,),
}
YTDLP_PLATFORMS = (Platform.TIKTOK, Platform.YOUTUBE)
# Mirrors repeater.sources.importer (not imported here, to keep yt-dlp out of the web process).
YTDLP_MIN_POLL_MINUTES = 30
MAX_BACKFILL = 200
MIN_POLL_MINUTES = 5
MAX_POLL_MINUTES = 24 * 60
DEFAULT_POLL_MINUTES = {SourceKind.PLATFORM_API: 15, SourceKind.YTDLP: 60, SourceKind.DRIVE: 10}
PROFILE_HOSTS = {
    Platform.TIKTOK: frozenset({"tiktok.com", "www.tiktok.com", "m.tiktok.com"}),
    Platform.YOUTUBE: frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"}),
}
YOUTUBE_PROFILE_PREFIXES = ("channel", "c", "user")
# Config keys that describe *what* is watched. Changing one restarts detection from scratch so
# a new profile/folder is not mistaken for a flood of "new" videos.
IDENTITY_KEYS = ("profile_url", "folder_id")
DETECTION_STATE_KEYS = ("baseline_at", "seen_ids")


def _kind(value: str | None) -> SourceKind:
    try:
        kind = SourceKind(value or "")
    except ValueError:
        raise HTTPException(status_code=404) from None
    if kind not in EDITABLE_KINDS:
        raise HTTPException(status_code=404)
    return kind


def _accounts_for(db: Session, workspace_id: int, kind: SourceKind) -> list[ConnectedAccount]:
    return list(
        db.execute(
            select(ConnectedAccount)
            .where(
                ConnectedAccount.workspace_id == workspace_id,
                ConnectedAccount.platform.in_(KIND_ACCOUNT_PLATFORMS[kind]),
                ConnectedAccount.status != AccountStatus.DISABLED,
            )
            .order_by(ConnectedAccount.platform, ConnectedAccount.id)
        ).scalars()
    )


def _get_source(db: Session, ctx: WorkspaceContext, source_id: int) -> Source:
    source = db.get(Source, source_id)
    if (
        source is None
        or source.workspace_id != ctx.workspace.id
        or source.kind not in EDITABLE_KINDS
        or source.profile_id is not None
    ):
        raise HTTPException(status_code=404)
    return source


# --------------------------------------------------------------------------- pages


@router.get("/sources")
def sources_page(request: Request, db: DB, ctx: CurrentWorkspace) -> Response:
    sources = list(
        db.execute(
            select(Source)
            .options(selectinload(Source.connected_account))
            .where(Source.workspace_id == ctx.workspace.id)
            .order_by(Source.kind == SourceKind.MANUAL, Source.id)
        ).scalars()
    )
    return render(
        request,
        "sources.html",
        {
            "nav": "sources",
            "sources": sources,
            "kinds": [k.value for k in EDITABLE_KINDS],
            "source_plans": dict(
                db.execute(
                    select(ContentPlan.source_id, ContentPlan.id).where(
                        ContentPlan.workspace_id == ctx.workspace.id
                    )
                ).all()
            ),
        },
    )


@router.get("/sources/new")
def new_source(request: Request, db: DB, ctx: CurrentWorkspace, kind: str = "platform_api") -> Response:
    source_kind = _kind(kind)
    draft = Source(
        workspace_id=ctx.workspace.id,
        kind=source_kind,
        name="",
        config={"backfill_count": 0},
        poll_interval_minutes=DEFAULT_POLL_MINUTES[source_kind],
        enabled=True,
    )
    return _form_page(request, db, ctx, draft, is_new=True)


@router.get("/sources/{source_id}/edit")
def edit_source(request: Request, db: DB, ctx: CurrentWorkspace, source_id: int) -> Response:
    plan_id = db.scalar(
        select(ContentPlan.id).where(
            ContentPlan.source_id == source_id, ContentPlan.workspace_id == ctx.workspace.id
        )
    )
    if plan_id:
        return RedirectResponse(f"/plans/{plan_id}/edit#source", 303)
    return _form_page(request, db, ctx, _get_source(db, ctx, source_id), is_new=False)


def _form_page(
    request: Request,
    db: Session,
    ctx: WorkspaceContext,
    source: Source,
    *,
    is_new: bool,
    error: FormError | None = None,
    values: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    config = dict(source.config or {})
    current = {
        "name": source.name or "",
        "account_id": source.connected_account_id,
        "platform": source.platform.value if source.platform else "tiktok",
        "profile_url": config.get("profile_url", ""),
        "folder": config.get("folder_id", ""),
        "poll_interval_minutes": source.poll_interval_minutes,
        "backfill_count": config.get("backfill_count", 0),
        "enabled": source.enabled,
        "ack": source.unofficial_ack_at is not None,
    }
    current.update(values or {})
    context = {
        "nav": "sources",
        "source": source,
        "kind": source.kind.value,
        "is_new": is_new,
        "accounts": []
        if source.kind == SourceKind.YTDLP
        else _accounts_for(db, ctx.workspace.id, source.kind),
        "values": current,
        "error": error,
        "ytdlp_platforms": [p.value for p in YTDLP_PLATFORMS],
        "min_poll": YTDLP_MIN_POLL_MINUTES if source.kind == SourceKind.YTDLP else MIN_POLL_MINUTES,
        "max_poll": MAX_POLL_MINUTES,
        "max_backfill": MAX_BACKFILL,
    }
    return render(request, "source_form.html", context, status_code=status_code)


# --------------------------------------------------------------------------- saving


@dataclass
class SourceInput:
    name: str
    account: ConnectedAccount | None
    platform: Platform | None
    config: dict[str, Any]
    poll_interval_minutes: int
    enabled: bool
    ack: bool


def _parse(db: Session, ctx: WorkspaceContext, kind: SourceKind, form: FormData) -> SourceInput:
    name = form_str(form, "name", max_len=200)
    min_poll = YTDLP_MIN_POLL_MINUTES if kind == SourceKind.YTDLP else MIN_POLL_MINUTES
    poll = form_int(form, "poll_interval_minutes", minimum=min_poll, maximum=MAX_POLL_MINUTES)
    backfill = form_int(form, "backfill_count", minimum=0, maximum=MAX_BACKFILL, default=0)
    # A yt-dlp source never has a connected account (contract C6): its detection and download use
    # yt-dlp only, never the OAuth tokens or the official API of an account. Any `account_id` in the
    # form is ignored, which also unlinks sources created before this rule.
    account = None if kind == SourceKind.YTDLP else _parse_account(db, ctx, kind, form)
    config: dict[str, Any] = {"backfill_count": backfill}
    platform: Platform | None
    if kind == SourceKind.PLATFORM_API:
        if account is None:
            raise FormError("sources.error_account_required")
        platform = Platform(account.platform)
    elif kind == SourceKind.YTDLP:
        platform = _parse_ytdlp_platform(form)
        config["profile_url"] = normalize_profile_url(
            form_str(form, "profile_url", max_len=500, required=True), platform
        )
    else:
        if account is None:
            raise FormError("sources.error_account_required")
        platform = Platform.GDRIVE
        try:
            config["folder_id"] = parse_folder_id(form_str(form, "folder", max_len=500, required=True))
        except PlatformError:
            raise FormError("sources.error_folder") from None
    return SourceInput(
        name=name,
        account=account,
        platform=platform,
        config=config,
        poll_interval_minutes=poll,
        enabled=form_bool(form, "enabled"),
        ack=form_bool(form, "ack"),
    )


def _parse_account(
    db: Session, ctx: WorkspaceContext, kind: SourceKind, form: FormData
) -> ConnectedAccount | None:
    raw = form.get("account_id")
    if not isinstance(raw, str) or not raw.strip():
        return None
    if not raw.isdigit():
        raise FormError("sources.error_account_required")
    account = db.get(ConnectedAccount, int(raw))
    if (
        account is None
        or account.workspace_id != ctx.workspace.id
        or Platform(account.platform) not in KIND_ACCOUNT_PLATFORMS[kind]
        or account.status == AccountStatus.DISABLED
    ):
        raise FormError("sources.error_account_required")
    return account


def _parse_ytdlp_platform(form: FormData) -> Platform:
    raw = form.get("platform")
    try:
        platform = Platform(raw if isinstance(raw, str) else "")
    except ValueError:
        raise FormError("sources.error_platform") from None
    if platform not in YTDLP_PLATFORMS:
        raise FormError("sources.error_platform")
    return platform


def normalize_profile_url(raw: str, platform: Platform) -> str:
    """Accept https://www.tiktok.com/@name or https://www.youtube.com/@name (also /channel/ID,
    /c/name, /user/name). Returns a clean https URL without query or fragment."""
    url = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in PROFILE_HOSTS[platform]:
        raise FormError("sources.error_profile_url")
    parts = [p for p in parsed.path.split("/") if p]
    valid = bool(parts) and (
        (parts[0].startswith("@") and len(parts[0]) > 1)
        or (platform == Platform.YOUTUBE and len(parts) >= 2 and parts[0] in YOUTUBE_PROFILE_PREFIXES)
    )
    if not valid:
        raise FormError("sources.error_profile_url")
    keep = parts[:1] if parts[0].startswith("@") else parts[:2]
    return f"https://{host}/{'/'.join(keep)}"


def _apply(source: Source, data: SourceInput, *, is_new: bool) -> bool:
    """Copy the input onto the source. Returns True if detection was reset."""
    old = dict(source.config or {})
    new = {**old, **data.config}
    reset = not is_new and (
        any(old.get(k) != new.get(k) for k in IDENTITY_KEYS)
        or source.connected_account_id != (data.account.id if data.account else None)
    )
    if reset:
        for key in DETECTION_STATE_KEYS:
            new.pop(key, None)
    source.name = data.name or _default_name(source.kind, data)
    source.connected_account_id = data.account.id if data.account else None
    source.platform = data.platform
    source.config = new
    source.poll_interval_minutes = data.poll_interval_minutes
    source.enabled = data.enabled
    if source.enabled or reset:
        source.last_error = None
        source.consecutive_failures = 0
    return reset


def _default_name(kind: SourceKind, data: SourceInput) -> str:
    if data.account is not None:
        return data.account.display_name[:200]
    return str(data.config.get("profile_url") or kind.value)[:200]


def _save(
    request: Request, db: Session, ctx: WorkspaceContext, source: Source, form: FormData, *, is_new: bool
) -> Response:
    try:
        data = _parse(db, ctx, source.kind, form)
        if source.kind == SourceKind.YTDLP and source.unofficial_ack_at is None and not data.ack:
            raise FormError("sources.error_ack_required")
    except FormError as exc:
        values = {
            k: form.get(k)
            for k in ("name", "platform", "profile_url", "folder", "poll_interval_minutes", "backfill_count")
        }
        values.update(
            enabled=form_bool(form, "enabled"),
            ack=form_bool(form, "ack"),
            account_id=_int_or_none(form.get("account_id")),
        )
        return _form_page(request, db, ctx, source, is_new=is_new, error=exc, values=values, status_code=400)
    if source.kind == SourceKind.YTDLP and source.unofficial_ack_at is None:
        source.unofficial_ack_at = utcnow()
    if is_new:
        db.add(source)
    reset = _apply(source, data, is_new=is_new)
    db.flush()
    _log_change(db, ctx, source, is_new=is_new, reset=reset)
    if source.enabled and (is_new or reset):
        kinds.enqueue_poll_source(db, source.id)
    kinds.enqueue_plan_workspace(db, ctx.workspace.id)
    db.commit()
    flash(request, "success", "sources.saved", name=source.name)
    return RedirectResponse("/sources", status_code=303)


def _int_or_none(value: object) -> int | None:
    return int(value) if isinstance(value, str) and value.isdigit() else None


def _log_change(db: Session, ctx: WorkspaceContext, source: Source, *, is_new: bool, reset: bool) -> None:
    verb = "Added" if is_new else "Updated"
    extra = " Detection restarts from the current videos." if reset else ""
    message = (
        f"{verb} source '{source.name}' ({source.kind.value}, {'on' if source.enabled else 'off'}).{extra}"
    )
    if source.kind == SourceKind.YTDLP and is_new:
        message += " The user confirmed the unofficial import warning."
    log_activity(
        db,
        ctx.workspace.id,
        ActivityLevel.INFO,
        "source",
        message,
        actor=ctx.actor,
        source_id=source.id,
        msg_key=(
            "activity.source_added"
            if is_new
            else "activity.source_updated_reset"
            if reset
            else "activity.source_updated"
        ),
        msg_params={
            "name": source.name,
            "kind": source.kind.value,
            "state": "on" if source.enabled else "off",
        },
    )


@router.post("/sources/new")
def create_source(
    request: Request, db: DB, ctx: CurrentWorkspace, form: Form, kind: str = "platform_api"
) -> Response:
    raw_kind = form.get("kind")
    source_kind = _kind(raw_kind if isinstance(raw_kind, str) else kind)
    source = Source(workspace_id=ctx.workspace.id, kind=source_kind, config={}, enabled=True)
    return _save(request, db, ctx, source, form, is_new=True)


@router.post("/sources/{source_id}/edit")
def update_source(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, source_id: int) -> Response:
    return _save(request, db, ctx, _get_source(db, ctx, source_id), form, is_new=False)


@router.post("/sources/{source_id}/poll")
def poll_now(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, source_id: int) -> Response:
    source = _get_source(db, ctx, source_id)
    if not source.enabled:
        flash(request, "error", "sources.poll_disabled", name=source.name)
        return RedirectResponse("/sources", status_code=303)
    queued = kinds.enqueue_poll_source(db, source.id)
    db.commit()
    flash(
        request,
        "success" if queued else "info",
        "sources.poll_queued" if queued else "sources.poll_already",
        name=source.name,
    )
    return RedirectResponse("/sources", status_code=303)


@router.post("/sources/{source_id}/toggle")
def toggle(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, source_id: int) -> Response:
    source = _get_source(db, ctx, source_id)
    if not source.enabled and source.kind == SourceKind.YTDLP and source.unofficial_ack_at is None:
        flash(request, "error", "sources.error_ack_required")
        return RedirectResponse(f"/sources/{source.id}/edit", status_code=303)
    source.enabled = not source.enabled
    if source.enabled:
        source.last_error = None
        source.consecutive_failures = 0
        kinds.enqueue_poll_source(db, source.id)
    log_activity(
        db,
        ctx.workspace.id,
        ActivityLevel.INFO,
        "source",
        f"{'Turned on' if source.enabled else 'Turned off'} source '{source.name}'.",
        actor=ctx.actor,
        source_id=source.id,
        msg_key="activity.source_on" if source.enabled else "activity.source_off",
        msg_params={"name": source.name},
    )
    kinds.enqueue_plan_workspace(db, ctx.workspace.id)
    db.commit()
    flash(request, "success", "sources.enabled" if source.enabled else "sources.disabled", name=source.name)
    return RedirectResponse("/sources", status_code=303)
