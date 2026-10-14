"""Library page, per-video actions and the streaming manual upload."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from repeater.engine import actions
from repeater.media import storage
from repeater.models import Job, Platform, PostStatus, ScheduledPost, Video, VideoStatus, Workspace
from repeater.sources import manual
from repeater.timeutil import utcnow
from repeater.web.routes import library
from tests.test_web_support import Recorder, csrf_token, post, set_lang

pytest_plugins = ["tests.test_web_support"]


def _uploads() -> list[Path]:
    return sorted(storage.uploads_dir().glob("*"))


@pytest.fixture
def fake_create(monkeypatch, db, workspace):
    """Replace create_video_from_upload: records the call and the uploaded bytes."""
    seen: dict = {}

    def create(session, *, workspace_id, upload_path, original_filename, caption="", title="", actor="user"):
        seen.update(
            workspace_id=workspace_id,
            path=Path(upload_path),
            data=Path(upload_path).read_bytes(),
            filename=original_filename,
            caption=caption,
            title=title,
            actor=actor,
        )
        video = Video(
            workspace_id=workspace_id, status=VideoStatus.PROCESSING, title=title or "untitled", overrides={}
        )
        session.add(video)
        session.flush()
        Path(upload_path).unlink()  # the real function moves the file into the library
        return video

    monkeypatch.setattr(manual, "create_video_from_upload", create)
    return seen


def _upload(
    client, *, data: bytes = b"\x00\x00\x00\x18ftypmp42" + b"x" * 5000, token: str | None = None, **fields
):
    token = csrf_token(client, "/library") if token is None else token
    form = {"csrf_token": token, "title": "My trick", "caption": "Watch this #magic", **fields}
    return client.post(
        "/library/upload", data=form, files={"file": ("trick.mp4", data, "video/mp4")}, follow_redirects=False
    )


def test_upload_streams_file_and_creates_video(web_owner, fake_create, db):
    payload = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 400
    resp = _upload(web_owner, data=payload)
    assert resp.status_code == 303
    video = db.execute(select(Video)).scalar_one()
    assert resp.headers["location"] == f"/library/{video.id}"
    assert fake_create["data"] == payload
    assert fake_create["filename"] == "trick.mp4"
    assert fake_create["title"] == "My trick" and fake_create["caption"] == "Watch this #magic"
    assert fake_create["actor"] == "admin@magic-lap.com"
    assert fake_create["path"].parent == storage.uploads_dir()
    assert _uploads() == []


def test_upload_with_real_import(web_owner, db, sample_video):
    resp = _upload(web_owner, data=sample_video.read_bytes())
    assert resp.status_code == 303
    video = db.execute(select(Video)).scalar_one()
    assert video.status == VideoStatus.PROCESSING and video.title == "My trick"
    assert storage.abs_path(video.original_path).read_bytes() == sample_video.read_bytes()
    assert db.execute(select(Job).where(Job.kind == "process_video")).scalars().first() is not None
    assert _uploads() == []


def test_upload_rejects_non_video_with_message(web_owner, db):
    resp = _upload(web_owner, data=b"this is not a video at all" * 100)
    assert resp.status_code == 303 and resp.headers["location"] == "/library"
    set_lang(web_owner, "en")
    assert "not a video" in web_owner.get("/library").text
    assert db.execute(select(Video)).first() is None
    assert _uploads() == []


def test_upload_requires_csrf_before_storing(web_owner, fake_create):
    resp = _upload(web_owner, token="wrong-token-value-wrong-token-value-1234")
    assert resp.status_code == 403
    assert fake_create == {} and _uploads() == []


def test_upload_accepts_csrf_header(web_owner, fake_create):
    token = csrf_token(web_owner, "/library")
    resp = web_owner.post(
        "/library/upload",
        files={"file": ("a.mov", b"data" * 100, "video/quicktime")},
        headers={"X-CSRF-Token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and fake_create["filename"] == "a.mov"


def test_upload_too_large_by_stream(web_owner, fake_create, monkeypatch):
    monkeypatch.setattr(library, "upload_limit_bytes", lambda: 1000)
    resp = _upload(web_owner, data=b"x" * 5000)
    assert resp.status_code == 303 and resp.headers["location"] == "/library"
    assert fake_create == {} and _uploads() == []
    set_lang(web_owner, "en")
    assert "larger than the 0 MB limit" in web_owner.get("/library").text


def test_upload_too_large_by_content_length(web_owner, fake_create, monkeypatch):
    monkeypatch.setattr(library, "upload_limit_bytes", lambda: 10)
    monkeypatch.setattr(library, "MULTIPART_OVERHEAD", 10)
    resp = _upload(web_owner, data=b"x" * 5000)
    assert resp.status_code == 303 and fake_create == {} and _uploads() == []


def test_upload_without_file(web_owner, fake_create):
    token = csrf_token(web_owner, "/library")
    resp = web_owner.post(
        "/library/upload",
        data={"csrf_token": token},
        files={"file": ("", b"", "application/octet-stream")},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and fake_create == {}
    set_lang(web_owner, "en")
    assert "Choose a video file" in web_owner.get("/library").text


def test_upload_rejects_non_multipart(web_owner, fake_create):
    token = csrf_token(web_owner, "/library")
    resp = web_owner.post("/library/upload", data={"csrf_token": token}, follow_redirects=False)
    assert resp.status_code == 303 and fake_create == {}


def test_upload_import_error_cleans_temp_file(web_owner, monkeypatch):
    monkeypatch.setattr(
        manual, "create_video_from_upload", Recorder(error=ValueError("'trick.mp4' is not a video"))
    )
    resp = _upload(web_owner)
    assert resp.status_code == 303 and _uploads() == []
    set_lang(web_owner, "en")
    assert "is not a video" in web_owner.get("/library").text


def test_upload_requires_login(web_client, fake_create):
    resp = web_client.post(
        "/library/upload", files={"file": ("a.mp4", b"x", "video/mp4")}, follow_redirects=False
    )
    assert resp.status_code in (303, 403)
    assert fake_create == {}


# --------------------------------------------------------------------------- grid and actions


def test_library_grid_shows_status_per_account(web_owner, db, workspace, make_account, web_make_video):
    tiktok = make_account("tiktok", display_name="TT account")
    video = web_make_video(title="Coin vanish")
    db.add(
        ScheduledPost(
            workspace_id=workspace.id,
            video_id=video.id,
            connected_account_id=tiktok.id,
            platform=Platform.TIKTOK,
            idempotency_key=f"{video.id}:{tiktok.id}:0",
            scheduled_at=utcnow(),
            status=PostStatus.PUBLISHED,
        )
    )
    db.commit()
    set_lang(web_owner, "en")
    html = web_owner.get("/library").text
    assert "Coin vanish" in html
    assert f'src="/library/{video.id}/thumb.jpg"' in html
    assert "Published" in html and "TT account" in html
    assert web_owner.get("/library?status=archived").text.count("Coin vanish") == 0


def test_library_is_workspace_scoped(web_owner, db, web_make_video):
    other = Workspace(name="Other")
    db.add(other)
    db.commit()
    web_make_video(workspace_id=other.id, title="Not yours")
    assert "Not yours" not in web_owner.get("/library?status=all").text


def test_library_pagination(web_owner, web_make_video):
    for i in range(30):
        web_make_video(title=f"clip {i:02d}", with_files=False)
    set_lang(web_owner, "en")
    first = web_owner.get("/library").text
    assert "Page 1 of 2" in first and "clip 29" in first and "clip 00" not in first
    assert "clip 00" in web_owner.get("/library?page=2").text
    assert web_owner.get("/library?page=abc").status_code == 200


@pytest.mark.parametrize(("path", "action"), [("archive", "archive_video"), ("restore", "restore_video")])
def test_archive_and_restore_call_engine(web_owner, web_make_video, monkeypatch, path, action):
    recorder = Recorder()
    monkeypatch.setattr(actions, action, recorder)
    video = web_make_video()
    resp = post(web_owner, f"/library/{video.id}/{path}", {"next": "/library?status=all"})
    assert resp.status_code == 303 and resp.headers["location"] == "/library?status=all"
    (args, kwargs) = recorder.calls[0]
    assert args[1].id == video.id and kwargs == {"actor": "admin@magic-lap.com"}


def test_action_error_is_flashed(web_owner, web_make_video, monkeypatch):
    monkeypatch.setattr(actions, "archive_video", Recorder(error=ValueError("cannot archive right now")))
    video = web_make_video()
    resp = post(web_owner, f"/library/{video.id}/archive")
    assert resp.headers["location"] == f"/library/{video.id}"
    assert "cannot archive right now" in web_owner.get(f"/library/{video.id}").text


def test_publish_now_to_account(web_owner, web_make_video, make_account, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(actions, "schedule_video_now", recorder)
    account = make_account("youtube", display_name="Channel")
    video = web_make_video()
    resp = post(web_owner, f"/library/{video.id}/publish-now", {"account_id": str(account.id)})
    assert resp.status_code == 303
    args, kwargs = recorder.calls[0]
    assert args[1].id == video.id and args[2].id == account.id and kwargs["actor"] == "admin@magic-lap.com"
    set_lang(web_owner, "en")
    assert "Publishing to Channel now." in web_owner.get(f"/library/{video.id}").text


def test_publish_now_rejects_foreign_or_missing_account(
    web_owner, web_make_video, db, monkeypatch, make_account
):
    recorder = Recorder()
    monkeypatch.setattr(actions, "schedule_video_now", recorder)
    other = Workspace(name="Other")
    db.add(other)
    db.commit()
    foreign = make_account("youtube", workspace_id=other.id)
    video = web_make_video()
    post(web_owner, f"/library/{video.id}/publish-now", {"account_id": str(foreign.id)})
    post(web_owner, f"/library/{video.id}/publish-now", {"account_id": "abc"})
    assert recorder.calls == []


def test_publish_now_engine_error(web_owner, web_make_video, make_account, monkeypatch):
    monkeypatch.setattr(
        actions,
        "schedule_video_now",
        Recorder(error=ValueError("Only videos that are ready can be published.")),
    )
    account = make_account("tiktok")
    video = web_make_video()
    post(web_owner, f"/library/{video.id}/publish-now", {"account_id": str(account.id)})
    assert "Only videos that are ready can be published." in web_owner.get(f"/library/{video.id}").text


def test_edit_overrides_and_platforms(web_owner, db, workspace, web_make_video, make_account):
    account = make_account("facebook")
    video = web_make_video(overrides={"keep": 1})
    post_row = ScheduledPost(
        workspace_id=workspace.id,
        video_id=video.id,
        connected_account_id=account.id,
        platform=Platform.FACEBOOK,
        idempotency_key=f"{video.id}:{account.id}:0",
        scheduled_at=utcnow(),
    )
    db.add(post_row)
    db.commit()
    resp = post(
        web_owner,
        f"/library/{video.id}/edit",
        {
            "title": "New title",
            "caption": "New caption",
            "platform_tiktok": "1",
            "platform_youtube": "1",
            "platform_instagram": "1",
        },
    )
    assert resp.status_code == 303
    db.expire_all()
    refreshed = db.get(Video, video.id)
    assert refreshed.overrides == {
        "keep": 1,
        "title": "New title",
        "caption": "New caption",
        "exclude_platforms": ["facebook"],
    }
    assert db.get(ScheduledPost, post_row.id).status == PostStatus.CANCELED
    assert db.execute(select(Job).where(Job.kind == "plan_workspace")).scalars().first() is not None
    # Clearing the fields removes the overrides again.
    post(
        web_owner,
        f"/library/{video.id}/edit",
        {
            "title": "",
            "caption": "",
            **{f"platform_{p}": "1" for p in ("tiktok", "youtube", "instagram", "facebook")},
        },
    )
    db.expire_all()
    assert db.get(Video, video.id).overrides == {"keep": 1}


def test_edit_rejects_too_long_title(web_owner, web_make_video, db):
    video = web_make_video()
    post(web_owner, f"/library/{video.id}/edit", {"title": "x" * 400})
    db.expire_all()
    assert "title" not in (db.get(Video, video.id).overrides or {})
    set_lang(web_owner, "en")
    assert "Title is too long" in web_owner.get(f"/library/{video.id}").text


def test_detail_shows_caption_preview_and_history(web_owner, db, workspace, web_make_video, make_account):
    from repeater.models import RotationRule

    account = make_account("instagram", display_name="magic.ig")
    db.add(
        RotationRule(
            workspace_id=workspace.id,
            connected_account_id=account.id,
            platform=Platform.INSTAGRAM,
            caption_template="{caption}",
            hashtags="magic tricks",
        )
    )
    video = web_make_video(caption="Original caption")
    db.add(
        ScheduledPost(
            workspace_id=workspace.id,
            video_id=video.id,
            connected_account_id=account.id,
            platform=Platform.INSTAGRAM,
            idempotency_key=f"{video.id}:{account.id}:0",
            scheduled_at=utcnow(),
            status=PostStatus.PUBLISHED,
            permalink="https://www.instagram.com/reel/abc/",
        )
    )
    db.commit()
    set_lang(web_owner, "en")
    html = web_owner.get(f"/library/{video.id}").text
    assert "Original caption\n\n#magic #tricks" in html
    assert "https://www.instagram.com/reel/abc/" in html
    assert f'src="/library/{video.id}/preview"' in html
