"""Disk retention for original files (PLAN.md 6.1). OWNER: sources/media module.

Encoded files and thumbnails are never touched here: they are what gets published and they stay
until the user deletes the video.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from repeater.media import storage
from repeater.models import ScheduledPost, Video, VideoStatus
from repeater.timeutil import utcnow

log = logging.getLogger("repeater.media")


def purge_expired_originals(session: Session, *, older_than_days: int, now: datetime | None = None) -> int:
    """Delete original files (keep encoded + thumbnail) for videos whose newest publish or
    import is older than `older_than_days`. Clears Video.original_path. Returns files removed.

    An original is only removed when the video's encoded file exists on disk, so no video is
    left without a playable file. Duplicates are the exception: they are never encoded and their
    content already lives in the video they duplicate. The caller commits."""
    cutoff = (now or utcnow()) - timedelta(days=max(0, int(older_than_days)))
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
            or_(Video.encoded_path.isnot(None), Video.status == VideoStatus.DUPLICATE),
            last_activity < cutoff,
        )
        .order_by(Video.id)
    )
    removed = 0
    for video in session.execute(query).scalars():
        removed += _purge_original(video)
    session.flush()
    return removed


def _purge_original(video: Video) -> int:
    try:
        original = storage.abs_path(video.original_path)
        encoded = storage.abs_path(video.encoded_path)
    except ValueError:
        log.warning("video %s has a stored path outside the media root; skipped", video.id)
        return 0
    if original is None:
        return 0
    if video.status != VideoStatus.DUPLICATE and (encoded is None or not encoded.is_file()):
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
