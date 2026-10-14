"""Publishing pipeline, captions and dashboard actions (repeater.engine.publisher, captions,
actions)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import pytest
import respx
from sqlalchemy import select

from repeater.accounts import load_tokens
from repeater.db import get_sessionmaker
from repeater.engine import actions, captions, publisher, rules
from repeater.jobs import kinds
from repeater.media import storage
from repeater.models import (
    AccountStatus,
    ActivityLevel,
    ActivityLog,
    Job,
    JobStatus,
    Platform,
    PostStatus,
    RotationRule,
    ScheduledPost,
    Video,
    VideoStatus,
)
from repeater.platforms import override
from repeater.platforms.base import (
    LIMITS,
    AuthError,
    NotSupportedError,
    OAuthTokens,
    PermanentError,
    PublishOutcome,
    PublishResult,
    RateLimitedError,
    RestartPublish,
    RetryableError,
    UnknownOutcomeError,
)
from repeater.queue import RetryLater
from repeater.timeutil import utcnow

# =========================================================================== fixtures & fakes


@pytest.fixture
def notifications(monkeypatch) -> list[tuple[str, dict]]:
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(publisher.notify_mod, "notify", lambda text, **kw: sent.append((text, kw)) or True)
    return sent


@dataclass
class FakePublisher:
    """Scripted adapter: each publish()/poll() call takes the next item (a PublishResult or an
    exception to raise)."""

    platform: Platform
    publish_script: list[Any] = field(default_factory=list)
    poll_script: list[Any] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    requests: list[Any] = field(default_factory=list)
    poll_states: list[dict] = field(default_factory=list)
    on_publish: Any = None

    @property
    def limits(self):
        return LIMITS[self.platform]

    def validate(self, account, req):
        self.requests.append(req)
        return list(self.problems)

    def publish(self, account, req):
        if self.on_publish:
            self.on_publish(req)
        return self._next(self.publish_script)

    def poll(self, account, state):
        self.poll_states.append(state)
        return self._next(self.poll_script)

    @staticmethod
    def _next(script: list[Any]):
        item = script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


PUBLISHED = PublishResult(
    PublishOutcome.PUBLISHED, platform_post_id="yt-123", permalink="https://youtu.be/yt-123"
)


@pytest.fixture
def setup(db, workspace, make_account, sample_video):
    """A YouTube account with an enabled rule, a READY video with a real encoded file and one
    post scheduled now."""

    def _make(platform: str = "youtube", **post_kw) -> tuple[ScheduledPost, Video, Any, RotationRule]:
        acc = make_account(platform, display_name=f"Magic Lab {platform}")
        rule = rules.ensure_rule(db, acc)
        rule.enabled = True
        rule.hashtags = "magic"
        video = Video(
            workspace_id=workspace.id,
            status=VideoStatus.READY,
            title="Coin trick",
            caption="Watch this coin vanish\n\n\n\nFollow for more",
            duration_s=4.0,
            width=1080,
            height=1920,
            fps=30.0,
        )
        db.add(video)
        db.flush()
        vdir = storage.ensure_video_dir(workspace.id, video.id)
        shutil.copy(sample_video, vdir / storage.ENCODED_NAME)
        (vdir / storage.THUMB_NAME).write_bytes(b"\xff\xd8\xff")
        video.encoded_path = storage.rel_path(vdir / storage.ENCODED_NAME)
        video.thumbnail_path = storage.rel_path(vdir / storage.THUMB_NAME)
        video.encoded_size_bytes = (vdir / storage.ENCODED_NAME).stat().st_size
        post = ScheduledPost(
            workspace_id=workspace.id,
            video_id=video.id,
            connected_account_id=acc.id,
            platform=Platform(platform),
            cycle=0,
            idempotency_key=f"{video.id}:{acc.id}:0",
            scheduled_at=post_kw.pop("scheduled_at", utcnow()),
            created_by=post_kw.pop("created_by", "scheduler"),
            **post_kw,
        )
        db.add(post)
        db.commit()
        return post, video, acc, rule

    return _make


def fake(platform: str = "youtube", **kw) -> FakePublisher:
    return FakePublisher(Platform(platform), **kw)


def jobs(db, kind: str) -> list[Job]:
    return list(db.execute(select(Job).where(Job.kind == kind).order_by(Job.id)).scalars())


def test_publish_now_restarts_a_terminal_tiktok_upload(db, setup):
    """A deliberate retry after fixing the rule must not keep polling a dead publish_id."""
    from repeater.platforms import tiktok as tt

    post, _, _, rule = setup(
        "tiktok",
        status=PostStatus.NEEDS_ATTENTION,
        publish_state={"publish_id": "dead-upload", "source": "FILE_UPLOAD"},
        error="TikTok rejected the video format.",
        attempts=4,
    )
    rule.settings = {
        **rule.settings,
        "privacy_level": "SELF_ONLY",
        "music_consent_at": utcnow().isoformat(),
    }
    db.commit()
    with respx.mock(assert_all_called=False) as router:
        router.post(tt.CREATOR_INFO_URL).respond(
            200,
            json={
                "data": {"privacy_level_options": ["SELF_ONLY"], "max_video_post_duration_sec": 300},
                "error": {"code": "ok"},
            },
        )
        dead = router.post(tt.STATUS_URL).respond(
            200,
            json={
                "data": {"status": "FAILED", "fail_reason": "file_format_check_failed"},
                "error": {"code": "ok"},
            },
        )
        init = router.post(tt.DIRECT_INIT_URL).respond(
            200,
            json={
                "data": {"publish_id": "fresh-upload"},
                "error": {"code": "ok"},
            },
        )
        actions.publish_now(db, post, actor="owner")
        db.commit()
        result = publisher.publish_post(db, post.id)
    assert result.status == PostStatus.PROCESSING
    assert init.call_count == 1 and dead.call_count == 0
    db.refresh(post)
    assert post.publish_state["publish_id"] == "fresh-upload" and post.attempts == 0


@pytest.mark.parametrize("status", [PostStatus.SCHEDULED, PostStatus.FAILED])
def test_publish_now_keeps_recoverable_upload_state(db, setup, status):
    post, _, _, _ = setup(status=status, publish_state={"publish_id": "possibly-live"})
    actions.publish_now(db, post, actor="owner")
    assert post.publish_state == {"publish_id": "possibly-live"}


def activities(db, level: ActivityLevel | None = None) -> list[ActivityLog]:
    q = select(ActivityLog).order_by(ActivityLog.id)
    if level is not None:
        q = q.where(ActivityLog.level == level)
    return list(db.execute(q).scalars())


# =========================================================================== captions


def _video(**kw) -> Video:
    return Video(
        id=kw.pop("id", 7),
        title=kw.pop("title", ""),
        caption=kw.pop("caption", ""),
        overrides=kw.pop("overrides", {}),
        **kw,
    )


def _rule(**kw) -> RotationRule:
    return RotationRule(
        caption_template=kw.pop("caption_template", "{caption}"), hashtags=kw.pop("hashtags", ""), **kw
    )


def test_render_template_variables_and_tidy():
    v = _video(title="Coin trick", caption="  Watch this\r\n\r\n\r\n\r\nFollow  \n")
    rule = _rule(
        caption_template="{title}\n\n{caption}\n\n\n\n{hashtags}\n{source_caption} {unknown}",
        hashtags="magic, #Tricks magic",
    )
    title, caption = captions.render(rule, v, "instagram")
    assert title == "Coin trick"
    assert caption == "Coin trick\n\nWatch this\n\nFollow\n\n#magic #Tricks\nWatch this\n\nFollow {unknown}"


def test_render_overrides_and_title_fallbacks():
    v = _video(
        title="",
        caption="\n  First line here  \nsecond",
        overrides={"title": " Custom ", "caption": "Override text"},
    )
    assert captions.render(_rule(), v, "facebook") == ("Custom", "Override text")
    v.overrides = {}
    assert captions.render(_rule(), v, "facebook")[0] == "First line here"
    v.title = "Stored title"
    assert captions.render(None, v, "facebook") == (
        "Stored title",
        "First line here  \nsecond".replace("  \n", "\n"),
    )


def test_render_appends_hashtags_when_template_has_none():
    v = _video(caption="Hello")
    assert captions.render(_rule(hashtags="#a b"), v, "tiktok")[1] == "Hello\n\n#a #b"
    assert captions.render(_rule(hashtags=""), v, "tiktok")[1] == "Hello"


def test_youtube_limits_bytes_and_title():
    v = _video(title="ع" * 150, caption="ع" * 3000)  # Arabic letters are 2 bytes in UTF-8
    title, caption = captions.render(_rule(), v, "youtube")
    assert len(title) == 100
    assert len(caption.encode("utf-8")) <= 5000 and caption == "ع" * 2500
    emoji = _video(caption="a" + "\U0001f600" * 2000)  # 4 bytes each
    _, caption = captions.render(_rule(), emoji, "youtube")
    assert len(caption.encode("utf-8")) <= 5000 and caption.endswith("\U0001f600")
    caption.encode("utf-8")  # no broken surrogates


def test_youtube_removes_angle_brackets():
    v = _video(title="<b>Best</b> trick", caption="5 > 3 and <3")
    assert captions.render(_rule(), v, "youtube") == ("bBest/b trick", "5  3 and 3")
    assert captions.render(_rule(), v, "facebook")[1] == "5 > 3 and <3"


def test_tiktok_counts_utf16_and_keeps_clusters_whole():
    family = "\U0001f468‍\U0001f469‍\U0001f467"  # 8 UTF-16 units
    v = _video(caption="x" * 2195 + family)
    _, caption = captions.render(_rule(), v, "tiktok")
    assert caption == "x" * 2195  # the ZWJ sequence does not fit and is not split
    flag = "\U0001f1ea\U0001f1ec"  # Egypt flag, 4 units
    v = _video(caption="x" * 2197 + flag)
    assert captions.render(_rule(), v, "tiktok")[1] == "x" * 2197
    v = _video(caption="ب" + "َ" * 2300)  # a letter with many harakat is never split off
    assert captions.render(_rule(), v, "tiktok")[1] == ""


def test_instagram_hashtag_limit_and_char_limit():
    tags = " ".join(f"#t{i}" for i in range(40))
    v = _video(caption=f"Intro {tags} end")
    _, caption = captions.render(_rule(), v, "instagram")
    assert caption.count("#") == 30 and caption.endswith("#t29 end")
    v = _video(caption="y" * 3000)
    assert len(captions.render(_rule(), v, "instagram")[1]) == 2200


def test_labels():
    assert captions.platform_name("youtube") == "YouTube"
    assert captions.video_label(_video(id=3)) == "#3"
    assert captions.video_label(_video(caption="\nhello world\nmore")) == "hello world"


# =========================================================================== publish outcomes


def test_publish_published(db, setup, notifications):
    post, video, acc, rule = setup()
    seen: dict[str, Any] = {}

    def check_uploading(req):
        with get_sessionmaker()() as other:
            seen["status"] = other.get(ScheduledPost, post.id).status

    pub = fake(publish_script=[PUBLISHED], on_publish=check_uploading)
    with override("publisher", "youtube", pub):
        step = publisher.publish_post(db, post.id)
    assert step.status == PostStatus.PUBLISHED and step.retry_in is None
    assert seen["status"] == PostStatus.UPLOADING  # committed before the network call
    db.refresh(post)
    assert post.status == PostStatus.PUBLISHED and post.platform_post_id == "yt-123"
    assert post.permalink == "https://youtu.be/yt-123" and post.published_at is not None
    assert post.caption == "Watch this coin vanish\n\nFollow for more\n\n#magic"
    req = pub.requests[0]
    assert req.post_id == post.id and req.video_path.is_absolute() and req.video_path.is_file()
    assert req.size_bytes == video.encoded_size_bytes and (req.width, req.height) == (1080, 1920)
    assert req.duration_s == 4.0 and req.title == "Coin trick" and req.settings == rule.settings
    assert req.thumbnail_path is not None and req.thumbnail_path.is_file()
    url = urlparse(req.public_url)
    assert url.scheme == "https" and url.netloc == "rotator.test"
    _, m, vid, expires, _sig = url.path.split("/")
    assert m == "m" and vid == str(video.id)
    assert 23.9 * 3600 < int(expires) - utcnow().timestamp() <= 24 * 3600
    info = activities(db, ActivityLevel.INFO)[-1]
    assert "Published 'Coin trick' to YouTube (Magic Lab youtube)" in info.message
    assert info.data["permalink"] == "https://youtu.be/yt-123"
    assert notifications == []


def test_publish_processing_then_poll(db, setup):
    post, *_ = setup("instagram")
    processing = PublishResult(PublishOutcome.PROCESSING, state={"container_id": "c1"}, poll_after_seconds=90)
    pub = fake("instagram", publish_script=[processing])
    now = utcnow()
    with override("publisher", "instagram", pub):
        step = publisher.publish_post(db, post.id, now=now)
    assert step.status == PostStatus.PROCESSING and step.retry_in is None
    db.refresh(post)
    assert post.publish_state["container_id"] == "c1"
    assert post.publish_state["_engine"]["processing_since"] == now.isoformat()
    (job,) = jobs(db, "poll_post")
    assert job.payload == {"post_id": post.id} and job.run_at == now + timedelta(seconds=90)

    still = PublishResult(
        PublishOutcome.PROCESSING, state={"container_id": "c1", "checks": 1}, poll_after_seconds=60
    )
    done = PublishResult(
        PublishOutcome.PUBLISHED, platform_post_id="ig-9", permalink="https://instagram.com/p/x"
    )
    pub.poll_script = [still, done]
    later = now + timedelta(minutes=2)
    with override("publisher", "instagram", pub):
        step = publisher.poll_post(db, post.id, now=later)
        assert step.status == PostStatus.PROCESSING
        db.refresh(post)
        assert post.publish_state["checks"] == 1
        assert post.publish_state["_engine"]["processing_since"] == now.isoformat()
        (job,) = jobs(db, "poll_post")
        assert job.run_at == later + timedelta(seconds=60)
        step = publisher.poll_post(db, post.id, now=later + timedelta(minutes=1))
    assert step.status == PostStatus.PUBLISHED
    assert pub.poll_states[0] == {"container_id": "c1"}  # engine bookkeeping is not passed on
    db.refresh(post)
    assert post.platform_post_id == "ig-9" and "_engine" not in post.publish_state


def test_poll_running_job_gets_retry_in(db, setup):
    post, *_ = setup("instagram")
    post.status = PostStatus.PROCESSING
    now = utcnow()
    post.publish_state = {"container_id": "c1", "_engine": {"processing_since": now.isoformat()}}
    db.add(
        Job(
            kind="poll_post",
            payload={"post_id": post.id},
            status=JobStatus.RUNNING,
            dedupe_key=f"poll_post:{post.id}",
        )
    )
    db.commit()
    pub = fake(
        "instagram",
        poll_script=[
            PublishResult(PublishOutcome.PROCESSING, state={"container_id": "c1"}, poll_after_seconds=45)
        ],
    )
    with override("publisher", "instagram", pub):
        step = publisher.poll_post(db, post.id, now=now)
    assert step.retry_in == 45 and step.status == PostStatus.PROCESSING


def test_poll_timeout_needs_attention(db, setup, notifications):
    """After 2 hours the platform is asked one last time; only a poll that still answers
    PROCESSING gives up (the video may be live, so the post is flagged as such)."""
    post, *_ = setup("instagram")
    post.status = PostStatus.PROCESSING
    started = utcnow() - timedelta(hours=2, minutes=1)
    post.publish_state = {"container_id": "c1", "_engine": {"processing_since": started.isoformat()}}
    db.commit()
    still = PublishResult(PublishOutcome.PROCESSING, state={"container_id": "c1"})
    pub = fake("instagram", poll_script=[still])
    with override("publisher", "instagram", pub):
        step = publisher.poll_post(db, post.id)
    assert step.status == PostStatus.NEEDS_ATTENTION and len(pub.poll_states) == 1
    assert "within 2 hours" in post.error
    assert publisher.engine_state(post.publish_state)["may_be_live"] is True
    assert len(notifications) == 1 and notifications[0][1]["level"] == "error"
    assert not jobs(db, "poll_post")


def test_poll_after_two_hours_still_asks_the_platform(db, setup, notifications):
    """engine-4: a worker that was down for 2 h 5 min (or a paused workspace) must not flag a post
    that is live; one status check decides."""
    post, *_ = setup("youtube")
    post.status = PostStatus.PROCESSING
    started = utcnow() - timedelta(hours=2, minutes=5)
    post.publish_state = {"video_id": "yt-1", "_engine": {"processing_since": started.isoformat()}}
    db.commit()
    done = PublishResult(PublishOutcome.PUBLISHED, platform_post_id="yt-1", state={"video_id": "yt-1"})
    pub = fake(poll_script=[done])
    with override("publisher", "youtube", pub):
        step = publisher.poll_post(db, post.id)
    assert step.status == PostStatus.PUBLISHED and len(pub.poll_states) == 1
    assert notifications == []


def test_poll_keeps_polling_a_paused_workspace_for_platforms_that_are_already_live(
    db, setup, workspace, notifications
):
    post, *_ = setup("youtube")
    post.status = PostStatus.PROCESSING
    t0 = utcnow()
    post.publish_state = {"video_id": "yt-1", "_engine": {"processing_since": t0.isoformat()}}
    workspace.paused = True
    db.commit()
    still = PublishResult(PublishOutcome.PROCESSING, state={"video_id": "yt-1"})
    done = PublishResult(PublishOutcome.PUBLISHED, platform_post_id="yt-1", state={"video_id": "yt-1"})
    pub = fake(poll_script=[still, done])
    with override("publisher", "youtube", pub):
        assert publisher.poll_post(db, post.id, now=t0 + timedelta(hours=1)).status == PostStatus.PROCESSING
        assert publisher.poll_post(db, post.id, now=t0 + timedelta(hours=3)).status == PostStatus.PUBLISHED
    assert len(pub.poll_states) == 2 and notifications == []


def test_poll_error_then_timeout_never_without_an_answer(db, setup, notifications):
    """A status check that keeps failing does not flag the post after 2 hours, only after the
    error ceiling; a rate limit with a long retry_after keeps the upload PROCESSING (api-missed-1)."""
    post, *_ = setup("youtube")
    post.status = PostStatus.PROCESSING
    t0 = utcnow() - timedelta(hours=3)
    post.publish_state = {"video_id": "yt-1", "_engine": {"processing_since": t0.isoformat()}}
    db.commit()
    quota = RateLimitedError("quotaExceeded", retry_after=16 * 3600)
    now = utcnow()
    with override("publisher", "youtube", fake(poll_script=[quota])):
        step = publisher.poll_post(db, post.id, now=now)
    assert step.status == PostStatus.PROCESSING and notifications == []
    (job,) = jobs(db, "poll_post")
    assert job.run_at >= now + timedelta(hours=16)
    # 16 h later the check works and the upload is live: PUBLISHED, not needs-attention.
    done = PublishResult(PublishOutcome.PUBLISHED, platform_post_id="yt-1", state={"video_id": "yt-1"})
    with override("publisher", "youtube", fake(poll_script=[done])):
        step = publisher.poll_post(db, post.id, now=job.run_at)
    assert step.status == PostStatus.PUBLISHED
    # Failing checks give up only after POLL_ERROR_CEILING.
    post2, *_ = setup("youtube")
    post2.status = PostStatus.PROCESSING
    ancient = utcnow() - publisher.POLL_ERROR_CEILING - timedelta(minutes=1)
    post2.publish_state = {"video_id": "yt-2", "_engine": {"processing_since": ancient.isoformat()}}
    db.commit()
    with override("publisher", "youtube", fake(poll_script=[RetryableError("HTTP 500")])):
        step = publisher.poll_post(db, post2.id)
    assert step.status == PostStatus.NEEDS_ATTENTION and len(notifications) == 1
    assert publisher.engine_state(post2.publish_state)["may_be_live"] is True


def test_poll_transient_error_polls_again(db, setup):
    post, *_ = setup("instagram")
    post.status = PostStatus.PROCESSING
    now = utcnow()
    post.publish_state = {"_engine": {"processing_since": now.isoformat()}}
    db.commit()
    pub = fake("instagram", poll_script=[RetryableError("Graph API 500", retry_after=300), KeyError("boom")])
    with override("publisher", "instagram", pub):
        publisher.poll_post(db, post.id, now=now)
        assert post.status == PostStatus.PROCESSING and "Graph API 500" in post.error
        assert jobs(db, "poll_post")[0].run_at == now + timedelta(seconds=300)
        publisher.poll_post(db, post.id, now=now)
    assert post.status == PostStatus.PROCESSING and "KeyError" in post.error


@pytest.mark.parametrize(
    "exc",
    [PermanentError("Container failed: ERROR"), NotSupportedError("nope"), AuthError("token revoked")],
)
def test_poll_terminal_errors(db, setup, notifications, exc):
    post, _video, acc, _rule = setup("instagram")
    post.status = PostStatus.PROCESSING
    post.publish_state = {"_engine": {"processing_since": utcnow().isoformat()}}
    db.commit()
    with override("publisher", "instagram", fake("instagram", poll_script=[exc])):
        step = publisher.poll_post(db, post.id)
    assert step.status == PostStatus.NEEDS_ATTENTION and exc.message in post.error
    assert (acc.status == AccountStatus.NEEDS_RECONNECT) == isinstance(exc, AuthError)


def test_poll_paused_or_disconnected(db, setup, workspace, notifications):
    post, _video, acc, _rule = setup("instagram")
    post.status = PostStatus.PROCESSING
    now = utcnow()
    post.publish_state = {"container_id": "c1", "_engine": {"processing_since": now.isoformat()}}
    workspace.paused = True
    db.commit()
    pub = fake("instagram")
    with override("publisher", "instagram", pub):
        step = publisher.poll_post(db, post.id, now=now)
    # Instagram's poll would publish the container, so a paused workspace is not polled (and the
    # wait does not count against the 2-hour limit).
    assert step.status == PostStatus.PROCESSING and pub.poll_states == []
    assert jobs(db, "poll_post")[0].run_at == now + timedelta(minutes=10)
    assert publisher._processing_since(post, now) == now
    for minutes in range(10, 400, 10):
        step = publisher.poll_post(db, post.id, now=now + timedelta(minutes=minutes))
    assert step.status == PostStatus.PROCESSING and pub.poll_states == []
    # Disconnected: the upload may be live, so never CANCELED (the planner would re-use it) and no
    # alert for something the user did themselves.
    acc.status = AccountStatus.DISABLED
    db.commit()
    assert publisher.poll_post(db, post.id, now=now).status == PostStatus.NEEDS_ATTENTION
    assert "disconnected" in post.error and publisher.engine_state(post.publish_state)["may_be_live"]
    assert post.publish_state["container_id"] == "c1" and notifications == []
    assert publisher.poll_post(db, post.id).status == PostStatus.NEEDS_ATTENTION  # no-op now


def test_poll_permanent_error_that_may_have_published_is_flagged_may_be_live(db, setup, notifications):
    """Instagram gives up on an unanswered media_publish with code publish_unconfirmed: the reel may
    be live, so the post is flagged (the planner counts it, the dashboard asks before a republish)."""
    from repeater.engine import actions

    post, _video, _acc, _rule = setup("instagram")
    post.status = PostStatus.PROCESSING
    now = utcnow()
    post.publish_state = {"container_id": "c1", "_engine": {"processing_since": now.isoformat()}}
    db.commit()
    pub = fake("instagram", poll_script=[PermanentError("The reel may be live.", code="publish_unconfirmed")])
    with override("publisher", "instagram", pub):
        step = publisher.poll_post(db, post.id, now=now)
    assert step.status == PostStatus.NEEDS_ATTENTION
    assert publisher.engine_state(post.publish_state)["may_be_live"] is True
    assert actions.may_be_live(post)
    # Any other permanent poll error stays a plain needs-attention (nothing hints at a live post).
    other, _v, _a, _r = setup("instagram")
    other.status = PostStatus.PROCESSING
    other.publish_state = {"_engine": {}}
    db.commit()
    pub = fake("instagram", poll_script=[PermanentError("Rejected.", code="policy")])
    with override("publisher", "instagram", pub):
        publisher.poll_post(db, other.id, now=now)
    assert "may_be_live" not in publisher.engine_state(other.publish_state)


# =========================================================================== publish errors


def test_retry_ladder_then_needs_attention(db, setup, notifications):
    post, *_ = setup()
    pub = fake(publish_script=[RetryableError("HTTP 503") for _ in range(4)])
    now = utcnow()
    expected = [5, 30, 120]
    with override("publisher", "youtube", pub):
        for attempt, minutes in enumerate(expected, start=1):
            step = publisher.publish_post(db, post.id, now=now)
            db.refresh(post)
            assert step.status == PostStatus.FAILED and post.attempts == attempt
            assert post.scheduled_at == now + timedelta(minutes=minutes)
            assert f"Attempt {attempt} failed: HTTP 503" in post.error
            (job,) = [j for j in jobs(db, "publish_post") if j.status == JobStatus.QUEUED]
            assert job.run_at == post.scheduled_at
            now = post.scheduled_at
        step = publisher.publish_post(db, post.id, now=now)
    db.refresh(post)
    assert step.status == PostStatus.NEEDS_ATTENTION and post.attempts == 4
    assert "gave up after 4 attempts" in post.error
    assert len(notifications) == 1 and "HTTP 503" in notifications[0][0]
    assert len(activities(db, ActivityLevel.WARNING)) == 3


def test_rate_limit_is_not_a_failed_attempt(db, setup, notifications):
    """api-5: a rate limit or quota error moves the post later (jittered, after retry_after) and
    leaves attempts and the retry ladder alone."""
    post, *_ = setup()
    now = utcnow()
    limited = [RateLimitedError("quota", retry_after=3600) for _ in range(6)]
    with override("publisher", "youtube", fake(publish_script=limited)):
        for _ in range(6):  # more often than the retry ladder allows
            step = publisher.publish_post(db, post.id, now=now)
            assert step.status == PostStatus.SCHEDULED
            assert post.attempts == 0 and post.status == PostStatus.SCHEDULED
            wait = post.scheduled_at - now
            assert (
                timedelta(hours=1)
                <= wait
                < timedelta(hours=1) + timedelta(seconds=publisher.RATE_LIMIT_JITTER_SECONDS)
            )
            now = post.scheduled_at
    assert notifications == [] and post.error.startswith("Rate limited")
    (job,) = [j for j in jobs(db, "publish_post") if j.status == JobStatus.QUEUED]
    assert job.run_at == post.scheduled_at


def test_rate_limited_posts_are_spread_with_jitter_and_keep_the_min_gap(db, setup, workspace, make_account):
    """Posts that hit the same quota error do not all return at 00:05: each gets its own
    deterministic jitter, never closer than the rule's min gap to the account's other posts."""
    first, video, acc, rule = setup()
    rule.min_gap_minutes = 240
    second = ScheduledPost(
        workspace_id=workspace.id,
        video_id=video.id,
        connected_account_id=acc.id,
        platform=Platform.YOUTUBE,
        cycle=1,
        idempotency_key=f"{video.id}:{acc.id}:1",
        scheduled_at=utcnow(),
        created_by="scheduler",
    )
    db.add(second)
    db.commit()
    now = utcnow()
    quota = [RateLimitedError("quotaExceeded", retry_after=6 * 3600) for _ in range(2)]
    with override("publisher", "youtube", fake(publish_script=quota)):
        publisher.publish_post(db, first.id, now=now)
        publisher.publish_post(db, second.id, now=now)
    gap = abs(first.scheduled_at - second.scheduled_at)
    assert gap >= timedelta(minutes=240)
    assert min(first.scheduled_at, second.scheduled_at) >= now + timedelta(hours=6)
    # The jitter is a function of the post, so it is repeatable.
    assert publisher._jitter_seconds(first.id, 1800) == publisher._jitter_seconds(first.id, 1800)


def test_rate_limit_gives_up_after_days(db, setup, notifications):
    post, *_ = setup()
    long_ago = utcnow() - publisher.RATE_LIMIT_GIVE_UP - timedelta(hours=1)
    post.publish_state = {"_engine": {"rate_limited_since": long_ago.isoformat()}}
    db.commit()
    with override("publisher", "youtube", fake(publish_script=[RateLimitedError("quota")])):
        step = publisher.publish_post(db, post.id)
    assert step.status == PostStatus.NEEDS_ATTENTION and len(notifications) == 1


def test_running_publish_job_gets_retry_in(db, setup):
    post, *_ = setup()
    db.add(
        Job(
            kind="publish_post",
            payload={"post_id": post.id},
            status=JobStatus.RUNNING,
            dedupe_key=f"publish_post:{post.id}",
        )
    )
    db.commit()
    with override("publisher", "youtube", fake(publish_script=[RetryableError("timeout")])):
        step = publisher.publish_post(db, post.id)
    assert step.retry_in == 300 and step.status == PostStatus.FAILED
    assert [j.status for j in jobs(db, "publish_post")] == [JobStatus.RUNNING]


def test_unexpected_exception_after_the_upload_started_is_not_retried(db, setup, notifications, caplog):
    """engine-3: bytes may have gone out, so a bug in the adapter ends in needs-attention
    ("may be live"), never in a blind retry."""
    post, *_ = setup()
    boom = ValueError("leaked https://api.example/x?access_token=SECRET123")
    pub = fake(publish_script=[boom])
    with override("publisher", "youtube", pub):
        step = publisher.publish_post(db, post.id)
    assert step.status == PostStatus.NEEDS_ATTENTION and post.attempts == 0
    assert "Unexpected error (ValueError)" in post.error and "SECRET123" not in post.error
    assert "may be live" in post.error and publisher.engine_state(post.publish_state)["may_be_live"]
    assert len(notifications) == 1 and "SECRET123" not in notifications[0][0]
    assert not [j for j in jobs(db, "publish_post") if j.status == JobStatus.QUEUED]
    for a in activities(db):
        assert "SECRET123" not in a.message and "Traceback" not in a.message
    assert "SECRET123" not in caplog.text


def test_retryable_error_is_still_retried_and_urls_are_scrubbed(db, setup):
    post, *_ = setup()
    exc = RetryableError("PUT https://upload.example/session?upload_id=SECRET failed (ReadTimeout)")
    with override("publisher", "youtube", fake(publish_script=[exc])):
        step = publisher.publish_post(db, post.id)
    assert step.status == PostStatus.FAILED and post.attempts == 1
    assert "SECRET" not in post.error and "<url>" in post.error
    assert all("SECRET" not in a.message for a in activities(db))


@pytest.mark.parametrize(
    "exc", [PermanentError("Video rejected by policy"), NotSupportedError("Not available")]
)
def test_permanent_errors_need_attention(db, setup, notifications, exc):
    post, *_ = setup()
    with override("publisher", "youtube", fake(publish_script=[exc])):
        step = publisher.publish_post(db, post.id)
    assert step.status == PostStatus.NEEDS_ATTENTION and post.error == exc.message
    assert post.attempts == 0
    err = activities(db, ActivityLevel.ERROR)[-1]
    assert exc.message in err.message and err.data["post_id"] == post.id
    assert len(notifications) == 1


def test_auth_error_marks_account_needs_reconnect(db, setup, notifications):
    post, _video, acc, _rule = setup()
    with override("publisher", "youtube", fake(publish_script=[AuthError("Token has been revoked")])):
        step = publisher.publish_post(db, post.id)
    assert step.status == PostStatus.NEEDS_ATTENTION
    db.refresh(acc)
    assert acc.status == AccountStatus.NEEDS_RECONNECT and acc.last_error == "Token has been revoked"
    assert "Reconnect the YouTube account" in post.error
    assert len(notifications) == 1


def test_validation_problems_need_attention(db, setup, notifications):
    post, *_ = setup("tiktok")
    pub = fake("tiktok", problems=["Choose who can view the TikTok posts.", " Accept the music terms. "])
    with override("publisher", "tiktok", pub):
        step = publisher.publish_post(db, post.id)
    assert step.status == PostStatus.NEEDS_ATTENTION
    assert post.error == "Choose who can view the TikTok posts. Accept the music terms."
    assert pub.publish_script == [] and len(notifications) == 1


def test_account_unavailable_needs_attention(db, setup, notifications):
    post, _video, acc, _rule = setup()
    acc.token_expires_at = utcnow() + timedelta(minutes=1)
    from repeater.accounts import save_tokens

    save_tokens(
        acc, OAuthTokens(access_token="old", refresh_token="r", expires_at=utcnow() + timedelta(minutes=1))
    )
    db.commit()

    class RevokedOAuth:
        name, uses_pkce = "google", False

        def refresh(self, ctx):
            raise AuthError("invalid_grant")

    with override("oauth", "google", RevokedOAuth()), override("publisher", "youtube", fake()):
        step = publisher.publish_post(db, post.id)
    assert step.status == PostStatus.NEEDS_ATTENTION and acc.status == AccountStatus.NEEDS_RECONNECT
    assert len(notifications) == 1


def test_token_refresh_is_committed_before_upload(db, setup):
    post, _video, acc, _rule = setup()
    from repeater.accounts import save_tokens

    save_tokens(
        acc, OAuthTokens(access_token="old", refresh_token="r1", expires_at=utcnow() + timedelta(minutes=1))
    )
    db.commit()

    class RotatingOAuth:
        name, uses_pkce = "google", False

        def refresh(self, ctx):
            return OAuthTokens(
                access_token="new", refresh_token="r2", expires_at=utcnow() + timedelta(hours=1)
            )

    with (
        override("oauth", "google", RotatingOAuth()),
        override("publisher", "youtube", fake(publish_script=[RetryableError("net")])),
    ):
        publisher.publish_post(db, post.id)
    with get_sessionmaker()() as other:
        assert load_tokens(other.get(type(acc), acc.id)).refresh_token == "r2"


# =========================================================================== re-checks


def test_rechecks_cancel_or_defer(db, setup, workspace):
    post, video, acc, rule = setup()
    pub = fake(publish_script=[PUBLISHED])
    with override("publisher", "youtube", pub):
        # Not due yet: the job is (re)queued for scheduled_at.
        later = utcnow() + timedelta(hours=3)
        post.scheduled_at = later
        db.commit()
        assert publisher.publish_post(db, post.id).status == PostStatus.SCHEDULED
        assert jobs(db, "publish_post")[0].run_at == later
        post.scheduled_at = utcnow()
        # Video still processing: deferred 15 minutes.
        video.status = VideoStatus.PROCESSING
        db.commit()
        now = utcnow()
        step = publisher.publish_post(db, post.id, now=now)
        assert step.status == PostStatus.SCHEDULED and post.scheduled_at == now + timedelta(minutes=15)
        video.status = VideoStatus.READY
        post.scheduled_at = utcnow()
        # Rule disabled: scheduler posts are canceled ...
        rule.enabled = False
        db.commit()
        assert publisher.publish_post(db, post.id).status == PostStatus.CANCELED
        assert "Rotation is turned off" in post.error
        # ... but a post the user asked for still goes out.
        post.status, post.created_by = PostStatus.SCHEDULED, "user"
        db.commit()
        assert publisher.publish_post(db, post.id).status == PostStatus.PUBLISHED
        # Already published: nothing happens.
        assert publisher.publish_post(db, post.id).status == PostStatus.PUBLISHED
    assert len(pub.requests) == 1


@pytest.mark.parametrize(
    ("change", "expected", "text"),
    [
        ("paused", PostStatus.CANCELED, "paused"),
        ("disabled", PostStatus.CANCELED, "disconnected"),
        ("reconnect", PostStatus.CANCELED, "reconnected"),
        ("archived", PostStatus.CANCELED, "archived"),
        ("missing_file", PostStatus.NEEDS_ATTENTION, "missing"),
        ("stale", PostStatus.CANCELED, "missed its time slot"),
    ],
)
def test_rechecks_before_publishing(db, setup, workspace, notifications, change, expected, text):
    post, video, acc, _rule = setup()
    if change == "paused":
        workspace.paused = True
    elif change == "disabled":
        acc.status = AccountStatus.DISABLED
    elif change == "reconnect":
        acc.status = AccountStatus.NEEDS_RECONNECT
    elif change == "archived":
        video.status = VideoStatus.ARCHIVED
    elif change == "missing_file":
        storage.abs_path(video.encoded_path).unlink()
    elif change == "stale":
        post.scheduled_at = utcnow() - timedelta(hours=4)
    db.commit()
    pub = fake(publish_script=[PUBLISHED])
    with override("publisher", "youtube", pub):
        step = publisher.publish_post(db, post.id)
    assert step.status == expected and text in post.error
    assert pub.requests == []


def test_missing_post_is_ignored(db):
    assert publisher.publish_post(db, 12345).status is None
    assert publisher.poll_post(db, 12345).status is None


def test_unexpected_outcome_needs_attention(db, setup, notifications):
    post, *_ = setup()
    with override("publisher", "youtube", fake(publish_script=[PublishResult("weird")])):
        step = publisher.publish_post(db, post.id)
    assert step.status == PostStatus.NEEDS_ATTENTION and "unexpected answer" in post.error


def test_job_handler_raises_retry_later(db, setup):
    from repeater.engine import jobs as engine_jobs

    post, *_ = setup()
    db.add(
        Job(
            kind="publish_post",
            payload={"post_id": post.id},
            status=JobStatus.RUNNING,
            dedupe_key=f"publish_post:{post.id}",
        )
    )
    db.commit()
    with (
        override("publisher", "youtube", fake(publish_script=[RetryableError("x")])),
        pytest.raises(RetryLater) as info,
    ):
        engine_jobs.handle_publish_post(db, {"post_id": post.id})
    assert info.value.seconds == 300


# =========================================================================== actions


def test_skip_post(db, setup):
    post, *_ = setup()
    actions.skip_post(db, post, actor="admin")
    db.commit()
    assert post.status == PostStatus.SKIPPED
    assert activities(db)[-1].actor == "admin"
    with pytest.raises(ValueError):
        actions.skip_post(db, post, actor="admin")
    post.status = PostStatus.UPLOADING
    db.commit()
    with pytest.raises(ValueError, match="uploading"):
        actions.skip_post(db, post, actor="admin")


def test_publish_now_moves_job_and_resets_attempts(db, setup):
    post, *_ = setup(scheduled_at=utcnow() + timedelta(hours=5))
    kinds.enqueue_publish_post(db, post.id, run_at=post.scheduled_at)
    post.status, post.attempts, post.error = PostStatus.NEEDS_ATTENTION, 4, "gave up"
    db.commit()
    before = utcnow()
    actions.publish_now(db, post, actor="admin")
    db.commit()
    assert post.status == PostStatus.SCHEDULED and post.attempts == 0 and post.error is None
    (job,) = jobs(db, "publish_post")
    assert before <= job.run_at <= utcnow() and job.run_at == post.scheduled_at
    post.status = PostStatus.PUBLISHED
    db.commit()
    with pytest.raises(ValueError):
        actions.publish_now(db, post, actor="admin")


def test_publish_now_refuses_when_paused_or_disconnected(db, setup, workspace):
    post, _video, acc, _rule = setup()
    workspace.paused = True
    db.commit()
    with pytest.raises(ValueError, match="paused"):
        actions.publish_now(db, post, actor="admin")
    workspace.paused = False
    acc.status = AccountStatus.NEEDS_RECONNECT
    db.commit()
    with pytest.raises(ValueError, match="reconnected"):
        actions.publish_now(db, post, actor="admin")


def test_schedule_video_now_creates_reuses_and_cycles(db, setup, make_account):
    post, video, acc, _rule = setup()
    # The existing scheduled post of the current cycle is reused and moved to now.
    reused = actions.schedule_video_now(db, video, acc, actor="admin")
    db.commit()
    assert reused.id == post.id and reused.created_by == "user"
    post.status = PostStatus.PUBLISHED
    db.commit()
    # After publishing, "publish now" means a new cycle.
    nxt = actions.schedule_video_now(db, video, acc, actor="admin")
    db.commit()
    assert nxt.id != post.id and nxt.cycle == 1 and nxt.idempotency_key == f"{video.id}:{acc.id}:1"
    nxt.status = PostStatus.CANCELED
    db.commit()
    assert actions.schedule_video_now(db, video, acc, actor="admin").id == nxt.id
    db.commit()
    nxt.status = PostStatus.PROCESSING
    db.commit()
    with pytest.raises(ValueError, match="right now"):
        actions.schedule_video_now(db, video, acc, actor="admin")
    # A fresh account starts at cycle 0 and gets a publish job now.
    ig = make_account("instagram")
    new = actions.schedule_video_now(db, video, ig, actor="admin")
    db.commit()
    assert new.cycle == 0 and new.status == PostStatus.SCHEDULED
    assert any(j.payload == {"post_id": new.id} for j in jobs(db, "publish_post"))


def test_schedule_video_now_refusals(db, setup, make_account):
    _post, video, acc, _rule = setup()
    drive = make_account("gdrive")
    with pytest.raises(ValueError, match="cannot be published to"):
        actions.schedule_video_now(db, video, drive, actor="admin")
    video.source_account_id = acc.id
    db.commit()
    with pytest.raises(ValueError, match="came from this account"):
        actions.schedule_video_now(db, video, acc, actor="admin")
    video.source_account_id = None
    video.duration_s = 120.0
    fb = make_account("facebook")
    db.commit()
    with pytest.raises(ValueError, match="Facebook cannot take this video"):
        actions.schedule_video_now(db, video, fb, actor="admin")
    video.status = VideoStatus.ARCHIVED
    db.commit()
    with pytest.raises(ValueError, match="ready"):
        actions.schedule_video_now(db, video, acc, actor="admin")


def test_archive_and_restore_video(db, setup, make_account):
    post, video, acc, _rule = setup()
    ig = make_account("instagram")
    done = ScheduledPost(
        workspace_id=video.workspace_id,
        video_id=video.id,
        connected_account_id=ig.id,
        platform=Platform.INSTAGRAM,
        idempotency_key=f"{video.id}:{ig.id}:0",
        scheduled_at=utcnow(),
        status=PostStatus.PUBLISHED,
    )
    db.add(done)
    db.commit()
    actions.archive_video(db, video, actor="admin")
    db.commit()
    db.refresh(post), db.refresh(done)
    assert video.status == VideoStatus.ARCHIVED
    assert post.status == PostStatus.CANCELED and done.status == PostStatus.PUBLISHED
    actions.archive_video(db, video, actor="admin")  # idempotent
    actions.restore_video(db, video, actor="admin")
    db.commit()
    assert video.status == VideoStatus.READY
    assert [j.payload for j in jobs(db, "plan_workspace")] == [{"workspace_id": video.workspace_id}]
    with pytest.raises(ValueError, match="Only archived"):
        actions.restore_video(db, video, actor="admin")


def test_restore_video_without_files_or_duplicate(db, setup):
    _post, video, *_ = setup()
    actions.archive_video(db, video, actor="admin")
    storage.abs_path(video.encoded_path).unlink()
    db.commit()
    with pytest.raises(ValueError, match="no longer on the server"):
        actions.restore_video(db, video, actor="admin")
    original = storage.ensure_video_dir(video.workspace_id, video.id) / "original.mp4"
    original.write_bytes(b"x")
    video.original_path = storage.rel_path(original)
    db.commit()
    actions.restore_video(db, video, actor="admin")
    assert video.status == VideoStatus.PROCESSING
    assert [j.payload for j in jobs(db, "process_video")] == [{"video_id": video.id}]
    other = Video(workspace_id=video.workspace_id, status=VideoStatus.ARCHIVED, duplicate_of_id=video.id)
    db.add(other)
    db.commit()
    with pytest.raises(ValueError, match="duplicate"):
        actions.restore_video(db, other, actor="admin")


def test_request_build_failure_is_retried(db, setup, monkeypatch):
    post, *_ = setup()

    def broken(*args, **kwargs):
        raise UnicodeError("bad caption")

    monkeypatch.setattr(publisher.captions, "render", broken)
    with override("publisher", "youtube", fake(publish_script=[PUBLISHED])):
        step = publisher.publish_post(db, post.id)
    assert step.status == PostStatus.FAILED and "UnicodeError" in post.error


def test_user_post_without_rule_uses_default_settings(db, setup):
    post, _video, _acc, rule = setup(created_by="user")
    db.delete(rule)
    db.commit()
    pub = fake(publish_script=[PUBLISHED])
    with override("publisher", "youtube", pub):
        assert publisher.publish_post(db, post.id).status == PostStatus.PUBLISHED
    assert pub.requests[0].settings == rules.default_settings("youtube")
    assert pub.requests[0].caption == "Watch this coin vanish\n\nFollow for more"


# =========================================================================== unknown outcome (engine-3, C1)


def test_unknown_outcome_with_state_polls_instead_of_uploading_again(db, setup, notifications):
    """The final-chunk response was lost: the adapter's state is saved and poll() asks the
    platform; the video is never uploaded a second time."""
    post, *_ = setup("tiktok")
    lost = UnknownOutcomeError(
        "No answer to the last chunk (ReadTimeout).", state={"publish_id": "p1", "upload_url": "SECRET"}
    )
    live = PublishResult(PublishOutcome.PUBLISHED, platform_post_id="tt-1", state={"publish_id": "p1"})
    pub = fake("tiktok", publish_script=[lost], poll_script=[live])
    now = utcnow()
    with override("publisher", "tiktok", pub):
        step = publisher.publish_post(db, post.id, now=now)
        assert step.status == PostStatus.PROCESSING and step.retry_in is None
        db.refresh(post)
        assert post.status == PostStatus.PROCESSING and post.attempts == 0
        assert post.publish_state["publish_id"] == "p1"
        assert publisher.engine_state(post.publish_state)["processing_since"] == now.isoformat()
        (job,) = jobs(db, "poll_post")
        assert job.run_at == now + timedelta(seconds=publisher.UNKNOWN_OUTCOME_POLL_SECONDS)
        assert not [j for j in jobs(db, "publish_post") if j.status == JobStatus.QUEUED]
        step = publisher.poll_post(db, post.id, now=job.run_at)
    assert step.status == PostStatus.PUBLISHED and pub.publish_script == []
    assert pub.poll_states == [{"publish_id": "p1", "upload_url": "SECRET"}]
    assert notifications == []
    # The state (it may hold an upload URL) is not logged in the activity entries.
    assert all("SECRET" not in a.message and "SECRET" not in str(a.data) for a in activities(db))


def test_unknown_outcome_without_state_is_needs_attention_not_a_retry(db, setup, notifications):
    post, *_ = setup("tiktok")
    pub = fake("tiktok", publish_script=[UnknownOutcomeError("Timeout after the last byte.")])
    with override("publisher", "tiktok", pub):
        step = publisher.publish_post(db, post.id)
    assert step.status == PostStatus.NEEDS_ATTENTION and post.attempts == 0
    assert "may be live" in post.error
    assert publisher.engine_state(post.publish_state)["may_be_live"] is True
    assert len(notifications) == 1 and notifications[0][1]["level"] == "error"
    assert not [j for j in jobs(db, "publish_post") if j.status == JobStatus.QUEUED]


def test_unknown_outcome_resolved_to_nothing_publishes_again_with_empty_state(db, setup):
    post, *_ = setup("instagram")
    lost = UnknownOutcomeError("lost", state={"container_id": "c1"})
    again = PublishResult(PublishOutcome.PROCESSING, state={"container_id": "c2"})
    pub = fake(
        "instagram",
        publish_script=[lost, again],
        poll_script=[RestartPublish("The upload is gone.", retry_after=60)],
    )
    now = utcnow()
    with override("publisher", "instagram", pub):
        publisher.publish_post(db, post.id, now=now)
        step = publisher.poll_post(db, post.id, now=now + timedelta(minutes=1))
        assert step.status == PostStatus.FAILED and post.attempts == 1 and post.publish_state == {}
        publisher.publish_post(db, post.id, now=post.scheduled_at)
    assert [r.state for r in pub.requests] == [{}, {}]
    assert post.publish_state["container_id"] == "c2"


def test_publish_request_carries_the_posts_adapter_state(db, setup):
    post, *_ = setup("youtube", created_by="user")
    post.publish_state = {"upload_uri": "resume-here", "_engine": {"may_be_live": True}}
    db.commit()
    pub = fake(publish_script=[PUBLISHED])
    with override("publisher", "youtube", pub):
        publisher.publish_post(db, post.id)
    # The adapter sees its own state, not the engine's bookkeeping, and cannot change the row.
    assert pub.requests[0].state == {"upload_uri": "resume-here"}


# =========================================================================== disconnect (engine-1)


def _youtube_ready(db, rule):
    rule.settings = {**rule.settings, "self_declared_made_for_kids": False}
    rule.window_start = rule.window_end = __import__("datetime").time(0, 0)
    rule.min_gap_minutes = 0
    db.commit()


@pytest.mark.parametrize("state", [PostStatus.PROCESSING, PostStatus.UPLOADING])
def test_disconnect_while_publishing_never_makes_a_reusable_canceled_post(
    db, setup, workspace, notifications, state
):
    """The same video must not be posted again after a disconnect and reconnect."""
    from repeater.accounts import disconnect, upsert_connected_accounts
    from repeater.engine import scheduler
    from repeater.platforms.base import ConnectedAccountData

    workspace.timezone = "UTC"
    workspace.cross_platform_gap_minutes = 0
    post, video, acc, rule = setup()
    _youtube_ready(db, rule)
    processing = PublishResult(
        PublishOutcome.PROCESSING, platform_post_id="yt-1", state={"video_id": "yt-1"}, poll_after_seconds=60
    )
    pub = fake(publish_script=[processing, PUBLISHED])
    with override("publisher", "youtube", pub):
        publisher.publish_post(db, post.id)
        if state == PostStatus.UPLOADING:  # the disconnect lands before the adapter answered
            post.status = PostStatus.UPLOADING
            post.platform_post_id, post.publish_state = None, {}
            db.commit()
        disconnect(db, db.get(type(acc), acc.id), revoke=False)
        db.commit()
        db.refresh(post)
        assert post.status == PostStatus.NEEDS_ATTENTION and "may be live" in post.error
        assert publisher.engine_state(post.publish_state)["may_be_live"] is True
        if state == PostStatus.PROCESSING:
            assert post.platform_post_id == "yt-1" and post.publish_state["video_id"] == "yt-1"
        # Reconnect the same channel and turn rotation back on: nothing is planned for the pair.
        upsert_connected_accounts(
            db,
            workspace.id,
            [
                ConnectedAccountData(
                    platform=Platform.YOUTUBE,
                    external_id=acc.external_id,
                    display_name="Magic",
                    tokens=OAuthTokens(
                        access_token="a2", refresh_token="r2", expires_at=utcnow() + timedelta(hours=1)
                    ),
                )
            ],
        )
        rules.find_rule(db, acc).enabled = True
        db.commit()
        assert scheduler.plan_workspace(db, workspace.id) == []
        db.commit()
        db.refresh(post)
        assert post.status == PostStatus.NEEDS_ATTENTION
    assert len(pub.requests) == 1  # uploaded once


def test_disconnect_cancels_unstarted_posts_and_marks_the_account(db, setup, workspace):
    from repeater.accounts import disconnect

    scheduled, _video, acc, _rule = setup()
    disconnect(db, db.get(type(acc), acc.id), revoke=False)
    db.commit()
    db.refresh(scheduled), db.refresh(acc)
    assert scheduled.status == PostStatus.CANCELED and scheduled.error == "account disconnected"
    assert acc.status == AccountStatus.DISABLED and acc.token_enc is None
    assert acc.extra["_disconnected_at"]


def test_a_legacy_canceled_post_that_reached_the_platform_is_not_reused(db, setup):
    """Posts canceled before the fix (platform id kept) start the next cycle instead of being wiped."""
    post, video, acc, _rule = setup()
    post.status, post.platform_post_id = PostStatus.CANCELED, "yt-live"
    post.publish_state = {"video_id": "yt-live"}
    db.commit()
    nxt = actions.schedule_video_now(db, video, acc, actor="admin")
    db.commit()
    assert nxt.id != post.id and nxt.cycle == 1
    db.refresh(post)
    assert post.platform_post_id == "yt-live" and post.publish_state == {"video_id": "yt-live"}


def test_a_needs_attention_post_that_may_be_live_keeps_its_row_on_publish_to_account(db, setup):
    post, video, acc, _rule = setup()
    post.status, post.platform_post_id = PostStatus.NEEDS_ATTENTION, "yt-live"
    db.commit()
    nxt = actions.schedule_video_now(db, video, acc, actor="admin")
    assert nxt.id != post.id and nxt.cycle == 1 and post.platform_post_id == "yt-live"


# =========================================================================== lost jobs (engine-11)


def test_publish_now_while_the_job_is_still_running_queues_a_follow_up(db, setup):
    """The job that ended the post is still sending its notification (RUNNING) when the user
    presses 'Publish now': a second job is queued, so the click is not lost."""
    post, *_ = setup()
    post.status = PostStatus.NEEDS_ATTENTION
    db.add(
        Job(
            kind=kinds.PUBLISH_POST,
            payload={"post_id": post.id},
            status=JobStatus.RUNNING,
            dedupe_key=f"publish_post:{post.id}",
            locked_at=utcnow(),
            locked_by="w:0",
            attempts=1,
            max_attempts=1,
        )
    )
    db.commit()
    actions.publish_now(db, post, actor="user")
    db.commit()
    assert post.status == PostStatus.SCHEDULED
    queued = [j for j in jobs(db, "publish_post") if j.status == JobStatus.QUEUED]
    assert len(queued) == 1 and queued[0].payload == {"post_id": post.id}
    assert queued[0].dedupe_key == f"publish_post:{post.id}:next"
    # Pressing it again moves that job instead of adding a third.
    actions.publish_now(db, post, actor="user")
    db.commit()
    assert len([j for j in jobs(db, "publish_post") if j.status == JobStatus.QUEUED]) == 1
    # The follow-up publishes once, whoever runs it.
    with override("publisher", "youtube", fake(publish_script=[PUBLISHED])):
        assert publisher.publish_post(db, post.id).status == PostStatus.PUBLISHED
        assert publisher.publish_post(db, post.id).status == PostStatus.PUBLISHED  # no-op, no second upload


# =========================================================================== C2 and duplicates


def test_invalidate_plan_cancels_unstarted_planner_posts_and_asks_for_a_plan(
    db, setup, workspace, make_account
):
    scheduled, video, acc, _rule = setup()
    other_acc = make_account("facebook")
    user_post = ScheduledPost(
        workspace_id=workspace.id,
        video_id=video.id,
        connected_account_id=other_acc.id,
        platform=Platform.FACEBOOK,
        idempotency_key=f"{video.id}:{other_acc.id}:0",
        scheduled_at=utcnow() + timedelta(hours=1),
        created_by="user",
    )
    other_planned = ScheduledPost(
        workspace_id=workspace.id,
        video_id=video.id,
        connected_account_id=other_acc.id,
        platform=Platform.FACEBOOK,
        cycle=1,
        idempotency_key=f"{video.id}:{other_acc.id}:1",
        scheduled_at=utcnow() + timedelta(hours=1),
        created_by="scheduler",
    )
    uploading = ScheduledPost(
        workspace_id=workspace.id,
        video_id=video.id,
        connected_account_id=acc.id,
        platform=Platform.YOUTUBE,
        cycle=1,
        idempotency_key=f"{video.id}:{acc.id}:1",
        scheduled_at=utcnow(),
        status=PostStatus.UPLOADING,
        created_by="scheduler",
    )
    failed = ScheduledPost(
        workspace_id=workspace.id,
        video_id=video.id,
        connected_account_id=acc.id,
        platform=Platform.YOUTUBE,
        cycle=2,
        idempotency_key=f"{video.id}:{acc.id}:2",
        scheduled_at=utcnow(),
        status=PostStatus.FAILED,
        created_by="scheduler",
    )
    db.add_all([user_post, other_planned, uploading, failed])
    db.commit()
    count = actions.invalidate_plan(db, workspace.id, account_id=acc.id, actor="admin")
    db.commit()
    assert count == 1
    db.refresh(scheduled), db.refresh(other_planned)
    assert scheduled.status == PostStatus.CANCELED and "re-planned" in scheduled.error
    assert other_planned.status == PostStatus.SCHEDULED  # another account
    assert user_post.status == PostStatus.SCHEDULED  # the user's own post
    assert (uploading.status, failed.status) == (PostStatus.UPLOADING, PostStatus.FAILED)
    (plan_job,) = jobs(db, "plan_workspace")
    assert plan_job.payload == {"workspace_id": workspace.id} and plan_job.run_at <= utcnow()
    assert any(a.actor == "admin" and "re-planned" in a.message for a in activities(db))
    # Whole workspace; the plan job that is queued for later is pulled forward.
    plan_job.run_at = utcnow() + timedelta(minutes=4)
    db.commit()
    assert actions.invalidate_plan(db, workspace.id) == 1  # only other_planned is left
    db.commit()
    db.refresh(plan_job), db.refresh(other_planned)
    assert other_planned.status == PostStatus.CANCELED and plan_job.run_at <= utcnow()
    assert actions.invalidate_plan(db, workspace.id) == 0  # nothing to cancel still asks for a plan


def test_invalidate_plan_lets_the_planner_apply_the_new_rule(db, setup, workspace):
    """engine-6: after a rule edit the posts outside the new window are replaced."""
    from datetime import time as dtime

    from repeater.engine import scheduler

    workspace.timezone = "UTC"
    workspace.cross_platform_gap_minutes = 0
    post, video, acc, rule = setup()
    post.status = PostStatus.CANCELED
    db.commit()
    _youtube_ready(db, rule)
    rule.daily_cap = 5
    db.commit()
    now = utcnow().replace(hour=1, minute=0, second=0, microsecond=0)
    first = scheduler.plan_workspace(db, workspace.id, now=now)
    db.commit()
    assert first and all(p.status == PostStatus.SCHEDULED for p in first)
    rule.window_start, rule.window_end, rule.daily_cap = dtime(19, 0), dtime(22, 0), 1
    db.commit()
    assert scheduler.plan_workspace(db, workspace.id, now=now + timedelta(minutes=5)) == []  # old posts stay
    assert actions.invalidate_plan(db, workspace.id, account_id=acc.id) == len(first)
    db.commit()
    replanned = scheduler.plan_workspace(db, workspace.id, now=now + timedelta(minutes=10))
    db.commit()
    assert replanned and all(19 <= p.scheduled_at.hour < 22 for p in replanned)
    assert {p.id for p in replanned} <= {p.id for p in first}  # the canceled rows were re-used


def test_cancel_conflicting_posts_after_a_duplicate_is_found(db, setup, workspace, make_account):
    """import-media-missed-1: the copy found on TikTok means TikTok already has the clip."""
    _post, video, yt, _rule = setup()
    tt = make_account("tiktok")
    ig = make_account("instagram")
    posts = {}
    for acc in (tt, ig):
        posts[acc.platform] = ScheduledPost(
            workspace_id=workspace.id,
            video_id=video.id,
            connected_account_id=acc.id,
            platform=acc.platform,
            idempotency_key=f"{video.id}:{acc.id}:0",
            scheduled_at=utcnow() + timedelta(hours=1),
            status=PostStatus.SCHEDULED if acc is tt else PostStatus.FAILED,
        )
        db.add(posts[acc.platform])
    dup = Video(
        workspace_id=workspace.id,
        status=VideoStatus.DUPLICATE,
        duplicate_of_id=video.id,
        source_platform=Platform.TIKTOK,
        source_account_id=tt.id,
    )
    published = ScheduledPost(
        workspace_id=workspace.id,
        video_id=video.id,
        connected_account_id=yt.id,
        platform=Platform.YOUTUBE,
        cycle=3,
        idempotency_key=f"{video.id}:{yt.id}:3",
        scheduled_at=utcnow(),
        status=PostStatus.PUBLISHED,
    )
    db.add_all([dup, published])
    db.commit()
    assert actions.cancel_conflicting_posts(db, video.id, actor="importer") == 1
    db.commit()
    db.refresh(posts[Platform.TIKTOK]), db.refresh(posts[Platform.INSTAGRAM])
    assert posts[Platform.TIKTOK].status == PostStatus.CANCELED
    assert "already exists on TikTok" in posts[Platform.TIKTOK].error
    assert posts[Platform.INSTAGRAM].status == PostStatus.FAILED  # a platform the duplicate is not on
    assert published.status == PostStatus.PUBLISHED
    assert actions.cancel_conflicting_posts(db, video.id) == 0
    assert actions.cancel_conflicting_posts(db, 424242) == 0
    assert any(a.actor == "importer" and "TikTok" in a.message for a in activities(db))
    # A video without duplicates has nothing to cancel.
    clean = Video(workspace_id=workspace.id, status=VideoStatus.READY)
    db.add(clean)
    db.commit()
    assert actions.cancel_conflicting_posts(db, clean.id) == 0


def test_publish_checks_duplicate_origins_as_a_backstop(db, setup, workspace, notifications):
    """A post queued before the duplicate was found is canceled when its job runs."""
    post, video, acc, _rule = setup("tiktok")
    dup = Video(
        workspace_id=workspace.id,
        status=VideoStatus.DUPLICATE,
        duplicate_of_id=video.id,
        source_platform=Platform.TIKTOK,
    )
    db.add(dup)
    db.commit()
    pub = fake("tiktok", publish_script=[PUBLISHED])
    with override("publisher", "tiktok", pub):
        step = publisher.publish_post(db, post.id)
    assert step.status == PostStatus.CANCELED and "already exists on TikTok" in post.error
    assert pub.requests == [] and notifications == []


def test_may_be_live_tells_the_dashboard_to_ask_for_a_check(db, setup):
    post, *_ = setup()
    assert actions.may_be_live(post) is False
    post.status = PostStatus.NEEDS_ATTENTION
    assert actions.may_be_live(post) is False  # e.g. a validation failure: nothing reached the platform
    post.publish_state = {"_engine": {"may_be_live": True}}
    assert actions.may_be_live(post) is True
    post.publish_state, post.platform_post_id = {}, "yt-1"
    assert actions.may_be_live(post) is True
    post.status = PostStatus.PUBLISHED
    assert actions.may_be_live(post) is False


def test_disconnect_during_a_token_refresh_waits_instead_of_deadlocking(db, setup):
    """Locks are taken post first, account second (publish_post/fresh_context and disconnect
    alike): a disconnect that arrives while a publish refreshes the token waits for it."""
    import threading

    from repeater.accounts import disconnect, save_tokens

    post, _video, acc, _rule = setup()
    save_tokens(
        acc,
        OAuthTokens(access_token="old", refresh_token="r", expires_at=utcnow() + timedelta(minutes=1)),
    )
    db.commit()
    entered, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    class SlowGoogle:
        name, uses_pkce = "google", False

        def refresh(self, ctx):
            entered.set()
            assert release.wait(10)
            return OAuthTokens(
                access_token="new", refresh_token="r", expires_at=utcnow() + timedelta(hours=1)
            )

    processing = PublishResult(PublishOutcome.PROCESSING, platform_post_id="yt-1", state={"video_id": "yt-1"})

    def run_publish():
        try:
            with get_sessionmaker()() as session:
                publisher.publish_post(session, post.id)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    def run_disconnect():
        try:
            with get_sessionmaker()() as session:
                disconnect(session, session.get(type(acc), acc.id), revoke=False)
                session.commit()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    with (
        override("oauth", "google", SlowGoogle()),
        override("publisher", "youtube", fake(publish_script=[processing])),
    ):
        t1 = threading.Thread(target=run_publish)
        t1.start()
        assert entered.wait(10)
        t2 = threading.Thread(target=run_disconnect)
        t2.start()
        t2.join(0.5)
        assert t2.is_alive()  # waiting for the post lock, not holding the account
        release.set()
        t1.join(15)
        t2.join(15)
    assert not t1.is_alive() and not t2.is_alive() and errors == []
    db.expire_all()
    assert db.get(type(acc), acc.id).status == AccountStatus.DISABLED
    assert db.get(type(acc), acc.id).token_enc is None  # the refresh did not resurrect a token
    stored = db.get(ScheduledPost, post.id)
    assert stored.platform_post_id == "yt-1" and stored.status in (
        PostStatus.PROCESSING,
        PostStatus.NEEDS_ATTENTION,
    )
