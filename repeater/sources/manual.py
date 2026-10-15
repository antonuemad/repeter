"""Manual uploads from the dashboard. OWNER: sources/media module."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from repeater.activity import log_activity
from repeater.jobs import kinds
from repeater.media import images, storage
from repeater.media.content import FORMATS
from repeater.media.probe import ProbeResult, probe
from repeater.messages import Message, UserError
from repeater.models import ActivityLevel, Source, SourceKind, Video, VideoStatus

MANUAL_SOURCE_NAME = "Manual uploads"
_ALLOWED_EXTENSIONS = frozenset(
    {"mp4", "mov", "m4v", "webm", "mkv", "avi", "3gp", "mpg", "mpeg", "ts", "flv", "wmv"}
)
# ffprobe format names -> file extension, in order of preference, for uploads whose name has no
# usable extension. MP4 and MOV share one demuxer ("mov,mp4,m4a,..."), so MP4 wins there.
_FORMAT_EXTENSIONS = {
    "mp4": "mp4",
    "matroska": "mkv",
    "avi": "avi",
    "mpegts": "ts",
    "flv": "flv",
    "mpeg": "mpg",
    "asf": "wmv",
}


def create_video_from_upload(
    session: Session,
    *,
    workspace_id: int,
    upload_path: Path,
    original_filename: str,
    caption: str = "",
    title: str = "",
    actor: str = "user",
    profile_id: int | None = None,
    media_kind: str = "video",
    content_format: str = "reel",
) -> Video:
    """Move an uploaded temp file (under storage.uploads_dir()) into the library as a new
    Video (source_platform None, source kind manual), and enqueue process_video. Validates the
    file with ffprobe first; raises ValueError with a user-facing message if it is not a video
    (the rejected temp file is deleted). The caller commits."""
    upload = _checked_upload(upload_path)
    if (
        content_format not in FORMATS.get(media_kind, ())
        or media_kind == "text"
        or (media_kind != "video" and profile_id is None)
    ):
        raise UserError("media.invalid_format", "Choose a supported content type and placement.")
    try:
        if media_kind == "image":
            images.decode(upload).close()
            info = None
        else:
            info = probe(upload)
    except ValueError as exc:
        upload.unlink(missing_ok=True)
        if isinstance(exc, UserError):
            raise
        raise UserError(
            "err.upload_unsupported",
            "'{filename}' is {detail}",
            filename=_display_name(original_filename),
            detail=str(exc),
        ) from None

    source = manual_source(session, workspace_id)
    video = Video(
        workspace_id=workspace_id,
        profile_id=profile_id,
        media_kind=media_kind,
        content_format=content_format,
        source_id=source.id,
        source_platform=None,
        external_id=None,
        title=(title or "").strip() or _display_name(original_filename, stem=True),
        caption=(caption or "").strip(),
        status=VideoStatus.PROCESSING,
        duration_s=info.duration_s if info else None,
        width=info.width if info else None,
        height=info.height if info else None,
        fps=info.fps if info else None,
        overrides={},
    )
    session.add(video)
    session.flush()

    dest = (
        storage.ensure_video_dir(workspace_id, video.id)
        / f"original.{_extension(original_filename, info) if info else 'image'}"
    )
    _move(upload, dest)
    video.original_path = storage.rel_path(dest)
    video.size_bytes = dest.stat().st_size
    kinds.enqueue_process_video(session, video.id)
    log_activity(
        session,
        workspace_id,
        ActivityLevel.INFO,
        "import",
        Message(
            "import.uploaded",
            f"Uploaded '{video.title}'; preparing it for publishing.",
            {"title": video.title},
        ),
        actor=actor,
        video_id=video.id,
    )
    return video


def manual_source(session: Session, workspace_id: int) -> Source:
    """The workspace's single 'manual uploads' source, created on first use."""
    source = session.execute(
        select(Source)
        .where(Source.workspace_id == workspace_id, Source.kind == SourceKind.MANUAL)
        .order_by(Source.id)
        .limit(1)
    ).scalar()
    if source is None:
        source = Source(
            workspace_id=workspace_id,
            kind=SourceKind.MANUAL,
            platform=None,
            name=MANUAL_SOURCE_NAME,
            config={},
            enabled=True,
        )
        session.add(source)
        session.flush()
    return source


def _checked_upload(upload_path: Path) -> Path:
    path = Path(upload_path).resolve()
    uploads = storage.uploads_dir().resolve()
    if uploads not in path.parents:
        raise UserError("err.upload_not_found", "The upload was not found. Please upload the file again.")
    if not path.is_file() or path.stat().st_size == 0:
        raise UserError(
            "err.upload_empty", "The upload is empty or incomplete. Please upload the file again."
        )
    return path


def _extension(original_filename: str, info: ProbeResult) -> str:
    ext = Path(original_filename or "").suffix.lower().lstrip(".")
    if ext in _ALLOWED_EXTENSIONS:
        return ext
    names = set(info.format_name.split(","))
    return next((ext for name, ext in _FORMAT_EXTENSIONS.items() if name in names), "mp4")


def _display_name(original_filename: str, *, stem: bool = False) -> str:
    name = Path((original_filename or "").replace("\\", "/")).name.strip() or "video"
    return (Path(name).stem if stem else name)[:200]


def _move(src: Path, dest: Path) -> None:
    try:
        os.replace(src, dest)
    except OSError:
        shutil.move(str(src), str(dest))  # different filesystem
