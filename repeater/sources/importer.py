"""Library feeding: detect new videos on a source, download them, fingerprint and encode them.

Flow (PLAN.md sections 3 and 5.6):
    poll_source(source)   -> new Video rows (status importing) + import_video jobs
    import_video(video)   -> original file on disk (status processing) + process_video job
    process_video(video)  -> duplicate check, platform encode, thumbnail (status ready)

These functions commit their own progress. They are called from the job handlers in
repeater.sources.jobs, and a worker usually rolls back on exceptions, so state that must
survive a retry (errors, retry counters, source failure counters) is committed first.

Rules that hold across the module:
- yt-dlp sources never touch OAuth tokens or the official listers, whatever account is linked to
  the source row (retention/isolation contract C6).
- A video moves on from IMPORTING/PROCESSING only if it is still in that state (the row is locked
  and re-read first), so what the user did meanwhile (archive) is never overwritten.
- The duplicate check runs under a per-workspace advisory lock and also sees videos that are still
  PROCESSING, so the same clip handled by two workers ends up as one video and one duplicate.
"""

from __future__ import annotations

import errno
import json
import logging
import re
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from repeater import notify as notify_mod
from repeater.accounts import AccountUnavailable, fresh_context, mark_needs_reconnect
from repeater.activity import log_activity
from repeater.config import get_settings
from repeater.engine import actions as engine_actions
from repeater.jobs import kinds
from repeater.media import storage
from repeater.media.encode import (
    TARGET_FPS,
    DiskFullError,
    EncodeError,
    EncodeTimeout,
    encode_for_platforms,
    encode_preset,
    encode_timeout,
    faster_preset,
    make_thumbnail,
)
from repeater.media.fingerprint import (
    MAX_DURATION_DIFF_S,
    fingerprint_video,
    is_duplicate,
    pack,
    sha256_file,
    unpack,
)
from repeater.media.probe import MediaToolError, ProbeResult, probe
from repeater.messages import Message, UserError, diagnostic
from repeater.models import (
    ActivityLevel,
    ConnectedAccount,
    Job,
    JobStatus,
    Platform,
    ScheduledPost,
    Source,
    SourceKind,
    Video,
    VideoStatus,
)
from repeater.platforms import get_lister
from repeater.platforms.base import (
    LIMITS,
    AuthError,
    NotSupportedError,
    PermanentError,
    PlatformError,
    RemoteVideo,
    RetryableError,
)
from repeater.queue import RetryLater, default_backoff
from repeater.sources import drive, ytdlp
from repeater.timeutil import ensure_aware, utcnow

log = logging.getLogger("repeater.sources")

# Items requested from a source on each regular poll (fewer on the unofficial path).
POLL_LIST_LIMIT = 50
UNOFFICIAL_LIST_LIMIT = 30
# Upper bound for config["backfill_count"] (existing videos imported on the first poll).
MAX_BACKFILL = 200
# config["seen_ids"] keeps this many most recent external ids. A Drive folder is listed completely
# and decided by ids alone, so its snapshot must cover the whole listing.
SEEN_IDS_CAP = 500
DRIVE_SEEN_IDS_CAP = drive.FULL_LISTING_LIMIT
# Config keys that say *what* a source watches (mirrors repeater.web.routes.sources.IDENTITY_KEYS).
IDENTITY_KEYS = ("profile_url", "folder_id")
# A source is paused after this many failed polls in a row (PLAN.md 3.3 rule 3).
MAX_CONSECUTIVE_FAILURES = 3
# The unofficial path is polled at most every 30 minutes (PLAN.md 3.3 rule 3).
YTDLP_MIN_POLL_MINUTES = 30
PLATFORM_API_PLATFORMS = (Platform.INSTAGRAM, Platform.FACEBOOK)
YTDLP_PLATFORMS = (Platform.TIKTOK, Platform.YOUTUBE)
# Videos processed at the same time in this worker process. Encoding is CPU-bound on a small
# VPS: one at a time finishes sooner than two sharing a core and leaves a job thread free for
# publishing and polling.
ENCODE_CONCURRENCY = 1
ENCODE_BUSY_RETRY_S = 45
# Postgres advisory-lock namespace of the per-workspace duplicate check (the planner's is 727001).
DUPLICATE_LOCK_NAMESPACE = 727_002
# Keep this much disk free (Postgres shares the volume); a download needs twice its known size.
MIN_FREE_BYTES = 2 * 1024**3
DISK_WAIT_S = 1800
# After a failed try the next one encodes with a faster x264 preset (first retry, second retry),
# so a long video that timed out on a small CPU still gets through instead of timing out 3 times.
RETRY_PRESETS = ("veryfast", "ultrafast")
# An encode shorter than this share of the source (minus the slack) means the file was cut off.
TRUNCATION_RATIO = 0.95
TRUNCATION_SLACK_S = 0.5
# This many undecodable samples at the end of the file: damaged before any encoding is tried.
TRUNCATED_TAIL_SAMPLES = 3
# Shortest/longest video any target platform accepts (see platforms.base.LIMITS).
MIN_DURATION_S = min(lim.min_duration_s for lim in LIMITS.values())
MAX_DURATION_S = max(lim.max_duration_s for lim in LIMITS.values())

_ACTIVE = (VideoStatus.IMPORTING, VideoStatus.PROCESSING)

# Tries per video when the running job row cannot be found (mirrors repeater.jobs.kinds).
DEFAULT_MAX_TRIES = {kinds.IMPORT_VIDEO: 4, kinds.PROCESS_VIDEO: 3}
_ATTEMPT_RE = re.compile(r"^Attempt (\d+) of \d+ failed")
_VIDEO_EXTENSIONS = frozenset(
    {"mp4", "mov", "m4v", "webm", "mkv", "avi", "3gp", "mpg", "mpeg", "ts", "wmv", "flv"}
)
FALLBACK_HINT = "Upload the original file or add it to a Google Drive folder source instead."


class SourceConfigError(Exception):
    """The source cannot be polled as configured (missing account, link, acknowledgement...)."""


@dataclass
class PollOutcome:
    status: str  # ok | skipped | blocked | failed
    listed: int = 0
    created: int = 0
    message: str = ""


# =========================================================================== poll_source


def poll_source(session: Session, source_id: int, *, now: datetime | None = None) -> PollOutcome:
    """Look for new videos on a source and queue their import. Never raises for source
    problems: failures are recorded on the source (and pause it after 3 in a row)."""
    source = session.get(Source, source_id)
    if source is None:
        return PollOutcome("skipped", message="source not found")
    if not source.enabled or source.kind == SourceKind.MANUAL:
        return PollOutcome("skipped", message="source disabled or manual")
    from repeater.commerce import work_allowed
    from repeater.models import Workspace

    if not work_allowed(session.get(Workspace, source.workspace_id)):
        source.enabled = False
        source.last_error = Message(
            "commerce.expired", "The workspace is closed or the subscription expired.", {}
        )
        session.commit()
        return PollOutcome("skipped", message=source.last_error)
    now = now or utcnow()
    source.last_polled_at = now
    if source.kind == SourceKind.YTDLP and source.unofficial_ack_at is None:
        return _block_unacknowledged(session, source)
    watched = _watched(source)
    session.commit()  # no open transaction or row lock while the (slow) listing runs
    failure: Exception | None = None
    remotes: list[RemoteVideo] = []
    try:
        remotes = _detect(session, source)
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the source
        failure = exc
    # The user may have edited the source while the listing ran. Lock the row and look again: the
    # result belongs to the old settings and must not overwrite the new ones.
    session.refresh(source, with_for_update=True)
    if not source.enabled or _watched(source) != watched:
        log.info("source %s was changed while it was being checked; the result is dropped", source.id)
        source.last_polled_at = None  # check again with the new settings on the next tick
        session.commit()
        return PollOutcome("skipped", message="the source was changed while it was being checked")
    if failure is not None:
        message = _failure_text(session, source, failure)
        _record_failure(session, source, message)
        session.commit()
        return PollOutcome("failed", message=message)
    from repeater.commerce import LimitError

    try:
        with session.begin_nested():
            created = _ingest(session, source, remotes, now)
    except LimitError as exc:
        source.enabled = False
        message = Message(exc.key, str(exc), {})
        _record_failure(session, source, message)
        session.commit()
        return PollOutcome("failed", message=message)
    source.last_success_at = now
    # Report only a completed listing. Never advance these on a failed/partial scan.
    source.config = {**source.config, "last_listed_count": len(remotes), "last_created_count": created}
    source.last_error = None
    source.consecutive_failures = 0
    session.commit()
    return PollOutcome("ok", listed=len(remotes), created=created)


def _watched(source: Source) -> tuple[Any, ...]:
    """What a poll depends on: if this changes during a poll, the poll's result is stale."""
    config = source.config or {}
    return (
        source.kind,
        source.platform,
        source.connected_account_id,
        source.profile_id,
        config.get("control_revision"),
        config.get("backfill_count"),
        config.get("watch_from"),
        config.get("include_images"),
        tuple(str(config.get(key) or "") for key in IDENTITY_KEYS),
        str(config.get("proxy") or ""),
        "baseline_at" in config,
    )


def due_sources(session: Session, now: datetime | None = None) -> list[Source]:
    """Enabled, pollable sources whose poll interval has elapsed."""
    now = now or utcnow()
    rows = session.execute(
        select(Source).where(Source.enabled.is_(True), Source.kind != SourceKind.MANUAL).order_by(Source.id)
    ).scalars()
    due: list[Source] = []
    for source in rows:
        minutes = max(int(source.poll_interval_minutes or 0), 1)
        if source.kind == SourceKind.YTDLP:
            minutes = max(minutes, YTDLP_MIN_POLL_MINUTES)
        last = ensure_aware(source.last_polled_at)
        if last is None or last + timedelta(minutes=minutes) <= now:
            due.append(source)
    return due


def enqueue_due_polls(session: Session, now: datetime | None = None) -> int:
    """Queue poll_source for every due source (for the engine's periodic tick). The poll job
    cannot re-queue itself while it runs because of its dedupe key."""
    count = 0
    for source in due_sources(session, now):
        if kinds.enqueue_poll_source(session, source.id) is not None:
            count += 1
    return count


def _block_unacknowledged(session: Session, source: Source) -> PollOutcome:
    message = Message(
        "source.consent_needed",
        "Unofficial import (yt-dlp) is off for this source until you confirm you own the content "
        "and accept the risk. " + FALLBACK_HINT,
        {},
    )
    if source.last_error != message:
        log_activity(
            session,
            source.workspace_id,
            ActivityLevel.WARNING,
            "source",
            Message(
                "source.blocked",
                f"Source '{_source_label(source)}' was not checked: {message}",
                {"p0": _source_label(source), "p1": diagnostic(message)},
            ),
            source_id=source.id,
        )
    source.last_error = message
    session.commit()
    return PollOutcome("blocked", message=message)


def _detect(session: Session, source: Source) -> list[RemoteVideo]:
    config = dict(source.config or {})
    first_poll = "baseline_at" not in config
    backfill = _backfill_count(config) if first_poll else 0
    since = None if first_poll else _parse_iso(config.get("baseline_at"))
    person_platform = source.profile_id is not None and source.kind == SourceKind.PLATFORM_API
    if person_platform:
        config["require_complete"] = True
        if first_poll and not backfill:
            since = _parse_iso(config.get("watch_from"))
        if since is not None and source.last_success_at:
            # Overlap for delayed provider visibility; external-id uniqueness makes it idempotent.
            since = max(since, ensure_aware(source.last_success_at) - timedelta(days=1))

    if source.kind == SourceKind.YTDLP:
        # Detection and download use yt-dlp only: never an OAuth token or an official lister,
        # even when a connected account is still linked to the row (retention/isolation C6).
        if source.platform not in YTDLP_PLATFORMS:
            raise SourceConfigError("Unofficial import supports TikTok and YouTube sources only.")
        profile_url = str(config.get("profile_url") or "").strip()
        if not profile_url:
            raise SourceConfigError("No profile link is set for this source.")
        proxy, cookies = ytdlp.options_for_source(source.id, config)
        limit = max(UNOFFICIAL_LIST_LIMIT, backfill)
        return ytdlp.list_profile(profile_url, limit=limit, proxy=proxy, cookies_file=cookies)

    platform = _lister_platform(source)
    account = _source_account(source, platform)
    ctx = fresh_context(session, account)
    if source.kind == SourceKind.DRIVE:
        # The whole folder, decided by file ids (createdTime is not "added to the folder").
        return get_lister(platform).list_recent(
            ctx, since=None, limit=max(drive.FULL_LISTING_LIMIT, backfill), config=config
        )
    limit = max(POLL_LIST_LIMIT, backfill)
    if person_platform and (not first_poll or not backfill):
        limit = 1001
    remotes = get_lister(platform).list_recent(ctx, since=since, limit=limit, config=config)
    if person_platform and limit == 1001 and len(remotes) >= limit:
        raise SourceConfigError(
            "Too many new videos to check safely in one pass. No checkpoint was advanced. "
            "Contact support or use the original files in Drive."
        )
    return remotes


def _lister_platform(source: Source) -> Platform:
    if source.kind == SourceKind.DRIVE:
        return Platform.GDRIVE
    if source.kind == SourceKind.PLATFORM_API:
        if source.platform not in PLATFORM_API_PLATFORMS:
            raise SourceConfigError(
                "Only Instagram and Facebook offer an official download. " + FALLBACK_HINT
            )
        return Platform(source.platform)
    raise SourceConfigError("This kind of source has no official API listing.")


def _source_account(source: Source, platform: Platform) -> ConnectedAccount:
    account = source.connected_account
    if account is None:
        raise SourceConfigError("No connected account is linked to this source. Connect the account first.")
    if account.workspace_id != source.workspace_id or account.platform != platform:
        raise SourceConfigError("The linked account does not match this source's platform.")
    if source.kind == SourceKind.PLATFORM_API and account.profile_id != source.profile_id:
        raise SourceConfigError("The linked account belongs to a different person's library.")
    return account


def _ingest(session: Session, source: Source, remotes: list[RemoteVideo], now: datetime) -> int:
    config = dict(source.config or {})
    remotes = [
        r
        for r in remotes
        if (
            r.is_video
            or (
                source.profile_id is not None
                and source.kind == SourceKind.DRIVE
                and source.config.get("include_images")
                and r.extra.get("media_kind") == "image"
            )
        )
        and r.external_id
        and len(str(r.external_id)) <= 128
    ]
    seen = [str(x) for x in config.get("seen_ids") or []]
    # What Repeater itself published to this platform comes back through the listing of an
    # account that is both source and target: never import it again, just remember it.
    own = _own_post_ids(session, source, remotes)
    candidates = [r for r in remotes if str(r.external_id) not in own]
    baseline = _parse_iso(config.get("baseline_at"))
    if baseline is None:
        chosen = _newest(candidates, _backfill_count(config))
        watch_from = _parse_iso(config.get("watch_from"))
        if source.profile_id and source.kind == SourceKind.PLATFORM_API and watch_from:
            # New-only starts at the explicit start action, not when the worker eventually runs.
            baseline = watch_from
            if not _backfill_count(config):
                chosen = [r for r in candidates if r.created_at and ensure_aware(r.created_at) > baseline]
        config["baseline_at"] = (baseline or now).isoformat()
    else:
        chosen = _new_items(source, candidates, remotes, baseline, set(seen))
    created = _create_videos(session, source, chosen)
    cap = DRIVE_SEEN_IDS_CAP if source.kind == SourceKind.DRIVE else SEEN_IDS_CAP
    config["seen_ids"] = _merge_seen([str(r.external_id) for r in remotes], seen, cap)
    source.config = config  # a new dict, so SQLAlchemy writes the JSONB column
    return created


def _own_post_ids(session: Session, source: Source, remotes: list[RemoteVideo]) -> set[str]:
    """Listed ids that are posts Repeater published itself on the source's platform. (TikTok posts
    that were not public yet have no id, so the fingerprint still has to catch those.)"""
    if source.kind == SourceKind.DRIVE or not source.platform or not remotes:
        return set()
    rows = session.execute(
        select(ScheduledPost.platform_post_id).where(
            ScheduledPost.workspace_id == source.workspace_id,
            ScheduledPost.platform == Platform(source.platform),
            ScheduledPost.platform_post_id.in_([str(r.external_id) for r in remotes]),
        )
    ).scalars()
    return {str(x) for x in rows}


def _new_items(
    source: Source,
    candidates: list[RemoteVideo],
    listing: list[RemoteVideo],
    baseline: datetime,
    seen: set[str],
) -> list[RemoteVideo]:
    """The listed videos that are new. A Drive folder is decided by file id alone: createdTime is
    when the file was created, so a video moved into the folder later is older than the source.
    Everywhere else a timestamp newer than the baseline counts; items without one (flat yt-dlp
    listings of YouTube Shorts) count when they are listed above the newest id seen before."""
    unseen = [r for r in candidates if str(r.external_id) not in seen]
    if source.kind == SourceKind.DRIVE:
        return unseen
    leading = _leading_unseen(source, listing, seen) if any(r.created_at is None for r in unseen) else set()
    new: list[RemoteVideo] = []
    for remote in unseen:
        created = ensure_aware(remote.created_at)
        if created is not None:
            if created > baseline:
                new.append(remote)
        elif str(remote.external_id) in leading:
            new.append(remote)
    return new


def _leading_unseen(source: Source, listing: list[RemoteVideo], seen: set[str]) -> set[str]:
    """Ids listed before the first already-seen id of a newest-first listing. Whatever is listed
    after it is older than something we already know, so it only appears because a newer video
    was deleted or made private and pushed it into the window. With nothing seen at all every
    listed id is new; with no overlap the gap is ambiguous and nothing is imported."""
    ids = [str(r.external_id) for r in listing]
    if not seen:
        return set(ids)
    for position, item in enumerate(ids):
        if item in seen:
            return set(ids[:position])
    if ids:
        log.warning(
            "source %s: none of the %d listed videos was seen before; importing nothing from this poll",
            source.id,
            len(ids),
        )
    return set()


def _newest(remotes: list[RemoteVideo], n: int) -> list[RemoteVideo]:
    if n <= 0:
        return []
    if all(r.created_at for r in remotes):
        remotes = sorted(remotes, key=lambda r: ensure_aware(r.created_at), reverse=True)
    return remotes[:n]  # listings are newest first when there are no timestamps


def _oldest_first(items: list[RemoteVideo]) -> list[RemoteVideo]:
    if items and all(r.created_at for r in items):
        return sorted(items, key=lambda r: ensure_aware(r.created_at))
    return list(reversed(items))


def _merge_seen(listed: list[str], previous: list[str], cap: int = SEEN_IDS_CAP) -> list[str]:
    merged: list[str] = []
    marks: set[str] = set()
    for item in listed + previous:
        if item not in marks:
            marks.add(item)
            merged.append(item)
    return merged[:cap]


def _backfill_count(config: dict[str, Any]) -> int:
    try:
        return max(0, min(int(config.get("backfill_count") or 0), MAX_BACKFILL))
    except (TypeError, ValueError):
        return 0


def _create_videos(session: Session, source: Source, items: list[RemoteVideo]) -> int:
    """Insert Video rows oldest first; the partial unique index skips known videos."""
    platform = Platform.GDRIVE if source.kind == SourceKind.DRIVE else source.platform
    if source.kind == SourceKind.YTDLP:  # the linked account (if any) is ignored, see _detect
        account_id = _match_profile_account(session, source)
    else:
        account_id = source.connected_account_id
    created = 0
    for remote in _oldest_first(items):
        from repeater.commerce import require_capacity

        known = session.scalar(
            select(Video.id).where(
                Video.workspace_id == source.workspace_id,
                Video.profile_id == source.profile_id,
                Video.source_platform == platform,
                Video.external_id == str(remote.external_id),
            )
        )
        if known:
            continue
        require_capacity(session, source.workspace_id, "items")
        require_capacity(
            session, source.workspace_id, "storage_bytes", get_settings().max_upload_mb * 1024**2 * 4
        )
        stmt = (
            pg_insert(Video)
            .values(
                workspace_id=source.workspace_id,
                profile_id=source.profile_id,
                media_kind="video" if remote.is_video else "image",
                content_format="reel" if remote.is_video else "post",
                source_id=source.id,
                source_platform=platform,
                source_account_id=account_id,
                external_id=str(remote.external_id),
                permalink=remote.permalink,
                title=(remote.title or "").strip(),
                caption=(remote.caption or "").strip(),
                source_published_at=ensure_aware(remote.created_at),
                status=VideoStatus.IMPORTING,
                overrides={},
            )
            .on_conflict_do_nothing(
                index_elements=[
                    "workspace_id",
                    # Must match the index literally even after psycopg prepares a generic plan.
                    func.coalesce(Video.profile_id, literal_column("0")),
                    "source_platform",
                    "external_id",
                ],
                index_where=Video.external_id.isnot(None),
            )
            .returning(Video.id)
        )
        video_id = session.execute(stmt).scalar_one_or_none()
        if video_id is not None:
            _enqueue_import(session, video_id, remote)
            created += 1
    return created


def _enqueue_import(session: Session, video_id: int, remote: RemoteVideo) -> None:
    job_id = kinds.enqueue_import_video(session, video_id)
    hints = _remote_hints(remote)
    if job_id is not None and hints:
        job = session.get(Job, job_id)
        if job is not None:
            job.payload = {**(job.payload or {}), **hints}


def _remote_hints(remote: RemoteVideo) -> dict[str, Any]:
    """Listing details the download step needs (CDN URLs are short-lived and have no column)."""
    hints: dict[str, Any] = {}
    if remote.download_url:
        hints["download_url"] = remote.download_url
    if remote.extra:
        try:
            json.dumps(remote.extra)
            hints["remote_extra"] = dict(remote.extra)
        except (TypeError, ValueError):
            pass
    return hints


def _match_profile_account(session: Session, source: Source) -> int | None:
    """For a yt-dlp source without a linked account, find the workspace's own account for the
    same profile so its videos are never re-posted to where they came from."""
    if source.kind != SourceKind.YTDLP or source.platform not in YTDLP_PLATFORMS:
        return None
    handle = _profile_handle(str((source.config or {}).get("profile_url") or ""))
    if not handle:
        return None
    accounts = session.execute(
        select(ConnectedAccount).where(
            ConnectedAccount.workspace_id == source.workspace_id, ConnectedAccount.platform == source.platform
        )
    ).scalars()
    for acc in accounts:
        extra = acc.extra or {}
        names = {str(extra.get(k) or "").lower().lstrip("@") for k in ("username", "custom_url")}
        ids = {str(extra.get("channel_id") or "").lower(), str(acc.external_id).lower()}
        if handle in names or handle in ids:
            return acc.id
    return None


def _profile_handle(url: str) -> str | None:
    parts = [p for p in urlparse(url).path.split("/") if p]
    for i, part in enumerate(parts):
        if part.startswith("@") and len(part) > 1:
            return part[1:].lower()
        if part in ("channel", "c", "user") and i + 1 < len(parts):
            return parts[i + 1].lower()
    return None


def _failure_text(session: Session, source: Source, exc: Exception) -> str:
    if isinstance(exc, AuthError):
        account = source.connected_account
        if account is not None:
            mark_needs_reconnect(session, account, exc.message)
        return f"{exc.message} Reconnect the account, then turn the source back on."
    if isinstance(exc, AccountUnavailable):
        return "The linked account is disconnected or needs to be reconnected."
    if isinstance(exc, PlatformError):
        return exc.message
    if isinstance(exc, SourceConfigError):
        return str(exc)
    _log_unexpected(exc, f"poll of source {source.id}")
    return f"Unexpected error while checking the source ({type(exc).__name__})."


def _record_failure(session: Session, source: Source, message: str) -> None:
    message = diagnostic(message, "source.failure")
    source.last_error = message[:2000]
    source.consecutive_failures = (source.consecutive_failures or 0) + 1
    label = _source_label(source)
    if source.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        source.enabled = False
        hint = (
            f" {FALLBACK_HINT}" if source.kind == SourceKind.YTDLP and "Google Drive" not in message else ""
        )
        text = Message(
            "source.paused",
            f"Source '{label}' was paused after {source.consecutive_failures} failed checks in a row. "
            f"Last error: {message}{hint}",
            {"p0": label, "p1": source.consecutive_failures, "p2": diagnostic(message), "p3": hint},
        )
        log_activity(session, source.workspace_id, ActivityLevel.ERROR, "source", text, source_id=source.id)
        _notify(text, source.workspace_id, "error", "source_failing")
    elif source.consecutive_failures == 1:
        log_activity(
            session,
            source.workspace_id,
            ActivityLevel.WARNING,
            "source",
            Message(
                "source.check_failed",
                f"Checking source '{label}' for new videos failed: {message}",
                {"p0": label, "p1": diagnostic(message)},
            ),
            source_id=source.id,
        )


def _notify(text: str, workspace_id: int | None, level: str, category: str = "alert") -> None:
    """Best-effort Telegram alert. By default only `category` and a dashboard link leave the server
    (notify.py); the text with titles and errors is sent only when the operator opted in."""
    try:
        notify_mod.notify(text, workspace_id=workspace_id, level=level, category=category)
    except Exception as exc:  # noqa: BLE001 - notifications are best effort
        log.warning("notification failed: %s", type(exc).__name__)


# =========================================================================== import_video


def import_video(session: Session, video_id: int, payload: dict[str, Any] | None = None) -> None:
    """Download the original file of an IMPORTING video, then queue process_video.
    Raises RetryLater on transient errors (after committing the retry state)."""
    payload = payload or {}
    video = session.get(Video, video_id)
    if video is None or video.status != VideoStatus.IMPORTING:
        return
    from repeater.commerce import work_allowed
    from repeater.models import Workspace

    if not work_allowed(session.get(Workspace, video.workspace_id)):
        _fail_video(
            session,
            video,
            Message("commerce.expired", "The workspace is closed or the subscription expired.", {}),
        )
        session.commit()
        return
    _require_free_space(session, video, _expected_download_bytes(payload))
    try:
        path = _download_original(session, video, payload)
    except RetryableError as exc:
        if exc.code == "disk_full":
            _wait_for_disk(session, video)
        _retry_or_fail(session, video, exc.message, kinds.IMPORT_VIDEO, payload, exc.retry_after)
        return
    except (PermanentError, NotSupportedError, SourceConfigError, AccountUnavailable, AuthError) as exc:
        _fail_video(session, video, _import_error_text(session, video, exc))
        session.commit()
        return
    except PlatformError as exc:  # unclassified adapter error: treat as transient
        _retry_or_fail(session, video, exc.message, kinds.IMPORT_VIDEO, payload)
        return
    except Exception as exc:  # noqa: BLE001 - unknown errors are retried, then reported
        if _is_disk_full(exc):
            session.rollback()
            _wait_for_disk(session, video)
        _log_unexpected(exc, f"import of video {video.id}")
        _retry_or_fail(
            session, video, f"Unexpected error ({type(exc).__name__})", kinds.IMPORT_VIDEO, payload
        )
        return
    # The user may have archived the video while it downloaded: lock the row and only move it
    # on if it is still importing. The file is kept either way (restoring an archived video
    # encodes it from the original).
    still_importing = _lock_status(session, video.id) == VideoStatus.IMPORTING
    video.original_path = storage.rel_path(path)
    video.size_bytes = path.stat().st_size
    video.sha256 = None  # fingerprints of an earlier original (retry) do not describe this file
    video.original_sha256 = None
    video.phash = None
    if still_importing:
        video.status = VideoStatus.PROCESSING
        video.error = None
        kinds.enqueue_process_video(session, video.id)
    else:
        log.info("video %s was changed while it downloaded; keeping the file, not processing it", video.id)
    session.commit()


def _download_original(session: Session, video: Video, payload: dict[str, Any]) -> Path:
    source = video.source
    if source is None:
        raise PermanentError("The source of this video was deleted.")
    dest_dir = storage.ensure_video_dir(video.workspace_id, video.id)
    _clear_previous_originals(dest_dir)
    if source.kind == SourceKind.YTDLP:
        if source.unofficial_ack_at is None:
            raise PermanentError("Unofficial import (yt-dlp) is turned off for this source. " + FALLBACK_HINT)
        proxy, cookies = ytdlp.options_for_source(source.id, source.config)
        url = _ytdlp_video_url(video, source)
        return ytdlp.download(url, dest_dir, proxy=proxy, cookies_file=cookies, basename="original")
    if source.kind == SourceKind.MANUAL:
        raise PermanentError("Uploaded videos have no source to download from; upload the file again.")
    platform = _lister_platform(source)
    ctx = fresh_context(session, _source_account(source, platform))
    remote = RemoteVideo(
        external_id=str(video.external_id or ""),
        created_at=video.source_published_at,
        title=video.title or "",
        caption=video.caption or "",
        permalink=video.permalink,
        download_url=payload.get("download_url"),
        extra=dict(payload.get("remote_extra") or {}),
    )
    if video.media_kind == "image":
        remote.extra["media_kind"] = "image"
    dest = dest_dir / f"original.{_guess_ext(remote)}"
    return get_lister(platform).download(ctx, remote, dest)


def _clear_previous_originals(dest_dir: Path) -> None:
    for p in dest_dir.glob("original.*"):
        if p.is_file():
            p.unlink(missing_ok=True)


def _ytdlp_video_url(video: Video, source: Source) -> str:
    if video.permalink and video.permalink.startswith(("http://", "https://")):
        return video.permalink
    vid = str(video.external_id or "")
    platform = video.source_platform or source.platform
    if vid and platform == Platform.YOUTUBE:
        return f"https://www.youtube.com/watch?v={vid}"
    if vid and platform == Platform.TIKTOK:
        username = _profile_handle(str((source.config or {}).get("profile_url") or ""))
        if username:
            return f"https://www.tiktok.com/@{username}/video/{vid}"
    raise PermanentError("The video has no link to download it from. " + FALLBACK_HINT)


def _guess_ext(remote: RemoteVideo) -> str:
    candidates = [str(remote.extra.get("ext") or ""), Path(urlparse(remote.download_url or "").path).suffix]
    for c in candidates:
        ext = c.lower().lstrip(".")
        if ext in _VIDEO_EXTENSIONS:
            return ext
    return "mp4"


def _import_error_text(session: Session, video: Video, exc: Exception) -> str:
    if isinstance(exc, AuthError):
        source = video.source
        if source is not None and source.connected_account is not None:
            mark_needs_reconnect(session, source.connected_account, exc.message)
        return f"{exc.message} Reconnect the account, then retry the import."
    if isinstance(exc, AccountUnavailable):
        return "The source account is disconnected or needs to be reconnected; retry the import afterwards."
    if isinstance(exc, NotSupportedError):
        return f"{exc.message} {FALLBACK_HINT}" if FALLBACK_HINT not in exc.message else exc.message
    if isinstance(exc, PlatformError):
        return exc.message
    return str(exc)


# =========================================================================== process_video


_ENCODE_SLOTS = threading.BoundedSemaphore(ENCODE_CONCURRENCY)


def process_video(session: Session, video_id: int, payload: dict[str, Any] | None = None) -> None:
    """Fingerprint a PROCESSING video, mark duplicates, otherwise encode it and make it READY.

    Only ENCODE_CONCURRENCY videos are processed at a time per worker process; another one
    arriving meanwhile is put back (RetryLater) without using up an attempt. Payload (and
    Video.overrides) may carry "skip_duplicate_check": true, used when the user said a video that
    was taken for a duplicate is not one."""
    payload = payload or {}
    video = session.get(Video, video_id)
    if video is None or video.status != VideoStatus.PROCESSING:
        return
    original = _original_file(video)
    if original is None:
        _fail_video(
            session,
            video,
            Message(
                "import.original_missing",
                "The original file is missing. Import or upload the video again.",
                {},
            ),
        )
        session.commit()
        return
    if not _ENCODE_SLOTS.acquire(blocking=False):
        raise RetryLater(ENCODE_BUSY_RETRY_S, "another video is being processed")
    try:
        _process(session, video, original, payload)
    finally:
        _ENCODE_SLOTS.release()


def _process(session: Session, video: Video, original: Path, payload: dict[str, Any]) -> None:
    video_id = video.id
    try:
        if video.media_kind == "image":
            from repeater.media import images

            image = images.decode(original)
            _lock_duplicate_check(session, video.workspace_id)
            if _lock_status(session, video_id) != VideoStatus.PROCESSING:
                image.close()
                session.rollback()
                return
            images.prepare(video, original, image)
            image.close()
            duplicate = session.scalar(
                select(Video)
                .where(
                    Video.workspace_id == video.workspace_id,
                    Video.profile_id.is_not_distinct_from(video.profile_id),
                    Video.id != video.id,
                    Video.media_kind == "image",
                    Video.sha256 == video.sha256,
                    Video.status.in_((VideoStatus.READY, VideoStatus.ARCHIVED)),
                )
                .order_by(Video.id)
                .limit(1)
            )
            if duplicate is not None and not payload.get("skip_duplicate_check"):
                _mark_duplicate(session, video, duplicate)
            session.commit()
            return
        info = probe(original)
        problem = _duration_problem(info.duration_s)
        if problem:
            _fail_video(session, video, problem)
            session.commit()
            return
        fp = fingerprint_video(original, info)
        if fp.tail_missing >= TRUNCATED_TAIL_SAMPLES:
            _fail_video(session, video, _damaged_text(info.duration_s, None))
            session.commit()
            return
        sha256 = sha256_file(original)
        skip_check = bool(
            payload.get("skip_duplicate_check") or (video.overrides or {}).get("skip_duplicate_check")
        )
        # One check-and-claim at a time per workspace, until the fingerprints are committed:
        # the video that commits first is the original, later ones see it and become duplicates.
        _lock_duplicate_check(session, video.workspace_id)
        if _lock_status(session, video_id) != VideoStatus.PROCESSING:
            session.rollback()  # archived (or deleted) while it was being fingerprinted
            return
        video.sha256 = sha256
        video.original_sha256 = sha256
        video.phash = pack(fp.frames, fp.audio)
        video.size_bytes = original.stat().st_size
        video.duration_s = info.duration_s
        duplicate = (
            None if skip_check else find_duplicate(session, video, info.duration_s, fp.frames, fp.audio)
        )
        if duplicate is not None:
            _mark_duplicate(session, video, duplicate)
            session.commit()
            return
        session.commit()  # keep fingerprints; release the locks before the long encode
        _require_free_space(session, video, None)
        _encode_and_finish(session, video, original, info, _retry_preset(payload, video))
    except RetryLater:
        raise
    except ValueError as exc:
        _fail_video(session, video, f"The file is {exc}" if str(exc).startswith("not a video") else str(exc))
        session.commit()
    except EncodeTimeout as exc:
        _retry_or_fail(session, video, str(exc).split(":")[0] + ".", kinds.PROCESS_VIDEO, payload)
    except EncodeError as exc:
        # ffmpeg ran and rejected the input; the same file will fail the same way again.
        _fail_video(
            session,
            video,
            Message("import.encode_failed", f"Encoding failed: {exc}", {"p0": diagnostic(exc)}),
        )
        session.commit()
    except DiskFullError:
        session.rollback()
        _wait_for_disk(session, session.get(Video, video_id) or video)
    except MediaToolError as exc:
        # The tools themselves are missing or hung (not the file's fault): try again later.
        _retry_or_fail(session, video, str(exc), kinds.PROCESS_VIDEO, payload)
    except Exception as exc:  # noqa: BLE001 - unknown errors are retried, then reported
        session.rollback()
        reloaded = session.get(Video, video_id)
        if _is_disk_full(exc) and reloaded is not None:
            _wait_for_disk(session, reloaded)
        _log_unexpected(exc, f"processing of video {video_id}")
        if reloaded is not None:
            _retry_or_fail(
                session, reloaded, f"Unexpected error ({type(exc).__name__})", kinds.PROCESS_VIDEO, payload
            )


def find_duplicate(
    session: Session, video: Video, duration_s: float, hashes: list[str], audio: str | None = None
) -> Video | None:
    """A video of the same workspace with the same bytes or the same picture and sound. READY and
    ARCHIVED videos count, and so do videos still PROCESSING whose fingerprints are already
    committed (they are being encoded right now and will be the original)."""
    base = select(Video).where(
        Video.workspace_id == video.workspace_id,
        Video.profile_id.is_not_distinct_from(video.profile_id),
        Video.id != video.id,
        Video.status.in_([VideoStatus.READY, VideoStatus.ARCHIVED, VideoStatus.PROCESSING]),
    )
    if video.sha256:
        exact = session.execute(base.where(Video.sha256 == video.sha256).order_by(Video.id).limit(1)).scalar()
        if exact is not None:
            return exact
    if not hashes:
        return None
    candidates = session.execute(
        base.where(
            Video.phash.isnot(None),
            Video.duration_s.between(duration_s - MAX_DURATION_DIFF_S, duration_s + MAX_DURATION_DIFF_S),
        )
        .order_by(Video.id)
        .execution_options(populate_existing=True)
    ).scalars()
    for other in candidates:
        other_frames, other_audio = unpack(other.phash)
        if is_duplicate(hashes, duration_s, other_frames, other.duration_s, audio, other_audio):
            return other
    return None


def _lock_duplicate_check(session: Session, workspace_id: int) -> None:
    """Serialise duplicate checks per workspace until the transaction ends."""
    session.execute(select(func.pg_advisory_xact_lock(DUPLICATE_LOCK_NAMESPACE, int(workspace_id))))


def _lock_status(session: Session, video_id: int) -> VideoStatus | None:
    """The video's status as stored right now, with its row locked until the transaction ends
    (None if the row is gone). Unflushed changes of this session are written first."""
    return session.execute(
        select(Video.status).where(Video.id == video_id).with_for_update()
    ).scalar_one_or_none()


def _damaged_text(source_s: float, encoded_s: float | None) -> str:
    if encoded_s is None:
        return "The file seems damaged or incomplete: its end cannot be read. Upload the original again."
    return (
        f"The file seems damaged or incomplete: it ends after {_mmss(encoded_s)} "
        f"although it should be {_mmss(source_s)} long. Upload the original again."
    )


def _retry_preset(payload: dict[str, Any], video: Video) -> str | None:
    """None (the configured preset) on the first try; a faster one after failed tries."""
    tries = _previous_tries(payload, video)
    if tries <= 0:
        return None
    return faster_preset(encode_preset(), RETRY_PRESETS[min(tries, len(RETRY_PRESETS)) - 1])


def _encode_and_finish(
    session: Session, video: Video, original: Path, info: ProbeResult, preset: str | None = None
) -> None:
    video_dir = storage.ensure_video_dir(video.workspace_id, video.id)
    encoded_path = video_dir / storage.ENCODED_NAME
    thumb_path = video_dir / storage.THUMB_NAME
    encoded = encode_for_platforms(
        original,
        encoded_path,
        timeout_s=encode_timeout(info.duration_s),  # settings: base + per media second, capped
        source_probe=info,
        preset=preset,
    )
    if encoded.duration_s < TRUNCATION_RATIO * info.duration_s - TRUNCATION_SLACK_S:
        # ffmpeg reads what it can and exits 0 on a file whose end is missing (partial copy).
        encoded_path.unlink(missing_ok=True)
        _fail_video(session, video, _damaged_text(info.duration_s, encoded.duration_s))
        session.commit()
        return
    make_thumbnail(encoded_path, thumb_path, at_s=min(1.0, encoded.duration_s / 2))
    session.refresh(video)
    video.encoded_path = storage.rel_path(encoded_path)
    video.encoded_size_bytes = encoded_path.stat().st_size
    video.thumbnail_path = storage.rel_path(thumb_path)
    video.duration_s = encoded.duration_s
    video.width = encoded.width
    video.height = encoded.height
    video.fps = float(TARGET_FPS)  # constant by construction (the encode forces 30 fps)
    video.error = None
    if video.status == VideoStatus.PROCESSING:  # the user may have archived it meanwhile
        video.status = VideoStatus.READY
    note = (
        " The source is landscape, so it was fitted into the vertical frame with black bars."
        if info.is_landscape
        else ""
    )
    if info.is_hdr or info.is_wide_gamut_sdr:
        note += " The HDR picture was converted to standard colours."
    note = Message(
        "message.join",
        note,
        {
            "parts": [
                *([Message("import.landscape", "")] if info.is_landscape else []),
                *([Message("import.hdr", "")] if info.is_hdr or info.is_wide_gamut_sdr else []),
            ]
        },
    )
    log_activity(
        session,
        video.workspace_id,
        ActivityLevel.INFO,
        "import",
        Message(
            "import.ready",
            f"Video '{_video_label(video)}' is ready ({_mmss(encoded.duration_s)}).{note}",
            {"p0": _video_label(video), "p1": _mmss(encoded.duration_s), "p2": note},
        ),
        video_id=video.id,
        source_id=video.source_id,
        source_width=info.width,
        source_height=info.height,
    )
    kinds.enqueue_plan_workspace(session, video.workspace_id)
    session.commit()


def _mark_duplicate(session: Session, video: Video, original: Video) -> None:
    video.status = VideoStatus.DUPLICATE
    video.duplicate_of_id = original.id
    video.error = None
    log_activity(
        session,
        video.workspace_id,
        ActivityLevel.INFO,
        "import",
        Message(
            "import.duplicate",
            f"Video '{_video_label(video)}' is the same as '{_video_label(original)}' already in the library; "
            "it will not be published twice.",
            {"p0": _video_label(video), "p1": _video_label(original)},
        ),
        video_id=video.id,
        duplicate_of_id=original.id,
        source_id=video.source_id,
    )
    # The original may already have posts scheduled to the platform/account the duplicate came
    # from, where the content now exists: the engine cancels them in this transaction (contract C2).
    session.flush()
    engine_actions.cancel_conflicting_posts(session, original.id, actor="system")


def _original_file(video: Video) -> Path | None:
    try:
        p = storage.abs_path(video.original_path)
    except ValueError:
        return None
    return p if p is not None and p.is_file() else None


def _duration_problem(duration_s: float) -> str | None:
    if duration_s < MIN_DURATION_S:
        return f"The video is too short ({duration_s:.1f} s); platforms need at least {MIN_DURATION_S:g} s."
    if duration_s > MAX_DURATION_S:
        return (
            f"The video is too long ({_mmss(duration_s)}); the longest any platform accepts is "
            f"{_mmss(MAX_DURATION_S)}."
        )
    return None


# =========================================================================== shared helpers


def retry_failed_video(session: Session, video: Video, *, actor: str = "user") -> None:
    """Put a FAILED video back in the pipeline (for a dashboard "retry" button)."""
    if video.status != VideoStatus.FAILED:
        raise UserError("err.not_failed", "Only failed videos can be retried.")
    if not _requeue(session, video):
        raise UserError("err.original_gone", "The original file is gone; upload the video again.")
    video.error = None
    log_activity(
        session,
        video.workspace_id,
        ActivityLevel.INFO,
        "import",
        Message("import.retry", f"Retrying video '{_video_label(video)}'", {"p0": _video_label(video)}),
        actor=actor,
        video_id=video.id,
    )


def release_duplicate(session: Session, video: Video, *, actor: str = "user") -> None:
    """ "Not a duplicate" (for a dashboard button): a video that was taken for a copy of another one
    is processed on its own, and the duplicate check is skipped for it from now on. The caller
    commits."""
    if video.status != VideoStatus.DUPLICATE:
        raise UserError("err.not_duplicate", "Only videos marked as duplicates can be released.")
    video.overrides = {**(video.overrides or {}), "skip_duplicate_check": True}
    video.duplicate_of_id = None
    if not _requeue(session, video):
        raise UserError("err.original_gone", "The original file is gone; upload the video again.")
    video.error = None
    log_activity(
        session,
        video.workspace_id,
        ActivityLevel.INFO,
        "import",
        Message(
            "import.released",
            f"Video '{_video_label(video)}' was released from its duplicate mark; it is being processed.",
            {"p0": _video_label(video)},
        ),
        actor=actor,
        video_id=video.id,
    )


def _requeue(session: Session, video: Video) -> bool:
    """Send a video back through the pipeline: processing when its original is still on disk,
    a new download otherwise. False when neither is possible."""
    if _original_file(video) is not None:
        video.status = VideoStatus.PROCESSING
        kinds.enqueue_process_video(session, video.id)
    elif video.external_id and video.source_id:
        video.status = VideoStatus.IMPORTING
        kinds.enqueue_import_video(session, video.id)
    else:
        return False
    return True


def _retry_or_fail(
    session: Session,
    video: Video,
    message: str,
    kind: str,
    payload: dict[str, Any],
    retry_after: float | None = None,
) -> None:
    """Commit the retry state and raise RetryLater, or mark the video failed when the job's
    attempts are used up. RetryLater does not consume queue attempts, so the count is kept in
    the job payload."""
    if _lock_status(session, video.id) not in _ACTIVE:
        session.commit()  # the video was archived (or finished) meanwhile: nothing to retry
        return
    message = diagnostic(message, "import.problem")
    job = _current_job(session, kind, video.id)
    tries = _previous_tries(payload, video) + 1
    max_tries = job.max_attempts if job is not None else DEFAULT_MAX_TRIES.get(kind, 3)
    if tries >= max_tries:
        _fail_video(
            session,
            video,
            Message(
                "import.giveup",
                f"{message} (gave up after {tries} attempts)",
                {"p0": diagnostic(message), "p1": tries},
            ),
        )
        session.commit()
        return
    delay = max(default_backoff(tries).total_seconds(), float(retry_after or 0))
    video.error = Message(
        "import.retry_error",
        f"Attempt {tries} of {max_tries} failed: {message} Retrying in about {_human(delay)}.",
        {"p0": tries, "p1": max_tries, "p2": diagnostic(message), "p3": _human(delay)},
    )[:2000]
    if job is not None:
        job.payload = {**(job.payload or {}), "retries": tries}
    session.commit()
    raise RetryLater(delay, message[:500])


def _previous_tries(payload: dict[str, Any], video: Video) -> int:
    """Failed tries so far: from the job payload, or from the error text written by the previous
    try when the payload could not be updated (e.g. the running job row was not visible)."""
    from_payload = int(payload.get("retries") or 0)
    m = _ATTEMPT_RE.match(video.error or "")
    return max(from_payload, int(m.group(1)) if m else 0)


def _current_job(session: Session, kind: str, video_id: int) -> Job | None:
    return session.execute(
        select(Job)
        .where(Job.kind == kind, Job.dedupe_key == f"{kind}:{video_id}", Job.status == JobStatus.RUNNING)
        .order_by(Job.id.desc())
        .limit(1)
    ).scalar()


def _fail_video(session: Session, video: Video, message: str) -> bool:
    """Mark an importing/processing video FAILED. Returns False (and changes nothing) when the
    user moved it on meanwhile, for example archived it. Videos that were taken for duplicates of
    this one get their own processing back, since there is no original after all."""
    if _lock_status(session, video.id) not in _ACTIVE:
        log.info("video %s is no longer being imported; not marking it failed", video.id)
        return False
    message = diagnostic(message, "import.problem")
    video.status = VideoStatus.FAILED
    video.error = message[:2000]
    log_activity(
        session,
        video.workspace_id,
        ActivityLevel.ERROR,
        "import",
        Message(
            "import.failed",
            f"Video '{_video_label(video)}' could not be imported: {message}",
            {"title": _video_label(video), "detail": diagnostic(message)},
        ),
        video_id=video.id,
        source_id=video.source_id,
    )
    _remove_partial_files(video)
    _release_duplicates_of(session, video)
    return True


def _release_duplicates_of(session: Session, failed: Video) -> None:
    """Videos marked DUPLICATE of `failed` were never published anywhere: process them again."""
    dependents = session.execute(
        select(Video)
        .where(Video.duplicate_of_id == failed.id, Video.status == VideoStatus.DUPLICATE)
        .order_by(Video.id)
        .with_for_update()
        .execution_options(populate_existing=True)  # never trust a copy loaded earlier
    ).scalars()
    for dependent in dependents:
        dependent.duplicate_of_id = None
        dependent.error = None
        if _requeue(session, dependent):
            log_activity(
                session,
                dependent.workspace_id,
                ActivityLevel.INFO,
                "import",
                Message(
                    "import.duplicate_recovered",
                    f"Video '{_video_label(dependent)}' was taken for a copy of '{_video_label(failed)}', which "
                    "failed, so it is being processed on its own.",
                    {"p0": _video_label(dependent), "p1": _video_label(failed)},
                ),
                video_id=dependent.id,
                duplicate_of_id=failed.id,
                source_id=dependent.source_id,
            )
        else:
            dependent.status = VideoStatus.FAILED
            dependent.error = Message(
                "import.duplicate_original_lost",
                "The video it duplicated failed and its own file is gone; upload it again.",
                {},
            )


def _remove_partial_files(video: Video) -> None:
    """Unfinished downloads of a video that will not be retried automatically."""
    try:
        video_dir = storage.media_root() / storage.video_dir_rel(video.workspace_id, video.id)
        for pattern in ("*.part", "*.ytdl", "*.temp"):
            for path in video_dir.glob(pattern):
                path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not remove partial files of video %s (%s)", video.id, type(exc).__name__)


# ---------------------------------------------------------------- disk space


def _expected_download_bytes(payload: dict[str, Any]) -> int | None:
    size = (payload.get("remote_extra") or {}).get("size")
    return int(size) if isinstance(size, int) and size > 0 else None


def _require_free_space(session: Session, video: Video, expected_bytes: int | None) -> None:
    """Defer the job (RetryLater, no attempt used) while the media disk is nearly full: the
    volume is shared with Postgres, and a failed copy/encode would only have to be redone."""
    need = max(MIN_FREE_BYTES, 2 * (expected_bytes or 0))
    if storage.disk_usage()[2] < need:
        _wait_for_disk(session, video)


def _wait_for_disk(session: Session, video: Video) -> NoReturn:
    message = Message(
        "import.disk_wait",
        "Waiting for free disk space on the server; this video continues automatically.",
        {},
    )
    if video.error != message:
        video.error = message
        text = Message(
            "import.disk_full",
            f"The server's disk is nearly full, so video '{_video_label(video)}' is waiting.",
            {"p0": _video_label(video)},
        )
        log_activity(
            session,
            video.workspace_id,
            ActivityLevel.WARNING,
            "import",
            text,
            video_id=video.id,
            source_id=video.source_id,
        )
        _notify(
            text + " Free some space or raise the volume size.", video.workspace_id, "warning", "disk_low"
        )
    session.commit()
    raise RetryLater(DISK_WAIT_S, "disk full")


def _is_disk_full(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT)


def _log_unexpected(exc: BaseException, context: str) -> None:
    """Log a bug with its stack but without the exception text, which may embed URLs."""
    frames = "".join(traceback.format_tb(exc.__traceback__))
    log.error("%s failed with %s\n%s", context, type(exc).__name__, frames)


def _source_label(source: Source) -> str:
    return source.name or f"{source.platform or source.kind} source #{source.id}"


def _video_label(video: Video) -> str:
    text = (video.title or video.caption or "").strip().splitlines()
    return (text[0][:60] if text else "") or f"#{video.id}"


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return ensure_aware(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _mmss(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _human(seconds: float) -> str:
    if seconds < 90:
        return Message("duration.seconds", f"{int(seconds)} s", {"n": int(seconds)})
    if seconds < 5400:
        return Message("duration.minutes", f"{int(round(seconds / 60))} min", {"n": int(round(seconds / 60))})
    return Message("duration.hours", f"{seconds / 3600:.1f} h", {"n": f"{seconds / 3600:.1f}"})
