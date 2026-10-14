"""Out-of-band notifications (Telegram). OWNER: engine module.

Uses the Bot API method sendMessage (https://core.telegram.org/bots/api#sendmessage) with
parse_mode HTML. In HTML mode every <, > and & that is not markup must be sent as an entity,
and the text may be at most 4096 characters after entity parsing.

Privacy: every workspace's alerts go to one operator chat, and the callers' texts quote video
titles, captions, channel and account names and platform errors. Those are the users' data (and
partly Google/Meta API data), so by default only a minimal message leaves the server: the kind of
event, a count, the workspace id and a link to the dashboard. The full text is sent only when the
operator opted in with REPEATER_TELEGRAM_INCLUDE_DETAILS=true (details_enabled).
"""

from __future__ import annotations

import html
import logging
import re

import httpx

from repeater.config import get_settings
from repeater.messages import Message, diagnostic, render

log = logging.getLogger("repeater.notify")

TELEGRAM_API = "https://api.telegram.org"
TIMEOUT_SECONDS = 10.0
# Telegram's limit is 4096 characters after entity parsing; keep room for the header.
MAX_BODY_CHARS = 3800

_LEVEL_LABELS = {"info": "Info", "warning": "Warning", "error": "Action needed"}
# category -> (singular, plural, dashboard path). The sentence never names a video or an account.
_CATEGORIES: dict[str, tuple[str, str, str]] = {
    "post_needs_attention": ("{n} post needs attention", "{n} posts need attention", "/schedule"),
    "uploads_interrupted": (
        "{n} upload was interrupted and may be live",
        "{n} uploads were interrupted and may be live",
        "/schedule",
    ),
    "account_reconnect": ("{n} account must be reconnected", "{n} accounts must be reconnected", "/accounts"),
    "source_failing": ("{n} source is failing", "{n} sources are failing", "/sources"),
    "disk_low": (
        "The server's disk is nearly full, so imports are waiting",
        "The server's disk is nearly full, so imports are waiting",
        "/library",
    ),
    "alert": ("Something needs your attention", "Something needs your attention", "/"),
}
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


def details_enabled() -> bool:
    """True when the operator opted in to alerts that quote titles, captions and account names
    (settings.telegram_include_details, env REPEATER_TELEGRAM_INCLUDE_DETAILS)."""
    return bool(get_settings().telegram_include_details)


def notify(
    text: str,
    *,
    workspace_id: int | None = None,
    level: str = "info",
    category: str = "alert",
    count: int = 1,
) -> bool:
    """Send a short message to the configured Telegram chat. Never raises; returns success.

    `text` is the full human message; it is only sent when details_enabled(). Otherwise a minimal
    message is sent, built from `category` (one of post_needs_attention, uploads_interrupted,
    account_reconnect, source_failing, disk_low; anything else is a generic alert) and `count`."""
    try:
        settings = get_settings()
        token, chat_id = settings.telegram_bot_token, settings.telegram_chat_id
        if not token or not chat_id:
            return False
        lang = _workspace_lang(workspace_id)
        if details_enabled():
            message = format_message(
                text, workspace_id=workspace_id, level=level, category=category, lang=lang
            )
        else:
            message = format_minimal(
                workspace_id=workspace_id, level=level, category=category, count=count, lang=lang
            )
        return _send(token, chat_id, message)
    except Exception as exc:  # noqa: BLE001 - notifications must never break the caller
        log.warning("Telegram notification failed (%s)", type(exc).__name__)
        return False


def _workspace_lang(workspace_id: int | None) -> str:
    # Alerts without a workspace belong to the operator; keep their established English default.
    if workspace_id is not None:
        from repeater.db import get_sessionmaker
        from repeater.models import Workspace

        with get_sessionmaker()() as session:
            workspace = session.get(Workspace, workspace_id)
            if workspace is not None and workspace.locale in ("ar", "en"):
                return workspace.locale
    return "en"


def _header(workspace_id: int | None, level: str, lang: str = "en") -> str:
    label = _LEVEL_LABELS.get(level, level.capitalize() or "Info")
    if lang == "ar":
        label = {"info": "معلومة", "warning": "تنبيه", "error": "يحتاج إلى تدخل"}.get(level, "تنبيه")
    header = f"<b>Repeater · {html.escape(label, quote=False)}</b>"
    if workspace_id is not None:
        label = "مساحة العمل" if lang == "ar" else "workspace"
        header += f" ({label} {int(workspace_id)})"
    return header


def dashboard_link(category: str = "alert") -> str:
    path = _CATEGORIES.get(category, _CATEGORIES["alert"])[2]
    return f"{get_settings().base_url.rstrip('/')}{path}"


def format_minimal(
    *,
    workspace_id: int | None = None,
    level: str = "info",
    category: str = "alert",
    count: int = 1,
    lang: str = "en",
) -> str:
    """The default alert: what kind of event, how many, and where to look. No names or titles."""
    singular, plural, _ = _CATEGORIES.get(category, _CATEGORIES["alert"])
    n = max(int(count), 1)
    sentence = (singular if n == 1 else plural).format(n=n)
    if lang == "ar":
        sentence = render(
            Message("notify." + (category if category in _CATEGORIES else "alert"), sentence, {"n": n}).info,
            lang,
        )
    link = html.escape(dashboard_link(category), quote=False)
    action = "افتح لوحة التحكم" if lang == "ar" else "Open the dashboard"
    return f"{_header(workspace_id, level, lang)}\n{html.escape(sentence, quote=False)}. {action}: {link}"


def format_message(
    text: str,
    *,
    workspace_id: int | None = None,
    level: str = "info",
    category: str | None = None,
    lang: str = "en",
) -> str:
    """The full alert quoting `text` (only sent when the operator opted in)."""
    body = render(diagnostic(text or "").info, lang).strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[: MAX_BODY_CHARS - 1].rstrip() + "…"
    message = f"{_header(workspace_id, level, lang)}\n{html.escape(body, quote=False)}"
    if category is not None:
        message += f"\n{html.escape(dashboard_link(category), quote=False)}"
    return message


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
