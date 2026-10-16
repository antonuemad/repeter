"""Public endpoints: health check, robots.txt, signed media URLs, legal pages, language switch."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import text

from repeater.config import get_settings
from repeater.crypto import verify_media_signature
from repeater.db import get_engine
from repeater.media import storage
from repeater.models import Video
from repeater.web import consent, i18n
from repeater.web.auth import session_user
from repeater.web.deps import DB, Form, safe_next
from repeater.web.templating import STATIC_DIR, render

log = logging.getLogger("repeater.web.media")
router = APIRouter()

ROBOTS_TXT = """\
# Signed video links are fetched by Facebook (facebookexternalhit) when publishing Reels.
User-agent: facebookexternalhit
Allow: /m/
Allow: /privacy
Allow: /terms
Disallow: /

User-agent: *
Allow: /privacy
Allow: /terms
Disallow: /
"""
MEDIA_HEADERS = {
    "Cache-Control": "private, max-age=3600",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
}
# base64url(HMAC-SHA256) without padding is 43 characters; anything else is not a signature.
_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def db_ping() -> None:
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))


@router.get("/healthz", include_in_schema=False)
def healthz() -> JSONResponse:
    try:
        db_ping()
    except Exception as exc:  # noqa: BLE001 - any database failure means "not healthy"
        log.warning("health check failed: %s", type(exc).__name__)
        return JSONResponse({"ok": False}, status_code=503)
    return JSONResponse({"ok": True})


@router.get("/robots.txt", include_in_schema=False)
def robots() -> PlainTextResponse:
    return PlainTextResponse(ROBOTS_TXT, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@router.api_route(
    "/m/{video_id}/{expires}/{sig}.{extension}", methods=["GET", "HEAD"], include_in_schema=False
)
def media(request: Request, db: DB, video_id: str, expires: str, sig: str, extension: str) -> Response:
    """Signed, expiring link to an encoded video, fetched by the platforms (PLAN.md 5.5, 7.4).
    No session: the HMAC signature over (video id, expiry) is the authorization."""
    if (
        not (video_id.isascii() and video_id.isdigit() and expires.isascii() and expires.isdigit())
        or len(video_id) > 18
        or len(expires) > 12
        or not _SIGNATURE_RE.match(sig)
    ):
        return PlainTextResponse("Forbidden", status_code=403, headers=MEDIA_HEADERS)
    vid, exp = int(video_id), int(expires)
    verdict = verify_media_signature(vid, exp, sig)
    if verdict == "invalid":
        return PlainTextResponse("Forbidden", status_code=403, headers=MEDIA_HEADERS)
    if verdict == "expired":
        return PlainTextResponse("This link has expired.", status_code=410, headers=MEDIA_HEADERS)
    video = db.get(Video, vid)
    if video is None or extension != ("jpg" if video.media_kind == "image" else "mp4"):
        return PlainTextResponse("Not found", status_code=404, headers=MEDIA_HEADERS)
    path = _encoded_file(video)
    if path is None:
        return PlainTextResponse("Not found", status_code=404, headers=MEDIA_HEADERS)
    # Access log for leak detection (PLAN.md 5.5); the signature itself is never logged.
    log.info(
        "media %s video=%s range=%s ip=%s ua=%s",
        request.method,
        vid,
        request.headers.get("range", "-"),
        request.client.host if request.client else "-",
        request.headers.get("user-agent", "-")[:120],
    )
    return FileResponse(
        path, media_type="image/jpeg" if video.media_kind == "image" else "video/mp4", headers=MEDIA_HEADERS
    )


def _encoded_file(video: Video | None) -> Path | None:
    if (
        video is None
        or not video.encoded_path
        or video.media_kind not in ("video", "image")
        or (
            (video.profile_id is not None or video.media_kind != "video" or video.content_format != "reel")
            and not get_settings().publishing_enabled
        )
    ):
        return None
    try:
        path = storage.abs_path(video.encoded_path)
    except ValueError:
        return None
    return path if path is not None and path.is_file() else None


@router.get("/privacy")
def privacy(request: Request) -> Response:
    return i18n.remember_requested(request, render(request, "privacy.html", _legal_context()))


@router.get("/terms")
def terms(request: Request) -> Response:
    return i18n.remember_requested(request, render(request, "terms.html", _legal_context()))


def _legal_context() -> dict[str, object]:
    settings = get_settings()
    return {
        "nav": "legal",
        "base_url": settings.base_url.rstrip("/"),
        "original_retention_days": settings.original_retention_days,
        "platform_retention_days": settings.platform_data_retention_days,
        "contact_email": "info@magic-lap.com",
        "effective_date": consent.LEGAL_VERSION,
    }


@router.post("/theme")
def switch_theme(form: Form) -> Response:
    raw_next = form.get("next")
    response = RedirectResponse(safe_next(raw_next if isinstance(raw_next, str) else None), status_code=303)
    theme = form.get("theme")
    if theme in ("light", "dark"):
        response.set_cookie(
            "repeater_theme",
            theme,
            max_age=365 * 24 * 3600,
            httponly=True,
            samesite="lax",
            secure=get_settings().is_production,
        )
    return response


@router.post("/lang")
def switch_language(request: Request, db: DB, form: Form) -> Response:
    lang = i18n.normalize(form.get("lang") if isinstance(form.get("lang"), str) else None)
    raw_next = form.get("next")
    target = _without_lang_param(safe_next(raw_next if isinstance(raw_next, str) else None))
    response = RedirectResponse(target, status_code=303)
    if lang is None:
        return response
    i18n.set_cookie(response, lang)
    user = session_user(request, db)
    if user is not None and user.locale != lang:
        user.locale = lang
        db.commit()
    return response


def _without_lang_param(target: str) -> str:
    """Drop `lang` from the redirect target: a `?lang=en` in the URL would override the choice
    that was just made."""
    parts = urlsplit(target)
    if i18n.QUERY_PARAM not in parts.query:
        return target
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k != i18n.QUERY_PARAM])
    return parts.path + (f"?{query}" if query else "")
