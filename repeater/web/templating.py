"""Jinja2 environment, template helpers and the page renderer."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import jinja2
from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse

from repeater.engine.captions import platform_name
from repeater.timeutil import ensure_aware, tz
from repeater.web import csrf, i18n

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
FLASH_KEY = "_flash"
MAX_FLASHES = 3
MAX_FLASH_CHARS = 300
# Flashes travel in the signed session cookie; browsers drop cookies over 4 KB, which would
# sign the user out. JSON escapes non-ASCII text, so the budget is measured on the JSON.
MAX_FLASH_JSON_BYTES = 1500


def fmt_dt(value: datetime | None, zone: ZoneInfo | str | None = None, style: str = "datetime") -> str:
    """Format an aware datetime in the workspace timezone (24-hour clock)."""
    if value is None:
        return "—"
    zone = zone if isinstance(zone, ZoneInfo) else tz(zone)
    local = ensure_aware(value).astimezone(zone)
    if style == "time":
        return local.strftime("%H:%M")
    if style == "date":
        return local.strftime("%Y-%m-%d")
    return local.strftime("%Y-%m-%d %H:%M")


def fmt_bytes(value: int | float | None) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    total = round(float(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _build_env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(("html", "xml"), default_for_string=True),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(
        fmt_dt=fmt_dt, fmt_bytes=fmt_bytes, fmt_duration=fmt_duration, platform_name=platform_name
    )
    return env


templates = Jinja2Templates(env=_build_env())


# --------------------------------------------------------------------------- flash messages


def flash(
    request: Request, level: str, key: str | None = None, *, text: str | None = None, **params: Any
) -> None:
    """Queue a message for the next rendered page. `key` is an i18n key; `text` is a message
    that is already human readable (e.g. a platform error), shown as is."""
    items = list(request.session.get(FLASH_KEY) or [])[-(MAX_FLASHES - 1) :]
    entry: dict[str, Any] = {"level": level}
    if key:
        entry["key"] = key
        entry["params"] = {k: str(v)[:MAX_FLASH_CHARS] for k, v in params.items()}
    if text:
        entry["text"] = text[:MAX_FLASH_CHARS]
    items.append(entry)
    while len(items) > 1 and len(json.dumps(items)) > MAX_FLASH_JSON_BYTES:
        items.pop(0)
    request.session[FLASH_KEY] = items


def pop_flashes(request: Request, lang: str) -> list[dict[str, str]]:
    items = request.session.pop(FLASH_KEY, None) or []
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        parts = []
        if item.get("key"):
            parts.append(i18n.message(lang, str(item["key"]), dict(item.get("params") or {})))
        if item.get("text"):
            parts.append(str(item["text"]))
        out.append({"level": str(item.get("level") or "info"), "message": " ".join(parts)})
    return out


# --------------------------------------------------------------------------- rendering


def base_context(request: Request) -> dict[str, Any]:
    lang = i18n.request_lang(request)
    user = getattr(request.state, "user", None)
    workspace = getattr(request.state, "workspace", None)
    return {
        "request": request,
        "lang": lang,
        "dir": i18n.direction(lang),
        "t": i18n.Translator(lang),
        "csrf_token": csrf.ensure_token(request.session),
        "csrf_field": csrf.FORM_FIELD,
        "user": user,
        "workspace": workspace,
        "role": getattr(request.state, "role", None),
        "zone": tz(workspace.timezone if workspace is not None else None),
        "flashes": pop_flashes(request, lang),
        "current_path": request.url.path,
        "nav": "",
    }


def render(
    request: Request, template: str, context: dict[str, Any] | None = None, *, status_code: int = 200
) -> HTMLResponse:
    ctx = base_context(request)
    ctx.update(context or {})
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)
