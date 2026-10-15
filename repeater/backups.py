"""Backup freshness from host-written, read-only receipts. Unknown is never healthy."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from repeater.config import get_settings

BACKUP_STALE_HOURS = 36


@dataclass(frozen=True)
class Receipt:
    state: str
    age_hours: int | None = None

    @property
    def ok(self) -> bool:
        return self.state == "fresh"


def receipt(path: str | Path, now: float | None = None, *, max_hours: int = BACKUP_STALE_HOURS) -> Receipt:
    now = time.time() if now is None else now
    try:
        epoch = float(Path(path).read_text().split()[0])
        if not math.isfinite(epoch) or epoch <= 0 or epoch > now + 300:
            return Receipt("invalid")
    except FileNotFoundError:
        return Receipt("missing")
    except (OSError, ValueError, IndexError):
        return Receipt("invalid")
    age = max(0, int((now - epoch) / 3600))
    return Receipt("stale" if now - epoch >= max_hours * 3600 else "fresh", age)


def status(path: str | Path | None = None, now: float | None = None) -> tuple[bool, str]:
    marker = path if path is not None else get_settings().backup_status_file
    if not marker:
        return True, ""
    record = receipt(marker, now)
    if record.state in ("missing", "invalid"):
        return False, "no successful backup recorded yet (backup.sh runs daily at 03:17)"
    if record.state == "stale":
        return False, f"STALE (the last successful backup was {record.age_hours} hours ago)"
    return True, f"ok (last success {record.age_hours} hours ago)"


def stale_hours(now: float | None = None) -> int | None:
    marker = get_settings().backup_status_file
    if not marker:
        return None
    record = receipt(marker, now)
    return record.age_hours if record.state == "stale" else None


def receipts(now: float | None = None) -> dict[str, dict]:
    """Separate local/offsite database/media receipts; configuration alone proves none of them."""
    marker = get_settings().backup_status_file
    if not marker:
        return {
            name: {"state": "not_configured", "age_hours": None}
            for name in ("local", "offsite_db", "offsite_media")
        }
    folder = Path(marker).parent
    return {
        name: asdict(receipt(folder / filename, now))
        for name, filename in (
            ("local", "last_local_success"),
            ("offsite_db", "last_offsite_db_success"),
            ("offsite_media", "last_offsite_media_success"),
        )
    }
