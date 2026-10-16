"""CSRF protection: one random token per session, required on every unsafe request either as the
form field `csrf_token` or the `X-CSRF-Token` header, compared in constant time.

Only the Meta callbacks are exempt: Meta calls them server-to-server without a session and
they are authenticated by the HMAC-signed `signed_request` instead. The streaming video upload
checks the token itself, before any file byte is written (see routes/library.py)."""

from __future__ import annotations

import hmac
import secrets
from collections.abc import MutableMapping
from typing import Any

from fastapi import Request

SESSION_KEY = "csrf"
FORM_FIELD = "csrf_token"
HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
EXEMPT_PATHS = frozenset(
    {"/meta/deauthorize", "/meta/data-deletion", "/instagram/deauthorize", "/instagram/data-deletion"}
)
# Routes that parse their multipart body as a stream and verify the token from it.
STREAMED_PATHS = frozenset({"/library/upload"})


class CSRFError(Exception):
    """The request did not carry the session's CSRF token."""


def ensure_token(session: MutableMapping[str, Any]) -> str:
    token = session.get(SESSION_KEY)
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session[SESSION_KEY] = token
    return token


def rotate_token(session: MutableMapping[str, Any]) -> str:
    session.pop(SESSION_KEY, None)
    return ensure_token(session)


def token_matches(session: MutableMapping[str, Any], candidate: object) -> bool:
    expected = session.get(SESSION_KEY)
    if not isinstance(expected, str) or not expected or not isinstance(candidate, str) or not candidate:
        return False
    return hmac.compare_digest(expected.encode(), candidate.encode())


def is_streamed_upload(request: Request) -> bool:
    content_type = request.headers.get("content-type", "")
    return request.url.path in STREAMED_PATHS and content_type.startswith("multipart/form-data")


async def csrf_protect(request: Request) -> None:
    """App-wide dependency. Raises CSRFError for unsafe requests without a valid token.

    This runs before any route-level authentication, so it must never buffer a request body of
    unbounded size: on a streamed path the body is only ever read by the route itself (as a
    stream, after authentication). Any other content type on such a path is refused here without
    reading a byte; the header token is still accepted."""
    if request.method in SAFE_METHODS or request.url.path in EXEMPT_PATHS:
        return
    if token_matches(request.session, request.headers.get(HEADER)):
        return
    if is_streamed_upload(request):
        request.state.csrf_deferred = True
        return
    if request.url.path in STREAMED_PATHS:
        raise CSRFError()
    content_type = request.headers.get("content-type", "")
    if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        # Only the streamed upload accepts files; anywhere else a file part is rejected (400).
        form = await request.form(max_files=0)
        if token_matches(request.session, form.get(FORM_FIELD)):
            return
    raise CSRFError()
