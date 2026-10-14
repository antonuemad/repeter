"""Freshness of the database backup, from the marker file backup.sh writes after each success.

backup.sh (deploy/) records "<epoch> <ISO time>" in settings.backup_status_file; the directory is
mounted read-only into the web and worker containers. Both `repeater.cli check` and the dashboard
overview use `status` to warn when the last successful backup is older than BACKUP_STALE_HOURS."""

from __future__ import annotations

import time
from pathlib import Path

from repeater.config import get_settings

BACKUP_STALE_HOURS = 36


def status(path: str | Path | None = None, now: float | None = None) -> tuple[bool, str]:
    """(ok, text). The text is empty when no marker location is configured here (a development
    machine); a missing or unreadable marker is not stale (a new server has no backup yet)."""
    marker = path if path is not None else get_settings().backup_status_file
    if not marker:
        return True, ""
    try:
        epoch = float(Path(marker).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return True, "no successful backup recorded yet (backup.sh runs daily at 03:17)"
    age_hours = ((now if now is not None else time.time()) - epoch) / 3600
    if age_hours >= BACKUP_STALE_HOURS:
        return False, f"STALE (the last successful backup was {int(age_hours)} hours ago)"
    return True, f"ok (last success {int(age_hours)} hours ago)"


def stale_hours(now: float | None = None) -> int | None:
    """Age in whole hours of the last successful backup when it is stale, else None."""
    ok, text = status(None, now)
    if ok or not text:
        return None
    return int(text.split("was ")[1].split(" hours")[0])
