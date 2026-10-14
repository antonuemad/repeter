"""User-visible activity log."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from repeater.messages import Message
from repeater.models import ActivityLevel, ActivityLog

log = logging.getLogger("repeater.activity")


def log_activity(
    session: Session,
    workspace_id: int | None,
    level: ActivityLevel | str,
    category: str,
    message: str,
    *,
    actor: str = "system",
    **data,
) -> ActivityLog:
    """Record an event shown in the dashboard log. Never pass tokens or secrets in data."""
    if isinstance(message, Message):
        data.update(msg_key=message.key, msg_params=message.info["params"])
    entry = ActivityLog(
        workspace_id=workspace_id,
        level=ActivityLevel(level),
        category=category,
        message=message[:4000],
        data={k: v for k, v in data.items() if v is not None},
        actor=actor,
    )
    session.add(entry)
    getattr(log, "error" if entry.level == ActivityLevel.ERROR else "info")(
        "[ws=%s] %s/%s: %s", workspace_id, category, entry.level, message
    )
    return entry
