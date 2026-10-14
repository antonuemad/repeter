"""Titles and captions per platform (PLAN.md 4.4).

A rule's caption_template may use {title}, {caption}, {hashtags} and {source_caption}
({description} is accepted as an alias of {caption}). Anything else in the template, including
other text in braces, is kept literally. When the rule has hashtags but the template does not
mention {hashtags}, they are appended after a blank line so they are never silently dropped.

{caption} is the per-video override (video.overrides["caption"]) when set, otherwise the caption
imported from the source; {source_caption} is always the imported caption.

Platform limits come from repeater.platforms.base.LIMITS. Lengths are counted the way each
platform counts them: TikTok in UTF-16 code units, YouTube descriptions in UTF-8 bytes, the
others in characters. Truncation never splits an emoji sequence or a letter from its marks.
YouTube titles and descriptions may not contain < or >, so those are removed for YouTube.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

from repeater.models import Platform, RotationRule, Video
from repeater.platforms.base import LIMITS

DEFAULT_TEMPLATE = "{caption}"
INSTAGRAM_MAX_HASHTAGS = 30
YOUTUBE_DESCRIPTION_MAX_BYTES = 5000

_VAR_RE = re.compile(r"\{(title|caption|description|hashtags|source_caption)\}")
_MANY_NEWLINES_RE = re.compile(r"\n{3,}")
_TRAILING_SPACE_RE = re.compile(r"[ \t]+\n")
_HASHTAG_RE = re.compile(r"(?<![\w#])#\w+")
_SPLIT_TAGS_RE = re.compile(r"[\s,]+")


def render(rule: RotationRule | None, video: Video, platform: Platform | str) -> tuple[str, str]:
    """Return (title, caption) for publishing `video` to `platform` under `rule`."""
    platform = Platform(platform)
    overrides = video.overrides or {}
    source_caption = tidy(video.caption or "")
    caption = tidy(_text(overrides.get("caption"))) or source_caption
    title = _single_line(_text(overrides.get("title")) or video.title or _first_line(caption))
    hashtags = normalize_hashtags(rule.hashtags if rule is not None else "")
    template = (rule.caption_template if rule is not None else None) or DEFAULT_TEMPLATE
    values = {
        "title": title,
        "caption": caption,
        "description": caption,
        "hashtags": hashtags,
        "source_caption": source_caption,
    }
    text = _VAR_RE.sub(lambda m: values[m.group(1)], template)
    if hashtags and "{hashtags}" not in template:
        text = f"{text}\n\n{hashtags}"
    if platform == Platform.YOUTUBE:
        title, text = _strip_angle_brackets(title), _strip_angle_brackets(text)
    text = tidy(text)
    if platform == Platform.INSTAGRAM:
        text = tidy(limit_hashtags(text, INSTAGRAM_MAX_HASHTAGS))
    return truncate_title(title, platform), truncate_caption(text, platform)


def tidy(text: str) -> str:
    """Normalise newlines, drop trailing spaces, collapse 3+ newlines into a blank line, trim."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING_SPACE_RE.sub("\n", text)
    return _MANY_NEWLINES_RE.sub("\n\n", text).strip()


def normalize_hashtags(raw: str | None) -> str:
    """'magic, #Tricks magic' -> '#magic #Tricks' (each tag once, original order)."""
    seen: set[str] = set()
    tags: list[str] = []
    for token in _SPLIT_TAGS_RE.split(raw or ""):
        word = token.strip().lstrip("#")
        if not word or word.casefold() in seen:
            continue
        seen.add(word.casefold())
        tags.append(f"#{word}")
    return " ".join(tags)


def limit_hashtags(text: str, maximum: int) -> str:
    """Remove hashtags after the first `maximum` ones."""
    count = 0

    def keep_first(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return m.group(0) if count <= maximum else ""

    out = _HASHTAG_RE.sub(keep_first, text)
    if count <= maximum:
        return text
    return re.sub(r"[ \t]{2,}", " ", out)


def truncate_title(title: str, platform: Platform | str) -> str:
    limit = LIMITS[Platform(platform)].max_title_chars
    if limit is None:
        return title
    return _truncate(title, limit, _utf16_units)


def truncate_caption(caption: str, platform: Platform | str) -> str:
    platform = Platform(platform)
    if platform == Platform.YOUTUBE:
        return _truncate(caption, YOUTUBE_DESCRIPTION_MAX_BYTES, _utf8_bytes)
    limit = LIMITS[platform].max_caption_chars
    if platform == Platform.TIKTOK:
        return _truncate(caption, limit, _utf16_units)
    return _truncate(caption, limit, lambda ch: 1)


# --------------------------------------------------------------------------- helpers


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _strip_angle_brackets(text: str) -> str:
    return text.replace("<", "").replace(">", "")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line
    return ""


def _single_line(text: str) -> str:
    return " ".join(text.split())


def _utf16_units(ch: str) -> int:
    return 2 if ord(ch) > 0xFFFF else 1


def _utf8_bytes(ch: str) -> int:
    return len(ch.encode("utf-8", "surrogatepass"))


def _truncate(text: str, limit: int, size: Callable[[str], int]) -> str:
    used = 0
    for index, ch in enumerate(text):
        used += size(ch)
        if used > limit:
            return text[: _cluster_boundary(text, index)].rstrip()
    return text


def _attaches_to_previous(ch: str) -> bool:
    cp = ord(ch)
    return (
        ch == "‍"
        or 0xFE00 <= cp <= 0xFE0F
        or 0x1F3FB <= cp <= 0x1F3FF
        or 0xE0020 <= cp <= 0xE007F
        or unicodedata.category(ch) in ("Mn", "Mc", "Me")
    )


def _cluster_boundary(text: str, index: int) -> int:
    """Move a cut position left so it does not split a character from its marks, an emoji
    ZWJ sequence or a flag (pair of regional indicators)."""
    while 0 < index < len(text) and (_attaches_to_previous(text[index]) or text[index - 1] == "‍"):
        index -= 1
    run = 0
    while index - run > 0 and 0x1F1E6 <= ord(text[index - run - 1]) <= 0x1F1FF:
        run += 1
    return index - 1 if run % 2 else index


# --------------------------------------------------------------------------- labels for messages

PLATFORM_NAMES = {
    Platform.TIKTOK: "TikTok",
    Platform.YOUTUBE: "YouTube",
    Platform.INSTAGRAM: "Instagram",
    Platform.FACEBOOK: "Facebook",
    Platform.GDRIVE: "Google Drive",
}


def platform_name(platform: Platform | str) -> str:
    try:
        return PLATFORM_NAMES[Platform(platform)]
    except ValueError:
        return str(platform)


def video_label(video: Video) -> str:
    """Short human label for activity messages: override title, title, caption line or id."""
    overrides = video.overrides or {}
    text = _single_line(_text(overrides.get("title")) or video.title or _first_line(video.caption or ""))
    return text[:60] or f"#{video.id}"
