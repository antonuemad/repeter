"""Rotation rules: TikTok Content Sharing Guidelines form, YouTube/Instagram/Facebook settings,
general rule fields and workspace settings."""

from __future__ import annotations

import re
from datetime import time

import pytest
from sqlalchemy import select

from repeater.config import get_settings
from repeater.models import Job, RotationRule, Workspace
from repeater.platforms.base import RULE_SETTINGS_DEFAULTS, RetryableError, TikTokCreatorInfo
from tests.test_web_support import post, set_lang

pytest_plugins = ["tests.test_web_support"]


@pytest.mark.parametrize(
    "lang,text",
    [
        ("en", "Testing period: publishing is disabled for every account."),
        ("ar", "فترة الاختبار: النشر ممنوع على كل الحسابات."),
    ],
)
def test_testing_lock_is_visible_without_workspace_resume_link(web_owner, monkeypatch, lang, text):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    set_lang(web_owner, lang)
    response = web_owner.get("/settings/workspace")
    assert response.status_code == 200 and text in response.text
    assert "Resume in settings" not in response.text and "استئناف من الإعدادات" not in response.text


def _info(**overrides) -> TikTokCreatorInfo:
    values = {
        "nickname": "Magic Lap",
        "avatar_url": "https://p16.tiktokcdn.com/a.jpg",
        "privacy_level_options": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
        "comment_disabled": False,
        "duet_disabled": True,
        "stitch_disabled": True,
        "max_video_post_duration_sec": 300,
        "username": "magiclap",
    }
    values.update(overrides)
    return TikTokCreatorInfo(**values)


@pytest.fixture
def creator(monkeypatch):
    """Fake creator_info; set `.error` to make it fail, `.info` to change the answer."""
    import repeater.platforms.tiktok as tiktok

    class Fake:
        def __init__(self) -> None:
            self.info = _info()
            self.error: Exception | None = None
            self.calls = 0

        def __call__(self, ctx):
            self.calls += 1
            if self.error:
                raise self.error
            return self.info

    fake = Fake()
    monkeypatch.setattr(tiktok, "creator_info", fake)
    return fake


@pytest.fixture
def tiktok_account(make_account):
    return make_account(
        "tiktok", display_name="Magic TikTok", extra={"open_id": "o1", "username": "magiclap"}
    )


def _base_form(**extra) -> dict:
    form = {
        "mode": "direct",  # a TikTok form rendered with creator info always submits its mode
        "daily_cap": "2",
        "min_gap_minutes": "240",
        "window_start": "10:00",
        "window_end": "22:00",
        "caption_template": "{caption}",
        "hashtags": "",
        "evergreen_days": "90",
    }
    form.update(extra)
    return form


def _rule(db, account) -> RotationRule:
    db.expire_all()
    return db.execute(
        select(RotationRule).where(RotationRule.connected_account_id == account.id)
    ).scalar_one()


def _save(client, account, form):
    return post(client, f"/rules/{account.id}", form, page=f"/rules/{account.id}")


# --------------------------------------------------------------------------- TikTok form rendering


def test_tiktok_form_follows_content_sharing_guidelines(web_owner, tiktok_account, creator, db):
    set_lang(web_owner, "en")
    resp = web_owner.get(f"/rules/{tiktok_account.id}")
    assert resp.status_code == 200
    html = resp.text
    # Creator info is fetched when rendering; nickname and avatar are shown.
    assert creator.calls == 1
    assert "Magic Lap" in html and "https://p16.tiktokcdn.com/a.jpg" in html
    # The privacy dropdown lists only the creator's options, with no option preselected.
    select_html = re.search(r'<select name="privacy_level".*?</select>', html, re.S).group(0)
    values = re.findall(r'<option value="([^"]*)"', select_html)
    assert values == ["", "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"]
    assert "FOLLOWER_OF_CREATOR" not in select_html
    assert re.search(r'<option value="[A-Z_]+"[^>]*\bselected\b', select_html) is None
    assert re.search(r'<option value="" disabled selected>', select_html)
    # Interaction toggles start unchecked; the ones the creator disabled are disabled.
    assert re.search(r'<input type="checkbox" name="allow_comment" value="1">', html)
    assert re.search(r'<input type="checkbox" name="allow_duet" value="1" disabled>', html)
    assert re.search(r'<input type="checkbox" name="allow_stitch" value="1" disabled>', html)
    # Commercial content disclosure, off by default, with both options.
    assert re.search(r'<input type="checkbox" name="disclose" value="1" data-disclose>', html)
    assert "Your brand" in html and "Branded content" in html
    assert "Promotional content" in html and "Paid partnership" in html
    assert "Branded content visibility cannot be set to private." in html
    # Consent declaration as a required checkbox, and the processing note.
    assert "By posting, you agree to TikTok's" in html and "Music Usage Confirmation" in html
    assert "https://www.tiktok.com/legal/page/global/music-usage-confirmation/en" in html
    assert "https://www.tiktok.com/legal/page/global/bc-policy/en" in html
    assert re.search(r'name="music_consent" value="1" required', html)
    assert "may take a few minutes" in html
    assert "300 s" in html


@pytest.fixture
def unaudited(monkeypatch):
    from repeater.config import get_settings

    monkeypatch.setattr(get_settings(), "tiktok_audited", False)


def test_unaudited_app_offers_only_only_me_and_warns_about_a_public_account(
    web_owner, tiktok_account, creator, unaudited
):
    set_lang(web_owner, "en")
    html = web_owner.get(f"/rules/{tiktok_account.id}").text
    select_html = re.search(r'<select name="privacy_level".*?</select>', html, re.S).group(0)
    assert re.findall(r'<option value="([^"]*)"', select_html) == ["", "SELF_ONLY"]
    assert "has not audited this app yet" in html
    assert "This TikTok account is public" in html  # creator_info offers PUBLIC_TO_EVERYONE
    # A private account is not offered PUBLIC_TO_EVERYONE: no warning about it.
    creator.info = _info(privacy_level_options=["FOLLOWER_OF_CREATOR", "SELF_ONLY"])
    html = web_owner.get(f"/rules/{tiktok_account.id}").text
    assert "has not audited this app yet" in html and "This TikTok account is public" not in html


def test_unaudited_app_refuses_any_other_privacy_level(web_owner, tiktok_account, creator, db, unaudited):
    set_lang(web_owner, "en")
    form = _base_form(enabled="1", music_consent="1", privacy_level="PUBLIC_TO_EVERYONE")
    resp = _save(web_owner, tiktok_account, form)
    assert resp.status_code == 400 and "only “Only me” can be used" in resp.text
    assert _rule(db, tiktok_account).enabled is False
    ok = _save(web_owner, tiktok_account, {**form, "privacy_level": "SELF_ONLY"})
    assert ok.status_code == 303
    assert _rule(db, tiktok_account).settings["privacy_level"] == "SELF_ONLY"


def test_audited_app_offers_every_creator_option(web_owner, tiktok_account, creator):
    html = web_owner.get(f"/rules/{tiktok_account.id}").text
    select_html = re.search(r'<select name="privacy_level".*?</select>', html, re.S).group(0)
    assert re.findall(r'<option value="([^"]*)"', select_html)[1:] == [
        "PUBLIC_TO_EVERYONE",
        "MUTUAL_FOLLOW_FRIENDS",
        "SELF_ONLY",
    ]
    assert "has not audited this app yet" not in html


def test_tiktok_form_in_arabic_keeps_english_declaration(web_owner, tiktok_account, creator):
    html = web_owner.get(f"/rules/{tiktok_account.id}").text
    assert "By posting, you agree to TikTok's" in html
    assert "تأكيد استخدام الموسيقى" in html
    assert "من يمكنه مشاهدة هذه الفيديوهات" in html


def test_tiktok_form_preselects_only_users_saved_choice(web_owner, tiktok_account, creator, db):
    rule = _rule(db, tiktok_account) if db.execute(select(RotationRule)).first() else None
    assert rule is None
    web_owner.get(f"/rules/{tiktok_account.id}")  # creates the default rule
    rule = _rule(db, tiktok_account)
    rule.settings = {**rule.settings, "privacy_level": "MUTUAL_FOLLOW_FRIENDS"}
    db.commit()
    html = web_owner.get(f"/rules/{tiktok_account.id}").text
    assert '<option value="MUTUAL_FOLLOW_FRIENDS" selected>' in html
    # A saved choice TikTok no longer offers is not preselected.
    creator.info = _info(privacy_level_options=["PUBLIC_TO_EVERYONE", "SELF_ONLY"])
    html = web_owner.get(f"/rules/{tiktok_account.id}").text
    assert "selected>" not in re.search(r'<select name="privacy_level".*?</select>', html, re.S).group(
        0
    ).replace("disabled selected>", "")


def test_tiktok_creator_info_failure_blocks_enabling(web_owner, tiktok_account, creator, db):
    creator.error = RetryableError("TikTok is temporarily unavailable.")
    set_lang(web_owner, "en")
    html = web_owner.get(f"/rules/{tiktok_account.id}").text
    assert "Could not load your TikTok creator settings" in html
    assert "TikTok is temporarily unavailable." in html
    # No "enabled" box while the rule is off: it cannot be turned on without creator settings, and
    # the form says that its TikTok fields are missing so saving cannot read them as answers.
    assert 'name="enabled"' not in html and 'name="tiktok_fields_absent" value="1"' in html
    assert 'name="privacy_level"' not in html
    resp = _save(web_owner, tiktok_account, _base_form(enabled="1"))
    assert resp.status_code == 400
    assert "cannot be turned on" in resp.text
    assert _rule(db, tiktok_account).enabled is False
    # A crafted "enabled" without the TikTok fields cannot switch it on either.
    crafted = _base_form(enabled="1", tiktok_fields_absent="1")
    crafted.pop("mode")
    assert _save(web_owner, tiktok_account, crafted).status_code == 303
    assert _rule(db, tiktok_account).enabled is False


def test_tiktok_creator_info_failure_still_allows_turning_off(web_owner, tiktok_account, creator, db):
    web_owner.get(f"/rules/{tiktok_account.id}")
    rule = _rule(db, tiktok_account)
    rule.enabled = True
    rule.settings = {**rule.settings, "privacy_level": "SELF_ONLY"}
    db.commit()
    creator.error = RetryableError("down")
    form = _base_form(daily_cap="3", tiktok_fields_absent="1")
    form.pop("mode")
    resp = _save(web_owner, tiktok_account, form)  # the "enabled" box was unchecked
    assert resp.status_code == 303
    rule = _rule(db, tiktok_account)
    assert rule.enabled is False and rule.daily_cap == 3
    assert rule.settings["privacy_level"] == "SELF_ONLY"  # untouched


@pytest.mark.parametrize("creator_ok_on_save", [False, True])
def test_tiktok_save_without_creator_fields_keeps_publishing_and_settings(
    web_owner, tiktok_account, creator, db, creator_ok_on_save
):
    """ux-compliance-7: saving the hashtags while TikTok's creator settings failed to load must not
    switch a running rule off or erase the approved privacy, interactions and music consent."""
    web_owner.get(f"/rules/{tiktok_account.id}")
    rule = _rule(db, tiktok_account)
    approved = {
        **rule.settings,
        "privacy_level": "MUTUAL_FOLLOW_FRIENDS",
        "allow_comment": True,
        "music_consent_at": "2026-10-01T10:00:00",
    }
    rule.enabled, rule.settings = True, approved
    db.commit()
    creator.error = RetryableError("TikTok is temporarily unavailable.")
    set_lang(web_owner, "en")
    html = web_owner.get(f"/rules/{tiktok_account.id}").text
    # The running rule stays visibly on and can still be turned off.
    assert re.search(r'name="enabled" value="1" data-rule-enabled checked>', html)
    assert 'name="tiktok_fields_absent" value="1"' in html
    if creator_ok_on_save:
        creator.error = None
    form = _base_form(tiktok_fields_absent="1", enabled="1", hashtags="#new")
    form.pop("mode")
    assert _save(web_owner, tiktok_account, form).status_code == 303
    rule = _rule(db, tiktok_account)
    assert rule.enabled is True and rule.hashtags == "#new"
    assert rule.settings == approved
    page = web_owner.get(f"/rules/{tiktok_account.id}").text
    assert "were left as they were" in page


def test_tiktok_disconnected_account_needs_reconnect(web_owner, tiktok_account, creator, db, monkeypatch):
    from repeater import accounts

    def unavailable(session, account, **kw):
        raise accounts.AccountUnavailable("gone")

    monkeypatch.setattr(accounts, "fresh_context", unavailable)
    set_lang(web_owner, "en")
    html = web_owner.get(f"/rules/{tiktok_account.id}").text
    assert "The TikTok account needs to be reconnected." in html
    assert creator.calls == 0


# --------------------------------------------------------------------------- TikTok form saving


def test_tiktok_save_valid_settings(web_owner, tiktok_account, creator, db):
    form = _base_form(
        enabled="1",
        mode="direct",
        privacy_level="PUBLIC_TO_EVERYONE",
        allow_comment="1",
        allow_duet="1",  # disabled by the creator: must be stored as False
        disclose="1",
        brand_organic="1",
        is_aigc="1",
        music_consent="1",
    )
    resp = _save(web_owner, tiktok_account, form)
    assert resp.status_code == 303 and resp.headers["location"] == f"/rules/{tiktok_account.id}"
    rule = _rule(db, tiktok_account)
    s = rule.settings
    assert rule.enabled is True
    assert s["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert s["allow_comment"] is True and s["allow_duet"] is False and s["allow_stitch"] is False
    assert s["brand_organic_toggle"] is True and s["brand_content_toggle"] is False
    assert s["is_aigc"] is True and s["mode"] == "direct"
    assert s["music_consent_at"]
    assert creator.calls == 2  # fetched again on save (the first call rendered the form)
    assert db.execute(select(Job).where(Job.kind == "plan_workspace")).scalars().first() is not None
    # Saving again keeps the original consent time.
    first_consent = s["music_consent_at"]
    _save(web_owner, tiktok_account, form)
    assert _rule(db, tiktok_account).settings["music_consent_at"] == first_consent


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"privacy_level": "FOLLOWER_OF_CREATOR", "music_consent": "1"}, "Choose one of the privacy options"),
        ({"music_consent": "1"}, "Choose who can view these videos"),
        ({"privacy_level": "PUBLIC_TO_EVERYONE"}, "Accept TikTok's Music Usage Confirmation"),
        (
            {"privacy_level": "SELF_ONLY", "disclose": "1", "brand_content": "1", "music_consent": "1"},
            "Branded content visibility cannot be set to private.",
        ),
        (
            {"privacy_level": "PUBLIC_TO_EVERYONE", "disclose": "1", "music_consent": "1"},
            "You need to indicate if your content promotes yourself, a third party, or both.",
        ),
    ],
)
def test_tiktok_save_rejects_invalid_settings(web_owner, tiktok_account, creator, db, extra, message):
    set_lang(web_owner, "en")
    resp = _save(web_owner, tiktok_account, _base_form(enabled="1", mode="direct", **extra))
    assert resp.status_code == 400
    assert message.replace("'", "&#39;") in resp.text
    rule = _rule(db, tiktok_account)
    assert rule.enabled is False and rule.settings.get("privacy_level") is None


def test_tiktok_branded_content_with_public_privacy(web_owner, tiktok_account, creator, db):
    form = _base_form(
        enabled="1", privacy_level="MUTUAL_FOLLOW_FRIENDS", disclose="1", brand_content="1", music_consent="1"
    )
    assert _save(web_owner, tiktok_account, form).status_code == 303
    s = _rule(db, tiktok_account).settings
    assert s["brand_content_toggle"] is True and s["brand_organic_toggle"] is False
    html = web_owner.get(f"/rules/{tiktok_account.id}").text
    # The branded declaration is the visible one after saving.
    assert re.search(r'<span data-consent-text="branded" lang="en"', html)


def test_tiktok_disabled_rule_can_be_saved_without_consent(web_owner, tiktok_account, creator, db):
    resp = _save(web_owner, tiktok_account, _base_form(mode="direct"))
    assert resp.status_code == 303
    rule = _rule(db, tiktok_account)
    assert rule.enabled is False and rule.settings["music_consent_at"] is None


def test_tiktok_inbox_mode_needs_no_privacy(web_owner, tiktok_account, creator, db):
    resp = _save(web_owner, tiktok_account, _base_form(enabled="1", mode="inbox"))
    assert resp.status_code == 303
    rule = _rule(db, tiktok_account)
    assert rule.enabled is True and rule.settings["mode"] == "inbox"


def test_disclosure_options_ignored_when_toggle_off(web_owner, tiktok_account, creator, db):
    form = _base_form(enabled="1", privacy_level="PUBLIC_TO_EVERYONE", music_consent="1", brand_content="1")
    assert _save(web_owner, tiktok_account, form).status_code == 303
    assert _rule(db, tiktok_account).settings["brand_content_toggle"] is False


# --------------------------------------------------------------------------- general fields


def test_general_fields_saved(web_owner, make_account, db):
    account = make_account("instagram")
    form = _base_form(
        enabled="1",
        daily_cap="4",
        min_gap_minutes="120",
        window_start="21:30",
        window_end="02:00",
        caption_template="{title}\n\n{caption}",
        hashtags="#magic tricks",
        evergreen="1",
        evergreen_days="60",
        only_new="1",
        share_to_feed="1",
    )
    assert _save(web_owner, account, form).status_code == 303
    rule = _rule(db, account)
    assert (rule.daily_cap, rule.min_gap_minutes) == (4, 120)
    assert (rule.window_start, rule.window_end) == (time(21, 30), time(2, 0))
    assert rule.caption_template == "{title}\n\n{caption}" and rule.hashtags == "#magic tricks"
    assert rule.evergreen_days == 60 and rule.only_after is not None
    assert rule.settings == {"share_to_feed": True, "is_ai_generated": False}
    only_after = rule.only_after
    _save(web_owner, account, form)
    assert _rule(db, account).only_after == only_after
    _save(web_owner, account, _base_form(enabled="1"))
    rule = _rule(db, account)
    assert rule.only_after is None and rule.evergreen_days is None and rule.settings["share_to_feed"] is False


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("daily_cap", "0", "Posts per day must be between 1 and 25."),
        ("daily_cap", "26", "Posts per day must be between 1 and 25."),
        ("daily_cap", "two", "Posts per day must be a whole number."),
        ("window_start", "25:99", "Posting window starts must be a time like 09:30."),
        ("min_gap_minutes", "", "Minimum gap between posts (minutes) is required."),
    ],
)
def test_general_field_validation(web_owner, make_account, db, field, value, message):
    account = make_account("facebook")
    set_lang(web_owner, "en")
    resp = _save(web_owner, account, _base_form(enabled="1", **{field: value}))
    assert resp.status_code == 400
    assert message in resp.text
    # What the user typed is shown again.
    assert f'value="{value}"' in resp.text or field == "min_gap_minutes"


def test_youtube_settings(web_owner, make_account, db):
    account = make_account("youtube")
    html = web_owner.get(f"/rules/{account.id}").text
    assert (
        'name="privacy_status"' in html
        and 'name="made_for_kids"' in html
        and "https://www.youtube.com/t/terms" in html
    )
    form = _base_form(
        enabled="1",
        privacy_status="unlisted",
        made_for_kids="no",
        category_id="27",
        notify_subscribers="1",
        contains_synthetic_media="1",
        tags="magic, card tricks, #illusion, magic",
    )
    assert _save(web_owner, account, form).status_code == 303
    s = _rule(db, account).settings
    assert s["privacy_status"] == "unlisted" and s["self_declared_made_for_kids"] is False
    assert (
        s["category_id"] == "27" and s["notify_subscribers"] is True and s["contains_synthetic_media"] is True
    )
    assert s["tags"] == ["magic", "card tricks", "illusion"]


@pytest.mark.parametrize(
    "extra",
    [
        {"privacy_status": "secret", "made_for_kids": "no", "category_id": "22"},
        {"privacy_status": "public", "category_id": "22"},
        {"privacy_status": "public", "made_for_kids": "no", "category_id": "999"},
        {
            "privacy_status": "public",
            "made_for_kids": "no",
            "category_id": "22",
            "tags": ",".join(f"tag{i}" for i in range(120)),
        },
    ],
)
def test_youtube_validation(web_owner, make_account, db, extra):
    account = make_account("youtube")
    resp = _save(web_owner, account, _base_form(enabled="1", **extra))
    assert resp.status_code == 400
    assert _rule(db, account).settings == RULE_SETTINGS_DEFAULTS["youtube"]


def test_facebook_settings(web_owner, make_account, db):
    account = make_account("facebook")
    assert _save(web_owner, account, _base_form(enabled="1", is_ai_generated="1")).status_code == 303
    assert _rule(db, account).settings == {"is_ai_generated": True}


def test_rules_list_and_scoping(web_owner, make_account, db, creator):
    account = make_account("tiktok", display_name="TT listed")
    make_account("gdrive", display_name="Drive not listed")
    set_lang(web_owner, "en")
    html = web_owner.get("/rules").text
    assert "TT listed" in html and "Drive not listed" not in html
    other = Workspace(name="Other")
    db.add(other)
    db.commit()
    foreign = make_account("youtube", workspace_id=other.id)
    assert web_owner.get(f"/rules/{foreign.id}").status_code == 404
    drive = make_account("gdrive")
    assert web_owner.get(f"/rules/{drive.id}").status_code == 404
    assert account.id


# --------------------------------------------------------------------------- workspace settings


def test_workspace_settings(web_owner, db, workspace):
    resp = post(
        web_owner,
        "/settings/workspace",
        {"timezone": "Asia/Riyadh", "cross_gap_hours": "12", "horizon_hours": "72", "paused": "1"},
        page="/rules",
    )
    assert resp.status_code == 303
    db.expire_all()
    ws = db.get(Workspace, workspace.id)
    assert (ws.timezone, ws.cross_platform_gap_minutes, ws.horizon_hours, ws.paused) == (
        "Asia/Riyadh",
        720,
        72,
        True,
    )
    set_lang(web_owner, "en")
    assert "Publishing is paused for this workspace." in web_owner.get("/").text
    assert db.execute(select(Job).where(Job.kind == "plan_workspace")).scalars().first() is not None


@pytest.mark.parametrize(
    "form",
    [
        {"timezone": "Mars/Base", "cross_gap_hours": "12", "horizon_hours": "72"},
        {"timezone": "Africa/Cairo", "cross_gap_hours": "-1", "horizon_hours": "72"},
        {"timezone": "Africa/Cairo", "cross_gap_hours": "12", "horizon_hours": "1000"},
    ],
)
def test_workspace_settings_validation(web_owner, db, workspace, form):
    resp = post(web_owner, "/settings/workspace", form, page="/rules")
    assert resp.status_code == 303 and resp.headers["location"] == "/settings/workspace"
    db.expire_all()
    ws = db.get(Workspace, workspace.id)
    assert ws.timezone == "Africa/Cairo" and ws.horizon_hours == 48
