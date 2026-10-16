"""Local rich-media validation shared by the Meta adapters; no network access."""

from repeater.config import get_settings
from repeater.media.content import problem
from repeater.models import Video
from repeater.platforms.base import PermanentError


def require_publishing():
    if not get_settings().publishing_enabled:
        raise PermanentError("Publishing is disabled on this server during testing.", code="testing_lock")


def problems(account, req):
    item = Video(
        media_kind=req.media_kind,
        content_format=req.content_format,
        encoded_path=str(req.video_path) if req.size_bytes > 0 else None,
        duration_s=req.duration_s,
        width=req.width,
        height=req.height,
        encoded_size_bytes=req.size_bytes,
        caption=req.caption,
        overrides={},
    )
    issue = problem(item, account)
    out = [f"Content is not compatible with this destination ({issue})."] if issue else []
    if req.media_kind == "image" and (req.size_bytes <= 0 or req.size_bytes > 8 * 1024**2):
        out.append("Prepared images must be JPEG files between 1 byte and 8 MB.")
    if req.media_kind == "text" and len(req.caption) > 5000:
        out.append("Text posts are limited to 5,000 characters in this pilot.")
    return out
