"""Rotation rule defaults and readiness checks. OWNER: web module (contract C3).

Every new rule starts OFF: connecting an account never starts publishing by itself. The user
reviews the platform settings and turns the rule on (YouTube Developer Policies III.E.3.d and
III.I.2: express prior consent; PLAN.md 7). Readiness problems are stable machine codes
(`rule_problems`); every consumer renders them with `PROBLEM_MESSAGES.get(code, code)` or its own
translation of the code."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repeater.activity import log_activity
from repeater.engine.captions import platform_name
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

TIKTOK_PRIVATE = "SELF_ONLY"
YOUTUBE_PRIVACY_VALUES = ("public", "unlisted", "private")

# Problem codes returned by rule_problems(), with English text written as a phrase that follows
# "until you ..." (the planner builds its warning from it). The dashboard translates the codes
# itself (i18n keys "problem.<code>").
PROBLEM_MESSAGES: dict[str, str] = {
    "tiktok_privacy_required": "choose who can view the posts (privacy level)",
    "tiktok_music_consent_required": "accept TikTok's Music Usage Confirmation",
    "tiktok_branded_private": "make the posts visible to more than only you (branded content cannot be private)",
    "youtube_audience_required": "say whether the videos are made for kids",
    "youtube_privacy_invalid": "choose a valid YouTube privacy setting",
}


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
    Returns None for non-publish platforms (gdrive). EVERY new rule starts DISABLED: the user
    must review the post settings (TikTok privacy and Music Usage Confirmation, YouTube audience
    answer ...) and turn publishing on explicitly."""
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
        enabled=False,
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
        message = (
            f"TikTok publishing for '{account.display_name}' is off until you choose who can view the "
            "posts and accept TikTok's Music Usage Confirmation in the TikTok posting settings."
        )
        msg_key = "activity.rule_created_tiktok"
    else:
        message = (
            f"Publishing to {platform_name(platform)} account '{account.display_name}' is off until you "
            "review its settings and turn it on."
        )
        msg_key = "activity.rule_created"
    log_activity(
        session,
        account.workspace_id,
        ActivityLevel.INFO,
        "publish",
        message,
        account_id=account.id,
        rule_id=rule.id,
        msg_key=msg_key,
        msg_params={"name": account.display_name, "platform": platform_name(platform)},
    )
    return rule


def settings_problems(platform: Platform | str, settings: Mapping[str, Any] | None) -> list[str]:
    """Problem codes for a platform's post settings (see PROBLEM_MESSAGES). The adapters validate
    everything again at publish time; this stops the planner and publish-now from creating posts
    that are certain to fail, and stops a platform from publishing before the user answered a
    question that has no default."""
    platform = Platform(platform)
    defaults = RULE_SETTINGS_DEFAULTS.get(platform.value, {})
    merged = {**defaults, **(settings or {})}
    problems: list[str] = []
    if platform == Platform.TIKTOK:
        if merged.get("mode") == "inbox":
            return []
        privacy = merged.get("privacy_level")
        if not privacy:
            problems.append("tiktok_privacy_required")
        if not merged.get("music_consent_at"):
            problems.append("tiktok_music_consent_required")
        if privacy == TIKTOK_PRIVATE and merged.get("brand_content_toggle"):
            problems.append("tiktok_branded_private")
    elif platform == Platform.YOUTUBE:
        if merged.get("privacy_status") not in YOUTUBE_PRIVACY_VALUES:
            problems.append("youtube_privacy_invalid")
        if not isinstance(merged.get("self_declared_made_for_kids"), bool):
            problems.append("youtube_audience_required")
    return problems


def rule_problems(rule: RotationRule) -> list[str]:
    """Problem codes that must be fixed before the planner may schedule posts for this rule."""
    return settings_problems(rule.platform, rule.settings)


def account_problems(session: Session, account: ConnectedAccount) -> list[str]:
    """Problem codes for an account: those of its rule, or of the platform defaults when it has
    no rule yet."""
    rule = find_rule(session, account)
    if rule is not None:
        return rule_problems(rule)
    return settings_problems(account.platform, None)


def effective_daily_cap(rule: RotationRule) -> int:
    """The user's daily cap, clamped to the engine ceiling for the platform."""
    cap = int(rule.daily_cap if rule.daily_cap is not None else 0)
    return max(0, min(cap, ENGINE_DAILY_CAPS.get(Platform(rule.platform), cap)))
