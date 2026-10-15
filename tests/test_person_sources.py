"""Phase three uses synthetic accounts, fake media and mocked HTTP only."""

from datetime import timedelta

import pytest
import respx
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from repeater.config import get_settings
from repeater.content_plans import simulate
from repeater.db import get_sessionmaker
from repeater.models import (
    AccountStatus,
    ConnectedAccount,
    Job,
    Platform,
    ScheduledPost,
    Source,
    SourceKind,
    Video,
    VideoStatus,
    Workspace,
)
from repeater.platforms import facebook, instagram, meta, override
from repeater.platforms.base import AuthError, PermanentError, RetryableError
from repeater.sources import importer
from repeater.timeutil import utcnow
from tests.test_content_plans import create_plan, save
from tests.test_sources import FakeLister, remote
from tests.test_web_support import post, set_lang

pytest_plugins = ["tests.test_web_support"]


@pytest.fixture(autouse=True)
def mock_network(monkeypatch):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    monkeypatch.setattr(importer.notify_mod, "notify", lambda *a, **k: False)
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def add(client, db, plan, account, mode="latest30"):
    r = save(client, db, plan, "platforms", {"source_account_id": account.id, "import_mode": mode})
    assert r.status_code == 303, r.text[:1000]
    db.expire_all()
    return db.scalar(
        select(Source).where(Source.profile_id == plan.profile_id, Source.connected_account_id == account.id)
    )


def control(client, db, plan, source, action):
    db.refresh(plan)
    response = post(
        client, f"/plans/{plan.id}/platforms/{source.id}", {"revision": plan.revision, "action": action}
    )
    db.expire_all()
    return response


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize("platform", [Platform.FACEBOOK, Platform.INSTAGRAM])
def test_save_and_control_are_explicit_idempotent_and_safe(
    web_owner, db, make_account, mock_network, platform
):
    p = create_plan(web_owner, db)
    a = make_account(platform)
    s = add(web_owner, db, p, a)
    assert not s.enabled and s.profile_id == p.profile_id
    assert s.config == {"backfill_count": 30}
    assert db.get(ConnectedAccount, a.id).profile_id == p.profile_id
    assert count(db, Job) == count(db, Video) == count(db, ScheduledPost) == 0
    assert control(web_owner, db, p, s, "check").status_code == 422
    for action in ["start", "check", "start"]:
        assert control(web_owner, db, p, s, action).status_code == 303
    assert count(db, Job) == 1
    assert db.scalar(select(Job)).kind == "poll_source"
    assert control(web_owner, db, p, s, "pause").status_code == 303
    db.refresh(s)
    assert not s.enabled
    assert importer.poll_source(db, s.id).status == "skipped"
    assert count(db, ScheduledPost) == 0 and not mock_network.calls
    assert (
        web_owner.get(f"/sources/{s.id}/edit", follow_redirects=False).headers["location"]
        == f"/plans/{p.id}/edit#platforms"
    )
    assert f"/plans/{p.id}/edit#platforms" in web_owner.get("/sources").text
    assert (
        save(web_owner, db, p, "platforms", {"source_account_id": a.id, "import_mode": "new"}).status_code
        == 422
    )
    assert count(db, Source) == 1


@pytest.mark.parametrize(
    "problem",
    [
        "person",
        "workspace",
        "disabled",
        "reconnect",
        "youtube",
        "tiktok",
        "drive",
        "mode",
        "legacy",
        "pending",
    ],
)
def test_rejects_unsupported_foreign_or_busy_source(web_owner, db, make_account, problem):
    p = create_plan(web_owner, db)
    a = make_account(Platform.FACEBOOK)
    if problem == "person":
        other = create_plan(web_owner, db, "Other person")
        a.profile_id = other.profile_id
    elif problem == "workspace":
        ws = Workspace(name="Other tenant")
        db.add(ws)
        db.flush()
        a.workspace_id = ws.id
    elif problem in {"disabled", "reconnect"}:
        a.status = AccountStatus.DISABLED if problem == "disabled" else AccountStatus.NEEDS_RECONNECT
    elif problem in {"youtube", "tiktok", "drive"}:
        a.platform = Platform.GDRIVE if problem == "drive" else Platform(problem)
    elif problem == "legacy":
        db.add(
            Source(
                workspace_id=p.workspace_id,
                connected_account_id=a.id,
                platform=a.platform,
                kind=SourceKind.PLATFORM_API,
                enabled=True,
            )
        )
    elif problem == "pending":
        v = Video(workspace_id=p.workspace_id, title="Old pending")
        db.add(v)
        db.flush()
        db.add(
            ScheduledPost(
                workspace_id=p.workspace_id,
                video_id=v.id,
                connected_account_id=a.id,
                platform=a.platform,
                scheduled_at=utcnow(),
                idempotency_key="pending",
            )
        )
    db.commit()
    n = count(db, Source)
    r = save(
        web_owner,
        db,
        p,
        "platforms",
        {"source_account_id": a.id, "import_mode": "invalid" if problem == "mode" else "latest30"},
    )
    assert r.status_code == 422 and count(db, Source) == n
    assert count(db, Job) == 0


def test_cross_person_control_revision_csrf_and_unique_constraint(web_owner, db, make_account):
    p = create_plan(web_owner, db)
    other = create_plan(web_owner, db, "Another")
    a = make_account(Platform.INSTAGRAM)
    s = add(web_owner, db, p, a)
    assert control(web_owner, db, other, s, "start").status_code == 404
    path = f"/plans/{p.id}/platforms/{s.id}"
    assert web_owner.post(path, data={"revision": p.revision, "action": "start"}).status_code == 403
    assert post(web_owner, path, {"revision": p.revision - 1, "action": "start"}).status_code == 422
    assert control(web_owner, db, p, s, "delete").status_code == 422
    assert post(web_owner, f"/sources/{s.id}/toggle").status_code == 404
    db.add(
        Source(
            workspace_id=p.workspace_id,
            profile_id=p.profile_id,
            connected_account_id=a.id,
            platform=a.platform,
            kind=SourceKind.PLATFORM_API,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    assert count(db, Job) == 0


@pytest.mark.parametrize("platform", [Platform.INSTAGRAM, Platform.FACEBOOK])
def test_last30_then_follow_more_than50_new_videos_and_rotate(web_owner, db, make_account, platform):
    p = create_plan(web_owner, db)
    origin = make_account(platform)
    destination = make_account(Platform.YOUTUBE)
    s = add(web_owner, db, p, origin)
    save(web_owner, db, p, "targets", {"targets": [str(origin.id), str(destination.id)]})
    save(web_owner, db, p, "rotation", {"rotation_mode": "repeat", "repeat_after_days": 7})
    assert control(web_owner, db, p, s, "start").status_code == 303
    now = utcnow()
    items = [remote(str(i), now - timedelta(days=i + 1)) for i in range(45)]
    fake = FakeLister(platform, items)
    with override("lister", platform, fake):
        first = importer.poll_source(db, s.id, now=now)
        assert first.created == 30
        assert importer.poll_source(db, s.id, now=now + timedelta(minutes=15)).created == 0
        fake.items = [remote(f"new-{i}", now + timedelta(minutes=30, seconds=i)) for i in range(60)]
        fake.items += items
        assert importer.poll_source(db, s.id, now=now + timedelta(hours=1)).created == 60
        assert control(web_owner, db, p, s, "pause").status_code == 303
        assert importer.poll_source(db, s.id).status == "skipped"
        assert control(web_owner, db, p, s, "start").status_code == 303
        assert importer.poll_source(db, s.id, now=now + timedelta(hours=2)).created == 0
    assert count(db, Video) == 90
    assert all(
        v.profile_id == p.profile_id and v.source_account_id == origin.id for v in db.scalars(select(Video))
    )
    assert fake.calls[1]["limit"] == 1001
    assert fake.calls[1]["config"]["require_complete"] is True
    assert simulate(db, p)["filled"] == 0  # importing files never enter the preview
    v = db.scalar(select(Video).where(Video.external_id == "new-0"))
    v.status, v.encoded_path, v.duration_s, v.width, v.height = VideoStatus.READY, "fake.mp4", 10, 1080, 1920
    db.commit()
    result = simulate(db, p)
    assert result["ready"] == 1 and result["filled"] > 1
    entries = [e for row in result["rows"] for e in row.entries if e.video]
    assert all(e.account.id == destination.id and e.video.id == v.id for e in entries)
    assert count(db, ScheduledPost) == 0
    assert {j.kind for j in db.scalars(select(Job))} <= {"poll_source", "import_video"}


def test_new_only_includes_videos_between_start_and_first_worker_check(web_owner, db, make_account):
    p = create_plan(web_owner, db)
    s = add(web_owner, db, p, make_account(Platform.INSTAGRAM), "new")
    control(web_owner, db, p, s, "start")
    db.refresh(s)
    start = importer._parse_iso(s.config["watch_from"])
    fake = FakeLister(
        s.platform,
        [remote("before", start - timedelta(seconds=1))]
        + [remote(f"new-{i}", start + timedelta(seconds=i + 1)) for i in range(60)],
    )
    with override("lister", s.platform, fake):
        assert importer.poll_source(db, s.id, now=start + timedelta(minutes=20)).created == 60
    assert fake.calls[0]["since"] == start
    assert db.scalar(select(Video.id).where(Video.external_id == "before")) is None


@pytest.mark.parametrize(
    "error", [AuthError("Reconnect"), RetryableError("Rate limited"), PermanentError("Permission missing")]
)
def test_failed_checks_do_not_advance_and_pause_after_three(web_owner, db, make_account, error):
    p = create_plan(web_owner, db)
    a = make_account(Platform.INSTAGRAM)
    s = add(web_owner, db, p, a)
    control(web_owner, db, p, s, "start")
    fake = FakeLister(s.platform)
    fake.list_error = error
    with override("lister", s.platform, fake):
        for _ in range(3):
            assert importer.poll_source(db, s.id).status == "failed"
    db.refresh(s)
    assert not s.enabled and s.last_success_at is None and "baseline_at" not in s.config
    assert count(db, Video) == count(db, ScheduledPost) == 0
    a.status = AccountStatus.ACTIVE
    db.commit()
    assert control(web_owner, db, p, s, "start").status_code == 303
    db.refresh(s)
    assert s.consecutive_failures == 0


def test_saturated_window_does_not_silently_advance_checkpoint(web_owner, db, make_account):
    p = create_plan(web_owner, db)
    s = add(web_owner, db, p, make_account(Platform.INSTAGRAM), "new")
    control(web_owner, db, p, s, "start")
    now = utcnow()
    fake = FakeLister(s.platform, [remote(str(i), now) for i in range(1001)])
    with override("lister", s.platform, fake):
        assert importer.poll_source(db, s.id).status == "failed"
    assert s.last_success_at is None and "baseline_at" not in s.config
    assert count(db, Video) == 0


def test_pause_resume_during_slow_listing_drops_old_result(web_owner, db, make_account):
    p = create_plan(web_owner, db)
    s = add(web_owner, db, p, make_account(Platform.INSTAGRAM))
    control(web_owner, db, p, s, "start")
    source_id = s.id

    class PausedLister(FakeLister):
        def list_recent(self, *args, **kwargs):
            with get_sessionmaker()() as other:
                row = other.get(Source, source_id)
                row.enabled = True  # pause then resume before the original check returned
                row.config = {**row.config, "control_revision": 999}
                other.commit()
            return [remote("stale", utcnow())]

    with override("lister", s.platform, PausedLister(s.platform)):
        assert importer.poll_source(db, source_id).status == "skipped"
    assert count(db, Video) == 0 and s.config["control_revision"] == 999


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_multiple_sources_status_empty_and_escaped_errors(web_owner, db, make_account, lang):
    p = create_plan(web_owner, db)
    a = make_account(Platform.INSTAGRAM)
    s = add(web_owner, db, p, a)
    add(web_owner, db, p, make_account(Platform.FACEBOOK), "new")
    s.last_error = "<script>alert(1)</script>"
    db.commit()
    set_lang(web_owner, lang)
    r = web_owner.get(f"/plans/{p.id}/edit")
    assert r.status_code == 200 and '<section class="card" id="platforms">' in r.text
    assert "<script>alert(1)</script>" not in r.text
    assert 'name="action" value="start"' in r.text
    assert web_owner.get(f"/plans/{p.id}").status_code == 200
    assert count(db, ScheduledPost) == 0


def test_instagram_paginates_past_photos_and_facebook_deduplicates_edges(mock_network):
    from tests.test_platform_facebook import REELS_URL, VIDEOS_URL
    from tests.test_platform_facebook import ctx as fb_ctx
    from tests.test_platform_instagram import MEDIA_URL
    from tests.test_platform_instagram import ctx as ig_ctx

    now = utcnow()
    images = [{"id": f"photo{i}", "media_type": "IMAGE"} for i in range(50)]
    items = [
        {"id": str(i), "media_type": "VIDEO", "timestamp": (now - timedelta(days=i)).isoformat()}
        for i in range(35)
    ]
    route = mock_network.get(MEDIA_URL).mock(
        side_effect=[
            Response(
                200,
                json={
                    "data": images,
                    "paging": {"next": f"{MEDIA_URL}?after=next", "cursors": {"after": "next"}},
                },
            ),
            Response(200, json={"data": items}),
        ]
    )
    result = instagram.lister.list_recent(ig_ctx(), limit=30, config={"require_complete": True})
    assert len(result) == 30 and route.call_count == 2
    fb_items = [{"id": str(i), "created_time": (now - timedelta(days=i)).isoformat()} for i in range(35)]
    mock_network.get(REELS_URL).respond(json={"data": fb_items})
    mock_network.get(VIDEOS_URL).respond(json={"data": fb_items})
    result = facebook.lister.list_recent(fb_ctx(), limit=30, config={"require_complete": True})
    assert len(result) == len({v.external_id for v in result}) == 30
    assert all(c.request.method == "GET" for c in mock_network.calls)


def test_incomplete_pagination_is_reported_not_accepted(mock_network):
    url = "https://graph.facebook.com/v26.0/123/media"
    mock_network.get(url).respond(
        json={"data": [{"id": "1"}], "paging": {"next": url + "?after=next", "cursors": {"after": "next"}}}
    )
    with meta.Graph("synthetic-token") as graph:
        with pytest.raises(RetryableError, match="page limit"):
            list(graph.paginate("123/media", max_pages=1, action="test", require_complete=True))
        with pytest.raises(RetryableError, match="cursor"):
            list(graph.paginate("123/media", max_pages=3, action="test", require_complete=True))


def test_mock_download_and_preparation_feed_rotation_without_publication(
    web_owner, db, make_account, monkeypatch, tmp_path
):
    from repeater.media.fingerprint import Fingerprint
    from repeater.media.probe import ProbeResult

    p = create_plan(web_owner, db)
    source_account = make_account(Platform.INSTAGRAM)
    target = make_account(Platform.FACEBOOK)
    s = add(web_owner, db, p, source_account)
    save(web_owner, db, p, "targets", {"targets": [str(target.id)]})
    control(web_owner, db, p, s, "start")
    sample = tmp_path / "synthetic.mp4"
    sample.write_bytes(b"mock video, no real content")
    fake = FakeLister(s.platform, [remote("test-file", utcnow() - timedelta(days=1))], sample)
    info = ProbeResult(10, 1080, 1920, 30, "h264", None, False, 26)
    monkeypatch.setattr(importer, "probe", lambda p: info)
    monkeypatch.setattr(importer, "fingerprint_video", lambda *a: Fingerprint([], None))

    def encode(original, output, **kwargs):
        output.write_bytes(b"mock encoded video")
        return info

    monkeypatch.setattr(importer, "encode_for_platforms", encode)
    monkeypatch.setattr(importer, "make_thumbnail", lambda p, out, **kw: out.write_bytes(b"mock thumb"))
    with override("lister", s.platform, fake):
        assert importer.poll_source(db, s.id).created == 1
        v = db.scalar(select(Video))
        importer.import_video(db, v.id)
        db.refresh(v)
        assert v.status == VideoStatus.PROCESSING
        importer.process_video(db, v.id)
    db.refresh(v)
    assert v.status == VideoStatus.READY and v.profile_id == p.profile_id
    assert simulate(db, p)["filled"] == 1
    assert count(db, ScheduledPost) == 0
    assert not any(j.kind in {"publish_post", "poll_publish"} for j in db.scalars(select(Job)))


def test_tenant_boundary_and_automatic_due_poll(web_owner, db, make_account):
    p = create_plan(web_owner, db)
    s = add(web_owner, db, p, make_account(Platform.FACEBOOK))
    ws = Workspace(name="Foreign tenant")
    db.add(ws)
    db.flush()
    from repeater.models import ContentPlan, CreatorProfile

    person = CreatorProfile(workspace_id=ws.id, name="Private")
    db.add(person)
    db.flush()
    other = ContentPlan(workspace_id=ws.id, profile_id=person.id, timezone="UTC", start_date=utcnow().date())
    db.add(other)
    db.commit()
    assert web_owner.get(f"/plans/{other.id}/edit").status_code == 404
    assert control(web_owner, db, other, s, "start").status_code == 404
    assert importer.enqueue_due_polls(db) == 0
    control(web_owner, db, p, s, "start")
    # A queued source poll is deduplicated by the timer; there can be only one live poll.
    importer.enqueue_due_polls(db)
    assert count(db, Job) == 1
    s.last_polled_at = utcnow()
    db.commit()
    assert s.id not in [row.id for row in importer.due_sources(db)]
    assert s.id in [row.id for row in importer.due_sources(db, now=utcnow() + timedelta(minutes=16))]
    assert count(db, ScheduledPost) == 0


def test_worker_rejects_source_account_from_different_person(web_owner, db, make_account, mock_network):
    p = create_plan(web_owner, db)
    a = make_account(Platform.INSTAGRAM)
    s = add(web_owner, db, p, a)
    control(web_owner, db, p, s, "start")
    other = create_plan(web_owner, db, "Other")
    a.profile_id = other.profile_id
    db.commit()
    assert importer.poll_source(db, s.id).status == "failed"
    assert not mock_network.calls and count(db, Video) == 0


def test_overlap_discovers_delayed_video_without_reimporting_known_content(web_owner, db, make_account):
    p = create_plan(web_owner, db)
    s = add(web_owner, db, p, make_account(Platform.FACEBOOK), "new")
    control(web_owner, db, p, s, "start")
    now = utcnow()
    fake = FakeLister(s.platform, [remote("first", now + timedelta(hours=1))])
    with override("lister", s.platform, fake):
        assert importer.poll_source(db, s.id, now=now + timedelta(hours=2)).created == 1
        fake.items += [remote("late-visible", now + timedelta(minutes=30))]
        assert importer.poll_source(db, s.id, now=now + timedelta(hours=3)).created == 1
        assert importer.poll_source(db, s.id, now=now + timedelta(hours=4)).created == 0
    assert count(db, Video) == 2
