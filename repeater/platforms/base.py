"""Contracts shared by all platform adapters.

Every platform module implements some of these protocols and exposes module-level objects:

    repeater.platforms.tiktok    -> oauth: OAuthProvider, publisher: Publisher, lister: MediaLister,
                                    creator_info(account) -> TikTokCreatorInfo
    repeater.platforms.google    -> oauth: OAuthProvider        (YouTube + Google Drive scopes)
    repeater.platforms.youtube   -> publisher: Publisher, lister: MediaLister
    repeater.platforms.meta      -> oauth: OAuthProvider        (Facebook Login: Pages + linked IG accounts)
    repeater.platforms.instagram -> publisher: Publisher, lister: MediaLister
    repeater.platforms.facebook  -> publisher: Publisher, lister: MediaLister
    repeater.sources.drive       -> lister: MediaLister         (Google Drive folder)

Conventions for ConnectedAccount.extra (non-secret identifiers):
    tiktok    {"open_id", "username"}
    youtube   {"channel_id", "uploads_playlist_id", "custom_url", "google_sub"}
    gdrive    {"email", "google_sub"}
    facebook  {"page_id", "fb_user_id", "can_manage"}
    instagram {"ig_user_id", "page_id", "username", "fb_user_id"}
`fb_user_id` is the app-scoped Facebook user id that authorized the Page; Meta's
deauthorize/data-deletion callbacks identify users by it.
`google_sub` is the Google account id (OpenID "sub") of the login that authorized a YouTube channel or
a Drive folder. Revoking one Google token removes every scope that user granted the project, so the
dashboard revokes remotely only when no other active Google account has the same sub. Absent on
accounts connected before it was stored (then a remote revoke is skipped while any other Google
account is active) or when Google gave none.
`can_manage` (bool) is True when the connecting user holds the MANAGE task on the Page, which
listing the Page's videos needs; absent on accounts connected before it was stored.
The engine keeps its own bookkeeping in extra under keys starting with "_" (_disconnected_at,
_refreshed_at); other modules must not overwrite `extra` wholesale.

Other cross-module functions:
    repeater.platforms.tiktok.creator_info(account: AccountContext) -> TikTokCreatorInfo
    repeater.platforms.meta.parse_signed_request(signed_request: str) -> dict  (ValueError if invalid)

Adapters never touch the database. They receive an AccountContext and return dataclasses;
the engine/importer persist results. Adapters raise the PlatformError subclasses below and
never put tokens in exception messages.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from repeater.models import Platform
from repeater.timeutil import ensure_aware, utcnow

# --------------------------------------------------------------------------- errors


class PlatformError(Exception):
    """Base error. `code` is the platform's error code/string when known."""

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class RetryableError(PlatformError):
    """Transient: network errors, 5xx, processing not finished. Safe to retry later."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class RateLimitedError(RetryableError):
    """Platform rate limit or daily cap hit."""


class RestartPublish(RetryableError):
    """Raised by Publisher.poll(): the platform-side upload is gone (expired, failed or refused)
    and nothing was published, so the engine must call publish() again (after retry_after)
    instead of polling the same upload. Any other RetryableError from poll() means "poll again":
    the post may already be live, and re-publishing could post it twice."""


class UnknownOutcomeError(PlatformError):
    """Raised by Publisher.publish() when bytes may already have reached the platform but the
    result is unclear (lost final-chunk response, unreadable 2xx body, timeout after the last
    byte). Re-publishing from scratch could post the video twice. `state` carries whatever the
    adapter needs to find out what happened (publish_id, video_id, resumable session URI,
    container id ...). The engine stores it in scheduled_posts.publish_state and polls: poll()
    must resolve it to PUBLISHED, PROCESSING, RestartPublish (definitely nothing there) or a
    final error. With an empty `state` the engine marks the post needs-attention ("may be live")
    instead of retrying. `state` may hold secrets such as upload URLs: never log it."""

    def __init__(self, message: str, *, state: dict[str, Any] | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.state: dict[str, Any] = dict(state or {})


class AuthError(PlatformError):
    """Token invalid, expired beyond refresh, or permission revoked: the user must reconnect."""


class PermanentError(PlatformError):
    """Request can never succeed as-is (bad video spec, missing settings, policy rejection)."""


class NotSupportedError(PlatformError):
    """Operation not available on this platform through official APIs (e.g. YouTube download)."""


# --------------------------------------------------------------------------- tokens & accounts


@dataclass
class OAuthTokens:
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None  # access token expiry (UTC); None = does not expire
    refresh_expires_at: datetime | None = None
    scopes: list[str] = field(default_factory=list)
    token_type: str = "Bearer"  # noqa: S105 - OAuth token type, not a secret
    extra: dict[str, Any] = field(default_factory=dict)  # e.g. {"open_id": ...}, page tokens

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        d["refresh_expires_at"] = self.refresh_expires_at.isoformat() if self.refresh_expires_at else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OAuthTokens:
        def _dt(v: Any) -> datetime | None:
            return ensure_aware(datetime.fromisoformat(v)) if v else None

        return cls(
            access_token=d["access_token"],
            refresh_token=d.get("refresh_token"),
            expires_at=_dt(d.get("expires_at")),
            refresh_expires_at=_dt(d.get("refresh_expires_at")),
            scopes=list(d.get("scopes") or []),
            token_type=d.get("token_type") or "Bearer",
            extra=dict(d.get("extra") or {}),
        )

    def expires_within(self, delta: timedelta, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= (now or utcnow()) + delta

    @staticmethod
    def expiry_from_seconds(seconds: float | int | None, now: datetime | None = None) -> datetime | None:
        if not seconds:
            return None
        return (now or utcnow()) + timedelta(seconds=float(seconds))


@dataclass
class ConnectedAccountData:
    """One account produced by an OAuth exchange. A single Meta login can yield several
    (each Facebook Page and each Instagram professional account linked to a Page)."""

    platform: Platform
    external_id: str
    display_name: str
    tokens: OAuthTokens
    avatar_url: str | None = None
    extra: dict[str, Any] = field(
        default_factory=dict
    )  # non-secret: page_id, ig_user_id, username, channel_id


@dataclass
class AccountContext:
    """What adapters receive at runtime (decrypted tokens + identifiers)."""

    account_id: int
    platform: Platform
    external_id: str
    tokens: OAuthTokens
    extra: dict[str, Any] = field(default_factory=dict)
    display_name: str = ""


# --------------------------------------------------------------------------- import


@dataclass
class RemoteVideo:
    """A video found on a source."""

    external_id: str
    created_at: datetime | None = None
    title: str = ""
    caption: str = ""
    permalink: str | None = None
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    # Direct file URL from an official API when available (Instagram media_url, Facebook source,
    # Google Drive). None for TikTok/YouTube, which have no official download.
    download_url: str | None = None
    is_video: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class MediaLister(Protocol):
    platform: Platform

    def list_recent(
        self,
        account: AccountContext,
        *,
        since: datetime | None = None,
        limit: int = 50,
        config: dict | None = None,
    ) -> list[RemoteVideo]:
        """Newest-first list of the account's own videos (only videos, no images)."""
        ...

    def download(self, account: AccountContext, remote: RemoteVideo, dest: Path) -> Path:
        """Download the original file to `dest` (a file path). Raise NotSupportedError if the
        platform offers no official download."""
        ...


# --------------------------------------------------------------------------- publish


@dataclass(frozen=True)
class PlatformLimits:
    min_duration_s: float
    max_duration_s: float
    max_size_bytes: int
    max_caption_chars: int
    max_title_chars: int | None
    api_daily_cap: int  # platform-side per-account cap (approximate where undocumented)


LIMITS: dict[Platform, PlatformLimits] = {
    # TikTok: API accepts up to 10 min (creator_info.max_video_post_duration_sec decides); 4 GB;
    # title up to 2200 UTF-16 units; ~15 posts/day/creator.
    Platform.TIKTOK: PlatformLimits(1, 600, 4 * 1024**3, 2200, None, 15),
    # YouTube Shorts: vertical/square up to 3 min; title <= 100 chars; description <= 5000 bytes.
    # api_daily_cap 100 is the Google Cloud project's videos.insert quota shared by every channel,
    # not a per-channel cap; repeater.engine.scheduler enforces it project-wide.
    Platform.YOUTUBE: PlatformLimits(1, 180, 256 * 1024**3, 5000, 100, 100),
    # Instagram Reels via API: 3 s - 15 min, 300 MB, caption 2200 chars / 30 hashtags.
    Platform.INSTAGRAM: PlatformLimits(3, 900, 300 * 1024**2, 2200, None, 50),
    # Facebook Reels via API: 3-90 s (docs), 30 API reels / Page / 24 h.
    Platform.FACEBOOK: PlatformLimits(3, 90, 1024**3, 63206, 255, 30),
}

# Keys stored in RotationRule.settings per platform. Contract between the web form, the engine
# and the adapters. Values here are the defaults for a new rule.
RULE_SETTINGS_DEFAULTS: dict[str, dict[str, Any]] = {
    "tiktok": {
        # TikTok Content Sharing Guidelines: no default privacy; user must pick from
        # creator_info.privacy_level_options. Interaction toggles start off.
        "privacy_level": None,
        "allow_comment": False,
        "allow_duet": False,
        "allow_stitch": False,
        "brand_content_toggle": False,  # "Branded content" (paid partnership); cannot be SELF_ONLY
        "brand_organic_toggle": False,  # "Your brand"
        "is_aigc": False,
        # ISO timestamp when the user accepted "By posting, you agree to TikTok's Music Usage
        # Confirmation" for this rule. Publishing is refused until set.
        "music_consent_at": None,
        "mode": "direct",  # direct (Direct Post) | inbox (draft in the creator's TikTok inbox)
    },
    "youtube": {
        "privacy_status": "public",  # public | unlisted | private
        # No default: YouTube requires an explicit audience answer and the user must give it
        # (settings validation fails while this is None).
        "self_declared_made_for_kids": None,
        "category_id": "22",
        "notify_subscribers": False,
        "contains_synthetic_media": False,
        "tags": [],
    },
    "instagram": {"share_to_feed": True, "is_ai_generated": False},
    "facebook": {"is_ai_generated": False},
}


@dataclass
class PublishRequest:
    post_id: int
    video_path: Path  # absolute path to the encoded MP4
    public_url: str  # signed HTTPS URL of the same file, for pull-based uploads
    size_bytes: int
    duration_s: float
    width: int
    height: int
    title: str
    caption: str
    settings: dict[str, Any]
    thumbnail_path: Path | None = None
    fps: float | None = None  # frame rate of the encoded file (Video.fps); None = unknown
    # scheduled_posts.publish_state from earlier attempts (empty on the first). Adapters resume
    # or check what an earlier attempt left behind instead of uploading again.
    state: dict[str, Any] = field(default_factory=dict)


class PublishOutcome(enum.StrEnum):
    PUBLISHED = "published"
    PROCESSING = "processing"  # call poll() later with `state`


@dataclass
class PublishResult:
    outcome: PublishOutcome
    platform_post_id: str | None = None
    permalink: str | None = None
    # Persisted to scheduled_posts.publish_state and handed back to poll().
    state: dict[str, Any] = field(default_factory=dict)
    poll_after_seconds: int = 60
    message: str = ""


@runtime_checkable
class Publisher(Protocol):
    platform: Platform
    limits: PlatformLimits

    def validate(self, account: AccountContext, req: PublishRequest) -> list[str]:
        """Cheap local checks (duration, size, required settings). Returns problems; [] = ok."""
        ...

    def publish(self, account: AccountContext, req: PublishRequest) -> PublishResult:
        """Upload and publish (or start publishing).

        RetryableError means nothing went live: the engine calls publish() again later.
        UnknownOutcomeError means bytes may have been delivered: the engine polls instead.
        Adapters must raise UnknownOutcomeError (with state) from the moment the final byte or
        the final "finish"/"publish" request has been sent, until the outcome is known.
        `req.state` carries state saved by earlier attempts."""
        ...

    def poll(self, account: AccountContext, state: dict[str, Any]) -> PublishResult:
        """Check a PROCESSING publish. Return PROCESSING again if not finished. `state` may be
        incomplete (e.g. only a resumable session URI from an UnknownOutcomeError): resolve it
        by asking the platform. Raise RestartPublish only when the platform confirms nothing
        was published."""
        ...


# --------------------------------------------------------------------------- OAuth


@runtime_checkable
class OAuthProvider(Protocol):
    name: str  # "tiktok" | "google" | "meta"
    uses_pkce: bool

    def authorize_url(
        self, *, state: str, redirect_uri: str, code_challenge: str | None, purpose: str
    ) -> str:
        """URL to send the user to. `purpose` is the platform the user clicked
        ("tiktok", "youtube", "gdrive", "meta") and selects the scopes."""
        ...

    def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: str | None, purpose: str
    ) -> list[ConnectedAccountData]: ...

    def refresh(self, account: AccountContext) -> OAuthTokens:
        """Return fresh tokens (may include a rotated refresh token). Raise AuthError if the
        grant is no longer valid."""
        ...

    def revoke(self, account: AccountContext) -> None:
        """Best-effort revocation at the provider."""
        ...


# --------------------------------------------------------------------------- TikTok specifics


@dataclass
class TikTokCreatorInfo:
    """Result of POST /v2/post/publish/creator_info/query/. Must be fetched when rendering the
    TikTok post-settings form and before each post (Content Sharing Guidelines)."""

    nickname: str
    avatar_url: str | None
    privacy_level_options: list[str]
    comment_disabled: bool
    duet_disabled: bool
    stitch_disabled: bool
    max_video_post_duration_sec: int
    username: str | None = None
