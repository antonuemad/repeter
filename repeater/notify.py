"""Out-of-band notifications (Telegram). OWNER: engine module."""

from __future__ import annotations


def notify(text: str, *, workspace_id: int | None = None, level: str = "info") -> bool:
    """Send a short message to the configured Telegram chat. Never raises; returns success."""
    raise NotImplementedError
