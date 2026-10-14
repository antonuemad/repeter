"""User actions on the schedule. OWNER: engine module (implemented by the engine work).

These are called by the web dashboard. Each function commits nothing; the caller commits.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from repeater.models import ConnectedAccount, ScheduledPost, Video


def skip_post(session: Session, post: ScheduledPost, *, actor: str) -> None:
    """Mark a scheduled (not yet uploading) post as skipped; the planner will not recreate it
    for the same cycle."""
    raise NotImplementedError


def publish_now(session: Session, post: ScheduledPost, *, actor: str) -> None:
    """Move a scheduled/failed/needs_attention post to now and enqueue publish_post."""
    raise NotImplementedError


def schedule_video_now(
    session: Session, video: Video, account: ConnectedAccount, *, actor: str
) -> ScheduledPost:
    """Create (or reuse) the post for video+account in the current cycle, scheduled now,
    bypassing windows/caps but not platform validation. Enqueue publish_post."""
    raise NotImplementedError


def archive_video(session: Session, video: Video, *, actor: str) -> None:
    """Remove a video from rotation and cancel its future posts."""
    raise NotImplementedError


def restore_video(session: Session, video: Video, *, actor: str) -> None:
    """Return an archived video to rotation (status ready) and trigger planning."""
    raise NotImplementedError
