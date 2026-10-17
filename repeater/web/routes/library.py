"""Library: video grid, detail page, authenticated thumbnails/previews, per-video actions and the
streaming manual upload."""

from __future__ import annotations

import importlib
import logging
import mimetypes
import secrets
from pathlib import Path
from typing import Any, BinaryIO

import anyio.to_thread
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool

from repeater.activity import log_activity
from repeater.config import get_settings
from repeater.engine import actions, captions
from repeater.engine import rules as engine_rules
from repeater.jobs import kinds
from repeater.media import content, storage
from repeater.messages import Message
from repeater.models import (
    PUBLISH_PLATFORMS,
    AccountStatus,
    ActivityLevel,
    ActivityLog,
    ConnectedAccount,
    ContentPlan,
    CreatorProfile,
    Platform,
    PostStatus,
    RotationRule,
    ScheduledPost,
    Video,
    VideoStatus,
)
from repeater.sources import manual
from repeater.web import csrf, messages
from repeater.web.auth import CurrentWorkspace, WorkspaceContext, require_owner
from repeater.web.deps import (
    DB,
    Form,
    FormError,
    Page,
    form_bool,
    form_str,
    form_text,
    query_int,
    safe_next,
)
from repeater.web.journey import remember_person
from repeater.web.templating import flash, render

log = logging.getLogger("repeater.web.library")
router = APIRouter()

PAGE_SIZE = 24
STATUS_FILTERS: dict[str, tuple[VideoStatus, ...]] = {
    "active": tuple(s for s in VideoStatus if s not in (VideoStatus.ARCHIVED, VideoStatus.DUPLICATE)),
    "ready": (VideoStatus.READY,),
    "processing": (VideoStatus.IMPORTING, VideoStatus.PROCESSING),
    "failed": (VideoStatus.FAILED,),
    "duplicate": (VideoStatus.DUPLICATE,),
    "archived": (VideoStatus.ARCHIVED,),
    "all": tuple(VideoStatus),
}
MAX_TITLE_CHARS = 300
MAX_CAPTION_CHARS = 5000


# --------------------------------------------------------------------------- helpers


def get_video(db: Session, ctx: WorkspaceContext, video_id: int) -> Video:
    video = db.get(Video, video_id)
    if video is None or video.workspace_id != ctx.workspace.id:
        raise HTTPException(status_code=404)
    return video


def target_accounts(db: Session, workspace_id: int) -> list[ConnectedAccount]:
    """Active accounts that can be published to."""
    return list(
        db.execute(
            select(ConnectedAccount)
            .where(
                ConnectedAccount.workspace_id == workspace_id,
                ConnectedAccount.platform.in_(PUBLISH_PLATFORMS),
                ConnectedAccount.status == AccountStatus.ACTIVE,
                ConnectedAccount.profile_id.is_(None),
            )
            .order_by(ConnectedAccount.platform, ConnectedAccount.id)
        ).scalars()
    )


def incomplete_accounts(db: Session, accounts: list[ConnectedAccount]) -> dict[int, list[str]]:
    """account id -> readiness problem codes, for the accounts whose posting settings are not
    complete (publish-now to them would only fail)."""
    rules = {
        r.connected_account_id: r
        for r in db.execute(
            select(RotationRule).where(RotationRule.connected_account_id.in_([a.id for a in accounts]))
        ).scalars()
    }
    out: dict[int, list[str]] = {}
    for account in accounts:
        rule = rules.get(account.id)
        problems = (
            engine_rules.rule_problems(rule)
            if rule is not None
            else engine_rules.settings_problems(account.platform, None)
        )
        if problems:
            out[account.id] = problems
    return out


def latest_posts(db: Session, video_ids: list[int]) -> dict[int, dict[int, ScheduledPost]]:
    """video id -> account id -> newest post (highest cycle, then newest row)."""
    out: dict[int, dict[int, ScheduledPost]] = {}
    if not video_ids:
        return out
    posts = db.execute(
        select(ScheduledPost)
        .where(ScheduledPost.video_id.in_(video_ids))
        .order_by(ScheduledPost.cycle, ScheduledPost.id)
    ).scalars()
    for post in posts:
        out.setdefault(post.video_id, {})[post.connected_account_id] = post
    return out


def _back(form: Any, default: str) -> RedirectResponse:
    raw = form.get("next") if form is not None else None
    return RedirectResponse(safe_next(raw if isinstance(raw, str) else None, default), status_code=303)


# --------------------------------------------------------------------------- pages


@router.get("/library")
def library(
    request: Request,
    db: DB,
    ctx: CurrentWorkspace,
    status: str = "active",
    page: str = "1",
    profile_id: int | None = None,
) -> Response:
    if profile_id is None:
        selected = getattr(request.state, "active_person", None)
        profile_id = (
            selected["id"]
            if selected
            else db.scalar(
                select(CreatorProfile.id)
                .join(ContentPlan, ContentPlan.profile_id == CreatorProfile.id)
                .where(CreatorProfile.workspace_id == ctx.workspace.id)
                .order_by(CreatorProfile.id.desc())
                .limit(1)
            )
        )
    return _library_page(request, db, ctx, status, page, profile_id or None)


def _library_page(request, db, ctx, status="active", page="1", profile_id=None, *, draft=None) -> Response:
    statuses = STATUS_FILTERS.get(status) or STATUS_FILTERS["active"]
    status = status if status in STATUS_FILTERS else "active"
    profiles = list(
        db.scalars(
            select(CreatorProfile)
            .where(CreatorProfile.workspace_id == ctx.workspace.id)
            .order_by(CreatorProfile.name)
        )
    )
    profile = next((p for p in profiles if p.id == profile_id), None)
    if profile_id is not None and profile is None:
        raise HTTPException(status_code=404)
    where = (
        Video.workspace_id == ctx.workspace.id,
        Video.status.in_(statuses),
        Video.profile_id.is_not_distinct_from(profile_id),
    )
    total = int(db.execute(select(func.count()).select_from(Video).where(*where)).scalar_one())
    pager = Page(number=query_int(page, default=1), size=PAGE_SIZE, total=total)
    videos = list(
        db.execute(
            select(Video)
            .where(*where)
            .order_by(Video.created_at.desc(), Video.id.desc())
            .offset(pager.offset)
            .limit(PAGE_SIZE)
        ).scalars()
    )
    accounts = [] if profile_id is not None else target_accounts(db, ctx.workspace.id)
    context = {
        "draft": draft or {},
        "plan": db.scalar(
            select(ContentPlan).where(
                ContentPlan.workspace_id == ctx.workspace.id, ContentPlan.profile_id == profile_id
            )
        )
        if profile
        else None,
        "profiles": profiles,
        "profile": profile,
        "profile_id": profile_id,
        "nav": "library",
        "videos": videos,
        "status_filter": status,
        "status_filters": list(STATUS_FILTERS),
        "pager": pager,
        "accounts": accounts,
        "incomplete": incomplete_accounts(db, accounts),
        "posts": latest_posts(db, [v.id for v in videos]),
        "max_upload_mb": get_settings().max_upload_mb,
    }
    if profile and context["plan"]:
        remember_person(request, profile, context["plan"])
    return render(request, "library.html", context, status_code=422 if draft else 200)


@router.get("/library/{video_id}")
def video_detail(request: Request, db: DB, ctx: CurrentWorkspace, video_id: int) -> Response:
    return _video_page(request, db, ctx, video_id)


def _video_page(request, db, ctx, video_id, *, draft=None) -> Response:
    video = get_video(db, ctx, video_id)
    posts = list(
        db.execute(
            select(ScheduledPost)
            .options(selectinload(ScheduledPost.connected_account))
            .where(ScheduledPost.video_id == video.id)
            .order_by(ScheduledPost.scheduled_at.desc(), ScheduledPost.id.desc())
        ).scalars()
    )
    accounts = [] if video.profile_id is not None else target_accounts(db, ctx.workspace.id)
    context = {
        "draft": draft or {},
        "formats": content.FORMATS[video.media_kind],
        "content_caption": content.caption(video),
        "compatibility": [
            {"account": a, "reason": content.problem(video, a)}
            for a in db.scalars(
                select(ConnectedAccount)
                .where(
                    ConnectedAccount.workspace_id == ctx.workspace.id,
                    ConnectedAccount.profile_id == video.profile_id,
                    ConnectedAccount.platform.in_(PUBLISH_PLATFORMS),
                )
                .order_by(ConnectedAccount.platform, ConnectedAccount.display_name)
            )
        ]
        if video.profile_id
        else [],
        "nav": "library",
        "video": video,
        "person_plan": db.scalar(
            select(ContentPlan).where(
                ContentPlan.workspace_id == ctx.workspace.id,
                ContentPlan.profile_id == video.profile_id,
            )
        )
        if video.profile_id
        else None,
        "posts": posts,
        "accounts": [a for a in accounts if a.id != video.source_account_id],
        "incomplete": incomplete_accounts(db, accounts),
        "can_delete": ctx.is_owner,
        "previews": _caption_previews(db, video, accounts),
        "excluded": [p.value for p in PUBLISH_PLATFORMS if not form_bool(draft, f"platform_{p.value}")]
        if draft is not None
        else _excluded_platforms(video),
        "publish_platforms": [p.value for p in PUBLISH_PLATFORMS],
        "has_preview": _preview_file(video) is not None,
        "overrides": draft if draft is not None else video.overrides or {},
    }
    if context["person_plan"]:
        remember_person(request, db.get(CreatorProfile, video.profile_id), context["person_plan"])
    return render(request, "library_detail.html", context, status_code=422 if draft is not None else 200)


def _caption_previews(db: Session, video: Video, accounts: list[ConnectedAccount]) -> list[dict[str, Any]]:
    rules = {
        r.connected_account_id: r
        for r in db.execute(
            select(RotationRule).where(RotationRule.workspace_id == video.workspace_id)
        ).scalars()
    }
    previews = []
    for account in accounts:
        title, caption = captions.render(rules.get(account.id), video, account.platform)
        previews.append({"account": account, "title": title, "caption": caption})
    return previews


def _excluded_platforms(video: Video) -> set[str]:
    raw = (video.overrides or {}).get("exclude_platforms") or []
    return {str(p).lower() for p in raw} if isinstance(raw, list | tuple) else set()


# --------------------------------------------------------------------------- files


def _file(rel: str | None) -> Path | None:
    try:
        path = storage.abs_path(rel)
    except ValueError:
        return None
    return path if path is not None and path.is_file() else None


def _preview_file(video: Video) -> Path | None:
    if video.media_kind == "text":
        return None
    if video.media_kind == "image":
        return _file(video.encoded_path)  # Never serve the untrusted original raster inline.
    return _file(video.encoded_path) or _file(video.original_path)


@router.get("/library/{video_id}/thumb.jpg")
def thumbnail(db: DB, ctx: CurrentWorkspace, video_id: int) -> Response:
    path = _file(get_video(db, ctx, video_id).thumbnail_path)
    if path is None:
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@router.api_route("/library/{video_id}/preview", methods=["GET", "HEAD"])
def preview(db: DB, ctx: CurrentWorkspace, video_id: int) -> Response:
    video = get_video(db, ctx, video_id)
    path = _preview_file(video)
    if path is None:
        raise HTTPException(status_code=404)
    media_type = "video/mp4" if path.suffix == ".mp4" else (mimetypes.guess_type(path.name)[0] or "video/mp4")
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


# --------------------------------------------------------------------------- actions


@router.post("/library/{video_id}/archive")
def archive(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, video_id: int) -> Response:
    video = get_video(db, ctx, video_id)
    return _run_action(
        request, db, form, f"/library/{video.id}", "library.archived", actions.archive_video, video, ctx
    )


@router.post("/library/{video_id}/restore")
def restore(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, video_id: int) -> Response:
    video = get_video(db, ctx, video_id)
    return _run_action(
        request, db, form, f"/library/{video.id}", "library.restored", actions.restore_video, video, ctx
    )


@router.post("/library/{video_id}/retry")
def retry(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, video_id: int) -> Response:
    video = get_video(db, ctx, video_id)
    return _run_action(
        request, db, form, f"/library/{video.id}", "library.retried", _retry_failed_video, video, ctx
    )


def _retry_failed_video(db: Session, video: Video, *, actor: str) -> None:
    # Imported on use: the importer loads yt-dlp, which the web process otherwise never needs.
    importlib.import_module("repeater.sources.importer").retry_failed_video(db, video, actor=actor)


def _run_action(
    request: Request,
    db: Session,
    form: Any,
    default_back: str,
    success_key: str,
    action: Any,
    video: Video,
    ctx: WorkspaceContext,
) -> Response:
    try:
        action(db, video, actor=ctx.actor)
        db.commit()
    except ValueError as exc:
        db.rollback()
        messages.flash_error(request, exc)
    else:
        flash(request, "success", success_key)
    return _back(form, default_back)


def _may_exist_on_platform(post: ScheduledPost | None) -> str | None:
    """Why a new upload to this account could create a second copy: 'published' when the latest
    post is live, 'uncertain' when it needs attention but may be live (it kept a platform id or an
    upload state)."""
    if post is None:
        return None
    if post.status == PostStatus.PUBLISHED:
        return "published"
    if actions.may_be_live(post):
        return "uncertain"
    return None


@router.post("/library/{video_id}/publish-now")
def publish_now(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, video_id: int) -> Response:
    video = get_video(db, ctx, video_id)
    account_id = form.get("account_id")
    account = (
        db.get(ConnectedAccount, int(account_id))
        if isinstance(account_id, str) and account_id.isdigit()
        else None
    )
    if account is None or account.workspace_id != ctx.workspace.id:
        flash(request, "error", "library.choose_account")
        return _back(form, f"/library/{video.id}")
    if engine_rules.account_problems(db, account):
        # The adapter would refuse the post (no privacy level, no audience answer ...).
        flash(request, "error", "library.account_incomplete", account=account.display_name)
        return _back(form, f"/library/{video.id}")
    reason = _may_exist_on_platform(latest_posts(db, [video.id]).get(video.id, {}).get(account.id))
    if reason is not None and not form_bool(form, "confirm_republish"):
        back = form.get("next")
        context = {
            "nav": "library",
            "video": video,
            "account": account,
            "reason": reason,
            "next": safe_next(back if isinstance(back, str) else None, f"/library/{video.id}"),
        }
        return render(request, "library_republish.html", context)
    try:
        actions.schedule_video_now(db, video, account, actor=ctx.actor)
        db.commit()
    except ValueError as exc:
        db.rollback()
        messages.flash_error(request, exc)
    else:
        flash(request, "success", "library.publishing_now", account=account.display_name)
    return _back(form, f"/library/{video.id}")


@router.post("/library/{video_id}/delete")
def delete_video(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, video_id: int) -> Response:
    """Deletes a video, its files, its post history and the activity entries that name it. Posts
    that are already on a platform stay there. Owner only; a video that is being prepared or
    published cannot be deleted until that is over."""
    require_owner(ctx)
    video = get_video(db, ctx, video_id)
    in_flight = db.execute(
        select(func.count())
        .select_from(ScheduledPost)
        .where(
            ScheduledPost.video_id == video.id,
            ScheduledPost.status.in_((PostStatus.UPLOADING, PostStatus.PROCESSING)),
        )
    ).scalar_one()
    if video.status in (VideoStatus.IMPORTING, VideoStatus.PROCESSING) or in_flight:
        flash(request, "error", "library.delete_busy")
        return RedirectResponse(f"/library/{video.id}", status_code=303)
    workspace_id, vid = video.workspace_id, video.id
    library_url = f"/library?profile_id={video.profile_id}" if video.profile_id else "/library"
    db.execute(
        delete(ActivityLog).where(
            ActivityLog.workspace_id == workspace_id, ActivityLog.data["video_id"].astext == str(vid)
        )
    )
    db.delete(video)
    log_activity(
        db,
        workspace_id,
        ActivityLevel.INFO,
        "publish",
        "Deleted a video from the library.",
        actor=ctx.actor,
        msg_key="activity.video_deleted",
    )
    kinds.enqueue_plan_workspace(db, workspace_id)
    db.commit()
    storage.delete_video_dir(workspace_id, vid)
    flash(request, "success", "library.deleted")
    return RedirectResponse(library_url, status_code=303)


@router.post("/library/{video_id}/edit")
def edit_video(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, video_id: int) -> Response:
    """Per-video title/caption overrides and the platforms it may be published to (YouTube
    Required Minimum Functionality: users choose title, description and platforms)."""
    video = get_video(db, ctx, video_id)
    try:
        title = form_str(form, "title", max_len=MAX_TITLE_CHARS)
        caption = form_text(form, "caption", max_len=MAX_CAPTION_CHARS)
        placement = form.get("content_format", video.content_format)
        if placement not in content.FORMATS[video.media_kind] or (
            video.profile_id is None and placement != "reel"
        ):
            raise FormError("media.invalid_format")
    except FormError as exc:
        flash(request, "error", exc.key, **exc.params)
        draft = _content_draft(form)
        draft["content_format"] = str(form.get("content_format", video.content_format))[:32]
        for p in PUBLISH_PLATFORMS:
            draft[f"platform_{p.value}"] = "1" if form_bool(form, f"platform_{p.value}") else ""
        return _video_page(request, db, ctx, video.id, draft=draft)
    excluded = sorted(p.value for p in PUBLISH_PLATFORMS if not form_bool(form, f"platform_{p.value}"))
    overrides = {
        k: v for k, v in (video.overrides or {}).items() if k not in ("title", "caption", "exclude_platforms")
    }
    if title:
        overrides["title"] = title
    if caption:
        overrides["caption"] = caption
    if excluded:
        overrides["exclude_platforms"] = excluded
    video.overrides = overrides
    video.content_format = placement
    canceled = _cancel_excluded_posts(db, video, excluded)
    log_activity(
        db,
        video.workspace_id,
        ActivityLevel.INFO,
        "publish",
        f"Updated the publishing details of '{captions.video_label(video)}'"
        + (f"; canceled {canceled} scheduled post(s) on excluded platforms." if canceled else "."),
        actor=ctx.actor,
        video_id=video.id,
        msg_key="activity.video_updated_canceled" if canceled else "activity.video_updated",
        msg_params={"title": captions.video_label(video), "canceled": canceled},
    )
    kinds.enqueue_plan_workspace(db, ctx.workspace.id)
    db.commit()
    flash(request, "success", "library.saved")
    return RedirectResponse(f"/library/{video.id}", status_code=303)


def _cancel_excluded_posts(db: Session, video: Video, excluded: list[str]) -> int:
    if not excluded:
        return 0
    result = db.execute(
        update(ScheduledPost)
        .where(
            ScheduledPost.video_id == video.id,
            ScheduledPost.platform.in_([Platform(p) for p in excluded]),
            ScheduledPost.status == PostStatus.SCHEDULED,
        )
        .values(
            status=PostStatus.CANCELED,
            error="platform excluded for this video",
            error_info=Message("publish.excluded", "platform excluded for this video").info,
        )
    )
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- streaming upload

UPLOAD_PATH = "/library/upload"
FILE_FIELD = "file"
TEXT_FIELD_LIMITS = {
    csrf.FORM_FIELD: 256,
    "title": MAX_TITLE_CHARS * 4,
    "caption": MAX_CAPTION_CHARS * 4,
    "profile_id": 12,
    "format": 32,
    "setup_plan_id": 12,
}
MAX_PARTS = 16
MULTIPART_OVERHEAD = 1024 * 1024


def upload_limit_bytes() -> int:
    return get_settings().max_upload_mb * 1024 * 1024


class UploadRejected(Exception):  # noqa: N818 - user-facing outcome, not a bug
    def __init__(self, key: str, **params: object) -> None:
        super().__init__(key)
        self.key = key
        self.params = params


class UploadSink:
    """Receives python-multipart callbacks: small text fields are kept in memory, the single
    video file goes straight to storage.uploads_dir(). The CSRF token (a form field placed before
    the file input) is checked before the first file byte is written."""

    def __init__(self, session: Any, *, csrf_required: bool, max_file_bytes: int) -> None:
        self.session = session
        self.csrf_required = csrf_required
        self.max_file_bytes = max_file_bytes
        self.fields: dict[str, str] = {}
        self.filename: str | None = None
        self.path: Path | None = None
        self.size = 0
        self._fh: BinaryIO | None = None
        self._pending: list[bytes] = []
        self._parts = 0
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._disposition = b""
        self._name: str | None = None
        self._is_file = False
        self._buf = bytearray()

    def callbacks(self) -> dict[str, Any]:
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": lambda d, s, e: self._header_name.extend(d[s:e]),
            "on_header_value": lambda d, s, e: self._header_value.extend(d[s:e]),
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
        }

    def _part_begin(self) -> None:
        self._parts += 1
        if self._parts > MAX_PARTS:
            raise UploadRejected("library.upload_bad_request")
        self._disposition, self._name, self._is_file = b"", None, False
        self._buf = bytearray()

    def _header_end(self) -> None:
        if bytes(self._header_name).lower() == b"content-disposition":
            self._disposition = bytes(self._header_value)
        self._header_name, self._header_value = bytearray(), bytearray()

    def _headers_finished(self) -> None:
        _, options = parse_options_header(self._disposition)
        self._name = options.get(b"name", b"").decode("utf-8", "replace")
        if b"filename" not in options:
            return
        if self._name != FILE_FIELD or self._fh is not None:
            raise UploadRejected("library.upload_bad_request")
        if self.csrf_required and not csrf.token_matches(self.session, self.fields.get(csrf.FORM_FIELD)):
            raise csrf.CSRFError()
        self._is_file = True
        if self.fields.get("format", "").startswith("image_"):
            from repeater.media.images import MAX_IMAGE_BYTES

            self.max_file_bytes = min(self.max_file_bytes, MAX_IMAGE_BYTES)
        self.filename = Path(options[b"filename"].decode("utf-8", "replace").replace("\\", "/")).name[:255]
        self.path = storage.uploads_dir() / f"{secrets.token_hex(16)}.part"
        self._fh = open(self.path, "wb")  # noqa: SIM115 - closed in finish()/discard()

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if self._is_file:
            self.size += len(chunk)
            if self.size > self.max_file_bytes:
                raise UploadRejected("library.upload_too_large", max_mb=self.max_file_bytes // (1024 * 1024))
            self._pending.append(chunk)
            return
        limit = TEXT_FIELD_LIMITS.get(self._name or "")
        if limit is None:
            return
        if len(self._buf) + len(chunk) > limit:
            raise UploadRejected("library.upload_bad_request")
        self._buf.extend(chunk)

    def _part_end(self) -> None:
        if not self._is_file and self._name in TEXT_FIELD_LIMITS:
            self.fields[self._name] = self._buf.decode("utf-8", "replace")
        self._is_file = False

    def flush(self) -> None:
        if self._fh is not None and self._pending:
            data, self._pending = b"".join(self._pending), []
            self._fh.write(data)

    def finish(self) -> None:
        self.flush()
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def discard(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if self.path is not None:
            self.path.unlink(missing_ok=True)


async def _receive_upload(request: Request, sink: UploadSink, boundary: bytes) -> None:
    parser = MultipartParser(boundary, sink.callbacks())
    async for chunk in request.stream():
        if chunk:
            parser.write(chunk)
            await anyio.to_thread.run_sync(sink.flush)
    parser.finalize()
    await anyio.to_thread.run_sync(sink.finish)


@router.post(UPLOAD_PATH)
async def upload(request: Request, db: DB, ctx: CurrentWorkspace) -> Response:
    content_type, options = parse_options_header(request.headers.get("content-type"))
    boundary = options.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        flash(request, "error", "library.upload_bad_request")
        return RedirectResponse("/library", status_code=303)
    limit = upload_limit_bytes()
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit + MULTIPART_OVERHEAD:
        flash(request, "error", "library.upload_too_large", max_mb=limit // (1024 * 1024))
        return RedirectResponse("/library", status_code=303)
    sink = UploadSink(
        request.session,
        csrf_required=bool(getattr(request.state, "csrf_deferred", False)),
        max_file_bytes=limit,
    )
    try:
        await _receive_upload(request, sink, boundary)
        if sink.csrf_required and not csrf.token_matches(request.session, sink.fields.get(csrf.FORM_FIELD)):
            raise csrf.CSRFError()
        if sink.path is None or sink.size == 0:
            raise UploadRejected("library.upload_no_file")
    except UploadRejected as exc:
        sink.discard()
        return await run_in_threadpool(_upload_error, request, db, ctx, sink, exc)
    except FormParserError:
        sink.discard()
        return await run_in_threadpool(
            _upload_error, request, db, ctx, sink, UploadRejected("library.upload_bad_request")
        )
    except BaseException:
        sink.discard()
        raise
    return await run_in_threadpool(_import_upload, request, db, ctx, sink)


def _import_upload(request: Request, db: Session, ctx: WorkspaceContext, sink: UploadSink) -> Response:
    assert sink.path is not None
    try:
        profile = _upload_profile(db, ctx, sink.fields.get("profile_id"))
        setup_plan = _setup_plan(db, ctx, sink.fields, profile)
        selection = sink.fields.get("format", "video_reel")
        if selection not in {"video_reel", "video_story", "image_post", "image_story"} or (
            profile is None and selection != "video_reel"
        ):
            raise FormError("media.invalid_format")
        kind, placement = selection.split("_")
        video = manual.create_video_from_upload(
            db,
            workspace_id=ctx.workspace.id,
            upload_path=sink.path,
            original_filename=sink.filename or "video",
            caption=sink.fields.get("caption", "")[:MAX_CAPTION_CHARS],
            title=sink.fields.get("title", "")[:MAX_TITLE_CHARS],
            actor=ctx.actor,
            profile_id=profile.id if profile else None,
            media_kind=kind,
            content_format=placement,
        )
        db.commit()
    except (FormError, ValueError) as exc:
        db.rollback()
        return _upload_error(request, db, ctx, sink, exc)
    finally:
        if sink.path.exists():
            sink.path.unlink(missing_ok=True)
    flash(request, "success", "library.uploaded", title=video.title)
    return RedirectResponse(
        f"/plans/{setup_plan.id}/setup/content" if setup_plan else f"/library/{video.id}", status_code=303
    )


def _upload_error(request, db, ctx, sink, exc) -> Response:
    # A partial multipart body is untrusted until its CSRF token has been checked.
    if sink.csrf_required and not csrf.token_matches(request.session, sink.fields.get(csrf.FORM_FIELD)):
        raise csrf.CSRFError()
    return _library_error(request, db, ctx, sink.fields, "upload", exc)


def _library_error(request, db, ctx, fields, form_name, exc) -> Response:
    try:
        person = _upload_profile(db, ctx, form_str(fields, "profile_id", max_len=10))
    except FormError:
        person = None
    if person is None:
        messages.flash_error(request, exc)
        return RedirectResponse("/library?profile_id=0", status_code=303)
    setup_plan = _setup_plan(db, ctx, fields, person)
    if setup_plan:
        from repeater.web.routes.plans import _context, _wizard

        data = _context(db, ctx, setup_plan)
        data.update(error=exc, draft={"form": form_name, **_content_draft(fields)})
        return _wizard(request, db, ctx, setup_plan, "content", data=data)
    messages.flash_error(request, exc)
    return _library_page(
        request, db, ctx, profile_id=person.id, draft={"form": form_name, **_content_draft(fields)}
    )


def _content_draft(fields) -> dict[str, str]:
    """Keep bounded drafts in the response, never in the small signed session cookie."""
    draft = {}
    for key, limit in (("title", MAX_TITLE_CHARS * 4), ("caption", MAX_CAPTION_CHARS * 4), ("format", 32)):
        value = fields.get(key, "")
        draft[key] = value[:limit] if isinstance(value, str) else ""
    return draft


def _upload_profile(db: Session, ctx: WorkspaceContext, value: str | None) -> CreatorProfile | None:
    if not value:
        return None
    if not value.isascii() or not value.isdecimal() or len(value) > 10 or not 1 <= int(value) <= 2147483647:
        raise FormError("media.person_required")
    person = db.scalar(
        select(CreatorProfile).where(
            CreatorProfile.id == int(value), CreatorProfile.workspace_id == ctx.workspace.id
        )
    )
    if person is None:
        raise FormError("media.person_required")
    return person


def _setup_plan(db, ctx, fields, person):
    """Only an owned matching person can return an upload to guided setup."""
    raw = fields.get("setup_plan_id")
    if raw is None:
        return None
    if (
        not isinstance(raw, str)
        or not raw.isascii()
        or not raw.isdecimal()
        or len(raw) > 10
        or not 1 <= int(raw) <= 2147483647
    ):
        raise HTTPException(400)
    plan = db.scalar(
        select(ContentPlan).where(
            ContentPlan.id == int(raw),
            ContentPlan.workspace_id == ctx.workspace.id,
            ContentPlan.profile_id == (person.id if person else None),
        )
    )
    if plan is None:
        raise HTTPException(404)
    return plan


@router.post("/library/text")
def create_text(request: Request, db: DB, ctx: CurrentWorkspace, form: Form) -> Response:
    try:
        person = _upload_profile(db, ctx, form_str(form, "profile_id", max_len=10, required=True))
        if person is None:
            raise FormError("media.person_required")
        setup_plan = _setup_plan(db, ctx, form, person)
        body = form_text(form, "caption", max_len=MAX_CAPTION_CHARS)
        if not body.strip():
            raise FormError("media.empty_text")
        title = form_str(form, "title", max_len=MAX_TITLE_CHARS, required=True)
        from repeater.commerce import require_capacity

        require_capacity(db, ctx.workspace.id, "items")
        item = Video(
            workspace_id=ctx.workspace.id,
            profile_id=person.id,
            media_kind="text",
            content_format="post",
            title=title,
            caption=body,
            status=VideoStatus.READY,
            overrides={},
        )
        db.add(item)
        db.flush()
        log_activity(
            db,
            ctx.workspace.id,
            ActivityLevel.INFO,
            "import",
            "Added a text post to a person's library.",
            actor=ctx.actor,
            video_id=item.id,
            msg_key="media.text_added",
        )
        db.commit()
    except FormError as exc:
        db.rollback()
        return _library_error(request, db, ctx, form, "text", exc)
    if setup_plan:
        flash(request, "success", "media.text_added")
    return RedirectResponse(
        f"/plans/{setup_plan.id}/setup/content" if setup_plan else f"/library/{item.id}", status_code=303
    )
