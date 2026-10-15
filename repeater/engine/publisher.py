"""Publishing pipeline: publish_post and poll_post (PLAN.md 4.2 step 6 and 4.3).

Both functions lock the post row, re-check that publishing is still wanted and commit their own
progress. publish_post commits status UPLOADING before any network call, so a crash mid-upload
leaves the post in UPLOADING (maintenance turns it into needs-attention) instead of uploading
the same video twice.

Outcomes of publish_post:
    PUBLISHED                    -> published fields, INFO activity
    PROCESSING                   -> publish_state saved, poll_post queued after poll_after_seconds
    UnknownOutcomeError          -> bytes may have reached the platform. With adapter state (a
                                    resumable session URI, a publish id ...): the state is saved,
                                    status PROCESSING and poll_post queued, so poll() asks the
                                    platform what happened. Without state: NEEDS_ATTENTION ("may
                                    be live"). Never a blind retry.
    RateLimitedError             -> not a failed attempt: the post is moved to a later, jittered
                                    time that keeps the account's min gap (attempts unchanged)
    RetryableError / pre-upload  -> attempts += 1, FAILED, retried after max(retry_after, 5/30/120
    unexpected exception            min); the 4th failure (3 retries) becomes NEEDS_ATTENTION + alert
    unexpected exception after   -> NEEDS_ATTENTION ("may be live"); the upload may have gone
    the UPLOADING commit            through, so it is not retried
    AuthError                    -> account marked needs-reconnect, post NEEDS_ATTENTION + alert
    PermanentError / NotSupported-> NEEDS_ATTENTION + alert
poll_post keeps polling while the adapter answers PROCESSING (or fails transiently) and gives up
with NEEDS_ATTENTION when a poll still answers PROCESSING two hours after the upload (never
without having asked the platform: a halted workspace or a worker outage does not count, and a
rate-limited or failing status check only gives up after POLL_ERROR_CEILING). RestartPublish from
poll means the platform dropped the upload without publishing it: the post goes back to
publish_post as a failed attempt.

Every PublishRequest carries the post's earlier adapter state (req.state).

Follow-ups: a running job blocks a new job with the same dedupe key, so when the follow-up is the
job that is running right now (publish_post retrying itself, poll_post polling again) the result
carries `retry_in` and the job handler raises RetryLater; otherwise the job is queued here (a
second job under a ":next" key when another run of the same kind is still running).
"""

from __future__ import annotations

import copy
import logging
import re
import traceback
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from repeater import notify as notify_mod
from repeater import queue
from repeater.accounts import (
    DISCONNECTED_WHILE_PUBLISHING,
    AccountUnavailable,
    fresh_context,
    mark_needs_reconnect,
)
from repeater.activity import log_activity
from repeater.config import get_settings
from repeater.crypto import signed_media_url
from repeater.engine import captions, rules
from repeater.jobs import kinds
from repeater.media import storage
from repeater.messages import Message, diagnostic
from repeater.models import (
    ACTIVE_POST_STATUSES,
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
    RateLimitedError,
    RestartPublish,
    RetryableError,
    UnknownOutcomeError,
)
from repeater.timeutil import ensure_aware, utcnow

log = logging.getLogger("repeater.engine.publisher")

RETRY_BACKOFF_MINUTES = (5, 30, 120)
MAX_RETRIES = len(RETRY_BACKOFF_MINUTES)
MAX_RETRY_AFTER_SECONDS = 48 * 3600
# A poll that still answers PROCESSING this long after the upload ends the wait.
POLL_TIMEOUT = timedelta(hours=2)
# Status checks that keep failing (or being rate limited) for this long end the wait too.
POLL_ERROR_CEILING = timedelta(hours=48)
POLL_ERROR_DELAY_SECONDS = 60
POLL_JITTER_SECONDS = 10 * 60
MIN_POLL_DELAY_SECONDS = 5
UNKNOWN_OUTCOME_POLL_SECONDS = 30
PAUSED_POLL_DELAY_SECONDS = 10 * 60
WAIT_FOR_VIDEO_SECONDS = 15 * 60
MEDIA_URL_TTL_SECONDS = 24 * 3600
# Rate limits are not failed attempts. The post is tried again after at least this long, plus a
# deterministic per-post jitter so posts that hit the same (project-wide) limit do not all return
# at the same second, and never closer than the rule's min gap to the account's other posts.
RATE_LIMIT_MIN_DELAY_SECONDS = 5 * 60
RATE_LIMIT_JITTER_SECONDS = 30 * 60
RATE_LIMIT_GIVE_UP = timedelta(days=3)
MIN_GAP_LOOKAHEAD = timedelta(days=7)
# Platforms whose poll() can make the post go live: Instagram publishes its FINISHED container;
# Facebook can send a deferred finish. A paused workspace or disabled rule must not poll them.
# Polling the other platforms only reads status and continues while publishing is halted.
PUBLISH_ON_POLL = frozenset({Platform.INSTAGRAM, Platform.FACEBOOK})
# A job that fires a little before scheduled_at (clock skew) still publishes.
EARLY_TOLERANCE = timedelta(seconds=30)
# Planner posts that missed their slot by this much (worker down) are re-planned instead of
# being published in a burst that would break the gaps between posts.
STALE_SLOT = timedelta(hours=3)
ENGINE_STATE_KEY = "_engine"
# PermanentError codes from poll() that mean "the request may have been carried out" (Instagram:
# media_publish never got a clear answer).
UNCONFIRMED_OUTCOME_CODES = frozenset({"publish_unconfirmed"})
PUBLISHABLE = (PostStatus.SCHEDULED, PostStatus.FAILED)

_URL_RE = re.compile(r"https?://\S+")


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


# =========================================================================== post state


def adapter_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """The part of scheduled_posts.publish_state that belongs to the platform adapter."""
    return {k: v for k, v in (state or {}).items() if k != ENGINE_STATE_KEY}


def engine_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """The engine's own bookkeeping inside publish_state (processing_since, may_be_live, ...)."""
    raw = (state or {}).get(ENGINE_STATE_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _with_engine(adapter: dict[str, Any], engine: dict[str, Any]) -> dict[str, Any]:
    merged = dict(adapter)
    if engine:
        merged[ENGINE_STATE_KEY] = engine
    return merged


def mark_may_be_live(post: ScheduledPost) -> None:
    """Remember that the video may already be on the platform although the post did not end as
    published: the planner then counts it for gaps, caps and quotas and never re-uses its cycle."""
    state = post.publish_state
    post.publish_state = _with_engine(adapter_state(state), {**engine_state(state), "may_be_live": True})


def reached_platform(platform_post_id: str | None, state: dict[str, Any] | None) -> bool:
    """True when the post got as far as the platform: it has a platform id, adapter state, or the
    engine recorded the upload (processing) or flagged it as possibly live."""
    if platform_post_id or adapter_state(state):
        return True
    engine = engine_state(state)
    return bool(engine.get("processing_since") or engine.get("may_be_live"))


def _parse_dt(raw: Any) -> datetime | None:
    try:
        return ensure_aware(datetime.fromisoformat(raw)) if raw else None
    except (TypeError, ValueError):
        return None


def _scrub(text: str) -> str:
    """Adapter messages may quote request URLs (upload URLs carry credentials): drop them."""

    def clean(value):
        return _URL_RE.sub("<url>", value or "")

    return text.remap(clean) if isinstance(text, Message) else clean(text)


def _jitter_seconds(post_id: int, span: int) -> int:
    return zlib.crc32(f"retry:{post_id}".encode()) % max(int(span), 1)


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
        return _needs_attention(
            session,
            target,
            diagnostic(" ".join(p.strip() for p in problems if p.strip()), "publish.validation"),
        )
    post.status = PostStatus.UPLOADING
    post.caption = req.caption
    post.error = None
    session.commit()
    try:
        result = adapter.publish(ctx, req)
    except Exception as exc:  # noqa: BLE001 - classified below
        _relock(session, post)
        return _publish_error(session, target, exc, now, uploading=True)
    _relock(session, post)
    return _apply_result(session, target, result, now, polling=False)


def _publish_target(session: Session, post: ScheduledPost, now: datetime) -> _Target | StepResult:
    """Everything needed to publish, or the StepResult of cancelling/deferring the post."""
    if not get_settings().publishing_enabled:
        return _cancel(
            session,
            post,
            Message("publish.testing_lock", "Publishing is disabled on this server during testing.", {}),
        )
    workspace = session.get(Workspace, post.workspace_id)
    if workspace is None or workspace.paused:
        return _cancel(
            session,
            post,
            Message(
                "publish.paused",
                "Publishing is paused for this workspace; it will be re-planned on resume.",
                {},
            ),
        )
    account = session.get(ConnectedAccount, post.connected_account_id)
    if account is None or account.status == AccountStatus.DISABLED:
        return _cancel(session, post, Message("publish.disconnected", "The account was disconnected.", {}))
    if account.status != AccountStatus.ACTIVE:
        return _cancel(
            session,
            post,
            Message(
                "publish.reconnect_first",
                "The account must be reconnected; the post will be re-planned after that.",
                {},
            ),
        )
    rule = rules.find_rule(session, account)
    if post.created_by != "user" and (rule is None or not rule.enabled):
        return _cancel(
            session, post, Message("publish.rotation_off", "Rotation is turned off for this account.", {})
        )
    video = session.get(Video, post.video_id)
    if video is None:
        return _cancel(session, post, Message("publish.video_gone", "The video no longer exists.", {}))
    if video.profile_id is not None or account.profile_id is not None:
        return _cancel(
            session,
            post,
            Message("plan.draft_only", "Person plans are preview-only; publishing is unavailable.", {}),
        )
    if video.status in (VideoStatus.IMPORTING, VideoStatus.PROCESSING):
        run_at = now + timedelta(seconds=WAIT_FOR_VIDEO_SECONDS)
        post.scheduled_at = run_at
        return _follow_up(session, post, kinds.PUBLISH_POST, run_at, now, "video is still processing")
    if video.status != VideoStatus.READY:
        return _cancel(
            session,
            post,
            Message(
                "publish.video_not_ready",
                f"The video is {video.status} and is no longer in rotation.",
                {"p0": video.status},
            ),
        )
    duplicate_on = _duplicate_origin(session, video, account)
    if duplicate_on is not None:
        return _cancel(
            session,
            post,
            Message(
                "publish.duplicate",
                f"The same video already exists on {duplicate_on}; the post was canceled.",
                {"p0": duplicate_on},
            ),
        )
    if (
        post.status == PostStatus.SCHEDULED
        and post.created_by != "user"
        and now - ensure_aware(post.scheduled_at) > STALE_SLOT
    ):
        return _cancel(
            session,
            post,
            Message("publish.missed_slot", "The post missed its time slot; it will be re-planned.", {}),
        )
    target = _Target(post, video, account, rule, Path())
    path = _encoded_file(video)
    if path is None:
        return _needs_attention(
            session,
            target,
            Message(
                "publish.encoded_missing",
                "The encoded video file is missing on the server; re-process the video.",
                {},
            ),
        )
    target.path = path
    return target


def _duplicate_origin(session: Session, video: Video, account: ConnectedAccount) -> str | None:
    """Name of the platform where a duplicate of the video was found on this very platform or
    account (the importer found the same content there after the post was planned)."""
    rows = session.execute(
        select(Video.source_platform, Video.source_account_id).where(
            Video.duplicate_of_id == video.id, Video.status == VideoStatus.DUPLICATE
        )
    ).all()
    for platform, account_id in rows:
        if (platform is not None and Platform(platform) == Platform(account.platform)) or (
            account_id is not None and int(account_id) == account.id
        ):
            return captions.platform_name(account.platform)
    return None


def _encoded_file(video: Video) -> Path | None:
    try:
        path = storage.abs_path(video.encoded_path)
    except ValueError:
        return None
    return path if path is not None and path.is_file() else None


def _build_request(target: _Target) -> PublishRequest:
    from repeater.upload_quota import reserve_start

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
        state=copy.deepcopy(adapter_state(target.post.publish_state)),
        on_session_start=((lambda: reserve_start(target.post.id)) if platform == Platform.YOUTUBE else None),
    )


def _publish_error(
    session: Session, target: _Target, exc: Exception, now: datetime, *, uploading: bool = False
) -> StepResult:
    """Classify an exception from publishing. `uploading` is True once the post was committed as
    UPLOADING, i.e. the adapter may have sent bytes: then nothing is retried blindly."""
    if isinstance(exc, UnknownOutcomeError):
        return _unknown_outcome(session, target, exc, now)
    if isinstance(exc, AuthError):
        mark_needs_reconnect(session, target.account, exc.message)
        return _needs_attention(
            session, target, f"{_scrub(exc.message)} {_reconnect_message(target.account)}".strip()
        )
    if isinstance(exc, PermanentError | NotSupportedError):
        return _needs_attention(session, target, _scrub(exc.message))
    if isinstance(exc, RateLimitedError):
        return _rate_limited(session, target, exc, now)
    retry_after = exc.retry_after if isinstance(exc, RetryableError) else None
    if isinstance(exc, PlatformError):
        message = _scrub(exc.message)
    else:
        _log_unexpected(exc, f"publishing post {target.post.id}")
        if uploading:
            return _needs_attention(
                session,
                target,
                Message(
                    "publish.unexpected_upload",
                    f"Unexpected error ({type(exc).__name__}) while uploading to "
                    f"{captions.platform_name(target.platform)}. The video may be live; check the account "
                    "before publishing again.",
                    {"p0": type(exc).__name__, "p1": captions.platform_name(target.platform)},
                ),
                may_be_live=True,
            )
        message = Message(
            "publish.unexpected", f"Unexpected error ({type(exc).__name__}).", {"p0": type(exc).__name__}
        )
    return _retry_or_give_up(session, target, message, retry_after, now)


def _unknown_outcome(
    session: Session, target: _Target, exc: UnknownOutcomeError, now: datetime
) -> StepResult:
    """The upload may have reached the platform. Re-publishing could post the video twice, so the
    adapter's state is saved and poll() finds out; without state there is nothing to poll."""
    post = target.post
    name = captions.platform_name(target.platform)
    message = _scrub(exc.message)[:300]
    state = adapter_state(exc.state)
    if not state:
        return _needs_attention(
            session,
            target,
            Message(
                "publish.unconfirmed",
                f"{name} did not confirm the upload ({message}). The video may be live; check the "
                "account before publishing again.",
                {"p0": name, "p1": diagnostic(message)},
            ),
            may_be_live=True,
        )
    post.status = PostStatus.PROCESSING
    post.publish_state = _with_engine(state, {"processing_since": now.isoformat()})
    post.error = None
    log_activity(
        session,
        post.workspace_id,
        ActivityLevel.WARNING,
        "publish",
        Message(
            "publish.checking_lost_answer",
            f"Uploading '{captions.video_label(target.video)}' to {_where(target)}: the answer was lost "
            f"({message}); checking with {name} whether it went through.",
            {
                "p0": captions.video_label(target.video),
                "p1": _where(target),
                "p2": diagnostic(message),
                "p3": name,
            },
        ),
        **_ids(target),
    )
    run_at = now + timedelta(seconds=UNKNOWN_OUTCOME_POLL_SECONDS)
    return _follow_up(session, post, kinds.POLL_POST, run_at, now, message)


def _record_quota_exhaustion(target: _Target, exc: Exception) -> None:
    from repeater.upload_quota import EXHAUSTED_CODES, mark_exhausted

    if (
        target.platform == Platform.YOUTUBE
        and isinstance(exc, RateLimitedError)
        and exc.code in EXHAUSTED_CODES
    ):
        mark_exhausted()


def _rate_limited(session: Session, target: _Target, exc: RateLimitedError, now: datetime) -> StepResult:
    """A rate limit or quota error is not a failed attempt (the platform refused the request
    before doing anything). The post moves to a later, jittered time that keeps the account's min
    gap; attempts and the retry ladder stay untouched."""
    _record_quota_exhaustion(target, exc)
    post = target.post
    message = _scrub(exc.message)
    engine = engine_state(post.publish_state)
    since = _parse_dt(engine.get("rate_limited_since")) or now
    if now - since > RATE_LIMIT_GIVE_UP:
        return _needs_attention(
            session,
            target,
            Message(
                "publish.rate_giveup",
                f"{message} ({captions.platform_name(target.platform)} kept limiting uploads for "
                f"{RATE_LIMIT_GIVE_UP.days} days.)",
                {
                    "p0": diagnostic(message),
                    "p1": captions.platform_name(target.platform),
                    "p2": RATE_LIMIT_GIVE_UP.days,
                },
            ),
        )
    delay = max(
        float(RATE_LIMIT_MIN_DELAY_SECONDS), min(float(exc.retry_after or 0), MAX_RETRY_AFTER_SECONDS)
    )
    run_at = now + timedelta(seconds=delay + _jitter_seconds(post.id, RATE_LIMIT_JITTER_SECONDS))
    run_at = _keep_min_gap(session, target, run_at)
    engine["rate_limited_since"] = since.isoformat()
    post.publish_state = _with_engine(adapter_state(post.publish_state), engine)
    post.status = PostStatus.FAILED if (post.attempts or 0) > 0 else PostStatus.SCHEDULED
    post.scheduled_at = run_at
    wait = (run_at - now).total_seconds()
    post.error = Message(
        "publish.rate_wait",
        f"Rate limited: {message} Trying again in {_human(wait)}.",
        {"p0": diagnostic(message), "p1": _human(wait)},
    )[:2000]
    log_activity(
        session,
        post.workspace_id,
        ActivityLevel.WARNING,
        "publish",
        Message(
            "publish.rate_activity",
            f"Publishing '{captions.video_label(target.video)}' to {_where(target)} was rate limited; "
            f"trying again in {_human(wait)} (this is not counted as a failed attempt): {message}",
            {
                "p0": captions.video_label(target.video),
                "p1": _where(target),
                "p2": _human(wait),
                "p3": diagnostic(message),
            },
        ),
        **_ids(target),
    )
    return _follow_up(session, post, kinds.PUBLISH_POST, run_at, now, message)


def _keep_min_gap(session: Session, target: _Target, t: datetime) -> datetime:
    """Move `t` later until it is at least the rule's min gap away from the account's other posts."""
    rule = target.rule
    gap = timedelta(minutes=max(int(rule.min_gap_minutes or 0), 0)) if rule is not None else timedelta(0)
    if gap <= timedelta(0):
        return t
    at = func.coalesce(ScheduledPost.published_at, ScheduledPost.scheduled_at)
    others = session.execute(
        select(at)
        .where(
            ScheduledPost.connected_account_id == target.account.id,
            ScheduledPost.id != target.post.id,
            ScheduledPost.status.in_([*ACTIVE_POST_STATUSES, PostStatus.PUBLISHED]),
            at > t - gap,
            at < t + MIN_GAP_LOOKAHEAD,
        )
        .order_by(at)
    ).scalars()
    for found in others:
        other = found if found.tzinfo else found.replace(tzinfo=UTC)
        if t - gap < other < t + gap:
            t = other + gap
    return t


def _retry_or_give_up(
    session: Session, target: _Target, message: str, retry_after: float | None, now: datetime
) -> StepResult:
    post = target.post
    post.attempts = (post.attempts or 0) + 1
    if post.attempts > MAX_RETRIES:
        return _needs_attention(
            session,
            target,
            Message(
                "publish.giveup",
                f"{message} (gave up after {post.attempts} attempts)",
                {"p0": diagnostic(message), "p1": post.attempts},
            ),
        )
    backoff = RETRY_BACKOFF_MINUTES[post.attempts - 1] * 60
    delay = max(float(backoff), min(float(retry_after or 0), MAX_RETRY_AFTER_SECONDS))
    run_at = now + timedelta(seconds=delay)
    post.status = PostStatus.FAILED
    post.scheduled_at = run_at
    post.error = Message(
        "publish.retry_error",
        f"Attempt {post.attempts} failed: {message} Retrying in {_human(delay)}.",
        {"p0": post.attempts, "p1": diagnostic(message), "p2": _human(delay)},
    )[:2000]
    log_activity(
        session,
        post.workspace_id,
        ActivityLevel.WARNING,
        "publish",
        Message(
            "publish.retry_activity",
            f"Publishing '{captions.video_label(target.video)}' to {_where(target)} failed; "
            f"retry {post.attempts} of {MAX_RETRIES} in {_human(delay)}: {message}",
            {
                "p0": captions.video_label(target.video),
                "p1": _where(target),
                "p2": post.attempts,
                "p3": MAX_RETRIES,
                "p4": _human(delay),
                "p5": diagnostic(message),
            },
        ),
        **_ids(target),
    )
    return _follow_up(session, post, kinds.PUBLISH_POST, run_at, now, message)


# =========================================================================== poll_post


def poll_post(session: Session, post_id: int, *, now: datetime | None = None) -> StepResult:
    """Check a PROCESSING post with the platform. Commits its own progress.

    Unlike publish_post, the video's status is not re-checked: the platform already has the
    upload, and archiving a video only stops posts that have not been uploaded yet. The platform
    is always asked before the post is given up on, and a paused workspace or a disabled rule only
    holds back platforms whose poll() would publish (Instagram)."""
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
    if account is None or video is None:
        return _cancel(
            session, post, Message("publish.target_gone", "The account or the video no longer exists.", {})
        )
    if account.status == AccountStatus.DISABLED:
        # The upload may be live although nobody can check it any more: never CANCELED, which the
        # planner reads as "never reached the platform".
        target = _Target(post, video, account, None, Path())
        return _needs_attention(
            session, target, DISCONNECTED_WHILE_PUBLISHING, may_be_live=True, notify=False
        )
    target = _Target(post, video, account, rules.find_rule(session, account), Path())
    if video.profile_id is not None or account.profile_id is not None:
        return _wait_while_halted(session, target, now)
    if not get_settings().publishing_enabled or (
        target.platform in PUBLISH_ON_POLL and _publishing_halted(session, target)
    ):
        return _wait_while_halted(session, target, now)
    try:
        ctx = fresh_context(session, account, now=now)
    except AccountUnavailable:
        return _needs_attention(session, target, _reconnect_message(account), may_be_live=True)
    except Exception as exc:  # noqa: BLE001 - handled like a poll error
        return _poll_error(session, target, exc, now)
    state = adapter_state(post.publish_state)
    try:
        result = get_publisher(target.platform).poll(ctx, state)
    except RestartPublish as exc:
        return _restart_publish(session, target, exc, now)
    except Exception as exc:  # noqa: BLE001 - classified below
        # Adapters record progress in `state` before raising (e.g. Instagram's publish attempts).
        post.publish_state = _with_engine(state, engine_state(post.publish_state))
        return _poll_error(session, target, exc, now)
    return _apply_result(session, target, result, now, polling=True)


def _publishing_halted(session: Session, target: _Target) -> bool:
    workspace = session.get(Workspace, target.post.workspace_id)
    if workspace is None or workspace.paused:
        return True
    rule = target.rule
    return target.post.created_by != "user" and (rule is None or not rule.enabled)


def _wait_while_halted(session: Session, target: _Target, now: datetime) -> StepResult:
    """Publishing is halted and polling would publish: wait, without letting the wait count
    against the 2-hour limit."""
    post = target.post
    delay = timedelta(seconds=PAUSED_POLL_DELAY_SECONDS)
    engine = engine_state(post.publish_state)
    engine["processing_since"] = min(_processing_since(post, now) + delay, now).isoformat()
    post.publish_state = _with_engine(adapter_state(post.publish_state), engine)
    return _follow_up(session, post, kinds.POLL_POST, now + delay, now, "publishing paused")


def _poll_error(session: Session, target: _Target, exc: Exception, now: datetime) -> StepResult:
    _record_quota_exhaustion(target, exc)
    post = target.post
    if isinstance(exc, AuthError):
        mark_needs_reconnect(session, target.account, exc.message)
        return _needs_attention(
            session,
            target,
            f"{_scrub(exc.message)} {_reconnect_message(target.account)}".strip(),
            may_be_live=True,
        )
    if isinstance(exc, PermanentError | NotSupportedError):
        # An adapter that gave up on confirming a request that may have been carried out says so
        # in its error code: the video may be live.
        return _needs_attention(
            session, target, _scrub(exc.message), may_be_live=exc.code in UNCONFIRMED_OUTCOME_CODES
        )
    if isinstance(exc, PlatformError):
        message = _scrub(exc.message)
    else:
        _log_unexpected(exc, f"polling post {post.id}")
        message = Message(
            "publish.unexpected", f"Unexpected error ({type(exc).__name__}).", {"p0": type(exc).__name__}
        )
    if now - _processing_since(post, now) > POLL_ERROR_CEILING:
        return _needs_attention(
            session,
            target,
            Message(
                "publish.poll_giveup",
                f"{captions.platform_name(target.platform)} could not be asked about the post for "
                f"{_human(POLL_ERROR_CEILING.total_seconds())} ({message}). The video may be live; check "
                "the account before publishing again.",
                {
                    "p0": captions.platform_name(target.platform),
                    "p1": _human(POLL_ERROR_CEILING.total_seconds()),
                    "p2": diagnostic(message),
                },
            ),
            may_be_live=True,
        )
    retry_after = exc.retry_after if isinstance(exc, RetryableError) else None
    delay = max(float(POLL_ERROR_DELAY_SECONDS), min(float(retry_after or 0), MAX_RETRY_AFTER_SECONDS))
    if isinstance(exc, RateLimitedError):
        # An uploaded video stays PROCESSING and is checked again after the limit; the 2-hour
        # clock only runs on answers, not on refusals to answer.
        delay += _jitter_seconds(post.id, POLL_JITTER_SECONDS)
    post.error = Message(
        "publish.poll_wait",
        f"Checking the upload failed: {message} Checking again in {_human(delay)}.",
        {"p0": diagnostic(message), "p1": _human(delay)},
    )[:2000]
    run_at = now + timedelta(seconds=delay)
    return _follow_up(session, post, kinds.POLL_POST, run_at, now, message)


def _restart_publish(session: Session, target: _Target, exc: RestartPublish, now: datetime) -> StepResult:
    """Nothing was published and the platform-side upload is gone: upload again later. This counts
    as a failed attempt, so a platform that keeps dropping uploads ends in NEEDS_ATTENTION."""
    post = target.post
    post.publish_state = {}
    post.platform_post_id = None
    post.permalink = None
    return _retry_or_give_up(session, target, _scrub(exc.message), exc.retry_after, now)


def _processing_since(post: ScheduledPost, now: datetime) -> datetime:
    since = _parse_dt(engine_state(post.publish_state).get("processing_since"))
    return since or ensure_aware(post.updated_at) or now


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
        post.publish_state = adapter_state(result.state) or adapter_state(post.publish_state)
        note = _publish_note(target.platform, result.message)
        log_activity(
            session,
            post.workspace_id,
            ActivityLevel.INFO,
            "publish",
            Message(
                "publish.published",
                f"Published '{captions.video_label(target.video)}' to {_where(target)}."
                + (f" {note}" if note else ""),
                {"p0": captions.video_label(target.video), "p1": _where(target), "p2": note},
            ),
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
        post.publish_state = _with_engine(
            adapter_state(result.state), {"processing_since": since.isoformat()}
        )
        if polling:
            post.error = None
            if now - since > POLL_TIMEOUT:
                # The platform was asked just now and is still processing: stop waiting.
                return _needs_attention(
                    session,
                    target,
                    Message(
                        "publish.poll_timeout",
                        f"{captions.platform_name(target.platform)} did not confirm the post within 2 hours "
                        "of the upload. Check the account before publishing again.",
                        {"p0": captions.platform_name(target.platform)},
                    ),
                    may_be_live=True,
                )
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
        Message(
            "publish.bad_answer",
            f"{captions.platform_name(target.platform)} returned an unexpected answer; check the account "
            "before publishing again.",
            {"p0": captions.platform_name(target.platform)},
        ),
        may_be_live=True,
    )


def _needs_attention(
    session: Session,
    target: _Target,
    message: str,
    *,
    may_be_live: bool = False,
    notify: bool = True,
) -> StepResult:
    """End the post as needs-attention. `may_be_live` records that the video may already be on
    the platform (the planner then counts it and never re-uses its cycle)."""
    post = target.post
    message = diagnostic(message)
    post.status = PostStatus.NEEDS_ATTENTION
    post.error = message[:2000]
    if may_be_live:
        mark_may_be_live(post)
    text = Message(
        "publish.failed",
        f"Could not publish '{captions.video_label(target.video)}' to {_where(target)}: {message}",
        {"p0": captions.video_label(target.video), "p1": _where(target), "p2": diagnostic(message)},
    )
    log_activity(session, post.workspace_id, ActivityLevel.ERROR, "publish", text, **_ids(target))
    session.commit()
    if notify:
        notify_mod.notify(
            text, workspace_id=post.workspace_id, level="error", category="post_needs_attention"
        )
    return StepResult(PostStatus.NEEDS_ATTENTION, message=message)


def _cancel(session: Session, post: ScheduledPost, reason: str) -> StepResult:
    reason = diagnostic(reason)
    post.status = PostStatus.CANCELED
    post.error = reason[:2000]
    log_activity(
        session,
        post.workspace_id,
        ActivityLevel.INFO,
        "publish",
        Message(
            "publish.canceled",
            f"Canceled post #{post.id}: {reason}",
            {"p0": post.id, "p1": diagnostic(reason)},
        ),
        post_id=post.id,
        video_id=post.video_id,
        account_id=post.connected_account_id,
        platform=str(post.platform),
    )
    session.commit()
    return StepResult(PostStatus.CANCELED, message=reason)


# =========================================================================== jobs


def queue_job(session: Session, kind: str, post_id: int, run_at: datetime) -> None:
    """Queue `kind` for the post at run_at, moving an already queued job to run_at. When the
    only job with the dedupe key is running right now (it will not see this change), a second job
    is queued: publish_post and poll_post are safe to run twice because they lock the post and
    check its status."""
    queue.enqueue_or_move(
        session, kind, {"post_id": post_id}, dedupe_key=f"{kind}:{post_id}", run_at=run_at, max_attempts=1
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
    return diagnostic(note, "publish.note") if note else ""


def _reconnect_message(account: ConnectedAccount) -> str:
    return Message(
        "account.reconnect_action",
        (
            f"Reconnect the {captions.platform_name(account.platform)} account '{account.display_name}', "
            "then publish again."
        ),
        {"platform": captions.platform_name(account.platform), "name": account.display_name},
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
        return Message("duration.seconds", f"{int(seconds)} seconds", {"n": int(seconds)})
    if minutes < 120:
        return Message("duration.minutes", f"{minutes} minute{'s' if minutes != 1 else ''}", {"n": minutes})
    value = f"{minutes / 60:g}" if minutes % 60 == 0 else f"{minutes / 60:.1f}"
    return Message("duration.hours", f"{value} hours", {"n": value})


def _log_unexpected(exc: BaseException, context: str) -> None:
    """Log a bug with its stack but without the exception text, which may embed URLs."""
    frames = "".join(traceback.format_tb(exc.__traceback__))
    log.error("%s failed with %s\n%s", context, type(exc).__name__, frames)
