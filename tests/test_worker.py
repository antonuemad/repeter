"""Worker loop, periodic ticks, engine job handlers (refresh_tokens, delete_user_data),
maintenance and Telegram notifications."""

from __future__ import annotations

import json
import logging
import shutil
import signal
import threading
from datetime import UTC, datetime, time, timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select, update

from repeater import notify, queue
from repeater.accounts import load_tokens
from repeater.config import get_settings
from repeater.db import get_sessionmaker
from repeater.engine import jobs as engine_jobs
from repeater.jobs import kinds, maintenance, worker
from repeater.jobs.registry import load_handlers
from repeater.media import storage
from repeater.models import (
    AccountStatus,
    ActivityLog,
    ConnectedAccount,
    DataDeletionRequest,
    Job,
    JobStatus,
    OAuthState,
    Platform,
    PostStatus,
    RotationRule,
    ScheduledPost,
    Source,
    SourceKind,
    Video,
    VideoStatus,
    Workspace,
)
from repeater.platforms import override
from repeater.platforms.base import (
    LIMITS,
    AuthError,
    OAuthTokens,
    PublishOutcome,
    PublishResult,
    RetryableError,
)
from repeater.timeutil import utcnow

TELEGRAM_URL = "https://api.telegram.org/bot123456:ABC-secret/sendMessage"


@pytest.fixture
def sessions():
    return get_sessionmaker()


@pytest.fixture
def notifications(monkeypatch) -> list[tuple[str, dict]]:
    sent: list[tuple[str, dict]] = []
    fake = lambda text, **kw: sent.append((text, kw)) or True  # noqa: E731
    monkeypatch.setattr(engine_jobs.notify_mod, "notify", fake)
    monkeypatch.setattr("repeater.engine.publisher.notify_mod.notify", fake)
    return sent


@pytest.fixture
def telegram(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:ABC-secret")
    monkeypatch.setattr(settings, "telegram_chat_id", "-100200300")
    return settings


class OkPublisher:
    platform = Platform.YOUTUBE
    limits = LIMITS[Platform.YOUTUBE]

    def __init__(self, results):
        self.results = list(results)

    def validate(self, account, req):
        return []

    def publish(self, account, req):
        item = self.results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def poll(self, account, state):  # pragma: no cover - not used
        raise AssertionError


def ready_video(db, workspace, sample_video, **kw) -> Video:
    v = Video(
        workspace_id=workspace.id,
        status=VideoStatus.READY,
        title=kw.pop("title", "Trick"),
        duration_s=4.0,
        width=1080,
        height=1920,
        created_at=kw.pop("created_at", utcnow() - timedelta(days=1)),
        **kw,
    )
    db.add(v)
    db.flush()
    d = storage.ensure_video_dir(workspace.id, v.id)
    shutil.copy(sample_video, d / storage.ENCODED_NAME)
    v.encoded_path = storage.rel_path(d / storage.ENCODED_NAME)
    v.encoded_size_bytes = (d / storage.ENCODED_NAME).stat().st_size
    db.commit()
    return v


def full_day_rule(db, account) -> RotationRule:
    rule = RotationRule(
        workspace_id=account.workspace_id,
        connected_account_id=account.id,
        platform=account.platform,
        daily_cap=1,
        min_gap_minutes=0,
        window_start=time(0, 0),
        window_end=time(0, 0),
    )
    db.add(rule)
    db.commit()
    return rule


# =========================================================================== run_once


def test_run_once_without_jobs(sessions):
    assert worker.run_once(sessions, "w1", handlers={}) is False


def test_run_once_plan_workspace_end_to_end(db, sessions, workspace, make_account, sample_video):
    acc = make_account("youtube")
    full_day_rule(db, acc)
    video = ready_video(db, workspace, sample_video)
    kinds.enqueue_plan_workspace(db, workspace.id)
    db.commit()
    assert worker.run_once(sessions, "w1") is True
    db.expire_all()
    job = db.execute(select(Job).where(Job.kind == "plan_workspace")).scalar_one()
    assert job.status == JobStatus.DONE and job.locked_by is None and job.finished_at is not None
    post = db.execute(select(ScheduledPost)).scalar_one()
    assert post.video_id == video.id and post.status == PostStatus.SCHEDULED
    publish_job = db.execute(select(Job).where(Job.kind == "publish_post")).scalar_one()
    assert publish_job.payload == {"post_id": post.id} and publish_job.status == JobStatus.QUEUED


def test_run_once_publish_end_to_end_and_retry(db, sessions, workspace, make_account, sample_video):
    acc = make_account("youtube")
    full_day_rule(db, acc)
    video = ready_video(db, workspace, sample_video)
    post = ScheduledPost(
        workspace_id=workspace.id,
        video_id=video.id,
        connected_account_id=acc.id,
        platform=Platform.YOUTUBE,
        idempotency_key=f"{video.id}:{acc.id}:0",
        scheduled_at=utcnow() - timedelta(seconds=5),
    )
    db.add(post)
    db.flush()
    kinds.enqueue_publish_post(db, post.id, run_at=post.scheduled_at)
    db.commit()
    published = PublishResult(
        PublishOutcome.PUBLISHED, platform_post_id="v1", permalink="https://youtu.be/v1"
    )
    fake = OkPublisher([RetryableError("HTTP 503"), published])
    with override("publisher", "youtube", fake):
        assert worker.run_once(sessions, "w1") is True
        db.expire_all()
        job = db.execute(select(Job).where(Job.kind == "publish_post")).scalar_one()
        # RetryLater: the same job is back in the queue, no attempt consumed.
        assert job.status == JobStatus.QUEUED and job.attempts == 0 and "HTTP 503" in job.last_error
        assert timedelta(minutes=4) < job.run_at - utcnow() <= timedelta(minutes=5)
        post = db.get(ScheduledPost, post.id)
        assert post.status == PostStatus.FAILED and post.attempts == 1
        assert worker.run_once(sessions, "w1") is False  # not due yet
        db.execute(update(Job).where(Job.id == job.id).values(run_at=utcnow() - timedelta(seconds=1)))
        db.execute(update(ScheduledPost).where(ScheduledPost.id == post.id).values(scheduled_at=utcnow()))
        db.commit()
        assert worker.run_once(sessions, "w1") is True
    db.expire_all()
    assert db.get(ScheduledPost, post.id).status == PostStatus.PUBLISHED
    assert db.get(Job, job.id).status == JobStatus.DONE


def test_run_once_failures(db, sessions):
    calls: list[dict] = []

    def boom(session, payload):
        calls.append(payload)
        session.add(Workspace(name="should be rolled back"))
        session.flush()
        raise RuntimeError("cannot reach https://api.example.com/x?access_token=SECRET")

    queue.enqueue(db, "boom", {"n": 1}, max_attempts=2)
    queue.enqueue(db, "orphan", {}, max_attempts=3)
    db.commit()
    assert worker.run_once(sessions, "w1", handlers={"boom": boom}) is True
    db.expire_all()
    job = db.execute(select(Job).where(Job.kind == "boom")).scalar_one()
    assert job.status == JobStatus.QUEUED and job.attempts == 1  # retried with backoff
    assert job.run_at > utcnow() + timedelta(seconds=50)
    assert job.last_error.startswith("RuntimeError: cannot reach <url>") and "SECRET" not in job.last_error
    assert db.execute(select(func.count()).select_from(Workspace)).scalar() == 0
    assert worker.run_once(sessions, "w1", handlers={"boom": boom}) is True
    db.expire_all()
    orphan = db.execute(select(Job).where(Job.kind == "orphan")).scalar_one()
    assert orphan.status == JobStatus.DEAD and "no handler" in orphan.last_error
    db.execute(update(Job).where(Job.kind == "boom").values(run_at=utcnow() - timedelta(seconds=1)))
    db.commit()
    assert worker.run_once(sessions, "w1", handlers={"boom": boom}) is True
    db.expire_all()
    assert db.execute(select(Job).where(Job.kind == "boom")).scalar_one().status == JobStatus.DEAD
    assert len(calls) == 2


def test_finish_skips_job_taken_by_someone_else(db, sessions):
    def requeued_meanwhile(session, payload):
        session.execute(update(Job).values(status=JobStatus.QUEUED, locked_by=None))
        session.commit()

    queue.enqueue(db, "k", {})
    db.commit()
    assert worker.run_once(sessions, "w1", handlers={"k": requeued_meanwhile}) is True
    db.expire_all()
    assert db.execute(select(Job)).scalar_one().status == JobStatus.QUEUED


def test_worker_threads_finish_running_job_on_stop(db, sessions, tmp_path):
    started, release = threading.Event(), threading.Event()

    def slow(session, payload):
        started.set()
        assert release.wait(10)

    queue.enqueue(db, "slow", {})
    db.commit()
    heartbeat = tmp_path / "hb" / "worker.heartbeat"
    w = worker.Worker(
        sessions,
        threads=1,
        poll_seconds=0.05,
        heartbeat_path=heartbeat,
        handlers={"slow": slow},
        tick_seconds=3600,
    )
    w.start()
    try:
        assert started.wait(10)
        w.stop()
        release.set()
        w.join(10)
    finally:
        w.stop()
        release.set()
    assert all(not t.is_alive() for t in w._threads)
    db.expire_all()
    assert db.execute(select(Job).where(Job.kind == "slow")).scalar_one().status == JobStatus.DONE
    assert heartbeat.exists()
    # The ticker ran too and queued the periodic jobs for later.
    assert {"refresh_tokens", "maintenance"} <= set(db.execute(select(Job.kind)).scalars())


# =========================================================================== tick


def test_tick_enqueues_periodic_jobs_idempotently(db, sessions, workspace, make_account):
    paused = Workspace(name="Paused", paused=True)
    db.add(paused)
    acc = make_account("instagram")
    db.add(
        Source(
            workspace_id=workspace.id,
            kind=SourceKind.PLATFORM_API,
            platform=Platform.INSTAGRAM,
            connected_account_id=acc.id,
            poll_interval_minutes=15,
            last_polled_at=utcnow() - timedelta(minutes=20),
        )
    )
    db.add(Source(workspace_id=workspace.id, kind=SourceKind.MANUAL))
    db.commit()
    now = datetime(2026, 12, 1, 10, 2, 30, tzinfo=UTC)
    counts = worker.tick(sessions, now=now)
    assert counts == {"poll_source": 1, "plan_workspace": 1, "refresh_tokens": 1, "maintenance": 1}
    by_kind = {j.kind: j for j in db.execute(select(Job)).scalars()}
    assert by_kind["plan_workspace"].payload == {"workspace_id": workspace.id}
    assert by_kind["plan_workspace"].run_at == datetime(2026, 12, 1, 10, 5, tzinfo=UTC)
    assert by_kind["refresh_tokens"].run_at == datetime(2026, 12, 1, 10, 30, tzinfo=UTC)
    assert by_kind["maintenance"].run_at == datetime(2026, 12, 1, 11, 0, tzinfo=UTC)
    assert by_kind["maintenance"].dedupe_key == "maintenance"
    assert worker.tick(sessions, now=now) == {
        "poll_source": 0,
        "plan_workspace": 0,
        "refresh_tokens": 0,
        "maintenance": 0,
    }


def test_tick_step_failure_is_isolated(db, sessions, workspace, monkeypatch):
    def broken(session, now):
        raise RuntimeError("sources module down")

    monkeypatch.setattr(worker, "_enqueue_due_polls", broken)
    counts = worker.tick(sessions, now=datetime(2026, 12, 1, 10, 0, tzinfo=UTC))
    assert counts["poll_source"] == -1 and counts["plan_workspace"] == 1


def test_next_boundary_and_heartbeat(tmp_path):
    t = datetime(2026, 12, 1, 10, 5, 0, tzinfo=UTC)
    assert worker.next_boundary(t, timedelta(minutes=5)) == datetime(2026, 12, 1, 10, 10, tzinfo=UTC)
    assert worker.next_boundary(t, timedelta(hours=1)) == datetime(2026, 12, 1, 11, 0, tzinfo=UTC)
    worker.touch_heartbeat(tmp_path / "a" / "b")
    assert (tmp_path / "a" / "b").exists()
    worker.touch_heartbeat(None)


def test_json_logging_and_safe_error():
    formatter = worker.JsonFormatter()
    secret = "".join(["hun", "ter2"])
    try:
        raise ValueError(f"password={secret}")
    except ValueError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "job %s failed", (7,), sys.exc_info())
    record.kind = "publish_post"
    out = json.loads(formatter.format(record))
    assert out["msg"] == "job 7 failed" and out["level"] == "error" and out["kind"] == "publish_post"
    assert out["exc_type"] == "ValueError" and "hunter2" not in json.dumps(out)
    assert (
        worker.safe_error(RuntimeError("token: abc https://x.y/z?q=1"))
        == "RuntimeError: token: <redacted> <url>"
    )
    assert worker.safe_error(RetryableError("HTTP 500")) == "RetryableError: HTTP 500"
    assert worker.safe_error(KeyError()) == "KeyError"


def test_load_handlers_registers_engine_kinds():
    handlers = load_handlers()
    for kind in (
        "plan_workspace",
        "publish_post",
        "poll_post",
        "refresh_tokens",
        "maintenance",
        "delete_user_data",
    ):
        assert kind in handlers


# =========================================================================== refresh_tokens


def test_refresh_tokens(db, make_account, notifications):
    good = make_account("tiktok", expires_in=timedelta(hours=1))
    revoked = make_account("youtube", expires_in=timedelta(minutes=30))
    flaky = make_account("instagram", expires_in=timedelta(hours=1, minutes=30))
    later = make_account("facebook", expires_in=timedelta(days=30))

    class TikTok:
        name, uses_pkce = "tiktok", False

        def refresh(self, ctx):
            return OAuthTokens(
                access_token="fresh", refresh_token="r2", expires_at=utcnow() + timedelta(hours=24)
            )

    class Google:
        name, uses_pkce = "google", False

        def refresh(self, ctx):
            raise AuthError("invalid_grant: Token has been expired or revoked.")

    class Meta:
        name, uses_pkce = "meta", False

        def refresh(self, ctx):
            raise RetryableError("Graph API 500")

    with (
        override("oauth", "tiktok", TikTok()),
        override("oauth", "google", Google()),
        override("oauth", "meta", Meta()),
    ):
        counts = engine_jobs.refresh_tokens(db)
    assert counts == {"refreshed": 1, "needs_reconnect": 1, "failed": 1}
    db.expire_all()
    assert load_tokens(db.get(ConnectedAccount, good.id)).access_token == "fresh"
    assert db.get(ConnectedAccount, revoked.id).status == AccountStatus.NEEDS_RECONNECT
    assert db.get(ConnectedAccount, flaky.id).status == AccountStatus.ACTIVE
    assert load_tokens(db.get(ConnectedAccount, later.id)).access_token == "access-token"
    assert len(notifications) == 1 and "must be reconnected" in notifications[0][0]
    assert "access-token" not in notifications[0][0]


# =========================================================================== delete_user_data


def test_delete_user_data(db, workspace, make_account, sample_video):
    page = make_account("facebook", extra={"page_id": "p1", "fb_user_id": "fbu-1"}, display_name="Magic Page")
    ig = make_account("instagram", extra={"ig_user_id": "ig1", "page_id": "p1", "fb_user_id": "fbu-1"})
    other = make_account("facebook", extra={"page_id": "p2", "fb_user_id": "fbu-2"})
    tiktok = make_account("tiktok")
    source = Source(
        workspace_id=workspace.id,
        kind=SourceKind.PLATFORM_API,
        platform=Platform.INSTAGRAM,
        connected_account_id=ig.id,
    )
    db.add(source)
    db.commit()
    from_page = ready_video(db, workspace, sample_video, source_account_id=page.id)
    from_source = ready_video(db, workspace, sample_video, source_id=source.id)
    keep = ready_video(db, workspace, sample_video, source_account_id=other.id)
    post_on_page = ScheduledPost(
        workspace_id=workspace.id,
        video_id=keep.id,
        connected_account_id=page.id,
        platform=Platform.FACEBOOK,
        idempotency_key="k1",
        scheduled_at=utcnow(),
        status=PostStatus.PUBLISHED,
        platform_post_id="fb-post",
        permalink="https://facebook.com/x",
    )
    post_to_tiktok = ScheduledPost(
        workspace_id=workspace.id,
        video_id=from_page.id,
        connected_account_id=tiktok.id,
        platform=Platform.TIKTOK,
        idempotency_key="k2",
        scheduled_at=utcnow() + timedelta(hours=1),
    )
    db.add(RotationRule(workspace_id=workspace.id, connected_account_id=page.id, platform=Platform.FACEBOOK))
    request = DataDeletionRequest(provider="meta", external_user_id="fbu-1", confirmation_code="abc123")
    db.add_all([post_on_page, post_to_tiktok, request])
    db.commit()
    dirs = [
        storage.media_root() / storage.video_dir_rel(workspace.id, v.id)
        for v in (from_page, from_source, keep)
    ]
    deleted_ids = (from_page.id, from_source.id, post_to_tiktok.id)
    engine_jobs.handle_delete_user_data(db, {"request_id": request.id})
    db.expire_all()
    request = db.get(DataDeletionRequest, request.id)
    assert request.status == "completed" and request.completed_at is not None
    for acc_id in (page.id, ig.id):
        acc = db.get(ConnectedAccount, acc_id)
        assert acc.status == AccountStatus.DISABLED and acc.token_enc is None
        assert acc.extra == {} and acc.external_id == f"deleted-{acc_id}" and acc.avatar_url is None
        assert "Magic Page" not in acc.display_name
    assert db.get(ConnectedAccount, other.id).extra["fb_user_id"] == "fbu-2"
    assert db.get(Video, deleted_ids[0]) is None and db.get(Video, deleted_ids[1]) is None
    assert db.get(Video, keep.id) is not None
    assert not dirs[0].exists() and not dirs[1].exists() and dirs[2].exists()
    assert db.get(Source, source.id) is None
    assert db.get(ScheduledPost, deleted_ids[2]) is None  # its video is gone
    scrubbed = db.get(ScheduledPost, post_on_page.id)
    assert scrubbed.platform_post_id is None and scrubbed.permalink is None
    assert [r.enabled for r in db.execute(select(RotationRule)).scalars()] == [False]
    # Running the job again is harmless.
    assert engine_jobs.delete_user_data(db, request.id) is False
    assert engine_jobs.delete_user_data(db, 424242) is False


# =========================================================================== maintenance


def _age(db, model, row_id, column: str, when: datetime) -> None:
    db.execute(update(model).where(model.id == row_id).values({column: when}))
    db.commit()


def test_maintenance(db, workspace, admin_user, make_account, monkeypatch):
    now = utcnow()
    settings = get_settings()
    # Jobs: a stale running job and an old finished job.
    stale = Job(kind="k", status=JobStatus.RUNNING, locked_at=now - timedelta(hours=2), locked_by="dead")
    old_done = Job(kind="k", status=JobStatus.DONE, finished_at=now - timedelta(days=30))
    db.add_all([stale, old_done])
    # OAuth states and activity.
    db.add_all(
        [
            OAuthState(
                state="old",
                workspace_id=workspace.id,
                user_id=admin_user.id,
                provider="meta",
                purpose="meta",
                created_at=now - timedelta(hours=2),
            ),
            OAuthState(
                state="new",
                workspace_id=workspace.id,
                user_id=admin_user.id,
                provider="meta",
                purpose="meta",
                created_at=now - timedelta(minutes=5),
            ),
            ActivityLog(
                workspace_id=workspace.id,
                category="system",
                message="ancient",
                created_at=now - timedelta(days=91),
            ),
            ActivityLog(
                workspace_id=workspace.id,
                category="system",
                message="recent",
                created_at=now - timedelta(days=89),
            ),
        ]
    )
    yt = make_account("youtube", extra={"channel_id": "UC1"})
    gone = make_account("youtube", extra={"channel_id": "UC2", "uploads_playlist_id": "UU2"})
    gone.status, gone.token_enc, gone.avatar_url = AccountStatus.DISABLED, None, "https://yt3.example/a.jpg"
    video = Video(workspace_id=workspace.id, status=VideoStatus.READY)
    db.add(video)
    db.commit()

    def post(account, key, status, **kw) -> ScheduledPost:
        p = ScheduledPost(
            workspace_id=workspace.id,
            video_id=video.id,
            connected_account_id=account.id,
            platform=account.platform,
            idempotency_key=key,
            scheduled_at=kw.pop("scheduled_at", now),
            status=status,
            **kw,
        )
        db.add(p)
        db.commit()
        return p

    uploading = post(yt, "up", PostStatus.UPLOADING)
    _age(db, ScheduledPost, uploading.id, "updated_at", now - timedelta(hours=3))
    fresh_upload = post(yt, "up2", PostStatus.UPLOADING)
    lost = post(yt, "lost", PostStatus.SCHEDULED, scheduled_at=now - timedelta(hours=1))
    processing = post(yt, "proc", PostStatus.PROCESSING)
    old_yt = post(
        yt,
        "old",
        PostStatus.PUBLISHED,
        published_at=now - timedelta(days=31),
        platform_post_id="v",
        permalink="l",
        publish_state={"a": 1},
    )
    new_yt = post(
        yt, "new", PostStatus.PUBLISHED, published_at=now - timedelta(days=5), platform_post_id="v2"
    )
    gone_post = post(
        gone, "gone", PostStatus.PUBLISHED, published_at=now, platform_post_id="v3", permalink="l3"
    )

    from repeater.media import retention

    def not_ready(session, *, older_than_days, now=None):
        assert older_than_days == settings.original_retention_days
        raise NotImplementedError

    monkeypatch.setattr(retention, "purge_expired_originals", not_ready)
    result = maintenance.run_maintenance(db)
    db.commit()
    assert result["requeued_jobs"] == 1 and result["purged_jobs"] == 1
    assert result["oauth_states"] == 1 and result["activity"] == 1 and result["originals"] == 0
    assert result["stuck_uploads"] == 1 and result["youtube_posts"] == 1 and result["youtube_accounts"] == 1
    db.expire_all()
    assert db.get(Job, stale.id).status == JobStatus.QUEUED and db.get(Job, old_done.id) is None
    assert [s.state for s in db.execute(select(OAuthState)).scalars()] == ["new"]
    messages = [a.message for a in db.execute(select(ActivityLog)).scalars()]
    assert "ancient" not in messages and "recent" in messages
    assert db.get(ScheduledPost, uploading.id).status == PostStatus.NEEDS_ATTENTION
    assert db.get(ScheduledPost, fresh_upload.id).status == PostStatus.UPLOADING
    queued = {
        (j.kind, j.payload.get("post_id"))
        for j in db.execute(select(Job).where(Job.status == JobStatus.QUEUED)).scalars()
    }
    assert ("publish_post", lost.id) in queued and ("poll_post", processing.id) in queued
    old = db.get(ScheduledPost, old_yt.id)
    assert old.platform_post_id is None and old.permalink is None and old.publish_state == {}
    assert db.get(ScheduledPost, new_yt.id).platform_post_id == "v2"
    gone = db.get(ConnectedAccount, gone.id)
    assert gone.extra == {} and gone.avatar_url is None
    assert db.get(ScheduledPost, gone_post.id).platform_post_id is None
    assert db.get(ConnectedAccount, yt.id).extra == {"channel_id": "UC1"}

    # Second run: everything is already clean; the retention step reports its count.
    monkeypatch.setattr(retention, "purge_expired_originals", lambda session, *, older_than_days, now=None: 3)
    again = maintenance.run_maintenance(db)
    assert again["originals"] == 3 and again["youtube_posts"] == 0 and again["requeued_posts"] == 0


def test_maintenance_step_failure_is_isolated(db, monkeypatch):
    def broken(session, now):
        raise RuntimeError("boom")

    monkeypatch.setattr(maintenance, "_delete_old_activity", broken)
    result = maintenance.run_maintenance(db)
    assert result["activity"] == -1 and result["oauth_states"] == 0


def test_maintenance_handler_via_worker(db, sessions, monkeypatch):
    from repeater.media import retention

    monkeypatch.setattr(retention, "purge_expired_originals", lambda session, *, older_than_days, now=None: 0)
    queue.enqueue(db, kinds.MAINTENANCE, {}, dedupe_key=kinds.MAINTENANCE, max_attempts=1)
    db.commit()
    assert worker.run_once(sessions, "w1") is True
    db.expire_all()
    assert db.execute(select(Job)).scalar_one().status == JobStatus.DONE


# =========================================================================== notify


def test_notify_not_configured_does_nothing():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__regex=r"https://api\.telegram\.org/.*")
        assert notify.notify("hello") is False
        assert not route.called


def test_notify_sends_escaped_html(telegram):
    with respx.mock() as mock:
        route = mock.post(TELEGRAM_URL).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {}})
        )
        assert notify.notify("Video <Coin & Cup> failed", workspace_id=3, level="error") is True
    body = json.loads(route.calls[0].request.content)
    assert body["chat_id"] == "-100200300" and body["parse_mode"] == "HTML"
    assert body["link_preview_options"] == {"is_disabled": True}
    assert (
        body["text"] == "<b>Repeater · Action needed</b> (workspace 3)\nVideo &lt;Coin &amp; Cup&gt; failed"
    )


def test_notify_truncates_long_text(telegram):
    with respx.mock() as mock:
        route = mock.post(TELEGRAM_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
        assert notify.notify("x" * 10_000) is True
    text = json.loads(route.calls[0].request.content)["text"]
    assert len(text) < 4096 and text.endswith("…")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        ),
        httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 5}}),
        httpx.Response(502, text="<html>bad gateway</html>"),
        httpx.Response(200, json={"ok": False}),
    ],
)
def test_notify_rejections_return_false(telegram, response, caplog):
    with respx.mock() as mock:
        mock.post(TELEGRAM_URL).mock(return_value=response)
        assert notify.notify("hi") is False
    assert "ABC-secret" not in caplog.text


def test_notify_network_error_never_raises_or_leaks(telegram, caplog):
    caplog.set_level(logging.DEBUG)
    with respx.mock() as mock:
        mock.post(TELEGRAM_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        assert notify.notify("hi") is False
    assert "ConnectTimeout" in caplog.text and "ABC-secret" not in caplog.text


def test_notify_uses_timeout(telegram, monkeypatch):
    seen = {}

    class Recorder(httpx.Client):
        def __init__(self, *args, **kw):
            seen["timeout"] = kw.get("timeout")
            super().__init__(*args, **kw)

    monkeypatch.setattr(notify.httpx, "Client", Recorder)
    with respx.mock() as mock:
        mock.post(TELEGRAM_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
        assert notify.notify("hi") is True
    assert seen["timeout"] == 10.0


def test_httpx_request_log_redacts_bot_token(caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    logging.getLogger("httpx").info('HTTP Request: %s %s "%s"', "POST", TELEGRAM_URL, "HTTP/1.1 200 OK")
    assert "ABC-secret" not in caplog.text and "/bot<redacted>/sendMessage" in caplog.text


def test_notify_survives_unexpected_errors(telegram, monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(notify, "_send", explode)
    assert notify.notify("hi") is False


def test_main_wires_signals_to_graceful_stop(monkeypatch, tmp_path):
    installed: dict[int, object] = {}
    events: list[str] = []

    class FakeWorker:
        def __init__(self, session_factory, *, threads, poll_seconds, heartbeat_path):
            events.append(f"threads={threads}")
            self.stop_event = threading.Event()

        def start(self):
            events.append("start")
            installed[signal.SIGTERM](signal.SIGTERM, None)  # as if Docker stopped the container

        def stop(self):
            events.append("stop")
            self.stop_event.set()

        def join(self, timeout=None):
            events.append("join")

    monkeypatch.setattr(worker, "Worker", FakeWorker)
    monkeypatch.setattr(worker, "configure_logging", lambda level: events.append(f"log={level}"))
    monkeypatch.setattr(worker.signal, "signal", lambda signum, fn: installed.__setitem__(signum, fn))
    monkeypatch.setattr(get_settings(), "worker_heartbeat_path", tmp_path / "hb")
    assert worker.main(["--threads", "3", "--log-level", "debug"]) == 0
    assert events == ["log=DEBUG", "threads=3", "start", "stop", "join"]
    assert set(installed) == {signal.SIGTERM, signal.SIGINT}
    assert (tmp_path / "hb").exists()
