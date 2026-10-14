"""Pure ASGI middleware: security headers on every response and request body size limits."""

from __future__ import annotations

from collections.abc import Mapping

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "img-src 'self' data: https:",
        "media-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        # OAuth providers are reached through redirects from GET links, never by form posts.
        "form-action 'self'",
        "object-src 'none'",
    )
)

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("X-Frame-Options", "DENY"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
)
HSTS = ("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


class SecurityHeadersMiddleware:
    """Adds the security headers to every HTTP response. HTML responses that did not choose a
    cache policy get `Cache-Control: no-store`, because pages show workspace data."""

    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.headers = SECURITY_HEADERS + ((HSTS,) if hsts else ())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers:
                    if name not in headers:
                        headers[name] = value
                content_type = headers.get("content-type", "")
                if content_type.startswith("text/html") and "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestTooLarge(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=413, detail="request body too large")


class BodySizeLimitMiddleware:
    """Rejects request bodies above a limit, by Content-Length up front and by counting the
    streamed bytes otherwise (chunked uploads). `path_limits` give routes that stream a larger
    body themselves (the dashboard video upload) their own, still finite, cap: no path is ever
    unlimited, because this runs before authentication."""

    def __init__(self, app: ASGIApp, *, max_bytes: int, path_limits: Mapping[str, int] | None = None) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.path_limits = dict(path_limits or {})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.path_limits.get(scope.get("path", ""), self.max_bytes)
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            response = PlainTextResponse("Request body too large.", status_code=413)
            await response(scope, receive, send)
            return
        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise RequestTooLarge()
            return message

        await self.app(scope, counting_receive, send)
