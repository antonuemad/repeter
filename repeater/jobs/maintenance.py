"""Hourly housekeeping job ("maintenance").

Each step runs in its own savepoint so one failing step never blocks the others:
    - return crashed workers' jobs to the queue and purge old finished jobs;
    - delete OAuth states older than 1 hour and activity older than 90 days;
    - delete expired original files (repeater.media.retention);
    - repair posts whose job was lost: re-queue publish_post / poll_post (the dedupe keys make
      this a no-op when the job exists) and turn uploads interrupted by a crash into
      needs-attention, because retrying them blindly could publish the video twice;
    - YouTube API data retention (PLAN.md 7.1): the API data kept for published YouTube posts
      (video id, link, upload state) is deleted after platform_data_retention_days (30), and
      the channel data of disconnected YouTube accounts is deleted at once (well within 7 days).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from repeater import queue
from repeater.activity import log_activity
from repeater.config import get_settings
from repeater.jobs import kinds
from repeater.jobs.registry import handler
from repeater.models import (
    AccountStatus,
    ActivityLevel,
    ActivityLog,
    ConnectedAccount,
    OAuthState,
    Platform,
    PostStatus,
    ScheduledPost,
)
from repeater.timeutil import utcnow

log = logging.getLogger("repeater.maintenance")

OAUTH_STATE_TTL = timedelta(hours=1)
ACTIVITY_RETENTION = timedelta(days=90)
FINISHED_JOB_RETENTION_DAYS = 14
# An upload still marked UPLOADING after this long was interrupted (worker crash or restart).
STUCK_UPLOAD_AFTER = timedelta(hours=2)
# Only posts this overdue are checked for a lost publish job.
LOST_JOB_GRACE = timedelta(minutes=10)


@handler(kinds.MAINTENANCE)
def handle_maintenance(session: Session, payload: dict) -> None:
    run_maintenance(session)
    session.commit()


def run_maintenance(session: Session, now: datetime | None = None) -> dict[str, int]:
    """Run every housekeeping step; returns a count per step (-1 when a step failed).
    Flushes but does not commit."""
    now = now or utcnow()
    settings = get_settings()
    steps: list[tuple[str, Callable[[], int]]] = [
        ("requeued_jobs", lambda: queue.requeue_stale(session, settings.job_lock_timeout_minutes)),
        ("purged_jobs", lambda: queue.purge_finished(session, FINISHED_JOB_RETENTION_DAYS)),
        ("oauth_states", lambda: _delete_old_oauth_states(session, now)),
        ("activity", lambda: _delete_old_activity(session, now)),
        ("originals", lambda: _purge_originals(session, settings.original_retention_days, now)),
        ("stuck_uploads", lambda: _flag_stuck_uploads(session, now)),
        ("requeued_posts", lambda: _requeue_lost_posts(session, now)),
        (
            "youtube_posts",
            lambda: _expire_youtube_post_data(session, settings.platform_data_retention_days, now),
        ),
        ("youtube_accounts", lambda: _scrub_disconnected_youtube(session)),
    ]
    results: dict[str, int] = {}
    for name, step in steps:
        try:
            with session.begin_nested():
                results[name] = int(step() or 0)
        except Exception as exc:  # noqa: BLE001 - isolate failing steps
            log.error("maintenance step %s failed (%s)", name, type(exc).__name__)
            results[name] = -1
    session.flush()
    log.info("maintenance done: %s", results)
    return results


def _delete_old_oauth_states(session: Session, now: datetime) -> int:
    res = session.execute(delete(OAuthState).where(OAuthState.created_at < now - OAUTH_STATE_TTL))
    return res.rowcount or 0


def _delete_old_activity(session: Session, now: datetime) -> int:
    res = session.execute(delete(ActivityLog).where(ActivityLog.created_at < now - ACTIVITY_RETENTION))
    return res.rowcount or 0


def _purge_originals(session: Session, days: int, now: datetime) -> int:
    from repeater.media import retention

    try:
        return retention.purge_expired_originals(session, older_than_days=days, now=now)
    except NotImplementedError:
        return 0


def _flag_stuck_uploads(session: Session, now: datetime) -> int:
    posts = session.execute(
        select(ScheduledPost).where(
            ScheduledPost.status == PostStatus.UPLOADING,
            ScheduledPost.updated_at < now - STUCK_UPLOAD_AFTER,
        )
    ).scalars()
    count = 0
    for post in posts:
        post.status = PostStatus.NEEDS_ATTENTION
        post.error = (
            "The upload was interrupted (the worker restarted). Check the account before publishing "
            "again, so the video is not posted twice."
        )
        log_activity(
            session,
            post.workspace_id,
            ActivityLevel.ERROR,
            "publish",
            f"Post #{post.id} to {post.platform}: {post.error}",
            post_id=post.id,
            video_id=post.video_id,
            account_id=post.connected_account_id,
        )
        count += 1
    return count


def _requeue_lost_posts(session: Session, now: datetime) -> int:
    count = 0
    due = session.execute(
        select(ScheduledPost.id).where(
            ScheduledPost.status.in_([PostStatus.SCHEDULED, PostStatus.FAILED]),
            ScheduledPost.scheduled_at < now - LOST_JOB_GRACE,
        )
    ).scalars()
    for post_id in due:
        count += kinds.enqueue_publish_post(session, post_id) is not None
    processing = session.execute(
        select(ScheduledPost.id).where(ScheduledPost.status == PostStatus.PROCESSING)
    ).scalars()
    for post_id in processing:
        count += kinds.enqueue_poll_post(session, post_id) is not None
    return count


def _expire_youtube_post_data(session: Session, days: int, now: datetime) -> int:
    res = session.execute(
        update(ScheduledPost)
        .where(
            ScheduledPost.platform == Platform.YOUTUBE,
            ScheduledPost.status == PostStatus.PUBLISHED,
            ScheduledPost.published_at < now - timedelta(days=max(int(days), 1)),
            (ScheduledPost.platform_post_id.isnot(None))
            | (ScheduledPost.permalink.isnot(None))
            | (ScheduledPost.publish_state != {}),
        )
        .values(platform_post_id=None, permalink=None, publish_state={})
        .execution_options(synchronize_session=False)
    )
    return res.rowcount or 0


def _scrub_disconnected_youtube(session: Session) -> int:
    accounts = session.execute(
        select(ConnectedAccount).where(
            ConnectedAccount.platform == Platform.YOUTUBE,
            ConnectedAccount.status == AccountStatus.DISABLED,
            ConnectedAccount.token_enc.is_(None),
        )
    ).scalars()
    count = 0
    for account in accounts:
        if not account.extra and account.avatar_url is None:
            continue
        account.extra = {}
        account.avatar_url = None
        session.execute(
            update(ScheduledPost)
            .where(ScheduledPost.connected_account_id == account.id)
            .values(platform_post_id=None, permalink=None, publish_state={})
            .execution_options(synchronize_session=False)
        )
        count += 1
    return count
