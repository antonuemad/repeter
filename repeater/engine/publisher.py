"""Publishing pipeline: publish_post and poll_post (PLAN.md 4.2 step 6 and 4.3).

Both functions lock the post row, re-check that publishing is still wanted and commit their own
progress. publish_post commits status UPLOADING before any network call, so a crash mid-upload
leaves the post in UPLOADING (maintenance turns it into needs-attention) instead of uploading
the same video twice.

Outcomes of publish_post:
    PUBLISHED                    -> published fields, INFO activity
    PROCESSING                   -> publish_state saved, poll_post queued after poll_after_seconds
    RetryableError / RateLimited -> attempts += 1, FAILED, retried after max(retry_after, 5/30/120 min);
    / unexpected exception          the 4th failure (3 retries) becomes NEEDS_ATTENTION + notification
    AuthError                    -> account marked needs-reconnect, post NEEDS_ATTENTION + notification
    PermanentError / NotSupported-> NEEDS_ATTENTION + notification
poll_post keeps polling while the adapter answers PROCESSING (or fails transiently) and gives up
with NEEDS_ATTENTION two hours after the upload. RestartPublish from poll means the platform
dropped the upload without publishing it: the post goes back to publish_post as a failed attempt.

Follow-ups: a running job blocks a new job with the same dedupe key, so when the follow-up is the
job that is running right now (publish_post retrying itself, poll_post polling again) the result
carries `retry_in` and the job handler raises RetryLater; otherwise the job is queued here.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from repeater import notify as notify_mod
from repeater.accounts import AccountUnavailable, fresh_context, mark_needs_reconnect
from repeater.activity import log_activity
from repeater.crypto import signed_media_url
from repeater.engine import captions, rules
from repeater.jobs import kinds
from repeater.media import storage
from repeater.models import (
    AccountStatus,
    ActivityLevel,
    ConnectedAccount,
    Job,
    JobStatus,
    Platform,
    PostStatus,
    RotationRule,
    ScheduledPost,
    Video,
    VideoStatus,
    Workspace,
)
from repeater.platforms import get_publisher
from repeater.platforms.base import (
    AuthError,
    NotSupportedError,
    PermanentError,
    PlatformError,
    PublishOutcome,
    PublishRequest,
    PublishResult,
    RestartPublish,
    RetryableError,
)
from repeater.timeutil import ensure_aware, utcnow

log = logging.getLogger("repeater.engine.publisher")

RETRY_BACKOFF_MINUTES = (5, 30, 120)
MAX_RETRIES = len(RETRY_BACKOFF_MINUTES)
MAX_RETRY_AFTER_SECONDS = 48 * 3600
POLL_TIMEOUT = timedelta(hours=2)
POLL_ERROR_DELAY_SECONDS = 60
MIN_POLL_DELAY_SECONDS = 5
PAUSED_POLL_DELAY_SECONDS = 10 * 60
WAIT_FOR_VIDEO_SECONDS = 15 * 60
MEDIA_URL_TTL_SECONDS = 24 * 3600
# A job that fires a little before scheduled_at (clock skew) still publishes.
EARLY_TOLERANCE = timedelta(seconds=30)
# Planner posts that missed their slot by this much (worker down) are re-planned instead of
# being published in a burst that would break the gaps between posts.
STALE_SLOT = timedelta(hours=3)
ENGINE_STATE_KEY = "_engine"
PUBLISHABLE = (PostStatus.SCHEDULED, PostStatus.FAILED)


@dataclass(frozen=True)
class StepResult:
    status: PostStatus | None
    # Seconds after which the calling job should run again (see module docstring).
    retry_in: float | None = None
    message: str = ""


@dataclass
class _Target:
    post: ScheduledPost
    video: Video
    account: ConnectedAccount
    rule: RotationRule | None
    path: Path

    @property
    def platform(self) -> Platform:
        return Platform(self.account.platform)


# =========================================================================== publish_post


def publish_post(session: Session, post_id: int, *, now: datetime | None = None) -> StepResult:
    """Publish one scheduled post. Commits its own progress (see module docstring)."""
    now = ensure_aware(now) or utcnow()
    post = _lock_post(session, post_id)
    if post is None:
        session.rollback()
        return StepResult(None, message="post not found")
    if post.status not in PUBLISHABLE:
        session.commit()
        return StepResult(post.status, message=f"post is {post.status}")
    scheduled_at = ensure_aware(post.scheduled_at)
    if scheduled_at > now + EARLY_TOLERANCE:
        return _follow_up(session, post, kinds.PUBLISH_POST, scheduled_at, now, "not due yet")
    target = _publish_target(session, post, now)
    if isinstance(target, StepResult):
        return target
    try:
        ctx = fresh_context(session, target.account, now=now)
    except AccountUnavailable:
        return _needs_attention(session, target, _reconnect_message(target.account))
    except Exception as exc:  # noqa: BLE001 - token refresh failures are retried like publish errors
        return _publish_error(session, target, exc, now)
    try:
        req = _build_request(target)
        adapter = get_publisher(target.platform)
        problems = adapter.validate(ctx, req)
    except Exception as exc:  # noqa: BLE001 - classified below
        return _publish_error(session, target, exc, now)
    if problems:
        return _needs_attention(session, target, " ".join(p.strip() for p in problems if p.strip()))
    post.status = PostStatus.UPLOADING
    post.caption = req.caption
    post.error = None
    session.commit()
    try:
        result = adapter.publish(ctx, req)
    except Exception as exc:  # noqa: BLE001 - classified below
        _relock(session, post)
        return _publish_error(session, target, exc, now)
    _relock(session, post)
    return _apply_result(session, target, result, now, polling=False)


def _publish_target(session: Session, post: ScheduledPost, now: datetime) -> _Target | StepResult:
    """Everything needed to publish, or the StepResult of cancelling/deferring the post."""
    workspace = session.get(Workspace, post.workspace_id)
    if workspace is None or workspace.paused:
        return _cancel(
            session, post, "Publishing is paused for this workspace; it will be re-planned on resume."
        )
    account = session.get(ConnectedAccount, post.connected_account_id)
    if account is None or account.status == AccountStatus.DISABLED:
        return _cancel(session, post, "The account was disconnected.")
    if account.status != AccountStatus.ACTIVE:
        return _cancel(
            session, post, "The account must be reconnected; the post will be re-planned after that."
        )
    rule = rules.find_rule(session, account)
    if post.created_by != "user" and (rule is None or not rule.enabled):
        return _cancel(session, post, "Rotation is turned off for this account.")
    video = session.get(Video, post.video_id)
    if video is None:
        return _cancel(session, post, "The video no longer exists.")
    if video.status in (VideoStatus.IMPORTING, VideoStatus.PROCESSING):
        run_at = now + timedelta(seconds=WAIT_FOR_VIDEO_SECONDS)
        post.scheduled_at = run_at
        return _follow_up(session, post, kinds.PUBLISH_POST, run_at, now, "video is still processing")
    if video.status != VideoStatus.READY:
        return _cancel(session, post, f"The video is {video.status} and is no longer in rotation.")
    if (
        post.status == PostStatus.SCHEDULED
        and post.created_by != "user"
        and now - ensure_aware(post.scheduled_at) > STALE_SLOT
    ):
        return _cancel(session, post, "The post missed its time slot; it will be re-planned.")
    target = _Target(post, video, account, rule, Path())
    path = _encoded_file(video)
    if path is None:
        return _needs_attention(
            session, target, "The encoded video file is missing on the server; re-process the video."
        )
    target.path = path
    return target


def _encoded_file(video: Video) -> Path | None:
    try:
        path = storage.abs_path(video.encoded_path)
    except ValueError:
        return None
    return path if path is not None and path.is_file() else None


def _build_request(target: _Target) -> PublishRequest:
    video, platform = target.video, target.platform
    title, caption = captions.render(target.rule, video, platform)
    settings = dict(target.rule.settings or {}) if target.rule else rules.default_settings(platform)
    try:
        thumbnail = storage.abs_path(video.thumbnail_path)
    except ValueError:
        thumbnail = None
    return PublishRequest(
        post_id=target.post.id,
        video_path=target.path,
        public_url=signed_media_url(video.id, ttl_seconds=MEDIA_URL_TTL_SECONDS),
        size_bytes=int(video.encoded_size_bytes or target.path.stat().st_size),
        duration_s=float(video.duration_s or 0.0),
        width=int(video.width or 0),
        height=int(video.height or 0),
        title=title,
        caption=caption,
        settings=settings,
        thumbnail_path=thumbnail if thumbnail is not None and thumbnail.is_file() else None,
        fps=float(video.fps) if video.fps else None,
    )


def _publish_error(session: Session, target: _Target, exc: Exception, now: datetime) -> StepResult:
    if isinstance(exc, AuthError):
        mark_needs_reconnect(session, target.account, exc.message)
        return _needs_attention(
            session, target, f"{exc.message} {_reconnect_message(target.account)}".strip()
        )
    if isinstance(exc, PermanentError | NotSupportedError):
        return _needs_attention(session, target, exc.message)
    retry_after = exc.retry_after if isinstance(exc, RetryableError) else None
    if isinstance(exc, PlatformError):
        message = exc.message
    else:
        _log_unexpected(exc, f"publishing post {target.post.id}")
        message = f"Unexpected error ({type(exc).__name__})."
    return _retry_or_give_up(session, target, message, retry_after, now)


def _retry_or_give_up(
    session: Session, target: _Target, message: str, retry_after: float | None, now: datetime
) -> StepResult:
    post = target.post
    post.attempts = (post.attempts or 0) + 1
    if post.attempts > MAX_RETRIES:
        return _needs_attention(session, target, f"{message} (gave up after {post.attempts} attempts)")
    backoff = RETRY_BACKOFF_MINUTES[post.attempts - 1] * 60
    delay = max(float(backoff), min(float(retry_after or 0), MAX_RETRY_AFTER_SECONDS))
    run_at = now + timedelta(seconds=delay)
    post.status = PostStatus.FAILED
    post.scheduled_at = run_at
    post.error = f"Attempt {post.attempts} failed: {message} Retrying in {_human(delay)}."[:2000]
    log_activity(
        session,
        post.workspace_id,
        ActivityLevel.WARNING,
        "publish",
        f"Publishing '{captions.video_label(target.video)}' to {_where(target)} failed; "
        f"retry {post.attempts} of {MAX_RETRIES} in {_human(delay)}: {message}",
        **_ids(target),
    )
    return _follow_up(session, post, kinds.PUBLISH_POST, run_at, now, message)


# =========================================================================== poll_post


def poll_post(session: Session, post_id: int, *, now: datetime | None = None) -> StepResult:
    """Check a PROCESSING post with the platform. Commits its own progress.

    Unlike publish_post, the video's status is not re-checked: the platform already has the
    upload, and archiving a video only stops posts that have not been uploaded yet."""
    now = ensure_aware(now) or utcnow()
    post = _lock_post(session, post_id)
    if post is None:
        session.rollback()
        return StepResult(None, message="post not found")
    if post.status != PostStatus.PROCESSING:
        session.commit()
        return StepResult(post.status, message=f"post is {post.status}")
    account = session.get(ConnectedAccount, post.connected_account_id)
    video = session.get(Video, post.video_id)
    if account is None or video is None or account.status == AccountStatus.DISABLED:
        return _cancel(
            session, post, "The account was disconnected while the platform was processing the upload."
        )
    target = _Target(post, video, account, rules.find_rule(session, account), Path())
    if now - _processing_since(post, now) > POLL_TIMEOUT:
        return _needs_attention(
            session,
            target,
            f"{captions.platform_name(target.platform)} did not confirm the post within 2 hours of the "
            "upload. Check the account before publishing again.",
        )
    if _publishing_halted(session, target):
        run_at = now + timedelta(seconds=PAUSED_POLL_DELAY_SECONDS)
        return _follow_up(session, post, kinds.POLL_POST, run_at, now, "publishing paused")
    try:
        ctx = fresh_context(session, account, now=now)
    except AccountUnavailable:
        return _needs_attention(session, target, _reconnect_message(account))
    except Exception as exc:  # noqa: BLE001 - handled like a poll error
        return _poll_error(session, target, exc, now)
    state = _adapter_state(post.publish_state)
    try:
        result = get_publisher(target.platform).poll(ctx, state)
    except RestartPublish as exc:
        return _restart_publish(session, target, exc, now)
    except Exception as exc:  # noqa: BLE001 - classified below
        # Adapters record progress in `state` before raising (e.g. Instagram's publish attempts).
        post.publish_state = _with_engine_state(post, state)
        return _poll_error(session, target, exc, now)
    return _apply_result(session, target, result, now, polling=True)


def _publishing_halted(session: Session, target: _Target) -> bool:
    workspace = session.get(Workspace, target.post.workspace_id)
    if workspace is None or workspace.paused:
        return True
    rule = target.rule
    return target.post.created_by != "user" and (rule is None or not rule.enabled)


def _poll_error(session: Session, target: _Target, exc: Exception, now: datetime) -> StepResult:
    if isinstance(exc, AuthError):
        mark_needs_reconnect(session, target.account, exc.message)
        return _needs_attention(
            session, target, f"{exc.message} {_reconnect_message(target.account)}".strip()
        )
    if isinstance(exc, PermanentError | NotSupportedError):
        return _needs_attention(session, target, exc.message)
    if isinstance(exc, PlatformError):
        message = exc.message
    else:
        _log_unexpected(exc, f"polling post {target.post.id}")
        message = f"Unexpected error ({type(exc).__name__})."
    retry_after = exc.retry_after if isinstance(exc, RetryableError) else None
    delay = max(float(POLL_ERROR_DELAY_SECONDS), min(float(retry_after or 0), MAX_RETRY_AFTER_SECONDS))
    target.post.error = f"Checking the upload failed: {message} Checking again in {_human(delay)}."[:2000]
    run_at = now + timedelta(seconds=delay)
    return _follow_up(session, target.post, kinds.POLL_POST, run_at, now, message)


def _restart_publish(session: Session, target: _Target, exc: RestartPublish, now: datetime) -> StepResult:
    """Nothing was published and the platform-side upload is gone: upload again later. This counts
    as a failed attempt, so a platform that keeps dropping uploads ends in NEEDS_ATTENTION."""
    post = target.post
    post.publish_state = {}
    post.platform_post_id = None
    post.permalink = None
    return _retry_or_give_up(session, target, exc.message, exc.retry_after, now)


def _with_engine_state(post: ScheduledPost, adapter_state: dict[str, Any]) -> dict[str, Any]:
    merged = dict(adapter_state)
    engine = (post.publish_state or {}).get(ENGINE_STATE_KEY)
    if engine is not None:
        merged[ENGINE_STATE_KEY] = engine
    return merged


def _processing_since(post: ScheduledPost, now: datetime) -> datetime:
    engine = (post.publish_state or {}).get(ENGINE_STATE_KEY) or {}
    raw = engine.get("processing_since") if isinstance(engine, dict) else None
    try:
        return ensure_aware(datetime.fromisoformat(raw)) if raw else ensure_aware(post.updated_at) or now
    except (TypeError, ValueError):
        return ensure_aware(post.updated_at) or now


def _adapter_state(state: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (state or {}).items() if k != ENGINE_STATE_KEY}


# =========================================================================== results


def _apply_result(
    session: Session, target: _Target, result: PublishResult, now: datetime, *, polling: bool
) -> StepResult:
    post = target.post
    if result.outcome == PublishOutcome.PUBLISHED:
        post.status = PostStatus.PUBLISHED
        post.platform_post_id = result.platform_post_id or post.platform_post_id
        post.permalink = result.permalink or post.permalink
        post.published_at = now
        post.error = None
        post.publish_state = _adapter_state(result.state) or _adapter_state(post.publish_state)
        note = _publish_note(target.platform, result.message)
        log_activity(
            session,
            post.workspace_id,
            ActivityLevel.INFO,
            "publish",
            f"Published '{captions.video_label(target.video)}' to {_where(target)}."
            + (f" {note}" if note else ""),
            permalink=post.permalink,
            **_ids(target),
        )
        session.commit()
        return StepResult(PostStatus.PUBLISHED)
    if result.outcome == PublishOutcome.PROCESSING:
        since = _processing_since(post, now) if polling else now
        post.status = PostStatus.PROCESSING
        post.platform_post_id = result.platform_post_id or post.platform_post_id
        post.permalink = result.permalink or post.permalink
        post.publish_state = {
            **_adapter_state(result.state),
            ENGINE_STATE_KEY: {"processing_since": since.isoformat()},
        }
        if polling:
            post.error = None
        delay = max(int(result.poll_after_seconds or 0), MIN_POLL_DELAY_SECONDS)
        return _follow_up(
            session,
            post,
            kinds.POLL_POST,
            now + timedelta(seconds=delay),
            now,
            result.message or "processing",
        )
    # The upload may already be live, so retrying could post the video twice.
    return _needs_attention(
        session,
        target,
        f"{captions.platform_name(target.platform)} returned an unexpected answer; check the account "
        "before publishing again.",
    )


def _needs_attention(session: Session, target: _Target, message: str) -> StepResult:
    post = target.post
    post.status = PostStatus.NEEDS_ATTENTION
    post.error = message[:2000]
    text = f"Could not publish '{captions.video_label(target.video)}' to {_where(target)}: {message}"
    log_activity(session, post.workspace_id, ActivityLevel.ERROR, "publish", text, **_ids(target))
    session.commit()
    notify_mod.notify(text, workspace_id=post.workspace_id, level="error")
    return StepResult(PostStatus.NEEDS_ATTENTION, message=message)


def _cancel(session: Session, post: ScheduledPost, reason: str) -> StepResult:
    post.status = PostStatus.CANCELED
    post.error = reason[:2000]
    log_activity(
        session,
        post.workspace_id,
        ActivityLevel.INFO,
        "publish",
        f"Canceled post #{post.id}: {reason}",
        post_id=post.id,
        video_id=post.video_id,
        account_id=post.connected_account_id,
        platform=str(post.platform),
    )
    session.commit()
    return StepResult(PostStatus.CANCELED, message=reason)


# =========================================================================== jobs


def queue_job(session: Session, kind: str, post_id: int, run_at: datetime) -> None:
    """Queue `kind` for the post at run_at, moving an already queued job to run_at."""
    enqueue = kinds.enqueue_poll_post if kind == kinds.POLL_POST else kinds.enqueue_publish_post
    if enqueue(session, post_id, run_at=run_at) is None:
        session.execute(
            update(Job)
            .where(Job.dedupe_key == f"{kind}:{post_id}", Job.status == JobStatus.QUEUED)
            .values(run_at=run_at)
        )


def queue_publish(session: Session, post_id: int, run_at: datetime) -> None:
    queue_job(session, kinds.PUBLISH_POST, post_id, run_at)


def _follow_up(
    session: Session, post: ScheduledPost, kind: str, run_at: datetime, now: datetime, message: str
) -> StepResult:
    delay = max(0.0, (run_at - now).total_seconds())
    if _job_running(session, kind, post.id):
        session.commit()
        return StepResult(post.status, retry_in=delay, message=message)
    queue_job(session, kind, post.id, run_at)
    session.commit()
    return StepResult(post.status, message=message)


def _job_running(session: Session, kind: str, post_id: int) -> bool:
    return (
        session.execute(
            select(Job.id)
            .where(Job.dedupe_key == f"{kind}:{post_id}", Job.status == JobStatus.RUNNING)
            .limit(1)
        ).scalar()
        is not None
    )


# =========================================================================== helpers


def _lock_post(session: Session, post_id: int) -> ScheduledPost | None:
    return session.get(ScheduledPost, post_id, with_for_update=True, populate_existing=True)


def _relock(session: Session, post: ScheduledPost) -> None:
    session.get(ScheduledPost, post.id, with_for_update=True, populate_existing=True)


def _publish_note(platform: Platform, message: str | None) -> str:
    """The adapter's remark about a finished publish (e.g. YouTube kept the video private, or a
    TikTok draft waits in the inbox), without its plain "Published on <platform>." opener."""
    note = (message or "").strip()
    opener = f"Published on {captions.platform_name(platform)}."
    if note.startswith(opener):
        note = note[len(opener) :].strip()
    return note


def _reconnect_message(account: ConnectedAccount) -> str:
    return (
        f"Reconnect the {captions.platform_name(account.platform)} account '{account.display_name}', "
        "then publish again."
    )


def _where(target: _Target) -> str:
    return f"{captions.platform_name(target.platform)} ({target.account.display_name})"


def _ids(target: _Target) -> dict[str, Any]:
    return {
        "post_id": target.post.id,
        "video_id": target.video.id,
        "account_id": target.account.id,
        "platform": target.platform.value,
    }


def _human(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return f"{int(seconds)} seconds"
    if minutes < 120:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{minutes / 60:g} hours" if minutes % 60 == 0 else f"{minutes / 60:.1f} hours"


def _log_unexpected(exc: BaseException, context: str) -> None:
    """Log a bug with its stack but without the exception text, which may embed URLs."""
    frames = "".join(traceback.format_tb(exc.__traceback__))
    log.error("%s failed with %s\n%s", context, type(exc).__name__, frames)
