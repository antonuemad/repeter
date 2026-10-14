"""Shared HTTP helpers for platform adapters (httpx, sync)."""

from __future__ import annotations

import os
from pathlib import Path

import httpx

from repeater import __version__
from repeater.platforms.base import AuthError, PermanentError, PlatformError, RateLimitedError, RetryableError

USER_AGENT = f"Repeater/{__version__} (+https://magic-lap.com)"
DEFAULT_TIMEOUT = httpx.Timeout(30.0, read=300.0, write=300.0)


def client(**kw) -> httpx.Client:
    headers = {"User-Agent": USER_AGENT, **kw.pop("headers", {})}
    return httpx.Client(timeout=kw.pop("timeout", DEFAULT_TIMEOUT), headers=headers, **kw)


def retry_after_seconds(resp: httpx.Response, default: float = 60.0) -> float:
    try:
        return float(resp.headers.get("retry-after", default))
    except ValueError:
        return default


def classify_http_error(resp: httpx.Response, message: str, *, code: str | None = None) -> PlatformError:
    """Generic mapping from an HTTP failure to our error classes. Adapters refine this with
    platform error codes before falling back to it."""
    s = resp.status_code
    if s == 401:
        return AuthError(message, code=code, status=s)
    if s == 429:
        return RateLimitedError(message, code=code, status=s, retry_after=retry_after_seconds(resp))
    if s >= 500:
        return RetryableError(message, code=code, status=s, retry_after=retry_after_seconds(resp, 120))
    return PermanentError(message, code=code, status=s)


def stream_download(
    url: str,
    dest: Path,
    *,
    headers: dict[str, str] | None = None,
    max_bytes: int = 4 * 1024**3,
    http: httpx.Client | None = None,
) -> Path:
    """Download `url` to `dest` atomically (via a .part file). Raises RetryableError on
    network/5xx errors and PermanentError on 4xx or oversize files."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    own = http is None
    c = http or client(follow_redirects=True)
    try:
        with c.stream("GET", url, headers=headers or {}) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise classify_http_error(resp, f"download failed with HTTP {resp.status_code}")
            total = 0
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise PermanentError(f"file larger than {max_bytes} bytes")
                    fh.write(chunk)
        os.replace(tmp, dest)
        return dest
    except httpx.HTTPError as exc:
        raise RetryableError(f"download network error: {type(exc).__name__}") from exc
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        if own:
            c.close()
