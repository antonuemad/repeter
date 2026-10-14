"""Rotation rule defaults and readiness checks. OWNER: engine module."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repeater.activity import log_activity
from repeater.models import (
    PUBLISH_PLATFORMS,
    ActivityLevel,
    ConnectedAccount,
    Platform,
    RotationRule,
)
from repeater.platforms.base import RULE_SETTINGS_DEFAULTS

# Per-account daily ceilings Repeater enforces whatever the user configures (PLAN.md 2.5):
# TikTok 10 (platform ~15), YouTube 5 per channel, Instagram 50 with a 20% margin, Facebook 25 (30).
ENGINE_DAILY_CAPS: dict[Platform, int] = {
    Platform.TIKTOK: 10,
    Platform.YOUTUBE: 5,
    Platform.INSTAGRAM: 40,
    Platform.FACEBOOK: 25,
}

# Evergreen interval suggested when the user turns evergreen on (PLAN.md 4.1).
DEFAULT_EVERGREEN_DAYS = 90


def find_rule(session: Session, account: ConnectedAccount) -> RotationRule | None:
    return session.execute(
        select(RotationRule).where(
            RotationRule.workspace_id == account.workspace_id,
            RotationRule.connected_account_id == account.id,
        )
    ).scalar_one_or_none()


def default_settings(platform: Platform | str) -> dict[str, Any]:
    return copy.deepcopy(RULE_SETTINGS_DEFAULTS.get(Platform(platform).value, {}))


def ensure_rule(session: Session, account: ConnectedAccount) -> RotationRule | None:
    """Create the default RotationRule for a newly connected publish account if it has none.
    Returns None for non-publish platforms (gdrive). TikTok rules start DISABLED until the user
    picks a privacy level and gives music-usage consent (Content Sharing Guidelines)."""
    platform = Platform(account.platform)
    if platform not in PUBLISH_PLATFORMS:
        return None
    existing = find_rule(session, account)
    if existing is not None:
        return existing
    rule = RotationRule(
        workspace_id=account.workspace_id,
        connected_account_id=account.id,
        platform=platform,
        enabled=platform != Platform.TIKTOK,
        settings=default_settings(platform),
        evergreen_days=None,
        only_after=None,
    )
    try:
        # Savepoint: a concurrent connect may have created the rule first.
        with session.begin_nested():
            session.add(rule)
            session.flush()
    except IntegrityError:
        return find_rule(session, account)
    if platform == Platform.TIKTOK:
        log_activity(
            session,
            account.workspace_id,
            ActivityLevel.INFO,
            "publish",
            f"TikTok publishing for '{account.display_name}' is off until you choose who can view the "
            "posts and accept TikTok's Music Usage Confirmation in the TikTok posting settings.",
            account_id=account.id,
            rule_id=rule.id,
        )
    return rule


def rule_problems(rule: RotationRule) -> list[str]:
    """Settings that must be completed before the planner may schedule posts for this rule.
    The adapters validate everything again at publish time; this only stops the planner from
    filling the calendar with posts that are certain to fail."""
    if Platform(rule.platform) != Platform.TIKTOK:
        return []
    settings = {**RULE_SETTINGS_DEFAULTS["tiktok"], **(rule.settings or {})}
    if settings.get("mode") == "inbox":
        return []
    problems: list[str] = []
    if not settings.get("privacy_level"):
        problems.append("choose who can view the posts (privacy level)")
    if not settings.get("music_consent_at"):
        problems.append("accept TikTok's Music Usage Confirmation")
    return problems


def effective_daily_cap(rule: RotationRule) -> int:
    """The user's daily cap, clamped to the engine ceiling for the platform."""
    cap = int(rule.daily_cap if rule.daily_cap is not None else 0)
    return max(0, min(cap, ENGINE_DAILY_CAPS.get(Platform(rule.platform), cap)))
