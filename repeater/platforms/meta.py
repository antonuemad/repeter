"""Meta: Facebook Login (OAuth) for Facebook Pages and the Instagram professional accounts linked
to them, plus the small Graph API client shared by the Instagram and Facebook adapters.

One login yields several accounts: every Page the user can create content on (platform
"facebook") and every Instagram professional account linked to such a Page ("instagram"). Both
use the Page access token, which does not expire when it is derived from a long-lived user token.

Every token-authenticated Graph call carries `appsecret_proof` (HMAC-SHA256 of the access token
keyed by the app secret). The access token travels in the Authorization header, never in a URL.

Official references (checked 2026-10-14):
    https://developers.facebook.com/docs/facebook-login/guides/advanced/manual-flow
    https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived
    https://developers.facebook.com/docs/graph-api/securing-requests
    https://developers.facebook.com/docs/graph-api/guides/error-handling
    https://developers.facebook.com/docs/graph-api/overview/rate-limiting
    https://developers.facebook.com/docs/graph-api/reference/user/accounts/
    https://developers.facebook.com/docs/graph-api/reference/page/
    https://developers.facebook.com/docs/permissions
    https://developers.facebook.com/docs/development/create-an-app/app-dashboard/data-deletion-callback
    https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/error-codes
    https://developers.facebook.com/docs/video-api/reference/error-codes
    https://developers.facebook.com/docs/video-api/guides/get-videos
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import hmac
import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Self
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx

from repeater.config import get_settings
from repeater.models import Platform
from repeater.platforms.base import (
    AccountContext,
    AuthError,
    ConnectedAccountData,
    OAuthTokens,
    PermanentError,
    PlatformError,
    RateLimitedError,
    RetryableError,
)
from repeater.platforms.http import classify_http_error, client
from repeater.timeutil import utcnow

log = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com"
DIALOG_URL = "https://www.facebook.com/{version}/dialog/oauth"
FACEBOOK_WEB_URL = "https://www.facebook.com"

# Facebook Login permissions (names checked against the permissions reference). Instagram API
# with Facebook Login needs instagram_basic + instagram_content_publish + pages_read_engagement;
# Page Reels need pages_show_list + pages_read_engagement + pages_manage_posts.
#
# ads_read: every Instagram publishing endpoint Repeater calls (GET content_publishing_limit,
# POST /{ig-user-id}/media, GET /{container-id}, POST /{ig-user-id}/media_publish) documents that
# "if the app user was granted a role on the Page via the Business Manager" (a business portfolio,
# which is how Magic Lap runs its Pages), the app also needs ONE of ads_management or ads_read.
# Without it Meta answers 403 code 200 "App lacks ads_management permission for the target" and the
# account loops between connected and needs-reconnect. ads_read is the smaller of the two options
# (read-only Ads Insights; Repeater never calls an ads endpoint), so that is the one requested.
# business_management stays: it is the documented alternative to ads_read / ads_management for
# reading GET /{ig-user-id}/media (the importer), and was requested before this change.
# Sources (checked 2026-10-14):
#   https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media_publish
#   https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/content_publishing_limit
#   https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-container
#   https://developers.facebook.com/docs/instagram-platform/content-publishing (Requirements)
SCOPES = (
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "instagram_basic",
    "instagram_content_publish",
    "ads_read",
    "business_management",
)
# Permissions an account needs to publish. A platform whose permissions were switched off in the
# Facebook dialog is not connected: its first post would fail with a permission error.
# ads_read is not listed: it is only needed when the Page role comes from a business portfolio,
# which Repeater cannot tell at connect time (see SCOPES).
PLATFORM_PERMISSIONS: dict[Platform, tuple[str, ...]] = {
    Platform.FACEBOOK: ("pages_show_list", "pages_read_engagement", "pages_manage_posts"),
    Platform.INSTAGRAM: (
        "pages_show_list",
        "pages_read_engagement",
        "instagram_basic",
        "instagram_content_publish",
    ),
}
ADS_SCOPES = frozenset({"ads_read", "ads_management"})
# "meta" connects everything the login returns; "facebook"/"instagram" keep only that platform so
# a click on "Connect Instagram" never silently adds Facebook Pages to the rotation.
PURPOSE_PLATFORMS: dict[str, frozenset[Platform]] = {
    "meta": frozenset({Platform.FACEBOOK, Platform.INSTAGRAM}),
    "facebook": frozenset({Platform.FACEBOOK}),
    "instagram": frozenset({Platform.INSTAGRAM}),
}
PUBLISH_TASK = "CREATE_CONTENT"
# Listing a Page's videos (GET /{page-id}/videos) needs a Page token requested by a person who can
# perform the MANAGE task (https://developers.facebook.com/docs/video-api/guides/get-videos).
MANAGE_TASK = "MANAGE"
PAGE_FIELDS = "id,name,access_token,tasks,picture{url},instagram_business_account{id,username,name,profile_picture_url,account_type}"

API_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_PAGES = 20
MAX_SIGNED_REQUEST_CHARS = 8192
_VERSION_RE = re.compile(r"^v\d+\.\d+$")
_SECRET_PARAMS = frozenset({"access_token", "appsecret_proof", "client_secret", "code", "fb_exchange_token"})
_ADS_RE = re.compile(r"\bads_(?:management|read)\b", re.IGNORECASE)

# --------------------------------------------------------------------------- configuration


def app_credentials() -> tuple[str, str]:
    settings = get_settings()
    if not (settings.meta_app_id and settings.meta_app_secret):
        raise PermanentError(
            "Facebook/Instagram is not configured on this server "
            "(set REPEATER_META_APP_ID and REPEATER_META_APP_SECRET).",
            code="not_configured",
        )
    return settings.meta_app_id, settings.meta_app_secret


def graph_version() -> str:
    version = get_settings().meta_graph_version.strip()
    if not _VERSION_RE.fullmatch(version):
        raise PermanentError(
            f"REPEATER_META_GRAPH_VERSION must look like 'v26.0' (got {version!r}).", code="not_configured"
        )
    return version


def appsecret_proof(access_token: str) -> str:
    """Hex HMAC-SHA256 of the access token keyed by the app secret (Graph API 'Secure Requests')."""
    _, secret = app_credentials()
    return hmac.new(secret.encode(), access_token.encode(), hashlib.sha256).hexdigest()


def node(object_id: Any) -> str:
    """A Graph object id as a single, escaped path segment."""
    text = str(object_id or "").strip()
    if not text:
        raise PermanentError("A Meta object id is missing.", code="missing_id")
    return quote(text, safe="")


def absolute_facebook_url(link: Any) -> str | None:
    """Graph returns some permalinks (e.g. video permalink_url) as paths on www.facebook.com."""
    if not isinstance(link, str) or not link.strip():
        return None
    link = link.strip()
    if link.startswith(("https://", "http://")):
        return link
    return FACEBOOK_WEB_URL + (link if link.startswith("/") else f"/{link}")


def parse_graph_time(value: Any) -> datetime | None:
    """Graph datetimes are ISO 8601 ('2019-09-25T17:18:30+0000') or Unix timestamps."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        if isinstance(value, int | float) or (isinstance(value, str) and value.strip().isdigit()):
            return datetime.fromtimestamp(float(value), UTC)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.strip())
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


# --------------------------------------------------------------------------- error mapping

AUTH_CODES = frozenset({102, 190})
AUTH_SUBCODES = frozenset({458, 459, 460, 463, 464, 467, 492})
PERMISSION_CODES = frozenset({10, *range(200, 300)})
RATE_LIMIT_CODES = frozenset({4, 17, 32, 341, 613, *range(80000, 80015)})
TRANSIENT_CODES = frozenset({1, 2})
POLICY_BLOCK_CODE = 368

IG_PUBLISH_LIMIT_SUBCODE = 2207042
IG_NOT_READY_SUBCODE = 2207027
IG_SPAM_SUBCODE = 2207051
IG_RESTRICTED_SUBCODE = 2207050
# Instagram subcodes whose documented fix is "try again" or "generate a new container".
IG_RETRYABLE_SUBCODES = frozenset({2207001, 2207003, 2207006, 2207008, 2207020, 2207032, 2207052, 2207053})
# Facebook video upload/session subcodes whose documented fix is "retry the upload".
FB_RETRYABLE_SUBCODES = frozenset({1363011, 1363019, 1363021, 1363030, 1363033, 1363037, 1363041})
FB_NO_UPLOAD_PERMISSION = 1363042
# "There was a problem uploading your video file" (video_reels / Video API error table, action
# "Retry several times"). Meta returns it as the top-level `code`, often with a 1363xxx subcode, but
# the Video API table headlines the column "Error Subcode", so it is honoured in both places.
FB_UPLOAD_RETRY_CODE = 6000

DEFAULT_RATE_LIMIT_RETRY = 15 * 60.0
CUSTOM_RATE_LIMIT_RETRY = 60 * 60.0  # 613: e.g. 30 API-published Reels per Page per 24 h
IG_PUBLISH_LIMIT_RETRY = 60 * 60.0  # the 24 h window frees slots one by one
POLICY_BLOCK_RETRY = 60 * 60.0
TRANSIENT_RETRY = 120.0

RECONNECT = "Reconnect the account from Repeater's Accounts page."
EXPLANATIONS: dict[int, str] = {
    190: f"Meta no longer accepts this account's access (it expired or was removed). {RECONNECT}",
    102: f"Meta no longer accepts this account's access (the session is invalid). {RECONNECT}",
    458: f"Repeater was removed from the Facebook account that connected this account. {RECONNECT}",
    459: "Facebook needs the account owner to log in at facebook.com and resolve a security check, "
    f"then {RECONNECT[0].lower()}{RECONNECT[1:]}",
    460: f"The Facebook password changed, so the old access no longer works. {RECONNECT}",
    463: f"The Facebook access expired. {RECONNECT}",
    464: "Facebook needs the account owner to log in at facebook.com and confirm the account, then "
    f"{RECONNECT[0].lower()}{RECONNECT[1:]}",
    467: f"The Facebook access is no longer valid. {RECONNECT}",
    492: "The Facebook user who connected this account no longer has a suitable role on the Page. "
    f"{RECONNECT}",
    368: "Meta temporarily blocked this action for a policy reason; Repeater will try again later.",
    506: "Meta refused a duplicate post. Change the caption before posting the same video again.",
    IG_PUBLISH_LIMIT_SUBCODE: (
        "Instagram's limit of API-published posts per 24 hours was reached for this account; "
        "posting resumes when the limit frees up."
    ),
    IG_NOT_READY_SUBCODE: "Instagram is still processing the video.",
    IG_SPAM_SUBCODE: (
        "Instagram blocked this post as suspected spam. Check the account in the Instagram app before "
        "publishing to it again."
    ),
    IG_RESTRICTED_SUBCODE: (
        "The Instagram account is restricted or inactive. Sign in to the Instagram app and complete any "
        "steps Instagram asks for."
    ),
    2207001: "Instagram had a temporary server error; Repeater will try again.",
    2207003: "Instagram timed out downloading the video; Repeater will try again.",
    2207006: "Instagram could not find the prepared upload; Repeater will create a new one.",
    2207008: "Instagram's prepared upload is not available yet or expired; Repeater will try again.",
    2207020: "The Instagram upload expired; Repeater will create a new one.",
    2207032: "Instagram could not create the post; Repeater will try again.",
    2207052: (
        "Instagram could not download the video from Repeater's public link. Make sure the server's "
        "public address (REPEATER_BASE_URL) is reachable from the internet."
    ),
    2207053: "Instagram had an unknown upload error; Repeater will try again with a new upload.",
    2207026: (
        "Instagram does not support this video format (it needs MP4 or MOV with H.264/HEVC video and AAC "
        "audio, moov atom at the front)."
    ),
    2207010: "The caption is too long for Instagram (at most 2200 characters, 30 hashtags and 20 @ tags).",
    2207040: "The caption has too many @ tags for Instagram (at most 20).",
    1363040: "Facebook Reels need an aspect ratio between 16:9 and 9:16.",
    1363127: "Facebook Reels need a resolution of at least 540x960 (1080x1920 recommended).",
    1363128: "Facebook Reels must be between 3 and 90 seconds long.",
    1363129: "Facebook Reels need a frame rate between 24 and 60 fps.",
    1363022: "The video file is too small for Facebook (at least 1 KB).",
    1363023: "The video file is too large for Facebook.",
    1363024: "Facebook does not support this video format.",
    1363025: "The video is too short for Facebook.",
    1363026: "The video is too long for Facebook.",
    1363031: "Facebook did not detect a video in the uploaded file.",
    1363032: "Facebook did not detect a video in the uploaded file.",
    FB_NO_UPLOAD_PERMISSION: f"Facebook refused the upload: no permission to upload videos to this Page. {RECONNECT}",
}
PERMISSION_EXPLANATION = (
    "Meta refused the request because a permission is missing or was removed. Reconnect the account and "
    "allow every permission Repeater asks for; the Facebook user must still be able to create content "
    "on the Page."
)
# The documented failure of a Page role granted through a business portfolio without ads_read /
# ads_management (Instagram error table: 403, code 200, "App lacks ads_management permission").
ADS_PERMISSION_EXPLANATION = (
    "Instagram refused the request because the Facebook user who connected this account has the Page "
    "role through a Meta Business portfolio, which also needs the ads_read permission. Reconnect the "
    "account and allow ads_read (Repeater never uses it to read or change ads)."
)
ADS_PERMISSION_HINT = (
    "If this Facebook user has the Page role through a Meta Business portfolio, Instagram publishing "
    "also needs the ads_read permission: reconnect the account and allow it."
)
UPLOAD_RETRY_EXPLANATION = "Facebook had a problem with the video upload; Repeater will try again."
RATE_LIMIT_EXPLANATION = "Meta's API rate limit was reached; Repeater will try again later."
TRANSIENT_EXPLANATION = "Meta had a temporary problem; Repeater will try again."


class PermissionDeniedError(AuthError):
    """A Graph permission error (codes 10 and 200-299, or no upload permission): the token itself
    is valid but lacks a permission or a Page task. Publishing treats it like any AuthError
    (reconnecting asks for the permissions again); read paths that reconnecting cannot fix, such as
    listing a Page's videos without the MANAGE task, turn it into a PermanentError instead."""


@dataclasses.dataclass(frozen=True)
class GraphErrorInfo:
    code: int | None
    subcode: int | None
    message: str
    error_type: str
    is_transient: bool
    trace_id: str | None


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    return None


def error_info(error: dict[str, Any]) -> GraphErrorInfo:
    user_msg = error.get("error_user_msg")
    message = user_msg if isinstance(user_msg, str) and user_msg.strip() else error.get("message")
    trace = error.get("fbtrace_id")
    return GraphErrorInfo(
        code=as_int(error.get("code")),
        subcode=as_int(error.get("error_subcode")),
        message=str(message or "").strip()[:300],
        error_type=str(error.get("type") or ""),
        is_transient=error.get("is_transient") is True,
        trace_id=str(trace) if trace else None,
    )


def rate_limit_retry_after(headers: httpx.Headers, default: float) -> float:
    """Seconds to wait, from X-Business-Use-Case-Usage (estimated_time_to_regain_access, minutes)
    or Retry-After, else `default`."""
    minutes = 0.0
    raw = headers.get("x-business-use-case-usage")
    if raw:
        try:
            usage = json.loads(raw)
        except ValueError:
            usage = None
        if isinstance(usage, dict):
            for entries in usage.values():
                for entry in entries if isinstance(entries, list) else []:
                    value = entry.get("estimated_time_to_regain_access") if isinstance(entry, dict) else None
                    if isinstance(value, int | float) and not isinstance(value, bool):
                        minutes = max(minutes, float(value))
    if minutes > 0:
        return minutes * 60.0
    try:
        seconds = float(headers.get("retry-after", ""))
    except ValueError:
        return default
    return seconds if seconds > 0 else default


def _describe(info: GraphErrorInfo, action: str, explanation: str | None) -> str:
    if explanation is None:
        explanation = f"Meta returned an error while {action}" + (
            f": {info.message}" if info.message else "."
        )
        if not explanation.endswith((".", "!", "?")):
            explanation += "."
    ref = "/".join(str(c) for c in (info.code, info.subcode) if c is not None)
    details = [f"Meta error {ref}"] if ref else []
    if info.trace_id:
        details.append(f"trace {info.trace_id}")
    return explanation + (f" ({', '.join(details)})" if details else "")


def _code_str(info: GraphErrorInfo) -> str | None:
    value = info.subcode if info.subcode is not None else info.code
    return str(value) if value is not None else None


def graph_error(
    error: dict[str, Any], *, status: int, headers: httpx.Headers | None = None, action: str
) -> PlatformError:
    """Map a Graph API error object ({"error": {...}}) to Repeater's error classes."""
    info = error_info(error)
    headers = headers or httpx.Headers()
    code, subcode = info.code, info.subcode
    is_auth = code in AUTH_CODES or (info.error_type == "OAuthException" and subcode in AUTH_SUBCODES)
    explanation = None
    if subcode is not None and (is_auth or subcode not in AUTH_SUBCODES):
        explanation = EXPLANATIONS.get(subcode)
    explanation = explanation or EXPLANATIONS.get(code if code is not None else -1)
    if explanation is None and code in PERMISSION_CODES and _ADS_RE.search(info.message):
        explanation = ADS_PERMISSION_EXPLANATION
    kw: dict[str, Any] = {"code": _code_str(info), "status": status}

    def msg(default: str | None = None) -> str:
        return _describe(info, action, explanation or default)

    if is_auth:
        return AuthError(msg(), **kw)
    if subcode == FB_NO_UPLOAD_PERMISSION or code in PERMISSION_CODES:
        return PermissionDeniedError(msg(PERMISSION_EXPLANATION), **kw)
    if subcode == IG_PUBLISH_LIMIT_SUBCODE:
        retry = rate_limit_retry_after(headers, IG_PUBLISH_LIMIT_RETRY)
        return RateLimitedError(msg(), retry_after=retry, **kw)
    if subcode in (IG_SPAM_SUBCODE, IG_RESTRICTED_SUBCODE):
        return PermanentError(msg(), **kw)
    if code in RATE_LIMIT_CODES:
        default = CUSTOM_RATE_LIMIT_RETRY if code == 613 else DEFAULT_RATE_LIMIT_RETRY
        return RateLimitedError(
            msg(RATE_LIMIT_EXPLANATION), retry_after=rate_limit_retry_after(headers, default), **kw
        )
    if code == POLICY_BLOCK_CODE:
        return RetryableError(msg(), retry_after=POLICY_BLOCK_RETRY, **kw)
    if (
        subcode in IG_RETRYABLE_SUBCODES
        or subcode in FB_RETRYABLE_SUBCODES
        or subcode == IG_NOT_READY_SUBCODE
    ):
        return RetryableError(msg(), retry_after=TRANSIENT_RETRY, **kw)
    if explanation is None and FB_UPLOAD_RETRY_CODE in (code, subcode):
        # Known permanent subcodes (format, duration ...) keep their explanation and stay permanent;
        # the engine's bounded retries are the stop condition for everything else.
        return RetryableError(msg(UPLOAD_RETRY_EXPLANATION), retry_after=TRANSIENT_RETRY, **kw)
    if info.is_transient or code in TRANSIENT_CODES or status >= 500:
        return RetryableError(msg(TRANSIENT_EXPLANATION), retry_after=TRANSIENT_RETRY, **kw)
    if explanation is not None or status < 400:
        return PermanentError(msg(), **kw)
    return classify_http_error(httpx.Response(status, headers=headers), msg(), code=kw["code"])


def scrub(text: str, secrets: tuple[str | None, ...]) -> str:
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


def redact(exc: PlatformError, secrets: tuple[str | None, ...]) -> PlatformError:
    exc.message = scrub(exc.message, secrets)
    exc.args = (exc.message,)
    return exc


def json_or_none(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def parse_response(resp: httpx.Response, action: str, secrets: tuple[str | None, ...] = ()) -> dict[str, Any]:
    """Return the JSON object of a successful Graph response or raise the mapped error."""
    payload = json_or_none(resp)
    error = payload.get("error") if isinstance(payload, dict) else None
    if resp.status_code < 400 and isinstance(payload, dict) and not isinstance(error, dict):
        return payload
    if isinstance(error, dict):
        exc = graph_error(error, status=resp.status_code, headers=resp.headers, action=action)
        info = error_info(error)
        log.warning(
            "Meta API error while %s: status=%s code=%s subcode=%s trace=%s",
            action,
            resp.status_code,
            info.code,
            info.subcode,
            info.trace_id,
        )
    elif resp.status_code < 400:
        exc = RetryableError(f"Meta returned an unreadable response while {action}.", status=resp.status_code)
    else:
        exc = classify_http_error(resp, f"Meta returned HTTP {resp.status_code} while {action}.")
        log.warning("Meta HTTP error while %s: status=%s", action, resp.status_code)
    raise redact(exc, secrets)


# --------------------------------------------------------------------------- Graph client


def _next_page_params(paging: Any) -> dict[str, str] | None:
    """Query parameters for the next page. Uses the cursor when present, otherwise the query of
    `paging.next` (minus credentials), so the token never travels inside a followed URL."""
    if not isinstance(paging, dict) or not paging.get("next"):
        return None
    cursors = paging.get("cursors")
    after = cursors.get("after") if isinstance(cursors, dict) else None
    if isinstance(after, str) and after:
        return {"after": after}
    query = urlsplit(str(paging["next"])).query
    params = {k: v for k, v in parse_qsl(query, keep_blank_values=True) if k not in _SECRET_PARAMS}
    return params or None


class Graph:
    """Graph API client bound to one access token (or none, for the OAuth token endpoint)."""

    def __init__(
        self,
        access_token: str | None = None,
        *,
        http: httpx.Client | None = None,
        timeout: httpx.Timeout = API_TIMEOUT,
    ) -> None:
        self._token = access_token or None
        self._proof = appsecret_proof(access_token) if access_token else None
        self._base = f"{GRAPH_URL}/{graph_version()}"
        self._own = http is None
        self._http = http or client(timeout=timeout)

    def with_token(self, access_token: str) -> Graph:
        """Another client on the same connection pool (closing it leaves the pool open)."""
        return Graph(access_token, http=self._http)

    def close(self) -> None:
        if self._own:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        action: str,
        secrets: tuple[str | None, ...] = (),
    ) -> dict[str, Any]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        data = {k: v for k, v in (data or {}).items() if v is not None} if method != "GET" else None
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
            if data is None:
                params["appsecret_proof"] = self._proof
            else:
                data["appsecret_proof"] = self._proof
        try:
            resp = self._http.request(
                method,
                f"{self._base}/{path.lstrip('/')}",
                params=params or None,
                data=data or None,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise RetryableError(f"Could not reach Meta while {action} ({type(exc).__name__}).") from exc
        return parse_response(resp, action, (self._token, self._proof, *secrets))

    def get(
        self, path: str, params: dict[str, Any] | None = None, *, action: str, **kw: Any
    ) -> dict[str, Any]:
        return self.request("GET", path, params=params, action=action, **kw)

    def post(
        self, path: str, data: dict[str, Any] | None = None, *, action: str, **kw: Any
    ) -> dict[str, Any]:
        return self.request("POST", path, data=data, action=action, **kw)

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        action: str,
        max_pages: int = DEFAULT_MAX_PAGES,
        require_complete: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Yield the objects of `data` across pages (at most `max_pages` requests)."""
        current = dict(params or {})
        for _ in range(max_pages):
            body = self.get(path, current, action=action)
            data = body.get("data")
            items = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
            yield from items
            following = _next_page_params(body.get("paging"))
            if following is None or (not items and not require_complete):
                return
            merged = {**current, **following}
            if merged == current:
                if require_complete:
                    raise RetryableError(
                        "The platform repeated a listing cursor; retry the check.", code="listing_incomplete"
                    )
                return
            current = merged
        if require_complete:
            raise RetryableError(
                "The platform listing exceeded the safe page limit. No checkpoint was advanced; "
                "contact support or use the original files in Drive.",
                code="listing_incomplete",
            )


# --------------------------------------------------------------------------- signed_request


def _b64url_decode(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        raise ValueError("signed_request is not base64url")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("signed_request is not base64url") from exc


def parse_signed_request(signed_request: str) -> dict:
    """Verify and decode a Meta signed_request (data deletion / deauthorize callbacks):
    base64url(HMAC-SHA256(payload, app secret)) "." base64url(JSON payload).
    Raises ValueError when it is malformed, tampered with or not HMAC-SHA256."""
    secret = get_settings().meta_app_secret
    if not secret:
        raise ValueError("Meta app secret is not configured")
    if not isinstance(signed_request, str) or len(signed_request) > MAX_SIGNED_REQUEST_CHARS:
        raise ValueError("signed_request is missing or too long")
    parts = signed_request.strip().split(".")
    if len(parts) != 2:
        raise ValueError("signed_request must have exactly two parts")
    encoded_sig, encoded_payload = parts
    signature = _b64url_decode(encoded_sig)
    expected = hmac.new(secret.encode(), encoded_payload.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("signed_request signature does not match")
    try:
        data = json.loads(_b64url_decode(encoded_payload))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("signed_request payload is not JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("signed_request payload is not an object")
    if str(data.get("algorithm") or "").upper() != "HMAC-SHA256":
        raise ValueError("signed_request algorithm must be HMAC-SHA256")
    return data


# --------------------------------------------------------------------------- OAuth


def _check_purpose(purpose: str) -> frozenset[Platform]:
    try:
        return PURPOSE_PLATFORMS[purpose]
    except KeyError:
        raise ValueError(f"unsupported Meta OAuth purpose: {purpose!r}") from None


def _token_from(body: dict[str, Any], action: str) -> str:
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise RetryableError(f"Meta returned no access token while {action}.", code="no_access_token")
    return token


def _exchange_for_long_lived_user_token(graph: Graph, *, code: str, redirect_uri: str) -> str:
    app_id, secret = app_credentials()
    action = "finishing the Facebook sign-in"
    short = _token_from(
        graph.get(
            "oauth/access_token",
            {"client_id": app_id, "redirect_uri": redirect_uri, "client_secret": secret, "code": code},
            action=action,
            secrets=(secret, code),
        ),
        action,
    )
    action = "getting long-lived Facebook access"
    return _token_from(
        graph.get(
            "oauth/access_token",
            {
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": secret,
                "fb_exchange_token": short,
            },
            action=action,
            secrets=(secret, short),
        ),
        action,
    )


def _granted_permissions(user: Graph) -> tuple[list[str], list[str]]:
    granted: list[str] = []
    declined: list[str] = []
    for item in user.paginate("me/permissions", action="reading the granted Facebook permissions"):
        name = item.get("permission")
        if not isinstance(name, str):
            continue
        (granted if item.get("status") == "granted" else declined).append(name)
    return granted, declined


def _page_avatar(page: dict[str, Any]) -> str | None:
    picture = page.get("picture")
    data = picture.get("data") if isinstance(picture, dict) else None
    url = data.get("url") if isinstance(data, dict) else None
    return url if isinstance(url, str) and url else None


def _page_tokens(page_token: str, scopes: list[str]) -> OAuthTokens:
    # Page tokens derived from a long-lived user token have no expiry date.
    return OAuthTokens(access_token=page_token, expires_at=None, scopes=list(scopes))


def _facebook_account(
    page: dict[str, Any], token: str, fb_user_id: str, scopes: list[str]
) -> ConnectedAccountData:
    page_id = str(page["id"])
    tasks = page.get("tasks") if isinstance(page.get("tasks"), list) else []
    return ConnectedAccountData(
        platform=Platform.FACEBOOK,
        external_id=page_id,
        display_name=str(page.get("name") or f"Facebook Page {page_id}"),
        tokens=_page_tokens(token, scopes),
        avatar_url=_page_avatar(page),
        # can_manage: the user holds the MANAGE task, which listing the Page's videos requires.
        extra={"page_id": page_id, "fb_user_id": fb_user_id, "can_manage": MANAGE_TASK in tasks},
    )


def _instagram_account(
    page: dict[str, Any], token: str, fb_user_id: str, scopes: list[str]
) -> ConnectedAccountData | None:
    ig = page.get("instagram_business_account")
    if not isinstance(ig, dict) or not ig.get("id"):
        return None
    ig_user_id = str(ig["id"])
    username = str(ig.get("username") or "")
    avatar = ig.get("profile_picture_url")
    return ConnectedAccountData(
        platform=Platform.INSTAGRAM,
        external_id=ig_user_id,
        display_name=username or str(ig.get("name") or f"Instagram account {ig_user_id}"),
        tokens=_page_tokens(token, scopes),
        avatar_url=avatar if isinstance(avatar, str) and avatar else None,
        extra={
            "ig_user_id": ig_user_id,
            **({"account_type": ig["account_type"]} if ig.get("account_type") else {}),
            "page_id": str(page["id"]),
            "username": username,
            "fb_user_id": fb_user_id,
        },
    )


def _publishable_pages(user: Graph) -> list[tuple[dict[str, Any], str]]:
    pages: list[tuple[dict[str, Any], str]] = []
    params = {"fields": PAGE_FIELDS, "limit": DEFAULT_PAGE_SIZE}
    for page in user.paginate("me/accounts", params, action="listing your Facebook Pages"):
        tasks = page.get("tasks") if isinstance(page.get("tasks"), list) else []
        token = page.get("access_token")
        if page.get("id") and PUBLISH_TASK in tasks and isinstance(token, str) and token:
            pages.append((page, token))
    return pages


def _platform_label(platform: Platform) -> str:
    return "Facebook Pages" if platform == Platform.FACEBOOK else "Instagram"


def _permissions_declined_error(missing: dict[Platform, list[str]]) -> PermanentError:
    parts = [
        f"{_platform_label(platform)} needs {', '.join(names)}"
        for platform, names in sorted(missing.items(), key=lambda item: item[0].value)
    ]
    return PermanentError(
        "Facebook did not give Repeater every permission it needs to publish: "
        + "; ".join(parts)
        + ". Connect again and keep every permission switched on in the Facebook dialog.",
        code="permissions_declined",
    )


def _no_accounts_error(
    platforms: frozenset[Platform], declined: list[str], missing: dict[Platform, list[str]]
) -> PermanentError:
    if missing:
        return _permissions_declined_error(missing)
    if platforms == {Platform.INSTAGRAM}:
        message = (
            "None of your Facebook Pages has a linked Instagram professional (Business or Creator) account "
            "that you can publish to. Link the Instagram account to a Page in Meta Business Suite, select "
            "both in the Facebook dialog, then connect again."
        )
    else:
        message = (
            "Facebook did not return any Page you can publish to. Select at least one Page in the Facebook "
            "dialog and make sure you have full control or content-creation access to it, then connect again."
        )
    required = {permission for platform in platforms for permission in PLATFORM_PERMISSIONS.get(platform, ())}
    absent = [p for p in SCOPES if p in declined and p in required]
    if absent:
        message += f" These permissions were declined and are needed: {', '.join(absent)}."
    return PermanentError(message, code="no_publishable_accounts")


class MetaOAuth:
    name = "meta"
    uses_pkce = False

    def authorize_url(
        self, *, state: str, redirect_uri: str, code_challenge: str | None, purpose: str
    ) -> str:
        _check_purpose(purpose)
        app_id, _ = app_credentials()
        params = {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
            "scope": ",".join(SCOPES),
            # Re-asks permissions the user declined before, so reconnecting can fix them.
            "auth_type": "rerequest",
        }
        return DIALOG_URL.format(version=graph_version()) + "?" + urlencode(params, safe=",")

    def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: str | None, purpose: str
    ) -> list[ConnectedAccountData]:
        platforms = _check_purpose(purpose)
        with Graph() as graph:
            user_token = _exchange_for_long_lived_user_token(graph, code=code, redirect_uri=redirect_uri)
            with graph.with_token(user_token) as user:
                me = user.get("me", {"fields": "id,name"}, action="reading your Facebook profile")
                fb_user_id = str(me.get("id") or "")
                if not fb_user_id:
                    raise PermanentError("Facebook did not return your user id.", code="missing_user_id")
                granted, declined = _granted_permissions(user)
                pages = _publishable_pages(user)
        accounts: list[ConnectedAccountData] = []
        seen: set[tuple[Platform, str]] = set()
        missing: dict[Platform, list[str]] = {}
        granted_set = set(granted)
        for page, token in pages:
            candidates = [
                _facebook_account(page, token, fb_user_id, granted),
                _instagram_account(page, token, fb_user_id, granted),
            ]
            for account in candidates:
                if account is None or account.platform not in platforms:
                    continue
                # A platform whose permission was switched off in the dialog would only fail at its
                # first scheduled post: leave it out and say why.
                absent = [p for p in PLATFORM_PERMISSIONS[account.platform] if p not in granted_set]
                if absent:
                    missing.setdefault(account.platform, absent)
                    continue
                key = (account.platform, account.external_id)
                if key not in seen:
                    seen.add(key)
                    accounts.append(account)
        if not accounts:
            raise _no_accounts_error(platforms, declined, missing)
        for platform, names in missing.items():
            log.warning(
                "Meta login: %s accounts were not connected because these permissions were declined: %s",
                platform.value,
                ", ".join(names),
            )
        if Platform.INSTAGRAM in platforms and not ADS_SCOPES & granted_set:
            log.info(
                "Meta login: neither ads_read nor ads_management was granted; Instagram publishing fails "
                "for users whose Page role comes from a business portfolio"
            )
        log.info(
            "Meta login: %d Page(s) with %s, %d account(s) returned",
            len(pages),
            PUBLISH_TASK,
            len(accounts),
        )
        return accounts

    def refresh(self, account: AccountContext) -> OAuthTokens:
        """Page tokens derived from a long-lived user token do not expire, so there is nothing to
        refresh. Meta invalidates them when the user's role, password or authorization changes;
        API calls then fail with code 190 and the account must be reconnected."""
        tokens = account.tokens
        if tokens.expires_at is not None and tokens.expires_at <= utcnow():
            raise AuthError(
                f"The Facebook access for this account expired. {RECONNECT}", code="token_expired"
            )
        return dataclasses.replace(tokens, scopes=list(tokens.scopes), extra=dict(tokens.extra))

    def revoke(self, account: AccountContext) -> None:
        """Deliberately a no-op. Revoking at Meta (DELETE /{user-id}/permissions) would remove the
        whole Facebook Login grant and disconnect every sibling Page and Instagram account connected in
        the same login. Disconnecting deletes the stored token in Repeater; users fully revoke access
        from Facebook Settings > Apps and Websites, which calls the deauthorize callback."""


oauth = MetaOAuth()
