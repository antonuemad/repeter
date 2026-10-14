"""Integration fixes between modules: queue dedupe under prepared statements, handler
registration, RestartPublish handling, adapter state kept on poll errors, adapter notes, the
encoded frame rate in PublishRequest, Google grant sharing on disconnect, web logging and the
create-admin --password-stdin flag."""

from __future__ import annotations

import io
import logging
import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from repeater import cli, queue
from repeater.db import get_sessionmaker
from repeater.engine import publisher, rules
from repeater.jobs import kinds, registry, worker
from repeater.media import storage
from repeater.models import (
    ActivityLevel,
    ActivityLog,
    ConnectedAccount,
    Job,
    JobStatus,
    Platform,
    PostStatus,
    ScheduledPost,
    User,
    Video,
    VideoStatus,
)
from repeater.platforms import facebook, override
from repeater.platforms.base import (
    LIMITS,
    PublishOutcome,
    PublishRequest,
    PublishResult,
    RestartPublish,
    RetryableError,
)
from repeater.security import verify_password
from repeater.timeutil import utcnow
from tests.test_web_support import post

pytest_plugins = ["tests.test_web_support"]


# =========================================================================== queue & registry


def test_enqueue_dedupe_survives_prepared_statements(db):
    """psycopg prepares a statement after a few runs on one connection; the partial-index
    predicate of ON CONFLICT must still match then (it used to fail on the ~10th enqueue)."""
    ids = []
    for i in range(20):
        ids.append(queue.enqueue(db, "k", {"i": i}, dedupe_key=f"k:{i}"))
        db.commit()
    assert all(ids) and len(set(ids)) == 20
    for i in range(10):
        assert queue.enqueue(db, "k", {}, dedupe_key=f"k:{i}") is None
    db.commit()
    assert len(db.execute(select(Job)).scalars().all()) == 20


def test_every_job_kind_has_a_registered_handler():
    assert "repeater.jobs.maintenance" in registry.HANDLER_MODULES
    handlers = registry.load_handlers()
    declared = {v for k, v in vars(kinds).items() if k.isupper() and isinstance(v, str)}
    assert declared <= set(handlers)


def test_web_source_form_mirrors_the_importer_limits():
    """The web process does not import the importer (it loads yt-dlp); keep the copies equal."""
    from repeater.sources import importer
    from repeater.web.routes import sources as web_sources

    assert web_sources.YTDLP_MIN_POLL_MINUTES == importer.YTDLP_MIN_POLL_MINUTES
    assert web_sources.MAX_BACKFILL == importer.MAX_BACKFILL


def test_worker_runs_maintenance_job(db):
    queue.enqueue(db, kinds.MAINTENANCE, {}, dedupe_key=kinds.MAINTENANCE, max_attempts=1)
    db.commit()
    assert worker.run_once(get_sessionmaker(), "w-int") is True
    db.expire_all()
    job = db.execute(select(Job)).scalar_one()
    assert job.status == JobStatus.DONE, job.last_error


# =========================================================================== engine <-> adapters


@dataclass
class ScriptedPublisher:
    platform: Platform
    poll_items: list[Any] = field(default_factory=list)
    requests: list[PublishRequest] = field(default_factory=list)
    mutate: dict[str, Any] = field(default_factory=dict)

    @property
    def limits(self):
        return LIMITS[self.platform]

    def validate(self, account, req):
        self.requests.append(req)
        return []

    def publish(self, account, req):  # pragma: no cover - not used here
        raise AssertionError("publish not expected")

    def poll(self, account, state):
        state.update(self.mutate)
        item = self.poll_items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def processing_post(db, workspace, make_account, sample_video):
    """An Instagram post in PROCESSING with a READY video and adapter state."""

    def _make(platform: str = "instagram", *, attempts: int = 0) -> ScheduledPost:
        account = make_account(platform, expires_in=None, extra={"ig_user_id": "1", "page_id": "2"})
        # New rules start disabled; an Instagram post is only polled (which publishes the
        # container) while publishing is switched on for the account.
        rules.ensure_rule(db, account).enabled = True
        video = Video(
            workspace_id=workspace.id,
            status=VideoStatus.READY,
            title="Rope trick",
            duration_s=4.0,
            width=1080,
            height=1920,
            fps=30.0,
        )
        db.add(video)
        db.flush()
        folder = storage.ensure_video_dir(workspace.id, video.id)
        shutil.copyfile(sample_video, folder / storage.ENCODED_NAME)
        video.encoded_path = storage.rel_path(folder / storage.ENCODED_NAME)
        now = utcnow()
        row = ScheduledPost(
            workspace_id=workspace.id,
            video_id=video.id,
            connected_account_id=account.id,
            platform=Platform(platform),
            idempotency_key=f"{video.id}:{account.id}:0",
            scheduled_at=now - timedelta(minutes=2),
            status=PostStatus.PROCESSING,
            attempts=attempts,
            platform_post_id="upload-1",
            publish_state={"container_id": "c-1", "_engine": {"processing_since": now.isoformat()}},
        )
        db.add(row)
        db.commit()
        return row

    return _make


def test_restart_publish_from_poll_goes_back_to_publishing(db, processing_post):
    row = processing_post()
    fake = ScriptedPublisher(
        Platform.INSTAGRAM,
        [RestartPublish("The Instagram upload expired.", code="container_expired", retry_after=60)],
    )
    now = utcnow()
    with override("publisher", "instagram", fake):
        step = publisher.poll_post(db, row.id, now=now)
    assert step.status == PostStatus.FAILED and step.retry_in is None
    db.expire_all()
    row = db.get(ScheduledPost, row.id)
    assert row.status == PostStatus.FAILED and row.attempts == 1
    assert row.publish_state == {} and row.platform_post_id is None
    assert "expired" in row.error
    job = db.execute(select(Job).where(Job.dedupe_key == f"{kinds.PUBLISH_POST}:{row.id}")).scalar_one()
    assert job.status == JobStatus.QUEUED
    assert abs((job.run_at - (now + timedelta(minutes=5))).total_seconds()) < 1
    assert db.execute(select(Job).where(Job.kind == kinds.POLL_POST)).first() is None


def test_restart_publish_gives_up_after_the_retry_budget(db, processing_post, monkeypatch):
    monkeypatch.setattr(publisher.notify_mod, "notify", lambda *a, **k: True)
    row = processing_post(attempts=publisher.MAX_RETRIES)
    fake = ScriptedPublisher(Platform.INSTAGRAM, [RestartPublish("Upload dropped again.")])
    with override("publisher", "instagram", fake):
        step = publisher.poll_post(db, row.id, now=utcnow())
    assert step.status == PostStatus.NEEDS_ATTENTION
    db.expire_all()
    assert db.get(ScheduledPost, row.id).status == PostStatus.NEEDS_ATTENTION


def test_poll_error_keeps_adapter_state_changes(db, processing_post):
    row = processing_post()
    fake = ScriptedPublisher(
        Platform.INSTAGRAM, [RetryableError("Graph is down.")], mutate={"publish_attempts": 2}
    )
    with override("publisher", "instagram", fake):
        step = publisher.poll_post(db, row.id, now=utcnow())
    assert step.status == PostStatus.PROCESSING
    db.expire_all()
    row = db.get(ScheduledPost, row.id)
    assert row.publish_state["publish_attempts"] == 2 and row.publish_state["container_id"] == "c-1"
    assert "processing_since" in row.publish_state["_engine"]
    assert row.platform_post_id == "upload-1"
    job = db.execute(select(Job).where(Job.kind == kinds.POLL_POST)).scalar_one()
    assert job.status == JobStatus.QUEUED


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("YouTube set the video to 'private' instead of 'public'.", "YouTube set the video to 'private'"),
        ("Published on Instagram.", None),
        ("", None),
    ],
)
def test_published_activity_carries_the_adapter_note(db, processing_post, message, expected):
    row = processing_post()
    done = PublishResult(PublishOutcome.PUBLISHED, platform_post_id="m-1", message=message)
    with override("publisher", "instagram", ScriptedPublisher(Platform.INSTAGRAM, [done])):
        assert publisher.poll_post(db, row.id, now=utcnow()).status == PostStatus.PUBLISHED
    entry = (
        db.execute(
            select(ActivityLog).where(ActivityLog.message.like("Published %")).order_by(ActivityLog.id.desc())
        )
        .scalars()
        .first()
    )
    assert entry.level == ActivityLevel.INFO
    if expected:
        assert expected in entry.message
    else:
        assert entry.message.endswith(").")


def test_facebook_uses_the_encoded_frame_rate_from_the_request(sample_video, monkeypatch):
    calls: list[Any] = []
    monkeypatch.setattr(facebook, "probe_fps", lambda path: calls.append(path) or 30.0)

    def request(fps: float | None) -> PublishRequest:
        return PublishRequest(
            post_id=1,
            video_path=sample_video,
            public_url="https://rotator.test/m/1/2/x.mp4",
            size_bytes=sample_video.stat().st_size,
            duration_s=4.0,
            width=1080,
            height=1920,
            title="",
            caption="",
            settings={},
            fps=fps,
        )

    ctx = type("Ctx", (), {"extra": {"page_id": "p"}, "external_id": "p"})()
    problems = facebook.publisher.validate(ctx, request(23.0))
    assert any("23.00 fps" in p for p in problems) and calls == []
    assert not any("fps" in p for p in facebook.publisher.validate(ctx, request(None)))
    assert calls == [sample_video]


# =========================================================================== web


@dataclass
class FakeGoogle:
    name: str = "google"
    uses_pkce: bool = True
    revoked: list[str] = field(default_factory=list)

    def revoke(self, ctx) -> None:
        self.revoked.append(ctx.external_id)


def test_disconnect_keeps_a_google_grant_other_channels_still_use(web_owner, db, make_account):
    """Two channels of one Google login (same google_sub, different refresh tokens: every connect
    issues a new one) share a grant; a channel of another login has its own."""
    first = make_account("youtube", external_id="UC-1", refresh_token="grant-a", extra={"google_sub": "g-1"})
    second = make_account("youtube", external_id="UC-2", refresh_token="grant-b", extra={"google_sub": "g-1"})
    other = make_account(
        "youtube", external_id="UC-3", refresh_token="own-grant", extra={"google_sub": "g-2"}
    )
    fake = FakeGoogle()
    with override("oauth", "google", fake):
        for account in (first, second, other):
            resp = post(web_owner, f"/accounts/{account.id}/disconnect", page="/accounts")
            assert resp.status_code == 303
    # The first channel shares its grant with the second: only the local tokens go.
    assert fake.revoked == ["UC-2", "UC-3"]
    db.expire_all()
    assert all(a.token_enc is None for a in db.execute(select(ConnectedAccount)).scalars())


def test_web_app_configures_json_logging_when_nothing_else_did(monkeypatch):
    from repeater.web import app as web_app

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    httpx_logger = logging.getLogger("httpx")
    saved_httpx = httpx_logger.level
    try:
        root.handlers[:] = []
        httpx_logger.setLevel(logging.NOTSET)
        monkeypatch.setenv("REPEATER_LOG_LEVEL", "info")
        web_app._configure_logging()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, worker.JsonFormatter)
        assert root.level == logging.INFO and httpx_logger.level == logging.WARNING
        # A host that configured logging itself is left alone.
        marker = logging.NullHandler()
        root.handlers[:] = [marker]
        web_app._configure_logging()
        assert root.handlers == [marker]
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        httpx_logger.setLevel(saved_httpx)


# =========================================================================== CLI


def test_create_admin_reads_the_password_from_stdin(db, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("a long enough password\n"))
    code = cli.main(
        ["create-admin", "--email", "Ops@Magic-Lap.com", "--password-stdin", "--workspace", "Ops"]
    )
    assert code == 0
    assert "ops@magic-lap.com is owner of workspace 'Ops'" in capsys.readouterr().out
    user = db.execute(select(User).where(User.email == "ops@magic-lap.com")).scalar_one()
    assert verify_password(user.password_hash, "a long enough password")

    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert cli.main(["create-admin", "--email", "x@magic-lap.com", "--password-stdin"]) == 2
    with pytest.raises(SystemExit):
        cli.main(["create-admin", "--email", "x@magic-lap.com", "--password", "p" * 12, "--password-stdin"])


def test_failed_video_can_be_retried_from_the_library(web_owner, db, web_make_video, sample_video):
    from tests.test_web_support import set_lang

    video = web_make_video(status=VideoStatus.FAILED, with_files=False, error="Encoding failed: boom")
    folder = storage.ensure_video_dir(video.workspace_id, video.id)
    shutil.copyfile(sample_video, folder / "original.mp4")
    video.original_path = storage.rel_path(folder / "original.mp4")
    db.commit()
    set_lang(web_owner, "en")
    page = web_owner.get(f"/library/{video.id}").text
    assert f'action="/library/{video.id}/retry"' in page
    resp = post(web_owner, f"/library/{video.id}/retry", page=f"/library/{video.id}")
    assert resp.status_code == 303 and resp.headers["location"] == f"/library/{video.id}"
    assert "The video is being prepared again." in web_owner.get(f"/library/{video.id}").text
    db.expire_all()
    video = db.get(Video, video.id)
    assert video.status == VideoStatus.PROCESSING and video.error is None
    job = db.execute(select(Job).where(Job.kind == kinds.PROCESS_VIDEO)).scalar_one()
    assert job.payload == {"video_id": video.id}
    # A video that is not failed cannot be retried; the importer's message is shown.
    post(web_owner, f"/library/{video.id}/retry", page=f"/library/{video.id}")
    assert "Only failed videos can be retried." in web_owner.get(f"/library/{video.id}").text
