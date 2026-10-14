"""Public endpoints: health check, robots.txt, signed media URLs, legal pages, language switch."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import text

from repeater.config import get_settings
from repeater.crypto import verify_media_signature
from repeater.db import get_engine
from repeater.media import storage
from repeater.models import Video
from repeater.web import i18n
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
MEDIA_HEADERS = {"Cache-Control": "private, max-age=3600", "X-Robots-Tag": "noindex, nofollow"}


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


@router.api_route("/m/{video_id}/{expires}/{sig}.mp4", methods=["GET", "HEAD"], include_in_schema=False)
def media(request: Request, db: DB, video_id: str, expires: str, sig: str) -> Response:
    """Signed, expiring link to an encoded video, fetched by the platforms (PLAN.md 5.5, 7.4).
    No session: the HMAC signature over (video id, expiry) is the authorization."""
    if not (video_id.isdigit() and expires.isdigit()) or len(video_id) > 18 or len(expires) > 12:
        return PlainTextResponse("Forbidden", status_code=403, headers=MEDIA_HEADERS)
    vid, exp = int(video_id), int(expires)
    verdict = verify_media_signature(vid, exp, sig)
    if verdict == "invalid":
        return PlainTextResponse("Forbidden", status_code=403, headers=MEDIA_HEADERS)
    if verdict == "expired":
        return PlainTextResponse("This link has expired.", status_code=410, headers=MEDIA_HEADERS)
    video = db.get(Video, vid)
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
    return FileResponse(path, media_type="video/mp4", headers=MEDIA_HEADERS)


def _encoded_file(video: Video | None) -> Path | None:
    if video is None or not video.encoded_path:
        return None
    try:
        path = storage.abs_path(video.encoded_path)
    except ValueError:
        return None
    return path if path is not None and path.is_file() else None


@router.get("/privacy")
def privacy(request: Request) -> Response:
    return render(request, "privacy.html", _legal_context())


@router.get("/terms")
def terms(request: Request) -> Response:
    return render(request, "terms.html", _legal_context())


def _legal_context() -> dict[str, object]:
    settings = get_settings()
    return {
        "nav": "legal",
        "base_url": settings.base_url.rstrip("/"),
        "original_retention_days": settings.original_retention_days,
        "platform_retention_days": settings.platform_data_retention_days,
        "contact_email": "info@magic-lap.com",
        "effective_date": "2026-10-14",
    }


@router.post("/lang")
def switch_language(request: Request, db: DB, form: Form) -> Response:
    lang = i18n.normalize(form.get("lang") if isinstance(form.get("lang"), str) else None)
    raw_next = form.get("next")
    response = RedirectResponse(safe_next(raw_next if isinstance(raw_next, str) else None), status_code=303)
    if lang is None:
        return response
    i18n.set_cookie(response, lang)
    user = session_user(request, db)
    if user is not None and user.locale != lang:
        user.locale = lang
        db.commit()
    return response
