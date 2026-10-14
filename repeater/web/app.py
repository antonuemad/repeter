"""ASGI application factory: `uvicorn repeater.web.app:create_app --factory`."""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from repeater import __version__
from repeater.config import get_settings
from repeater.web import auth, csrf
from repeater.web.middleware import SECURITY_HEADERS, BodySizeLimitMiddleware, SecurityHeadersMiddleware
from repeater.web.routes import (
    access,
    accounts,
    activity,
    consent,
    library,
    meta,
    oauth,
    overview,
    public,
    rules,
    schedule,
    sources,
)
from repeater.web.templating import STATIC_DIR, render

log = logging.getLogger("repeater.web")

FORM_BODY_LIMIT = 2 * 1024 * 1024
# Error pages: (title key, message key) per status code.
ERROR_MESSAGES = {
    400: ("error.400_title", "error.400_body"),
    403: ("error.403_title", "error.403_body"),
    404: ("error.404_title", "error.404_body"),
    405: ("error.405_title", "error.405_body"),
    410: ("error.410_title", "error.410_body"),
    413: ("error.413_title", "error.413_body"),
    429: ("error.429_title", "error.429_body"),
    500: ("error.500_title", "error.500_body"),
}
SPECIAL_403 = {"no_workspace": "error.no_workspace", "owner_only": "error.owner_only"}


def create_app() -> FastAPI:
    _configure_logging()
    app = FastAPI(
        title="Repeater",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(csrf.csrf_protect)],
    )
    app.state.login_limiter = auth.LoginRateLimiter()
    for router in (
        public.router,
        access.router,
        meta.router,
        auth.router,
        consent.router,
        oauth.router,
        overview.router,
        library.router,
        schedule.router,
        accounts.router,
        sources.router,
        rules.router,
        activity.router,
    ):
        app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    _add_exception_handlers(app)
    _add_middleware(app)
    return app


def _configure_logging() -> None:
    """uvicorn configures only its own loggers, so INFO records of repeater.* (signed media access,
    Meta callbacks) would be dropped. Use the worker's JSON stdout logging, which also keeps httpx
    request URLs (which can carry credentials) out of the logs. Left alone when the host already
    configured logging (tests, a custom uvicorn --log-config)."""
    if logging.getLogger().handlers:
        return
    from repeater.jobs.worker import configure_logging

    configure_logging(os.environ.get("REPEATER_LOG_LEVEL", "INFO").upper())


def _add_middleware(app: FastAPI) -> None:
    """Starlette wraps in reverse order of registration: the last one added runs first."""
    settings = get_settings()
    # The streamed upload gets its own cap (file limit + two multipart overheads), never "unlimited":
    # the body limit runs before authentication.
    upload_cap = library.upload_limit_bytes() + 2 * library.MULTIPART_OVERHEAD
    app.add_middleware(
        BodySizeLimitMiddleware, max_bytes=FORM_BODY_LIMIT, path_limits={library.UPLOAD_PATH: upload_cap}
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=settings.session_cookie_name,
        max_age=settings.session_max_age_seconds,
        same_site="lax",
        https_only=settings.is_production,
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    if settings.trusted_hosts and "*" not in settings.trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts))
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)


def _wants_html(request: Request) -> bool:
    path = request.url.path
    if (
        path.startswith(("/m/", "/static/"))
        or path in csrf.EXEMPT_PATHS
        or path in ("/healthz", "/robots.txt")
    ):
        return False
    return "session" in request.scope


def error_page(
    request: Request, status: int, message_key: str | None = None, detail: str | None = None
) -> Response:
    if not _wants_html(request):
        if request.url.path in csrf.EXEMPT_PATHS:
            return JSONResponse({"error": detail or "request failed"}, status_code=status)
        return PlainTextResponse(str(status), status_code=status)
    title_key, body_key = ERROR_MESSAGES.get(
        status, ERROR_MESSAGES[400] if status < 500 else ERROR_MESSAGES[500]
    )
    context = {"status": status, "title_key": title_key, "body_key": message_key or body_key, "nav": ""}
    try:
        return render(request, "error.html", context, status_code=status)
    except Exception:  # noqa: BLE001 - never fail while reporting a failure
        log.exception("error page rendering failed")
        return PlainTextResponse(str(status), status_code=status)


def _add_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(auth.LoginRequired)
    async def _login_required(request: Request, exc: auth.LoginRequired) -> Response:
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        next_url = target if request.method == "GET" else "/"
        return RedirectResponse(f"/login?next={quote(next_url, safe='')}", status_code=303)

    @app.exception_handler(auth.ConsentRequired)
    async def _consent_required(request: Request, exc: auth.ConsentRequired) -> Response:
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        next_url = target if request.method == "GET" else "/"
        return RedirectResponse(f"/consent?next={quote(next_url, safe='')}", status_code=303)

    @app.exception_handler(csrf.CSRFError)
    async def _csrf_failed(request: Request, exc: csrf.CSRFError) -> Response:
        return error_page(request, 403, "error.csrf")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        special = SPECIAL_403.get(str(exc.detail)) if exc.status_code == 403 else None
        response = error_page(request, exc.status_code, special, detail=str(exc.detail))
        for name, value in (exc.headers or {}).items():
            response.headers[name] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(request: Request, exc: RequestValidationError) -> Response:
        # Bad path parameters (e.g. /library/abc) are pages that do not exist.
        in_path = any(err.get("loc", ("",))[0] == "path" for err in exc.errors())
        return error_page(request, 404 if in_path else 400)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> Response:
        log.error(
            "unhandled error on %s %s: %s", request.method, request.url.path, type(exc).__name__, exc_info=exc
        )
        response = error_page(request, 500)
        # This handler runs outside the middleware stack, so add the security headers here.
        for name, value in SECURITY_HEADERS:
            response.headers.setdefault(name, value)
        return response
