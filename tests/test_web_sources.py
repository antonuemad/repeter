"""Sources: create/edit official, unofficial (yt-dlp) and Drive sources; poll now; on/off."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from repeater.models import Job, Platform, Source, SourceKind, Workspace
from repeater.timeutil import utcnow
from repeater.web.deps import FormError
from repeater.web.routes.sources import normalize_profile_url
from tests.test_web_support import post, set_lang

pytest_plugins = ["tests.test_web_support"]

FOLDER_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


def _jobs(db, kind: str) -> list[Job]:
    return list(db.execute(select(Job).where(Job.kind == kind)).scalars())


def _create(client, kind: str, form: dict):
    return post(client, f"/sources/new?kind={kind}", {"kind": kind, **form}, page=f"/sources/new?kind={kind}")


def test_create_platform_api_source(web_owner, db, make_account):
    insta = make_account("instagram", display_name="magic.ig")
    resp = _create(
        web_owner,
        "platform_api",
        {"account_id": str(insta.id), "poll_interval_minutes": "15", "backfill_count": "5", "enabled": "1"},
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/sources"
    source = db.execute(select(Source)).scalar_one()
    assert source.kind == SourceKind.PLATFORM_API and source.platform == Platform.INSTAGRAM
    assert source.connected_account_id == insta.id and source.name == "magic.ig"
    assert source.config == {"backfill_count": 5} and source.enabled
    assert [j.payload for j in _jobs(db, "poll_source")] == [{"source_id": source.id}]
    assert _jobs(db, "plan_workspace")


def test_platform_api_requires_meta_account(web_owner, db, make_account):
    tiktok = make_account("tiktok")
    set_lang(web_owner, "en")
    for account_id in ("", str(tiktok.id), "abc", "999999"):
        resp = _create(web_owner, "platform_api", {"account_id": account_id, "poll_interval_minutes": "15"})
        assert resp.status_code == 400, account_id
        assert "Choose a connected account for this source." in resp.text
    assert db.execute(select(Source)).first() is None


def test_ytdlp_requires_acknowledgement(web_owner, db):
    set_lang(web_owner, "en")
    form = {
        "platform": "tiktok",
        "profile_url": "https://www.tiktok.com/@magiclap",
        "poll_interval_minutes": "60",
        "enabled": "1",
    }
    resp = _create(web_owner, "ytdlp", form)
    assert resp.status_code == 400
    assert "Confirm the unofficial import warning" in resp.text
    assert 'value="https://www.tiktok.com/@magiclap"' in resp.text  # input kept
    assert db.execute(select(Source)).first() is None


def test_ytdlp_form_shows_warning(web_owner):
    set_lang(web_owner, "en")
    html = web_owner.get("/sources/new?kind=ytdlp").text
    assert "Unofficial import: read before turning it on" in html
    assert "yt-dlp" in html and "Google Drive" in html
    assert 'name="ack" value="1" required' in html


def test_create_ytdlp_source_with_ack(web_owner, db, make_account):
    tiktok = make_account("tiktok", display_name="Magic TikTok")
    form = {
        "platform": "tiktok",
        "profile_url": "tiktok.com/@MagicLap/?lang=en#top",
        "account_id": str(tiktok.id),
        "poll_interval_minutes": "45",
        "backfill_count": "3",
        "enabled": "1",
        "ack": "1",
    }
    assert _create(web_owner, "ytdlp", form).status_code == 303
    source = db.execute(select(Source)).scalar_one()
    assert source.kind == SourceKind.YTDLP and source.platform == Platform.TIKTOK
    # A yt-dlp source never keeps a connected account (contract C6), even if one is submitted.
    assert source.unofficial_ack_at is not None and source.connected_account_id is None
    assert source.config == {"backfill_count": 3, "profile_url": "https://tiktok.com/@MagicLap"}


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"platform": "tiktok", "profile_url": "https://evil.example/@x"}, "Enter a profile link"),
        ({"platform": "tiktok", "profile_url": "https://www.tiktok.com/video/123"}, "Enter a profile link"),
        ({"platform": "youtube", "profile_url": "https://www.tiktok.com/@x"}, "Enter a profile link"),
        ({"platform": "instagram", "profile_url": "https://www.tiktok.com/@x"}, "Choose TikTok or YouTube."),
        (
            {"platform": "tiktok", "profile_url": "https://www.tiktok.com/@x", "poll_interval_minutes": "10"},
            "between 30 and 1440",
        ),
    ],
)
def test_ytdlp_validation(web_owner, db, form, message):
    set_lang(web_owner, "en")
    payload = {"poll_interval_minutes": "60", "ack": "1", **form}
    resp = _create(web_owner, "ytdlp", payload)
    assert resp.status_code == 400 and message in resp.text


def test_ytdlp_form_has_no_linked_account_option(web_owner, make_account):
    make_account("tiktok", display_name="Magic TikTok")
    set_lang(web_owner, "en")
    html = web_owner.get("/sources/new?kind=ytdlp").text
    assert 'name="account_id"' not in html
    assert "Linked account" not in html and "detected through the official API" not in html
    assert "never uses the tokens or the official API of any connected account" in html


def test_ytdlp_edit_unlinks_a_legacy_linked_account(web_owner, db, make_account):
    tiktok = make_account("tiktok", display_name="Magic TikTok")
    source = Source(
        workspace_id=tiktok.workspace_id,
        kind=SourceKind.YTDLP,
        platform=Platform.TIKTOK,
        connected_account_id=tiktok.id,
        name="Old",
        config={"profile_url": "https://www.tiktok.com/@x", "backfill_count": 0},
        poll_interval_minutes=60,
        enabled=False,
        unofficial_ack_at=utcnow(),
    )
    db.add(source)
    db.commit()
    form = {
        "platform": "tiktok",
        "profile_url": "https://www.tiktok.com/@x",
        "account_id": str(tiktok.id),
        "poll_interval_minutes": "60",
    }
    resp = post(web_owner, f"/sources/{source.id}/edit", form, page=f"/sources/{source.id}/edit")
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Source, source.id).connected_account_id is None


def test_normalize_profile_url():
    assert (
        normalize_profile_url("https://www.youtube.com/@magic/shorts?x=1", Platform.YOUTUBE)
        == "https://www.youtube.com/@magic"
    )
    assert (
        normalize_profile_url("https://m.youtube.com/channel/UC123/videos", Platform.YOUTUBE)
        == "https://m.youtube.com/channel/UC123"
    )
    for bad in (
        "javascript:alert(1)",
        "ftp://www.tiktok.com/@x",
        "https://www.tiktok.com/",
        "https://www.youtube.com/watch?v=1",
    ):
        with pytest.raises(FormError):
            normalize_profile_url(bad, Platform.TIKTOK if "tiktok" in bad else Platform.YOUTUBE)


def test_create_drive_source_from_link(web_owner, db, make_account):
    drive = make_account("gdrive", display_name="drive@magic-lap.com")
    form = {
        "account_id": str(drive.id),
        "folder": f"https://drive.google.com/drive/folders/{FOLDER_ID}?usp=sharing",
        "poll_interval_minutes": "10",
        "enabled": "1",
    }
    assert _create(web_owner, "drive", form).status_code == 303
    source = db.execute(select(Source)).scalar_one()
    assert source.kind == SourceKind.DRIVE and source.platform == Platform.GDRIVE
    assert source.config["folder_id"] == FOLDER_ID


def test_drive_source_rejects_bad_folder(web_owner, db, make_account):
    drive = make_account("gdrive")
    set_lang(web_owner, "en")
    resp = _create(
        web_owner,
        "drive",
        {
            "account_id": str(drive.id),
            "folder": "https://evil.example/folders/x",
            "poll_interval_minutes": "10",
        },
    )
    assert resp.status_code == 400 and "Google Drive folder link is not valid" in resp.text


def test_unknown_kind_is_404(web_owner):
    assert web_owner.get("/sources/new?kind=manual").status_code == 404
    assert web_owner.get("/sources/new?kind=bogus").status_code == 404


def _ytdlp_source(db, workspace, **config) -> Source:
    from repeater.timeutil import utcnow

    source = Source(
        workspace_id=workspace.id,
        kind=SourceKind.YTDLP,
        platform=Platform.TIKTOK,
        name="TikTok profile",
        config={
            "profile_url": "https://www.tiktok.com/@old",
            "baseline_at": "2026-10-01T00:00:00+00:00",
            "seen_ids": ["1"],
            **config,
        },
        unofficial_ack_at=utcnow(),
        poll_interval_minutes=60,
        enabled=True,
    )
    db.add(source)
    db.commit()
    return source


def test_edit_keeps_detection_state_unless_profile_changes(web_owner, db, workspace):
    source = _ytdlp_source(db, workspace)
    base = {"platform": "tiktok", "poll_interval_minutes": "90", "enabled": "1"}
    resp = post(
        web_owner,
        f"/sources/{source.id}/edit",
        {**base, "profile_url": "https://www.tiktok.com/@old", "name": "Renamed"},
        page="/sources",
    )
    assert resp.status_code == 303
    db.expire_all()
    edited = db.get(Source, source.id)
    assert edited.name == "Renamed" and edited.poll_interval_minutes == 90
    assert edited.config["seen_ids"] == ["1"] and "baseline_at" in edited.config
    post(
        web_owner,
        f"/sources/{source.id}/edit",
        {**base, "profile_url": "https://www.tiktok.com/@new"},
        page="/sources",
    )
    db.expire_all()
    edited = db.get(Source, source.id)
    assert edited.config["profile_url"] == "https://www.tiktok.com/@new"
    assert "seen_ids" not in edited.config and "baseline_at" not in edited.config
    assert _jobs(db, "poll_source")


def test_edit_form_shows_acknowledgement_date(web_owner, db, workspace):
    source = _ytdlp_source(db, workspace)
    set_lang(web_owner, "en")
    html = web_owner.get(f"/sources/{source.id}/edit").text
    assert "Risk accepted on" in html and 'name="ack"' not in html


def test_poll_now_and_toggle(web_owner, db, workspace):
    source = _ytdlp_source(db, workspace)
    resp = post(web_owner, f"/sources/{source.id}/poll", page="/sources")
    assert resp.status_code == 303
    assert [j.payload for j in _jobs(db, "poll_source")] == [{"source_id": source.id}]
    set_lang(web_owner, "en")
    post(web_owner, f"/sources/{source.id}/poll", page="/sources")
    assert "is already being checked" in web_owner.get("/sources").text
    post(web_owner, f"/sources/{source.id}/toggle", page="/sources")
    db.expire_all()
    assert db.get(Source, source.id).enabled is False
    post(web_owner, f"/sources/{source.id}/poll", page="/sources")
    assert "before checking it" in web_owner.get("/sources").text
    post(web_owner, f"/sources/{source.id}/toggle", page="/sources")
    db.expire_all()
    assert db.get(Source, source.id).enabled is True


def test_toggle_unacknowledged_ytdlp_is_refused(web_owner, db, workspace):
    source = _ytdlp_source(db, workspace)
    source.unofficial_ack_at = None
    source.enabled = False
    db.commit()
    resp = post(web_owner, f"/sources/{source.id}/toggle", page="/sources")
    assert resp.headers["location"] == f"/sources/{source.id}/edit"
    db.expire_all()
    assert db.get(Source, source.id).enabled is False


def test_sources_are_workspace_scoped(web_owner, db):
    other = Workspace(name="Other")
    db.add(other)
    db.commit()
    foreign = _ytdlp_source(db, other)
    assert web_owner.get(f"/sources/{foreign.id}/edit").status_code == 404
    assert post(web_owner, f"/sources/{foreign.id}/poll", page="/sources").status_code == 404
    assert "TikTok profile" not in web_owner.get("/sources").text


def test_sources_list_shows_errors(web_owner, db, workspace):
    source = _ytdlp_source(db, workspace)
    source.last_error = "yt-dlp could not read the profile"
    source.consecutive_failures = 2
    db.commit()
    set_lang(web_owner, "en")
    html = web_owner.get("/sources").text
    assert "yt-dlp could not read the profile" in html and "2 failed checks in a row" in html
