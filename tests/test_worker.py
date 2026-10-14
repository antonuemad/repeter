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
    settings = {"self_declared_made_for_kids": False} if account.platform == Platform.YOUTUBE else {}
    rule = RotationRule(
        workspace_id=account.workspace_id,
        connected_account_id=account.id,
        platform=account.platform,
        daily_cap=1,
        min_gap_minutes=0,
        window_start=time(0, 0),
        window_end=time(0, 0),
        settings=settings,
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
    assert counts == {
        "requeued_jobs": 0,
        "poll_source": 1,
        "plan_workspace": 1,
        "refresh_tokens": 1,
        "maintenance": 1,
    }
    by_kind = {j.kind: j for j in db.execute(select(Job)).scalars()}
    assert by_kind["plan_workspace"].payload == {"workspace_id": workspace.id}
    assert by_kind["plan_workspace"].run_at == datetime(2026, 12, 1, 10, 5, tzinfo=UTC)
    assert by_kind["refresh_tokens"].run_at == datetime(2026, 12, 1, 10, 30, tzinfo=UTC)
    assert by_kind["maintenance"].run_at == datetime(2026, 12, 1, 11, 0, tzinfo=UTC)
    assert by_kind["maintenance"].dedupe_key == "maintenance"
    assert worker.tick(sessions, now=now) == {
        "requeued_jobs": 0,
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
    ig = make_account(
        "instagram",
        extra={"ig_user_id": "ig1", "page_id": "p1", "fb_user_id": "fbu-1"},
        display_name="magic.private.handle",
    )
    other = make_account(
        "facebook", extra={"page_id": "p2", "fb_user_id": "fbu-2"}, display_name="Other Page"
    )
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
    from_source = ready_video(db, workspace, sample_video, source_id=source.id, caption="Secret caption")
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
    # What the engine and the importer wrote about these accounts and videos.
    db.add_all(
        [
            ActivityLog(
                workspace_id=workspace.id,
                category="publish",
                message="Published 'Coin trick' to Instagram (magic.private.handle).",
                data={"account_id": ig.id, "permalink": "https://www.instagram.com/reel/SECRETCODE/"},
            ),
            ActivityLog(
                workspace_id=workspace.id,
                category="account",
                message="Connected facebook account 'Magic Page'",
                data={"account_id": page.id},
            ),
            ActivityLog(
                workspace_id=workspace.id,
                category="import",
                message="Video 'Secret caption' is ready.",
                data={"video_id": from_source.id},
            ),
            ActivityLog(
                workspace_id=workspace.id,
                category="system",
                message="Magic Page was mentioned without ids",
                data={},
            ),
            ActivityLog(
                workspace_id=workspace.id,
                category="account",
                message="Connected facebook account 'Other Page'",
                data={"account_id": other.id},
            ),
        ]
    )
    db.add_all(
        [
            Job(
                kind="import_video",
                payload={"video_id": from_source.id, "download_url": "https://scontent.cdn/x.mp4?sig=abc"},
                status=JobStatus.DONE,
                dedupe_key=f"import_video:{from_source.id}",
                finished_at=utcnow(),
            ),
            Job(kind="poll_source", payload={"source_id": source.id}),
            Job(kind="import_video", payload={"video_id": keep.id}),
        ]
    )
    db.commit()
    dirs = [
        storage.media_root() / storage.video_dir_rel(workspace.id, v.id)
        for v in (from_page, from_source, keep)
    ]
    deleted_ids = (from_page.id, from_source.id, post_to_tiktok.id)
    page_id, ig_id, post_on_page_id = page.id, ig.id, post_on_page.id
    engine_jobs.handle_delete_user_data(db, {"request_id": request.id})
    db.expire_all()
    request = db.get(DataDeletionRequest, request.id)
    assert request.status == "completed" and request.completed_at is not None
    # The accounts are deleted (with their posts and rules), not just scrubbed.
    assert db.get(ConnectedAccount, page_id) is None and db.get(ConnectedAccount, ig_id) is None
    assert db.get(ScheduledPost, post_on_page_id) is None
    assert db.execute(select(RotationRule)).first() is None
    assert db.get(ConnectedAccount, other.id).extra["fb_user_id"] == "fbu-2"
    assert db.get(ConnectedAccount, tiktok.id) is not None
    assert db.get(Video, deleted_ids[0]) is None and db.get(Video, deleted_ids[1]) is None
    assert db.get(Video, keep.id) is not None
    assert not dirs[0].exists() and not dirs[1].exists() and dirs[2].exists()
    assert db.get(Source, source.id) is None
    assert db.get(ScheduledPost, deleted_ids[2]) is None  # its video is gone
    # engine-8 / ux-compliance-missed-2: nothing that names the accounts or the imported videos is
    # left in the activity log or the job payloads.
    text = " ".join(f"{a.message} {json.dumps(a.data)}" for a in db.execute(select(ActivityLog)).scalars())
    for secret in ("magic.private.handle", "SECRETCODE", "Magic Page", "Secret caption"):
        assert secret not in text
    assert "Other Page" in text  # another user's entries are untouched
    assert "Deleted the data of a Facebook account at Meta's request" in text
    payloads = json.dumps([j.payload for j in db.execute(select(Job)).scalars()])
    assert "scontent.cdn" not in payloads and f'"source_id": {source.id}' not in payloads
    assert f'"video_id": {keep.id}' in payloads
    # Running the job again is harmless.
    assert engine_jobs.delete_user_data(db, request.id) is False
    assert engine_jobs.delete_user_data(db, 424242) is False


# =========================================================================== maintenance


def test_meta_deletion_after_account_retention_removes_imports_and_quoted_activity(
    db,
    workspace,
    make_account,
    sample_video,
):
    from repeater.accounts import disconnect
    from repeater.sources import importer

    page = make_account("facebook", extra={"fb_user_id": "deleted-user"})
    other = make_account("facebook", extra={"fb_user_id": "other-user"})
    source = Source(
        workspace_id=workspace.id,
        kind=SourceKind.PLATFORM_API,
        platform=Platform.FACEBOOK,
        connected_account_id=page.id,
    )
    db.add(source)
    db.commit()
    direct = ready_video(db, workspace, sample_video, source_account_id=page.id, title="PRIVATE META TITLE")
    via_source = ready_video(db, workspace, sample_video, source_id=source.id)
    copy = ready_video(db, workspace, sample_video, title="Drive copy")
    copy.original_path = copy.encoded_path
    keep = ready_video(db, workspace, sample_video, source_account_id=other.id)
    importer._mark_duplicate(db, copy, direct)
    # A second notice quotes the original even after the duplicate link is released.
    importer._release_duplicates_of(db, direct)
    db.add(
        Job(kind="import_video", payload={"video_id": direct.id, "download_url": "https://meta.cdn/secret"})
    )
    db.commit()
    ids = (direct.id, via_source.id)
    dirs = [storage.media_root() / storage.video_dir_rel(workspace.id, i) for i in ids]
    page_id = page.id
    disconnect(db, page, revoke=False)
    db.commit()
    assert (
        maintenance.run_maintenance(
            db, now=utcnow() + maintenance.DISCONNECTED_RETENTION + timedelta(hours=1)
        )["disconnected_accounts"]
        == 1
    )
    db.commit()
    db.expire_all()
    assert db.get(ConnectedAccount, page_id) is None
    assert all(db.get(Video, i) is not None for i in ids), "imports stay until explicit deletion"
    notices = list(
        db.execute(
            select(ActivityLog).where(ActivityLog.data["duplicate_of_id"].astext == str(ids[0]))
        ).scalars()
    )
    assert len(notices) == 2 and all("PRIVATE META TITLE" in a.message for a in notices)
    request = DataDeletionRequest(provider="meta", external_user_id="deleted-user", confirmation_code="late")
    db.add(request)
    db.commit()
    assert engine_jobs.delete_user_data(db, request.id)
    db.expire_all()
    assert all(db.get(Video, i) is None for i in ids)
    assert all(not d.exists() for d in dirs)
    assert db.get(Video, keep.id) is not None and db.get(Video, copy.id) is not None
    assert db.get(ConnectedAccount, other.id) is not None
    assert db.get(DataDeletionRequest, request.id).status == "completed"
    activity = " ".join(a.message for a in db.execute(select(ActivityLog)).scalars())
    assert "PRIVATE META TITLE" not in activity
    assert "meta.cdn" not in json.dumps([j.payload for j in db.execute(select(Job)).scalars()])


def test_meta_deletion_removes_duplicate_notice_for_an_active_account(
    db, workspace, make_account, sample_video
):
    from repeater.sources import importer

    account = make_account("instagram", extra={"fb_user_id": "active-user"})
    original = ready_video(db, workspace, sample_video, source_account_id=account.id, title="PRIVATE TITLE")
    copy = ready_video(db, workspace, sample_video, title="Drive copy")
    importer._mark_duplicate(db, copy, original)
    request = DataDeletionRequest(provider="meta", external_user_id="active-user", confirmation_code="active")
    db.add(request)
    db.commit()
    assert engine_jobs.delete_user_data(db, request.id)
    assert "PRIVATE TITLE" not in " ".join(a.message for a in db.execute(select(ActivityLog)).scalars())


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
    gone.extra = {**gone.extra, "_disconnected_at": now.isoformat()}
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

    purged: list[int] = []

    def purge(session, *, older_than_days, now=None):
        assert older_than_days == settings.original_retention_days
        purged.append(older_than_days)
        return 4

    monkeypatch.setattr(retention, "purge_expired_originals", purge)
    result = maintenance.run_maintenance(db)
    db.commit()
    assert result["requeued_jobs"] == 1 and result["purged_jobs"] == 1
    assert result["oauth_states"] == 1 and result["activity"] == 1 and result["originals"] == 4
    assert purged == [settings.original_retention_days]
    assert result["stuck_uploads"] == 1 and result["youtube_posts"] == 1
    assert result["disconnected_accounts"] == 0
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
    # A freshly disconnected account is kept for a few days (a reconnect finds its history).
    assert db.get(ConnectedAccount, gone.id) is not None
    assert db.get(ScheduledPost, gone_post.id).platform_post_id == "v3"
    assert db.get(ConnectedAccount, yt.id).extra["channel_id"] == "UC1"

    # Second run: everything is already clean; the retention step reports its count.
    monkeypatch.setattr(retention, "purge_expired_originals", lambda session, *, older_than_days, now=None: 3)
    again = maintenance.run_maintenance(db)
    assert again["originals"] == 3 and again["youtube_posts"] == 0 and again["requeued_posts"] == 0
    assert again["disconnected_accounts"] == 0
    # Retention (C5(b)): the account is deleted, with its posts, a few days after the disconnect.
    gone_id, gone_post_id = gone.id, gone_post.id
    later = maintenance.run_maintenance(db, now=now + maintenance.DISCONNECTED_RETENTION + timedelta(hours=1))
    db.commit()
    assert later["disconnected_accounts"] == 1
    db.expire_all()
    assert db.get(ConnectedAccount, gone_id) is None and db.get(ScheduledPost, gone_post_id) is None


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


def test_notify_default_message_has_no_titles_or_names(telegram):
    """ux-compliance-10: only the kind of event, a count, the workspace and a link leave the
    server unless the operator opted in."""
    text = "Could not publish 'Coin <Trick>' to YouTube (Magic Lap Channel): quotaExceeded"
    with respx.mock() as mock:
        route = mock.post(TELEGRAM_URL).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {}})
        )
        assert notify.notify(text, workspace_id=3, level="error", category="post_needs_attention") is True
        assert notify.notify(text, workspace_id=3, level="error") is True
        assert notify.notify("x", level="error", category="uploads_interrupted", count=3) is True
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert bodies[0]["chat_id"] == "-100200300" and bodies[0]["parse_mode"] == "HTML"
    assert bodies[0]["link_preview_options"] == {"is_disabled": True}
    assert bodies[0]["text"] == (
        "<b>Repeater · Action needed</b> (workspace 3)\n"
        "1 post needs attention. Open the dashboard: https://rotator.test/schedule"
    )
    assert bodies[1]["text"].endswith(
        "Something needs your attention. Open the dashboard: https://rotator.test/"
    )
    assert "3 uploads were interrupted and may be live" in bodies[2]["text"]
    for body in bodies:
        for leaked in ("Coin", "Magic Lap Channel", "quotaExceeded", "YouTube"):
            assert leaked not in body["text"]


def test_notify_sends_escaped_details_only_when_opted_in(telegram, monkeypatch):
    monkeypatch.setattr(notify, "details_enabled", lambda: True)
    with respx.mock() as mock:
        route = mock.post(TELEGRAM_URL).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {}})
        )
        assert notify.notify("Video <Coin & Cup> failed", workspace_id=3, level="error") is True
    body = json.loads(route.calls[0].request.content)
    assert body["text"] == (
        "<b>Repeater · Action needed</b> (workspace 3)\nVideo &lt;Coin &amp; Cup&gt; failed\nhttps://rotator.test/"
    )


def test_notify_minimal_message_has_a_category_for_a_nearly_full_disk():
    text = notify.format_minimal(workspace_id=2, level="warning", category="disk_low")
    assert "disk is nearly full" in text and "https://rotator.test/library" in text
    assert "Something needs your attention" not in text


def test_notify_details_are_off_by_default_and_follow_the_setting(monkeypatch):
    from repeater.config import Settings, get_settings

    assert Settings.model_fields["telegram_include_details"].default is False
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_include_details", False)
    assert notify.details_enabled() is False
    monkeypatch.setattr(settings, "telegram_include_details", True)
    assert notify.details_enabled() is True


def test_notify_details_flag_is_read_from_the_environment(monkeypatch):
    from repeater.config import Settings

    monkeypatch.setenv("REPEATER_TELEGRAM_INCLUDE_DETAILS", "true")
    assert Settings(_env_file=None).telegram_include_details is True
    monkeypatch.setenv("REPEATER_TELEGRAM_INCLUDE_DETAILS", "no")
    assert Settings(_env_file=None).telegram_include_details is False


def test_notify_truncates_long_text(telegram, monkeypatch):
    monkeypatch.setattr(notify, "details_enabled", lambda: True)
    with respx.mock() as mock:
        route = mock.post(TELEGRAM_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
        assert notify.notify("x" * 10_000) is True
    text = json.loads(route.calls[0].request.content)["text"]
    assert len(text) < 4096 and "…\n" in text


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
            events.append(f"join={timeout}")
            return True

    monkeypatch.setattr(worker, "Worker", FakeWorker)
    monkeypatch.setattr(worker, "configure_logging", lambda level: events.append(f"log={level}"))
    monkeypatch.setattr(worker.signal, "signal", lambda signum, fn: installed.__setitem__(signum, fn))
    monkeypatch.setattr(get_settings(), "worker_heartbeat_path", tmp_path / "hb")
    assert worker.main(["--threads", "3", "--log-level", "debug"]) == 0
    grace = get_settings().worker_shutdown_grace_s
    assert events == ["log=DEBUG", "threads=3", "start", "stop", f"join={grace}"]
    assert set(installed) == {signal.SIGTERM, signal.SIGINT}
    assert (tmp_path / "hb").exists()


# =========================================================================== worker: leases, recovery, shutdown


def _running_job(db, kind, payload, *, locked_ago, locked_by="dead:1", key=None):
    job = Job(
        kind=kind,
        payload=payload,
        status=JobStatus.RUNNING,
        dedupe_key=key,
        locked_at=utcnow() - locked_ago,
        locked_by=locked_by,
        attempts=1,
        max_attempts=1,
        run_at=utcnow() - locked_ago,
    )
    db.add(job)
    db.commit()
    return job


def test_tick_requeues_stale_jobs_including_a_stuck_maintenance_job(db, sessions, workspace):
    """engine-2: a maintenance job left RUNNING by a crash used to block the hourly maintenance
    (its dedupe key) and, with it, the only call of requeue_stale."""
    long_ago = timedelta(days=1)
    stuck_maintenance = _running_job(db, kinds.MAINTENANCE, {}, locked_ago=long_ago, key=kinds.MAINTENANCE)
    stuck_plan = _running_job(
        db,
        kinds.PLAN_WORKSPACE,
        {"workspace_id": workspace.id},
        locked_ago=long_ago,
        key=f"plan_workspace:{workspace.id}",
    )
    alive = _running_job(
        db, kinds.POLL_POST, {"post_id": 1}, locked_ago=timedelta(seconds=30), key="poll_post:1"
    )
    counts = worker.tick(sessions, now=datetime(2026, 12, 1, 10, 2, tzinfo=UTC))
    assert counts["requeued_jobs"] == 2
    db.expire_all()
    for job in (stuck_maintenance, stuck_plan):
        row = db.get(Job, job.id)
        assert row.status == JobStatus.QUEUED and row.locked_by is None
    assert db.get(Job, alive.id).status == JobStatus.RUNNING  # its lease is fresh
    # The next tick can enqueue (and claim) maintenance again: the dedupe key is free.
    handlers = {kinds.MAINTENANCE: lambda session, payload: None}
    assert worker.run_once(sessions, "w", handlers=handlers, job_kinds=[kinds.MAINTENANCE]) is True
    db.expire_all()
    assert db.get(Job, stuck_maintenance.id).status == JobStatus.DONE


def test_worker_start_recovers_what_a_dead_predecessor_left_running(db, sessions):
    # Same name (host and pid) as the worker below: certainly dead, requeued at once.
    mine = _running_job(db, "k", {}, locked_ago=timedelta(seconds=5), locked_by="box:7:0")
    # Another process that is simply quiet: left alone until its lease lapses.
    other = _running_job(db, "k", {}, locked_ago=timedelta(seconds=5), locked_by="box:8:0")
    lapsed = _running_job(db, "k", {}, locked_ago=timedelta(hours=2), locked_by="box:9:0")
    w = worker.Worker(
        sessions,
        threads=1,
        poll_seconds=0.05,
        heartbeat_path=None,
        handlers={},
        tick_seconds=3600,
        name="box:7",
    )
    w._recover_after_restart()  # what start() does before it starts the threads
    db.expire_all()
    assert db.get(Job, mine.id).status == JobStatus.QUEUED
    assert db.get(Job, mine.id).attempts == 1  # the crash used an attempt
    assert db.get(Job, lapsed.id).status == JobStatus.QUEUED
    assert db.get(Job, other.id).status == JobStatus.RUNNING


def test_lease_is_renewed_while_a_job_runs(db, sessions):
    """deploy-ops-6: a job that runs longer than the stale timeout is not requeued: the worker keeps
    renewing its lease."""
    seen: dict[str, datetime] = {}

    def slow(session, payload):
        job_id = payload["job_id"]
        with sessions() as other:
            other.execute(update(Job).where(Job.id == job_id).values(locked_at=utcnow() - timedelta(hours=5)))
            other.commit()
        deadline = time_module.monotonic() + 10
        while time_module.monotonic() < deadline:
            with sessions() as other:
                locked_at = other.get(Job, job_id).locked_at
            if locked_at > utcnow() - timedelta(minutes=1):
                seen["renewed"] = locked_at
                return
            time_module.sleep(0.02)

    job_id = queue.enqueue(db, "slow", {})
    job = db.get(Job, job_id)
    job.payload = {"job_id": job_id}
    db.commit()
    assert worker.run_once(sessions, "w1", handlers={"slow": slow}, lease_seconds=0.05) is True
    assert "renewed" in seen
    db.expire_all()
    assert db.get(Job, job_id).status == JobStatus.DONE


def test_lease_thread_stops_with_the_job(db, sessions):
    queue.enqueue(db, "quick", {})
    db.commit()
    before = {t.name for t in threading.enumerate()}
    assert worker.run_once(sessions, "w1", handlers={"quick": lambda s, p: None}, lease_seconds=60) is True
    assert not [t for t in threading.enumerate() if t.name.startswith("lease-") and t.name not in before]


def test_recording_the_outcome_is_retried_after_a_database_error(db, sessions, monkeypatch):
    """engine-2: a DB blip in _finish used to leave the job RUNNING."""
    monkeypatch.setattr(worker, "FINISH_RETRY_DELAYS", (0.0, 0.0, 0.0))
    real = worker._finish
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("connection reset")
        return real(*args, **kwargs)

    monkeypatch.setattr(worker, "_finish", flaky)
    queue.enqueue(db, "k", {})
    db.commit()
    assert worker.run_once(sessions, "w1", handlers={"k": lambda s, p: None}) is True
    assert calls["n"] == 3
    db.expire_all()
    assert db.execute(select(Job)).scalar_one().status == JobStatus.DONE

    monkeypatch.setattr(worker, "_finish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    queue.enqueue(db, "k", {})
    db.commit()
    with pytest.raises(RuntimeError, match="down"):
        worker.run_once(sessions, "w1", handlers={"k": lambda s, p: None})


def test_shutdown_grace_lets_a_running_job_finish(db, sessions):
    started, release = threading.Event(), threading.Event()

    def slow(session, payload):
        started.set()
        assert release.wait(10)

    queue.enqueue(db, "slow", {})
    db.commit()
    w = worker.Worker(
        sessions,
        threads=1,
        poll_seconds=0.05,
        heartbeat_path=None,
        handlers={"slow": slow},
        tick_seconds=3600,
    )
    w.start()
    try:
        assert started.wait(10)
        w.stop()  # SIGTERM
        threading.Timer(0.3, release.set).start()
        assert w.join(10) is True  # finished inside the grace period
    finally:
        release.set()
        w.stop()
    db.expire_all()
    assert db.execute(select(Job).where(Job.kind == "slow")).scalar_one().status == JobStatus.DONE


def test_jobs_still_running_after_the_grace_period_go_back_to_the_queue(db, sessions):
    started, release = threading.Event(), threading.Event()

    def stuck(session, payload):
        started.set()
        release.wait(10)

    queue.enqueue(db, "stuck", {})
    db.commit()
    w = worker.Worker(
        sessions,
        threads=1,
        poll_seconds=0.05,
        heartbeat_path=None,
        handlers={"stuck": stuck},
        tick_seconds=3600,
    )
    w.start()
    try:
        assert started.wait(10)
        w.stop()
        assert w.join(0.2) is False  # grace over, the handler is still busy
        assert w.release_in_flight() == 1
        db.expire_all()
        job = db.execute(select(Job).where(Job.kind == "stuck")).scalar_one()
        assert job.status == JobStatus.QUEUED and job.attempts == 0 and job.locked_by is None
    finally:
        release.set()
        w.join(10)
    db.expire_all()
    # The abandoned handler finishing later does not touch the job another worker may hold now.
    assert db.execute(select(Job).where(Job.kind == "stuck")).scalar_one().status == JobStatus.QUEUED


def test_main_returns_unfinished_jobs_when_the_grace_runs_out(monkeypatch, tmp_path):
    events: list[str] = []
    installed: dict[int, object] = {}

    class SlowWorker:
        def __init__(self, session_factory, *, threads, poll_seconds, heartbeat_path):
            self.stop_event = threading.Event()

        def start(self):
            installed[signal.SIGTERM](signal.SIGTERM, None)

        def stop(self):
            self.stop_event.set()

        def join(self, timeout=None):
            events.append(f"join={timeout}")
            return False

        def release_in_flight(self):
            events.append("release")
            return 2

    monkeypatch.setattr(worker, "Worker", SlowWorker)
    monkeypatch.setattr(worker, "configure_logging", lambda level: None)
    monkeypatch.setattr(worker.signal, "signal", lambda signum, fn: installed.__setitem__(signum, fn))
    monkeypatch.setattr(get_settings(), "worker_heartbeat_path", tmp_path / "hb")
    monkeypatch.setattr(get_settings(), "worker_shutdown_grace_s", 7)
    assert worker.main([]) == 0
    assert events == ["join=7", "release"]


def test_reserved_thread_takes_publishing_while_the_others_encode(db, sessions):
    """engine-7: with two threads, one never takes bulk jobs, so a long encode on the other cannot
    hold up a due publish_post."""
    first_bulk, release = threading.Event(), threading.Event()
    ran: list[str] = []

    def bulk(session, payload):
        ran.append(f"bulk{payload['n']}")
        first_bulk.set()
        release.wait(10)

    def publish(session, payload):
        ran.append("publish")

    for n in (1, 2):
        queue.enqueue(db, "process_video", {"n": n})
    db.commit()
    w = worker.Worker(
        sessions,
        threads=2,
        poll_seconds=0.02,
        heartbeat_path=None,
        handlers={"process_video": bulk, "publish_post": publish},
        tick_seconds=3600,
    )
    assert w.urgent_kinds() == ["publish_post"]
    w.start()
    try:
        assert first_bulk.wait(10)
        queue.enqueue(db, "publish_post", {"post_id": 1}, dedupe_key="publish_post:1")
        db.commit()
        deadline = time_module.monotonic() + 10
        while "publish" not in ran and time_module.monotonic() < deadline:
            time_module.sleep(0.02)
        assert ran.count("publish") == 1
        assert [r for r in ran if r.startswith("bulk")] == ["bulk1"]  # bulk2 waits: no free bulk thread
        db.expire_all()
        queued = db.execute(
            select(Job).where(Job.status == JobStatus.QUEUED, Job.kind.in_(["process_video", "publish_post"]))
        ).scalars()
        assert [j.kind for j in queued] == ["process_video"]
    finally:
        release.set()
        w.stop()
        w.join(10)
    single = worker.Worker(
        sessions, threads=1, poll_seconds=1, heartbeat_path=None, handlers={"publish_post": publish}
    )
    assert single.urgent_kinds() is None  # one thread takes everything, urgent first


def test_tick_and_claim_never_starve_publishing_behind_an_import_backlog(db, sessions):
    t0 = utcnow() - timedelta(hours=2)
    for vid in range(1, 51):
        queue.enqueue(db, kinds.IMPORT_VIDEO, {"video_id": vid}, run_at=t0, dedupe_key=f"import_video:{vid}")
    kinds.enqueue_publish_post(db, 99, run_at=utcnow() - timedelta(minutes=1))
    db.commit()
    ran: list[str] = []
    handlers = {
        kinds.IMPORT_VIDEO: lambda s, p: ran.append("import"),
        kinds.PUBLISH_POST: lambda s, p: ran.append("publish"),
    }
    assert worker.run_once(sessions, "w", handlers=handlers) is True
    assert ran == ["publish"]


import time as time_module  # noqa: E402 - used by the thread tests above

# =========================================================================== maintenance: interrupted uploads (engine-13)


@pytest.fixture
def alerts(monkeypatch) -> list[tuple[str, dict]]:
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(maintenance.notify_mod, "notify", lambda text, **kw: sent.append((text, kw)) or True)
    return sent


def _post_row(db, workspace, account, video, status, key, **kw) -> ScheduledPost:
    post = ScheduledPost(
        workspace_id=workspace.id,
        video_id=video.id,
        connected_account_id=account.id,
        platform=account.platform,
        idempotency_key=key,
        scheduled_at=kw.pop("scheduled_at", utcnow()),
        status=status,
        **kw,
    )
    db.add(post)
    db.commit()
    return post


def test_interrupted_uploads_are_flagged_alerted_once_and_may_be_live(db, workspace, make_account, alerts):
    acc = make_account("tiktok")
    other_ws = Workspace(name="Other", timezone="UTC")
    db.add(other_ws)
    db.commit()
    other_acc = make_account("tiktok", workspace_id=other_ws.id)
    video = Video(workspace_id=workspace.id, status=VideoStatus.READY)
    other_video = Video(workspace_id=other_ws.id, status=VideoStatus.READY)
    db.add_all([video, other_video])
    db.commit()
    posts = [
        _post_row(db, workspace, acc, video, PostStatus.UPLOADING, "u1"),
        _post_row(db, workspace, acc, video, PostStatus.UPLOADING, "u2"),
        _post_row(db, other_ws, other_acc, other_video, PostStatus.UPLOADING, "u3"),
    ]
    fresh = _post_row(db, workspace, acc, video, PostStatus.UPLOADING, "u4")
    for post in posts:
        _age(db, ScheduledPost, post.id, "updated_at", utcnow() - timedelta(hours=3))
    maintenance.handle_maintenance(db, {})
    db.commit()
    db.expire_all()
    for post in posts:
        row = db.get(ScheduledPost, post.id)
        assert row.status == PostStatus.NEEDS_ATTENTION and "interrupted" in row.error
        assert row.publish_state["_engine"]["may_be_live"] is True
    assert db.get(ScheduledPost, fresh.id).status == PostStatus.UPLOADING
    # One alert per workspace, asked for after the commit, with ids and a count only.
    by_ws = {kw["workspace_id"]: (text, kw) for text, kw in alerts}
    assert set(by_ws) == {workspace.id, other_ws.id} and len(alerts) == 2
    text, kw = by_ws[workspace.id]
    assert kw["category"] == "uploads_interrupted" and kw["count"] == 2 and kw["level"] == "error"
    assert f"#{posts[0].id}" in text and f"#{posts[1].id}" in text and "may be live" in text
    alerts.clear()
    maintenance.handle_maintenance(db, {})
    db.commit()
    assert alerts == []  # nothing new to report


def test_no_alert_when_the_flagging_step_rolled_back(db, workspace, make_account, alerts, monkeypatch):
    acc = make_account("tiktok")
    video = Video(workspace_id=workspace.id, status=VideoStatus.READY)
    db.add(video)
    db.commit()
    post = _post_row(db, workspace, acc, video, PostStatus.UPLOADING, "u1")
    _age(db, ScheduledPost, post.id, "updated_at", utcnow() - timedelta(hours=3))
    real = maintenance.log_activity

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(maintenance, "log_activity", explode)
    flagged: list[tuple[int, int]] = []
    result = maintenance.run_maintenance(db, flagged=flagged)
    monkeypatch.setattr(maintenance, "log_activity", real)
    assert result["stuck_uploads"] == -1 and flagged == []
    maintenance._alert_interrupted_uploads(flagged)
    assert alerts == []


# =========================================================================== retention (engine-9, engine-missed-2, C5)


def test_youtube_data_is_expired_on_every_post_that_is_not_in_flight(db, workspace, make_account):
    yt = make_account("youtube")
    video = Video(workspace_id=workspace.id, status=VideoStatus.READY)
    db.add(video)
    db.commit()
    now = utcnow()
    old = now - timedelta(days=31)
    rows = {}
    for i, status in enumerate(
        (
            PostStatus.PUBLISHED,
            PostStatus.NEEDS_ATTENTION,
            PostStatus.CANCELED,
            PostStatus.FAILED,
            PostStatus.PROCESSING,
            PostStatus.UPLOADING,
        )
    ):
        rows[status] = _post_row(
            db,
            workspace,
            yt,
            video,
            status,
            f"k{i}",
            platform_post_id="ytOLD",
            permalink="https://www.youtube.com/shorts/ytOLD",
            publish_state={"video_id": "ytOLD", "_engine": {"processing_since": old.isoformat()}},
            published_at=old if status == PostStatus.PUBLISHED else None,
        )
        _age(db, ScheduledPost, rows[status].id, "updated_at", old)
    recent = _post_row(
        db, workspace, yt, video, PostStatus.NEEDS_ATTENTION, "recent", platform_post_id="ytNEW"
    )
    result = maintenance.run_maintenance(db, now=now)
    db.commit()
    assert result["youtube_posts"] == 4
    db.expire_all()
    for status in (PostStatus.PUBLISHED, PostStatus.NEEDS_ATTENTION, PostStatus.CANCELED, PostStatus.FAILED):
        row = db.get(ScheduledPost, rows[status].id)
        assert row.platform_post_id is None and row.permalink is None, status
        assert "video_id" not in row.publish_state
        if status in (PostStatus.NEEDS_ATTENTION, PostStatus.CANCELED):
            # No API data left, but the planner still knows the upload may be live.
            assert row.publish_state == {"_engine": {"may_be_live": True}}
        else:
            assert row.publish_state == {}
        assert row.updated_at < now - timedelta(days=30)  # the scrub is not a status change
    for status in (PostStatus.PROCESSING, PostStatus.UPLOADING):
        assert db.get(ScheduledPost, rows[status].id).platform_post_id == "ytOLD"  # still polled
    assert db.get(ScheduledPost, recent.id).platform_post_id == "ytNEW"
    again = maintenance.run_maintenance(db, now=now)
    assert again["youtube_posts"] == 0  # nothing left to scrub, nothing touched twice


def test_youtube_links_and_channel_titles_leave_the_activity_log_after_30_days(db, workspace, make_account):
    yt = make_account("youtube", display_name="Magic Lap Channel")
    old, recent = utcnow() - timedelta(days=31), utcnow() - timedelta(days=2)
    permalink = "https://www.youtube.com/shorts/VIDID12345"

    def entry(message, when, **data):
        db.add(
            ActivityLog(
                workspace_id=workspace.id, category="publish", message=message, data=data, created_at=when
            )
        )

    entry(
        "Published 'Coin' to YouTube (Magic Lap Channel).",
        old,
        platform="youtube",
        permalink=permalink,
        account_id=yt.id,
        msg_key="activity.rule_updated_on",  # the dashboard translates these from their parameters
        msg_params={"platform": "YouTube", "name": "Magic Lap Channel"},
    )
    entry(
        "Published 'Coin' to YouTube (Magic Lap Channel).",
        recent,
        platform="youtube",
        permalink=permalink,
        account_id=yt.id,
    )
    entry("Published 'Cup' to Instagram.", old, platform="instagram", permalink="https://instagram.com/x")
    db.commit()
    result = maintenance.run_maintenance(db)
    db.commit()
    assert result["youtube_activity"] == 2
    rows = db.execute(select(ActivityLog).order_by(ActivityLog.id)).scalars().all()
    assert "permalink" not in rows[0].data and "Magic Lap Channel" not in rows[0].message
    assert "YouTube channel" in rows[0].message and rows[0].data["account_id"] == yt.id
    assert rows[0].data["msg_params"] == {"platform": "YouTube", "name": "YouTube channel"}
    assert rows[1].data["permalink"] == permalink and "Magic Lap Channel" in rows[1].message  # < 30 days
    assert rows[2].data["permalink"] == "https://instagram.com/x"  # other platforms are not YouTube data
    assert "VIDID12345" not in " ".join(f"{r.message}{r.data}" for r in rows[:1])


def test_channel_data_that_was_not_refreshed_in_30_days_is_scrubbed(db, workspace, make_account):
    stale = make_account(
        "youtube", display_name="Old Title", extra={"channel_id": "UC1", "custom_url": "@old"}
    )
    stale.avatar_url = "https://yt3.example/a.jpg"
    fresh = make_account(
        "youtube", display_name="Fresh Title", extra={"channel_id": "UC2", "custom_url": "@new"}
    )
    fresh.avatar_url = "https://yt3.example/b.jpg"
    stale.extra = {**stale.extra, "_refreshed_at": (utcnow() - timedelta(days=31)).isoformat()}
    fresh.extra = {**fresh.extra, "_refreshed_at": (utcnow() - timedelta(days=3)).isoformat()}
    db.add(
        ActivityLog(
            workspace_id=workspace.id,
            category="account",
            message="Connected youtube account 'Old Title'",
            data={"account_id": stale.id},
        )
    )
    db.commit()
    result = maintenance.run_maintenance(db)
    db.commit()
    db.expire_all()
    assert result["youtube_channels"] == 1
    stale, fresh = db.get(ConnectedAccount, stale.id), db.get(ConnectedAccount, fresh.id)
    assert stale.display_name == "YouTube channel" and stale.avatar_url is None
    assert stale.extra["channel_id"] == "UC1" and "custom_url" not in stale.extra
    assert fresh.display_name == "Fresh Title" and fresh.avatar_url and fresh.extra["custom_url"] == "@new"
    assert "Old Title" not in db.execute(select(ActivityLog.message)).scalar_one()
    assert maintenance.run_maintenance(db)["youtube_channels"] == 0  # idempotent


def test_accounts_connected_without_a_stamp_count_from_their_creation(db, make_account):
    acc = make_account("youtube", display_name="Title")
    _age(db, ConnectedAccount, acc.id, "created_at", utcnow() - timedelta(days=40))
    assert maintenance.run_maintenance(db)["youtube_channels"] == 1


class FakeChannels:
    """youtube.refresh_account_info stand-in (contract C4)."""

    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def __call__(self, account):
        self.calls.append(account.external_id)
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def youtube_refresher(monkeypatch):
    from repeater.platforms import youtube

    def install(fake):
        monkeypatch.setattr(youtube, "refresh_account_info", fake)
        return fake

    return install


def test_refresh_youtube_channels_re_reads_old_channel_data(db, workspace, make_account, youtube_refresher):
    old = make_account("youtube", display_name="Old Title", extra={"channel_id": "UC1", "custom_url": "@old"})
    recent = make_account("youtube", display_name="Recent", extra={"channel_id": "UC2"})
    old.extra = {**old.extra, "_refreshed_at": (utcnow() - timedelta(days=26)).isoformat()}
    recent.extra = {**recent.extra, "_refreshed_at": (utcnow() - timedelta(days=2)).isoformat()}
    db.add(
        ActivityLog(
            workspace_id=workspace.id,
            category="publish",
            message="Published 'Coin' to YouTube (Old Title).",
            data={"account_id": old.id},
        )
    )
    db.commit()
    fake = youtube_refresher(
        FakeChannels(
            {
                "display_name": "New Title",
                "avatar_url": "https://yt3.example/new.jpg",
                "extra": {"channel_id": "UC1", "uploads_playlist_id": "UU1", "custom_url": "@new"},
            }
        )
    )
    counts = engine_jobs.refresh_youtube_channels(db)
    assert counts == {"refreshed": 1, "failed": 0} and fake.calls == ["youtube-1"]
    db.expire_all()
    acc = db.get(ConnectedAccount, old.id)
    assert acc.display_name == "New Title" and acc.avatar_url == "https://yt3.example/new.jpg"
    assert acc.extra["custom_url"] == "@new" and acc.extra["uploads_playlist_id"] == "UU1"
    assert datetime.fromisoformat(acc.extra["_refreshed_at"]) > utcnow() - timedelta(minutes=1)
    assert db.get(ConnectedAccount, recent.id).display_name == "Recent"
    # The old title is gone from the activity text at once, not only after 30 days.
    assert "Old Title" not in db.execute(select(ActivityLog.message)).scalar_one()
    assert engine_jobs.refresh_youtube_channels(db) == {"refreshed": 0, "failed": 0}  # fresh now


def test_refresh_youtube_channels_handles_failures(db, make_account, youtube_refresher, notifications):
    acc = make_account("youtube", display_name="Title", extra={"channel_id": "UC1"})
    acc.extra = {**acc.extra, "_refreshed_at": (utcnow() - timedelta(days=26)).isoformat()}
    db.commit()
    youtube_refresher(FakeChannels(error=RuntimeError("boom")))
    assert engine_jobs.refresh_youtube_channels(db) == {"refreshed": 0, "failed": 1}
    youtube_refresher(FakeChannels({"display_name": "", "avatar_url": None, "extra": {}}))
    assert engine_jobs.refresh_youtube_channels(db) == {"refreshed": 0, "failed": 1}
    youtube_refresher(FakeChannels(error=AuthError("channel access revoked")))
    assert engine_jobs.refresh_youtube_channels(db) == {"refreshed": 0, "failed": 1}
    db.expire_all()
    acc = db.get(ConnectedAccount, acc.id)
    assert acc.status == AccountStatus.NEEDS_RECONNECT and acc.display_name == "Title"


def test_handle_refresh_tokens_also_refreshes_channels(db, make_account, youtube_refresher):
    acc = make_account("youtube", display_name="Old", extra={"channel_id": "UC1"})
    acc.extra = {**acc.extra, "_refreshed_at": (utcnow() - timedelta(days=30)).isoformat()}
    db.commit()
    youtube_refresher(FakeChannels({"display_name": "New", "avatar_url": None, "extra": {}}))
    engine_jobs.handle_refresh_tokens(db, {})
    db.commit()
    db.expire_all()
    assert db.get(ConnectedAccount, acc.id).display_name == "New"


def test_disconnected_accounts_are_deleted_after_the_retention_period(db, workspace, make_account):
    from repeater.accounts import disconnect, upsert_connected_accounts
    from repeater.platforms.base import ConnectedAccountData

    gone = make_account("facebook", display_name="Magic Page", extra={"page_id": "p1"})
    recent = make_account("facebook", display_name="Recent Page")
    reconnected = make_account("facebook", display_name="Back Page", external_id="fb-back")
    video = Video(workspace_id=workspace.id, status=VideoStatus.READY, source_account_id=gone.id)
    db.add(video)
    db.add_all(
        [
            RotationRule(workspace_id=workspace.id, connected_account_id=gone.id, platform=Platform.FACEBOOK),
        ]
    )
    db.commit()
    post = _post_row(
        db,
        workspace,
        gone,
        video,
        PostStatus.PUBLISHED,
        "g1",
        platform_post_id="fb1",
        permalink="https://fb/x",
    )
    db.add_all(
        [
            ActivityLog(
                workspace_id=workspace.id,
                category="publish",
                message="Published 'Coin' to Facebook (Magic Page).",
                data={"account_id": gone.id, "permalink": "https://fb/x"},
            ),
            ActivityLog(workspace_id=workspace.id, category="system", message="Unrelated entry", data={}),
        ]
    )
    db.commit()
    for account in (gone, recent, reconnected):
        disconnect(db, db.get(ConnectedAccount, account.id), revoke=False)
    db.commit()
    gone_id, recent_id, post_id, video_id = gone.id, recent.id, post.id, video.id
    # The disconnect itself is not logged as a way to find the person later.
    now = utcnow()
    assert maintenance.run_maintenance(db, now=now + timedelta(days=2))["disconnected_accounts"] == 0
    # `reconnected` comes back before the deadline: its row and history are kept.
    upsert_connected_accounts(
        db,
        workspace.id,
        [
            ConnectedAccountData(
                platform=Platform.FACEBOOK,
                external_id="fb-back",
                display_name="Back Page",
                tokens=OAuthTokens(
                    access_token="a", refresh_token="r", expires_at=utcnow() + timedelta(hours=1)
                ),
            )
        ],
    )
    db.commit()
    # `recent` was disconnected later than `gone`.
    db.get(ConnectedAccount, recent_id).extra = {
        **db.get(ConnectedAccount, recent_id).extra,
        "_disconnected_at": (now + timedelta(days=3)).isoformat(),
    }
    db.commit()
    result = maintenance.run_maintenance(
        db, now=now + maintenance.DISCONNECTED_RETENTION + timedelta(hours=1)
    )
    db.commit()
    assert result["disconnected_accounts"] == 1
    db.expire_all()
    assert db.get(ConnectedAccount, gone_id) is None and db.get(ScheduledPost, post_id) is None
    assert db.execute(select(RotationRule)).first() is None
    assert db.get(ConnectedAccount, recent_id) is not None
    assert db.get(ConnectedAccount, reconnected.id).status == AccountStatus.ACTIVE
    assert "_disconnected_at" not in db.get(ConnectedAccount, reconnected.id).extra
    text = " ".join(f"{a.message} {a.data}" for a in db.execute(select(ActivityLog)).scalars())
    assert "Magic Page" not in text and "https://fb/x" not in text and "Unrelated entry" in text
    assert "Recent Page" in text  # entries of accounts that are still within the period stay
    kept = db.get(Video, video_id)
    assert kept is not None and kept.source_account_id is None  # library videos stay


def test_originals_of_failed_videos_and_abandoned_partial_files_are_purged(db, workspace):
    root = storage.media_root()
    failed = Video(workspace_id=workspace.id, status=VideoStatus.FAILED, error="encode failed")
    failed_recent = Video(workspace_id=workspace.id, status=VideoStatus.FAILED, error="encode failed")
    ready = Video(workspace_id=workspace.id, status=VideoStatus.READY)
    db.add_all([failed, failed_recent, ready])
    db.flush()
    paths = {}
    for video in (failed, failed_recent, ready):
        d = storage.ensure_video_dir(workspace.id, video.id)
        original = d / "original.mp4"
        original.write_bytes(b"x")
        video.original_path = storage.rel_path(original)
        paths[video.id] = original
    db.commit()
    now = utcnow()
    _age(db, Video, failed.id, "updated_at", now - timedelta(days=8))
    _age(db, Video, failed_recent.id, "updated_at", now - timedelta(days=2))
    # Partial files: a killed encode, a half dashboard upload, and a fresh one that is in use.
    vdir = storage.ensure_video_dir(workspace.id, ready.id)
    old_part = vdir / ".encoded.mp4.abc123def456.part"
    old_upload = storage.uploads_dir() / "deadbeef.part"
    fresh_part = vdir / ".encoded.mp4.fresh.part"
    for p in (old_part, old_upload, fresh_part):
        p.write_bytes(b"partial")
    import os

    for p in (old_part, old_upload):
        os.utime(p, (now.timestamp() - 2 * 86400, now.timestamp() - 2 * 86400))
    result = maintenance.run_maintenance(db, now=now)
    db.commit()
    assert result["failed_originals"] == 1 and result["partial_files"] == 2
    db.expire_all()
    assert not paths[failed.id].exists() and db.get(Video, failed.id).original_path is None
    assert paths[failed_recent.id].exists() and paths[ready.id].exists()
    assert not old_part.exists() and not old_upload.exists() and fresh_part.exists()
    assert root.exists()


# =========================================================================== accounts: locking (engine-12)


def test_refresh_job_skips_an_account_disconnected_while_it_runs(db, workspace, make_account, monkeypatch):
    from repeater.accounts import disconnect

    monkeypatch.setattr(engine_jobs.notify_mod, "notify", lambda *a, **k: True)
    a = make_account("youtube", expires_in=timedelta(minutes=30))
    b = make_account("youtube", expires_in=timedelta(minutes=30))
    calls: list[int] = []

    class Provider:
        name, uses_pkce = "google", False

        def refresh(self, ctx):
            calls.append(ctx.account_id)
            if len(calls) == 1:
                # While the job refreshes the first channel the owner disconnects the second one.
                with get_sessionmaker()() as web:
                    disconnect(web, web.get(ConnectedAccount, b.id), revoke=False)
                    web.commit()
            return OAuthTokens(
                access_token=f"new-{ctx.account_id}",
                refresh_token="r",
                expires_at=utcnow() + timedelta(hours=1),
            )

    with override("oauth", "google", Provider()):
        counts = engine_jobs.refresh_tokens(db)
    assert counts == {"refreshed": 1, "needs_reconnect": 0, "failed": 0} and calls == [a.id]
    with get_sessionmaker()() as s:
        row = s.get(ConnectedAccount, b.id)
        assert row.status == AccountStatus.DISABLED and row.token_enc is None


def test_refresh_job_does_not_flip_a_disconnected_account_to_needs_reconnect(
    db, workspace, make_account, notifications
):
    from repeater.accounts import disconnect

    a = make_account("youtube", expires_in=timedelta(minutes=30))
    b = make_account("youtube", expires_in=timedelta(minutes=30))
    calls: list[int] = []

    class Provider:
        name, uses_pkce = "google", False

        def refresh(self, ctx):
            calls.append(ctx.account_id)
            if len(calls) == 1:
                with get_sessionmaker()() as web:
                    disconnect(web, web.get(ConnectedAccount, b.id), revoke=False)
                    web.commit()
            raise AuthError("invalid_grant")

    with override("oauth", "google", Provider()):
        engine_jobs.refresh_tokens(db)
    with get_sessionmaker()() as s:
        assert s.get(ConnectedAccount, b.id).status == AccountStatus.DISABLED
        assert s.get(ConnectedAccount, a.id).status == AccountStatus.NEEDS_RECONNECT
    assert len(notifications) == 1 and "must be reconnected" in notifications[0][0]


def test_fresh_context_does_not_refresh_again_when_someone_else_just_did(db, make_account):
    """Providers may rotate refresh tokens: a second refresh with the old token would fail and
    wrongly mark the account for reconnection."""
    from repeater.accounts import fresh_context, save_tokens

    acc = make_account("tiktok", expires_in=timedelta(minutes=5))
    stale_view = acc  # this session's object still shows the old, expiring token
    with get_sessionmaker()() as other:
        row = other.get(ConnectedAccount, acc.id)
        save_tokens(
            row,
            OAuthTokens(
                access_token="rotated", refresh_token="r2", expires_at=utcnow() + timedelta(hours=24)
            ),
        )
        other.commit()
    calls: list[int] = []

    class Provider:
        name, uses_pkce = "tiktok", False

        def refresh(self, ctx):
            calls.append(1)
            raise AuthError("refresh token already used")

    with override("oauth", "tiktok", Provider()):
        ctx = fresh_context(db, stale_view)
    assert calls == [] and ctx.tokens.access_token == "rotated"
    assert stale_view.status == AccountStatus.ACTIVE


def test_fresh_context_refuses_an_account_disconnected_since_it_was_loaded(db, make_account):
    from repeater.accounts import disconnect, fresh_context

    acc = make_account("tiktok", expires_in=timedelta(minutes=5))
    with get_sessionmaker()() as web:
        disconnect(web, web.get(ConnectedAccount, acc.id), revoke=False)
        web.commit()
    from repeater.accounts import AccountUnavailable

    with pytest.raises(AccountUnavailable):
        fresh_context(db, acc)
    with get_sessionmaker()() as s:
        assert s.get(ConnectedAccount, acc.id).token_enc is None  # nothing was written back
