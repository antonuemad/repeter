"""Disk retention for original files (PLAN.md 6.1). OWNER: sources/media module.

Encoded files and thumbnails are never touched here: they are what gets published and they stay
until the user deletes the video. Originals go after settings.original_retention_days
(purge_expired_originals: videos with an encoded file, and duplicates) or FAILED_ORIGINAL_DAYS
(purge_failed_originals: videos that failed to import or encode have nothing else on disk worth
keeping and can be up to 4 GB; purge_expired_originals(failed_older_than_days=...) does both in one
pass). Abandoned *.part files left by killed downloads and encodes are swept by
sweep_partial_files().
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session

from repeater.media import storage
from repeater.models import ScheduledPost, Video, VideoStatus
from repeater.timeutil import utcnow

log = logging.getLogger("repeater.media")

# Originals of FAILED videos are deleted this long after the failure (retention contract C5(e)).
FAILED_ORIGINAL_DAYS = 7
# Temporary files of downloads and encodes older than this are abandoned (their job is long gone).
PARTIAL_FILE_MAX_AGE = timedelta(days=1)
_PARTIAL_GLOBS = ("ws*/v*/*.part", "ws*/v*/.*.part", "uploads/*.part")


def purge_expired_originals(
    session: Session,
    *,
    older_than_days: int,
    now: datetime | None = None,
    failed_older_than_days: int | None = None,
) -> int:
    """Delete original files (keep encoded + thumbnail) for videos whose newest publish or
    import is older than `older_than_days`. With `failed_older_than_days` it also deletes the
    originals of FAILED videos that have been failed that long (see purge_failed_originals for
    that alone). Clears Video.original_path. Returns files removed.

    An original is only removed when the video's encoded file exists on disk, so no video is
    left without a playable file. Duplicates are the exception: they are never encoded and their
    content already lives in the video they duplicate. FAILED videos have no encoded file; a
    retry downloads the original again, or asks for a new upload. The caller commits."""
    current = now or utcnow()
    cutoff = current - timedelta(days=max(0, int(older_than_days)))
    last_publish = (
        select(ScheduledPost.video_id, func.max(ScheduledPost.published_at).label("last_published"))
        .group_by(ScheduledPost.video_id)
        .subquery()
    )
    # GREATEST ignores NULLs in PostgreSQL, so never-published videos fall back to created_at.
    last_activity = func.greatest(Video.created_at, last_publish.c.last_published)
    query = (
        select(Video)
        .outerjoin(last_publish, last_publish.c.video_id == Video.id)
        .where(
            Video.original_path.isnot(None),
            or_(
                and_(
                    or_(
                        Video.encoded_path.isnot(None),
                        Video.status.in_([VideoStatus.DUPLICATE, VideoStatus.ARCHIVED]),
                    ),
                    last_activity < cutoff,
                ),
                *(
                    [_failed_before(current, failed_older_than_days)]
                    if failed_older_than_days is not None
                    else []
                ),
            ),
        )
        .order_by(Video.id)
    )
    removed = 0
    for video in session.execute(query).scalars():
        removed += _purge_original(video)
    session.flush()
    return removed


def purge_failed_originals(
    session: Session, *, older_than_days: int = FAILED_ORIGINAL_DAYS, now: datetime | None = None
) -> int:
    """Delete the original of every video that has been FAILED for `older_than_days` (counted from
    its last change) and clear Video.original_path. Retrying such a video downloads it again, or
    asks for a new upload. Returns files removed. The caller commits."""
    query = (
        select(Video)
        .where(
            Video.original_path.isnot(None),
            _failed_before(now or utcnow(), older_than_days),
        )
        .order_by(Video.id)
    )
    removed = 0
    for video in session.execute(query).scalars():
        removed += _purge_original(video)
    session.flush()
    return removed


def _failed_before(now: datetime, days: int) -> ColumnElement[bool]:
    return and_(
        Video.status == VideoStatus.FAILED, Video.updated_at < now - timedelta(days=max(0, int(days)))
    )


def _purge_original(video: Video) -> int:
    try:
        original = storage.abs_path(video.original_path)
        encoded = storage.abs_path(video.encoded_path)
    except ValueError:
        log.warning("video %s has a stored path outside the media root; skipped", video.id)
        return 0
    if original is None:
        return 0
    if video.status not in (VideoStatus.DUPLICATE, VideoStatus.FAILED, VideoStatus.ARCHIVED) and (
        encoded is None or not encoded.is_file()
    ):
        return 0
    if encoded is not None and original == encoded:
        return 0
    existed = original.is_file()
    try:
        original.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not delete original of video %s: %s", video.id, type(exc).__name__)
        return 0
    video.original_path = None
    return 1 if existed else 0


def sweep_partial_files(*, max_age: timedelta = PARTIAL_FILE_MAX_AGE, now: datetime | None = None) -> int:
    """Delete abandoned temporary files under the media root: yt-dlp / download `.part` files of
    permanently failed imports and `.encoded.mp4.<id>.part` files of encodes whose worker was
    killed. Files younger than `max_age` may belong to a running job and are kept. Returns the
    number of files removed."""
    root = storage.media_root()
    cutoff = (now or utcnow()).timestamp() - max_age.total_seconds()
    removed = 0
    for pattern in _PARTIAL_GLOBS:
        for path in root.glob(pattern):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError as exc:
                log.warning("could not delete %s (%s)", path.name, type(exc).__name__)
    return removed
