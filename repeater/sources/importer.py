"""Library feeding: detect new videos on a source, download them, fingerprint and encode them.

Flow (PLAN.md sections 3 and 5.6):
    poll_source(source)   -> new Video rows (status importing) + import_video jobs
    import_video(video)   -> original file on disk (status processing) + process_video job
    process_video(video)  -> duplicate check, platform encode, thumbnail (status ready)

These functions commit their own progress. They are called from the job handlers in
repeater.sources.jobs, and a worker usually rolls back on exceptions, so state that must
survive a retry (errors, retry counters, source failure counters) is committed first.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from repeater import notify as notify_mod
from repeater.accounts import AccountUnavailable, fresh_context, mark_needs_reconnect
from repeater.activity import log_activity
from repeater.jobs import kinds
from repeater.media import storage
from repeater.media.encode import EncodeError, EncodeTimeout, encode_for_platforms, make_thumbnail
from repeater.media.fingerprint import MAX_DURATION_DIFF_S, is_duplicate, phash_frames, sha256_file
from repeater.media.probe import MediaToolError, ProbeResult, probe
from repeater.models import (
    ActivityLevel,
    ConnectedAccount,
    Job,
    JobStatus,
    Platform,
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
from repeater.sources import ytdlp
from repeater.timeutil import ensure_aware, utcnow

log = logging.getLogger("repeater.sources")

# Items requested from a source on each regular poll (fewer on the unofficial path).
POLL_LIST_LIMIT = 50
UNOFFICIAL_LIST_LIMIT = 30
# Upper bound for config["backfill_count"] (existing videos imported on the first poll).
MAX_BACKFILL = 200
# config["seen_ids"] keeps this many most recent external ids.
SEEN_IDS_CAP = 500
# A source is paused after this many failed polls in a row (PLAN.md 3.3 rule 3).
MAX_CONSECUTIVE_FAILURES = 3
# The unofficial path is polled at most every 30 minutes (PLAN.md 3.3 rule 3).
YTDLP_MIN_POLL_MINUTES = 30
PLATFORM_API_PLATFORMS = (Platform.INSTAGRAM, Platform.FACEBOOK)
YTDLP_PLATFORMS = (Platform.TIKTOK, Platform.YOUTUBE)
ENCODE_TIMEOUT_S = 900
# Shortest/longest video any target platform accepts (see platforms.base.LIMITS).
MIN_DURATION_S = min(lim.min_duration_s for lim in LIMITS.values())
MAX_DURATION_S = max(lim.max_duration_s for lim in LIMITS.values())

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
    now = now or utcnow()
    source.last_polled_at = now
    if source.kind == SourceKind.YTDLP and source.unofficial_ack_at is None:
        return _block_unacknowledged(session, source)
    try:
        remotes = _detect(session, source)
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the source
        message = _failure_text(session, source, exc)
        _record_failure(session, source, message)
        session.commit()
        return PollOutcome("failed", message=message)
    created = _ingest(session, source, remotes, now)
    source.last_success_at = now
    source.last_error = None
    source.consecutive_failures = 0
    session.commit()
    return PollOutcome("ok", listed=len(remotes), created=created)


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
    message = (
        "Unofficial import (yt-dlp) is off for this source until you confirm you own the content "
        "and accept the risk. " + FALLBACK_HINT
    )
    if source.last_error != message:
        log_activity(
            session,
            source.workspace_id,
            ActivityLevel.WARNING,
            "source",
            f"Source '{_source_label(source)}' was not checked: {message}",
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

    if source.kind == SourceKind.YTDLP and source.platform not in YTDLP_PLATFORMS:
        raise SourceConfigError("Unofficial import supports TikTok and YouTube sources only.")
    if source.kind == SourceKind.YTDLP and source.connected_account_id is None:
        profile_url = str(config.get("profile_url") or "").strip()
        if not profile_url:
            raise SourceConfigError("No profile link is set for this source.")
        proxy, cookies = ytdlp.options_for_source(source.id, config)
        limit = max(UNOFFICIAL_LIST_LIMIT, backfill)
        return ytdlp.list_profile(profile_url, limit=limit, proxy=proxy, cookies_file=cookies)

    platform = _lister_platform(source)
    account = _source_account(source, platform)
    ctx = fresh_context(session, account)
    limit = max(POLL_LIST_LIMIT, backfill)
    return get_lister(platform).list_recent(ctx, since=since, limit=limit, config=config)


def _lister_platform(source: Source) -> Platform:
    if source.kind == SourceKind.DRIVE:
        return Platform.GDRIVE
    if source.kind == SourceKind.PLATFORM_API:
        if source.platform not in PLATFORM_API_PLATFORMS:
            raise SourceConfigError(
                "Only Instagram and Facebook offer an official download. " + FALLBACK_HINT
            )
        return Platform(source.platform)
    account = source.connected_account
    if account is None or account.platform not in YTDLP_PLATFORMS or account.platform != source.platform:
        raise SourceConfigError("The linked account must be the TikTok or YouTube account of this source.")
    return Platform(account.platform)


def _source_account(source: Source, platform: Platform) -> ConnectedAccount:
    account = source.connected_account
    if account is None:
        raise SourceConfigError("No connected account is linked to this source. Connect the account first.")
    if account.workspace_id != source.workspace_id or account.platform != platform:
        raise SourceConfigError("The linked account does not match this source's platform.")
    return account


def _ingest(session: Session, source: Source, remotes: list[RemoteVideo], now: datetime) -> int:
    config = dict(source.config or {})
    remotes = [r for r in remotes if r.is_video and r.external_id and len(str(r.external_id)) <= 128]
    seen = [str(x) for x in config.get("seen_ids") or []]
    baseline = _parse_iso(config.get("baseline_at"))
    if baseline is None:
        chosen = _newest(remotes, _backfill_count(config))
        config["baseline_at"] = now.isoformat()
    else:
        seen_set = set(seen)
        chosen = [r for r in remotes if _is_new(r, baseline, seen_set)]
    created = _create_videos(session, source, chosen)
    config["seen_ids"] = _merge_seen([str(r.external_id) for r in remotes], seen)
    source.config = config  # a new dict, so SQLAlchemy writes the JSONB column
    return created


def _is_new(remote: RemoteVideo, baseline: datetime, seen: set[str]) -> bool:
    if str(remote.external_id) in seen:
        return False
    created = ensure_aware(remote.created_at)
    # Without a timestamp (flat yt-dlp listings) "not seen before" is all we can go on.
    return created is None or created > baseline


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


def _merge_seen(listed: list[str], previous: list[str]) -> list[str]:
    merged: list[str] = []
    marks: set[str] = set()
    for item in listed + previous:
        if item not in marks:
            marks.add(item)
            merged.append(item)
    return merged[:SEEN_IDS_CAP]


def _backfill_count(config: dict[str, Any]) -> int:
    try:
        return max(0, min(int(config.get("backfill_count") or 0), MAX_BACKFILL))
    except (TypeError, ValueError):
        return 0


def _create_videos(session: Session, source: Source, items: list[RemoteVideo]) -> int:
    """Insert Video rows oldest first; the partial unique index skips known videos."""
    platform = Platform.GDRIVE if source.kind == SourceKind.DRIVE else source.platform
    account_id = source.connected_account_id or _match_profile_account(session, source)
    created = 0
    for remote in _oldest_first(items):
        stmt = (
            pg_insert(Video)
            .values(
                workspace_id=source.workspace_id,
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
                index_elements=["workspace_id", "source_platform", "external_id"],
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
    source.last_error = message[:2000]
    source.consecutive_failures = (source.consecutive_failures or 0) + 1
    label = _source_label(source)
    if source.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        source.enabled = False
        hint = (
            f" {FALLBACK_HINT}" if source.kind == SourceKind.YTDLP and "Google Drive" not in message else ""
        )
        text = (
            f"Source '{label}' was paused after {source.consecutive_failures} failed checks in a row. "
            f"Last error: {message}{hint}"
        )
        log_activity(session, source.workspace_id, ActivityLevel.ERROR, "source", text, source_id=source.id)
        _notify(text, source.workspace_id, "error")
    elif source.consecutive_failures == 1:
        log_activity(
            session,
            source.workspace_id,
            ActivityLevel.WARNING,
            "source",
            f"Checking source '{label}' for new videos failed: {message}",
            source_id=source.id,
        )


def _notify(text: str, workspace_id: int | None, level: str) -> None:
    try:
        notify_mod.notify(text, workspace_id=workspace_id, level=level)
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
    try:
        path = _download_original(session, video, payload)
    except RetryableError as exc:
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
        _log_unexpected(exc, f"import of video {video.id}")
        _retry_or_fail(
            session, video, f"Unexpected error ({type(exc).__name__})", kinds.IMPORT_VIDEO, payload
        )
        return
    video.original_path = storage.rel_path(path)
    video.size_bytes = path.stat().st_size
    video.status = VideoStatus.PROCESSING
    video.error = None
    kinds.enqueue_process_video(session, video.id)
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
        if not username and source.connected_account is not None:
            username = str((source.connected_account.extra or {}).get("username") or "").lstrip("@") or None
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


def process_video(session: Session, video_id: int, payload: dict[str, Any] | None = None) -> None:
    """Fingerprint a PROCESSING video, mark duplicates, otherwise encode it and make it READY."""
    payload = payload or {}
    video = session.get(Video, video_id)
    if video is None or video.status != VideoStatus.PROCESSING:
        return
    original = _original_file(video)
    if original is None:
        _fail_video(session, video, "The original file is missing. Import or upload the video again.")
        session.commit()
        return
    try:
        info = probe(original)
        problem = _duration_problem(info.duration_s)
        if problem:
            _fail_video(session, video, problem)
            session.commit()
            return
        hashes = phash_frames(original, info.duration_s)
        video.sha256 = sha256_file(original)
        video.phash = " ".join(hashes) or None
        video.size_bytes = original.stat().st_size
        video.duration_s = info.duration_s
        duplicate = find_duplicate(session, video, info.duration_s, hashes)
        if duplicate is not None:
            _mark_duplicate(session, video, duplicate)
            session.commit()
            return
        session.commit()  # keep fingerprints; release the row before the long encode
        _encode_and_finish(session, video, original, info)
    except ValueError as exc:
        _fail_video(session, video, f"The file is {exc}" if str(exc).startswith("not a video") else str(exc))
        session.commit()
    except EncodeTimeout as exc:
        _retry_or_fail(session, video, str(exc).split(":")[0] + ".", kinds.PROCESS_VIDEO, payload)
    except EncodeError as exc:
        # ffmpeg ran and rejected the input; the same file will fail the same way again.
        _fail_video(session, video, f"Encoding failed: {exc}")
        session.commit()
    except MediaToolError as exc:
        # The tools themselves are missing or hung (not the file's fault): try again later.
        _retry_or_fail(session, video, str(exc), kinds.PROCESS_VIDEO, payload)
    except Exception as exc:  # noqa: BLE001 - unknown errors are retried, then reported
        session.rollback()
        _log_unexpected(exc, f"processing of video {video_id}")
        video = session.get(Video, video_id)
        if video is not None:
            _retry_or_fail(
                session, video, f"Unexpected error ({type(exc).__name__})", kinds.PROCESS_VIDEO, payload
            )


def find_duplicate(session: Session, video: Video, duration_s: float, hashes: list[str]) -> Video | None:
    """A READY/ARCHIVED video of the same workspace with the same bytes or the same picture."""
    base = select(Video).where(
        Video.workspace_id == video.workspace_id,
        Video.id != video.id,
        Video.status.in_([VideoStatus.READY, VideoStatus.ARCHIVED]),
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
        ).order_by(Video.id)
    ).scalars()
    for other in candidates:
        if is_duplicate(hashes, duration_s, (other.phash or "").split(), other.duration_s):
            return other
    return None


def _encode_and_finish(session: Session, video: Video, original: Path, info: ProbeResult) -> None:
    video_dir = storage.ensure_video_dir(video.workspace_id, video.id)
    encoded_path = video_dir / storage.ENCODED_NAME
    thumb_path = video_dir / storage.THUMB_NAME
    encoded = encode_for_platforms(original, encoded_path, timeout_s=ENCODE_TIMEOUT_S, source_probe=info)
    make_thumbnail(encoded_path, thumb_path, at_s=min(1.0, encoded.duration_s / 2))
    session.refresh(video)
    video.encoded_path = storage.rel_path(encoded_path)
    video.encoded_size_bytes = encoded_path.stat().st_size
    video.thumbnail_path = storage.rel_path(thumb_path)
    video.duration_s = encoded.duration_s
    video.width = encoded.width
    video.height = encoded.height
    video.fps = encoded.fps
    video.error = None
    if video.status == VideoStatus.PROCESSING:  # the user may have archived it meanwhile
        video.status = VideoStatus.READY
    note = (
        " The source is landscape, so it was fitted into the vertical frame with black bars."
        if info.is_landscape
        else ""
    )
    log_activity(
        session,
        video.workspace_id,
        ActivityLevel.INFO,
        "import",
        f"Video '{_video_label(video)}' is ready ({_mmss(encoded.duration_s)}).{note}",
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
        f"Video '{_video_label(video)}' is the same as '{_video_label(original)}' already in the library; "
        "it will not be published twice.",
        video_id=video.id,
        duplicate_of_id=original.id,
        source_id=video.source_id,
    )


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
        raise ValueError("Only failed videos can be retried.")
    if _original_file(video) is not None:
        video.status = VideoStatus.PROCESSING
        kinds.enqueue_process_video(session, video.id)
    elif video.external_id and video.source_id:
        video.status = VideoStatus.IMPORTING
        kinds.enqueue_import_video(session, video.id)
    else:
        raise ValueError("The original file is gone; upload the video again.")
    video.error = None
    log_activity(
        session,
        video.workspace_id,
        ActivityLevel.INFO,
        "import",
        f"Retrying video '{_video_label(video)}'",
        actor=actor,
        video_id=video.id,
    )


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
    job = _current_job(session, kind, video.id)
    tries = _previous_tries(payload, video) + 1
    max_tries = job.max_attempts if job is not None else DEFAULT_MAX_TRIES.get(kind, 3)
    if tries >= max_tries:
        _fail_video(session, video, f"{message} (gave up after {tries} attempts)")
        session.commit()
        return
    delay = max(default_backoff(tries).total_seconds(), float(retry_after or 0))
    video.error = f"Attempt {tries} of {max_tries} failed: {message} Retrying in about {_human(delay)}."[
        :2000
    ]
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


def _fail_video(session: Session, video: Video, message: str) -> None:
    video.status = VideoStatus.FAILED
    video.error = message[:2000]
    log_activity(
        session,
        video.workspace_id,
        ActivityLevel.ERROR,
        "import",
        f"Video '{_video_label(video)}' could not be imported: {message}",
        video_id=video.id,
        source_id=video.source_id,
    )


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
        return f"{int(seconds)} s"
    if seconds < 5400:
        return f"{int(round(seconds / 60))} min"
    return f"{seconds / 3600:.1f} h"
