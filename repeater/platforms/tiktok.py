"""TikTok adapter: Login Kit for Web (OAuth v2), Content Posting API (Direct Post and Upload to
inbox; PULL_FROM_URL transfer with FILE_UPLOAD as the fallback) and Display API (video.list).

The encoded file is already on Repeater's server, so TikTok pulls it from the signed media URL
(PublishRequest.public_url). That needs the URL prefix https://<host>/m/ to be verified in the
TikTok app; until it is, init answers url_ownership_unverified and the file is uploaded instead.

Official references (checked 2026-10-14):
    https://developers.tiktok.com/doc/login-kit-web
    https://developers.tiktok.com/doc/oauth-user-access-token-management
    https://developers.tiktok.com/doc/tiktok-api-v2-get-user-info
    https://developers.tiktok.com/doc/content-posting-api-reference-query-creator-info
    https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
    https://developers.tiktok.com/doc/content-posting-api-reference-upload-video
    https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide
    https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status
    https://developers.tiktok.com/doc/tiktok-api-v2-video-list
    https://developers.tiktok.com/doc/tiktok-api-v2-error-handling
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from repeater.config import get_settings
from repeater.messages import Message
from repeater.models import Platform
from repeater.platforms.base import (
    LIMITS,
    RULE_SETTINGS_DEFAULTS,
    AccountContext,
    AuthError,
    ConnectedAccountData,
    NotSupportedError,
    OAuthTokens,
    PermanentError,
    PlatformError,
    PlatformLimits,
    PublishOutcome,
    PublishRequest,
    PublishResult,
    RateLimitedError,
    RemoteVideo,
    RestartPublish,
    RetryableError,
    TikTokCreatorInfo,
    UnknownOutcomeError,
)
from repeater.platforms.http import classify_http_error, client, retry_after_seconds
from repeater.timeutil import ensure_aware, utcnow

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- endpoints

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
API_BASE = "https://open.tiktokapis.com"
TOKEN_URL = f"{API_BASE}/v2/oauth/token/"
REVOKE_URL = f"{API_BASE}/v2/oauth/revoke/"
USER_INFO_URL = f"{API_BASE}/v2/user/info/"
CREATOR_INFO_URL = f"{API_BASE}/v2/post/publish/creator_info/query/"
DIRECT_INIT_URL = f"{API_BASE}/v2/post/publish/video/init/"
INBOX_INIT_URL = f"{API_BASE}/v2/post/publish/inbox/video/init/"
STATUS_URL = f"{API_BASE}/v2/post/publish/status/fetch/"
VIDEO_LIST_URL = f"{API_BASE}/v2/video/list/"

# External review currently covers identity and public metadata only. Keep posting
# permissions out of every new authorization until a later, separately approved phase.
SCOPES = ("user.info.basic", "video.list")
PUBLISHING_SCOPES = ("video.publish", "video.upload")
USER_FIELDS = "open_id,union_id,avatar_url,display_name"
VIDEO_FIELDS = "id,create_time,cover_image_url,share_url,video_description,duration,height,width,title"

JSON_CONTENT_TYPE = "application/json; charset=UTF-8"
API_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
UPLOAD_TIMEOUT = httpx.Timeout(300.0, connect=15.0, pool=30.0)

PRIVACY_LEVELS = ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY")
MODES = ("direct", "inbox")
MAX_TITLE_UTF16 = 2200
MAX_LIST_PAGE_SIZE = 20
MAX_LIST_PAGES = 50
POLL_SECONDS = 30  # status/fetch allows 30 requests per minute per token

SOURCE_PULL = "PULL_FROM_URL"
SOURCE_FILE = "FILE_UPLOAD"
# TikTok answers url_ownership_unverified until the URL prefix is verified in the app. The answer
# is remembered per process, but only for an hour so that verifying the prefix needs no restart.
PULL_UNVERIFIED_RETRY_SECONDS = 3600.0
# An upload_url lives one hour (media transfer guide); a FILE_UPLOAD still waiting for bytes after
# that can never finish.
UPLOAD_URL_LIFETIME = timedelta(hours=1)
# TikTok shares publicaly_available_post_id only after moderation, "usually within one minute, in
# some cases a few hours" (get-video-status). poll() waits this long after PUBLISH_COMPLETE for it,
# but never past POST_ID_MAX_AFTER_INIT after init, so the engine's 2 h poll limit cannot turn a
# live post into needs-attention.
POST_ID_WAIT = timedelta(minutes=30)
POST_ID_MAX_AFTER_INIT = timedelta(minutes=100)
POST_ID_POLL_SECONDS = 300

# Daily caps (per creator, per app, pending inbox drafts) reset over a 24-hour window that
# TikTok does not disclose, so retrying sooner only burns more requests.
DAILY_CAP_RETRY_AFTER = 12 * 3600
RATE_LIMIT_RETRY_AFTER = 60.0
FAILED_RETRY_AFTER = 300.0

# --------------------------------------------------------------------------- chunk plan

MIB = 1024 * 1024
# The transfer guide gives chunk bounds in "MB" without defining the unit (its examples use
# binary sizes), so each bound uses the stricter reading: a plan is valid under either.
MIN_CHUNK_BYTES = 5 * MIB
MAX_CHUNK_BYTES = 64 * 1000 * 1000
MAX_FINAL_CHUNK_BYTES = 128 * 1000 * 1000
MAX_CHUNKS = 1000
DEFAULT_CHUNK_BYTES = 16 * MIB
CHUNK_ATTEMPTS = 3
CHUNK_RETRY_BACKOFF = 2.0

_sleep = time.sleep  # replaced in tests
_monotonic = time.monotonic  # replaced in tests


class _PullCache:
    """When PULL_FROM_URL was last refused with url_ownership_unverified (monotonic seconds)."""

    unverified_until: float = 0.0


_pull_cache = _PullCache()


@dataclass(frozen=True)
class ChunkPlan:
    video_size: int
    chunk_size: int
    total_chunk_count: int

    def ranges(self) -> Iterator[tuple[int, int]]:
        """Inclusive (first_byte, last_byte) of each chunk; the last one absorbs the remainder."""
        for index in range(self.total_chunk_count):
            first = index * self.chunk_size
            if index == self.total_chunk_count - 1:
                yield first, self.video_size - 1
            else:
                yield first, first + self.chunk_size - 1

    def source_info(self) -> dict[str, Any]:
        return {
            "source": "FILE_UPLOAD",
            "video_size": self.video_size,
            "chunk_size": self.chunk_size,
            "total_chunk_count": self.total_chunk_count,
        }


def plan_chunks(video_size: int, preferred_chunk: int = DEFAULT_CHUNK_BYTES) -> ChunkPlan:
    """FILE_UPLOAD parameters per the media transfer guide: files up to one chunk go whole;
    larger files use `chunk_size` pieces, total_chunk_count = floor(size / chunk_size), and the
    final chunk carries the remainder (always < 2 * chunk_size <= 128 MB)."""
    if not MIN_CHUNK_BYTES <= preferred_chunk <= MAX_CHUNK_BYTES:
        raise ValueError(f"chunk size must be between {MIN_CHUNK_BYTES} and {MAX_CHUNK_BYTES} bytes")
    if video_size <= 0:
        raise PermanentError("The video file is empty.", code="empty_file")
    max_size = LIMITS[Platform.TIKTOK].max_size_bytes
    if video_size > max_size:
        raise PermanentError(
            f"The video is {video_size} bytes; TikTok accepts at most {max_size} bytes.",
            code="file_too_large",
        )
    if video_size <= preferred_chunk:
        return ChunkPlan(video_size, video_size, 1)
    chunk = max(preferred_chunk, math.ceil(video_size / MAX_CHUNKS))
    if video_size > MAX_CHUNK_BYTES:
        # Files over 64 MB must arrive in at least two chunks.
        chunk = min(chunk, video_size // 2)
    return ChunkPlan(video_size, chunk, video_size // chunk)


# --------------------------------------------------------------------------- small helpers


def truncate_utf16(text: str, limit: int) -> str:
    """Cut `text` to at most `limit` UTF-16 code units without splitting a surrogate pair."""
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return text[:index]
    return text


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _scrub(text: str, secrets: tuple[str | None, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _json_or_none(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _credentials() -> tuple[str, str]:
    settings = get_settings()
    if not (settings.tiktok_client_key and settings.tiktok_client_secret):
        raise PermanentError(
            "TikTok is not configured: set REPEATER_TIKTOK_CLIENT_KEY and REPEATER_TIKTOK_CLIENT_SECRET.",
            code="not_configured",
        )
    return settings.tiktok_client_key, settings.tiktok_client_secret


def _check_purpose(purpose: str) -> None:
    if purpose != "tiktok":
        raise ValueError(f"unsupported TikTok OAuth purpose: {purpose!r}")


class _NetworkError(RetryableError):
    """A request to TikTok failed without an HTTP answer. `may_have_arrived` is False when the
    request provably never reached TikTok (no connection, incomplete send), True when TikTok may
    have acted on it and only the reply was lost."""

    def __init__(self, message: str, *, may_have_arrived: bool) -> None:
        super().__init__(message)
        self.may_have_arrived = may_have_arrived


_NEVER_ARRIVED = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
    httpx.WriteError,
    httpx.WriteTimeout,
)


def _may_have_arrived(exc: Exception) -> bool:
    return not isinstance(exc, _NEVER_ARRIVED)


def _network_error(action: str, exc: Exception) -> _NetworkError:
    return _NetworkError(
        f"Could not reach TikTok while {action} ({type(exc).__name__}).",
        may_have_arrived=_may_have_arrived(exc),
    )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return ensure_aware(datetime.fromisoformat(value))
    except ValueError:
        return None


# --------------------------------------------------------------------------- API error mapping

_AUTH_CODES = {"access_token_invalid", "scope_not_authorized", "scope_permission_missed"}
_DAILY_CAP_CODES = {"spam_risk_too_many_posts", "reached_active_user_cap", "spam_risk_too_many_pending_share"}
_RETRYABLE_CODES = {"internal_error"}
_CODE_EXPLANATIONS = {
    "access_token_invalid": "TikTok rejected the access token; reconnect the TikTok account.",
    "scope_not_authorized": (
        "The TikTok account did not grant a permission Repeater needs; reconnect it and allow every "
        "requested permission."
    ),
    "scope_permission_missed": (
        "The TikTok authorization is missing a permission Repeater needs; reconnect the account."
    ),
    "rate_limit_exceeded": "TikTok's API rate limit was reached; Repeater will retry shortly.",
    "spam_risk_too_many_posts": (
        "TikTok's daily posting limit for this account was reached; posting resumes later."
    ),
    "reached_active_user_cap": (
        "The Repeater TikTok app reached TikTok's daily quota of posting accounts; posting resumes later."
    ),
    "spam_risk_too_many_pending_share": (
        "Too many TikTok drafts are waiting in this account's inbox (TikTok allows 5 per 24 hours); "
        "finish or delete them in the TikTok app."
    ),
    "unaudited_client_can_only_post_to_private_accounts": (
        "TikTok only accepts posts from apps that have not passed TikTok's audit yet when both "
        "conditions hold: the TikTok account is set to private in the TikTok app, and the privacy "
        "level of the post is 'Only me' (SELF_ONLY). Fix both until the audit is approved."
    ),
    "privacy_level_option_mismatch": (
        "The chosen TikTok privacy level is not allowed for this account (apps that have not passed "
        "TikTok's audit can only post 'Only me' / SELF_ONLY). Choose one of the options TikTok offers "
        "in the TikTok posting settings."
    ),
    "spam_risk_user_banned_from_posting": "TikTok has blocked this account from posting new videos.",
    "url_ownership_unverified": "TikTok requires the video URL's domain to be verified for this app.",
    "invalid_publish_id": "TikTok does not know this upload any more; it cannot be tracked.",
    "token_not_authorized_for_specified_publish_id": (
        "This TikTok upload belongs to a different authorization; it cannot be tracked."
    ),
    "internal_error": "TikTok had a temporary internal error; Repeater will retry.",
}


def _api_error(resp: httpx.Response, error: dict[str, Any], action: str) -> PlatformError:
    code = str(error.get("code") or "") or None
    log_id = error.get("log_id") or error.get("logid")
    detail = _CODE_EXPLANATIONS.get(code or "")
    if detail is None:
        tiktok_message = str(error.get("message") or "").strip()[:300]
        detail = f"TikTok returned an error while {action}" + (
            f": {tiktok_message}" if tiktok_message else "."
        )
    suffix = f" (TikTok error {code}" + (f", log id {log_id})" if log_id else ")") if code else ""
    message = detail + suffix
    status = resp.status_code
    if code in _AUTH_CODES:
        return AuthError(message, code=code, status=status)
    if code == "rate_limit_exceeded":
        retry = retry_after_seconds(resp, RATE_LIMIT_RETRY_AFTER)
        return RateLimitedError(message, code=code, status=status, retry_after=retry)
    if code in _DAILY_CAP_CODES:
        return RateLimitedError(message, code=code, status=status, retry_after=DAILY_CAP_RETRY_AFTER)
    if code in _RETRYABLE_CODES:
        return RetryableError(message, code=code, status=status, retry_after=FAILED_RETRY_AFTER)
    if code in _CODE_EXPLANATIONS:
        return PermanentError(message, code=code, status=status)
    if status >= 400:
        return classify_http_error(resp, message, code=code)
    return PermanentError(message, code=code, status=status)


def _redact(exc: PlatformError, secrets: tuple[str | None, ...]) -> PlatformError:
    exc.message = _scrub(exc.message, secrets)
    exc.args = (exc.message,)
    return exc


def _parse_api_response(resp: httpx.Response, action: str, token: str) -> dict[str, Any]:
    payload = _json_or_none(resp)
    payload = payload if isinstance(payload, dict) else {}
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = error.get("code")
    if resp.status_code < 400 and code in (None, "", "ok"):
        data = payload.get("data")
        return data if isinstance(data, dict) else {}
    if code:
        exc = _api_error(resp, error, action)
    else:
        exc = classify_http_error(resp, f"TikTok returned HTTP {resp.status_code} while {action}.")
    log.warning(
        "TikTok API error while %s: code=%s status=%s log_id=%s",
        action,
        exc.code,
        resp.status_code,
        error.get("log_id"),
    )
    raise _redact(exc, (token,))


def _api_request(
    method: str,
    url: str,
    token: str,
    *,
    action: str,
    body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    if method == "POST":
        headers["Content-Type"] = JSON_CONTENT_TYPE
    try:
        with client(timeout=API_TIMEOUT) as http:
            resp = http.request(method, url, headers=headers, json=body, params=params)
    except httpx.HTTPError as exc:
        raise _network_error(action, exc) from exc
    return _parse_api_response(resp, action, token)


# --------------------------------------------------------------------------- OAuth


def _token_error_fields(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(code, description, log_id) from a token/revoke error body; code None means success."""
    raw = payload.get("error")
    if isinstance(raw, dict):
        code, description, log_id = raw.get("code"), raw.get("message"), raw.get("log_id")
    else:
        code, description, log_id = raw, None, None
    description = payload.get("error_description") or description
    log_id = payload.get("log_id") or log_id
    code = str(code) if code and code != "ok" else None
    return code, (str(description) if description else None), (str(log_id) if log_id else None)


def _token_error(resp: httpx.Response, payload: dict[str, Any], action: str) -> PlatformError:
    code, description, log_id = _token_error_fields(payload)
    log.warning("TikTok token endpoint error while %s: error=%s log_id=%s", action, code, log_id)
    message = f"TikTok refused to {action}"
    if description:
        message += f": {description[:300]}"
    message += f" (TikTok error {code})" if code else f" (HTTP {resp.status_code})"
    status = resp.status_code
    if status >= 500:
        return RetryableError(message, code=code, status=status, retry_after=retry_after_seconds(resp, 120))
    if status == 429 or code == "rate_limit_exceeded":
        return RateLimitedError(
            message, code=code, status=status, retry_after=retry_after_seconds(resp, RATE_LIMIT_RETRY_AFTER)
        )
    if code in ("invalid_client", "unauthorized_client"):
        return PermanentError(
            message + ". Check REPEATER_TIKTOK_CLIENT_KEY and REPEATER_TIKTOK_CLIENT_SECRET.",
            code=code,
            status=status,
        )
    return AuthError(message + ". Reconnect the TikTok account.", code=code, status=status)


def _form_post(url: str, form: dict[str, str], action: str) -> httpx.Response:
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"}
    try:
        with client(timeout=API_TIMEOUT) as http:
            return http.post(url, data=form, headers=headers)
    except httpx.HTTPError as exc:
        raise _network_error(action, exc) from exc


def _token_request(form: dict[str, str], action: str, secrets: tuple[str | None, ...]) -> dict[str, Any]:
    resp = _form_post(TOKEN_URL, form, action)
    payload = _json_or_none(resp)
    payload = payload if isinstance(payload, dict) else {}
    code, _, _ = _token_error_fields(payload)
    if resp.status_code < 400 and code is None and payload.get("access_token"):
        return payload
    raise _redact(_token_error(resp, payload, action), secrets)


def _tokens_from_payload(payload: dict[str, Any]) -> OAuthTokens:
    now = utcnow()
    scopes = [s.strip() for s in str(payload.get("scope") or "").split(",") if s.strip()]
    extra = {"open_id": payload["open_id"]} if payload.get("open_id") else {}
    return OAuthTokens(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token") or None,
        expires_at=OAuthTokens.expiry_from_seconds(payload.get("expires_in"), now),
        refresh_expires_at=OAuthTokens.expiry_from_seconds(payload.get("refresh_expires_in"), now),
        scopes=scopes,
        token_type=str(payload.get("token_type") or "Bearer"),
        extra=extra,
    )


def _user_info(access_token: str) -> dict[str, Any]:
    data = _api_request(
        "GET",
        USER_INFO_URL,
        access_token,
        action="reading the TikTok profile",
        params={"fields": USER_FIELDS},
    )
    user = data.get("user")
    return user if isinstance(user, dict) else {}


def read_profile(account: AccountContext) -> dict[str, Any]:
    """Read the authorized identity without invoking any publishing endpoint."""
    user = _user_info(account.tokens.access_token)
    if user.get("open_id") != account.external_id:
        raise AuthError(
            "TikTok returned a different account; reconnect this account.", code="identity_mismatch"
        )
    return user


class TikTokOAuth:
    name = "tiktok"
    # Login Kit for Web uses a server-side code exchange; code_verifier is documented as
    # required only for mobile and desktop apps.
    uses_pkce = False

    def authorize_url(
        self, *, state: str, redirect_uri: str, code_challenge: str | None, purpose: str
    ) -> str:
        _check_purpose(purpose)
        client_key, _ = _credentials()
        params = {
            "client_key": client_key,
            "response_type": "code",
            "scope": ",".join(SCOPES),
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params, safe=',')}"

    def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: str | None, purpose: str
    ) -> list[ConnectedAccountData]:
        _check_purpose(purpose)
        client_key, client_secret = _credentials()
        form = {
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        payload = _token_request(form, "connect the TikTok account", (client_secret, code))
        tokens = _tokens_from_payload(payload)
        user = _user_info(tokens.access_token)
        open_id = str(user.get("open_id") or tokens.extra.get("open_id") or "")
        if not open_id:
            raise PermanentError("TikTok did not return the account id (open_id).", code="missing_open_id")
        tokens.extra["open_id"] = open_id
        extra: dict[str, Any] = {"open_id": open_id}
        return [
            ConnectedAccountData(
                platform=Platform.TIKTOK,
                external_id=open_id,
                display_name=str(user.get("display_name") or "TikTok account"),
                tokens=tokens,
                avatar_url=user.get("avatar_url") or None,
                extra=extra,
            )
        ]

    def refresh(self, account: AccountContext) -> OAuthTokens:
        current = account.tokens
        if not current.refresh_token:
            raise AuthError("No TikTok refresh token is stored; reconnect the TikTok account.")
        if current.refresh_expires_at and current.refresh_expires_at <= utcnow():
            raise AuthError(
                "The TikTok authorization expired (it lasts 365 days); reconnect the TikTok account.",
                code="refresh_token_expired",
            )
        client_key, client_secret = _credentials()
        form = {
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": current.refresh_token,
        }
        payload = _token_request(
            form, "refresh the TikTok authorization", (client_secret, current.refresh_token)
        )
        tokens = _tokens_from_payload(payload)
        if not tokens.refresh_token:
            tokens.refresh_token = current.refresh_token
            tokens.refresh_expires_at = current.refresh_expires_at
        elif tokens.refresh_expires_at is None:
            tokens.refresh_expires_at = current.refresh_expires_at
        if not tokens.scopes:
            tokens.scopes = list(current.scopes)
        tokens.extra = {**current.extra, **tokens.extra}
        return tokens

    def revoke(self, account: AccountContext) -> None:
        access_token = account.tokens.access_token
        if account.tokens.expires_within(timedelta(0)) and account.tokens.refresh_token:
            try:
                access_token = self.refresh(account).access_token
            except AuthError:
                return  # the grant is already gone; nothing left to revoke
        client_key, client_secret = _credentials()
        form = {"client_key": client_key, "client_secret": client_secret, "token": access_token}
        action = "revoke the TikTok authorization"
        resp = _form_post(REVOKE_URL, form, action)
        payload = _json_or_none(resp)
        payload = payload if isinstance(payload, dict) else {}
        code, _, _ = _token_error_fields(payload)
        if resp.status_code < 400 and code is None:
            return
        raise _redact(_token_error(resp, payload, action), (client_secret, access_token))


# --------------------------------------------------------------------------- creator info


def creator_info(account: AccountContext) -> TikTokCreatorInfo:
    """POST /v2/post/publish/creator_info/query/ (video.publish scope, 20 requests/min)."""
    data = _api_request(
        "POST",
        CREATOR_INFO_URL,
        account.tokens.access_token,
        action="checking the TikTok creator settings",
    )
    options = data.get("privacy_level_options")
    max_duration = data.get("max_video_post_duration_sec")
    return TikTokCreatorInfo(
        nickname=str(data.get("creator_nickname") or ""),
        avatar_url=data.get("creator_avatar_url") or None,
        privacy_level_options=[str(o) for o in options] if isinstance(options, list) else [],
        comment_disabled=bool(data.get("comment_disabled", False)),
        duet_disabled=bool(data.get("duet_disabled", False)),
        stitch_disabled=bool(data.get("stitch_disabled", False)),
        max_video_post_duration_sec=(
            int(max_duration)
            if isinstance(max_duration, int | float) and max_duration > 0
            else int(LIMITS[Platform.TIKTOK].max_duration_s)
        ),
        username=data.get("creator_username") or None,
    )


# --------------------------------------------------------------------------- publishing


def _rule_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    return {**RULE_SETTINGS_DEFAULTS["tiktok"], **(raw or {})}


def _client_audited() -> bool:
    """Whether TikTok has audited this app (setting REPEATER_TIKTOK_AUDITED, default false). An
    unaudited app can only post 'Only me' (SELF_ONLY) to private accounts."""
    return bool(get_settings().tiktok_audited)


def _direct_settings_problems(settings: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    privacy = settings.get("privacy_level")
    if not privacy:
        problems.append("Choose who can view the TikTok posts (privacy level); TikTok allows no default.")
    elif privacy not in PRIVACY_LEVELS:
        problems.append(f"Unknown TikTok privacy level {privacy!r}.")
    elif privacy != "SELF_ONLY" and not _client_audited():
        problems.append(
            "TikTok has not audited this app yet, so it only accepts 'Only me' (SELF_ONLY) posts, and "
            "only for a TikTok account that is set to private. Choose 'Only me' until the audit is approved."
        )
    if not settings.get("music_consent_at"):
        problems.append(
            "Accept TikTok's Music Usage Confirmation in the TikTok posting settings before publishing."
        )
    if _flag(settings.get("brand_content_toggle")) and privacy == "SELF_ONLY":
        problems.append("TikTok branded content cannot be posted with the 'Only me' privacy level.")
    return problems


def _file_problems(req: PublishRequest, limits: PlatformLimits) -> list[str]:
    problems: list[str] = []
    if req.duration_s < limits.min_duration_s:
        problems.append(f"The video is shorter than TikTok's minimum of {limits.min_duration_s:g} s.")
    if req.duration_s > limits.max_duration_s:
        problems.append(
            f"The video is {req.duration_s:.0f} s long; TikTok accepts at most "
            f"{limits.max_duration_s:.0f} s through the API."
        )
    if req.size_bytes <= 0:
        problems.append("The video file is empty.")
    elif req.size_bytes > limits.max_size_bytes:
        problems.append("The video is larger than TikTok's 4 GB limit.")
    return problems


def _check_against_creator_info(
    settings: dict[str, Any], req: PublishRequest, info: TikTokCreatorInfo
) -> None:
    privacy = settings["privacy_level"]
    if privacy not in info.privacy_level_options:
        allowed = ", ".join(info.privacy_level_options) or "none"
        raise PermanentError(
            f"The TikTok privacy level {privacy} is no longer available for this account (TikTok now "
            f"allows: {allowed}). Update the TikTok posting settings.",
            code="privacy_level_option_mismatch",
        )
    if req.duration_s > info.max_video_post_duration_sec:
        raise PermanentError(
            f"The video is {req.duration_s:.0f} s long but this TikTok account can post at most "
            f"{info.max_video_post_duration_sec} s.",
            code="duration_exceeds_creator_limit",
        )


def _post_info(settings: dict[str, Any], req: PublishRequest, info: TikTokCreatorInfo) -> dict[str, Any]:
    post: dict[str, Any] = {}
    title = truncate_utf16(req.caption or "", MAX_TITLE_UTF16)
    if title:
        post["title"] = title
    post.update(
        {
            "privacy_level": settings["privacy_level"],
            "disable_comment": not _flag(settings.get("allow_comment")) or info.comment_disabled,
            "disable_duet": not _flag(settings.get("allow_duet")) or info.duet_disabled,
            "disable_stitch": not _flag(settings.get("allow_stitch")) or info.stitch_disabled,
            "brand_content_toggle": _flag(settings.get("brand_content_toggle")),
            "brand_organic_toggle": _flag(settings.get("brand_organic_toggle")),
            "is_aigc": _flag(settings.get("is_aigc")),
        }
    )
    return post


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError as exc:
        raise PermanentError(
            "The encoded video file is missing on the server; re-process the video.", code="file_missing"
        ) from exc


def _start_upload(
    url: str,
    account: AccountContext,
    body: dict[str, Any],
    action: str,
    *,
    pull: bool,
) -> tuple[str, str | None]:
    """POST the init request; returns (publish_id, upload_url). With PULL_FROM_URL there is no
    upload_url and the init request is also the "publish" request: TikTok starts downloading and
    posting at once, so a lost answer means the video may be live (UnknownOutcomeError with no
    state: nothing is known that poll() could ask about)."""
    try:
        data = _api_request("POST", url, account.tokens.access_token, action=action, body=body)
    except _NetworkError as exc:
        if pull and exc.may_have_arrived:
            raise UnknownOutcomeError(
                f"TikTok did not answer while {action}; it may already be posting the video.",
                code="init_outcome_unknown",
            ) from exc
        raise
    except RetryableError as exc:
        if pull and exc.status is not None and 500 <= exc.status < 600:
            raise UnknownOutcomeError(
                f"TikTok did not confirm the pull request while {action}; it may already be posting the video.",
                code="init_outcome_unknown",
            ) from exc
        raise
    publish_id = data.get("publish_id")
    upload_url = data.get("upload_url")
    if pull:
        if not publish_id:
            raise UnknownOutcomeError(
                f"TikTok accepted the request but returned no publish id while {action}; it may already "
                "be posting the video.",
                code="missing_publish_id",
            )
        return str(publish_id), None
    if not publish_id or not upload_url:
        raise RetryableError(
            f"TikTok did not return an upload link while {action}.", code="missing_upload_url"
        )
    return str(publish_id), str(upload_url)


def _pull_possible(public_url: str) -> bool:
    """PULL_FROM_URL needs an HTTPS URL, and is skipped for a while after TikTok refused it."""
    return public_url.startswith("https://") and _monotonic() >= _pull_cache.unverified_until


_RANGE_RE = re.compile(r"bytes\s+0-(\d+)/\d+")


def _already_received(resp: httpx.Response, last: int) -> bool:
    match = _RANGE_RE.fullmatch(resp.headers.get("content-range", "").strip())
    return bool(match) and int(match.group(1)) >= last


def _chunk_status_error(resp: httpx.Response) -> PlatformError:
    status = resp.status_code
    if status in (403, 404):
        return RetryableError(
            "The TikTok upload link expired or is no longer valid; the upload will start over.",
            code="upload_url_invalid",
            status=status,
        )
    if status == 416:
        return RetryableError(
            "TikTok's upload progress did not match the chunk sent; the upload will start over.",
            code="upload_range_mismatch",
            status=status,
        )
    if status == 429:
        return RateLimitedError(
            "TikTok throttled the upload.", status=status, retry_after=retry_after_seconds(resp)
        )
    return PermanentError(f"TikTok rejected the uploaded video data (HTTP {status}).", status=status)


class _FinalChunkUnknown(Exception):  # noqa: N818
    """The last chunk may have reached TikTok (a try ended with a lost reply or a server error)
    and a later try failed: TikTok may hold the whole file and be publishing it."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _put_chunk(
    http: httpx.Client, url: str, data: bytes, first: int, last: int, total: int, final: bool
) -> None:
    headers = {
        "Content-Type": "video/mp4",
        "Content-Length": str(len(data)),
        "Content-Range": f"bytes {first}-{last}/{total}",
    }
    error: PlatformError = RetryableError("The TikTok upload did not complete.")
    delivered = False  # a try of the FINAL chunk may have reached TikTok although it failed

    def failure(exc: PlatformError) -> Exception:
        # Before the last byte may have arrived nothing can be live, so the upload simply starts
        # over. Afterwards any failure is ambiguous: let poll() ask TikTok.
        return _FinalChunkUnknown(exc.message) if final and delivered else exc

    for attempt in range(CHUNK_ATTEMPTS):
        if attempt:
            _sleep(CHUNK_RETRY_BACKOFF * attempt)
        try:
            resp = http.put(url, content=data, headers=headers)
        except httpx.HTTPError as exc:
            delivered = delivered or _may_have_arrived(exc)
            error = RetryableError(
                f"Network error while uploading the video to TikTok ({type(exc).__name__})."
            )
            continue
        if resp.status_code >= 500:
            delivered = True
            error = RetryableError(
                f"TikTok's upload server failed (HTTP {resp.status_code}).",
                status=resp.status_code,
                retry_after=retry_after_seconds(resp, 120),
            )
            continue
        if resp.status_code == 416 and attempt and _already_received(resp, last):
            return  # an earlier attempt reached TikTok even though its response was lost
        if resp.status_code >= 400:
            raise failure(_chunk_status_error(resp))
        if final and resp.status_code not in (200, 201):
            raise failure(
                RetryableError(
                    "TikTok did not confirm that the whole video arrived.", status=resp.status_code
                )
            )
        return
    raise failure(error)


def _upload_file(upload_url: str, path: Path, plan: ChunkPlan) -> None:
    """PUT each chunk in order to the upload_url (valid for one hour). Raises _FinalChunkUnknown
    when the outcome of the last chunk is unclear."""
    with client(timeout=UPLOAD_TIMEOUT) as http, path.open("rb") as fh:
        for index, (first, last) in enumerate(plan.ranges()):
            fh.seek(first)
            data = fh.read(last - first + 1)
            if len(data) != last - first + 1:
                raise PermanentError(
                    "The video file changed while it was being uploaded.", code="file_changed"
                )
            final = index == plan.total_chunk_count - 1
            _put_chunk(http, upload_url, data, first, last, plan.video_size, final)


class TikTokPublisher:
    platform = Platform.TIKTOK
    limits = LIMITS[Platform.TIKTOK]

    def __init__(self, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> None:
        self.chunk_bytes = chunk_bytes

    def validate(self, account: AccountContext, req: PublishRequest) -> list[str]:
        settings = _rule_settings(req.settings)
        problems: list[str] = []
        mode = settings.get("mode")
        if mode not in MODES:
            problems.append(f"Unknown TikTok posting mode {mode!r} (use 'direct' or 'inbox').")
        elif mode == "direct":
            problems.extend(_direct_settings_problems(settings))
        problems.extend(_file_problems(req, self.limits))
        return problems

    def publish(self, account: AccountContext, req: PublishRequest) -> PublishResult:
        problems = self.validate(account, req)
        if problems:
            raise PermanentError(" ".join(problems), code="invalid_settings")
        if req.state.get("publish_id"):
            # An earlier attempt got as far as TikTok: ask about that upload instead of starting
            # another post. Only a dropped upload (RestartPublish) clears the way for a new init.
            try:
                return self.poll(account, req.state)
            except RestartPublish:
                log.info("TikTok dropped the earlier upload of post %s; starting a new one", req.post_id)
        settings = _rule_settings(req.settings)
        plan = plan_chunks(_file_size(req.video_path), self.chunk_bytes)
        mode = settings["mode"]
        if mode == "inbox":
            username = account.extra.get("username")
            post_info: dict[str, Any] | None = None
            action, url = "sending the video to the TikTok inbox", INBOX_INIT_URL
        else:
            info = creator_info(account)
            _check_against_creator_info(settings, req, info)
            username = info.username or account.extra.get("username")
            post_info = _post_info(settings, req, info)
            action, url = "starting the TikTok post", DIRECT_INIT_URL
        source = SOURCE_PULL if _pull_possible(req.public_url) else SOURCE_FILE
        try:
            publish_id, upload_url = _start_upload(
                url, account, _init_body(post_info, source, req, plan), action, pull=source == SOURCE_PULL
            )
        except PermanentError as exc:
            if source != SOURCE_PULL or exc.code != "url_ownership_unverified":
                raise
            _pull_cache.unverified_until = _monotonic() + PULL_UNVERIFIED_RETRY_SECONDS
            log.warning(
                "TikTok has not verified the media URL prefix of this app; uploading the file instead "
                "(verify https://<host>/m/ in the TikTok app to let TikTok pull videos)"
            )
            source = SOURCE_FILE
            publish_id, upload_url = _start_upload(
                url, account, _init_body(post_info, source, req, plan), action, pull=False
            )
        state: dict[str, Any] = {
            "publish_id": publish_id,
            "mode": mode,
            "username": username,
            "source": source,
            "init_at": utcnow().isoformat(),
        }
        if mode == "direct":
            state["privacy_level"] = settings["privacy_level"]
        log.info(
            "TikTok %s post started: post=%s publish_id=%s source=%s chunks=%s",
            mode,
            req.post_id,
            publish_id,
            source,
            plan.total_chunk_count if source == SOURCE_FILE else 0,
        )
        if source == SOURCE_PULL:
            return PublishResult(
                outcome=PublishOutcome.PROCESSING,
                state=state,
                poll_after_seconds=POLL_SECONDS,
                message="TikTok is downloading the video; this can take a few minutes.",
            )
        if upload_url is None:  # _start_upload always returns the link for FILE_UPLOAD
            raise RetryableError("TikTok did not return an upload link.", code="missing_upload_url")
        state["video_size"] = plan.video_size
        try:
            _upload_file(upload_url, req.video_path, plan)
        except _FinalChunkUnknown as exc:
            raise UnknownOutcomeError(
                f"{exc.message} TikTok may hold the whole video; Repeater will ask TikTok.",
                state=state,
                code="upload_outcome_unknown",
            ) from exc
        return PublishResult(
            outcome=PublishOutcome.PROCESSING,
            state=state,
            poll_after_seconds=POLL_SECONDS,
            message="Uploaded to TikTok; TikTok is processing the video, which can take a few minutes.",
        )

    def poll(self, account: AccountContext, state: dict[str, Any]) -> PublishResult:
        publish_id = state.get("publish_id")
        if not publish_id:
            raise PermanentError("The TikTok upload id is missing; the post cannot be tracked.")
        data = _api_request(
            "POST",
            STATUS_URL,
            account.tokens.access_token,
            action="checking the TikTok post status",
            body={"publish_id": publish_id},
        )
        status = data.get("status")
        now = utcnow()
        if status == "PUBLISH_COMPLETE":
            return _completed(state, data, now)
        if status == "SEND_TO_USER_INBOX":
            return PublishResult(
                outcome=PublishOutcome.PUBLISHED,
                state=dict(state),
                message=Message(
                    "tiktok.inbox",
                    "The draft is waiting in the TikTok inbox: open the TikTok app to finish posting it.",
                ),
            )
        if status == "FAILED":
            raise _failure_error(str(data.get("fail_reason") or ""))
        if status == "PROCESSING_UPLOAD":
            _check_upload_alive(state, data, now)
        note = "" if status in ("PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD") else f" (status {status})"
        return PublishResult(
            outcome=PublishOutcome.PROCESSING,
            state=dict(state),
            poll_after_seconds=POLL_SECONDS,
            message=f"TikTok is still processing the video{note}.",
        )


def _init_body(
    post_info: dict[str, Any] | None, source: str, req: PublishRequest, plan: ChunkPlan
) -> dict[str, Any]:
    source_info = (
        {"source": SOURCE_PULL, "video_url": req.public_url} if source == SOURCE_PULL else plan.source_info()
    )
    return (
        {"post_info": post_info, "source_info": source_info}
        if post_info is not None
        else {"source_info": source_info}
    )


def _check_upload_alive(state: dict[str, Any], data: dict[str, Any], now: datetime) -> None:
    """A FILE_UPLOAD that still waits for bytes after the upload_url's hour is over can never
    finish, and nothing was published: the upload has to start over."""
    init_at = _parse_time(state.get("init_at"))
    if state.get("source") != SOURCE_FILE or init_at is None or now - init_at <= UPLOAD_URL_LIFETIME:
        return
    uploaded, size = data.get("uploaded_bytes"), state.get("video_size")
    if isinstance(uploaded, int) and isinstance(size, int) and uploaded >= size:
        return  # TikTok has every byte and is still moving on
    raise RestartPublish(
        "TikTok never received the whole video before the upload link expired, so nothing was "
        "published; the upload will start over.",
        code="upload_incomplete",
        retry_after=FAILED_RETRY_AFTER,
    )


def _first_post_id(data: dict[str, Any]) -> str | None:
    # TikTok's field name really is spelled "publicaly"; accept the corrected spelling too.
    ids = data.get("publicaly_available_post_id")
    if ids is None:
        ids = data.get("publicly_available_post_id")
    if isinstance(ids, list):
        ids = ids[0] if ids else None
    return str(ids) if ids not in (None, "") else None


def _expects_post_id(state: dict[str, Any]) -> bool:
    """Only a public Direct Post gets a publicaly_available_post_id, once it passed moderation."""
    return state.get("mode") == "direct" and state.get("privacy_level") == "PUBLIC_TO_EVERYONE"


def _completed(state: dict[str, Any], data: dict[str, Any], now: datetime) -> PublishResult:
    post_id = _first_post_id(data)
    username = state.get("username")
    permalink = f"https://www.tiktok.com/@{username}/video/{post_id}" if post_id and username else None
    if post_id:
        return PublishResult(
            outcome=PublishOutcome.PUBLISHED,
            platform_post_id=post_id,
            permalink=permalink,
            state=dict(state),
            message="Published on TikTok.",
        )
    if _expects_post_id(state):
        # The post is live, but TikTok hands out its id only after moderation. Keep asking for a
        # bounded time (never publishing again) so the dashboard gets the id and the link.
        seen = _parse_time(state.get("complete_seen_at")) or now
        deadline = seen + POST_ID_WAIT
        init_at = _parse_time(state.get("init_at"))
        if init_at is not None:
            deadline = min(deadline, init_at + POST_ID_MAX_AFTER_INIT)
        if now < deadline:
            return PublishResult(
                outcome=PublishOutcome.PROCESSING,
                state={**state, "complete_seen_at": seen.isoformat()},
                poll_after_seconds=POST_ID_POLL_SECONDS,
                message="Published on TikTok; waiting for TikTok's moderation to share the post link.",
            )
    return PublishResult(
        outcome=PublishOutcome.PUBLISHED,
        state=dict(state),
        message=(
            "Published on TikTok. TikTok shares the post link only for public posts that passed moderation."
        ),
    )


_FAIL_RETRYABLE = {"internal", "video_pull_failed", "photo_pull_failed", "publish_cancelled"}
_FAIL_MESSAGES = {
    "file_format_check_failed": "TikTok rejected the video format.",
    "duration_check_failed": "TikTok rejected the video length for this account.",
    "frame_rate_check_failed": "TikTok rejected the video frame rate (23-60 fps is required).",
    "picture_size_check_failed": "TikTok rejected the video dimensions (each side must be 360-4096 px).",
    "internal": "TikTok had a temporary problem processing the video.",
    "video_pull_failed": "TikTok could not fetch the video in time.",
    "photo_pull_failed": "TikTok could not fetch the photo in time.",
    "publish_cancelled": "The TikTok upload was cancelled before processing finished.",
    "auth_removed": "Access to the TikTok account was removed while the video was processing; reconnect it.",
    "spam_risk_too_many_posts": "TikTok's daily posting limit for this account was reached.",
    "spam_risk_user_banned_from_posting": "TikTok has blocked this account from posting new videos.",
    "spam_risk_text": "TikTok flagged the caption as spam; change the caption before posting again.",
    "spam_risk": "TikTok flagged this post as risky and refused it.",
}


def _failure_error(reason: str) -> PlatformError:
    message = _FAIL_MESSAGES.get(reason, "TikTok could not publish the video.")
    message += f" (TikTok fail reason: {reason or 'unknown'})"
    code = reason or None
    if reason == "auth_removed":
        return AuthError(message, code=code)
    if reason == "spam_risk_too_many_posts":
        # FAILED is final for this publish_id: nothing was posted. Start over once the daily cap
        # (a 24 h window TikTok does not disclose) has had time to reset.
        return RestartPublish(message, code=code, retry_after=DAILY_CAP_RETRY_AFTER)
    if reason in _FAIL_RETRYABLE:
        # This publish_id is finished for good; a new upload is needed.
        return RestartPublish(message, code=code, retry_after=FAILED_RETRY_AFTER)
    return PermanentError(message, code=code)


# --------------------------------------------------------------------------- listing (Display API)


def _remote_video(raw: dict[str, Any]) -> RemoteVideo | None:
    video_id = raw.get("id")
    if not video_id:
        return None
    created = raw.get("create_time")
    duration = raw.get("duration")
    extra = {"cover_image_url": raw["cover_image_url"]} if raw.get("cover_image_url") else {}
    return RemoteVideo(
        external_id=str(video_id),
        created_at=datetime.fromtimestamp(created, UTC) if isinstance(created, int | float) else None,
        title=str(raw.get("title") or ""),
        caption=str(raw.get("video_description") or ""),
        permalink=raw.get("share_url") or None,
        duration_s=float(duration) if isinstance(duration, int | float) else None,
        width=int(raw["width"]) if isinstance(raw.get("width"), int | float) else None,
        height=int(raw["height"]) if isinstance(raw.get("height"), int | float) else None,
        download_url=None,
        extra=extra,
    )


class TikTokLister:
    platform = Platform.TIKTOK

    def list_recent(
        self,
        account: AccountContext,
        *,
        since: datetime | None = None,
        limit: int = 50,
        config: dict | None = None,
    ) -> list[RemoteVideo]:
        """Public videos only (Display API), newest first; stops at the first video older
        than `since` (videos posted at exactly `since` are included)."""
        since = ensure_aware(since)
        out: list[RemoteVideo] = []
        cursor: int | None = None
        for _ in range(MAX_LIST_PAGES):
            if len(out) >= limit:
                break
            body: dict[str, Any] = {"max_count": min(MAX_LIST_PAGE_SIZE, limit - len(out))}
            if cursor is not None:
                body["cursor"] = cursor
            data = _api_request(
                "POST",
                VIDEO_LIST_URL,
                account.tokens.access_token,
                action="listing the TikTok videos",
                body=body,
                params={"fields": VIDEO_FIELDS},
            )
            videos = [v for v in (data.get("videos") or []) if isinstance(v, dict)]
            for raw in videos:
                video = _remote_video(raw)
                if video is None:
                    continue
                if since and video.created_at and video.created_at < since:
                    return out
                out.append(video)
                if len(out) >= limit:
                    return out
            next_cursor = data.get("cursor")
            if not data.get("has_more") or not videos or next_cursor is None or next_cursor == cursor:
                break
            cursor = next_cursor
        return out

    def download(self, account: AccountContext, remote: RemoteVideo, dest: Path) -> Path:
        raise NotSupportedError(
            "TikTok offers no official way to download video files. Upload the original file, use a "
            "Google Drive folder, or enable the unofficial importer for this source.",
            code="no_official_download",
        )


oauth = TikTokOAuth()
publisher = TikTokPublisher()
lister = TikTokLister()
