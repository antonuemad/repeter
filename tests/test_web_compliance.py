"""Compliance and UX fixes in the dashboard: publishing starts only when the user turns it on,
confirmations before double posts, translated messages, first-run guidance, accessibility."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from repeater.activity import log_activity
from repeater.engine import actions, scheduler
from repeater.engine import rules as engine_rules
from repeater.media import storage
from repeater.models import (
    ActivityLevel,
    ActivityLog,
    ConnectedAccount,
    Job,
    Platform,
    PostStatus,
    RotationRule,
    ScheduledPost,
    Video,
    VideoStatus,
    Workspace,
)
from repeater.platforms import override
from repeater.platforms.base import ConnectedAccountData, TikTokCreatorInfo
from repeater.timeutil import utcnow
from repeater.web import i18n, messages
from tests.test_web_oauth import FakeProvider, _meta_accounts, _start, _tokens
from tests.test_web_support import Recorder, post, set_lang

pytest_plugins = ["tests.test_web_support"]

STATIC = Path(__file__).resolve().parent.parent / "repeater" / "web" / "static"


def _rules(db) -> list[RotationRule]:
    db.expire_all()
    return list(db.execute(select(RotationRule).order_by(RotationRule.id)).scalars())


def _add_post(db, workspace, video, account, *, status=PostStatus.SCHEDULED, **fields) -> ScheduledPost:
    row = ScheduledPost(
        workspace_id=workspace.id,
        video_id=video.id,
        connected_account_id=account.id,
        platform=account.platform,
        idempotency_key=f"{video.id}:{account.id}:{fields.get('cycle', 0)}",
        scheduled_at=fields.pop("scheduled_at", utcnow() + timedelta(hours=3)),
        status=status,
        **fields,
    )
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------------------- ux-compliance-1: nothing starts by itself


def test_meta_login_creates_disabled_rules_and_nothing_is_planned(web_owner, db, workspace, web_make_video):
    web_make_video()
    fake = FakeProvider("meta", uses_pkce=False, accounts=_meta_accounts())
    with override("oauth", "meta", fake):
        _, state = _start(web_owner, "meta")
        resp = web_owner.get(f"/oauth/meta/callback?code=c&state={state}", follow_redirects=False)
    # Two new accounts: the rules list, where each is reviewed and turned on on its own.
    assert resp.status_code == 303 and resp.headers["location"] == "/rules"
    assert [r.enabled for r in _rules(db)] == [False, False]
    now = datetime(2026, 10, 14, 10, 30, tzinfo=UTC)
    assert scheduler.plan_workspace(db, workspace.id, now=now) == []
    set_lang(web_owner, "en")
    html = web_owner.get("/rules").text
    assert "Publishing is off for Magic Page, magic.ig until you review the settings" in html
    assert html.count("Review and turn on") == 2


def test_youtube_login_creates_a_disabled_rule_without_an_audience_answer(web_owner, db):
    fake = FakeProvider("google", uses_pkce=True)
    fake.accounts = [
        ConnectedAccountData(
            platform=Platform.YOUTUBE,
            external_id="UC123",
            display_name="Magic Channel",
            tokens=_tokens(),
            extra={"channel_id": "UC123"},
        )
    ]
    with override("oauth", "google", fake):
        _, state = _start(web_owner, "youtube")
        resp = web_owner.get(f"/oauth/google/callback?code=c&state={state}", follow_redirects=False)
    account = db.execute(select(ConnectedAccount)).scalar_one()
    assert resp.headers["location"] == f"/rules/{account.id}"
    (rule,) = _rules(db)
    assert rule.enabled is False and rule.settings["self_declared_made_for_kids"] is None
    assert engine_rules.rule_problems(rule) == ["youtube_audience_required"]
    set_lang(web_owner, "en")
    html = web_owner.get(f"/rules/{account.id}").text
    # Neither answer is pre-selected.
    assert '<input type="radio" name="made_for_kids" value="yes" required>' in html
    assert '<input type="radio" name="made_for_kids" value="no" required>' in html
    assert "Say whether these videos are made for kids." in html
    assert "YouTube requires an answer and there is no default" in html
    assert "Publishing to this account is off" in html


def test_youtube_rule_needs_an_explicit_audience_answer_to_be_saved(web_owner, db, make_account):
    account = make_account("youtube", display_name="Channel")
    form = {
        "daily_cap": "2",
        "min_gap_minutes": "240",
        "window_start": "10:00",
        "window_end": "22:00",
        "caption_template": "{caption}",
        "privacy_status": "public",
        "category_id": "22",
        "enabled": "1",
        "evergreen_days": "90",
    }
    page = f"/rules/{account.id}"
    resp = post(web_owner, page, form, page=page)
    assert resp.status_code == 400 and _rules(db)[0].enabled is False
    assert post(web_owner, page, {**form, "made_for_kids": "no"}, page=page).status_code == 303
    (rule,) = _rules(db)
    assert rule.enabled is True and rule.settings["self_declared_made_for_kids"] is False
    assert engine_rules.rule_problems(rule) == []
    set_lang(web_owner, "en")
    html = web_owner.get(page).text
    assert '<input type="radio" name="made_for_kids" value="no" required checked>' in html
    assert '<input type="radio" name="made_for_kids" value="yes" required>' in html


def test_a_reconnected_enabled_rule_with_problems_is_sent_to_its_settings(web_owner, db, make_account):
    account = make_account("youtube", external_id="UC9", display_name="Chan")
    rule = engine_rules.ensure_rule(db, account)
    rule.enabled = True  # on, but the audience question was never answered
    db.commit()
    fake = FakeProvider("google", uses_pkce=True)
    fake.accounts = [
        ConnectedAccountData(
            platform=Platform.YOUTUBE, external_id="UC9", display_name="Chan", tokens=_tokens(), extra={}
        )
    ]
    with override("oauth", "google", fake):
        _, state = _start(web_owner, "youtube")
        resp = web_owner.get(f"/oauth/google/callback?code=c&state={state}", follow_redirects=False)
    assert resp.headers["location"] == f"/rules/{account.id}"


# --------------------------------------------------------------------------- engine rules module (C3)


def test_every_new_rule_starts_disabled(db, make_account):
    for platform in ("tiktok", "youtube", "instagram", "facebook"):
        account = make_account(platform)
        rule = engine_rules.ensure_rule(db, account)
        assert rule is not None and rule.enabled is False, platform
    assert engine_rules.ensure_rule(db, make_account("gdrive")) is None
    db.commit()
    entries = [a.data.get("msg_key") for a in db.execute(select(ActivityLog)).scalars()]
    assert entries.count("activity.rule_created_tiktok") == 1 and entries.count("activity.rule_created") == 3


def test_problem_codes_are_stable_and_have_messages(db, make_account):
    codes = {
        "tiktok_privacy_required",
        "tiktok_music_consent_required",
        "tiktok_branded_private",
        "youtube_audience_required",
        "youtube_privacy_invalid",
    }
    assert codes <= set(engine_rules.PROBLEM_MESSAGES)
    assert all(engine_rules.PROBLEM_MESSAGES[c] for c in codes)
    for code in codes:
        assert i18n.EN[f"problem.{code}"] and i18n.AR[f"problem.{code}"]
    sp = engine_rules.settings_problems
    assert sp("tiktok", None) == ["tiktok_privacy_required", "tiktok_music_consent_required"]
    assert sp("tiktok", {"privacy_level": "SELF_ONLY", "music_consent_at": "x"}) == []
    assert sp(
        "tiktok", {"privacy_level": "SELF_ONLY", "music_consent_at": "x", "brand_content_toggle": True}
    ) == ["tiktok_branded_private"]
    assert sp("tiktok", {"mode": "inbox"}) == []
    assert sp("youtube", None) == ["youtube_audience_required"]
    assert sp("youtube", {"self_declared_made_for_kids": True}) == []
    assert sp("youtube", {"self_declared_made_for_kids": False, "privacy_status": "secret"}) == [
        "youtube_privacy_invalid"
    ]
    assert sp("instagram", None) == [] and sp("facebook", None) == []
    account = make_account("youtube")
    assert engine_rules.account_problems(db, account) == ["youtube_audience_required"]  # no rule yet
    rule = engine_rules.ensure_rule(db, account)
    rule.settings = {**rule.settings, "self_declared_made_for_kids": False}
    assert engine_rules.account_problems(db, account) == [] and engine_rules.rule_problems(rule) == []


# --------------------------------------------------------------------------- engine-6: re-plan on edits


def _workspace_form(**overrides) -> dict:
    form = {"timezone": "Africa/Cairo", "cross_gap_hours": "24", "horizon_hours": "48"}
    form.update(overrides)
    return form


@pytest.mark.parametrize(
    "change",
    [
        {"timezone": "Europe/Berlin"},
        {"cross_gap_hours": "6"},
        {"horizon_hours": "72"},
        {"paused": "1"},
    ],
)
def test_workspace_edits_invalidate_the_plan(web_owner, db, workspace, web_plan_calls, change):
    resp = post(web_owner, "/settings/workspace", _workspace_form(**change), page="/rules")
    assert resp.status_code == 303
    (args, kwargs), *rest = web_plan_calls.calls
    assert rest == [] and args[1] == workspace.id
    assert kwargs == {"account_id": None, "actor": "admin@magic-lap.com"}


def test_saving_unchanged_workspace_settings_only_enqueues_planning(web_owner, db, workspace, web_plan_calls):
    assert post(web_owner, "/settings/workspace", _workspace_form(), page="/rules").status_code == 303
    assert web_plan_calls.calls == []
    assert db.execute(select(Job).where(Job.kind == "plan_workspace")).scalars().first() is not None


def test_resume_is_logged_and_replans(web_owner, db, workspace, web_plan_calls):
    post(web_owner, "/settings/workspace", _workspace_form(paused="1"), page="/rules")
    post(web_owner, "/settings/workspace", _workspace_form(), page="/rules")
    assert len(web_plan_calls.calls) == 2
    keys = [a.data.get("msg_key") for a in db.execute(select(ActivityLog).order_by(ActivityLog.id)).scalars()]
    assert "activity.paused" in keys and "activity.resumed" in keys


def _rule_form(**overrides) -> dict:
    form = {
        "daily_cap": "2",
        "min_gap_minutes": "240",
        "window_start": "10:00",
        "window_end": "22:00",
        "caption_template": "{caption}",
        "hashtags": "",
        "evergreen_days": "90",
        "mode": "direct",
    }
    form.update(overrides)
    return form


@pytest.mark.parametrize(
    "change",
    [
        {"daily_cap": "1"},
        {"min_gap_minutes": "60"},
        {"window_start": "19:00"},
        {"window_end": "23:00"},
        {"evergreen": "1"},
        {"only_new": "1"},
        {"enabled": "1"},
    ],
)
def test_rule_scheduling_edits_invalidate_the_accounts_plan(
    web_owner, db, make_account, web_make_rule, web_plan_calls, change
):
    account = make_account("instagram", display_name="magic.ig")
    web_make_rule(account, enabled=False)
    rule = _rules(db)[0]
    rule.daily_cap, rule.min_gap_minutes = 2, 240
    from datetime import time

    rule.window_start, rule.window_end = time(10, 0), time(22, 0)
    db.commit()
    page = f"/rules/{account.id}"
    resp = post(web_owner, page, _rule_form(**change), page=page)
    assert resp.status_code == 303
    ((args, kwargs),) = web_plan_calls.calls
    assert args[1] == account.workspace_id
    assert kwargs == {"account_id": account.id, "actor": "admin@magic-lap.com"}


def test_caption_only_edit_does_not_invalidate_the_plan(
    web_owner, db, make_account, web_make_rule, web_plan_calls
):
    account = make_account("facebook")
    web_make_rule(account)
    rule = _rules(db)[0]
    from datetime import time

    rule.daily_cap, rule.min_gap_minutes = 2, 240
    rule.window_start, rule.window_end = time(10, 0), time(22, 0)
    db.commit()
    page = f"/rules/{account.id}"
    resp = post(web_owner, page, _rule_form(caption_template="New {caption}", hashtags="#x"), page=page)
    assert resp.status_code == 303 and web_plan_calls.calls == []
    assert _rules(db)[0].caption_template == "New {caption}"


# --------------------------------------------------------------------------- ux-compliance-2: double posts


def test_publish_now_asks_before_uploading(web_owner, db, workspace, make_account, web_make_video):
    account = make_account("youtube", display_name="Channel")
    video = web_make_video()
    scheduled = _add_post(db, workspace, video, account)
    set_lang(web_owner, "en")
    html = web_owner.get("/schedule").text
    form = re.search(rf'<form method="post" action="/schedule/{scheduled.id}/publish-now"[^>]*>', html).group(
        0
    )
    assert 'data-confirm="Publish this post now?"' in form


def test_needs_attention_post_asks_with_a_stronger_warning(
    web_owner, db, workspace, make_account, web_make_video
):
    account = make_account("youtube", display_name="Channel")
    video = web_make_video()
    row = _add_post(
        db, workspace, video, account, status=PostStatus.NEEDS_ATTENTION, error="Check the account"
    )
    set_lang(web_owner, "en")
    html = web_owner.get("/schedule").text
    form = re.search(rf'<form method="post" action="/schedule/{row.id}/publish-now"[^>]*>', html).group(0)
    assert "may already be on the platform" in form


def test_post_that_may_be_live_needs_an_explicit_confirmation(
    web_owner, db, workspace, make_account, web_make_video, monkeypatch
):
    recorder = Recorder()
    monkeypatch.setattr(actions, "publish_now", recorder)
    account = make_account("youtube", display_name="Channel")
    video = web_make_video()
    row = _add_post(
        db,
        workspace,
        video,
        account,
        status=PostStatus.NEEDS_ATTENTION,
        platform_post_id="yt-abc123",
        error="did not confirm the post within 2 hours",
    )
    set_lang(web_owner, "en")
    schedule_html = web_owner.get("/schedule").text
    # No one-click form: a link to a page that explains the risk.
    assert f'action="/schedule/{row.id}/publish-now"' not in schedule_html
    assert f'href="/schedule/{row.id}/publish-now?next=/schedule"' in schedule_html
    page = web_owner.get(f"/schedule/{row.id}/publish-now?next=/schedule")
    assert page.status_code == 200
    assert "may already be live on the platform" in page.text
    assert 'name="confirm_duplicate" value="1"' in page.text
    assert f'action="/schedule/{row.id}/skip"' in page.text  # "it is already live: skip"
    # Posting without the confirmation changes nothing and queues nothing.
    resp = post(web_owner, f"/schedule/{row.id}/publish-now", {"next": "/schedule"}, page="/schedule")
    assert resp.status_code == 303 and recorder.calls == []
    db.expire_all()
    assert db.get(ScheduledPost, row.id).status == PostStatus.NEEDS_ATTENTION
    assert db.execute(select(Job).where(Job.kind == "publish_post")).scalars().first() is None
    assert "may already be live" in web_owner.get("/schedule").text
    # With the confirmation the engine action runs.
    resp = post(
        web_owner,
        f"/schedule/{row.id}/publish-now",
        {"next": "/schedule", "confirm_duplicate": "1"},
        page="/schedule",
    )
    assert resp.status_code == 303 and len(recorder.calls) == 1
    # The confirmation page for a post that is no longer actionable just goes back.
    row.status = PostStatus.PUBLISHED
    db.commit()
    assert web_owner.get(f"/schedule/{row.id}/publish-now", follow_redirects=False).status_code == 303


def test_publish_state_alone_also_requires_confirmation(
    web_owner, db, workspace, make_account, web_make_video, monkeypatch
):
    recorder = Recorder()
    monkeypatch.setattr(actions, "publish_now", recorder)
    account = make_account("tiktok")
    video = web_make_video()
    row = _add_post(
        db, workspace, video, account, status=PostStatus.NEEDS_ATTENTION, publish_state={"publish_id": "p1"}
    )
    post(web_owner, f"/schedule/{row.id}/publish-now", page="/schedule")
    assert recorder.calls == []


def test_library_publish_now_asks_before_a_second_copy(
    web_owner, db, workspace, make_account, web_make_video, web_make_rule, monkeypatch
):
    recorder = Recorder()
    monkeypatch.setattr(actions, "schedule_video_now", recorder)
    account = make_account("youtube", display_name="Channel")
    web_make_rule(account)
    video = web_make_video()
    _add_post(db, workspace, video, account, status=PostStatus.PUBLISHED, published_at=utcnow())
    set_lang(web_owner, "en")
    assert 'data-confirm="Publish this video to the chosen account now?"' in web_owner.get("/library").text
    data = {"account_id": str(account.id), "next": "/library"}
    resp = post(web_owner, f"/library/{video.id}/publish-now", data, page="/library")
    assert resp.status_code == 200 and recorder.calls == []
    assert "already published to this account" in resp.text
    assert 'name="confirm_republish" value="1"' in resp.text
    resp = post(
        web_owner, f"/library/{video.id}/publish-now", {**data, "confirm_republish": "1"}, page="/library"
    )
    assert resp.status_code == 303 and len(recorder.calls) == 1


def test_library_publish_now_warns_when_the_last_attempt_may_be_live(
    web_owner, db, workspace, make_account, web_make_video, web_make_rule, monkeypatch
):
    recorder = Recorder()
    monkeypatch.setattr(actions, "schedule_video_now", recorder)
    account = make_account("youtube")
    web_make_rule(account)
    video = web_make_video()
    _add_post(
        db, workspace, video, account, status=PostStatus.NEEDS_ATTENTION, publish_state={"session": "x"}
    )
    set_lang(web_owner, "en")
    resp = post(
        web_owner, f"/library/{video.id}/publish-now", {"account_id": str(account.id)}, page="/library"
    )
    assert resp.status_code == 200 and "may already be live there" in resp.text and recorder.calls == []


# --------------------------------------------------------------------------- ux-compliance-9: incomplete settings


def test_publish_now_is_blocked_for_accounts_with_incomplete_settings(
    web_owner, db, workspace, make_account, web_make_video, web_make_rule, monkeypatch
):
    recorder = Recorder()
    monkeypatch.setattr(actions, "schedule_video_now", recorder)
    tiktok = make_account("tiktok", display_name="Fresh TikTok")
    ready = make_account("youtube", display_name="Ready channel")
    web_make_rule(ready)
    video = web_make_video()
    set_lang(web_owner, "en")
    library_html = web_owner.get("/library").text
    assert re.search(
        r'<option value="\d+" disabled>TikTok · Fresh TikTok \(complete settings first\)', library_html
    )
    assert re.search(rf'<option value="{ready.id}">YouTube · Ready channel</option>', library_html)
    detail = web_owner.get(f"/library/{video.id}").text
    assert f'href="/rules/{tiktok.id}"' in detail and "complete settings first" in detail
    resp = post(
        web_owner, f"/library/{video.id}/publish-now", {"account_id": str(tiktok.id)}, page="/library"
    )
    assert resp.status_code == 303 and recorder.calls == []
    assert "Fresh TikTok is not ready to publish" in web_owner.get(f"/library/{video.id}").text
    assert db.execute(select(ScheduledPost)).first() is None
    post(web_owner, f"/library/{video.id}/publish-now", {"account_id": str(ready.id)}, page="/library")
    assert len(recorder.calls) == 1


# --------------------------------------------------------------------------- ux-compliance-8: translated messages


def _tiktok_creator(monkeypatch) -> None:
    import repeater.platforms.tiktok as tiktok

    monkeypatch.setattr(
        tiktok,
        "creator_info",
        lambda ctx: TikTokCreatorInfo(
            nickname="Magic Lap",
            avatar_url="https://p16.tiktokcdn.com/a.jpg",
            privacy_level_options=["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
            comment_disabled=False,
            duet_disabled=False,
            stitch_disabled=False,
            max_video_post_duration_sec=300,
        ),
    )


def test_setup_problems_of_a_new_tiktok_rule_are_translated(web_owner, make_account, monkeypatch):
    _tiktok_creator(monkeypatch)
    account = make_account("tiktok", display_name="Magic TikTok")
    html = web_owner.get(f"/rules/{account.id}").text  # Arabic by default
    assert "اختر من يمكنه مشاهدة المنشورات (مستوى الخصوصية)." in html
    assert "وافق على تأكيد استخدام الموسيقى من تيك توك." in html
    assert "choose who can view" not in html and "accept TikTok's Music Usage" not in html
    set_lang(web_owner, "en")
    html = web_owner.get(f"/rules/{account.id}").text
    assert "Choose who can view the posts (privacy level)." in html
    assert "Accept TikTok&#39;s Music Usage Confirmation." in html


def test_translator_problem_falls_back_to_the_engine_text():
    translator = i18n.Translator("ar")
    assert translator.problem("youtube_audience_required") == i18n.AR["problem.youtube_audience_required"]
    assert translator.problem("brand_new_code") == "brand_new_code"
    engine_rules.PROBLEM_MESSAGES["zz_test_only"] = "do the thing"
    try:
        assert translator.problem("zz_test_only") == "do the thing"
    finally:
        del engine_rules.PROBLEM_MESSAGES["zz_test_only"]


@pytest.mark.parametrize(
    ("english", "key"),
    [
        ("This post is uploading and can no longer be skipped.", "err.post_not_skippable"),
        ("This post is uploading and cannot be published again from here.", "err.post_not_publishable"),
        ("Publishing is paused for this workspace; resume it first.", "err.paused"),
        ("The account is disconnected; connect it again first.", "err.account_disconnected"),
        ("The TikTok account 'Magic' must be reconnected first.", "err.account_reconnect"),
        ("The video and the account belong to different workspaces.", "err.workspace_mismatch"),
        ("Google Drive accounts cannot be published to.", "err.not_publishable"),
        ("Only videos that are ready (imported and encoded) can be published.", "err.video_not_ready"),
        (
            "This video came from this account; publishing it there again would duplicate it.",
            "err.same_origin",
        ),
        ("YouTube cannot take this video: it is longer than 3 minutes.", "err.platform_limit"),
        ("This video is being published to this account right now.", "err.in_flight"),
        ("Only archived videos can be restored.", "err.not_archived"),
        (
            "This video is a duplicate of another video in the library and stays out of rotation.",
            "err.duplicate_video",
        ),
        ("The video's files are no longer on the server; upload it again.", "err.files_gone"),
        ("Only failed videos can be retried.", "err.not_failed"),
        ("The original file is gone; upload the video again.", "err.original_gone"),
        ("The upload was not found. Please upload the file again.", "err.upload_not_found"),
        ("The upload is empty or incomplete. Please upload the file again.", "err.upload_empty"),
    ],
)
def test_known_engine_refusals_have_translations(english, key):
    known = messages.known_error(ValueError(english))
    assert known is not None and known[0] == key
    assert i18n.EN[key] and i18n.AR[key]
    rendered = i18n.message("en", known[0], known[1])
    assert "{" not in rendered
    if key in ("err.platform_limit", "err.account_reconnect", "err.not_publishable"):
        assert known[1]  # the named parts became parameters
    elif key in ("err.post_not_skippable", "err.post_not_publishable"):
        assert rendered == english.replace(" is uploading and", "")  # the status word is dropped
    else:
        assert rendered == english
    assert "{" not in i18n.message("ar", known[0], known[1])


def test_user_error_with_a_key_is_translated_and_unknown_errors_are_not():
    class UserError(ValueError):
        def __init__(self, key, **params):
            super().__init__(key)
            self.key, self.params = key, params

    assert messages.known_error(UserError("err.paused")) == ("err.paused", {})
    assert messages.known_error(UserError("no.such.key")) is None  # shown as its text instead
    assert messages.known_error(ValueError("something new")) is None


def test_action_refusal_is_flashed_in_arabic(
    web_owner, db, workspace, make_account, web_make_video, monkeypatch
):
    account = make_account("tiktok", display_name="TT")
    video = web_make_video()
    row = _add_post(db, workspace, video, account)
    monkeypatch.setattr(
        actions,
        "publish_now",
        Recorder(error=ValueError("Publishing is paused for this workspace; resume it first.")),
    )
    post(web_owner, f"/schedule/{row.id}/publish-now", {"confirm_duplicate": "1"}, page="/schedule")
    assert "النشر متوقف مؤقتًا في مساحة العمل هذه" in web_owner.get("/schedule").text


def test_activity_entries_written_by_the_dashboard_are_translated(web_owner, db, workspace):
    log_activity(db, workspace.id, ActivityLevel.INFO, "publish", "Imported 3 videos from somewhere")
    db.commit()
    form = _workspace_form(paused="1")
    post(web_owner, "/settings/workspace", form, page="/rules")
    ar = web_owner.get("/activity").text
    assert "تسجيل الدخول إلى اللوحة" in ar and "إيقاف كل النشر مؤقتًا." in ar
    assert "Signed in to the dashboard" not in ar
    assert "Imported 3 videos from somewhere" in ar  # other modules' entries are shown as they are
    set_lang(web_owner, "en")
    en = web_owner.get("/activity").text
    assert "Signed in to the dashboard" in en and "Paused all publishing." in en
    assert "تسجيل الدخول إلى اللوحة" not in en
    assert "Accepted the Terms of Service and the Privacy Policy" in en


def test_activity_entry_with_unknown_key_shows_its_stored_text():
    entry = ActivityLog(message="Stored English text", data={"msg_key": "no.such.key"})
    assert i18n.Translator("ar").activity(entry) == "Stored English text"
    entry = ActivityLog(
        message="x",
        data={
            "msg_key": "activity.source_added",
            "msg_params": {"name": "N", "kind": "drive", "state": "on"},
        },
    )
    assert i18n.Translator("en").activity(entry) == "Added source 'N' (Google Drive folder, on)."
    assert "مجلد Google Drive" in i18n.Translator("ar").activity(entry)


def test_platform_errors_are_shown_as_received_under_a_translated_label(
    web_owner, db, workspace, make_account, web_make_video
):
    account = make_account("instagram", display_name="magic.ig")
    video = web_make_video()
    _add_post(
        db, workspace, video, account, status=PostStatus.NEEDS_ATTENTION, error="Instagram rejected the video"
    )
    html = web_owner.get("/schedule").text
    assert "السبب:" in html and "Instagram rejected the video" in html


# --------------------------------------------------------------------------- ux-compliance-11: first run


def test_first_run_overview_has_a_checklist(web_owner):
    set_lang(web_owner, "en")
    html = web_owner.get("/").text
    card = html.split('class="card setup"', 1)[1].split("</section>", 1)[0]
    for href in ("/accounts", "/library", "/rules"):
        assert f'href="{href}"' in card, href
    assert "Nothing is published until you turn it on yourself" in card
    assert card.count("To do") == 3 and "Done" not in card
    set_lang(web_owner, "ar")
    assert "ابدأ من هنا" in web_owner.get("/").text


def test_checklist_shows_progress_and_disappears_when_everything_is_done(
    web_owner, db, make_account, web_make_video, web_make_rule
):
    set_lang(web_owner, "en")
    account = make_account("youtube", display_name="Channel")
    web_make_rule(account, enabled=False)
    web_make_video()
    card = web_owner.get("/").text.split('class="card setup"', 1)[1].split("</section>", 1)[0]
    assert card.count("Done") == 2 and card.count("To do") == 1
    web_make_rule(account, enabled=True)
    assert 'class="card setup"' not in web_owner.get("/").text


# --------------------------------------------------------------------------- ux-compliance-13: RTL arrows


def test_back_links_and_flow_arrows_follow_the_text_direction(
    web_owner, db, workspace, make_account, web_make_video, web_make_rule
):
    account = make_account("instagram", display_name="magic.ig")
    web_make_rule(account)
    video = web_make_video()
    _add_post(db, workspace, video, account, status=PostStatus.NEEDS_ATTENTION, error="x")
    for path, ar_link, en_link in (
        (f"/rules/{account.id}", '<a href="/rules">→ القواعد</a>', '<a href="/rules">← Rules</a>'),
        (f"/library/{video.id}", '<a href="/library">→ المكتبة</a>', '<a href="/library">← Library</a>'),
        ("/sources/new?kind=ytdlp", '<a href="/sources">→ المصادر</a>', '<a href="/sources">← Sources</a>'),
    ):
        set_lang(web_owner, "ar")
        assert ar_link in web_owner.get(path).text, path
        set_lang(web_owner, "en")
        assert en_link in web_owner.get(path).text, path
    for lang, arrow in (("ar", "←"), ("en", "→")):
        set_lang(web_owner, lang)
        card = web_owner.get("/").text.split('class="card attention"', 1)[1].split("</section>", 1)[0]
        line = re.search(r"<li>(?:(?!</li>).)*magic\.ig(?:(?!</li>).)*</li>", card, re.S).group(0)
        assert arrow in line, lang


# --------------------------------------------------------------------------- ux-compliance-14: TikTok hover


def test_tiktok_form_carries_the_disabled_button_message(web_owner, make_account, monkeypatch):
    _tiktok_creator(monkeypatch)
    account = make_account("tiktok")
    set_lang(web_owner, "en")
    html = web_owner.get(f"/rules/{account.id}").text
    assert (
        'data-disclose-message="You need to indicate if your content promotes yourself, a third party, or both."'
        in html
    )
    assert (
        '<span class="submit-wrap" data-submit-wrap><button type="submit" class="button" data-submit>' in html
    )
    assert 'id="disclose-help"' in html
    js = (STATIC / "app.js").read_text("utf-8")
    assert 'submitWrap.setAttribute("title", discloseMessage)' in js
    assert 'submit.setAttribute("aria-describedby", "disclose-help")' in js
    assert 'submitWrap.removeAttribute("title")' in js


# --------------------------------------------------------------------------- ux-compliance-missed-5: thumbnails


def test_thumbnail_link_is_not_a_focus_stop_or_an_unnamed_link(web_owner, web_make_video):
    web_make_video(title="Coin trick")
    html = web_owner.get("/library").text
    thumbs = re.findall(r'<a [^>]*class="thumb"[^>]*>', html)
    assert thumbs and all('aria-hidden="true"' in t and 'tabindex="-1"' in t for t in thumbs)
    assert 'class="video-title"' in html  # the title link is the named way in


# --------------------------------------------------------------------------- ux-compliance-12: contrast


def _luminance(color: str) -> float:
    color = color.lstrip("#")
    channels = [int(color[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _themes() -> dict[str, dict[str, str]]:
    css = (STATIC / "app.css").read_text("utf-8")
    light_block = css.split(":root {", 1)[1].split("}", 1)[0]
    dark_block = css.split("@media (prefers-color-scheme: dark) {", 1)[1].split("}", 1)[0]
    light = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6});", light_block))
    dark = {**light, **dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6});", dark_block))}
    return {"light": light, "dark": dark}


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_colours_reach_wcag_aa(theme):
    colors = _themes()[theme]
    problems = []
    for token in ("danger-text", "primary-text"):
        background = "danger" if token == "danger-text" else "primary"
        if _ratio(colors[token], colors[background]) < 4.5:
            problems.append((token, background))
    for token in ("text", "muted", "primary", "danger", "success", "warning", "info"):
        for background in ("bg", "surface"):
            if _ratio(colors[token], colors[background]) < 4.5:
                problems.append((token, background, round(_ratio(colors[token], colors[background]), 2)))
    for platform in ("tiktok", "youtube", "instagram", "facebook", "gdrive"):
        for background in ("bg", "surface"):
            if _ratio(colors[f"pf-{platform}"], colors[background]) < 4.5:
                problems.append((platform, background))
    assert not problems, problems


def test_danger_button_uses_the_per_theme_text_colour():
    css = (STATIC / "app.css").read_text("utf-8")
    assert "color: var(--danger-text)" in css and "color: #fff; }" not in css
    for platform in ("tiktok", "youtube", "instagram", "facebook", "gdrive"):
        assert f".platform-{platform} {{ color: var(--pf-{platform}); }}" in css


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("surface,alpha", [("banner-warning", 0.18), ("flash-warning", 0.12)])
def test_warning_surface_links_reach_wcag_aa(theme, surface, alpha):
    css = (STATIC / "app.css").read_text()
    colors = _themes()[theme]

    def rgb(color):
        return [int(color[i : i + 2], 16) for i in (1, 3, 5)]

    mixed = "#" + "".join(
        f"{round(a * alpha + b * (1 - alpha)):02x}"
        for a, b in zip(rgb(colors["warning"]), rgb(colors["surface"]), strict=True)
    )
    override = re.search(r"\." + surface + r" a\s*\{[^}]*color:\s*var\(--([\w-]+)\)", css)
    token = override.group(1) if override else "primary"
    assert _ratio(colors[token], mixed) >= 4.5


# --------------------------------------------------------------------------- ux-compliance-missed-4: delete a video


def test_owner_deletes_a_video_with_its_files_posts_and_log_entries(
    web_owner, db, workspace, make_account, web_make_video
):
    account = make_account("instagram", display_name="magic.ig")
    video = web_make_video(title="Coin trick")
    other = web_make_video(title="Keep me")
    _add_post(
        db,
        workspace,
        video,
        account,
        status=PostStatus.PUBLISHED,
        published_at=utcnow(),
        permalink="https://www.instagram.com/reel/ABC123/",
    )
    log_activity(
        db,
        workspace.id,
        ActivityLevel.INFO,
        "publish",
        "Published 'Coin trick' to Instagram",
        video_id=video.id,
        permalink="https://www.instagram.com/reel/ABC123/",
    )
    log_activity(db, workspace.id, ActivityLevel.INFO, "import", "Imported 'Keep me'", video_id=other.id)
    db.commit()
    video_id, other_id = video.id, other.id
    folder = storage.media_root() / storage.video_dir_rel(workspace.id, video_id)
    assert folder.exists()
    set_lang(web_owner, "en")
    detail = web_owner.get(f"/library/{video_id}").text
    assert f'action="/library/{video_id}/delete"' in detail and "Delete this video for good?" in detail
    resp = post(web_owner, f"/library/{video_id}/delete", page=f"/library/{video_id}")
    assert resp.status_code == 303 and resp.headers["location"] == "/library"
    db.expire_all()
    assert db.get(Video, video_id) is None and db.get(Video, other_id) is not None
    assert db.execute(select(ScheduledPost).where(ScheduledPost.video_id == video_id)).first() is None
    assert not folder.exists()
    messages_left = [a.message for a in db.execute(select(ActivityLog)).scalars()]
    assert not any("Coin trick" in m for m in messages_left)
    assert any("Keep me" in m for m in messages_left)
    assert not any("instagram.com" in str(a.data) for a in db.execute(select(ActivityLog)).scalars())
    assert any(m == "Deleted a video from the library." for m in messages_left)
    assert "The video was deleted." in web_owner.get("/library").text


def test_delete_is_owner_only_workspace_scoped_and_needs_csrf(
    web_owner, web_editor, db, workspace, web_make_video
):
    video = web_make_video()
    set_lang(web_editor, "en")
    assert "/delete" not in web_editor.get(f"/library/{video.id}").text
    resp = post(web_editor, f"/library/{video.id}/delete", page="/library")
    assert resp.status_code == 403
    assert web_owner.post(f"/library/{video.id}/delete", follow_redirects=False).status_code == 403
    db.expire_all()
    assert db.get(Video, video.id) is not None
    foreign = Workspace(name="Other")
    db.add(foreign)
    db.commit()
    stranger = web_make_video(workspace_id=foreign.id)
    assert post(web_owner, f"/library/{stranger.id}/delete", page="/library").status_code == 404


@pytest.mark.parametrize("busy", ["processing", "uploading"])
def test_a_video_being_processed_or_published_cannot_be_deleted(
    web_owner, db, workspace, make_account, web_make_video, busy
):
    account = make_account("instagram")
    if busy == "processing":
        video = web_make_video(status=VideoStatus.PROCESSING)
    else:
        video = web_make_video()
        _add_post(db, workspace, video, account, status=PostStatus.UPLOADING)
    resp = post(web_owner, f"/library/{video.id}/delete", page="/library")
    assert resp.status_code == 303 and resp.headers["location"] == f"/library/{video.id}"
    db.expire_all()
    assert db.get(Video, video.id) is not None
    set_lang(web_owner, "en")
    assert "is being prepared or published right now" in web_owner.get(f"/library/{video.id}").text


def test_overview_warns_the_owner_when_the_backup_is_stale(web_owner, web_editor, monkeypatch, tmp_path):
    import time

    from repeater.config import get_settings

    marker = tmp_path / "last_success"
    monkeypatch.setattr(get_settings(), "backup_status_file", marker)
    set_lang(web_owner, "en")
    set_lang(web_editor, "en")
    assert "database backup" not in web_owner.get("/").text  # no record yet: a new server
    marker.write_text(f"{int(time.time() - 3 * 3600)} 2026-10-14T03:17:00Z\n")
    assert "database backup" not in web_owner.get("/").text
    marker.write_text(f"{int(time.time() - 50 * 3600)} 2026-10-12T03:17:00Z\n")
    assert "last successful database backup was 50 hours ago" in web_owner.get("/").text
    assert "database backup" not in web_editor.get("/").text  # editors cannot act on it


def test_persisted_domain_errors_and_activity_render_in_arabic_and_english(
    web_owner, db, workspace, make_account, web_make_video
):
    from repeater.messages import msg
    from repeater.models import AccountStatus, Source, SourceKind
    from repeater.platforms.base import AuthError

    detail = AuthError("Stored connection was refused", code="invalid_token").message
    account = make_account("youtube", display_name="Preview channel")
    account.status = AccountStatus.NEEDS_RECONNECT
    account.last_error = detail
    video = web_make_video(status=VideoStatus.FAILED, error=detail)
    _add_post(db, workspace, video, account, status=PostStatus.NEEDS_ATTENTION, error=detail)
    source = Source(
        workspace_id=workspace.id,
        kind=SourceKind.DRIVE,
        name="Preview source",
        enabled=False,
        last_error=detail,
        consecutive_failures=3,
    )
    db.add(source)
    log_activity(
        db,
        workspace.id,
        ActivityLevel.INFO,
        "import",
        msg("import.uploaded", "Uploaded '{title}'; preparing it for publishing.", title="Preview clip"),
    )
    db.commit()
    for path in (
        "/",
        "/schedule",
        "/accounts",
        "/sources",
        f"/sources/{source.id}/edit",
        f"/library/{video.id}",
    ):
        set_lang(web_owner, "ar")
        arabic = web_owner.get(path)
        assert arabic.status_code == 200, path
        assert "Stored connection was refused" not in arabic.text, path
        assert "invalid_token" in arabic.text, path
        set_lang(web_owner, "en")
        english = web_owner.get(path)
        assert "Stored connection was refused" in english.text, path
    set_lang(web_owner, "ar")
    overview = web_owner.get("/").text
    assert "تم رفع" in overview and "preparing it for publishing" not in overview
