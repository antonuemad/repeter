"""Adapter registry. Modules are imported lazily so a missing optional dependency in one
platform never breaks the others. Tests can override adapters with `override(...)`."""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from repeater.models import PROVIDER_FOR_PLATFORM, Platform
from repeater.platforms.base import MediaLister, OAuthProvider, Publisher

_PROVIDER_MODULES = {
    "tiktok": "repeater.platforms.tiktok",
    "google": "repeater.platforms.google",
    "meta": "repeater.platforms.meta",
}
_PUBLISHER_MODULES = {
    Platform.TIKTOK: "repeater.platforms.tiktok",
    Platform.YOUTUBE: "repeater.platforms.youtube",
    Platform.INSTAGRAM: "repeater.platforms.instagram",
    Platform.FACEBOOK: "repeater.platforms.facebook",
}
_LISTER_MODULES = {
    **_PUBLISHER_MODULES,
    Platform.GDRIVE: "repeater.sources.drive",
}

_overrides: dict[tuple[str, str], Any] = {}


def get_oauth_provider(name: str) -> OAuthProvider:
    if ("oauth", name) in _overrides:
        return _overrides[("oauth", name)]
    return importlib.import_module(_PROVIDER_MODULES[name]).oauth


def get_provider_for_platform(platform: Platform | str) -> OAuthProvider:
    return get_oauth_provider(PROVIDER_FOR_PLATFORM[Platform(platform)])


def get_publisher(platform: Platform | str) -> Publisher:
    p = Platform(platform)
    if ("publisher", p.value) in _overrides:
        return _overrides[("publisher", p.value)]
    return importlib.import_module(_PUBLISHER_MODULES[p]).publisher


def get_lister(platform: Platform | str) -> MediaLister:
    p = Platform(platform)
    if ("lister", p.value) in _overrides:
        return _overrides[("lister", p.value)]
    return importlib.import_module(_LISTER_MODULES[p]).lister


@contextmanager
def override(kind: str, name: str, obj: Any) -> Iterator[Any]:
    """Temporarily replace an adapter: override("publisher", "tiktok", fake)."""
    key = (kind, str(name))
    prev = _overrides.get(key)
    _overrides[key] = obj
    try:
        yield obj
    finally:
        if prev is None:
            _overrides.pop(key, None)
        else:
            _overrides[key] = prev
