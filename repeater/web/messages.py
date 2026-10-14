"""Showing errors raised by other modules in the user's language.

Engine actions and importers raise UserError with explicit keys and parameters. The pattern
map below is only a compatibility fallback for legacy callers and third-party ValueErrors.
Unknown diagnostics remain readable rather than being discarded."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from fastapi import Request

from repeater.web import i18n
from repeater.web.templating import flash

# (pattern for the whole message, i18n key). Named groups become the key's parameters.
_KNOWN: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.S), key)
    for pattern, key in (
        (r"This post is \w+ and can no longer be skipped\.", "err.post_not_skippable"),
        (r"This post is \w+ and cannot be published again from here\.", "err.post_not_publishable"),
        (r"Publishing is paused for this workspace; resume it first\.", "err.paused"),
        (r"The account is disconnected; connect it again first\.", "err.account_disconnected"),
        (r"The (?P<platform>.+) account '(?P<name>.+)' must be reconnected first\.", "err.account_reconnect"),
        (r"The video and the account belong to different workspaces\.", "err.workspace_mismatch"),
        (r"(?P<platform>.+) accounts cannot be published to\.", "err.not_publishable"),
        (r"Only videos that are ready \(imported and encoded\) can be published\.", "err.video_not_ready"),
        (
            r"This video came from this account; publishing it there again would duplicate it\.",
            "err.same_origin",
        ),
        (r"(?P<platform>.+?) cannot take this video: (?P<problem>.+)\.", "err.platform_limit"),
        (r"This video is being published to this account right now\.", "err.in_flight"),
        (r"Only archived videos can be restored\.", "err.not_archived"),
        (
            r"This video is a duplicate of another video in the library and stays out of rotation\.",
            "err.duplicate_video",
        ),
        (r"The video's files are no longer on the server; upload it again\.", "err.files_gone"),
        (r"Only failed videos can be retried\.", "err.not_failed"),
        (r"The original file is gone; upload the video again\.", "err.original_gone"),
        (r"The upload was not found\. Please upload the file again\.", "err.upload_not_found"),
        (r"The upload is empty or incomplete\. Please upload the file again\.", "err.upload_empty"),
    )
)


def known_error(exc: BaseException) -> tuple[str, Mapping[str, Any]] | None:
    """(i18n key, params) for an exception the dashboard can translate, else None."""
    key = getattr(exc, "key", None)
    if isinstance(key, str) and i18n.has_key(key):
        params = getattr(exc, "params", None)
        return key, dict(params) if isinstance(params, Mapping) else {}
    text = str(exc)
    for pattern, known_key in _KNOWN:
        match = pattern.fullmatch(text)
        if match:
            return known_key, match.groupdict()
    return None


def flash_error(request: Request, exc: BaseException) -> None:
    """Flash an error from another module, translated when it is a known one."""
    known = known_error(exc)
    if known is None:
        flash(request, "error", text=str(exc))
    else:
        key, params = known
        flash(request, "error", key, **params)
