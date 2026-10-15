"""Phase-four acceptance: synthetic accounts and bounded raster fixtures; HTTP is mocked."""

import io
from datetime import timedelta

import pytest
import respx
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from repeater.config import get_settings
from repeater.content_plans import simulate
from repeater.engine import actions, publisher
from repeater.media import content, images, storage
from repeater.models import (
    CreatorProfile,
    Job,
    Platform,
    PostStatus,
    ScheduledPost,
    Source,
    SourceKind,
    Video,
    VideoStatus,
    Workspace,
)
from repeater.sources import drive, importer
from tests.test_content_plans import create_plan, folder_mock, save, video
from tests.test_plan_rotation import NOW, assignments, history, setup_plan
from tests.test_sources import FOLDER_ID, _drive_file, ctx
from tests.test_web_support import csrf_token, post, set_lang

pytest_plugins = ["tests.test_web_support"]


@pytest.fixture(autouse=True)
def mock_http():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def raster(size=(900, 1600), fmt="PNG", color=(40, 90, 170, 150)):
    out = io.BytesIO()
    Image.new("RGBA", size, color).save(out, fmt)
    return out.getvalue()


def upload(client, person, payload, fmt="image_story", token=None):
    return client.post(
        "/library/upload",
        data={
            "csrf_token": token or csrf_token(client, "/library"),
            "profile_id": str(person),
            "format": fmt,
            "title": "Synthetic image",
            "caption": "A caption",
        },
        files={"file": ("untrusted.svg", payload, "image/svg+xml")},
        follow_redirects=False,
    )


def finish(db, response):
    item = db.get(Video, int(response.headers["location"].split("/")[-1]))
    assert item.status == VideoStatus.PROCESSING
    importer.process_video(db, item.id)
    db.refresh(item)
    return item


def test_person_image_upload_decode_preview_rotation_and_no_publication(
    web_owner, db, make_account, mock_http
):
    plan, (account,) = setup_plan(web_owner, db, make_account)
    item = finish(db, upload(web_owner, plan.profile_id, raster()))
    assert (item.media_kind, item.content_format, item.status) == ("image", "story", VideoStatus.READY)
    assert item.profile_id == plan.profile_id and item.width == 900 and item.height == 1600
    response = web_owner.get(f"/library/{item.id}/preview")
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-content-type-options"] == "nosniff"
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.mode == "RGB" and not image.getexif()
    assert web_owner.get(f"/library/{item.id}").status_code == 200
    choices = assignments(simulate(db, plan, now=NOW), account)
    assert choices[0][1].video.id == item.id
    assert choices[1][1].reason == "cooldown" and choices[7][1].video.id == item.id
    assert not mock_http.calls
    assert db.scalar(select(func.count()).select_from(ScheduledPost)) == 0
    assert not list(db.scalars(select(Job).where(Job.kind.in_(("publish_post", "poll_post")))))


def test_person_video_story_upload_uses_existing_processing(web_owner, db, sample_video):
    plan = create_plan(web_owner, db)
    response = upload(web_owner, plan.profile_id, sample_video.read_bytes(), "video_story")
    item = db.get(Video, int(response.headers["location"].split("/")[-1]))
    assert (
        item.profile_id == plan.profile_id and item.content_format == "story" and item.media_kind == "video"
    )
    assert item.status == VideoStatus.PROCESSING


@pytest.mark.parametrize(
    "payload", [b"<svg><script>alert(1)</script></svg>", b"<html>bad</html>", b"RIFFbrokenWEBP"]
)
def test_spoofed_image_rejected_and_temp_removed(web_owner, db, payload):
    p = create_plan(web_owner, db)
    response = upload(web_owner, p.profile_id, payload)
    assert response.headers["location"] == "/library"
    assert db.scalar(select(func.count()).select_from(Video)) == 0
    assert list(storage.uploads_dir().glob("*.part")) == []


def test_image_limits_animation_and_csrf(web_owner, db, tmp_path, monkeypatch):
    p = create_plan(web_owner, db)
    assert upload(web_owner, p.profile_id, raster(), token="invalid-token").status_code == 403
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 100)
    assert upload(web_owner, p.profile_id, raster()).headers["location"] == "/library"
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 20 * 1024 * 1024)
    monkeypatch.setattr(images, "MAX_IMAGE_PIXELS", 100)
    path = tmp_path / "big.png"
    path.write_bytes(raster())
    with pytest.raises(ValueError):
        images.decode(path)
    monkeypatch.setattr(images, "MAX_IMAGE_PIXELS", 24_000_000)
    Image.new("RGB", (10, 10), "red").save(
        path, "PNG", save_all=True, append_images=[Image.new("RGB", (10, 10), "blue")]
    )
    with pytest.raises(ValueError):
        images.decode(path)
    assert list(storage.uploads_dir().glob("*.part")) == []


def test_image_exif_orientation_and_metadata_removed(tmp_path):
    path = tmp_path / "oriented.jpg"
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "private metadata"
    Image.new("RGB", (160, 90)).save(path, exif=exif)
    result = images.decode(path)
    assert result.size == (90, 160) and not result.getexif()


def test_foreign_or_missing_person_cannot_create_rich_content(web_owner, db):
    other = Workspace(name="Other tenant")
    db.add(other)
    db.flush()
    person = CreatorProfile(workspace_id=other.id, name="Private")
    db.add(person)
    db.commit()
    for value in (person.id, "", "invalid", "9999999999"):
        assert upload(web_owner, value, raster()).headers["location"] == "/library"
        assert (
            post(
                web_owner, "/library/text", {"profile_id": str(value), "title": "x", "caption": "private"}
            ).headers["location"]
            == "/library"
        )
    assert db.scalar(select(func.count()).select_from(Video)) == 0
    assert list(storage.uploads_dir().glob("*.part")) == []


def test_text_create_edit_escape_archive_restore_and_preview(web_owner, db, make_account):
    p, (account,) = setup_plan(web_owner, db, make_account)
    r = post(
        web_owner,
        "/library/text",
        {
            "profile_id": str(p.profile_id),
            "title": "Synthetic post",
            "caption": "<script>alert(1)</script>\nA real line",
        },
    )
    item = db.get(Video, int(r.headers["location"].split("/")[-1]))
    assert item.media_kind == "text" and item.encoded_path is None and item.status == VideoStatus.READY
    html = web_owner.get(r.headers["location"]).text
    assert "&lt;script&gt;" in html and "<script>alert(1)</script>" not in html
    assert web_owner.get(f"/library/{item.id}/preview").status_code == 404
    assert assignments(simulate(db, p, now=NOW), account)[0][1].video.id == item.id
    assert (
        post(
            web_owner,
            f"/library/{item.id}/edit",
            {"caption": "Updated body", "content_format": "post", "platform_facebook": "1"},
        ).status_code
        == 303
    )
    db.refresh(item)
    assert content.caption(item) == "Updated body"
    assert post(web_owner, f"/library/{item.id}/archive", {}).status_code == 303
    db.refresh(item)
    assert item.status == VideoStatus.ARCHIVED
    assert post(web_owner, f"/library/{item.id}/restore", {}).status_code == 303
    db.refresh(item)
    assert item.status == VideoStatus.READY


@pytest.mark.parametrize("caption", ["   ", "x" * 5001])
def test_invalid_text_is_not_saved(web_owner, db, caption):
    p = create_plan(web_owner, db)
    post(web_owner, "/library/text", {"profile_id": str(p.profile_id), "title": "x", "caption": caption})
    assert db.scalar(select(func.count()).select_from(Video)) == 0


@pytest.mark.parametrize(
    "kind,placement,platform,extra,expected",
    [
        ("image", "post", Platform.FACEBOOK, {}, None),
        ("text", "post", Platform.FACEBOOK, {}, None),
        ("text", "post", Platform.INSTAGRAM, {}, "unsupported"),
        ("image", "post", Platform.YOUTUBE, {}, "unsupported"),
        ("image", "post", Platform.TIKTOK, {}, "unsupported"),
        ("image", "story", Platform.INSTAGRAM, {}, "business_required"),
        ("image", "story", Platform.INSTAGRAM, {"account_type": "MEDIA_CREATOR"}, "business_required"),
        ("image", "story", Platform.INSTAGRAM, {"account_type": "BUSINESS"}, None),
        ("video", "story", Platform.FACEBOOK, {}, None),
        ("video", "story", Platform.YOUTUBE, {}, "unsupported"),
        ("image", "post", Platform.FACEBOOK, {"can_manage": False}, "permission"),
    ],
)
def test_platform_matrix(kind, placement, platform, extra, expected):
    from repeater.models import ConnectedAccount

    item = Video(
        media_kind=kind,
        content_format=placement,
        width=900,
        height=1600,
        duration_s=30,
        encoded_path="fake",
        caption="hello",
        overrides={},
    )
    account = ConnectedAccount(platform=platform, extra=extra)
    assert content.problem(item, account) == expected


def test_story_limits_and_instagram_image_ratio():
    from repeater.models import ConnectedAccount

    a = ConnectedAccount(platform=Platform.INSTAGRAM, extra={"account_type": "BUSINESS"})
    item = Video(
        media_kind="image", content_format="post", width=900, height=1600, encoded_path="fake", caption="hi"
    )
    assert content.problem(item, a) == "image_ratio"
    item.width = 1600
    assert content.problem(item, a) is None
    item.caption = "x" * 2201
    assert content.problem(item, a) == "caption_length"
    item.content_format, item.media_kind, item.duration_s = "story", "video", 61
    assert content.problem(item, a) == "story_vertical"
    item.width = 900
    assert content.problem(item, a) == "story_duration"
    item.duration_s, item.encoded_size_bytes = 30, 101 * 1024 * 1024
    assert content.problem(item, a) == "story_size"


def test_mixed_rotation_is_fair_with_shared_cooldown_and_format_changes(web_owner, db, make_account):
    p, (account,) = setup_plan(web_owner, db, make_account, days=3)
    one = video(db, p, "Video")
    two = video(db, p, "Image", media_kind="image", content_format="story")
    three = video(db, p, "Text", media_kind="text", content_format="post", caption="Body")
    three.encoded_path = None
    db.flush()
    entries = assignments(simulate(db, p, now=NOW), account)
    assert [e.video.id for _, e in entries[:6]] == [one.id, two.id, three.id] * 2
    history(db, p, account, two, NOW - timedelta(hours=1))
    two.content_format = "post"
    entries = assignments(simulate(db, p, now=NOW), account)
    assert all(e.video != two for _, e in entries[:3])
    assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_incompatible_formats_explained_in_schedule_and_detail(web_owner, db, make_account):
    p = create_plan(web_owner, db)
    a = make_account(Platform.YOUTUBE)
    save(web_owner, db, p, "targets", {"targets": [str(a.id)]})
    item = video(db, p, "Synthetic text", media_kind="text", content_format="post", caption="Body")
    item.encoded_path = None
    db.commit()
    result = simulate(db, p)
    assert result["filled"] == 0 and result["incompatibilities"][0]["reason"] == "unsupported"
    set_lang(web_owner, "en")
    assert "Format exclusions (1)" in web_owner.get(f"/plans/{p.id}").text
    assert "not available for this platform" in web_owner.get(f"/library/{item.id}").text
    for lang in ("en", "ar"):
        set_lang(web_owner, lang)
        assert "media.unsupported" not in web_owner.get(f"/plans/{p.id}").text


def test_duplicate_image_does_not_reenter_rotation(web_owner, db):
    p = create_plan(web_owner, db)
    one = finish(db, upload(web_owner, p.profile_id, raster(), "image_post"))
    two = finish(db, upload(web_owner, p.profile_id, raster(), "image_story"))
    assert two.status == VideoStatus.DUPLICATE and two.duplicate_of_id == one.id
    q = create_plan(web_owner, db, "Other person")
    third = finish(db, upload(web_owner, q.profile_id, raster()))
    assert third.status == VideoStatus.READY


def test_drive_images_opt_in_shortcuts_download_and_dedup(web_owner, db, make_account, mock_http):
    p = create_plan(web_owner, db)
    a = make_account(Platform.GDRIVE)
    folder_mock(mock_http)
    save(
        web_owner,
        db,
        p,
        "source",
        {"drive_id": str(a.id), "folder": "test_folder_123456", "backfill": "30", "include_images": "1"},
    )
    db.expire_all()
    source = db.get(Source, p.source_id)
    assert source.config["include_images"] is True
    files = [
        dict(_drive_file(1, NOW), mimeType="image/png", name="photo.png"),
        dict(
            _drive_file(2, NOW),
            mimeType=drive.SHORTCUT_MIME,
            shortcutDetails={
                "targetId": "target3",
                "targetMimeType": "image/webp",
                "targetResourceKey": "key",
            },
        ),
    ]
    listing = mock_http.get(f"{drive.API_BASE}/files").respond(200, json={"files": files})
    assert drive.lister.list_recent(ctx(), config={"folder_id": FOLDER_ID}) == []
    remotes = drive.lister.list_recent(ctx(), config={"folder_id": FOLDER_ID, "include_images": True})
    assert len(remotes) == 2 and not remotes[0].is_video
    assert "image/jpeg" in listing.calls[-1].request.url.params["q"]
    assert importer._ingest(db, source, remotes, NOW) == 2
    db.commit()
    rows = list(db.scalars(select(Video).order_by(Video.id)))
    assert all(v.media_kind == "image" and v.profile_id == p.profile_id for v in rows)
    # Download with mocked HTTP, including the shortcut resource-key header.
    download = mock_http.get(drive.file_download_url("target3")).respond(200, content=raster())
    dest = storage.ensure_video_dir(p.workspace_id, rows[0].id) / "mock.image"
    drive.lister.download(ctx(), remotes[1], dest)
    assert download.calls[0].request.headers[drive.RESOURCE_KEYS_HEADER] == "target3/key"
    assert dest.read_bytes() == raster()
    assert importer._ingest(db, source, remotes, NOW + timedelta(minutes=15)) == 0


@pytest.mark.parametrize("enabled", [False, True])
def test_rich_content_never_reaches_a_provider_even_if_global_lock_changes(
    web_owner, db, make_account, monkeypatch, mock_http, enabled
):
    p, (account,) = setup_plan(web_owner, db, make_account)
    item = video(db, p, "Image", media_kind="image", content_format="story")
    db.commit()
    monkeypatch.setattr(get_settings(), "publishing_enabled", enabled)
    with pytest.raises(ValueError):
        actions.schedule_video_now(db, item, account, actor="test")
    record = history(db, p, account, item, NOW, status=PostStatus.SCHEDULED)
    record.created_by = "user"
    db.commit()
    publisher.publish_post(db, record.id, now=NOW)
    db.refresh(record)
    assert record.status != PostStatus.PUBLISHED
    assert not mock_http.calls


def test_database_rejects_invalid_kind_placement_or_unowned_images(db, workspace):
    for kind, placement, profile in [
        ("image", "reel", None),
        ("text", "story", None),
        ("image", "post", None),
    ]:
        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(
                Video(
                    workspace_id=workspace.id, media_kind=kind, content_format=placement, profile_id=profile
                )
            )
            db.flush()


def test_meta_remembers_business_type_for_story_eligibility():
    from repeater.platforms.meta import _instagram_account

    account = _instagram_account(
        {"id": "fake-page", "instagram_business_account": {"id": "fake-ig", "account_type": "BUSINESS"}},
        "fake-token",
        "fake-user",
        [],
    )
    assert account.extra["account_type"] == "BUSINESS"


def test_signed_video_endpoint_never_serves_images(web_owner, web_client, db):
    from repeater import crypto

    p = create_plan(web_owner, db)
    item = finish(db, upload(web_owner, p.profile_id, raster()))
    assert web_client.get(crypto.signed_media_path(item.id)).status_code == 404


def test_drive_image_worker_completes_and_retry_retains_size_bound(
    web_owner, db, make_account, mock_http, monkeypatch
):
    from repeater.platforms.base import PermanentError

    p = create_plan(web_owner, db)
    a = make_account(Platform.GDRIVE)
    src = Source(
        workspace_id=p.workspace_id,
        profile_id=p.profile_id,
        connected_account_id=a.id,
        kind=SourceKind.DRIVE,
        platform=Platform.GDRIVE,
        name="Synthetic image folder",
        config={"folder_id": FOLDER_ID, "backfill_count": 30, "include_images": True},
        enabled=False,
    )
    db.add(src)
    db.flush()
    remote = drive._remote_video(
        dict(_drive_file(8, NOW), mimeType="image/png", name="image.png"), include_images=True
    )
    importer._ingest(db, src, [remote], NOW)
    db.commit()
    item = db.scalar(select(Video))
    # No short-lived listing hints: a retried image must still use the image download bound.
    mock_http.get(
        f"{drive.API_BASE}/files/{remote.external_id}",
        params={"fields": drive.SHORTCUT_FIELDS, "supportsAllDrives": "true"},
    ).respond(200, json={"id": "file8", "mimeType": "image/png"})
    mock_http.get(drive.file_download_url(remote.external_id)).respond(200, content=raster())
    monkeypatch.setattr(importer, "fresh_context", lambda *args, **kw: ctx())
    importer.import_video(db, item.id, {})
    db.refresh(item)
    assert item.status == VideoStatus.PROCESSING
    importer.process_video(db, item.id)
    assert item.status == VideoStatus.READY and item.encoded_path.endswith("image.jpg")
    assert item.content_format == "post"
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 10)
    with pytest.raises(PermanentError):
        importer._download_original(db, item, {})


def test_changing_drive_image_option_drops_inflight_listing(web_owner, db, make_account, monkeypatch):
    from repeater.db import get_sessionmaker

    p = create_plan(web_owner, db)
    a = make_account(Platform.GDRIVE)
    src = Source(
        workspace_id=p.workspace_id,
        profile_id=p.profile_id,
        connected_account_id=a.id,
        kind=SourceKind.DRIVE,
        platform=Platform.GDRIVE,
        name="Synthetic folder",
        config={"folder_id": FOLDER_ID, "backfill_count": 30, "include_images": True},
        enabled=True,
    )
    db.add(src)
    db.commit()
    remote = drive._remote_video(dict(_drive_file(9, NOW), mimeType="image/png"), include_images=True)

    def change_during_listing(session, source):
        with get_sessionmaker()() as other:
            changed = other.get(Source, source.id)
            changed.config = {**changed.config, "include_images": False}
            other.commit()
        return [remote]

    monkeypatch.setattr(importer, "_detect", change_during_listing)
    assert importer.poll_source(db, src.id, now=NOW).status == "skipped"
    db.refresh(src)
    assert src.config["include_images"] is False and "baseline_at" not in src.config
    assert db.scalar(select(func.count()).select_from(Video)) == 0


def test_saving_drive_image_option_preserves_existing_checkpoint(web_owner, db, make_account, mock_http):
    p = create_plan(web_owner, db)
    a = make_account(Platform.GDRIVE)
    folder_mock(mock_http)
    data = {"drive_id": str(a.id), "folder": "test_folder_123456", "backfill": "30"}
    save(web_owner, db, p, "source", data)
    db.expire_all()
    src = db.get(Source, p.source_id)
    src.config = {**src.config, "seen_ids": ["old-video"], "baseline_at": NOW.isoformat()}
    src.last_polled_at = NOW
    src.enabled = True
    db.commit()
    save(web_owner, db, p, "source", {**data, "include_images": "1"})
    db.refresh(src)
    assert src.config["include_images"] is True and src.config["seen_ids"] == ["old-video"]
    assert src.config["baseline_at"] == NOW.isoformat()


def _submitted_forms(html):
    """Read native form controls as a browser submission would (unchecked boxes are absent)."""
    from html.parser import HTMLParser

    class Forms(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms = []
            self.current = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form":
                self.current = (attrs.get("action"), {})
                self.forms.append(self.current)
            elif tag == "input" and self.current and attrs.get("name"):
                if attrs.get("type") != "checkbox" or "checked" in attrs:
                    self.current[1][attrs["name"]] = attrs.get("value", "")

        def handle_endtag(self, tag):
            if tag == "form":
                self.current = None

    parser = Forms()
    parser.feed(html)
    return parser.forms


@pytest.mark.parametrize("existing,selected", [(True, False), (False, True)])
def test_folder_browse_pagination_and_choice_keep_explicit_image_selection(
    web_owner, db, make_account, mock_http, existing, selected
):
    from httpx import Response

    from tests.test_content_plans import FOLDER

    p = create_plan(web_owner, db)
    a = make_account(Platform.GDRIVE)
    folder_mock(mock_http)
    data = {"drive_id": str(a.id), "folder": FOLDER, "backfill": "7"}
    if existing:
        save(web_owner, db, p, "source", {**data, "include_images": "1"})
    mock_http.get(f"{drive.API_BASE}/files").mock(
        side_effect=[
            Response(
                200, json={"files": [{"id": FOLDER, "name": "Synthetic folder"}], "nextPageToken": "next"}
            ),
            Response(200, json={"files": [{"id": FOLDER, "name": "Synthetic folder"}]}),
        ]
    )
    response = post(
        web_owner, f"/plans/{p.id}/folders", {**data, **({"include_images": "1"} if selected else {})}
    )
    assert response.status_code == 200
    forms = _submitted_forms(response.text)
    visible = next(fields for action, fields in forms if action == f"/plans/{p.id}/source")
    assert (visible.get("include_images") == "1") is selected
    pagination = next(
        fields for action, fields in forms if action == f"/plans/{p.id}/folders" and "page_token" in fields
    )
    assert pagination["include_images"] == ("1" if selected else "0")
    response = post(web_owner, f"/plans/{p.id}/folders", pagination)
    choices = [
        fields for action, fields in _submitted_forms(response.text) if action == f"/plans/{p.id}/source"
    ]
    assert len(choices) == 2
    chosen = choices[-1]
    assert (chosen.get("include_images") == "1") is selected
    assert chosen["backfill"] == "7"
    assert post(web_owner, f"/plans/{p.id}/source", chosen).status_code == 303
    db.expire_all()
    assert db.get(Source, p.source_id).config["include_images"] is selected
