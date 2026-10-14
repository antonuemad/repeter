"""Manual uploads from the dashboard. OWNER: sources/media module."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from repeater.models import Video


def create_video_from_upload(
    session: Session,
    *,
    workspace_id: int,
    upload_path: Path,
    original_filename: str,
    caption: str = "",
    title: str = "",
    actor: str = "user",
) -> Video:
    """Move an uploaded temp file (under storage.uploads_dir()) into the library as a new
    Video (source_platform None, source kind manual), and enqueue process_video. Validates the
    file with ffprobe first; raises ValueError with a user-facing message if it is not a video."""
    raise NotImplementedError
