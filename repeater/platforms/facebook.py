"""Facebook Page Reels publishing and Page video import (Graph API with a Page access token, see
repeater.platforms.meta).

Publishing uploads the local encoded file (no public URL needed):
    1. POST /{page-id}/video_reels upload_phase=start           -> video_id
    2. POST rupload.facebook.com/video-upload/{version}/{video_id}  binary body,
       headers Authorization: OAuth <page token>, offset, file_size (resumable via status)
    3. POST /{page-id}/video_reels upload_phase=finish video_state=PUBLISHED
    4. GET  /{video-id}?fields=status until the publishing phase completes          (poll)
Finishing the same video_id twice cannot create a second reel, so step 3 is retried in place;
a brand-new upload only happens when Facebook reports the video expired or the upload failed.

Official references (checked 2026-10-14):
    https://developers.facebook.com/docs/video-api/guides/reels-publishing
    https://developers.facebook.com/docs/graph-api/reference/page/video_reels
    https://developers.facebook.com/docs/video-api/guides/get-videos
    https://developers.facebook.com/docs/graph-api/reference/video/
    https://developers.facebook.com/docs/video-api/reference/error-codes
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Iterator
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import httpx

from repeater.config import get_settings
from repeater.models import Platform
from repeater.platforms import meta
from repeater.platforms.base import (
    LIMITS,
    RULE_SETTINGS_DEFAULTS,
    AccountContext,
    AuthError,
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
from repeater.platforms.http import classify_http_error, client, retry_after_seconds, stream_download
from repeater.timeutil import ensure_aware

log = logging.getLogger(__name__)

RUPLOAD_URL = "https://rupload.facebook.com/video-upload/{version}/{video_id}"
UPLOAD_TIMEOUT = httpx.Timeout(30.0, connect=15.0, read=600.0, write=600.0)
UPLOAD_CHUNK_BYTES = 1024 * 1024
UPLOAD_ATTEMPTS = 3
FINISH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
POLL_SECONDS = 30
MAX_TITLE_CHARS = 255
MIN_SHORT_SIDE = 540
MIN_LONG_SIDE = 960
MIN_ASPECT_RATIO = 9 / 16
MAX_ASPECT_RATIO = 16 / 9
MIN_FPS = 24.0
MAX_FPS = 60.0
FPS_TOLERANCE = 0.01
FFPROBE_TIMEOUT_SECONDS = 20
LIST_FIELDS = "id,description,created_time,updated_time,permalink_url,length,source"
LIST_EDGES = (("video_reels", "reels"), ("videos", "videos"))
LIST_PAGE_SIZE = 50
MAX_LIST_PAGES = 20
_PUBLISHED_PHASE = frozenset({"complete", "completed"})
_STARTED_PHASE = frozenset({"in_progress", "complete", "completed"})

_sleep = time.sleep  # replaced in tests


# --------------------------------------------------------------------------- helpers


def _settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    return {**RULE_SETTINGS_DEFAULTS["facebook"], **(raw or {})}


def _page_id(account: AccountContext) -> str:
    page_id = str(account.extra.get("page_id") or account.external_id or "")
    if not page_id:
        raise PermanentError(
            "This Facebook Page has no Page id stored; reconnect it.", code="missing_page_id"
        )
    return page_id


def probe_fps(path: Path) -> float | None:
    """Average frame rate of the first video stream via ffprobe, or None when unknown."""
    settings = get_settings()
    cmd = [
        settings.ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate,r_frame_rate",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SECONDS, check=False
        )
        streams = json.loads(proc.stdout or "{}").get("streams") or []
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if proc.returncode != 0 or not streams or not isinstance(streams[0], dict):
        return None
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            rate = Fraction(str(streams[0].get(key) or ""))
        except (ValueError, ZeroDivisionError):
            continue
        if rate > 0:
            return float(rate)
    return None


def _size_problems(req: PublishRequest) -> list[str]:
    limits = LIMITS[Platform.FACEBOOK]
    problems: list[str] = []
    if req.duration_s < limits.min_duration_s:
        problems.append(
            f"The video is shorter than Facebook Reels' minimum of {limits.min_duration_s:g} seconds."
        )
    if req.duration_s > limits.max_duration_s:
        problems.append(
            f"The video is {req.duration_s:.0f} s long; Facebook Reels published through the API can be at "
            f"most {limits.max_duration_s:g} seconds."
        )
    if req.size_bytes <= 0:
        problems.append("The video file is empty.")
    elif req.size_bytes > limits.max_size_bytes:
        problems.append(f"The video is larger than {limits.max_size_bytes // 1024**2} MB.")
    if len(req.caption or "") > limits.max_caption_chars:
        problems.append(f"The description is longer than Facebook's {limits.max_caption_chars} characters.")
    return problems


def _picture_problems(req: PublishRequest) -> list[str]:
    if req.width <= 0 or req.height <= 0:
        return ["The video dimensions are unknown; re-process the video."]
    problems: list[str] = []
    short, long = sorted((req.width, req.height))
    if short < MIN_SHORT_SIDE or long < MIN_LONG_SIDE:
        problems.append(
            f"The video is {req.width}x{req.height}; Facebook Reels need at least 540x960 (1080x1920 recommended)."
        )
    ratio = req.width / req.height
    if not MIN_ASPECT_RATIO - 1e-3 <= ratio <= MAX_ASPECT_RATIO + 1e-3:
        problems.append(
            f"The video's aspect ratio ({req.width}x{req.height}) must be between 16:9 and 9:16 for Facebook Reels."
        )
    fps = req.fps
    if not fps and req.video_path and Path(req.video_path).is_file():
        fps = probe_fps(req.video_path)
    if fps is not None and not MIN_FPS - FPS_TOLERANCE <= fps <= MAX_FPS + FPS_TOLERANCE:
        problems.append(f"The video runs at {fps:.2f} fps; Facebook Reels need 24 to 60 fps.")
    return problems


def _file_size(path: Path) -> int:
    try:
        size = Path(path).stat().st_size
    except OSError as exc:
        raise PermanentError(
            "The encoded video file is missing on the server; re-process the video.", code="file_missing"
        ) from exc
    if size <= 0:
        raise PermanentError("The encoded video file is empty; re-process the video.", code="file_empty")
    return size


def _read_status(graph: meta.Graph, video_id: str) -> dict[str, Any]:
    body = graph.get(meta.node(video_id), {"fields": "status"}, action="checking the Facebook reel")
    status = body.get("status")
    return status if isinstance(status, dict) else {}


def _phase(status: dict[str, Any], name: str) -> dict[str, Any]:
    value = status.get(name)
    return value if isinstance(value, dict) else {}


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _publishing_started(graph: meta.Graph, video_id: str) -> bool:
    """Best effort: whether an earlier finish request reached Facebook."""
    try:
        status = _read_status(graph, video_id)
    except PlatformError:
        return False
    publishing = _phase(status, "publishing_phase")
    return _lower(publishing.get("status")) in _STARTED_PHASE or _lower(publishing.get("publish_status")) in (
        "published",
        "scheduled",
    )


# --------------------------------------------------------------------------- upload (rupload)


def _iter_file(path: Path, offset: int) -> Iterator[bytes]:
    with Path(path).open("rb") as fh:
        fh.seek(offset)
        while chunk := fh.read(UPLOAD_CHUNK_BYTES):
            yield chunk


def _upload_error(resp: httpx.Response, token: str) -> PlatformError:
    payload: Any
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    status = resp.status_code
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        exc = meta.graph_error(
            payload["error"], status=status, headers=resp.headers, action="uploading the video"
        )
        return meta.redact(exc, (token,))
    debug = payload.get("debug_info") if isinstance(payload, dict) else None
    if isinstance(debug, dict):
        kind = str(debug.get("type") or "upload error")
        detail = meta.scrub(str(debug.get("message") or "")[:300], (token,))
        message = f"Facebook rejected the uploaded video data ({kind})" + (f": {detail}" if detail else ".")
        if status in (401, 403) and not debug.get("retriable"):
            return AuthError(message + f" {meta.RECONNECT}", code=kind, status=status)
        if debug.get("retriable") or kind in (
            "OffsetInvalidError",
            "PartialRequestError",
            "ProcessingFailedError",
        ):
            return RetryableError(message, code=kind, status=status, retry_after=meta.TRANSIENT_RETRY)
        return PermanentError(message, code=kind, status=status)
    if status == 429:
        return RateLimitedError(
            "Facebook throttled the upload.", status=status, retry_after=retry_after_seconds(resp, 60.0)
        )
    if status < 400:
        return RetryableError("Facebook did not confirm that the whole video arrived.", status=status)
    return classify_http_error(resp, f"Facebook rejected the video upload (HTTP {status}).")


def _bytes_received(graph: meta.Graph, video_id: str, size: int) -> int:
    """Resume offset from the upload status (`bytes_transfered`, spelled as in the API)."""
    try:
        status = _read_status(graph, video_id)
    except (RetryableError, PermanentError):
        return 0
    value = _phase(status, "uploading_phase").get("bytes_transfered")
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= size:
        return value
    return 0


def _upload(graph: meta.Graph, video_id: str, path: Path, size: int, token: str) -> None:
    url = RUPLOAD_URL.format(version=meta.graph_version(), video_id=meta.node(video_id))
    offset = 0
    error: PlatformError = RetryableError("The upload to Facebook did not complete.")
    with client(timeout=UPLOAD_TIMEOUT) as http:
        for attempt in range(UPLOAD_ATTEMPTS):
            if attempt:
                _sleep(RETRY_BACKOFF_SECONDS * attempt)
                offset = _bytes_received(graph, video_id, size)
                if offset >= size:
                    return
            headers = {
                "Authorization": f"OAuth {token}",
                "offset": str(offset),
                "file_size": str(size),
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size - offset),
            }
            try:
                resp = http.post(url, content=_iter_file(path, offset), headers=headers)
            except httpx.HTTPError as exc:
                error = RetryableError(
                    f"Network error while uploading the video to Facebook ({type(exc).__name__})."
                )
                continue
            body = meta.json_or_none(resp)
            if resp.status_code == 200 and isinstance(body, dict) and body.get("success") is True:
                return
            error = _upload_error(resp, token)
            if isinstance(error, RateLimitedError) or not isinstance(error, RetryableError):
                raise error
    raise error


# --------------------------------------------------------------------------- start / finish


def _start(graph: meta.Graph, page_id: str) -> str:
    body = graph.post(
        f"{meta.node(page_id)}/video_reels",
        {"upload_phase": "start"},
        action="starting the Facebook reel upload",
    )
    video_id = body.get("video_id")
    if not video_id:
        raise RetryableError("Facebook did not return an upload id.", code="missing_video_id")
    return str(video_id)


def _finish_params(video_id: str, req: PublishRequest, settings: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {"upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED"}
    if req.caption:
        data["description"] = req.caption
    title = " ".join((req.title or "").split())[:MAX_TITLE_CHARS]
    if title:
        data["title"] = title
    if meta.flag(settings.get("is_ai_generated")):
        data["is_ai_generated"] = "true"
    return data


def _finish(graph: meta.Graph, page_id: str, video_id: str, data: dict[str, Any]) -> str | None:
    """Send the finish request, retrying the same video_id on transient failures. Returns the
    post id when Facebook includes one."""
    error: PlatformError = RetryableError("Facebook did not confirm the reel was published.")
    for attempt in range(FINISH_ATTEMPTS):
        if attempt:
            _sleep(RETRY_BACKOFF_SECONDS * attempt)
        try:
            body = graph.post(
                f"{meta.node(page_id)}/video_reels", data, action="publishing the Facebook reel"
            )
        except PlatformError as exc:
            # An earlier attempt may have landed even though its response was lost.
            if attempt and _publishing_started(graph, video_id):
                return None
            if isinstance(exc, RateLimitedError) or not isinstance(exc, RetryableError):
                raise
            error = exc
            continue
        if body.get("success") is False:
            detail = str(body.get("message") or "").strip()[:300]
            raise PermanentError(
                "Facebook did not publish the reel" + (f": {detail}" if detail else "."),
                code="finish_refused",
            )
        post_id = body.get("post_id")
        return str(post_id) if post_id else None
    if _publishing_started(graph, video_id):
        return None
    raise error


# --------------------------------------------------------------------------- poll interpretation


def _phase_error(phase: dict[str, Any]) -> str | None:
    messages: list[str] = []
    err = phase.get("error")
    if isinstance(err, dict) and err.get("message"):
        messages.append(str(err["message"]))
    elif isinstance(err, str) and err:
        messages.append(err)
    errors = phase.get("errors")
    for item in errors if isinstance(errors, list) else []:
        text = item.get("message") if isinstance(item, dict) else item
        if text:
            messages.append(str(text))
    if messages:
        return "; ".join(messages)[:300]
    if _lower(phase.get("status")) == "error":
        return "no reason given"
    return None


def _status_summary(status: dict[str, Any]) -> str:
    parts = [f"video {_lower(status.get('video_status')) or 'unknown'}"]
    for name, label in (
        ("uploading_phase", "upload"),
        ("processing_phase", "processing"),
        ("publishing_phase", "publishing"),
    ):
        value = _lower(_phase(status, name).get("status"))
        if value:
            parts.append(f"{label} {value}")
    return ", ".join(parts)


def _check_failures(status: dict[str, Any]) -> None:
    video_status = _lower(status.get("video_status"))
    upload_error = _phase_error(_phase(status, "uploading_phase"))
    if video_status == "expired":
        raise RestartPublish(
            "The Facebook upload expired before it was published; Repeater will upload the video again.",
            code="video_expired",
        )
    if video_status == "upload_failed":
        detail = f": {upload_error}" if upload_error else "."
        raise RestartPublish(
            f"The upload to Facebook failed{detail} Repeater will upload the video again.",
            code="upload_failed",
        )
    for name, verb in (("processing_phase", "process"), ("publishing_phase", "publish")):
        detail = _phase_error(_phase(status, name))
        if detail:
            raise PermanentError(
                f"Facebook could not {verb} the reel: {detail}", code=f"{name.split('_')[0]}_error"
            )
    if video_status == "error":
        detail = f": {upload_error}" if upload_error else " (no reason given)."
        raise PermanentError(f"Facebook could not process or publish the reel{detail}", code="video_error")


def _is_published(status: dict[str, Any]) -> bool:
    publishing = _phase(status, "publishing_phase")
    publish_status = _lower(publishing.get("publish_status"))
    if publish_status == "published":
        return True
    if _lower(publishing.get("status")) in _PUBLISHED_PHASE:
        if publish_status == "draft":
            raise PermanentError(
                "Facebook saved the reel as a draft instead of publishing it; publish it from Meta Business "
                "Suite.",
                code="saved_as_draft",
            )
        return publish_status in ("", "published")
    return False


def _permalink(graph: meta.Graph, video_id: str) -> str | None:
    try:
        body = graph.get(
            meta.node(video_id), {"fields": "permalink_url"}, action="reading the Facebook reel link"
        )
    except PlatformError as exc:
        # The reel is live; a missing link must not turn a successful publish into a failure.
        log.warning(
            "Facebook permalink lookup failed for video %s: %s", video_id, exc.code or type(exc).__name__
        )
        return None
    return meta.absolute_facebook_url(body.get("permalink_url"))


# --------------------------------------------------------------------------- publisher


class FacebookPublisher:
    platform = Platform.FACEBOOK
    limits = LIMITS[Platform.FACEBOOK]

    def validate(self, account: AccountContext, req: PublishRequest) -> list[str]:
        problems = _size_problems(req)
        problems.extend(_picture_problems(req))
        if not (account.extra.get("page_id") or account.external_id):
            problems.append("This Facebook Page has no Page id stored; reconnect it.")
        return problems

    def publish(self, account: AccountContext, req: PublishRequest) -> PublishResult:
        problems = self.validate(account, req)
        if problems:
            raise PermanentError(" ".join(problems), code="invalid_video")
        page_id = _page_id(account)
        size = _file_size(req.video_path)
        data_settings = _settings(req.settings)
        token = account.tokens.access_token
        with meta.Graph(token) as graph:
            video_id = _start(graph, page_id)
            log.info("Facebook reel upload started: post=%s video=%s bytes=%s", req.post_id, video_id, size)
            _upload(graph, video_id, req.video_path, size, token)
            post_id = _finish(graph, page_id, video_id, _finish_params(video_id, req, data_settings))
        state: dict[str, Any] = {"video_id": video_id, "page_id": page_id}
        if post_id:
            state["post_id"] = post_id
        return PublishResult(
            outcome=PublishOutcome.PROCESSING,
            platform_post_id=video_id,
            state=state,
            poll_after_seconds=POLL_SECONDS,
            message="Uploaded to Facebook; Facebook is processing the reel.",
        )

    def poll(self, account: AccountContext, state: dict[str, Any]) -> PublishResult:
        video_id = state.get("video_id")
        if not video_id:
            raise PermanentError(
                "The Facebook video id is missing; the reel cannot be tracked.", code="missing_video_id"
            )
        video_id = str(video_id)
        with meta.Graph(account.tokens.access_token) as graph:
            status = _read_status(graph, video_id)
            _check_failures(status)
            if _is_published(status):
                return PublishResult(
                    outcome=PublishOutcome.PUBLISHED,
                    platform_post_id=video_id,
                    permalink=_permalink(graph, video_id),
                    state=dict(state),
                    message="Published on Facebook.",
                )
        return PublishResult(
            outcome=PublishOutcome.PROCESSING,
            platform_post_id=video_id,
            state=dict(state),
            poll_after_seconds=POLL_SECONDS,
            message=f"Facebook is still processing the reel ({_status_summary(status)}).",
        )


# --------------------------------------------------------------------------- lister


def _remote_video(item: dict[str, Any], edge: str) -> RemoteVideo | None:
    if not item.get("id"):
        return None
    length = item.get("length")
    source = item.get("source") if isinstance(item.get("source"), str) else None
    return RemoteVideo(
        external_id=str(item["id"]),
        created_at=meta.parse_graph_time(item.get("created_time"))
        or meta.parse_graph_time(item.get("updated_time")),
        caption=str(item.get("description") or ""),
        permalink=meta.absolute_facebook_url(item.get("permalink_url")),
        duration_s=float(length)
        if isinstance(length, int | float) and not isinstance(length, bool)
        else None,
        download_url=source or None,
        extra={"edge": edge},
    )


def _max_download_bytes() -> int:
    return max(get_settings().max_upload_mb * 1024**2, LIMITS[Platform.FACEBOOK].max_size_bytes)


class FacebookLister:
    platform = Platform.FACEBOOK

    def list_recent(
        self,
        account: AccountContext,
        *,
        since: datetime | None = None,
        limit: int = 50,
        config: dict | None = None,
    ) -> list[RemoteVideo]:
        """Newest-first Page videos from the Reels and Videos edges, deduplicated by id, posted at
        or after `since`."""
        if limit <= 0:
            return []
        since = ensure_aware(since)
        page_id = _page_id(account)
        params: dict[str, Any] = {"fields": LIST_FIELDS, "limit": LIST_PAGE_SIZE}
        if since is not None:
            params["since"] = int(since.timestamp())
        found: dict[str, RemoteVideo] = {}
        with meta.Graph(account.tokens.access_token) as graph:
            for edge, label in LIST_EDGES:
                taken = 0
                items = graph.paginate(
                    f"{meta.node(page_id)}/{edge}",
                    params,
                    action=f"listing the Facebook Page {label}",
                    max_pages=MAX_LIST_PAGES,
                )
                for item in items:
                    video = _remote_video(item, edge)
                    if video is None:
                        continue
                    if since is not None and video.created_at is not None and video.created_at < since:
                        break
                    if video.external_id not in found:
                        found[video.external_id] = video
                    taken += 1
                    if taken >= limit:
                        break
        videos = sorted(
            found.values(),
            key=lambda v: v.created_at.timestamp() if v.created_at else float("-inf"),
            reverse=True,
        )
        return videos[:limit]

    def download(self, account: AccountContext, remote: RemoteVideo, dest: Path) -> Path:
        """Fetch a fresh `source` link for the video and stream the file to `dest`."""
        with meta.Graph(account.tokens.access_token) as graph:
            body = graph.get(
                meta.node(remote.external_id), {"fields": "source"}, action="getting the Facebook video file"
            )
        url = body.get("source")
        if not isinstance(url, str) or not url:
            raise PermanentError(
                "Facebook does not provide the file for this video (it may be restricted, for example for "
                "copyright, or not owned by the Page). Upload the original video file to Repeater instead.",
                code="source_unavailable",
            )
        if not url.startswith("https://"):
            raise PermanentError("Facebook returned an unexpected file link.", code="bad_source_url")
        return stream_download(url, dest, max_bytes=_max_download_bytes())


publisher = FacebookPublisher()
lister = FacebookLister()
