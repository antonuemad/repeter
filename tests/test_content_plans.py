"""Synthetic people, fake OAuth and mocked Drive only. Never contact or publish to real accounts."""

from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from repeater.config import get_settings
from repeater.content_plans import simulate, slots
from repeater.models import (
    AccountStatus,
    ConnectedAccount,
    ContentPlan,
    CreatorProfile,
    Job,
    OAuthState,
    PlanTarget,
    Platform,
    ScheduledPost,
    Source,
    SourceKind,
    Video,
    VideoStatus,
    Workspace,
)
from repeater.platforms import override
from repeater.platforms.base import RemoteVideo
from repeater.sources import drive, importer
from repeater.timeutil import utcnow
from tests.test_web_oauth import FakeProvider, _meta_accounts
from tests.test_web_support import post, set_lang

pytest_plugins = ["tests.test_web_support"]
FOLDER = "test_folder_123456"


@pytest.fixture(autouse=True)
def network_is_mocked():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def create_plan(client, db, name="Synthetic creator"):
    response = post(client, "/plans", {"name": name})
    assert response.status_code == 303
    return db.get(ContentPlan, int(response.headers["location"].split("/")[2]))


def save(client, db, plan, section, data):
    db.refresh(plan)
    return post(client, f"/plans/{plan.id}/{section}", {"revision": str(plan.revision), **data})


def folder_mock(mock, *, status=200, **extra):
    return mock.get(f"{drive.API_BASE}/files/{FOLDER}").mock(
        return_value=Response(
            status, json={"id": FOLDER, "name": "Creator originals", "mimeType": drive.FOLDER_MIME, **extra}
        )
    )


def video(db, plan, title, **kwargs):
    v = Video(
        workspace_id=plan.workspace_id,
        profile_id=plan.profile_id,
        title=title,
        status=VideoStatus.READY,
        encoded_path="synthetic.mp4",
        duration_s=10,
        width=1080,
        height=1920,
        **kwargs,
    )
    db.add(v)
    db.flush()
    return v


@pytest.mark.parametrize("lock", [False, True])
def test_complete_draft_flow_never_enqueues_or_publishes(
    web_owner, db, make_account, monkeypatch, network_is_mocked, lock
):
    monkeypatch.setattr(get_settings(), "publishing_enabled", lock)
    p = create_plan(web_owner, db)
    a = make_account(Platform.FACEBOOK)
    g = make_account(Platform.GDRIVE)
    call = folder_mock(network_is_mocked)
    assert (
        save(
            web_owner,
            db,
            p,
            "source",
            {"drive_id": g.id, "folder": f"https://drive.google.com/drive/folders/{FOLDER}", "backfill": 30},
        ).status_code
        == 303
    )
    assert call.call_count == 1
    assert save(web_owner, db, p, "targets", {"targets": [str(a.id)]}).status_code == 303
    today = utcnow().date()
    assert (
        save(
            web_owner,
            db,
            p,
            "schedule",
            {
                "timezone": "UTC",
                "start_date": (today + timedelta(days=1)).isoformat(),
                "weekdays": [str(i) for i in range(7)],
                "times": ["10:00", "18:00"],
                "ordering": "oldest",
            },
        ).status_code
        == 303
    )
    set_lang(web_owner, "en")
    preview = web_owner.get(f"/plans/{p.id}")
    assert preview.status_code == 200
    assert "Waiting for an eligible video" in preview.text
    db.expire_all()
    assert db.get(ConnectedAccount, a.id).profile_id == p.profile_id
    assert not db.get(Source, p.source_id).enabled
    assert db.scalar(select(func.count()).select_from(Job)) == 0
    assert db.scalar(select(func.count()).select_from(ScheduledPost)) == 0
    assert db.scalar(select(func.count()).select_from(Video)) == 0
    assert len(network_is_mocked.calls) == 1


def test_explicit_import_and_pause_only_enqueue_source_poll(web_owner, db, make_account, network_is_mocked):
    p = create_plan(web_owner, db)
    g = make_account(Platform.GDRIVE)
    folder_mock(network_is_mocked)
    assert save(web_owner, db, p, "source", {"drive_id": g.id, "folder": FOLDER}).status_code == 303
    assert save(web_owner, db, p, "imports", {"action": "start"}).status_code == 303
    assert save(web_owner, db, p, "imports", {"action": "start"}).status_code == 303
    db.expire_all()
    assert db.get(Source, p.source_id).enabled
    jobs = list(db.scalars(select(Job)))
    assert len(jobs) == 1 and jobs[0].kind == "poll_source"
    assert save(web_owner, db, p, "imports", {"action": "pause"}).status_code == 303
    db.expire_all()
    assert not db.get(Source, p.source_id).enabled
    assert db.scalar(select(func.count()).select_from(ScheduledPost)) == 0
    assert db.scalar(select(func.count()).select_from(Video)) == 0


@pytest.mark.parametrize(
    "status,mime,trashed",
    [
        (403, drive.FOLDER_MIME, False),
        (404, drive.FOLDER_MIME, False),
        (200, "video/mp4", False),
        (200, drive.FOLDER_MIME, True),
    ],
)
def test_invalid_folder_never_saves_source(
    web_owner, db, make_account, network_is_mocked, status, mime, trashed
):
    p = create_plan(web_owner, db)
    g = make_account(Platform.GDRIVE)
    folder_mock(network_is_mocked, status=status, mimeType=mime, trashed=trashed)
    result = save(web_owner, db, p, "source", {"drive_id": g.id, "folder": FOLDER})
    assert result.status_code == 422
    db.refresh(p)
    assert p.source_id is None
    assert db.scalar(select(func.count()).select_from(Source)) == 0


def test_browse_folder_pagination_escapes_names_without_import(
    web_owner, db, make_account, network_is_mocked
):
    p = create_plan(web_owner, db)
    g = make_account(Platform.GDRIVE)
    call = network_is_mocked.get(f"{drive.API_BASE}/files").mock(
        return_value=Response(
            200, json={"files": [{"id": FOLDER, "name": "<script>bad</script>"}], "nextPageToken": "next123"}
        )
    )
    result = post(web_owner, f"/plans/{p.id}/folders", {"drive_id": g.id, "page_token": "first123"})
    assert result.status_code == 200 and "&lt;script&gt;bad" in result.text
    assert 'value="next123"' in result.text
    assert call.calls.last.request.url.params["pageToken"] == "first123"
    assert "video/" not in call.calls.last.request.url.params["q"]
    assert db.scalar(select(func.count()).select_from(Job)) == 0


@pytest.mark.parametrize("outcome", ["success", "cancel", "missing_code"])
def test_oauth_returns_to_exact_plan_and_preserves_ownership(web_owner, db, outcome):
    p = create_plan(web_owner, db)
    fake = FakeProvider("meta", False, _meta_accounts())
    path = f"/plans/{p.id}/edit"
    with override("oauth", "meta", fake):
        response = web_owner.get("/connect/meta", params={"return_to": path}, follow_redirects=False)
        state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
        assert db.get(OAuthState, state).return_path == path
        params = {"state": state}
        if outcome == "success":
            params["code"] = "fake"
        elif outcome == "cancel":
            params["error"] = "access_denied"
        response = web_owner.get("/oauth/meta/callback", params=params, follow_redirects=False)
    assert response.headers["location"] == path
    assert db.scalar(select(func.count()).select_from(Job)) == 0
    assert all(a.profile_id is None for a in db.scalars(select(ConnectedAccount)))


@pytest.mark.parametrize(
    "target",
    ["https://evil.example", "//evil.example", "/plans/999/edit", "/plans/1/edit?next=//evil.example"],
)
def test_oauth_return_is_not_an_open_redirect(web_owner, db, target):
    with override("oauth", "meta", FakeProvider("meta", False)):
        r = web_owner.get("/connect/meta", params={"return_to": target}, follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    assert db.get(OAuthState, state).return_path is None


def test_other_workspace_and_person_cannot_be_selected(web_owner, db, make_account):
    p = create_plan(web_owner, db)
    other = create_plan(web_owner, db, "Other person")
    a = make_account(Platform.FACEBOOK)
    assert save(web_owner, db, other, "targets", {"targets": [str(a.id)]}).status_code == 303
    assert save(web_owner, db, p, "targets", {"targets": [str(a.id)]}).status_code == 422
    ws = Workspace(name="Other workspace")
    db.add(ws)
    db.commit()
    foreign = make_account(Platform.FACEBOOK, workspace_id=ws.id)
    assert save(web_owner, db, p, "targets", {"targets": [str(foreign.id)]}).status_code == 422
    foreign_person = CreatorProfile(workspace_id=ws.id, name="Private")
    db.add(foreign_person)
    db.flush()
    foreign_plan = ContentPlan(
        workspace_id=ws.id, profile_id=foreign_person.id, timezone="UTC", start_date=date.today()
    )
    db.add(foreign_plan)
    db.commit()
    for path in [
        f"/plans/{foreign_plan.id}",
        f"/plans/{foreign_plan.id}/edit",
        f"/library?profile_id={foreign_person.id}",
    ]:
        assert web_owner.get(path).status_code == 404
    assert post(web_owner, f"/plans/{foreign_plan.id}/targets", {"revision": 1}).status_code == 404
    assert db.scalar(select(func.count()).select_from(PlanTarget).where(PlanTarget.plan_id == p.id)) == 0


def test_db_rejects_cross_workspace_profile_assignment(db, workspace, make_account):
    ws = Workspace(name="Other")
    db.add(ws)
    db.flush()
    person = CreatorProfile(workspace_id=ws.id, name="Other creator")
    db.add(person)
    db.flush()
    a = make_account(Platform.FACEBOOK)
    a.profile_id = person.id
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_stale_tab_does_not_overwrite_and_csrf_is_required(web_owner, db):
    p = create_plan(web_owner, db)
    assert save(web_owner, db, p, "person", {"name": "New name"}).status_code == 303
    r = post(web_owner, f"/plans/{p.id}/person", {"revision": 1, "name": "Stale name"})
    assert r.status_code == 422
    db.expire_all()
    assert db.get(CreatorProfile, p.profile_id).name == "New name"
    assert web_owner.post(f"/plans/{p.id}/imports", data={"action": "start"}).status_code == 403


@pytest.mark.parametrize(
    "changes",
    [
        {"times": ["25:00"]},
        {"times": ["10:00", "10:00"]},
        {"weekdays": []},
        {"weekdays": ["7"]},
        {"timezone": "Invalid/Zone"},
        {"start_date": "2000-01-01"},
        {"ordering": "random"},
        {"times": ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00"]},
    ],
)
def test_invalid_schedules_do_not_mutate(web_owner, db, changes):
    p = create_plan(web_owner, db)
    data = {
        "times": ["10:00"],
        "weekdays": ["0"],
        "timezone": "UTC",
        "start_date": (utcnow().date() + timedelta(days=1)).isoformat(),
        "ordering": "oldest",
        **changes,
    }
    assert save(web_owner, db, p, "schedule", data).status_code == 422
    db.refresh(p)
    assert p.revision == 1 and p.weekdays == list(range(7))


def test_dst_gaps_and_folds_and_past_times():
    p = ContentPlan(
        timezone="America/New_York", start_date=date(2026, 3, 8), weekdays=list(range(7)), times=["02:30"]
    )
    result = slots(p, now=datetime(2026, 3, 8, tzinfo=UTC))
    assert len(result) == 29 and result[0] == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)
    p.start_date, p.times = date(2026, 11, 1), ["01:30"]
    result = slots(p, now=datetime(2026, 11, 1, tzinfo=UTC))
    assert len(result) == 30 and result[0] == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    result = slots(p, now=datetime(2026, 11, 1, 6, tzinfo=UTC))
    assert len(result) == 29


def test_preview_isolates_library_and_orders_each_target_without_recycling(web_owner, db, make_account):
    p = create_plan(web_owner, db)
    other = create_plan(web_owner, db, "Another")
    a = make_account(Platform.FACEBOOK)
    b = make_account(Platform.INSTAGRAM)
    assert save(web_owner, db, p, "targets", {"targets": [str(a.id), str(b.id)]}).status_code == 303
    older = video(db, p, "Older", source_published_at=utcnow() - timedelta(days=2))
    newer = video(db, p, "Newer", source_published_at=utcnow() - timedelta(days=1))
    video(db, other, "Must never appear")
    db.commit()
    db.refresh(p)
    result = simulate(db, p)
    assert result["ready"] == 2 and result["filled"] == 4
    assert [e.video.id for e in result["rows"][0].entries] == [older.id, older.id]
    assert all(e.video is None for e in result["rows"][2].entries)
    p.ordering = "newest"
    result = simulate(db, p)
    assert all(e.video.id == newer.id for e in result["rows"][0].entries)
    b.status = AccountStatus.NEEDS_RECONNECT
    db.flush()
    result = simulate(db, p)
    assert result["filled"] == 2
    assert db.scalar(select(func.count()).select_from(ScheduledPost)) == 0
    assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_library_filter_keeps_person_and_unassigned_content_separate(web_owner, db):
    p = create_plan(web_owner, db)
    other = create_plan(web_owner, db, "Other")
    video(db, p, "Owned video")
    video(db, other, "Other video")
    db.add(Video(workspace_id=p.workspace_id, title="Unassigned video", status=VideoStatus.READY))
    db.commit()
    r = web_owner.get(f"/library?profile_id={p.profile_id}")
    assert (
        r.status_code == 200
        and "Owned video" in r.text
        and "Other video" not in r.text
        and "Unassigned video" not in r.text
    )
    assert 'action="/library/upload"' not in r.text
    r = web_owner.get("/library")
    assert "Unassigned video" in r.text and "Owned video" not in r.text
    assert (
        f"profile_id={p.profile_id}" in web_owner.get(f"/library?profile_id={p.profile_id}&status=all").text
    )


def test_import_inherits_profile_and_dedup_does_not_cross_people(web_owner, db):
    p = create_plan(web_owner, db)
    q = create_plan(web_owner, db, "Second")
    remote = RemoteVideo(external_id="shared-file", title="Owned material")
    for plan in [p, q]:
        source = Source(
            workspace_id=plan.workspace_id,
            profile_id=plan.profile_id,
            kind=SourceKind.DRIVE,
            platform=Platform.GDRIVE,
            name="Synthetic",
            enabled=False,
        )
        db.add(source)
        db.flush()
        assert importer._create_videos(db, source, [remote]) == 1
        assert importer._create_videos(db, source, [remote]) == 0
    db.flush()
    items = list(db.scalars(select(Video).order_by(Video.id)))
    assert [v.profile_id for v in items] == [p.profile_id, q.profile_id]
    items[0].status, items[0].sha256 = VideoStatus.READY, "samebytes"
    items[1].sha256 = "samebytes"
    db.flush()
    assert importer.find_duplicate(db, items[1], 10, []) is None
    clone = video(db, p, "Clone", sha256="samebytes")
    assert importer.find_duplicate(db, clone, 10, []).id == items[0].id


@pytest.mark.parametrize("mode", ["once", "repeat"])
def test_legacy_paths_cannot_publish_person_media_even_with_server_enabled(
    web_owner, db, make_account, web_make_rule, mode
):
    from repeater.engine import actions, publisher, scheduler

    p = create_plan(web_owner, db)
    p.rotation_mode = mode
    a = make_account(Platform.FACEBOOK)
    web_make_rule(a, enabled=True)
    assert save(web_owner, db, p, "targets", {"targets": [str(a.id)]}).status_code == 303
    v = video(db, p, "Preview only")
    db.commit()
    db.refresh(a)
    with pytest.raises(actions.UserError, match="preview-only"):
        actions.schedule_video_now(db, v, a, actor="test")
    assert scheduler._active_rules(db, p.workspace_id) == []
    ws = db.get(Workspace, p.workspace_id)
    ctx = scheduler._load_context(db, ws, utcnow())
    assert all(x.id != v.id for x in ctx.videos)
    # Even a stale/malicious queued row is canceled before any adapter access.
    queued = ScheduledPost(
        workspace_id=p.workspace_id,
        video_id=v.id,
        connected_account_id=a.id,
        platform=a.platform,
        scheduled_at=utcnow(),
        idempotency_key="synthetic-draft",
        cycle=0,
        created_by="user",
    )
    db.add(queued)
    db.flush()
    result = publisher._publish_target(db, queued, utcnow())
    assert result is not None and queued.status.value == "canceled"


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_new_pages_render_in_both_languages(web_owner, db, make_account, lang):
    p = create_plan(web_owner, db, "الشخص التجريبي")
    make_account(Platform.FACEBOOK)
    set_lang(web_owner, lang)
    for path in [
        "/",
        "/plans",
        f"/plans/{p.id}/edit",
        f"/plans/{p.id}",
        f"/library?profile_id={p.profile_id}",
    ]:
        r = web_owner.get(path)
        assert r.status_code == 200
        assert f'lang="{lang}"' in r.text
        assert "plan.drive_invalid" not in r.text


def test_preview_excludes_used_overridden_and_wrong_format_content(web_owner, db, make_account):
    from repeater.models import PostStatus

    p = create_plan(web_owner, db)
    a = make_account(Platform.FACEBOOK)
    assert save(web_owner, db, p, "targets", {"targets": [str(a.id)]}).status_code == 303
    used = video(db, p, "Already used")
    video(db, p, "Excluded", overrides={"exclude_platforms": ["facebook"]})
    too_long = video(db, p, "Wrong format")
    too_long.duration_s = 100000
    valid = video(db, p, "Available")
    db.add(
        ScheduledPost(
            workspace_id=p.workspace_id,
            video_id=used.id,
            connected_account_id=a.id,
            platform=a.platform,
            status=PostStatus.PUBLISHED,
            scheduled_at=utcnow(),
            idempotency_key="already-used",
        )
    )
    db.commit()
    db.refresh(p)
    result = simulate(db, p)
    assert result["filled"] == 1 and result["excluded"] == 3
    assert result["rows"][0].entries[0].video.id == valid.id


@pytest.mark.parametrize("mode", ["once", "repeat"])
def test_profile_poll_cannot_reach_provider_even_when_global_enabled(
    web_owner, db, make_account, monkeypatch, mode
):
    from repeater.engine import publisher
    from repeater.models import PostStatus

    p = create_plan(web_owner, db)
    p.rotation_mode = mode
    a = make_account(Platform.INSTAGRAM)
    assert save(web_owner, db, p, "targets", {"targets": [str(a.id)]}).status_code == 303
    v = video(db, p, "Never poll this")
    queued = ScheduledPost(
        workspace_id=p.workspace_id,
        video_id=v.id,
        connected_account_id=a.id,
        platform=a.platform,
        status=PostStatus.PROCESSING,
        scheduled_at=utcnow(),
        idempotency_key="never-poll",
    )
    db.add(queued)
    db.commit()

    def unexpected(*args, **kwargs):
        raise AssertionError("A draft reached provider code")

    monkeypatch.setattr(publisher, "get_publisher", unexpected)
    monkeypatch.setattr(publisher, "fresh_context", unexpected)
    result = publisher.poll_post(db, queued.id)
    assert result is not None
    assert queued.status == PostStatus.PROCESSING


def test_plan_owned_sources_use_plan_controls_and_keep_folder(web_owner, db, make_account, network_is_mocked):
    p = create_plan(web_owner, db)
    g = make_account(Platform.GDRIVE)
    folder_mock(network_is_mocked)
    assert (
        save(web_owner, db, p, "source", {"drive_id": g.id, "folder": FOLDER, "backfill": 15}).status_code
        == 303
    )
    db.refresh(p)
    assert (
        web_owner.get(f"/sources/{p.source_id}/edit", follow_redirects=False).headers["location"]
        == f"/plans/{p.id}/edit#source"
    )
    assert post(web_owner, f"/sources/{p.source_id}/toggle").status_code == 404
    html = web_owner.get("/sources").text
    assert f'href="/plans/{p.id}/edit#source"' in html
    assert f'action="/sources/{p.source_id}/toggle"' not in html
    source = db.get(Source, p.source_id)
    source.last_polled_at = utcnow()
    db.commit()
    result = save(web_owner, db, p, "source", {"drive_id": g.id, "folder": "different_folder_123"})
    assert result.status_code == 422
    db.refresh(source)
    assert source.config["folder_id"] == FOLDER and source.config["backfill_count"] == 15


def test_page_with_pending_publications_cannot_be_claimed(web_owner, db, make_account):
    p = create_plan(web_owner, db)
    a = make_account(Platform.FACEBOOK)
    v = Video(workspace_id=p.workspace_id, title="Legacy")
    db.add(v)
    db.flush()
    db.add(
        ScheduledPost(
            workspace_id=p.workspace_id,
            video_id=v.id,
            connected_account_id=a.id,
            platform=a.platform,
            scheduled_at=utcnow(),
            idempotency_key="pending-legacy",
        )
    )
    db.commit()
    assert save(web_owner, db, p, "targets", {"targets": [str(a.id)]}).status_code == 422
    db.refresh(a)
    assert a.profile_id is None


def test_empty_selection_retains_page_ownership_without_scheduling(web_owner, db, make_account):
    p = create_plan(web_owner, db)
    a = make_account(Platform.FACEBOOK)
    assert save(web_owner, db, p, "targets", {"targets": [str(a.id)]}).status_code == 303
    assert save(web_owner, db, p, "targets", {}).status_code == 303
    db.refresh(a)
    assert a.profile_id == p.profile_id
    assert db.scalar(select(func.count()).select_from(PlanTarget)) == 0
    assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_video_details_and_deletion_return_to_the_person_library(web_owner, db):
    p = create_plan(web_owner, db)
    v = video(db, p, "Person video")
    db.commit()
    page = web_owner.get(f"/library/{v.id}")
    assert page.status_code == 200
    assert f'href="/library?profile_id={p.profile_id}"' in page.text
    assert f'action="/library/{v.id}/publish-now"' not in page.text
    response = post(web_owner, f"/library/{v.id}/delete")
    assert response.status_code == 303
    assert response.headers["location"] == f"/library?profile_id={p.profile_id}"
    assert web_owner.get(response.headers["location"]).status_code == 200
