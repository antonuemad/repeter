"""Conservative pilot eligibility. This module never calls a publishing adapter.

One library item has one placement. Changing placement retains the item's rotation history.
The short story window and vertical canvas are deliberate pilot rules, not universal API maxima.
"""

from repeater.models import ConnectedAccount, Platform, Video

FORMATS = {"video": ("reel", "story"), "image": ("post", "story"), "text": ("post",)}


def caption(item: Video) -> str:
    return str((item.overrides or {}).get("caption", item.caption) or "")


def problem(item: Video, account: ConnectedAccount) -> str | None:
    from repeater.engine.scheduler import limits_problem

    kind, placement, platform = item.media_kind or "video", item.content_format or "reel", account.platform
    if placement not in FORMATS.get(kind, ()):
        return "unsupported"
    if kind != "text" and not item.encoded_path:
        return "not_ready"
    if platform == Platform.FACEBOOK and (account.extra or {}).get("can_manage") is False:
        return "permission"
    if kind == "text":
        if platform != Platform.FACEBOOK:
            return "unsupported"
        return None if caption(item).strip() else "empty_text"
    if placement == "story":
        if platform not in (Platform.FACEBOOK, Platform.INSTAGRAM):
            return "unsupported"
        if platform == Platform.INSTAGRAM and (account.extra or {}).get("account_type") != "BUSINESS":
            return "business_required"
        if not item.width or not item.height or abs(item.width / item.height - 9 / 16) > 0.02:
            return "story_vertical"
        if kind == "video" and (item.duration_s is None or not 3 <= item.duration_s <= 60):
            return "story_duration"
        if kind == "video" and (item.encoded_size_bytes or 0) > 100 * 1024 * 1024:
            return "story_size"
        return None
    if kind == "image":
        if platform not in (Platform.FACEBOOK, Platform.INSTAGRAM):
            return "unsupported"
        if platform == Platform.INSTAGRAM:
            if not item.width or not item.height or not 0.8 <= item.width / item.height <= 1.91:
                return "image_ratio"
            if len(caption(item)) > 2200:
                return "caption_length"
        return None
    return "video_limits" if limits_problem(item, platform) else None
