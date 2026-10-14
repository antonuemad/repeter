"""Out-of-band notifications (Telegram). OWNER: engine module.

Uses the Bot API method sendMessage (https://core.telegram.org/bots/api#sendmessage) with
parse_mode HTML. In HTML mode every <, > and & that is not markup must be sent as an entity,
and the text may be at most 4096 characters after entity parsing.
"""

from __future__ import annotations

import html
import logging
import re

import httpx

from repeater.config import get_settings

log = logging.getLogger("repeater.notify")

TELEGRAM_API = "https://api.telegram.org"
TIMEOUT_SECONDS = 10.0
# Telegram's limit is 4096 characters after entity parsing; keep room for the header.
MAX_BODY_CHARS = 3800

_LEVEL_LABELS = {"info": "Info", "warning": "Warning", "error": "Action needed"}
_BOT_TOKEN_RE = re.compile(r"/bot[^/\s]+/")


class _RedactBotToken(logging.Filter):
    """httpx logs every request URL at INFO level; Bot API URLs embed the bot token."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "/bot" in message:
            record.msg = _BOT_TOKEN_RE.sub("/bot<redacted>/", message)
            record.args = ()
        return True


logging.getLogger("httpx").addFilter(_RedactBotToken())


def notify(text: str, *, workspace_id: int | None = None, level: str = "info") -> bool:
    """Send a short message to the configured Telegram chat. Never raises; returns success."""
    try:
        settings = get_settings()
        token, chat_id = settings.telegram_bot_token, settings.telegram_chat_id
        if not token or not chat_id:
            return False
        return _send(token, chat_id, format_message(text, workspace_id=workspace_id, level=level))
    except Exception as exc:  # noqa: BLE001 - notifications must never break the caller
        log.warning("Telegram notification failed (%s)", type(exc).__name__)
        return False


def format_message(text: str, *, workspace_id: int | None = None, level: str = "info") -> str:
    label = _LEVEL_LABELS.get(level, level.capitalize() or "Info")
    header = f"<b>Repeater · {html.escape(label, quote=False)}</b>"
    if workspace_id is not None:
        header += f" (workspace {int(workspace_id)})"
    body = (text or "").strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[: MAX_BODY_CHARS - 1].rstrip() + "…"
    return f"{header}\n{html.escape(body, quote=False)}"


def _send(token: str, chat_id: str, message: str) -> bool:
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            resp = client.post(f"{TELEGRAM_API}/bot{token}/sendMessage", json=payload)
    except httpx.HTTPError as exc:
        # Never log str(exc): httpx includes the request URL, which contains the bot token.
        log.warning("Telegram notification failed (%s)", type(exc).__name__)
        return False
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code == 200 and isinstance(data, dict) and data.get("ok") is True:
        return True
    description = str(data.get("description") or "") if isinstance(data, dict) else ""
    log.warning(
        "Telegram rejected the notification (HTTP %s): %s",
        resp.status_code,
        description.replace(token, "<redacted>")[:200],
    )
    return False
