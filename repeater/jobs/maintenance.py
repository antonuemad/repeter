"""Hourly housekeeping job ("maintenance").

Each step runs in its own savepoint so one failing step never blocks the others:
    - return the jobs of dead workers to the queue (also done on every worker tick) and purge old
      finished jobs;
    - delete OAuth states older than 1 hour and activity older than 90 days;
    - purge original files (repeater.media.retention; originals of failed videos after 7 days)
      and abandoned partial downloads/encodes (*.part older than a day);
    - repair posts whose job was lost: re-queue publish_post / poll_post (the dedupe keys make
      this a no-op when the job exists) and turn uploads interrupted by a crash into
      needs-attention ("may be live", with one alert per workspace after the commit), because
      retrying them blindly could publish the video twice;
    - data retention (PLAN.md 7.1, YouTube Developer Policies III.E.4.c):
        (b) accounts disconnected more than DISCONNECTED_RETENTION ago are deleted with their post
            history (platform post ids, permalinks) and the activity-log entries that name them;
            videos imported from them stay;
        (c) YouTube API data of active accounts is deleted after platform_data_retention_days
            (30) unless refreshed: video ids, links and upload state on every post that is not in
            flight, permalinks and the channel title in old activity entries, and the channel
            title and avatar of accounts that could not be refreshed (engine.jobs refreshes them
            every 25 days).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import Text, case, cast, delete, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from repeater import notify as notify_mod
from repeater import queue
from repeater.accounts import (
    GENERIC_CHANNEL_NAME,
    channel_data_refreshed_at,
    delete_account_records,
    disconnected_at,
    redact_name_in_activity,
)
from repeater.activity import log_activity
from repeater.config import get_settings
from repeater.engine.publisher import mark_may_be_live
from repeater.jobs import kinds
from repeater.jobs.registry import handler
from repeater.media import storage
from repeater.models import (
    AccountStatus,
    ActivityLevel,
    ActivityLog,
    ConnectedAccount,
    OAuthState,
    Platform,
    PostStatus,
    ScheduledPost,
    Video,
    VideoStatus,
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
# A disconnected account is deleted this long after the disconnect. The commitments are "within 7
# days" (YouTube Developer Policies, the privacy policy); the margin covers a stopped worker.
DISCONNECTED_RETENTION = timedelta(days=5)
# Originals of videos that failed to import or encode are kept this long (for retries/diagnosis).
FAILED_ORIGINAL_RETENTION = timedelta(days=7)
# Partial downloads, encodes and uploads older than this were abandoned.
PARTIAL_FILE_MAX_AGE = timedelta(days=1)
# Posts in these statuses may be on the platform without being published; when their API data
# is expired a marker remains so the planner still knows (no API data in it).
_NOT_CONFIRMED = (PostStatus.NEEDS_ATTENTION, PostStatus.CANCELED)


@handler(kinds.MAINTENANCE)
def handle_maintenance(session: Session, payload: dict) -> None:
    flagged: list[tuple[int, int]] = []
    run_maintenance(session, flagged=flagged)
    session.commit()
    _alert_interrupted_uploads(flagged)


def run_maintenance(
    session: Session, now: datetime | None = None, *, flagged: list[tuple[int, int]] | None = None
) -> dict[str, int]:
    """Run every housekeeping step; returns a count per step (-1 when a step failed).
    Flushes but does not commit. Uploads turned into needs-attention are appended to `flagged`
    as (workspace_id, post_id): the caller alerts after committing."""
    now = now or utcnow()
    settings = get_settings()
    retention_days = settings.platform_data_retention_days
    steps: list[tuple[str, Callable[[], int]]] = [
        ("requeued_jobs", lambda: queue.requeue_stale(session, queue.lease_timeout_minutes())),
        ("purged_jobs", lambda: queue.purge_finished(session, FINISHED_JOB_RETENTION_DAYS)),
        ("oauth_states", lambda: _delete_old_oauth_states(session, now)),
        ("activity", lambda: _delete_old_activity(session, now)),
        ("originals", lambda: _purge_originals(session, settings.original_retention_days, now)),
        ("failed_originals", lambda: _purge_failed_originals(session, now)),
        ("partial_files", lambda: _purge_partial_files(now)),
        ("stuck_uploads", lambda: _flag_stuck_uploads(session, now, flagged)),
        ("requeued_posts", lambda: _requeue_lost_posts(session, now)),
        ("youtube_posts", lambda: _expire_youtube_post_data(session, retention_days, now)),
        ("youtube_activity", lambda: _expire_youtube_activity(session, retention_days, now)),
        ("youtube_channels", lambda: _scrub_stale_youtube_channels(session, retention_days, now)),
        ("disconnected_accounts", lambda: _delete_disconnected_accounts(session, now)),
    ]
    results: dict[str, int] = {}
    for name, step in steps:
        try:
            with session.begin_nested():
                results[name] = int(step() or 0)
        except Exception as exc:  # noqa: BLE001 - isolate failing steps
            log.error("maintenance step %s failed (%s)", name, type(exc).__name__)
            results[name] = -1
            if name == "stuck_uploads" and flagged is not None:
                flagged.clear()  # the step rolled back: nothing was flagged
    session.flush()
    log.info("maintenance done: %s", results)
    return results


def _alert_interrupted_uploads(flagged: list[tuple[int, int]]) -> None:
    """One alert per workspace for the uploads maintenance just turned into needs-attention. Sent
    after the commit: a rolled-back step must not alert."""
    by_workspace: dict[int, list[int]] = defaultdict(list)
    for workspace_id, post_id in flagged:
        by_workspace[workspace_id].append(post_id)
    for workspace_id, post_ids in by_workspace.items():
        count = len(post_ids)
        notify_mod.notify(
            f"{count} upload{'s were' if count != 1 else ' was'} interrupted (the worker restarted) and "
            f"may be live: post{'s' if count != 1 else ''} "
            f"{', '.join(f'#{i}' for i in post_ids[:20])}. Check the account before publishing again.",
            workspace_id=workspace_id,
            level="error",
            category="uploads_interrupted",
            count=count,
        )


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


def _purge_failed_originals(session: Session, now: datetime) -> int:
    """Delete the original file of videos that failed to import or encode after 7 days."""
    videos = session.execute(
        select(Video).where(
            Video.status == VideoStatus.FAILED,
            Video.original_path.isnot(None),
            Video.updated_at < now - FAILED_ORIGINAL_RETENTION,
        )
    ).scalars()
    removed = 0
    for video in videos:
        try:
            path = storage.abs_path(video.original_path)
            if path is not None and path.is_file():
                path.unlink()
                removed += 1
        except (OSError, ValueError) as exc:
            log.warning("could not delete the original of failed video %s (%s)", video.id, type(exc).__name__)
            continue
        video.original_path = None
    return removed


def _purge_partial_files(now: datetime) -> int:
    """Delete abandoned *.part files (killed encodes, downloads and dashboard uploads)."""
    root = storage.media_root()
    cutoff = now.timestamp() - PARTIAL_FILE_MAX_AGE.total_seconds()
    removed = 0
    for pattern in ("uploads/*.part", "ws*/v*/*.part"):
        for path in root.glob(pattern):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError as exc:
                log.warning("could not delete a partial file (%s)", type(exc).__name__)
    return removed


def _flag_stuck_uploads(session: Session, now: datetime, flagged: list[tuple[int, int]] | None = None) -> int:
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
        mark_may_be_live(post)
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
        if flagged is not None:
            flagged.append((post.workspace_id, post.id))
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
    """Delete the YouTube API data kept on posts: the video id, the link and the adapter's upload
    state, on every post that is not in flight (published, failed, canceled, needs-attention ...;
    uploading and processing posts still need the id to poll). A needs-attention or canceled post
    keeps a marker (no API data) that it may be live, so its cycle stays blocked."""
    cutoff = now - timedelta(days=max(int(days), 1))
    adapter_keys = ScheduledPost.publish_state.op("-", return_type=JSONB)(literal("_engine", type_=Text))
    marker = literal({"_engine": {"may_be_live": True}}, type_=JSONB)
    new_state = case((ScheduledPost.status.in_(_NOT_CONFIRMED), marker), else_=literal({}, type_=JSONB))
    res = session.execute(
        update(ScheduledPost)
        .where(
            ScheduledPost.platform == Platform.YOUTUBE,
            ScheduledPost.status.notin_([PostStatus.UPLOADING, PostStatus.PROCESSING]),
            func.coalesce(ScheduledPost.published_at, ScheduledPost.updated_at) < cutoff,
            or_(
                ScheduledPost.platform_post_id.isnot(None),
                ScheduledPost.permalink.isnot(None),
                adapter_keys != {},
            ),
        )
        # updated_at stays as it is: the scrub is not a status change (the planner's account
        # suspension looks at recent changes).
        .values(
            platform_post_id=None,
            permalink=None,
            publish_state=new_state,
            updated_at=ScheduledPost.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    return res.rowcount or 0


def _expire_youtube_activity(session: Session, days: int, now: datetime) -> int:
    """Old activity entries about YouTube: drop the stored link (it holds the video id) and the
    channel title in the text. Entries about disconnected accounts go with the account."""
    cutoff = now - timedelta(days=max(int(days), 1))
    res = session.execute(
        update(ActivityLog)
        .where(
            ActivityLog.created_at < cutoff,
            ActivityLog.data["platform"].astext == Platform.YOUTUBE.value,
            ActivityLog.data.has_key("permalink"),
        )
        .values(data=ActivityLog.data.op("-", return_type=JSONB)(cast("permalink", Text)))
        .execution_options(synchronize_session=False)
    )
    count = res.rowcount or 0
    accounts = session.execute(
        select(ConnectedAccount).where(
            ConnectedAccount.platform == Platform.YOUTUBE, ConnectedAccount.status != AccountStatus.DISABLED
        )
    ).scalars()
    for account in accounts:
        if account.display_name and account.display_name != GENERIC_CHANNEL_NAME:
            count += redact_name_in_activity(session, account, account.display_name, before=cutoff)
    return count


def _scrub_stale_youtube_channels(session: Session, days: int, now: datetime) -> int:
    """Replace the channel title and avatar of YouTube accounts whose data was not refreshed within
    `days` (engine.jobs refreshes them every 25 days; this covers failures and a missing
    youtube.refresh_account_info). The title is removed from the activity text too."""
    cutoff = now - timedelta(days=max(int(days), 1))
    accounts = session.execute(
        select(ConnectedAccount).where(
            ConnectedAccount.platform == Platform.YOUTUBE, ConnectedAccount.status != AccountStatus.DISABLED
        )
    ).scalars()
    count = 0
    for account in accounts:
        if channel_data_refreshed_at(account.extra, account.created_at) > cutoff:
            continue
        stale_name = account.display_name
        extra = {k: v for k, v in (account.extra or {}).items() if k != "custom_url"}
        if stale_name == GENERIC_CHANNEL_NAME and account.avatar_url is None and extra == account.extra:
            continue
        if stale_name and stale_name != GENERIC_CHANNEL_NAME:
            redact_name_in_activity(session, account, stale_name)
        account.display_name = GENERIC_CHANNEL_NAME
        account.avatar_url = None
        account.extra = extra
        count += 1
    return count


def _delete_disconnected_accounts(session: Session, now: datetime) -> int:
    """Delete accounts disconnected more than DISCONNECTED_RETENTION ago, with their post history
    and the activity entries that name them (imported library videos stay). OAuth tokens were
    already wiped at the disconnect itself."""
    cutoff = (now if now.tzinfo else now.replace(tzinfo=UTC)) - DISCONNECTED_RETENTION
    accounts = session.execute(
        select(ConnectedAccount).where(ConnectedAccount.status == AccountStatus.DISABLED)
    ).scalars()
    count = 0
    for account in list(accounts):
        since = disconnected_at(account)
        if since is not None and since <= cutoff:
            delete_account_records(session, account)
            count += 1
    return count
