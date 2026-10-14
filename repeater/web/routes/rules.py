"""Rotation rules per target account, platform post settings, and workspace settings.

The TikTok settings form follows TikTok's Content Sharing Guidelines: creator_info is fetched
every time the form is rendered (and again on save), the privacy dropdown only offers
`privacy_level_options` and has no default, interaction toggles start unchecked and are disabled
when the creator turned them off, commercial content disclosure offers "Your brand" and
"Branded content" (branded content cannot be private), and the Music Usage Confirmation consent
is an explicit checkbox.

While TikTok has not audited the app (settings.tiktok_audited is false) every API post is
restricted to private viewing, so only "Only me" is offered and a public account is warned about."""

from __future__ import annotations

import importlib
import logging
import zoneinfo
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import FormData

from repeater import accounts as account_service
from repeater.activity import log_activity
from repeater.config import get_settings
from repeater.crypto import CryptoError
from repeater.engine import actions
from repeater.engine import rules as engine_rules
from repeater.engine.captions import platform_name
from repeater.jobs import kinds
from repeater.models import (
    PUBLISH_PLATFORMS,
    AccountStatus,
    ActivityLevel,
    ConnectedAccount,
    Platform,
    RotationRule,
)
from repeater.platforms.base import RULE_SETTINGS_DEFAULTS, PlatformError, TikTokCreatorInfo
from repeater.timeutil import utcnow
from repeater.web.auth import CurrentWorkspace, WorkspaceContext
from repeater.web.deps import DB, Form, FormError, form_bool, form_int, form_str, form_text, form_time
from repeater.web.templating import flash, render

log = logging.getLogger("repeater.web.rules")
router = APIRouter()

TIKTOK_PRIVACY_LEVELS = ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY")
TIKTOK_PRIVATE = "SELF_ONLY"
TIKTOK_PUBLIC = "PUBLIC_TO_EVERYONE"
YOUTUBE_PRIVACY = ("public", "unlisted", "private")
# Assignable YouTube video categories (videoCategories.list); the description is translated.
YOUTUBE_CATEGORIES = ("1", "2", "10", "15", "17", "19", "20", "22", "23", "24", "25", "26", "27", "28", "29")
YOUTUBE_TAGS_MAX_CHARS = 500
MAX_TEMPLATE_CHARS = 5000
MAX_HASHTAG_CHARS = 1000
MAX_GAP_MINUTES = 7 * 24 * 60
MIN_EVERGREEN_DAYS = 7
MAX_EVERGREEN_DAYS = 3650
MIN_HORIZON_HOURS = 12
MAX_HORIZON_HOURS = 7 * 24
MAX_CROSS_GAP_HOURS = 7 * 24


@lru_cache(maxsize=1)
def timezones() -> list[str]:
    return sorted(
        z for z in zoneinfo.available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/"))
    )


# --------------------------------------------------------------------------- lookups


def _publish_accounts(db: Session, workspace_id: int) -> list[ConnectedAccount]:
    return list(
        db.execute(
            select(ConnectedAccount)
            .where(
                ConnectedAccount.workspace_id == workspace_id,
                ConnectedAccount.platform.in_(PUBLISH_PLATFORMS),
                ConnectedAccount.status != AccountStatus.DISABLED,
            )
            .order_by(ConnectedAccount.platform, ConnectedAccount.id)
        ).scalars()
    )


def _get_account(db: Session, ctx: WorkspaceContext, account_id: int) -> ConnectedAccount:
    account = db.get(ConnectedAccount, account_id)
    if (
        account is None
        or account.workspace_id != ctx.workspace.id
        or Platform(account.platform) not in PUBLISH_PLATFORMS
        or account.status == AccountStatus.DISABLED
    ):
        raise HTTPException(status_code=404)
    return account


def _rule_for(db: Session, account: ConnectedAccount) -> RotationRule:
    rule = engine_rules.ensure_rule(db, account)
    if rule is None:  # not a publish platform; guarded by _get_account
        raise HTTPException(status_code=404)
    return rule


def merged_settings(rule: RotationRule) -> dict[str, Any]:
    defaults = RULE_SETTINGS_DEFAULTS.get(Platform(rule.platform).value, {})
    return {**defaults, **(rule.settings or {})}


# --------------------------------------------------------------------------- TikTok creator info


@dataclass
class CreatorInfoResult:
    info: TikTokCreatorInfo | None = None
    error: str | None = None


def fetch_creator_info(db: Session, account: ConnectedAccount) -> CreatorInfoResult:
    """Latest creator_info, as required each time the TikTok post settings are shown."""
    try:
        context = account_service.fresh_context(db, account)
    except (account_service.AccountUnavailable, CryptoError):
        db.commit()  # keep the needs-reconnect mark set by fresh_context
        return CreatorInfoResult(error="rules.tiktok_reconnect")
    except PlatformError as exc:
        db.rollback()
        return CreatorInfoResult(error=exc.message)
    # Persist refreshed tokens first: TikTok may have rotated the refresh token.
    db.commit()
    try:
        info = importlib.import_module("repeater.platforms.tiktok").creator_info(context)
    except PlatformError as exc:
        log.info("TikTok creator_info failed for account %s: %s", account.id, exc.code or type(exc).__name__)
        return CreatorInfoResult(error=exc.message)
    return CreatorInfoResult(info=info)


# --------------------------------------------------------------------------- list page


@router.get("/rules")
def rules_page(request: Request, db: DB, ctx: CurrentWorkspace) -> Response:
    accounts = _publish_accounts(db, ctx.workspace.id)
    rules = {
        r.connected_account_id: r
        for r in db.execute(
            select(RotationRule)
            .options(selectinload(RotationRule.connected_account))
            .where(RotationRule.workspace_id == ctx.workspace.id)
        ).scalars()
    }
    rows = [
        {
            "account": a,
            "rule": rules.get(a.id),
            "problems": engine_rules.rule_problems(rules[a.id]) if a.id in rules else [],
            "cap": engine_rules.effective_daily_cap(rules[a.id]) if a.id in rules else None,
        }
        for a in accounts
    ]
    return _rules_list(request, ctx, rows)


def _rules_list(request: Request, ctx: WorkspaceContext, rows: list[dict[str, Any]]) -> Response:
    return render(request, "rules.html", {"nav": "rules", "rows": rows})


@router.get("/settings/workspace")
def workspace_settings(request: Request, ctx: CurrentWorkspace) -> Response:
    ws = ctx.workspace
    return render(
        request,
        "settings_workspace.html",
        {
            "nav": "settings",
            "timezones": timezones() if ws.timezone in timezones() else [ws.timezone, *timezones()],
            "ws_values": {
                "timezone": ws.timezone,
                "cross_gap_hours": round((ws.cross_platform_gap_minutes or 0) / 60, 2),
                "horizon_hours": ws.horizon_hours,
                "paused": ws.paused,
            },
            "limits": {
                "min_horizon": MIN_HORIZON_HOURS,
                "max_horizon": MAX_HORIZON_HOURS,
                "max_cross_gap": MAX_CROSS_GAP_HOURS,
            },
        },
    )


@router.post("/settings/workspace")
def save_workspace(request: Request, db: DB, ctx: CurrentWorkspace, form: Form) -> Response:
    ws = ctx.workspace
    try:
        zone = form_str(form, "timezone", max_len=64, required=True)
        if zone not in timezones() and zone != "UTC":
            raise FormError("rules.error_timezone")
        gap_hours = form_int(form, "cross_gap_hours", minimum=0, maximum=MAX_CROSS_GAP_HOURS)
        horizon = form_int(form, "horizon_hours", minimum=MIN_HORIZON_HOURS, maximum=MAX_HORIZON_HOURS)
    except FormError as exc:
        flash(request, "error", exc.key, **exc.params)
        return RedirectResponse("/settings/workspace", status_code=303)
    paused = form_bool(form, "paused")
    changed_pause = paused != ws.paused
    plan_changed = (
        changed_pause
        or zone != ws.timezone
        or gap_hours * 60 != (ws.cross_platform_gap_minutes or 0)
        or horizon != ws.horizon_hours
    )
    ws.timezone = zone
    ws.cross_platform_gap_minutes = gap_hours * 60
    ws.horizon_hours = horizon
    ws.paused = paused
    if changed_pause:
        message, key = (
            ("Paused all publishing.", "activity.paused")
            if paused
            else ("Resumed publishing.", "activity.resumed")
        )
    else:
        message, key = "Updated the workspace settings.", "activity.workspace_updated"
    log_activity(db, ws.id, ActivityLevel.INFO, "system", message, actor=ctx.actor, msg_key=key)
    _replan(db, ctx, changed=plan_changed)
    db.commit()
    flash(request, "success", "rules.workspace_saved")
    return RedirectResponse("/settings/workspace", status_code=303)


def _replan(db: Session, ctx: WorkspaceContext, *, changed: bool, account_id: int | None = None) -> None:
    """Posts the planner already created keep the window, cap, gaps and timezone they were planned
    under (it never moves existing posts), so when any of those changed the not-yet-started
    planner posts are canceled and planned again under the new rules (contract C2)."""
    if changed:
        actions.invalidate_plan(db, ctx.workspace.id, account_id=account_id, actor=ctx.actor)
    else:
        kinds.enqueue_plan_workspace(db, ctx.workspace.id)


# --------------------------------------------------------------------------- rule form


@router.get("/rules/{account_id}")
def rule_form(request: Request, db: DB, ctx: CurrentWorkspace, account_id: int) -> Response:
    account = _get_account(db, ctx, account_id)
    rule = _rule_for(db, account)
    db.commit()
    creator = fetch_creator_info(db, account) if account.platform == Platform.TIKTOK else CreatorInfoResult()
    return _render_rule(request, ctx, account, rule, _rule_values(rule), creator)


def _rule_values(rule: RotationRule) -> dict[str, Any]:
    settings = merged_settings(rule)
    return {
        "enabled": rule.enabled,
        "daily_cap": rule.daily_cap,
        "min_gap_minutes": rule.min_gap_minutes,
        "window_start": rule.window_start.strftime("%H:%M") if rule.window_start else "10:00",
        "window_end": rule.window_end.strftime("%H:%M") if rule.window_end else "22:00",
        "caption_template": rule.caption_template or "{caption}",
        "hashtags": rule.hashtags or "",
        "evergreen": rule.evergreen_days is not None,
        "evergreen_days": rule.evergreen_days or engine_rules.DEFAULT_EVERGREEN_DAYS,
        "only_new": rule.only_after is not None,
        "settings": settings,
        "tags_text": ", ".join(str(t) for t in settings.get("tags") or []),
        "disclose": bool(settings.get("brand_content_toggle") or settings.get("brand_organic_toggle")),
        "music_consent": bool(settings.get("music_consent_at")),
    }


def _render_rule(
    request: Request,
    ctx: WorkspaceContext,
    account: ConnectedAccount,
    rule: RotationRule,
    values: dict[str, Any],
    creator: CreatorInfoResult,
    *,
    error: FormError | None = None,
    status_code: int = 200,
) -> Response:
    platform = Platform(account.platform)
    privacy_options = list(creator.info.privacy_level_options) if creator.info else []
    unaudited = platform == Platform.TIKTOK and not get_settings().tiktok_audited
    # An unaudited app can only post "Only me", and only to a private account: a public account
    # is the one that is offered PUBLIC_TO_EVERYONE.
    account_is_public = unaudited and TIKTOK_PUBLIC in privacy_options
    if unaudited:
        privacy_options = [opt for opt in privacy_options if opt == TIKTOK_PRIVATE]
    saved_privacy = values["settings"].get("privacy_level")
    context = {
        "nav": "rules",
        "account": account,
        "rule": rule,
        "platform": platform.value,
        "values": values,
        "error": error,
        "creator": creator.info,
        "creator_error": creator.error,
        # Pre-select only the user's own earlier choice, and only if TikTok still offers it.
        "selected_privacy": saved_privacy if saved_privacy in privacy_options else None,
        "privacy_options": privacy_options,
        "tiktok_unaudited": unaudited,
        "tiktok_account_public": account_is_public,
        "engine_cap": engine_rules.ENGINE_DAILY_CAPS.get(platform, 1),
        "problems": engine_rules.rule_problems(rule),
        "youtube_privacy": YOUTUBE_PRIVACY,
        "youtube_categories": YOUTUBE_CATEGORIES,
        "max_gap": MAX_GAP_MINUTES,
        "min_evergreen": MIN_EVERGREEN_DAYS,
        "max_evergreen": MAX_EVERGREEN_DAYS,
    }
    return render(request, "rule_form.html", context, status_code=status_code)


SCHEDULING_FIELDS = (
    "enabled",
    "daily_cap",
    "min_gap_minutes",
    "window_start",
    "window_end",
    "evergreen_days",
)


def _scheduling_signature(rule: RotationRule) -> tuple[Any, ...]:
    """What the planner bases its slots on; captions and platform settings are read at publish time."""
    return (*(getattr(rule, name) for name in SCHEDULING_FIELDS), rule.only_after is not None)


@router.post("/rules/{account_id}")
def save_rule(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, account_id: int) -> Response:
    account = _get_account(db, ctx, account_id)
    rule = _rule_for(db, account)
    db.commit()
    platform = Platform(account.platform)
    creator = fetch_creator_info(db, account) if platform == Platform.TIKTOK else CreatorInfoResult()
    # The TikTok fields are missing from a form that was rendered without creator info. Missing
    # fields are not answers: the stored TikTok settings and the on/off state must not change.
    fields_absent = platform == Platform.TIKTOK and _tiktok_fields_absent(form)
    try:
        general = _parse_general(form, platform)
        if fields_absent:
            general["enabled"] = general["enabled"] and bool(rule.enabled)
        settings = (
            None
            if fields_absent
            else _parse_platform_settings(
                form, platform, merged_settings(rule), creator, enabled=general["enabled"]
            )
        )
    except FormError as exc:
        return _render_rule(
            request, ctx, account, rule, _submitted_values(form, rule), creator, error=exc, status_code=400
        )
    before = _scheduling_signature(rule)
    _apply_general(rule, general)
    if settings is not None:
        rule.settings = settings
    log_activity(
        db,
        ctx.workspace.id,
        ActivityLevel.INFO,
        "publish",
        f"Updated the publishing rule for {account.platform} account '{account.display_name}' "
        f"({'on' if rule.enabled else 'off'}).",
        actor=ctx.actor,
        account_id=account.id,
        rule_id=rule.id,
        msg_key="activity.rule_updated_on" if rule.enabled else "activity.rule_updated_off",
        msg_params={"platform": platform_name(platform), "name": account.display_name},
    )
    _replan(db, ctx, changed=_scheduling_signature(rule) != before, account_id=account.id)
    db.commit()
    problems = engine_rules.rule_problems(rule)
    flash(request, "success", "rules.saved", name=account.display_name)
    if fields_absent:
        flash(request, "warning", "rules.tiktok_unchanged")
    if rule.enabled and problems:
        flash(request, "warning", "rules.saved_incomplete")
    return RedirectResponse(f"/rules/{account.id}", status_code=303)


def _tiktok_fields_absent(form: FormData) -> bool:
    return form.get("tiktok_fields_absent") == "1" or form.get("mode") not in ("direct", "inbox")


def _parse_general(form: FormData, platform: Platform) -> dict[str, Any]:
    cap_max = engine_rules.ENGINE_DAILY_CAPS.get(platform, 1)
    evergreen = form_bool(form, "evergreen")
    return {
        "enabled": form_bool(form, "enabled"),
        "daily_cap": form_int(form, "daily_cap", minimum=1, maximum=cap_max),
        "min_gap_minutes": form_int(form, "min_gap_minutes", minimum=0, maximum=MAX_GAP_MINUTES),
        "window_start": form_time(form, "window_start"),
        "window_end": form_time(form, "window_end"),
        "caption_template": form_text(form, "caption_template", max_len=MAX_TEMPLATE_CHARS) or "{caption}",
        "hashtags": form_str(form, "hashtags", max_len=MAX_HASHTAG_CHARS),
        "evergreen_days": (
            form_int(form, "evergreen_days", minimum=MIN_EVERGREEN_DAYS, maximum=MAX_EVERGREEN_DAYS)
            if evergreen
            else None
        ),
        "only_new": form_bool(form, "only_new"),
    }


def _apply_general(rule: RotationRule, general: dict[str, Any]) -> None:
    rule.enabled = general["enabled"]
    rule.daily_cap = general["daily_cap"]
    rule.min_gap_minutes = general["min_gap_minutes"]
    rule.window_start = general["window_start"]
    rule.window_end = general["window_end"]
    rule.caption_template = general["caption_template"]
    rule.hashtags = general["hashtags"]
    rule.evergreen_days = general["evergreen_days"]
    if not general["only_new"]:
        rule.only_after = None
    elif rule.only_after is None:
        rule.only_after = utcnow()


def _parse_platform_settings(
    form: FormData, platform: Platform, current: dict[str, Any], creator: CreatorInfoResult, *, enabled: bool
) -> dict[str, Any] | None:
    """New RotationRule.settings, or None to keep the stored ones unchanged."""
    if platform == Platform.TIKTOK:
        return _parse_tiktok(form, current, creator, enabled=enabled)
    if platform == Platform.YOUTUBE:
        return _parse_youtube(form, current)
    if platform == Platform.INSTAGRAM:
        return {
            **current,
            "share_to_feed": form_bool(form, "share_to_feed"),
            "is_ai_generated": form_bool(form, "is_ai_generated"),
        }
    return {**current, "is_ai_generated": form_bool(form, "is_ai_generated")}


def _parse_tiktok(
    form: FormData, current: dict[str, Any], creator: CreatorInfoResult, *, enabled: bool
) -> dict[str, Any] | None:
    info = creator.info
    if info is None:
        # Without fresh creator info the settings cannot be checked: they may only be turned off.
        if enabled:
            raise FormError("rules.error_creator_info")
        return None
    mode = form.get("mode") if form.get("mode") in ("direct", "inbox") else "direct"
    settings = {**current, "mode": mode, "is_aigc": form_bool(form, "is_aigc")}
    privacy = form.get("privacy_level")
    privacy = privacy if isinstance(privacy, str) and privacy else None
    if privacy is not None and privacy not in info.privacy_level_options:
        raise FormError("rules.error_privacy_option")
    if mode == "direct" and privacy not in (None, TIKTOK_PRIVATE) and not get_settings().tiktok_audited:
        raise FormError("rules.error_unaudited_privacy")
    disclose = form_bool(form, "disclose")
    brand_organic = disclose and form_bool(form, "brand_organic")
    brand_content = disclose and form_bool(form, "brand_content")
    if disclose and not (brand_organic or brand_content):
        raise FormError("rules.error_disclose_choice")
    if brand_content and privacy == TIKTOK_PRIVATE:
        raise FormError("rules.error_branded_private")
    consent = form_bool(form, "music_consent")
    if mode == "direct" and enabled:
        if privacy is None:
            raise FormError("rules.error_privacy_required")
        if not consent:
            raise FormError("rules.error_consent_required")
    settings.update(
        privacy_level=privacy,
        allow_comment=form_bool(form, "allow_comment") and not info.comment_disabled,
        allow_duet=form_bool(form, "allow_duet") and not info.duet_disabled,
        allow_stitch=form_bool(form, "allow_stitch") and not info.stitch_disabled,
        brand_organic_toggle=brand_organic,
        brand_content_toggle=brand_content,
        music_consent_at=(current.get("music_consent_at") or utcnow().isoformat()) if consent else None,
    )
    return settings


def _parse_youtube(form: FormData, current: dict[str, Any]) -> dict[str, Any]:
    privacy = form.get("privacy_status")
    if privacy not in YOUTUBE_PRIVACY:
        raise FormError("rules.error_youtube_privacy")
    kids = form.get("made_for_kids")
    if kids not in ("yes", "no"):
        raise FormError("rules.error_made_for_kids")
    category = form.get("category_id")
    if category not in YOUTUBE_CATEGORIES:
        raise FormError("rules.error_category")
    tags = _parse_tags(form_str(form, "tags", max_len=YOUTUBE_TAGS_MAX_CHARS * 2))
    return {
        **current,
        "privacy_status": privacy,
        "self_declared_made_for_kids": kids == "yes",
        "category_id": category,
        "notify_subscribers": form_bool(form, "notify_subscribers"),
        "contains_synthetic_media": form_bool(form, "contains_synthetic_media"),
        "tags": tags,
    }


def _parse_tags(raw: str) -> list[str]:
    """Comma-separated tags. YouTube counts commas, and two quotes for tags with spaces,
    towards its 500-character limit."""
    tags: list[str] = []
    for part in raw.split(","):
        tag = " ".join(part.replace("#", " ").split())
        if tag and tag not in tags:
            tags.append(tag)
    size = sum(len(t) + (2 if " " in t else 0) for t in tags) + max(0, len(tags) - 1)
    if size > YOUTUBE_TAGS_MAX_CHARS:
        raise FormError("rules.error_tags_too_long", max=YOUTUBE_TAGS_MAX_CHARS)
    return tags


def _submitted_values(form: FormData, rule: RotationRule) -> dict[str, Any]:
    """Re-display what the user typed after a validation error."""
    values = _rule_values(rule)
    for key in (
        "daily_cap",
        "min_gap_minutes",
        "window_start",
        "window_end",
        "caption_template",
        "hashtags",
        "evergreen_days",
    ):
        raw = form.get(key)
        if isinstance(raw, str):
            values[key] = raw
    values.update(
        enabled=form_bool(form, "enabled"),
        evergreen=form_bool(form, "evergreen"),
        only_new=form_bool(form, "only_new"),
        disclose=form_bool(form, "disclose"),
        music_consent=form_bool(form, "music_consent"),
        tags_text=form.get("tags") if isinstance(form.get("tags"), str) else values["tags_text"],
    )
    submitted: dict[str, Any] = dict(values["settings"])
    for key in (
        "allow_comment",
        "allow_duet",
        "allow_stitch",
        "is_aigc",
        "share_to_feed",
        "is_ai_generated",
        "notify_subscribers",
        "contains_synthetic_media",
    ):
        submitted[key] = form_bool(form, key)
    submitted["brand_organic_toggle"] = form_bool(form, "brand_organic")
    submitted["brand_content_toggle"] = form_bool(form, "brand_content")
    for key, field in (
        ("privacy_level", "privacy_level"),
        ("privacy_status", "privacy_status"),
        ("category_id", "category_id"),
        ("mode", "mode"),
    ):
        raw = form.get(field)
        if isinstance(raw, str) and raw:
            submitted[key] = raw
    if form.get("made_for_kids") in ("yes", "no"):
        submitted["self_declared_made_for_kids"] = form.get("made_for_kids") == "yes"
    values["settings"] = submitted
    return values
