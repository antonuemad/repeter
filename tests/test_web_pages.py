"""Every dashboard page renders in Arabic and English; translations are complete; legal pages."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest

from repeater.activity import log_activity
from repeater.models import (
    AccountStatus,
    ActivityLevel,
    DataDeletionRequest,
    Platform,
    PostStatus,
    RotationRule,
    ScheduledPost,
    Source,
    SourceKind,
    VideoStatus,
)
from repeater.platforms.base import RULE_SETTINGS_DEFAULTS, TikTokCreatorInfo
from repeater.timeutil import utcnow
from repeater.web import i18n
from tests.test_web_support import set_lang

pytest_plugins = ["tests.test_web_support"]

TEMPLATES = Path(__file__).resolve().parent.parent / "repeater" / "web" / "templates"


@pytest.fixture
def populated(db, workspace, make_account, web_make_video, monkeypatch):
    """A workspace with one of everything, so every page has content to render."""
    tiktok = make_account(
        "tiktok", display_name="Magic TikTok", extra={"open_id": "o1", "username": "magiclap"}
    )
    youtube = make_account("youtube", display_name="Magic YouTube", extra={"channel_id": "UC1"})
    insta = make_account("instagram", display_name="magic.ig", extra={"ig_user_id": "1", "fb_user_id": "9"})
    drive = make_account("gdrive", display_name="drive@magic-lap.com", extra={"email": "drive@magic-lap.com"})
    broken = make_account("facebook", display_name="Magic Page", extra={"page_id": "2", "fb_user_id": "9"})
    broken.status = AccountStatus.NEEDS_RECONNECT
    broken.last_error = "token expired"
    for account in (tiktok, youtube, insta, broken):
        db.add(
            RotationRule(
                workspace_id=workspace.id,
                connected_account_id=account.id,
                platform=account.platform,
                settings=dict(RULE_SETTINGS_DEFAULTS[account.platform.value]),
                enabled=True,
            )
        )
    video = web_make_video(title="Coin trick")
    web_make_video(status=VideoStatus.FAILED, title="Broken file", with_files=False, error="not a video")
    web_make_video(status=VideoStatus.ARCHIVED, title="Old one")
    now = utcnow()
    for i, (account, status) in enumerate(
        [
            (tiktok, PostStatus.SCHEDULED),
            (youtube, PostStatus.PUBLISHED),
            (insta, PostStatus.NEEDS_ATTENTION),
            (broken, PostStatus.FAILED),
        ]
    ):
        db.add(
            ScheduledPost(
                workspace_id=workspace.id,
                video_id=video.id,
                connected_account_id=account.id,
                platform=account.platform,
                idempotency_key=f"{video.id}:{account.id}:0",
                scheduled_at=now + timedelta(hours=2 + i)
                if status != PostStatus.PUBLISHED
                else now - timedelta(days=1),
                published_at=now - timedelta(days=1) if status == PostStatus.PUBLISHED else None,
                permalink="https://youtube.com/shorts/abc" if status == PostStatus.PUBLISHED else None,
                status=status,
                error="Instagram rejected the video" if status == PostStatus.NEEDS_ATTENTION else None,
            )
        )
    db.add_all(
        [
            Source(
                workspace_id=workspace.id,
                kind=SourceKind.PLATFORM_API,
                platform=Platform.INSTAGRAM,
                connected_account_id=insta.id,
                name="Instagram source",
                config={},
                last_error="Instagram is down",
            ),
            Source(
                workspace_id=workspace.id,
                kind=SourceKind.YTDLP,
                platform=Platform.TIKTOK,
                name="TikTok profile",
                config={"profile_url": "https://www.tiktok.com/@magiclap"},
                unofficial_ack_at=now,
                poll_interval_minutes=60,
            ),
            Source(
                workspace_id=workspace.id,
                kind=SourceKind.DRIVE,
                platform=Platform.GDRIVE,
                connected_account_id=drive.id,
                name="Drive",
                config={"folder_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"},
            ),
            Source(workspace_id=workspace.id, kind=SourceKind.MANUAL, name="Manual uploads", config={}),
            DataDeletionRequest(provider="meta", external_user_id="9", confirmation_code="abc123def456"),
        ]
    )
    log_activity(db, workspace.id, ActivityLevel.ERROR, "publish", "Publishing failed <script>x</script>")
    log_activity(db, workspace.id, ActivityLevel.INFO, "import", "Imported 3 videos")
    db.commit()

    import repeater.platforms.tiktok as tiktok_module

    monkeypatch.setattr(
        tiktok_module,
        "creator_info",
        lambda ctx: TikTokCreatorInfo(
            nickname="Magic Lap",
            avatar_url="https://p16.tiktokcdn.com/avatar.jpg",
            privacy_level_options=["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
            comment_disabled=False,
            duet_disabled=False,
            stitch_disabled=False,
            max_video_post_duration_sec=600,
        ),
    )
    sources = {s.kind: s for s in db.query(Source).all()}
    return {
        "video": video,
        "tiktok": tiktok,
        "youtube": youtube,
        "insta": insta,
        "facebook": broken,
        "sources": sources,
    }


def _pages(data) -> list[str]:
    s = data["sources"]
    return [
        "/",
        "/library",
        "/library?status=all&page=1",
        "/library?status=archived",
        f"/library/{data['video'].id}",
        "/schedule",
        "/accounts",
        f"/accounts/{data['youtube'].id}/disconnect",
        "/sources",
        "/sources/new?kind=platform_api",
        "/sources/new?kind=ytdlp",
        "/sources/new?kind=drive",
        f"/sources/{s[SourceKind.PLATFORM_API].id}/edit",
        f"/sources/{s[SourceKind.YTDLP].id}/edit",
        f"/sources/{s[SourceKind.DRIVE].id}/edit",
        "/rules",
        f"/rules/{data['tiktok'].id}",
        f"/rules/{data['youtube'].id}",
        f"/rules/{data['insta'].id}",
        f"/rules/{data['facebook'].id}",
        "/activity",
        "/activity?level=error&category=publish",
        "/privacy",
        "/terms",
        "/meta/deletion-status/abc123def456",
    ]


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_every_page_renders(web_owner, populated, lang):
    set_lang(web_owner, lang)
    direction = "rtl" if lang == "ar" else "ltr"
    for path in _pages(populated):
        resp = web_owner.get(path)
        assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text[:300]}"
        assert f'<html lang="{lang}" dir="{direction}">' in resp.text, path
        assert 'href="/privacy"' in resp.text and 'href="/terms"' in resp.text, path
        assert "<script>x</script>" not in resp.text, path
        # Untranslated keys would show up as dotted identifiers.
        leftovers = re.findall(
            r">\s*((?:nav|library|rules|sources|accounts|overview|schedule|error)\.[a-z_]+)\s*<", resp.text
        )
        assert not leftovers, f"{path}: {leftovers}"


def test_overview_shows_attention_items(web_owner, populated):
    set_lang(web_owner, "en")
    html = web_owner.get("/").text
    assert "Needs your attention" in html
    assert "Magic Page" in html and "needs to be reconnected" in html
    assert "Instagram rejected the video" in html
    assert "Broken file" in html
    assert "Instagram is down" in html
    assert "Storage" in html and "used of" in html


def test_overview_survives_missing_disk(web_owner, populated, monkeypatch):
    from repeater.media import storage

    def broken():
        raise OSError("no volume")

    monkeypatch.setattr(storage, "disk_usage", broken)
    set_lang(web_owner, "en")
    assert "Storage usage is not available" in web_owner.get("/").text


def test_pages_render_for_an_empty_workspace(web_owner):
    for path in ("/", "/library", "/schedule", "/accounts", "/sources", "/rules", "/activity"):
        assert web_owner.get(path).status_code == 200, path


def test_activity_filters_and_pagination(web_owner, db, workspace):
    for i in range(60):
        log_activity(db, workspace.id, ActivityLevel.INFO, "import", f"event number {i}")
    log_activity(db, workspace.id, ActivityLevel.ERROR, "publish", "the only error")
    db.commit()
    set_lang(web_owner, "en")
    page1 = web_owner.get("/activity").text
    assert "Page 1 of 2" in page1 and "the only error" in page1
    page2 = web_owner.get("/activity?page=2").text
    assert "event number 0" in page2
    errors = web_owner.get("/activity?level=error").text
    assert "the only error" in errors and "event number" not in errors
    publish = web_owner.get("/activity?category=publish&level=bogus").text
    assert "the only error" in publish and "event number" not in publish


def test_activity_is_workspace_scoped(web_owner, db):
    from repeater.models import Workspace

    other = Workspace(name="Other")
    db.add(other)
    db.flush()
    log_activity(db, other.id, ActivityLevel.INFO, "import", "secret other workspace event")
    db.commit()
    assert "secret other workspace event" not in web_owner.get("/activity").text


# --------------------------------------------------------------------------- translations


def _template_keys() -> set[str]:
    keys: set[str] = set()
    for path in TEMPLATES.rglob("*.html"):
        keys.update(re.findall(r"\bt\('([a-z0-9_.A-Z]+)'", path.read_text("utf-8")))
    return {k for k in keys if not k.endswith((".", "_"))}


def test_translation_tables_have_the_same_keys():
    assert set(i18n.EN) == set(i18n.AR)
    assert all(i18n.AR[k].strip() for k in i18n.AR), "empty Arabic string"
    assert all(i18n.EN[k].strip() for k in i18n.EN), "empty English string"


def test_every_template_key_is_translated():
    missing = sorted(k for k in _template_keys() if k not in i18n.EN)
    assert not missing, missing


def test_every_python_key_is_translated():
    root = TEMPLATES.parent
    keys: set[str] = set()
    for path in root.rglob("*.py"):
        text = path.read_text("utf-8")
        keys.update(
            re.findall(
                r"\"((?:error|login|library|oauth|sources|rules|schedule|accounts|activity|err|problem|consent)"
                r"\.[a-z0-9_]+)\"",
                text,
            )
        )
    missing = sorted(k for k in keys if k not in i18n.EN and not k.endswith((".html", "_")))
    assert not missing, missing


def test_dynamic_keys_are_translated():
    groups = {
        "video_status": [s.value for s in VideoStatus],
        "post_status": [s.value for s in PostStatus],
        "account_status": [s.value for s in AccountStatus],
        "level": [lvl.value for lvl in ActivityLevel],
        "kind": [k.value for k in SourceKind],
    }
    for group, values in groups.items():
        for value in values:
            assert f"{group}.{value}" in i18n.EN, f"{group}.{value}"


def test_translator_field_labels_and_missing_params():
    assert i18n.message("en", "error.field_required", {"field": "daily_cap"}) == "Posts per day is required."
    assert "عدد المنشورات يوميًا" in i18n.message("ar", "error.field_required", {"field": "daily_cap"})
    assert i18n.t("en", "pager.position", page=1) == "Page 1 of {pages}"
    assert i18n.t("en", "no.such.key") == "no.such.key"
    assert i18n.normalize("EN") == "en" and i18n.normalize("fr") is None


# --------------------------------------------------------------------------- legal pages


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_privacy_policy_checklist(web_client, lang):
    set_lang(web_client, lang)
    resp = web_client.get("/privacy")
    assert resp.status_code == 200
    html = resp.text
    assert '<meta name="robots" content="index, follow">' in html
    for needle in (
        "Magic Lap",
        "info@magic-lap.com",
        "YouTube API Services",
        "https://www.youtube.com/t/terms",
        "https://policies.google.com/privacy",
        "https://myaccount.google.com/permissions",
        "https://developers.google.com/terms/api-services-user-data-policy",
        "Limited Use",
        "yt-dlp",
        "AES-256-GCM",
        "/meta/deletion-status/",
        "30",
    ):
        assert needle in html, f"{lang}: {needle}"
    if lang == "en":
        for needle in ("TikTok", "Meta", "do not sell", "OAuth access and refresh tokens", "Activity logs"):
            assert needle in html, needle
    else:
        for needle in ("تيك توك", "ميتا", "لا نبيع", "توكنات OAuth", "سجل النشاط"):
            assert needle in html, needle


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_terms_link_youtube_terms_and_google_privacy(web_client, lang):
    set_lang(web_client, lang)
    html = web_client.get("/terms").text
    assert "https://www.youtube.com/t/terms" in html
    assert "https://policies.google.com/privacy" in html
    assert "Magic Lap" in html and 'href="/privacy"' in html


def test_legal_pages_are_public(web_app):
    from fastapi.testclient import TestClient

    with TestClient(web_app) as client:
        for path in ("/privacy", "/terms"):
            resp = client.get(path, follow_redirects=False)
            assert resp.status_code == 200, path


def test_overview_upload_action_opens_form_even_with_existing_videos(web_owner, web_make_video):
    """A primary upload action must reach the form, including a populated library."""
    from html import unescape

    web_make_video(title="Already in the library")
    overview = web_owner.get("/").text
    target = unescape(re.search(r'href="([^"]*#upload)"', overview).group(1))
    page = web_owner.get(target)
    assert page.status_code == 200
    disclosure = re.search(r'<details\b[^>]*\bid="upload"[^>]*>', page.text).group(0)
    assert re.search(r"\bopen\b", disclosure), "upload action left the form collapsed"
    assert 'action="/library/upload"' in page.text
