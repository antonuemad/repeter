"""Person execution with synthetic tenants, fake publishers and denied unmocked HTTP."""

from datetime import timedelta

import pytest
import respx
from sqlalchemy import func, select

from repeater.config import get_settings
from repeater.crypto import signed_media_path
from repeater.engine import publisher, rules, scheduler
from repeater.media import storage
from repeater.models import (
    Job,
    PlanTarget,
    Platform,
    PostStatus,
    ScheduledPost,
    Video,
    VideoStatus,
    Workspace,
)
from repeater.platforms import override
from repeater.platforms.base import PublishOutcome, PublishResult, RetryableError, UnknownOutcomeError
from repeater.timeutil import utcnow
from tests.test_content_plans import create_plan, save
from tests.test_engine_publish import FakePublisher
from tests.test_web_support import post, set_lang

pytest_plugins = ["tests.test_web_support"]


@pytest.fixture(autouse=True)
def mocked_network():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def arrangement(web_owner, db, workspace, make_account):
    def build(kind="image", placement="post", platform="facebook"):
        now = utcnow().replace(minute=0, second=0, microsecond=0)
        at = now + timedelta(hours=1)
        plan = create_plan(web_owner, db)
        plan.enabled = True
        plan.timezone = "UTC"
        plan.start_date = at.date()
        plan.times = [at.strftime("%H:%M")]
        plan.rotation_mode = "repeat"
        plan.repeat_after_days = 1
        workspace.paused = False
        workspace.horizon_hours = 48
        account = make_account(platform, extra={"account_type": "BUSINESS"})
        account.profile_id = plan.profile_id
        rule = rules.ensure_rule(db, account)
        rule.settings = {**rule.settings, "self_declared_made_for_kids": False}
        db.add(PlanTarget(plan_id=plan.id, account_id=account.id, workspace_id=workspace.id))
        item = Video(
            workspace_id=workspace.id,
            profile_id=plan.profile_id,
            status=VideoStatus.READY,
            media_kind=kind,
            content_format=placement,
            title="Synthetic material",
            caption="Synthetic post",
            width=1080,
            height=1920 if placement in ("story", "reel") else 1080,
            duration_s=10,
            fps=30,
        )
        db.add(item)
        db.flush()
        if kind != "text":
            path = storage.ensure_video_dir(workspace.id, item.id) / (
                "image.jpg" if kind == "image" else "encoded.mp4"
            )
            path.write_bytes(b"\xff\xd8\xffsynthetic-jpeg" if kind == "image" else b"synthetic-video")
            item.encoded_path, item.encoded_size_bytes = storage.rel_path(path), path.stat().st_size
        db.commit()
        return plan, account, item, now, at

    return build


def scheduled(db, workspace, now):
    posts = scheduler.plan_workspace(db, workspace.id, now=now)
    db.commit()
    return posts


@pytest.mark.parametrize(
    "kind,placement,platform",
    [
        ("text", "post", "facebook"),
        ("image", "post", "facebook"),
        ("image", "story", "facebook"),
        ("video", "story", "facebook"),
        ("image", "post", "instagram"),
        ("image", "story", "instagram"),
        ("video", "story", "instagram"),
        ("video", "reel", "youtube"),
    ],
)
def test_plan_worker_to_publisher_all_formats(arrangement, db, workspace, kind, placement, platform):
    plan, account, item, now, at = arrangement(kind, placement, platform)
    result = scheduled(db, workspace, now)
    assert len(result) == 1  # Only one outstanding use of a pair, even in repeat mode.
    record = result[0]
    assert record.plan_id == plan.id and record.slot_at == at and record.plan_revision == plan.revision
    assert scheduled(db, workspace, now) == []
    fake = FakePublisher(
        Platform(platform),
        publish_script=[PublishResult(PublishOutcome.PUBLISHED, platform_post_id="synthetic-result")],
    )
    with override("publisher", Platform(platform), fake):
        assert publisher.publish_post(db, record.id, now=at).status == PostStatus.PUBLISHED
    request = fake.requests[0]
    assert (request.media_kind, request.content_format) == (kind, placement)
    assert request.caption == "Synthetic post"
    assert request.public_url.endswith(".jpg") if kind == "image" else True
    assert request.public_url == "" and request.size_bytes == 0 if kind == "text" else True
    # Actual confirmed history controls the next cycle, and only the next eligible slot is queued.
    later = scheduled(db, workspace, at + timedelta(minutes=1))
    assert len(later) == 1 and later[0].cycle == 1 and later[0].scheduled_at >= at + timedelta(days=1)


@pytest.mark.parametrize("gate", ["server", "workspace", "plan"])
def test_three_execution_gates_block_reservations(
    arrangement, db, workspace, monkeypatch, gate, mocked_network
):
    plan, account, item, now, at = arrangement()
    if gate == "server":
        monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    elif gate == "workspace":
        workspace.paused = True
    else:
        plan.enabled = False
    db.commit()
    assert scheduled(db, workspace, now) == []
    assert db.scalar(select(func.count()).select_from(Job)) == 0
    assert not mocked_network.calls


@pytest.mark.parametrize(
    "change", ["stop", "revision", "target", "ownership", "archive", "server", "caption", "repeat"]
)
def test_recheck_before_any_provider_call(arrangement, db, workspace, monkeypatch, change, mocked_network):
    plan, account, item, now, at = arrangement("text")
    record = scheduled(db, workspace, now)[0]
    if change == "stop":
        plan.enabled = False
    if change == "revision":
        plan.revision += 1
    if change == "target":
        db.delete(db.get(PlanTarget, (plan.id, account.id)))
    if change == "ownership":
        account.profile_id = None
    if change == "archive":
        item.status = VideoStatus.ARCHIVED
    if change == "server":
        monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    if change == "caption":
        item.caption = ""
    if change == "repeat":
        db.add(
            ScheduledPost(
                workspace_id=workspace.id,
                video_id=item.id,
                connected_account_id=account.id,
                platform=account.platform,
                idempotency_key="prior",
                cycle=7,
                status=PostStatus.PUBLISHED,
                scheduled_at=at - timedelta(hours=1),
                published_at=at - timedelta(hours=1),
            )
        )
    db.commit()
    fake = FakePublisher(Platform.FACEBOOK)
    with override("publisher", Platform.FACEBOOK, fake):
        assert publisher.publish_post(db, record.id, now=at).status == PostStatus.CANCELED
    assert not fake.requests and not mocked_network.calls


def test_pending_unknown_outcome_blocks_new_cycles(arrangement, db, workspace):
    plan, account, item, now, at = arrangement("text")
    record = scheduled(db, workspace, now)[0]
    fake = FakePublisher(Platform.FACEBOOK, publish_script=[UnknownOutcomeError("simulated lost answer")])
    with override("publisher", Platform.FACEBOOK, fake):
        assert publisher.publish_post(db, record.id, now=at).status == PostStatus.NEEDS_ATTENTION
    assert publisher.engine_state(record.publish_state)["may_be_live"]
    assert scheduled(db, workspace, at + timedelta(days=2)) == []


def test_retry_and_poll_keep_plan_provenance(arrangement, db, workspace, monkeypatch):
    plan, account, item, now, at = arrangement("image", "story", "instagram")
    record = scheduled(db, workspace, now)[0]
    fake = FakePublisher(
        Platform.INSTAGRAM,
        publish_script=[
            RetryableError("synthetic refusal"),
            PublishResult(PublishOutcome.PROCESSING, state={"container_id": "fake"}),
        ],
        poll_script=[PublishResult(PublishOutcome.PUBLISHED, platform_post_id="fake-result")],
    )
    with override("publisher", Platform.INSTAGRAM, fake):
        assert publisher.publish_post(db, record.id, now=at).status == PostStatus.FAILED
        assert record.slot_at == at
        assert publisher.publish_post(db, record.id, now=record.scheduled_at).status == PostStatus.PROCESSING
        monkeypatch.setattr(get_settings(), "publishing_enabled", False)
        publisher.poll_post(db, record.id, now=at + timedelta(minutes=10))
        assert not fake.poll_states
        monkeypatch.setattr(get_settings(), "publishing_enabled", True)
        plan.enabled = False
        db.commit()
        publisher.poll_post(db, record.id, now=at + timedelta(minutes=20))
        assert not fake.poll_states
        plan.enabled = True
        db.commit()
        assert (
            publisher.poll_post(db, record.id, now=at + timedelta(minutes=30)).status == PostStatus.PUBLISHED
        )


def test_pause_cancels_only_untouched_work(arrangement, web_owner, db, workspace):
    plan, account, item, now, at = arrangement()
    record = scheduled(db, workspace, now)[0]
    assert save(web_owner, db, plan, "execution", {"action": "pause"}).status_code == 303
    db.refresh(record)
    db.refresh(plan)
    assert record.status == PostStatus.CANCELED and not plan.enabled
    assert scheduled(db, workspace, now) == []
    assert save(web_owner, db, plan, "execution", {"action": "start"}).status_code == 303
    replacement = scheduled(db, workspace, now)[0]
    assert replacement.id != record.id and replacement.slot_at == record.slot_at
    replacement.status = PostStatus.PROCESSING
    replacement.publish_state = {"container_id": "synthetic-container"}
    db.commit()
    save(web_owner, db, plan, "execution", {"action": "pause"})
    db.refresh(replacement)
    assert replacement.status == PostStatus.PROCESSING and replacement.publish_state


def test_edit_stops_plan_and_preserves_skip(arrangement, web_owner, db, workspace):
    plan, account, item, now, at = arrangement()
    record = scheduled(db, workspace, now)[0]
    record.status = PostStatus.SKIPPED
    db.commit()
    later = scheduled(db, workspace, now)
    assert all(p.slot_at > record.slot_at for p in later)
    assert (
        save(web_owner, db, plan, "rotation", {"rotation_mode": "repeat", "repeat_after_days": 5}).status_code
        == 303
    )
    db.refresh(plan)
    db.refresh(record)
    assert not plan.enabled and record.status == PostStatus.SKIPPED


@pytest.mark.parametrize("lang,phrase", [("en", "Testing lock:"), ("ar", "قفل الاختبار:")])
def test_activation_locked_has_no_side_effects(arrangement, web_owner, db, monkeypatch, lang, phrase):
    plan, account, item, now, at = arrangement()
    plan.enabled = False
    db.commit()
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    set_lang(web_owner, lang)
    page = web_owner.get(f"/plans/{plan.id}")
    assert phrase in page.text and 'disabled aria-describedby="execution-lock"' in page.text
    assert save(web_owner, db, plan, "execution", {"action": "start"}).status_code == 422
    db.refresh(plan)
    assert not plan.enabled and db.scalar(select(func.count()).select_from(Job)) == 0


def test_execution_authorization_and_stale_revision(arrangement, web_owner, web_editor, db):
    plan, account, item, now, at = arrangement()
    assert save(web_editor, db, plan, "execution", {"action": "pause"}).status_code == 403
    assert (
        post(web_owner, f"/plans/{plan.id}/execution", {"revision": 0, "action": "pause"}).status_code == 422
    )
    other = Workspace(name="Other tenant", timezone="UTC")
    db.add(other)
    db.flush()
    from repeater.models import ContentPlan, CreatorProfile

    profile = CreatorProfile(workspace_id=other.id, name="Other person")
    db.add(profile)
    db.flush()
    foreign = ContentPlan(
        workspace_id=other.id,
        profile_id=profile.id,
        timezone="UTC",
        start_date=at.date(),
        weekdays=[0],
        times=["10:00"],
        ordering="oldest",
    )
    db.add(foreign)
    db.commit()
    assert (
        post(
            web_owner, f"/plans/{foreign.id}/execution", {"revision": foreign.revision, "action": "start"}
        ).status_code
        == 404
    )
    db.refresh(foreign)
    assert not foreign.enabled


def test_person_signed_media_type_and_lock(arrangement, web_client, db, monkeypatch):
    plan, account, item, now, at = arrangement()
    url = signed_media_path(item.id, extension="jpg")
    response = web_client.get(url)
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert web_client.get(signed_media_path(item.id)).status_code == 404
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    assert web_client.get(url).status_code == 404


def test_parallel_planners_reserve_once(arrangement, db, workspace):
    from concurrent.futures import ThreadPoolExecutor

    from repeater.db import get_sessionmaker

    plan, account, item, now, at = arrangement()
    workspace_id = workspace.id

    def run():
        with get_sessionmaker()() as session:
            result = scheduler.plan_workspace(session, workspace_id, now=now)
            session.commit()
            return len(result)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: run(), range(2))) == 1
    assert db.scalar(select(func.count()).select_from(ScheduledPost)) == 1


@pytest.mark.parametrize(
    "kind,placement,platform",
    [
        ("text", "post", "facebook"),
        ("image", "post", "facebook"),
        ("image", "story", "facebook"),
        ("video", "story", "facebook"),
        ("image", "post", "instagram"),
        ("image", "story", "instagram"),
        ("video", "story", "instagram"),
    ],
)
def test_saved_plan_through_real_adapter_with_mocked_http(
    arrangement, db, workspace, mocked_network, monkeypatch, kind, placement, platform
):
    from urllib.parse import parse_qs

    import httpx

    from repeater.platforms import facebook

    monkeypatch.setattr(facebook, "_sleep", lambda _: None)
    plan, account, item, now, at = arrangement(kind, placement, platform)
    account.external_id = "123"
    db.commit()
    base = "https://graph.facebook.com/v26.0"
    if platform == "instagram":
        mocked_network.get(f"{base}/123/content_publishing_limit").respond(200, json={"data": []})
        mocked_network.post(f"{base}/123/media").respond(200, json={"id": "container"})
        mocked_network.get(f"{base}/container").respond(200, json={"status_code": "FINISHED"})
        final = mocked_network.post(f"{base}/123/media_publish").respond(200, json={"id": "posted"})
        mocked_network.get(f"{base}/posted").respond(
            200, json={"permalink": "https://instagram.test/synthetic"}
        )
    elif placement == "story" and kind == "video":

        def story(request):
            if parse_qs(request.content.decode()).get("upload_phase") == ["start"]:
                return httpx.Response(
                    200,
                    json={
                        "video_id": "777",
                        "upload_url": "https://rupload.facebook.com/video-upload/v26.0/777",
                    },
                )
            return httpx.Response(200, json={"success": True})

        final = mocked_network.post(f"{base}/123/video_stories").mock(side_effect=story)
        mocked_network.post("https://rupload.facebook.com/video-upload/v26.0/777").respond(
            200, json={"success": True}
        )
        mocked_network.get(f"{base}/777").respond(
            200,
            json={
                "status": {
                    "video_status": "ready",
                    "publishing_phase": {"status": "complete", "publish_status": "published"},
                }
            },
        )
    else:
        edge = "feed" if kind == "text" else "photos" if placement == "post" else "photo_stories"
        if placement == "story":
            mocked_network.post(f"{base}/123/photos").respond(200, json={"id": "prepared-photo"})
        final = mocked_network.post(f"{base}/123/{edge}").respond(200, json={"id": "posted", "success": True})
    record = scheduled(db, workspace, now)[0]
    result = publisher.publish_post(db, record.id, now=at)
    if result.status == PostStatus.PROCESSING:
        result = publisher.poll_post(db, record.id, now=at + timedelta(seconds=30))
    assert result.status == PostStatus.PUBLISHED and final.called
    assert record.published_at is not None and record.plan_id == plan.id
    count = len(mocked_network.calls)
    publisher.publish_post(db, record.id, now=at + timedelta(minutes=1))
    publisher.poll_post(db, record.id, now=at + timedelta(minutes=1))
    assert len(mocked_network.calls) == count  # Replayed jobs cannot publish twice.


@pytest.mark.parametrize("restriction", ["audience", "quota"])
def test_youtube_reservations_respect_settings_and_project_budget(
    arrangement, db, workspace, monkeypatch, restriction
):
    plan, account, item, now, at = arrangement("video", "reel", "youtube")
    if restriction == "audience":
        rules.find_rule(db, account).settings = {}
        db.commit()
    else:
        monkeypatch.setattr(get_settings(), "youtube_daily_upload_quota", 0)
    assert scheduled(db, workspace, now) == []


def test_missed_slots_do_not_publish_in_a_burst(arrangement, db, workspace, mocked_network):
    plan, account, item, now, at = arrangement("text")
    record = scheduled(db, workspace, now)[0]
    assert publisher.publish_post(db, record.id, now=at + timedelta(hours=4)).status == PostStatus.CANCELED
    assert not mocked_network.calls
    assert all(
        p.scheduled_at > at + timedelta(hours=4) for p in scheduled(db, workspace, at + timedelta(hours=4))
    )


def test_retry_preserves_minimum_destination_gap(arrangement, db, workspace):
    plan, account, item, now, at = arrangement("text")
    record = scheduled(db, workspace, now)[0]
    rule = rules.find_rule(db, account)
    rule.min_gap_minutes = 60
    other = Video(
        workspace_id=workspace.id,
        profile_id=plan.profile_id,
        media_kind="text",
        content_format="post",
        status=VideoStatus.READY,
        title="Other",
        caption="Other synthetic post",
    )
    db.add(other)
    db.flush()
    db.add(
        ScheduledPost(
            workspace_id=workspace.id,
            video_id=other.id,
            connected_account_id=account.id,
            platform=account.platform,
            idempotency_key="other-reservation",
            scheduled_at=at + timedelta(minutes=30),
            status=PostStatus.SCHEDULED,
        )
    )
    db.commit()
    fake = FakePublisher(Platform.FACEBOOK, publish_script=[RetryableError("synthetic temporary refusal")])
    with override("publisher", Platform.FACEBOOK, fake):
        assert publisher.publish_post(db, record.id, now=at).status == PostStatus.FAILED
    assert record.slot_at == at and record.scheduled_at == at + timedelta(minutes=90)


def test_source_control_releases_stale_plan_reservations(arrangement, web_owner, db, workspace):
    from repeater.models import Source, SourceKind
    from tests.test_person_sources import control

    plan, account, item, now, at = arrangement()
    source = Source(
        workspace_id=workspace.id,
        profile_id=plan.profile_id,
        connected_account_id=account.id,
        platform=account.platform,
        kind=SourceKind.PLATFORM_API,
        name="Synthetic source",
        enabled=True,
        config={},
    )
    db.add(source)
    db.commit()
    record = scheduled(db, workspace, now)[0]
    assert control(web_owner, db, plan, source, "pause").status_code == 303
    db.refresh(record)
    assert record.status == PostStatus.CANCELED and plan.enabled
    replacement = scheduled(db, workspace, now)[0]
    assert replacement.slot_at == at and replacement.plan_revision == plan.revision
