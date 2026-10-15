"""Instagram Reels publishing and media import through the Instagram API with Facebook Login
(host graph.facebook.com, Page access token, see repeater.platforms.meta).

Publishing is pull-based: Instagram cURLs the encoded file from Repeater's signed public URL.
    1. GET  /{ig-user-id}/content_publishing_limit   refuse early when the 24 h quota is used up
    2. POST /{ig-user-id}/media (media_type=REELS)   -> container id          (publish)
    3. GET  /{container-id}?fields=status_code,status                          (poll)
    4. POST /{ig-user-id}/media_publish creation_id  -> media id               (poll, once FINISHED)
A container can be published only once, so after step 4 has been sent the adapter never creates
a new container for the same post unless Instagram reports the old one EXPIRED or ERROR, or
refused every attempt to publish it with answers that prove nothing went live (container not
found, not ready ...). Step 4 can fail with an unknown outcome (network error, 5xx, code 1/2):
the container keeps its id in the saved state, every poll reads the container status BEFORE any
media_publish (PUBLISHED means it landed), and after MAX_PUBLISH_ATTEMPTS unconfirmed calls the
post goes to needs-attention ("check the account") instead of being sent again.

Official references (checked 2026-10-14):
    https://developers.facebook.com/docs/instagram-platform/content-publishing
    https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media
    https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media_publish
    https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/content_publishing_limit
    https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-container
    https://developers.facebook.com/docs/instagram-platform/reference/instagram-media
    https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/error-codes
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from repeater.config import get_settings
from repeater.models import Platform
from repeater.platforms import meta
from repeater.platforms.base import (
    LIMITS,
    RULE_SETTINGS_DEFAULTS,
    AccountContext,
    PermanentError,
    PlatformError,
    PublishOutcome,
    PublishRequest,
    PublishResult,
    RateLimitedError,
    RemoteVideo,
    RestartPublish,
    RetryableError,
)
from repeater.platforms.http import stream_download
from repeater.timeutil import ensure_aware, utcnow

log = logging.getLogger(__name__)

FIRST_POLL_SECONDS = 30
POLL_SECONDS = 60  # Meta recommends checking a container about once per minute
MIN_ASPECT_RATIO = 0.01
MAX_ASPECT_RATIO = 10.0
MAX_HASHTAGS = 30
MAX_MENTIONS = 20
MEDIA_FIELDS = "id,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,caption"
RECOVERY_FIELDS = "id,caption,timestamp,permalink"
RECOVERY_LOOKBACK = 5
MAX_PUBLISH_ATTEMPTS = 5
CLOCK_SKEW = timedelta(minutes=5)
LIST_PAGE_SIZE = 50
MAX_LIST_PAGES = 20
CAPTION_KEY_CHARS = 200
_EXCLUDED_PRODUCT_TYPES = frozenset({"STORY", "AD"})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})  # noqa: S104 - compared, not bound

_HASHTAG_RE = re.compile(r"(?<![\w#&])#\w")
_MENTION_RE = re.compile(r"(?<![\w@])@[\w.]")
_SUBCODE_RE = re.compile(r"\b(\d{7})\b")
_STATE_CONTAINER_KEYS = ("container_id", "created_at", "media_id", "publish_attempts", "publish_unknown")
# media_publish refusals that prove the call did not publish: the container is not ready, not found
# or expired (Instagram error table). Anything else (5xx, code 1/2, 2207001, network errors) may
# have landed although the answer was an error.
_DEFINITIVE_REFUSALS = frozenset({"2207027", "2207006", "2207008", "2207020", "2207052", "2207053"})


# --------------------------------------------------------------------------- helpers


def _settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    return {**RULE_SETTINGS_DEFAULTS["instagram"], **(raw or {})}


def _ig_user_id(account: AccountContext) -> str:
    ig_user_id = str(account.extra.get("ig_user_id") or account.external_id or "")
    if not ig_user_id:
        raise PermanentError(
            "This Instagram account has no Instagram id stored; reconnect it.", code="missing_ig_user_id"
        )
    return ig_user_id


def caption_key(text: Any) -> str:
    """Comparable form of a caption. Instagram drops '@' from captions read back by apps that lack
    admin-equivalent Page tasks, so it is removed before comparing."""
    return " ".join(str(text or "").replace("@", "").split()).casefold()[:CAPTION_KEY_CHARS]


def _caption_problems(caption: str) -> list[str]:
    problems: list[str] = []
    limit = LIMITS[Platform.INSTAGRAM].max_caption_chars
    if len(caption) > limit:
        problems.append(f"The caption has {len(caption)} characters; Instagram allows at most {limit}.")
    hashtags = len(_HASHTAG_RE.findall(caption))
    if hashtags > MAX_HASHTAGS:
        problems.append(f"The caption has {hashtags} hashtags; Instagram allows at most {MAX_HASHTAGS}.")
    mentions = len(_MENTION_RE.findall(caption))
    if mentions > MAX_MENTIONS:
        problems.append(f"The caption has {mentions} @ tags; Instagram allows at most {MAX_MENTIONS}.")
    return problems


def _file_problems(req: PublishRequest) -> list[str]:
    limits = LIMITS[Platform.INSTAGRAM]
    problems: list[str] = []
    if req.duration_s < limits.min_duration_s:
        problems.append(
            f"The video is shorter than Instagram's minimum of {limits.min_duration_s:g} seconds."
        )
    if req.duration_s > limits.max_duration_s:
        problems.append(
            f"The video is {req.duration_s:.0f} s long; Instagram Reels published through the API can be "
            f"at most {limits.max_duration_s / 60:g} minutes."
        )
    if req.size_bytes <= 0:
        problems.append("The video file is empty.")
    elif req.size_bytes > limits.max_size_bytes:
        problems.append(
            f"The video is {req.size_bytes / 1024**2:.0f} MB; Instagram accepts at most "
            f"{limits.max_size_bytes // 1024**2} MB."
        )
    if req.width <= 0 or req.height <= 0:
        problems.append("The video dimensions are unknown; re-process the video.")
    else:
        ratio = req.width / req.height
        if not MIN_ASPECT_RATIO <= ratio <= MAX_ASPECT_RATIO:
            problems.append(
                f"The video's aspect ratio ({req.width}x{req.height}) is outside Instagram's range of "
                "0.01:1 to 10:1."
            )
    return problems


def _public_url_problem(url: str) -> str | None:
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.hostname in _LOCAL_HOSTS:
        return (
            "Instagram downloads the video from a public link, but the server has no public address. Set "
            "REPEATER_BASE_URL to the dashboard's public https address."
        )
    return None


def _processing(state: dict[str, Any], message: str, seconds: int = POLL_SECONDS) -> PublishResult:
    return PublishResult(
        outcome=PublishOutcome.PROCESSING, state=dict(state), poll_after_seconds=seconds, message=message
    )


@contextlib.contextmanager
def _permission_hint(account: AccountContext) -> Iterator[None]:
    """A permission error on a publishing endpoint is, for a user whose Page role comes from a
    business portfolio, the missing ads_read / ads_management permission (Meta's own message rarely
    says so): add that hint when the account was not granted either."""
    try:
        yield
    except meta.PermissionDeniedError as exc:
        granted = set(account.tokens.scopes or [])
        if not granted & meta.ADS_SCOPES and "ads_" not in exc.message:
            exc.message = f"{exc.message} {meta.ADS_PERMISSION_HINT}"
            exc.args = (exc.message,)
        raise


# --------------------------------------------------------------------------- publish steps


def _check_publishing_quota(graph: meta.Graph, ig_user_id: str) -> None:
    body = graph.get(
        f"{meta.node(ig_user_id)}/content_publishing_limit",
        {"fields": "quota_usage,config"},
        action="checking Instagram's publishing limit",
    )
    data = body.get("data")
    entry = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
    usage, total = meta.as_int(entry.get("quota_usage")), meta.as_int(config.get("quota_total"))
    if usage is None or total is None or total <= 0 or usage < total:
        return
    duration = meta.as_int(config.get("quota_duration"))
    hours = duration / 3600 if duration and duration > 0 else 24
    raise RateLimitedError(
        f"This Instagram account already published {usage} posts through the API in the last {hours:g} "
        f"hours, which is Instagram's limit ({total}); Repeater will try again later.",
        code="publishing_limit",
        retry_after=meta.IG_PUBLISH_LIMIT_RETRY,
    )


def _create_container(
    graph: meta.Graph, ig_user_id: str, req: PublishRequest, settings: dict[str, Any]
) -> str:
    data: dict[str, Any] = {
        "media_type": "REELS",
        "video_url": req.public_url,
        "share_to_feed": "true" if meta.flag(settings.get("share_to_feed")) else "false",
    }
    if req.caption:
        data["caption"] = req.caption
    if meta.flag(settings.get("is_ai_generated")):
        data["is_ai_generated"] = "true"
    body = graph.post(
        f"{meta.node(ig_user_id)}/media",
        data,
        action="sending the video to Instagram",
        secrets=(req.public_url,),
    )
    container_id = body.get("id")
    if not container_id:
        raise RetryableError("Instagram did not return an upload id.", code="missing_container_id")
    return str(container_id)


def _published(graph: meta.Graph, state: dict[str, Any], media_id: str) -> PublishResult:
    permalink: str | None = None
    try:
        body = graph.get(
            meta.node(media_id), {"fields": "permalink"}, action="reading the Instagram post link"
        )
        link = body.get("permalink")
        permalink = link if isinstance(link, str) and link else None
    except PlatformError as exc:
        # The reel is live; a missing link must not turn a successful publish into a failure.
        log.warning(
            "Instagram permalink lookup failed for media %s: %s", media_id, exc.code or type(exc).__name__
        )
    return PublishResult(
        outcome=PublishOutcome.PUBLISHED,
        platform_post_id=media_id,
        permalink=permalink,
        state=dict(state),
        message="Published on Instagram.",
    )


def _refused_before_publishing(exc: RetryableError) -> bool:
    """Whether Instagram's answer proves this media_publish call did not publish: a 4xx whose
    subcode says the container is not ready, not found or expired."""
    return exc.status is not None and 400 <= exc.status < 500 and exc.code in _DEFINITIVE_REFUSALS


def _count_failed_publish(state: dict[str, Any], *, unknown: bool) -> None:
    state["publish_attempts"] = int(state.get("publish_attempts") or 0) + 1
    if unknown:
        state["publish_unknown"] = int(state.get("publish_unknown") or 0) + 1


def _publish_given_up(state: dict[str, Any]) -> PlatformError:
    """media_publish failed MAX_PUBLISH_ATTEMPTS times and the container still reads FINISHED (the
    status was read right before this decision, so a call that landed would show PUBLISHED).

    If every refusal proved nothing went live, the prepared upload is useless: send the video again.
    If any call had an unknown outcome, sending again could post it twice: ask the user to check."""
    if state.get("publish_unknown"):
        return PermanentError(
            "Instagram did not confirm whether the reel was published (its answers to the publish "
            "request were errors or never arrived) and the upload is still waiting. The reel may be "
            "live: check the Instagram account before publishing this video again.",
            code="publish_unconfirmed",
        )
    _reset_container(state)
    return RestartPublish(
        "Instagram kept refusing to publish the prepared upload; Repeater will send the video again.",
        code="publish_refused",
        retry_after=meta.TRANSIENT_RETRY,
    )


def _publish_container(
    graph: meta.Graph, ig_user_id: str, container_id: str, state: dict[str, Any]
) -> PublishResult:
    try:
        body = graph.post(
            f"{meta.node(ig_user_id)}/media_publish",
            {"creation_id": container_id},
            action="publishing the Instagram reel",
        )
    except RateLimitedError:
        raise  # rejected before publishing: nothing went live
    except RetryableError as exc:
        # Not ready yet, or an outcome we cannot know (network error, 5xx). The next poll reads the
        # container status first, so a reel that did go live is found instead of posted twice. This
        # call never starts over by itself: only the next poll can, after reading that status.
        log.info("Instagram media_publish for container %s not confirmed: %s", container_id, exc.code)
        _count_failed_publish(state, unknown=not _refused_before_publishing(exc))
        return _processing(state, "Instagram has not confirmed the publish yet; checking again shortly.")
    media_id = body.get("id")
    if not media_id:
        _count_failed_publish(state, unknown=True)
        return _processing(state, "Instagram did not return the new post id yet; checking again shortly.")
    # Saved before any further call so a later poll never publishes the container again.
    state["media_id"] = str(media_id)
    return _published(graph, state, str(media_id))


def _recover_published(graph: meta.Graph, ig_user_id: str, state: dict[str, Any]) -> PublishResult:
    """The container says PUBLISHED but the media id was lost (e.g. the media_publish response
    never arrived): look for the reel among the newest media."""
    created_at = meta.parse_graph_time(state.get("created_at"))
    wanted = state.get("caption_key")
    try:
        items = list(
            graph.paginate(
                f"{meta.node(ig_user_id)}/media",
                {"fields": RECOVERY_FIELDS, "limit": RECOVERY_LOOKBACK},
                action="looking up the published Instagram reel",
                max_pages=1,
            )
        )
    except PlatformError as exc:
        log.warning("Instagram recovery lookup failed: %s", exc.code or type(exc).__name__)
        items = []
    match = _find_recovered(items, created_at, wanted)
    if match is None:
        return PublishResult(
            outcome=PublishOutcome.PUBLISHED,
            state=dict(state),
            message="Published on Instagram (Repeater could not identify the new post's link).",
        )
    state["media_id"] = str(match["id"])
    link = match.get("permalink")
    return PublishResult(
        outcome=PublishOutcome.PUBLISHED,
        platform_post_id=str(match["id"]),
        permalink=link if isinstance(link, str) and link else None,
        state=dict(state),
        message="Published on Instagram.",
    )


def _find_recovered(
    items: list[dict[str, Any]], created_at: datetime | None, wanted: Any
) -> dict[str, Any] | None:
    candidates: list[tuple[datetime | None, dict[str, Any]]] = []
    for item in items[:RECOVERY_LOOKBACK]:
        if not item.get("id"):
            continue
        posted = meta.parse_graph_time(item.get("timestamp"))
        if created_at is not None and (posted is None or posted < created_at - CLOCK_SKEW):
            continue
        if isinstance(wanted, str) and caption_key(item.get("caption")) != wanted:
            continue
        candidates.append((posted, item))
    if not candidates:
        return None
    # The earliest match after the container was created is the one this post produced.
    candidates.sort(key=lambda c: c[0].timestamp() if c[0] else float("inf"))
    return candidates[0][1]


def _reset_container(state: dict[str, Any]) -> None:
    for key in _STATE_CONTAINER_KEYS:
        state.pop(key, None)


def _container_error(status: Any, state: dict[str, Any]) -> PlatformError:
    text = str(status or "").strip()[:300]
    match = _SUBCODE_RE.search(text)
    subcode = int(match.group(1)) if match else None
    explanation = meta.EXPLANATIONS.get(subcode or -1)
    reason = explanation or (
        f"Instagram could not process the video: {text}."
        if text
        else "Instagram could not process the video (no reason given). Check it meets Instagram's Reels "
        "specs: MP4/MOV, H.264 or HEVC, AAC audio, 23-60 fps, 3 s to 15 min."
    )
    code = str(subcode) if subcode else "container_error"
    if subcode in meta.IG_RETRYABLE_SUBCODES:
        _reset_container(state)
        return RestartPublish(reason, code=code, retry_after=meta.TRANSIENT_RETRY)
    return PermanentError(reason, code=code)


# --------------------------------------------------------------------------- publisher


class InstagramPublisher:
    platform = Platform.INSTAGRAM
    limits = LIMITS[Platform.INSTAGRAM]

    def validate(self, account: AccountContext, req: PublishRequest) -> list[str]:
        problems = _file_problems(req)
        problems.extend(_caption_problems(req.caption or ""))
        url_problem = _public_url_problem(req.public_url)
        if url_problem:
            problems.append(url_problem)
        if not (account.extra.get("ig_user_id") or account.external_id):
            problems.append("This Instagram account has no Instagram id stored; reconnect it.")
        return problems

    def publish(self, account: AccountContext, req: PublishRequest) -> PublishResult:
        container_id = req.state.get("container_id")
        if container_id:
            # An earlier attempt already created this post's container (and may have tried to
            # publish it): never create a second one. poll() publishes it, finds it published, or
            # restarts when Instagram reports it expired or broken.
            return _processing(
                req.state,
                "An earlier upload to Instagram exists; checking what became of it.",
                FIRST_POLL_SECONDS,
            )
        problems = self.validate(account, req)
        if problems:
            raise PermanentError(" ".join(problems), code="invalid_video")
        ig_user_id = _ig_user_id(account)
        settings = _settings(req.settings)
        with _permission_hint(account), meta.Graph(account.tokens.access_token) as graph:
            _check_publishing_quota(graph, ig_user_id)
            created_at = utcnow()
            container_id = _create_container(graph, ig_user_id, req, settings)
        log.info("Instagram container created: post=%s container=%s", req.post_id, container_id)
        return PublishResult(
            outcome=PublishOutcome.PROCESSING,
            state={
                "container_id": container_id,
                "created_at": created_at.isoformat(),
                "ig_user_id": ig_user_id,
                "caption_key": caption_key(req.caption),
            },
            poll_after_seconds=FIRST_POLL_SECONDS,
            message="Sent to Instagram; Instagram is downloading and processing the video.",
        )

    def poll(self, account: AccountContext, state: dict[str, Any]) -> PublishResult:
        """Note: on EXPIRED (and retryable ERRORs) the container keys are removed from `state` in
        place and RestartPublish(code="container_expired") is raised: publish() must run again.
        The same restart happens after MAX_PUBLISH_ATTEMPTS media_publish refusals that all prove
        nothing went live; any unknown outcome among them ends in a PermanentError instead."""
        container_id = state.get("container_id")
        if not container_id:
            raise PermanentError(
                "The Instagram upload id is missing; the post cannot be tracked.", code="missing_container"
            )
        ig_user_id = str(state.get("ig_user_id") or _ig_user_id(account))
        with _permission_hint(account), meta.Graph(account.tokens.access_token) as graph:
            if state.get("media_id"):
                return _published(graph, state, str(state["media_id"]))
            info = graph.get(
                meta.node(container_id),
                {"fields": "status_code,status"},
                action="checking the Instagram upload",
            )
            status_code = str(info.get("status_code") or "").upper()
            if status_code == "FINISHED":
                if int(state.get("publish_attempts") or 0) >= MAX_PUBLISH_ATTEMPTS:
                    raise _publish_given_up(state)
                return _publish_container(graph, ig_user_id, str(container_id), state)
            if status_code == "PUBLISHED":
                return _recover_published(graph, ig_user_id, state)
        if status_code == "ERROR":
            raise _container_error(info.get("status"), state)
        if status_code == "EXPIRED":
            _reset_container(state)
            raise RestartPublish(
                "The Instagram upload expired before it was published (uploads last 24 hours); Repeater "
                "will send the video again.",
                code="container_expired",
            )
        note = "" if status_code in ("IN_PROGRESS", "") else f" (status {status_code})"
        return _processing(state, f"Instagram is still processing the video{note}.")


# --------------------------------------------------------------------------- lister


def _remote_video(item: dict[str, Any]) -> RemoteVideo | None:
    if item.get("media_type") != "VIDEO" or not item.get("id"):
        return None
    product_type = str(item.get("media_product_type") or "")
    if product_type in _EXCLUDED_PRODUCT_TYPES:
        return None
    media_url = item.get("media_url") if isinstance(item.get("media_url"), str) else None
    extra: dict[str, Any] = {"media_product_type": product_type or None}
    if item.get("thumbnail_url"):
        extra["thumbnail_url"] = item["thumbnail_url"]
    if not media_url:
        # Meta omits media_url when the video contains copyrighted material (e.g. licensed music).
        extra["media_url_missing"] = True
    return RemoteVideo(
        external_id=str(item["id"]),
        created_at=meta.parse_graph_time(item.get("timestamp")),
        caption=str(item.get("caption") or ""),
        permalink=item.get("permalink") or None,
        download_url=media_url or None,
        extra=extra,
    )


def _max_download_bytes() -> int:
    return max(get_settings().max_upload_mb * 1024**2, LIMITS[Platform.INSTAGRAM].max_size_bytes)


class InstagramLister:
    platform = Platform.INSTAGRAM

    def list_recent(
        self,
        account: AccountContext,
        *,
        since: datetime | None = None,
        limit: int = 50,
        config: dict | None = None,
    ) -> list[RemoteVideo]:
        """Newest-first videos (feed videos and reels; no images, albums or stories) posted at or
        after `since`."""
        if limit <= 0:
            return []
        since = ensure_aware(since)
        ig_user_id = _ig_user_id(account)
        params: dict[str, Any] = {"fields": MEDIA_FIELDS, "limit": LIST_PAGE_SIZE}
        if since is not None:
            params["since"] = int(since.timestamp())
        out: list[RemoteVideo] = []
        with meta.Graph(account.tokens.access_token) as graph:
            pages = graph.paginate(
                f"{meta.node(ig_user_id)}/media",
                params,
                action="listing the Instagram posts",
                max_pages=MAX_LIST_PAGES,
                require_complete=bool((config or {}).get("require_complete")),
            )
            for item in pages:
                posted = meta.parse_graph_time(item.get("timestamp"))
                if since is not None and posted is not None and posted < since:
                    break
                video = _remote_video(item)
                if video is not None:
                    out.append(video)
                    if len(out) >= limit:
                        break
        out.sort(key=lambda v: v.created_at.timestamp() if v.created_at else float("-inf"), reverse=True)
        return out

    def download(self, account: AccountContext, remote: RemoteVideo, dest: Path) -> Path:
        """Fetch a fresh media_url (CDN links expire) and stream the file to `dest`."""
        with meta.Graph(account.tokens.access_token) as graph:
            body = graph.get(
                meta.node(remote.external_id),
                {"fields": "media_url"},
                action="getting the Instagram video file",
            )
        url = body.get("media_url")
        if not isinstance(url, str) or not url:
            raise PermanentError(
                "Instagram does not provide the file for this video, usually because it contains licensed "
                "music or was flagged for copyright. Upload the original video file to Repeater (or add it "
                "to the Google Drive folder) instead.",
                code="media_url_unavailable",
            )
        if not url.startswith("https://"):
            raise PermanentError("Instagram returned an unexpected file link.", code="bad_media_url")
        return stream_download(url, dest, max_bytes=_max_download_bytes())


publisher = InstagramPublisher()
lister = InstagramLister()
