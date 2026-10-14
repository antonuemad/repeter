"""Unofficial import with yt-dlp (TikTok and YouTube), PLAN.md sections 3.3 and 7.1.

Isolation rules:
- This module never reads OAuth tokens, ConnectedAccount rows or platform API clients. It only
  receives public URLs, an optional proxy and an optional cookies file.
- It is used only for sources whose owner explicitly acknowledged the unofficial path
  (Source.unofficial_ack_at); the importer enforces that before calling in.
- Only TikTok and YouTube URLs and extractors are allowed, so a user-supplied link can never
  make the server fetch arbitrary addresses through yt-dlp's generic extractor.
- Requests are slow and few: sleeps between requests/downloads and modest retries.
"""

from __future__ import annotations

import itertools
import logging
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from yt_dlp import YoutubeDL
from yt_dlp.networking.exceptions import HTTPError as YtHTTPError
from yt_dlp.networking.exceptions import TransportError
from yt_dlp.utils import DownloadError, ExtractorError, GeoRestrictedError, UnsupportedError, YoutubeDLError

from repeater.config import get_settings
from repeater.platforms.base import (
    PermanentError,
    PlatformError,
    RateLimitedError,
    RemoteVideo,
    RetryableError,
)

log = logging.getLogger("repeater.ytdlp")

ALLOWED_HOSTS = ("tiktok.com", "youtube.com", "youtu.be")
# Regexes matched (case-insensitively, whole name) against yt-dlp extractor names.
ALLOWED_EXTRACTORS = [
    "tiktok",
    "tiktok:user",
    r"vm\.tiktok",
    "youtube",
    "youtube:tab",
    "youtube:user",
    "youtubeytbe",
]
SOCKET_TIMEOUT_S = 30
NETWORK_RETRIES = 3
EXTRACTOR_RETRIES = 2
SLEEP_BETWEEN_REQUESTS_S = 1.0
SLEEP_BEFORE_DOWNLOAD_S = (2.0, 6.0)  # random sleep range before each download
MAX_FILESIZE = 4 * 1024**3
DEFAULT_LIST_LIMIT = 30
# Best video up to 1080p, preferring H.264 in MP4 with M4A audio. yt-dlp's own format
# preference (which ranks TikTok's watermarked "download" format low) still applies first.
FORMAT = "bv*+ba/b"
FORMAT_SORT = ["res:1080", "vcodec:h264", "ext:mp4:m4a"]

FALLBACK_HINT = "Upload the original file or add it to your Google Drive folder instead."
_YOUTUBE_CHANNEL_PATH = re.compile(r"^/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)/?$")
_HTTP_STATUS_RE = re.compile(r"http error (\d{3})")
_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@")


# --------------------------------------------------------------------------- configuration


def options_for_source(source_id: int, config: dict[str, Any] | None) -> tuple[str | None, Path | None]:
    """(proxy, cookies_file) for a source: config["proxy"] overrides settings.ytdlp_proxy; the
    cookies file is settings.ytdlp_cookies_dir/source-{id}.txt when it exists."""
    settings = get_settings()
    proxy = str((config or {}).get("proxy") or "").strip() or settings.ytdlp_proxy.strip() or None
    cookies: Path | None = None
    if settings.ytdlp_cookies_dir:
        candidate = Path(settings.ytdlp_cookies_dir) / f"source-{int(source_id)}.txt"
        if candidate.is_file():
            cookies = candidate
    return proxy, cookies


def check_url(url: str) -> str:
    """Return the URL if it is an http(s) TikTok/YouTube link, else raise PermanentError."""
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    allowed = any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)
    if parsed.scheme not in ("http", "https") or not allowed:
        raise PermanentError(
            "Only TikTok and YouTube links can be imported this way.", code="unsupported_url"
        )
    return url.strip()


def normalize_profile_url(url: str) -> str:
    """YouTube channel links without a tab are pointed at the channel's Shorts tab."""
    url = check_url(url)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if (host == "youtube.com" or host.endswith(".youtube.com")) and _YOUTUBE_CHANNEL_PATH.match(parsed.path):
        return parsed._replace(path=parsed.path.rstrip("/") + "/shorts", query="", fragment="").geturl()
    return url


# --------------------------------------------------------------------------- public API


def list_profile(
    url: str,
    *,
    limit: int = DEFAULT_LIST_LIMIT,
    proxy: str | None = None,
    cookies_file: Path | None = None,
) -> list[RemoteVideo]:
    """Newest-first videos of a TikTok/YouTube profile using a flat (metadata-only) listing.
    `created_at` is set only when yt-dlp reports an exact timestamp."""
    if limit <= 0:
        return []
    target = normalize_profile_url(url)
    params = _base_params(proxy, cookies_file) | {
        "extract_flat": "in_playlist",
        "playlist_items": f"1:{int(limit)}",
        "lazy_playlist": True,
        "skip_download": True,
    }
    info = _run(lambda ydl: ydl.extract_info(target, download=False), params, proxy)
    if not isinstance(info, dict):
        raise RetryableError("yt-dlp returned no information for the profile.", code="empty")
    entries = info.get("entries")
    if entries is None:
        raise PermanentError(
            "This is a link to a single video. Enter the profile link instead "
            "(for example https://www.tiktok.com/@name or https://www.youtube.com/@name).",
            code="not_a_profile",
        )
    out: list[RemoteVideo] = []
    for entry in itertools.islice(entries, int(limit) * 2):
        remote = _remote_from_entry(entry)
        if remote is not None:
            out.append(remote)
        if len(out) >= limit:
            break
    return out


def download(
    video_url: str,
    dest_dir: Path,
    *,
    proxy: str | None = None,
    cookies_file: Path | None = None,
    basename: str = "original",
) -> Path:
    """Download one video into dest_dir/<basename>.<ext> (MP4 when merging is needed)."""
    target = check_url(video_url)
    dest_dir = Path(dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    params = download_params(dest_dir, proxy=proxy, cookies_file=cookies_file, basename=basename)
    info = _run(lambda ydl: ydl.extract_info(target, download=True), params, proxy)
    if not isinstance(info, dict):
        raise RetryableError("yt-dlp returned no information for the video.", code="empty")
    if info.get("_type") in ("playlist", "multi_video"):
        raise PermanentError(
            "The link points to a playlist or profile, not a single video.", code="not_a_video"
        )
    return _downloaded_file(info, dest_dir, basename)


def download_params(
    dest_dir: Path, *, proxy: str | None = None, cookies_file: Path | None = None, basename: str = "original"
) -> dict[str, Any]:
    """YoutubeDL options for a single-video download into dest_dir/<basename>.<ext>."""
    params = _base_params(proxy, cookies_file) | {
        "format": FORMAT,
        "format_sort": list(FORMAT_SORT),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "outtmpl": {"default": f"{basename}.%(ext)s"},
        "paths": {"home": str(dest_dir), "temp": str(dest_dir)},
        "max_filesize": MAX_FILESIZE,
        "sleep_interval": SLEEP_BEFORE_DOWNLOAD_S[0],
        "max_sleep_interval": SLEEP_BEFORE_DOWNLOAD_S[1],
        "overwrites": True,
    }
    ffmpeg = shutil.which(get_settings().ffmpeg_bin)
    if ffmpeg:
        params["ffmpeg_location"] = ffmpeg
    return params


# --------------------------------------------------------------------------- error mapping


def scrub(text: str, proxy: str | None = None) -> str:
    """Remove proxy credentials from a message before it is logged or shown."""
    if proxy:
        text = text.replace(proxy, "<proxy>")
    return _USERINFO_RE.sub(r"\1***@", text)


def classify_error(exc: BaseException, *, proxy: str | None = None) -> PlatformError:
    """Map a yt-dlp failure to RetryableError/RateLimitedError (worth trying again later) or
    PermanentError (will not work as-is). Messages suggest Drive or manual upload."""
    chain = _exception_chain(exc)
    text = scrub(" | ".join(str(e) for e in chain), proxy)
    low = text.lower().replace("’", "'")
    status = _http_status(chain, low)
    detail = _detail(str(exc), proxy)

    def permanent(message: str, code: str) -> PermanentError:
        return PermanentError(f"{message} {FALLBACK_HINT} ({detail})", code=code, status=status)

    def retryable(
        message: str, code: str, cls: type[RetryableError] = RetryableError, after: float | None = None
    ) -> RetryableError:
        return cls(f"{message} ({detail})", code=code, status=status, retry_after=after)

    if any(isinstance(e, UnsupportedError) for e in chain) or "unsupported url" in low:
        return permanent("This link is not a supported TikTok or YouTube link.", "unsupported_url")
    if "not a bot" in low:
        return retryable(
            "The platform asked to confirm this is not a bot (common on server IP addresses). "
            f"Will retry later; a cookies file or a residential proxy helps. {FALLBACK_HINT}",
            "bot_check",
            RateLimitedError,
            3600,
        )
    if status == 429 or "too many requests" in low or "rate limit" in low or "rate-limit" in low:
        return retryable(
            "The platform is rate limiting downloads; will retry later.",
            "rate_limited",
            RateLimitedError,
            1800,
        )
    if "private" in low:
        return permanent("The video or profile is private, so it cannot be downloaded.", "private")
    if _mentions_login(low):
        return permanent(
            "This video can only be downloaded while logged in (age-restricted or members-only).",
            "login_required",
        )
    if (
        any(isinstance(e, GeoRestrictedError) for e in chain)
        or "your country" in low
        or "geo restrict" in low
    ):
        return permanent(
            "The video is not available from the server's region; a proxy in another country may help.",
            "geo_restricted",
        )
    if "requested format is not available" in low:
        return permanent(
            "No downloadable version up to 1080p was offered for this video.", "format_unavailable"
        )
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return PermanentError(
            "ffmpeg is not installed on the server, so the download cannot be merged.", code="ffmpeg"
        )
    if status in (404, 410) or _mentions_removed(low):
        return permanent("The video is no longer available (removed, deleted or blocked).", "removed")
    if status is not None and status >= 500:
        return retryable("The platform had a temporary server error; will retry later.", "server_error")
    if any(isinstance(e, TransportError) for e in chain) or _mentions_network(low):
        return retryable("Network error while talking to the platform; will retry later.", "network")
    if status in (401, 403):
        return retryable(
            "The platform blocked the request (HTTP 403). Will retry later; a proxy or cookies file may help.",
            "blocked",
        )
    return retryable("The unofficial download failed; will retry later.", "unknown")


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain and len(chain) < 6:
        chain.append(current)
        nxt: BaseException | None = None
        if isinstance(current, DownloadError) and current.exc_info and len(current.exc_info) > 1:
            nxt = current.exc_info[1]
        elif isinstance(current, ExtractorError):
            nxt = current.cause if isinstance(current.cause, BaseException) else None
            if nxt is None and current.exc_info and current.exc_info[1] is not current:
                nxt = current.exc_info[1]
        nxt = nxt or current.__cause__
        current = nxt if isinstance(nxt, BaseException) else None
    return chain


def _http_status(chain: list[BaseException], low: str) -> int | None:
    for e in chain:
        if isinstance(e, YtHTTPError):
            return int(e.status)
    m = _HTTP_STATUS_RE.search(low)
    return int(m.group(1)) if m else None


def _mentions_login(low: str) -> bool:
    words = (
        "login required",
        "log in",
        "login to",
        "sign in",
        "age-restricted",
        "age restricted",
        "confirm your age",
    )
    words += (
        "inappropriate for some users",
        "members-only",
        "members only",
        "join this channel",
        "requires authentication",
    )
    return any(w in low for w in words)


def _mentions_removed(low: str) -> bool:
    words = ("video unavailable", "has been removed", "been deleted", "no longer available", "does not exist")
    words += ("account has been terminated", "copyright", "this video is unavailable", "not available")
    return any(w in low for w in words)


def _mentions_network(low: str) -> bool:
    words = ("timed out", "timeout", "connection", "network", "temporary failure", "name resolution")
    words += ("ssl", "incompleteread", "incomplete read", "reset by peer", "unable to download")
    return any(w in low for w in words)


def _detail(message: str, proxy: str | None) -> str:
    first = scrub(message, proxy).strip().splitlines()[0] if message.strip() else ""
    first = first.removeprefix("ERROR: ").strip()
    return f"yt-dlp: {first[:200]}" if first else "yt-dlp: no details"


# --------------------------------------------------------------------------- internals


class _Logger:
    """Routes yt-dlp output to logging with proxy credentials removed."""

    def __init__(self, proxy: str | None) -> None:
        self._proxy = proxy

    def debug(self, msg: str) -> None:
        log.debug("%s", scrub(str(msg), self._proxy))

    def info(self, msg: str) -> None:
        log.debug("%s", scrub(str(msg), self._proxy))

    def warning(self, msg: str) -> None:
        log.warning("%s", scrub(str(msg), self._proxy))

    def error(self, msg: str) -> None:
        # Errors also surface as exceptions, which the caller classifies and reports.
        log.info("%s", scrub(str(msg), self._proxy))


def _base_params(proxy: str | None, cookies_file: Path | None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _Logger(proxy),
        "socket_timeout": SOCKET_TIMEOUT_S,
        "retries": NETWORK_RETRIES,
        "fragment_retries": NETWORK_RETRIES,
        "extractor_retries": EXTRACTOR_RETRIES,
        "sleep_interval_requests": SLEEP_BETWEEN_REQUESTS_S,
        "allowed_extractors": list(ALLOWED_EXTRACTORS),
    }
    if proxy:
        params["proxy"] = proxy
    if cookies_file:
        params["cookiefile"] = str(cookies_file)
    return params


def _run(fn: Callable[[YoutubeDL], Any], params: dict[str, Any], proxy: str | None) -> Any:
    try:
        with YoutubeDL(params) as ydl:
            return fn(ydl)
    except PlatformError:
        raise
    except YoutubeDLError as exc:
        err = classify_error(exc, proxy=proxy)
        log.warning("yt-dlp failed (%s): %s", err.code, err.message)
        raise err from None
    except OSError as exc:
        raise RetryableError(
            f"Could not write the downloaded file ({type(exc).__name__}).", code="io"
        ) from None
    except Exception as exc:  # noqa: BLE001 - extractor bugs surface as arbitrary exceptions
        log.warning("yt-dlp crashed: %s: %s", type(exc).__name__, scrub(str(exc), proxy)[:300])
        raise RetryableError(
            f"The unofficial download failed unexpectedly ({type(exc).__name__}).", code="crash"
        ) from None


def _remote_from_entry(entry: Any) -> RemoteVideo | None:
    if not isinstance(entry, dict) or entry.get("_type") in ("playlist", "multi_video"):
        return None
    video_id = str(entry.get("id") or "").strip()
    url = str(entry.get("webpage_url") or entry.get("url") or "").strip()
    if not url.startswith(("http://", "https://")) and video_id:
        ie_key = str(entry.get("ie_key") or entry.get("extractor_key") or "").lower()
        url = f"https://www.youtube.com/watch?v={video_id}" if ie_key.startswith("youtube") else ""
    if not video_id or not url or len(video_id) > 128:
        return None
    title = str(entry.get("title") or "").strip()
    description = str(entry.get("description") or "").strip()
    duration = entry.get("duration")
    return RemoteVideo(
        external_id=video_id,
        created_at=_timestamp(entry.get("timestamp")),
        title=title,
        caption=description or title,
        permalink=url,
        duration_s=float(duration) if isinstance(duration, int | float) else None,
        extra={"ie_key": entry.get("ie_key")} if entry.get("ie_key") else {},
    )


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(value), UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _downloaded_file(info: dict[str, Any], dest_dir: Path, basename: str) -> Path:
    for item in info.get("requested_downloads") or []:
        fp = item.get("filepath") if isinstance(item, dict) else None
        if fp:
            p = Path(fp).resolve()
            if p.parent == dest_dir and p.is_file():
                return p
    candidates = [
        p
        for p in dest_dir.glob(f"{basename}.*")
        if p.is_file() and p.suffix.lower() not in (".part", ".ytdl", ".temp", ".tmp")
    ]
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    raise PermanentError(
        f"The download finished without a video file (it may be larger than the size limit). {FALLBACK_HINT}",
        code="no_file",
    )
