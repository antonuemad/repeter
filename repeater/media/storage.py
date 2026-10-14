"""On-disk layout of the media library, relative to settings.media_root:

    ws{workspace_id}/v{video_id}/original.<ext>
    ws{workspace_id}/v{video_id}/encoded.mp4
    ws{workspace_id}/v{video_id}/thumb.jpg
    uploads/<random>.part              (in-flight dashboard uploads)

Database columns store paths relative to media_root; resolve with abs_path().
"""

from __future__ import annotations

import shutil
from pathlib import Path

from repeater.config import get_settings

ENCODED_NAME = "encoded.mp4"
THUMB_NAME = "thumb.jpg"


def media_root() -> Path:
    root = Path(get_settings().media_root)
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def video_dir_rel(workspace_id: int, video_id: int) -> str:
    return f"ws{int(workspace_id)}/v{int(video_id)}"


def ensure_video_dir(workspace_id: int, video_id: int) -> Path:
    p = media_root() / video_dir_rel(workspace_id, video_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def abs_path(rel: str | None) -> Path | None:
    """Resolve a stored relative path, refusing anything outside media_root."""
    if not rel:
        return None
    root = media_root()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ValueError("path escapes media root")
    return p


def rel_path(p: Path) -> str:
    return str(Path(p).resolve().relative_to(media_root()))


def uploads_dir() -> Path:
    p = media_root() / "uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def delete_video_dir(workspace_id: int, video_id: int) -> None:
    shutil.rmtree(media_root() / video_dir_rel(workspace_id, video_id), ignore_errors=True)


def disk_usage() -> tuple[int, int, int]:
    """(total, used, free) bytes of the media volume."""
    u = shutil.disk_usage(media_root())
    return u.total, u.used, u.free
