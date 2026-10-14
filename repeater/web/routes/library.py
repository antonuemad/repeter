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
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool

from repeater.activity import log_activity
from repeater.config import get_settings
from repeater.engine import actions, captions
from repeater.jobs import kinds
from repeater.media import storage
from repeater.models import (
    PUBLISH_PLATFORMS,
    AccountStatus,
    ActivityLevel,
    ConnectedAccount,
    Platform,
    PostStatus,
    RotationRule,
    ScheduledPost,
    Video,
    VideoStatus,
)
from repeater.sources import manual
from repeater.web import csrf
from repeater.web.auth import CurrentWorkspace, WorkspaceContext
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
            )
            .order_by(ConnectedAccount.platform, ConnectedAccount.id)
        ).scalars()
    )


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
    request: Request, db: DB, ctx: CurrentWorkspace, status: str = "active", page: str = "1"
) -> Response:
    statuses = STATUS_FILTERS.get(status) or STATUS_FILTERS["active"]
    status = status if status in STATUS_FILTERS else "active"
    where = (Video.workspace_id == ctx.workspace.id, Video.status.in_(statuses))
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
    accounts = target_accounts(db, ctx.workspace.id)
    context = {
        "nav": "library",
        "videos": videos,
        "status_filter": status,
        "status_filters": list(STATUS_FILTERS),
        "pager": pager,
        "accounts": accounts,
        "posts": latest_posts(db, [v.id for v in videos]),
        "max_upload_mb": get_settings().max_upload_mb,
    }
    return render(request, "library.html", context)


@router.get("/library/{video_id}")
def video_detail(request: Request, db: DB, ctx: CurrentWorkspace, video_id: int) -> Response:
    video = get_video(db, ctx, video_id)
    posts = list(
        db.execute(
            select(ScheduledPost)
            .options(selectinload(ScheduledPost.connected_account))
            .where(ScheduledPost.video_id == video.id)
            .order_by(ScheduledPost.scheduled_at.desc(), ScheduledPost.id.desc())
        ).scalars()
    )
    accounts = target_accounts(db, ctx.workspace.id)
    context = {
        "nav": "library",
        "video": video,
        "posts": posts,
        "accounts": [a for a in accounts if a.id != video.source_account_id],
        "previews": _caption_previews(db, video, accounts),
        "excluded": _excluded_platforms(video),
        "publish_platforms": [p.value for p in PUBLISH_PLATFORMS],
        "has_preview": _preview_file(video) is not None,
        "overrides": video.overrides or {},
    }
    return render(request, "library_detail.html", context)


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
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=300"})


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
        flash(request, "error", text=str(exc))
    else:
        flash(request, "success", success_key)
    return _back(form, default_back)


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
    try:
        actions.schedule_video_now(db, video, account, actor=ctx.actor)
        db.commit()
    except ValueError as exc:
        db.rollback()
        flash(request, "error", text=str(exc))
    else:
        flash(request, "success", "library.publishing_now", account=account.display_name)
    return _back(form, f"/library/{video.id}")


@router.post("/library/{video_id}/edit")
def edit_video(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, video_id: int) -> Response:
    """Per-video title/caption overrides and the platforms it may be published to (YouTube
    Required Minimum Functionality: users choose title, description and platforms)."""
    video = get_video(db, ctx, video_id)
    try:
        title = form_str(form, "title", max_len=MAX_TITLE_CHARS)
        caption = form_text(form, "caption", max_len=MAX_CAPTION_CHARS)
    except FormError as exc:
        flash(request, "error", exc.key, **exc.params)
        return RedirectResponse(f"/library/{video.id}", status_code=303)
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
        .values(status=PostStatus.CANCELED, error="platform excluded for this video")
    )
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- streaming upload

UPLOAD_PATH = "/library/upload"
FILE_FIELD = "file"
TEXT_FIELD_LIMITS = {csrf.FORM_FIELD: 256, "title": MAX_TITLE_CHARS * 4, "caption": MAX_CAPTION_CHARS * 4}
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
        flash(request, "error", exc.key, **exc.params)
        return RedirectResponse("/library", status_code=303)
    except FormParserError:
        sink.discard()
        flash(request, "error", "library.upload_bad_request")
        return RedirectResponse("/library", status_code=303)
    except BaseException:
        sink.discard()
        raise
    return await run_in_threadpool(_import_upload, request, db, ctx, sink)


def _import_upload(request: Request, db: Session, ctx: WorkspaceContext, sink: UploadSink) -> Response:
    assert sink.path is not None
    try:
        video = manual.create_video_from_upload(
            db,
            workspace_id=ctx.workspace.id,
            upload_path=sink.path,
            original_filename=sink.filename or "video",
            caption=sink.fields.get("caption", "")[:MAX_CAPTION_CHARS],
            title=sink.fields.get("title", "")[:MAX_TITLE_CHARS],
            actor=ctx.actor,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        flash(request, "error", text=str(exc))
        return RedirectResponse("/library", status_code=303)
    finally:
        if sink.path.exists():
            sink.path.unlink(missing_ok=True)
    flash(request, "success", "library.uploaded", title=video.title)
    return RedirectResponse(f"/library/{video.id}", status_code=303)
