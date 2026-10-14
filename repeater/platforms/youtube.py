"""YouTube Shorts publisher and channel lister (YouTube Data API v3).

Publishing uses the resumable upload protocol of videos.insert; YouTube classifies any vertical
or square video of up to 3 minutes as a Short, so there is no Shorts flag to set. Listing walks
the channel's uploads playlist (playlistItems.list, 1 quota unit per page) instead of
search.list, which has its own 100-calls/day bucket and indexing delays.

Once the last byte of the file has been sent, a lost answer no longer proves that no video exists:
publish() then raises UnknownOutcomeError carrying the resumable session URI, and poll() asks the
session (a finished one repeats its original response) instead of uploading the file again.

Docs: https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol
      https://developers.google.com/youtube/v3/docs/videos
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any, BinaryIO
from zoneinfo import ZoneInfo

import httpx

from repeater.models import Platform
from repeater.platforms import http
from repeater.platforms.base import (
    LIMITS,
    RULE_SETTINGS_DEFAULTS,
    AccountContext,
    AuthError,
    NotSupportedError,
    PermanentError,
    PlatformError,
    PublishOutcome,
    PublishRequest,
    PublishResult,
    RateLimitedError,
    RemoteVideo,
    RestartPublish,
    RetryableError,
    UnknownOutcomeError,
)
from repeater.timeutil import ensure_aware, utcnow

log = logging.getLogger("repeater.platforms.youtube")

API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SHORTS_URL = "https://www.youtube.com/shorts/{id}"
WATCH_URL = "https://www.youtube.com/watch?v={id}"

# Resumable chunks must be multiples of 256 KiB (except the last one) and all the same size.
CHUNK_GRANULARITY = 256 * 1024
CHUNK_SIZE = 8 * 1024 * 1024
UPLOAD_CONTENT_TYPE = "video/mp4"
RESUMABLE_STATUSES = frozenset({500, 502, 503, 504})
MAX_UPLOAD_RETRIES = 5
MAX_BACKOFF_SECONDS = 32.0
MAX_INLINE_RETRY_AFTER = 60.0

TITLE_MAX = 100
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500
ELLIPSIS = "\u2026"

SHORTS_MAX_SECONDS = 180.0
PRIVACY_VALUES = ("public", "unlisted", "private")

QUOTA_TZ = ZoneInfo("America/Los_Angeles")
# Start a little after midnight PT so the first retry does not race the quota reset.
QUOTA_RESET_MARGIN = timedelta(minutes=5)
UPLOAD_LIMIT_RETRY_SECONDS = 24 * 3600.0

PLAYLIST_PAGE_SIZE = 50
VIDEOS_BATCH_SIZE = 50
MAX_PLAYLIST_PAGES = 20
NOT_FOUND_POLL_LIMIT = 3
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

TITLE_REQUIRED_MESSAGE = (
    "Set a title for this video before it can be posted to YouTube (the video has no title and no "
    "caption text to take one from)."
)
NO_CHANNEL_MESSAGE = (
    "This Google account has no YouTube channel. Create a channel at youtube.com "
    "(or pick the Google account that owns the channel), then connect again."
)

_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded"})
_RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})

_REJECTION_HINTS = {
    "duplicate": "YouTube detected it as a duplicate of a video already on the channel",
    "copyright": "copyright",
    "claim": "a copyright claim",
    "length": "the video is too long for this channel",
    "inappropriate": "inappropriate content",
    "legal": "a legal issue",
    "termsOfUse": "a Terms of Service violation",
    "trademark": "a trademark issue",
    "uploaderAccountClosed": "the channel is closed",
    "uploaderAccountSuspended": "the channel is suspended",
}
_FAILURE_HINTS = {
    "codec": "unsupported codec",
    "conversion": "conversion failed",
    "emptyFile": "the file was empty",
    "invalidFile": "the file is not a valid video",
    "tooSmall": "the file is too small",
    "uploadAborted": "the upload was aborted",
}


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# --------------------------------------------------------------------------- API errors


def parse_api_error(resp: httpx.Response) -> tuple[str | None, str]:
    """Return (reason, message) from a Google API error body. Handles the YouTube format
    {"error": {"message", "errors": [{"reason"}]}} and the OAuth format {"error", "error_description"}."""
    try:
        body = resp.json()
    except ValueError:
        return None, ""
    if not isinstance(body, dict):
        return None, ""
    err = body.get("error")
    if isinstance(err, str):
        return err, _clean_message(body.get("error_description"))
    if not isinstance(err, dict):
        return None, ""
    reasons = [e.get("reason") for e in err.get("errors") or [] if isinstance(e, dict)]
    reasons = [r for r in reasons if isinstance(r, str) and r]
    known = [r for r in reasons if r in _QUOTA_REASONS | _RATE_REASONS | {"uploadLimitExceeded"}]
    reason = known[0] if known else (reasons[0] if reasons else None)
    return reason, _clean_message(err.get("message"))


def _clean_message(value: Any) -> str:
    """Google error messages sometimes embed HTML links; keep plain, short text."""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<[^>]+>", "", value)
    return " ".join(text.split())[:300]


def seconds_until_quota_reset(now: datetime | None = None) -> float:
    """YouTube daily quotas reset at midnight Pacific Time."""
    now = ensure_aware(now) or utcnow()
    local = now.astimezone(QUOTA_TZ)
    reset = datetime.combine(local.date() + timedelta(days=1), dtime(0), tzinfo=QUOTA_TZ)
    return (reset - now).total_seconds() + QUOTA_RESET_MARGIN.total_seconds()


def youtube_api_error(resp: httpx.Response, action: str) -> PlatformError:
    """Map a failed YouTube Data API response to our error classes. The reason decides first
    because YouTube reuses HTTP codes (uploadLimitExceeded is a 400, youtubeSignupRequired a 401)."""
    status = resp.status_code
    reason, detail = parse_api_error(resp)
    kw: dict[str, Any] = {"code": reason, "status": status}
    if reason in _QUOTA_REASONS:
        return RateLimitedError(
            "The YouTube API daily quota of this app is used up. "
            "Publishing resumes automatically after midnight Pacific Time.",
            retry_after=seconds_until_quota_reset(),
            **kw,
        )
    if reason == "uploadLimitExceeded":
        return RateLimitedError(
            "This YouTube channel reached its daily upload limit (YouTube does not publish the "
            "number; it counts uploads from YouTube itself too). Repeater will try again in 24 hours.",
            retry_after=UPLOAD_LIMIT_RETRY_SECONDS,
            **kw,
        )
    if reason in _RATE_REASONS:
        return RateLimitedError(
            f"YouTube is rate limiting requests ({action}); retrying shortly.",
            retry_after=http.retry_after_seconds(resp, 60.0),
            **kw,
        )
    if reason == "youtubeSignupRequired":
        return PermanentError(NO_CHANNEL_MESSAGE, **kw)
    if reason == "insufficientPermissions":
        return AuthError(
            "Repeater is missing YouTube permissions for this channel. Reconnect it and allow "
            "every permission on the Google consent screen.",
            **kw,
        )
    if reason == "forbidden":
        return PermanentError(f"YouTube refused the {action}: {detail or 'access forbidden'}", **kw)
    if status == 401:
        return AuthError("YouTube rejected the stored sign-in for this channel. Reconnect the channel.", **kw)
    suffix = f": {detail}" if detail else ""
    return http.classify_http_error(resp, f"YouTube {action} failed (HTTP {status}){suffix}", code=reason)


def _auth(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _json_object(resp: httpx.Response, action: str) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError as exc:
        raise RetryableError(f"YouTube {action}: unreadable response") from exc
    if not isinstance(data, dict):
        raise RetryableError(f"YouTube {action}: unexpected response")
    return data


def api_get(
    c: httpx.Client, access_token: str, path: str, params: dict[str, Any], action: str
) -> dict[str, Any]:
    """GET a YouTube Data API resource and return its JSON body, mapping failures."""
    try:
        resp = c.get(API_BASE + path, params=params, headers=_auth(access_token))
    except httpx.TransportError as exc:
        raise RetryableError(f"YouTube {action}: network error ({type(exc).__name__})") from exc
    if resp.status_code != 200:
        raise youtube_api_error(resp, action)
    return _json_object(resp, action)


def list_my_channels(c: httpx.Client, access_token: str) -> list[dict[str, Any]]:
    """channels.list(mine=true): the channels owned by the authorized identity."""
    data = api_get(
        c,
        access_token,
        "/channels",
        {"part": "snippet,contentDetails", "mine": "true", "maxResults": 50},
        "channel lookup",
    )
    return [item for item in data.get("items") or [] if isinstance(item, dict) and item.get("id")]


def channel_summary(channel: dict[str, Any]) -> dict[str, Any]:
    """The account fields Repeater stores for a channels.list item: display_name, avatar_url and
    extra (channel_id, uploads_playlist_id, custom_url). Shared by the OAuth connect flow and
    refresh_account_info so both read the channel the same way."""
    channel_id = str(channel["id"])
    snippet = channel.get("snippet") or {}
    related = (channel.get("contentDetails") or {}).get("relatedPlaylists") or {}
    thumbs = snippet.get("thumbnails") or {}
    avatar = next(
        (thumbs[k].get("url") for k in ("medium", "default", "high") if isinstance(thumbs.get(k), dict)), None
    )
    custom_url = snippet.get("customUrl")
    return {
        "display_name": snippet.get("title") or custom_url or channel_id,
        "avatar_url": avatar,
        "extra": {
            "channel_id": channel_id,
            "uploads_playlist_id": related.get("uploads"),
            "custom_url": custom_url,
        },
    }


def refresh_account_info(account: AccountContext) -> dict[str, Any]:
    """Re-read the connected channel with channels.list(mine=true) (1 quota unit). The YouTube
    Developer Policies (III.E.4.c) require stored API data to be refreshed or deleted within 30
    days; the engine calls this at least every 25 days and stores the result.

    Returns {"display_name", "avatar_url", "extra": {"channel_id", "uploads_playlist_id",
    "custom_url"}}. PermanentError (channelNotFound) when the signed-in Google identity no longer
    owns the channel."""
    wanted = str((account.extra or {}).get("channel_id") or account.external_id)
    with http.client() as c:
        channels = list_my_channels(c, account.tokens.access_token)
    for channel in channels:
        if str(channel["id"]) == wanted:
            return channel_summary(channel)
    raise PermanentError(
        "The YouTube channel is no longer available to the connected Google account. Reconnect the "
        "channel if it still exists.",
        code="channelNotFound",
    )


# --------------------------------------------------------------------------- text limits


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _clean_text(text: str | None) -> str:
    """Remove characters YouTube rejects in titles/descriptions (< and >), NULs and other C0
    controls (except newlines/tabs), and lone surrogates."""
    if not text:
        return ""
    text = text.encode("utf-8", "ignore").decode("utf-8")
    return "".join(ch for ch in text if ch not in "<>" and (ch in "\n\r\t" or ord(ch) >= 32))


_REGIONAL_INDICATORS = "".join(chr(cp) for cp in range(0x1F1E6, 0x1F200))


def _continues_cluster(ch: str) -> bool:
    """True for characters that attach to the previous one: joiners, variation selectors,
    emoji skin tones, tag characters and combining marks (e.g. Arabic harakat)."""
    cp = ord(ch)
    return (
        ch == "\u200d"
        or 0xFE00 <= cp <= 0xFE0F
        or 0x1F3FB <= cp <= 0x1F3FF
        or 0xE0020 <= cp <= 0xE007F
        or unicodedata.category(ch) in ("Mn", "Mc", "Me")
    )


def _safe_prefix(text: str, n: int) -> str:
    """The first n code points of text, shortened so no emoji sequence, flag or letter with
    its diacritics is split."""
    i = n
    while 0 < i < len(text) and (_continues_cluster(text[i]) or text[i - 1] == "\u200d"):
        i -= 1
    prefix = text[:i]
    trailing_ri = len(prefix) - len(prefix.rstrip(_REGIONAL_INDICATORS))
    if trailing_ri % 2:  # half of a flag (flags are pairs of regional indicators)
        prefix = prefix[:-1]
    return prefix


def _fit_utf16(text: str, budget: int) -> int:
    """How many leading code points fit in `budget` UTF-16 code units."""
    used = 0
    for i, ch in enumerate(text):
        used += 2 if ord(ch) > 0xFFFF else 1
        if used > budget:
            return i
    return len(text)


def truncate_title(text: str, limit: int = TITLE_MAX) -> str:
    """Limit a single-line title to `limit` characters. Characters are counted in UTF-16 code
    units (an emoji counts as 2), which is never more lenient than counting code points."""
    if _utf16_len(text) <= limit:
        return text
    cut = _safe_prefix(text, _fit_utf16(text, limit - len(ELLIPSIS)))
    space = cut.rfind(" ")
    if space >= len(cut) * 0.6:
        cut = cut[:space]
    return cut.rstrip() + ELLIPSIS


def truncate_bytes(text: str, limit: int = DESCRIPTION_MAX_BYTES) -> str:
    """Limit text to `limit` UTF-8 bytes without splitting a character or an emoji sequence."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    budget = limit - len(ELLIPSIS.encode("utf-8"))
    fitting = len(raw[:budget].decode("utf-8", "ignore"))
    return _safe_prefix(text, fitting).rstrip() + ELLIPSIS


def build_title(title: str | None, caption: str | None) -> str:
    """Title for videos.insert: no < or >, at most 100 characters. Falls back to the first
    non-empty caption line. Returns "" when the video has neither: there is deliberately no
    constant default title (YouTube API Developer Policies III.C.3), so such a post is not
    published until the user sets one."""
    candidates = [title or ""] + (caption or "").splitlines()
    for candidate in candidates:
        single_line = " ".join(_clean_text(candidate).split())
        if single_line:
            return truncate_title(single_line)
    return ""


def build_description(caption: str | None) -> str:
    return truncate_bytes(_clean_text(caption).strip())


def build_tags(raw: Iterable[Any] | str | None) -> list[str]:
    """Tags within YouTube's 500-character total: commas between tags count, and a tag with a
    space counts as if wrapped in quotes."""
    items = raw.split(",") if isinstance(raw, str) else list(raw or [])
    out: list[str] = []
    seen: set[str] = set()
    total = 0
    for item in items:
        if not isinstance(item, str):
            continue
        tag = " ".join(_clean_text(item).replace(",", " ").split()).lstrip("#").strip()
        if not tag or tag.casefold() in seen:
            continue
        cost = _utf16_len(tag) + (2 if " " in tag else 0) + (1 if out else 0)
        if total + cost > TAGS_MAX_CHARS:
            continue
        out.append(tag)
        seen.add(tag.casefold())
        total += cost
    return out


# --------------------------------------------------------------------------- misc helpers

_DURATION_RE = re.compile(
    r"^P(?:(?P<w>\d+)W)?(?:(?P<d>\d+)D)?"
    r"(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:[.,]\d+)?)S)?)?$"
)


def parse_iso_duration(value: str | None) -> float | None:
    """Parse an ISO 8601 duration such as PT1M5S or P1DT2H (contentDetails.duration)."""
    if not value or not isinstance(value, str):
        return None
    m = _DURATION_RE.match(value.strip().upper())
    if not m or value.strip().upper() in ("P", "PT"):
        return None
    parts = {k: float((v or "0").replace(",", ".")) for k, v in m.groupdict().items()}
    return parts["w"] * 604800 + parts["d"] * 86400 + parts["h"] * 3600 + parts["m"] * 60 + parts["s"]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return ensure_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def video_permalink(video_id: str, duration_s: float | None) -> str:
    if duration_s is not None and duration_s <= SHORTS_MAX_SECONDS:
        return SHORTS_URL.format(id=video_id)
    return WATCH_URL.format(id=video_id)


def effective_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    return {**RULE_SETTINGS_DEFAULTS["youtube"], **(settings or {})}


def settings_problems(settings: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if settings.get("privacy_status") not in PRIVACY_VALUES:
        problems.append("YouTube visibility must be public, unlisted or private.")
    if not isinstance(settings.get("self_declared_made_for_kids"), bool):
        problems.append("Choose whether these videos are made for kids (YouTube requires this declaration).")
    for key, label in (
        ("notify_subscribers", "Notify subscribers"),
        ("contains_synthetic_media", "Altered or synthetic content"),
    ):
        if not isinstance(settings.get(key), bool):
            problems.append(f"YouTube setting '{label}' must be on or off.")
    category = settings.get("category_id")
    if isinstance(category, bool) or not str(category).isdigit():
        problems.append("YouTube category must be a numeric category id (e.g. 22 = People & Blogs).")
    tags = settings.get("tags")
    if tags is not None and not isinstance(tags, str | list):
        problems.append("YouTube tags must be a list of words.")
    return problems


# --------------------------------------------------------------------------- resumable upload


class _SessionExpired(Exception):  # noqa: N818
    """The resumable session URI is gone (HTTP 404); the upload must start over."""


class _OutcomeUnknown(Exception):  # noqa: N818
    """The last byte of the file was (or may have been) delivered and the result is unclear: lost
    response, failed status queries, or an unreadable 2xx body. The video may exist on YouTube,
    so the upload must not start over; the session URI is how poll() finds out."""

    def __init__(self, session_uri: str, reason: str) -> None:
        super().__init__(reason)
        self.session_uri = session_uri
        self.reason = reason


class _Backoff:
    """Counts consecutive failures inside one publish() call and sleeps between attempts."""

    def __init__(self, what: str) -> None:
        self.what = what
        self.failures = 0

    def reset(self) -> None:
        self.failures = 0

    def fail(self, reason: str, resp: httpx.Response | None = None) -> None:
        self.failures += 1
        retry_after = http.retry_after_seconds(resp, 0.0) if resp is not None else 0.0
        if self.failures > MAX_UPLOAD_RETRIES or retry_after > MAX_INLINE_RETRY_AFTER:
            raise RetryableError(
                f"YouTube {self.what} kept failing ({reason}); Repeater will retry later.",
                retry_after=retry_after or None,
                status=resp.status_code if resp is not None else None,
            )
        delay = retry_after or min(2.0 ** (self.failures - 1), MAX_BACKOFF_SECONDS) + random.uniform(0, 1)
        log.info("YouTube %s interrupted (%s); retry %d in %.1fs", self.what, reason, self.failures, delay)
        _sleep(delay)


def _next_offset(resp: httpx.Response) -> int:
    """308 Resume Incomplete carries `Range: bytes=0-N` (bytes received); none = nothing yet."""
    m = re.match(r"^\s*bytes=(\d+)-(\d+)\s*$", resp.headers.get("range", ""))
    return int(m.group(2)) + 1 if m else 0


def _status_query(c: httpx.Client, access_token: str, session_uri: str, total: int) -> httpx.Response:
    """Empty PUT with `Content-Range: bytes */total`: asks the session how far the upload got.
    A finished session answers with its original response (the video resource, or the error)."""
    headers = {**_auth(access_token), "Content-Range": f"bytes */{total}"}
    return c.put(session_uri, content=b"", headers=headers)


@dataclass
class _ResumableUpload:
    client: httpx.Client
    access_token: str
    path: Path
    total: int
    # True once the request carrying the file's last byte has been issued (or the server reports
    # it has every byte): from then on the video may exist.
    final_sent: bool = field(default=False, init=False)
    # True once, after that, a reply was lost (network error, 5xx) or never asked for: from then on
    # a failure no longer proves that no video was created. A clear 4xx answer to the final chunk
    # itself still does (YouTube refused it).
    ambiguous: bool = field(default=False, init=False)

    def start(self, metadata: dict[str, Any], notify_subscribers: bool) -> str:
        """Open a resumable session and return its URI (Location header). The URI is a
        credential for the upload: it is never logged or put in a message."""
        params = {
            "uploadType": "resumable",
            "part": "snippet,status",
            "notifySubscribers": "true" if notify_subscribers else "false",
        }
        headers = {
            **_auth(self.access_token),
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(self.total),
            "X-Upload-Content-Type": UPLOAD_CONTENT_TYPE,
        }
        body = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
        backoff = _Backoff("upload start")
        while True:
            try:
                resp = self.client.post(UPLOAD_URL, params=params, headers=headers, content=body)
            except httpx.TransportError as exc:
                backoff.fail(f"network error {type(exc).__name__}")
                continue
            if resp.status_code in RESUMABLE_STATUSES:
                backoff.fail(f"HTTP {resp.status_code}", resp)
                continue
            if resp.status_code != 200:
                raise youtube_api_error(resp, "upload")
            location = resp.headers.get("location", "")
            if not location.startswith("https://"):
                raise RetryableError("YouTube did not return an upload session; will retry.")
            return location

    def run(self, session_uri: str, *, resume: bool = False) -> dict[str, Any]:
        """Send the file in chunks, resuming after interruptions. Returns the video resource.

        `resume=True` continues a session left by an earlier attempt: the first request is a
        status query, so finished or partly delivered sessions are never re-sent from byte 0.
        Raises _SessionExpired on 404 and _OutcomeUnknown when the last byte may have been
        delivered and the answer was lost."""
        self.final_sent = self.ambiguous = resume
        try:
            return self._loop(session_uri, resume)
        except (RetryableError, AuthError) as exc:  # a rejected token on a probe proves nothing either
            if self.ambiguous:
                raise _OutcomeUnknown(session_uri, exc.message) from exc
            raise

    def _loop(self, session_uri: str, resume: bool) -> dict[str, Any]:
        backoff = _Backoff("upload")
        offset, probe = 0, resume
        with open(self.path, "rb") as fh:
            while True:
                try:
                    if probe or offset >= self.total:
                        if offset >= self.total:  # YouTube holds every byte: only its answer is missing
                            self.final_sent = self.ambiguous = True
                        resp = self._query_status(session_uri)
                    else:
                        resp = self._send_chunk(session_uri, fh, offset)
                except httpx.TransportError as exc:
                    self.ambiguous = self.ambiguous or self.final_sent
                    backoff.fail(f"network error {type(exc).__name__}")
                    probe = True
                    continue
                status = resp.status_code
                if status in (200, 201):
                    try:
                        return _json_object(resp, "upload")
                    except RetryableError as exc:  # a 2xx means the video exists; we just cannot read it
                        raise _OutcomeUnknown(session_uri, exc.message) from exc
                if status == 308:
                    new_offset = _next_offset(resp)
                    if new_offset > offset:
                        backoff.reset()
                    elif not probe or new_offset >= self.total:
                        backoff.fail("no progress", resp)
                    offset, probe = new_offset, False
                    continue
                if status in RESUMABLE_STATUSES:
                    self.ambiguous = self.ambiguous or self.final_sent
                    backoff.fail(f"HTTP {status}", resp)
                    probe = True
                    continue
                if status == 404:
                    raise _SessionExpired()
                raise youtube_api_error(resp, "upload")

    def _send_chunk(self, uri: str, fh: BinaryIO, offset: int) -> httpx.Response:
        fh.seek(offset)
        data = fh.read(CHUNK_SIZE)
        if not data:
            raise PermanentError("The video file became shorter while uploading.")
        end = offset + len(data) - 1
        if end >= self.total - 1:
            self.final_sent = True  # set before the request: the bytes may arrive even if the reply is lost
        headers = {
            **_auth(self.access_token),
            "Content-Type": UPLOAD_CONTENT_TYPE,
            "Content-Range": f"bytes {offset}-{end}/{self.total}",
        }
        return self.client.put(uri, content=data, headers=headers)

    def _query_status(self, uri: str) -> httpx.Response:
        return _status_query(self.client, self.access_token, uri, self.total)


# --------------------------------------------------------------------------- publisher


def _video_metadata(req: PublishRequest, settings: dict[str, Any]) -> dict[str, Any]:
    snippet: dict[str, Any] = {
        "title": build_title(req.title, req.caption),
        "description": build_description(req.caption),
        "categoryId": str(settings["category_id"]),
    }
    tags = build_tags(settings.get("tags"))
    if tags:
        snippet["tags"] = tags
    return {
        "snippet": snippet,
        "status": {
            "privacyStatus": settings["privacy_status"],
            "selfDeclaredMadeForKids": settings["self_declared_made_for_kids"],
            "containsSyntheticMedia": settings["contains_synthetic_media"],
        },
    }


def _poll_delay(processing: dict[str, Any]) -> int:
    progress = processing.get("processingProgress") or {}
    try:
        left_ms = float(progress.get("timeLeftMs"))
    except (TypeError, ValueError):
        return 60
    return int(min(max(left_ms / 1000.0, 30.0), 300.0))


def _privacy_note(status: dict[str, Any], state: dict[str, Any]) -> str:
    requested, actual = state.get("requested_privacy"), status.get("privacyStatus")
    if requested and actual and requested != actual:
        return (
            f"YouTube set the video to '{actual}' instead of '{requested}' (this can happen for API "
            "projects that have not passed YouTube's audit). Change it in YouTube Studio if needed."
        )
    return ""


def _interpret(resource: dict[str, Any], state: dict[str, Any]) -> PublishResult:
    """Turn a video resource (status + processingDetails) into a publish outcome."""
    video_id = state["video_id"]
    status = resource.get("status") or {}
    processing = resource.get("processingDetails") or {}
    upload = status.get("uploadStatus")
    if upload == "processed":
        return PublishResult(
            outcome=PublishOutcome.PUBLISHED,
            platform_post_id=video_id,
            permalink=SHORTS_URL.format(id=video_id),
            state=state,
            message=_privacy_note(status, state),
        )
    if upload == "rejected":
        reason = status.get("rejectionReason") or "unknown"
        hint = _REJECTION_HINTS.get(reason, reason)
        raise PermanentError(f"YouTube rejected the video ({hint}; reason: {reason}).", code=reason)
    if upload == "failed":
        reason = status.get("failureReason") or "unknown"
        hint = _FAILURE_HINTS.get(reason, reason)
        raise PermanentError(f"YouTube could not process the upload ({hint}; reason: {reason}).", code=reason)
    if upload == "deleted":
        raise PermanentError("The video was deleted on YouTube before processing finished.", code="deleted")
    if processing.get("processingStatus") == "failed":
        reason = processing.get("processingFailureReason") or "unknown"
        raise PermanentError(f"YouTube processing failed (reason: {reason}).", code=reason)
    return PublishResult(
        outcome=PublishOutcome.PROCESSING,
        platform_post_id=video_id,
        state=state,
        poll_after_seconds=_poll_delay(processing),
        message="Uploaded to YouTube; waiting for YouTube to finish processing.",
    )


class YouTubePublisher:
    platform = Platform.YOUTUBE
    limits = LIMITS[Platform.YOUTUBE]

    def validate(self, account: AccountContext, req: PublishRequest) -> list[str]:
        lim = self.limits
        problems: list[str] = []
        if req.duration_s < lim.min_duration_s:
            problems.append(
                f"Video is {req.duration_s:.1f} s long; YouTube needs at least {lim.min_duration_s:g} s."
            )
        if req.duration_s > lim.max_duration_s:
            problems.append(
                f"Video is {req.duration_s:.0f} s long; YouTube Shorts allow at most "
                f"{lim.max_duration_s:.0f} s (3 minutes)."
            )
        if req.size_bytes > lim.max_size_bytes:
            problems.append("Video file is larger than YouTube's 256 GB upload limit.")
        if req.width <= 0 or req.height <= 0:
            problems.append("Video dimensions are unknown; re-process the video before publishing.")
        elif req.width > req.height:
            problems.append(
                f"Video is landscape ({req.width}x{req.height}); YouTube Shorts must be vertical or square."
            )
        if not build_title(req.title, req.caption):
            problems.append(TITLE_REQUIRED_MESSAGE)
        problems.extend(settings_problems(effective_settings(req.settings)))
        return problems

    def publish(self, account: AccountContext, req: PublishRequest) -> PublishResult:
        settings = effective_settings(req.settings)
        problems = settings_problems(settings)
        if not build_title(req.title, req.caption):
            problems.append(TITLE_REQUIRED_MESSAGE)
        if problems:
            raise PermanentError(" ".join(problems), code="invalid_settings")
        earlier = req.state or {}
        if isinstance(earlier.get("video_id"), str) and earlier["video_id"]:
            return self.poll(account, earlier)  # an earlier attempt already created the video
        total = _file_size(req.video_path)
        metadata = _video_metadata(req, settings)
        resume_uri = _resumable_session(earlier, total)
        with http.client() as c:
            upload = _ResumableUpload(c, account.tokens.access_token, req.video_path, total)
            try:
                resource = _upload_with_restart(
                    upload, metadata, bool(settings["notify_subscribers"]), resume_uri=resume_uri
                )
            except _OutcomeUnknown as exc:
                raise UnknownOutcomeError(
                    "YouTube did not confirm the upload after the last byte was sent "
                    f"({exc.reason}); Repeater will ask YouTube whether the video was created.",
                    state={
                        "session_uri": exc.session_uri,
                        "total": total,
                        "requested_privacy": settings["privacy_status"],
                    },
                    code="upload_outcome_unknown",
                ) from exc
        video_id = resource.get("id")
        if not isinstance(video_id, str) or not video_id:
            # The bytes were accepted but the answer names no video: it may be live, never retry.
            raise UnknownOutcomeError(
                "YouTube accepted the upload but returned no video id; check the channel before "
                "publishing again.",
                code="no_video_id",
            )
        log.info("YouTube upload complete for post %s: video %s", req.post_id, video_id)
        state = {"video_id": video_id, "requested_privacy": settings["privacy_status"]}
        return _interpret(resource, state)

    def poll(self, account: AccountContext, state: dict[str, Any]) -> PublishResult:
        video_id = state.get("video_id")
        if not isinstance(video_id, str) or not video_id:
            if state.get("session_uri"):
                return _poll_session(account, state)
            raise PermanentError("Missing YouTube video id in publish state.", code="no_video_id")
        with http.client() as c:
            data = api_get(
                c,
                account.tokens.access_token,
                "/videos",
                {"part": "status,processingDetails", "id": video_id},
                "status check",
            )
        items = [i for i in data.get("items") or [] if isinstance(i, dict)]
        if not items:
            misses = int(state.get("not_found_polls") or 0) + 1
            if misses >= NOT_FOUND_POLL_LIMIT:
                raise PermanentError(
                    "The uploaded video no longer exists on YouTube (it may have been deleted).",
                    code="videoNotFound",
                )
            return PublishResult(
                outcome=PublishOutcome.PROCESSING,
                platform_post_id=video_id,
                state={**state, "not_found_polls": misses},
                message="YouTube does not list the video yet.",
            )
        clean_state = {k: v for k, v in state.items() if k != "not_found_polls"}
        return _interpret(items[0], clean_state)


def _file_size(path: Path) -> int:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PermanentError("The encoded video file is missing; re-process the video.") from exc
    if size <= 0:
        raise PermanentError("The encoded video file is empty; re-process the video.")
    return size


def _resumable_session(state: dict[str, Any], total: int) -> str | None:
    """The session URI an earlier attempt left in `state`, if it belongs to this very file."""
    uri = state.get("session_uri")
    if isinstance(uri, str) and uri.startswith("https://") and state.get("total") == total:
        return uri
    return None


def _upload_with_restart(
    upload: _ResumableUpload, metadata: dict[str, Any], notify: bool, *, resume_uri: str | None = None
) -> dict[str, Any]:
    """Run the resumable upload. A session left by an earlier attempt (`resume_uri`) is resumed
    first; when a session has expired (404) a new one is started, at most twice."""
    starts = 0
    while True:
        if resume_uri is not None:
            session_uri, resume, resume_uri = resume_uri, True, None
        else:
            if starts >= 2:
                raise RetryableError("The YouTube upload session expired twice; Repeater will retry later.")
            session_uri, resume = upload.start(metadata, notify), False
            starts += 1
        try:
            return upload.run(session_uri, resume=resume)
        except _SessionExpired:
            log.info("YouTube upload session expired (new sessions started: %d); starting over", starts)


def _poll_session(account: AccountContext, state: dict[str, Any]) -> PublishResult:
    """Resolve an unknown outcome: ask the resumable session whether the video was created.
    A finished session repeats its original answer (the video resource); 308 with every byte
    present means YouTube is still finishing; any other 308 or a 404 proves that no video
    exists, so publish() has to run again."""
    session_uri, total = state.get("session_uri"), state.get("total")
    if (
        not isinstance(session_uri, str)
        or not session_uri.startswith("https://")
        or not isinstance(total, int)
    ):
        raise PermanentError("Missing YouTube video id in publish state.", code="no_video_id")
    with http.client() as c:
        try:
            resp = _status_query(c, account.tokens.access_token, session_uri, total)
        except httpx.TransportError as exc:
            raise RetryableError(
                f"YouTube upload status check: network error ({type(exc).__name__})"
            ) from exc
    status = resp.status_code
    if status in (200, 201):
        resource = _json_object(resp, "upload status check")  # unreadable: poll again
        video_id = resource.get("id")
        if not isinstance(video_id, str) or not video_id:
            raise PermanentError(
                "YouTube accepted the upload but returned no video id; check the channel before "
                "publishing again.",
                code="no_video_id",
            )
        video_state: dict[str, Any] = {"video_id": video_id}
        if state.get("requested_privacy"):
            video_state["requested_privacy"] = state["requested_privacy"]
        log.info("YouTube upload resolved by status query: video %s", video_id)
        return _interpret(resource, video_state)
    if status == 308:
        if _next_offset(resp) >= total:
            return PublishResult(
                outcome=PublishOutcome.PROCESSING,
                state=dict(state),
                poll_after_seconds=30,
                message="YouTube has received the whole file and is finishing the upload.",
            )
        raise RestartPublish(
            "YouTube never received the whole video, so nothing was published; the upload will start over.",
            code="upload_incomplete",
            status=status,
        )
    if status in (404, 410):
        raise RestartPublish(
            "The YouTube upload session is gone and no video was created; the upload will start over.",
            code="session_expired",
            status=status,
        )
    if status in RESUMABLE_STATUSES:
        raise RetryableError(
            f"YouTube upload status check failed (HTTP {status}).",
            status=status,
            retry_after=http.retry_after_seconds(resp, 60.0),
        )
    raise youtube_api_error(resp, "upload status check")


# --------------------------------------------------------------------------- lister


@dataclass
class _PlaylistEntry:
    video_id: str
    published_at: datetime | None
    privacy: str | None


class YouTubeLister:
    platform = Platform.YOUTUBE

    def list_recent(
        self,
        account: AccountContext,
        *,
        since: datetime | None = None,
        limit: int = 50,
        config: dict | None = None,
    ) -> list[RemoteVideo]:
        """Newest-first uploads of the channel. Private uploads are skipped unless
        config["include_private"] is true. Live and upcoming broadcasts are skipped."""
        if limit <= 0:
            return []
        include_private = bool((config or {}).get("include_private"))
        token = account.tokens.access_token
        with http.client() as c:
            playlist_id = self._uploads_playlist_id(c, account)
            entries = self._playlist_entries(
                c, token, playlist_id, ensure_aware(since), limit, include_private
            )
            details = self._video_details(c, token, [e.video_id for e in entries])
        videos = [v for v in (self._remote_video(e, details.get(e.video_id)) for e in entries) if v]
        videos.sort(key=lambda v: v.created_at or _EPOCH, reverse=True)
        return videos[:limit]

    def download(self, account: AccountContext, remote: RemoteVideo, dest: Path) -> Path:
        raise NotSupportedError(
            "YouTube has no official download, and the YouTube API Developer Policies do not allow "
            "downloading videos through the API. Upload the original file or add it from Google Drive.",
            code="no_official_download",
        )

    def _uploads_playlist_id(self, c: httpx.Client, account: AccountContext) -> str:
        playlist_id = (account.extra or {}).get("uploads_playlist_id")
        if playlist_id:
            return str(playlist_id)
        channel_id = (account.extra or {}).get("channel_id") or account.external_id
        params: dict[str, Any] = {"part": "contentDetails"}
        params.update({"id": channel_id} if channel_id else {"mine": "true"})
        data = api_get(c, account.tokens.access_token, "/channels", params, "channel lookup")
        for item in data.get("items") or []:
            uploads = ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
            if uploads:
                return str(uploads)
        raise PermanentError(
            "The YouTube channel was not found or has no uploads playlist.", code="channelNotFound"
        )

    def _playlist_entries(
        self,
        c: httpx.Client,
        token: str,
        playlist_id: str,
        since: datetime | None,
        limit: int,
        include_private: bool,
    ) -> list[_PlaylistEntry]:
        """Walk the uploads playlist newest first, stopping at `since` or `limit`. The uploads
        playlist is ordered newest first in practice; the caller re-sorts anyway."""
        entries: list[_PlaylistEntry] = []
        page_token: str | None = None
        for _ in range(MAX_PLAYLIST_PAGES):
            params: dict[str, Any] = {
                "part": "snippet,contentDetails,status",
                "playlistId": playlist_id,
                "maxResults": PLAYLIST_PAGE_SIZE,
            }
            if page_token:
                params["pageToken"] = page_token
            data = api_get(c, token, "/playlistItems", params, "uploads listing")
            for item in data.get("items") or []:
                entry = _playlist_entry(item)
                if entry is None:
                    continue
                if since and entry.published_at and entry.published_at < since:
                    return entries
                if entry.privacy == "private" and not include_private:
                    continue
                entries.append(entry)
                if len(entries) >= limit:
                    return entries
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return entries

    def _video_details(self, c: httpx.Client, token: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        details: dict[str, dict[str, Any]] = {}
        for start in range(0, len(ids), VIDEOS_BATCH_SIZE):
            batch = ids[start : start + VIDEOS_BATCH_SIZE]
            data = api_get(
                c,
                token,
                "/videos",
                {"part": "contentDetails,snippet", "id": ",".join(batch)},
                "video details",
            )
            for item in data.get("items") or []:
                if isinstance(item, dict) and item.get("id"):
                    details[str(item["id"])] = item
        return details

    def _remote_video(self, entry: _PlaylistEntry, item: dict[str, Any] | None) -> RemoteVideo | None:
        if item is None:  # deleted or no longer visible since the playlist was read
            return None
        snippet = item.get("snippet") or {}
        if snippet.get("liveBroadcastContent") in ("live", "upcoming"):
            return None
        duration = parse_iso_duration((item.get("contentDetails") or {}).get("duration"))
        return RemoteVideo(
            external_id=entry.video_id,
            created_at=_parse_time(snippet.get("publishedAt")) or entry.published_at,
            title=snippet.get("title") or "",
            caption=snippet.get("description") or "",
            permalink=video_permalink(entry.video_id, duration),
            duration_s=duration,
            download_url=None,
            extra={"channel_id": snippet.get("channelId"), "privacy_status": entry.privacy},
        )


def _playlist_entry(item: Any) -> _PlaylistEntry | None:
    if not isinstance(item, dict):
        return None
    details = item.get("contentDetails") or {}
    snippet = item.get("snippet") or {}
    video_id = details.get("videoId") or (snippet.get("resourceId") or {}).get("videoId")
    if not video_id:
        return None
    return _PlaylistEntry(
        video_id=str(video_id),
        published_at=_parse_time(details.get("videoPublishedAt")) or _parse_time(snippet.get("publishedAt")),
        privacy=(item.get("status") or {}).get("privacyStatus"),
    )


publisher = YouTubePublisher()
lister = YouTubeLister()
