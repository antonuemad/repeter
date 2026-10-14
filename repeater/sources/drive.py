"""Google Drive folder as a video source (Drive API v3, official).

Source config: {"folder_id": "<id or full folder URL>"}. The account is a ConnectedAccount with
platform gdrive whose Google token carries a scope that can read file content.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx

from repeater.models import Platform
from repeater.platforms import http
from repeater.platforms.base import (
    AccountContext,
    AuthError,
    PermanentError,
    PlatformError,
    RateLimitedError,
    RemoteVideo,
    RetryableError,
)
from repeater.timeutil import ensure_aware

API_BASE = "https://www.googleapis.com/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
LIST_FIELDS = "nextPageToken,files(id,name,mimeType,createdTime,size,description,webViewLink)"
FOLDER_FIELDS = "id,name,mimeType,trashed"
PAGE_SIZE_MAX = 100
MAX_PAGES = 10
API_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_DOWNLOAD_BYTES = 4 * 1024**3

# Drive error reasons (error.errors[].reason) that mean "slow down".
_RATE_LIMIT_REASONS = frozenset({"userRateLimitExceeded", "rateLimitExceeded", "dailyLimitExceeded"})
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_FOLDER_PATH_RE = re.compile(r"/folders/([A-Za-z0-9_-]+)")
_VIDEO_EXTENSIONS = frozenset({"mp4", "mov", "m4v", "webm", "mkv", "avi", "3gp", "mpg", "mpeg", "ts", "wmv"})

RECONNECT_HINT = "Reconnect Google Drive in Accounts."


def parse_folder_id(value: str | None) -> str:
    """Accept a bare folder id or a Drive folder link (…/folders/<id>, …?id=<id>)."""
    raw = (value or "").strip()
    if not raw:
        raise PermanentError("No Google Drive folder is set for this source. Paste the folder link.")
    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if host != "google.com" and not host.endswith(".google.com"):
            raise PermanentError("The Google Drive folder link is not a drive.google.com link.")
        m = _FOLDER_PATH_RE.search(parsed.path)
        candidate = m.group(1) if m else (parse_qs(parsed.query).get("id") or [""])[0]
    else:
        candidate = raw
    if not _ID_RE.match(candidate):
        raise PermanentError("The Google Drive folder link is not valid. Copy it again from Drive.")
    return candidate


def file_download_url(file_id: str) -> str:
    return f"{API_BASE}/files/{quote(file_id, safe='')}?alt=media&supportsAllDrives=true"


class DriveLister:
    platform = Platform.GDRIVE

    def list_recent(
        self,
        account: AccountContext,
        *,
        since: datetime | None = None,
        limit: int = 50,
        config: dict | None = None,
    ) -> list[RemoteVideo]:
        """Newest-first video files directly inside the configured folder."""
        if limit <= 0:
            return []
        folder_id = parse_folder_id((config or {}).get("folder_id"))
        since = ensure_aware(since)
        headers = _auth(account)
        with http.client(timeout=API_TIMEOUT) as c:
            files = _list_folder(c, headers, folder_id, limit, since)
            if not files:
                _check_folder(c, headers, folder_id)
        return [rv for rv in (_remote_video(f) for f in files) if rv is not None][:limit]

    def download(self, account: AccountContext, remote: RemoteVideo, dest: Path) -> Path:
        url = remote.download_url or file_download_url(remote.external_id)
        if not url.startswith(f"{API_BASE}/files/"):
            raise PermanentError("Refusing to download a Google Drive file from an unexpected address.")
        try:
            return http.stream_download(url, dest, headers=_auth(account), max_bytes=MAX_DOWNLOAD_BYTES)
        except AuthError as exc:
            raise AuthError(
                f"Google Drive refused the download. {RECONNECT_HINT}", status=exc.status
            ) from None
        except PermanentError as exc:
            raise _download_error(exc) from None


def _auth(account: AccountContext) -> dict[str, str]:
    return {"Authorization": f"Bearer {account.tokens.access_token}"}


def _list_folder(
    c: httpx.Client, headers: dict[str, str], folder_id: str, limit: int, since: datetime | None
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "q": f"'{folder_id}' in parents and trashed = false and mimeType contains 'video/'",
        "orderBy": "createdTime desc",
        "fields": LIST_FIELDS,
        "pageSize": min(limit, PAGE_SIZE_MAX),
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    out: list[dict[str, Any]] = []
    for _ in range(MAX_PAGES):
        data = _get_json(c, f"{API_BASE}/files", params, headers, "folder listing")
        for item in data.get("files") or []:
            created = _parse_time(item.get("createdTime"))
            if since and created and created < since:
                return out  # ordered newest first: everything after this is older
            out.append(item)
            if len(out) >= limit:
                return out
        token = data.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return out


def _check_folder(c: httpx.Client, headers: dict[str, str], folder_id: str) -> None:
    """An empty listing is ambiguous; make sure the folder itself is still reachable."""
    params = {"fields": FOLDER_FIELDS, "supportsAllDrives": "true"}
    meta = _get_json(c, f"{API_BASE}/files/{quote(folder_id, safe='')}", params, headers, "folder lookup")
    if meta.get("mimeType") != FOLDER_MIME:
        raise PermanentError("The Google Drive link points to a file, not a folder.", code="notAFolder")
    if meta.get("trashed"):
        raise PermanentError("The Google Drive folder is in the trash.", code="folderTrashed")


def _get_json(
    c: httpx.Client, url: str, params: dict[str, Any], headers: dict[str, str], action: str
) -> dict[str, Any]:
    try:
        resp = c.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise RetryableError(f"Google Drive {action} failed: network error ({type(exc).__name__})") from None
    if resp.status_code >= 400:
        raise drive_api_error(resp, action)
    try:
        data = resp.json()
    except ValueError:
        raise RetryableError(f"Google Drive {action} returned an unreadable response") from None
    return data if isinstance(data, dict) else {}


def drive_api_error(resp: httpx.Response, action: str) -> PlatformError:
    """Map a Drive API error response (see "Resolve errors" in the Drive docs) to our errors."""
    reason, detail = _error_details(resp)
    status = resp.status_code
    suffix = f": {detail}" if detail else ""
    if status == 401:
        return AuthError(
            f"Google Drive access expired or was revoked. {RECONNECT_HINT}", code=reason, status=status
        )
    if status == 404:
        return PermanentError(
            "The Google Drive folder was not found, or the connected Google account cannot open it.",
            code=reason or "notFound",
            status=status,
        )
    if status == 429 or (status == 403 and reason in _RATE_LIMIT_REASONS):
        return RateLimitedError(
            f"Google Drive rate limit reached during {action}",
            code=reason,
            status=status,
            retry_after=http.retry_after_seconds(resp, 300),
        )
    if status == 403:
        return PermanentError(f"Google Drive denied the {action}{suffix}", code=reason, status=status)
    return http.classify_http_error(
        resp, f"Google Drive {action} failed with HTTP {status}{suffix}", code=reason
    )


def _error_details(resp: httpx.Response) -> tuple[str | None, str]:
    try:
        err = resp.json().get("error") or {}
    except (ValueError, AttributeError):
        return None, ""
    if not isinstance(err, dict):
        return None, ""
    reasons = [e.get("reason") for e in err.get("errors") or [] if isinstance(e, dict) and e.get("reason")]
    message = str(err.get("message") or "").strip()[:300]
    return (reasons[0] if reasons else None), message


def _download_error(exc: PermanentError) -> PlatformError:
    if exc.status == 404:
        return PermanentError(
            "The file is no longer in Google Drive, or the connected account cannot read it.", status=404
        )
    if exc.status == 403:
        # Drive uses 403 both for rate limits and for files whose owner blocked downloads; the
        # streaming helper does not expose the reason, so retry a bounded number of times.
        return RetryableError(
            "Google Drive refused the download (rate limit, or downloads are disabled for this file).",
            status=403,
            retry_after=600,
        )
    return exc


def _remote_video(item: dict[str, Any]) -> RemoteVideo | None:
    file_id = item.get("id")
    if not file_id or not str(item.get("mimeType") or "").startswith("video/"):
        return None
    name = str(item.get("name") or "")
    stem = Path(name).stem if name else ""
    description = str(item.get("description") or "").strip()
    ext = Path(name).suffix.lower().lstrip(".")
    extra: dict[str, Any] = {"name": name, "mime_type": item.get("mimeType")}
    if ext in _VIDEO_EXTENSIONS:
        extra["ext"] = ext
    size = item.get("size")
    if size is not None and str(size).isdigit():
        extra["size"] = int(size)
    return RemoteVideo(
        external_id=str(file_id),
        created_at=_parse_time(item.get("createdTime")),
        title=stem,
        caption=description or stem,
        permalink=item.get("webViewLink") or None,
        download_url=file_download_url(str(file_id)),
        extra=extra,
    )


def _parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return ensure_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


lister = DriveLister()
