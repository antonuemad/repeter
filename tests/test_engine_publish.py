"""Publishing pipeline, captions and dashboard actions (repeater.engine.publisher, captions,
actions)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import pytest
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
    RetryableError,
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
    post, *_ = setup("instagram")
    post.status = PostStatus.PROCESSING
    started = utcnow() - timedelta(hours=2, minutes=1)
    post.publish_state = {"container_id": "c1", "_engine": {"processing_since": started.isoformat()}}
    db.commit()
    pub = fake("instagram")
    with override("publisher", "instagram", pub):
        step = publisher.poll_post(db, post.id)
    assert step.status == PostStatus.NEEDS_ATTENTION and pub.poll_states == []
    assert "within 2 hours" in post.error
    assert len(notifications) == 1 and notifications[0][1]["level"] == "error"


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


def test_poll_paused_or_disconnected(db, setup, workspace):
    post, _video, acc, _rule = setup("instagram")
    post.status = PostStatus.PROCESSING
    now = utcnow()
    post.publish_state = {"_engine": {"processing_since": now.isoformat()}}
    workspace.paused = True
    db.commit()
    with override("publisher", "instagram", fake("instagram")):
        step = publisher.poll_post(db, post.id, now=now)
    assert step.status == PostStatus.PROCESSING
    assert jobs(db, "poll_post")[0].run_at == now + timedelta(minutes=10)
    acc.status = AccountStatus.DISABLED
    db.commit()
    assert publisher.poll_post(db, post.id, now=now).status == PostStatus.CANCELED
    assert publisher.poll_post(db, post.id).status == PostStatus.CANCELED  # no-op now


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


def test_rate_limit_uses_retry_after_when_longer(db, setup):
    post, *_ = setup()
    now = utcnow()
    with override("publisher", "youtube", fake(publish_script=[RateLimitedError("quota", retry_after=3600)])):
        publisher.publish_post(db, post.id, now=now)
    assert post.status == PostStatus.FAILED and post.scheduled_at == now + timedelta(hours=1)


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


def test_unexpected_exception_is_retryable_and_redacted(db, setup, caplog):
    post, *_ = setup()
    boom = ValueError("leaked https://api.example/x?access_token=SECRET123")
    with override("publisher", "youtube", fake(publish_script=[boom])):
        step = publisher.publish_post(db, post.id)
    assert step.status == PostStatus.FAILED
    assert "Unexpected error (ValueError)" in post.error and "SECRET123" not in post.error
    for a in activities(db):
        assert "SECRET123" not in a.message and "Traceback" not in a.message
    assert "SECRET123" not in caplog.text


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
