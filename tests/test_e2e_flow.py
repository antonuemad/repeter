"""End-to-end flows across the web dashboard, the worker, the importer and the rotation engine.

Real pieces: the FastAPI app (login, upload, OAuth callback, rule form, pages), the Postgres
queue and worker loop, ffmpeg (probe, fingerprint, encode, thumbnail), the planner and the
publish/poll pipeline. Fakes: the OAuth providers, TikTok's creator_info, the platform
publishers and the Instagram lister (no platform API is ever called).
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from repeater.db import get_sessionmaker
from repeater.engine import publisher as engine_publisher
from repeater.engine import scheduler
from repeater.jobs import kinds, worker
from repeater.media import storage
from repeater.media.probe import probe
from repeater.models import (
    ActivityLog,
    ConnectedAccount,
    Job,
    JobStatus,
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
    ConnectedAccountData,
    OAuthTokens,
    PublishOutcome,
    PublishResult,
    RemoteVideo,
    TikTokCreatorInfo,
)
from repeater.timeutil import utcnow
from tests.test_web_support import csrf_token, post, set_lang

pytest_plugins = ["tests.test_web_support"]

CROSS_GAP_MINUTES = 180
ALL_PLATFORMS = (Platform.TIKTOK, Platform.YOUTUBE, Platform.INSTAGRAM, Platform.FACEBOOK)


# =========================================================================== fakes


@dataclass
class FakeOAuth:
    """OAuth provider that returns canned accounts for a code."""

    name: str
    accounts: list[ConnectedAccountData]
    uses_pkce: bool = False
    exchanged: list[dict[str, Any]] = field(default_factory=list)

    def authorize_url(
        self, *, state: str, redirect_uri: str, code_challenge: str | None, purpose: str
    ) -> str:
        return f"https://{self.name}.provider.test/authorize?state={state}&purpose={purpose}"

    def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: str | None, purpose: str
    ) -> list[ConnectedAccountData]:
        self.exchanged.append({"code": code, "redirect_uri": redirect_uri, "purpose": purpose})
        return list(self.accounts)

    def refresh(self, account):  # pragma: no cover - tokens are fresh in these tests
        raise AssertionError("no refresh expected")

    def revoke(self, account) -> None:  # pragma: no cover
        return None


@dataclass
class FakePublisher:
    """Publisher that accepts every upload: publish() -> PROCESSING, poll() -> PUBLISHED."""

    platform: Platform
    requests: list[Any] = field(default_factory=list)
    polled: list[dict[str, Any]] = field(default_factory=list)

    @property
    def limits(self):
        return LIMITS[self.platform]

    def validate(self, account, req) -> list[str]:
        return []

    def publish(self, account, req) -> PublishResult:
        self.requests.append(req)
        return PublishResult(
            PublishOutcome.PROCESSING,
            state={"upload_id": f"{self.platform.value}-upload-{req.post_id}"},
            poll_after_seconds=30,
        )

    def poll(self, account, state) -> PublishResult:
        self.polled.append(dict(state))
        post_id = state["upload_id"].rsplit("-", 1)[-1]
        return PublishResult(
            PublishOutcome.PUBLISHED,
            platform_post_id=f"{self.platform.value}-post-{post_id}",
            permalink=f"https://{self.platform.value}.example/p/{post_id}",
            state=state,
        )


@dataclass
class FakeInstagramLister:
    """Official-API listing whose download copies a local file (the 'CDN')."""

    files: dict[str, Path]
    items: list[RemoteVideo]
    downloads: list[tuple[str, str | None, Path]] = field(default_factory=list)
    platform: Platform = Platform.INSTAGRAM

    def list_recent(self, account, *, since=None, limit=50, config=None) -> list[RemoteVideo]:
        return sorted(self.items, key=lambda r: r.created_at, reverse=True)[:limit]

    def download(self, account, remote: RemoteVideo, dest: Path) -> Path:
        self.downloads.append((remote.external_id, remote.download_url, dest))
        shutil.copyfile(self.files[remote.external_id], dest)
        return dest


def _tokens(*, expires: bool = True) -> OAuthTokens:
    return OAuthTokens(
        access_token="fake-access",  # noqa: S106
        refresh_token="fake-refresh" if expires else None,  # noqa: S106
        expires_at=utcnow() + timedelta(days=1) if expires else None,
        scopes=["fake"],
    )


def _fake_providers() -> dict[str, FakeOAuth]:
    return {
        "tiktok": FakeOAuth(
            "tiktok",
            [
                ConnectedAccountData(
                    Platform.TIKTOK,
                    "tt-open-id",
                    "Magic Lab TikTok",
                    _tokens(),
                    extra={"open_id": "tt-open-id", "username": "magiclab"},
                )
            ],
        ),
        "google": FakeOAuth(
            "google",
            [
                ConnectedAccountData(
                    Platform.YOUTUBE,
                    "UC-magic",
                    "Magic Lab Shorts",
                    _tokens(),
                    extra={
                        "channel_id": "UC-magic",
                        "uploads_playlist_id": "UU-magic",
                        "custom_url": "@magic",
                    },
                )
            ],
            uses_pkce=True,
        ),
        "meta": FakeOAuth(
            "meta",
            [
                ConnectedAccountData(
                    Platform.FACEBOOK,
                    "page-1",
                    "Magic Lab Page",
                    _tokens(expires=False),
                    extra={"page_id": "page-1", "fb_user_id": "fb-user-1"},
                ),
                ConnectedAccountData(
                    Platform.INSTAGRAM,
                    "ig-1",
                    "magic.lab",
                    _tokens(expires=False),
                    extra={
                        "ig_user_id": "ig-1",
                        "page_id": "page-1",
                        "username": "magic.lab",
                        "fb_user_id": "fb-user-1",
                    },
                ),
            ],
        ),
    }


# =========================================================================== helpers


def run_jobs(*, kinds_filter: list[str] | None = None, limit: int = 25) -> list[str]:
    """Drive the real worker loop until no due job is left; returns the kinds that ran."""
    sessions = get_sessionmaker()
    ran: list[str] = []
    for _ in range(limit):
        query = select(Job.kind).where(Job.status == JobStatus.QUEUED, Job.run_at <= utcnow())
        if kinds_filter is not None:
            query = query.where(Job.kind.in_(kinds_filter))
        with sessions() as s:
            due = s.execute(query.order_by(Job.run_at, Job.id).limit(1)).scalar()
        if not worker.run_once(sessions, "e2e-worker", job_kinds=kinds_filter):
            break
        ran.append(due or "?")
    return ran


def upload(client, path: Path, *, title: str, caption: str):
    token = csrf_token(client, "/library")
    with path.open("rb") as fh:
        return client.post(
            "/library/upload",
            data={"csrf_token": token, "title": title, "caption": caption},
            files={"file": (path.name, fh, "video/mp4")},
            follow_redirects=False,
        )


def connect(client, purpose: str, provider: str) -> str:
    """Click a connect button and come back from the provider; returns the final redirect."""
    start = client.get(f"/connect/{purpose}", follow_redirects=False)
    assert start.status_code == 303, start.text[:300]
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    back = client.get(f"/oauth/{provider}/callback?code=code-{purpose}&state={state}", follow_redirects=False)
    assert back.status_code == 303, back.text[:300]
    return back.headers["location"]


def configure_tiktok_rule(client, account_id: int, monkeypatch) -> None:
    import repeater.platforms.tiktok as tiktok_module

    info = TikTokCreatorInfo(
        nickname="Magic Lab",
        avatar_url=None,
        privacy_level_options=["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
        comment_disabled=False,
        duet_disabled=True,
        stitch_disabled=False,
        max_video_post_duration_sec=600,
        username="magiclab",
    )
    monkeypatch.setattr(tiktok_module, "creator_info", lambda ctx: info)
    form = client.get(f"/rules/{account_id}")
    assert form.status_code == 200 and "SELF_ONLY" in form.text
    resp = post(
        client,
        f"/rules/{account_id}",
        {
            "enabled": "on",
            "daily_cap": "2",
            "min_gap_minutes": "60",
            "window_start": "10:00",
            "window_end": "22:00",
            "caption_template": "{caption}",
            "hashtags": "magic tricks",
            "mode": "direct",
            "privacy_level": "SELF_ONLY",
            "allow_comment": "on",
            "allow_duet": "on",
            "music_consent": "on",
        },
        page=f"/rules/{account_id}",
    )
    assert resp.status_code == 303 and resp.headers["location"] == f"/rules/{account_id}", resp.text[:500]


# =========================================================================== upload -> publish


def test_upload_encode_plan_publish_on_all_platforms(web_owner, db, workspace, sample_video, monkeypatch):
    set_lang(web_owner, "en")
    # 1. The owner uploads a video through the dashboard.
    resp = upload(web_owner, sample_video, title="Vanishing coin", caption="Watch the coin vanish #magic")
    assert resp.status_code == 303, resp.text[:300]
    video = db.execute(select(Video)).scalar_one()
    assert resp.headers["location"] == f"/library/{video.id}"
    assert video.status == VideoStatus.PROCESSING
    assert list(storage.uploads_dir().iterdir()) == []

    # 2. The worker loop processes it with real ffmpeg.
    ran = run_jobs(kinds_filter=[kinds.PROCESS_VIDEO])
    assert ran == [kinds.PROCESS_VIDEO]
    db.expire_all()
    video = db.get(Video, video.id)
    assert video.status == VideoStatus.READY, video.error
    encoded = storage.abs_path(video.encoded_path)
    info = probe(encoded)
    assert (info.width, info.height) == (1080, 1920) and info.has_audio
    assert storage.abs_path(video.thumbnail_path).is_file()
    assert video.sha256 and video.phash and abs(video.duration_s - 4.0) < 0.2
    assert video.fps == pytest.approx(30.0)

    # 3. Accounts on all four platforms are connected through the OAuth flow.
    providers = _fake_providers()
    with (
        override("oauth", "tiktok", providers["tiktok"]),
        override("oauth", "google", providers["google"]),
        override("oauth", "meta", providers["meta"]),
    ):
        tiktok_redirect = connect(web_owner, "tiktok", "tiktok")
        assert connect(web_owner, "youtube", "google") == "/accounts"
        assert connect(web_owner, "meta", "meta") == "/accounts"
    assert providers["meta"].exchanged[0]["purpose"] == "meta"
    db.expire_all()
    accounts = {a.platform: a for a in db.execute(select(ConnectedAccount)).scalars()}
    assert set(accounts) == set(ALL_PLATFORMS)
    # A new TikTok account goes straight to its posting settings; its rule starts off.
    assert tiktok_redirect == f"/rules/{accounts[Platform.TIKTOK].id}"
    rules = {r.platform: r for r in db.execute(select(RotationRule)).scalars()}
    assert set(rules) == set(ALL_PLATFORMS)
    assert rules[Platform.TIKTOK].enabled is False
    assert all(rules[p].enabled for p in ALL_PLATFORMS if p != Platform.TIKTOK)

    # 4. The owner fills in TikTok's posting settings (privacy level + music consent).
    configure_tiktok_rule(web_owner, accounts[Platform.TIKTOK].id, monkeypatch)
    db.expire_all()
    tiktok_rule = db.get(RotationRule, rules[Platform.TIKTOK].id)
    assert tiktok_rule.enabled is True
    assert tiktok_rule.settings["privacy_level"] == "SELF_ONLY"
    assert tiktok_rule.settings["music_consent_at"]
    assert tiktok_rule.settings["allow_duet"] is False  # creator_info disables duets

    # 5. The planner fills the horizon, keeping the cross-platform gap.
    ws = db.get(Workspace, workspace.id)
    ws.cross_platform_gap_minutes = CROSS_GAP_MINUTES
    db.commit()
    now = utcnow().replace(microsecond=0) - timedelta(hours=49)
    created = scheduler.plan_workspace(db, workspace.id, now=now)
    db.commit()
    assert len(created) == 4
    posts = sorted(db.execute(select(ScheduledPost)).scalars(), key=lambda p: p.scheduled_at)
    assert {p.platform for p in posts} == set(ALL_PLATFORMS)
    assert all(p.video_id == video.id and p.status == PostStatus.SCHEDULED for p in posts)
    for p in posts:
        assert now + scheduler.PLAN_LEAD <= p.scheduled_at <= now + timedelta(hours=48)
    for a, b in zip(posts, posts[1:], strict=False):
        assert b.scheduled_at - a.scheduled_at >= timedelta(minutes=CROSS_GAP_MINUTES)
    publish_jobs = db.execute(select(Job).where(Job.kind == kinds.PUBLISH_POST)).scalars().all()
    assert {j.payload["post_id"] for j in publish_jobs} == {p.id for p in posts}

    # 6. Each post is published (PROCESSING) and then confirmed by polling (PUBLISHED).
    fakes = {p: FakePublisher(p) for p in ALL_PLATFORMS}
    with (
        override("publisher", "tiktok", fakes[Platform.TIKTOK]),
        override("publisher", "youtube", fakes[Platform.YOUTUBE]),
        override("publisher", "instagram", fakes[Platform.INSTAGRAM]),
        override("publisher", "facebook", fakes[Platform.FACEBOOK]),
    ):
        for p in posts:
            at = p.scheduled_at
            step = engine_publisher.publish_post(db, p.id, now=at)
            assert step.status == PostStatus.PROCESSING, step.message
            poll_job = db.execute(
                select(Job).where(
                    Job.dedupe_key == f"{kinds.POLL_POST}:{p.id}", Job.status == JobStatus.QUEUED
                )
            ).scalar_one()
            assert poll_job.run_at == at + timedelta(seconds=30)
            step = engine_publisher.poll_post(db, p.id, now=at + timedelta(seconds=30))
            assert step.status == PostStatus.PUBLISHED, step.message

    db.expire_all()
    posts = sorted(db.execute(select(ScheduledPost)).scalars(), key=lambda p: p.scheduled_at)
    for p in posts:
        assert p.status == PostStatus.PUBLISHED
        assert p.platform_post_id == f"{p.platform.value}-post-{p.id}"
        assert p.permalink == f"https://{p.platform.value}.example/p/{p.id}"
        assert p.published_at == p.scheduled_at + timedelta(seconds=30)
        assert "_engine" not in fakes[p.platform].polled[0]
    # What the adapters received: the encoded file, a signed public URL, captions and settings.
    tiktok_req = fakes[Platform.TIKTOK].requests[0]
    assert tiktok_req.video_path == encoded and tiktok_req.size_bytes == encoded.stat().st_size
    assert tiktok_req.fps == pytest.approx(30.0) and (tiktok_req.width, tiktok_req.height) == (1080, 1920)
    assert re.fullmatch(rf"https://rotator\.test/m/{video.id}/\d+/[\w-]+\.mp4", tiktok_req.public_url)
    assert tiktok_req.caption.startswith("Watch the coin vanish #magic") and "#tricks" in tiktok_req.caption
    assert tiktok_req.settings["privacy_level"] == "SELF_ONLY"
    assert fakes[Platform.YOUTUBE].requests[0].title == "Vanishing coin"
    assert fakes[Platform.YOUTUBE].requests[0].settings["privacy_status"] == "public"
    assert fakes[Platform.INSTAGRAM].requests[0].settings["share_to_feed"] is True

    # 7. The dashboard shows the published links.
    schedule_html = web_owner.get("/schedule").text
    library_html = web_owner.get(f"/library/{video.id}").text
    for p in posts:
        assert f'href="{p.permalink}"' in schedule_html, p.platform
        assert f'href="{p.permalink}"' in library_html, p.platform
    assert "Vanishing coin" in web_owner.get("/library").text

    # 8. Planning again does not schedule the same video on the same accounts twice.
    assert scheduler.plan_workspace(db, workspace.id, now=now + timedelta(hours=1)) == []
    db.rollback()


# =========================================================================== official-API import


def _instagram_setup(db, workspace, make_account, files: dict[str, Path], items: list[RemoteVideo]):
    ig = make_account(
        "instagram",
        external_id="ig-77",
        display_name="magic.lab",
        expires_in=None,
        refresh_token=None,
        extra={"ig_user_id": "ig-77", "page_id": "page-77", "username": "magic.lab", "fb_user_id": "u-1"},
    )
    source = Source(
        workspace_id=workspace.id,
        kind=SourceKind.PLATFORM_API,
        platform=Platform.INSTAGRAM,
        connected_account_id=ig.id,
        name="Magic Lab Instagram",
        config={"backfill_count": 1},
        enabled=True,
    )
    db.add(source)
    db.commit()
    return ig, source, FakeInstagramLister(files=files, items=items)


def test_platform_api_source_imports_video_end_to_end(
    db, workspace, make_account, sample_video, sample_video_landscape
):
    t0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    old = RemoteVideo(
        external_id="reel-old",
        created_at=t0,
        caption="Older reel",
        permalink="https://www.instagram.com/reel/old/",
        download_url="https://cdn.instagram.test/old.mp4",
        extra={"media_type": "VIDEO"},
    )
    ig, source, lister = _instagram_setup(
        db, workspace, make_account, {"reel-old": sample_video_landscape}, [old]
    )
    with override("lister", "instagram", lister):
        # The worker's ticker finds the due source and queues its poll.
        counts = worker.tick(get_sessionmaker())
        assert counts["poll_source"] == 1
        ran = run_jobs(kinds_filter=[kinds.POLL_SOURCE, kinds.IMPORT_VIDEO, kinds.PROCESS_VIDEO])
        assert ran == [kinds.POLL_SOURCE, kinds.IMPORT_VIDEO, kinds.PROCESS_VIDEO]

        db.expire_all()
        video = db.execute(select(Video)).scalar_one()
        assert video.status == VideoStatus.READY, video.error
        assert video.source_id == source.id and video.source_platform == Platform.INSTAGRAM
        assert video.source_account_id == ig.id and video.external_id == "reel-old"
        assert video.caption == "Older reel" and video.permalink == "https://www.instagram.com/reel/old/"
        assert video.source_published_at == t0
        # The listing's download URL travelled to the download step through the job payload.
        assert lister.downloads[0][:2] == ("reel-old", "https://cdn.instagram.test/old.mp4")
        assert lister.downloads[0][2].parent == storage.abs_path(
            storage.video_dir_rel(workspace.id, video.id)
        )
        info = probe(storage.abs_path(video.encoded_path))
        assert (info.width, info.height) == (1080, 1920)  # the landscape source was padded
        ready = db.execute(select(ActivityLog).where(ActivityLog.category == "import")).scalars().all()
        assert any("is ready" in a.message and "landscape" in a.message for a in ready)
        db.refresh(source)
        assert source.last_error is None and source.config["seen_ids"] == ["reel-old"]

        # A newer reel appears; the next poll imports only it.
        new = RemoteVideo(
            external_id="reel-new",
            created_at=utcnow() + timedelta(minutes=1),
            caption="Brand new reel",
            download_url="https://cdn.instagram.test/new.mp4",
        )
        lister.items.append(new)
        lister.files["reel-new"] = sample_video
        kinds.enqueue_poll_source(db, source.id)
        db.commit()
        ran = run_jobs(kinds_filter=[kinds.POLL_SOURCE, kinds.IMPORT_VIDEO, kinds.PROCESS_VIDEO])
        assert ran == [kinds.POLL_SOURCE, kinds.IMPORT_VIDEO, kinds.PROCESS_VIDEO]

    db.expire_all()
    videos = {v.external_id: v for v in db.execute(select(Video)).scalars()}
    assert set(videos) == {"reel-old", "reel-new"}
    assert videos["reel-new"].status == VideoStatus.READY
    assert [d[0] for d in lister.downloads] == ["reel-old", "reel-new"]


def test_duplicate_import_keeps_content_off_its_origin_account(
    web_owner, db, workspace, make_account, sample_video
):
    """The same clip uploaded by hand and found again on Instagram: the Instagram copy becomes a
    DUPLICATE, and the uploaded original is never planned back onto that Instagram account."""
    assert upload(web_owner, sample_video, title="Card force", caption="Pick a card").status_code == 303
    run_jobs(kinds_filter=[kinds.PROCESS_VIDEO])
    original = db.execute(select(Video)).scalar_one()
    db.refresh(original)
    assert original.status == VideoStatus.READY

    reel = RemoteVideo(external_id="reel-dup", created_at=utcnow(), caption="Pick a card (reel)")
    ig, source, lister = _instagram_setup(db, workspace, make_account, {"reel-dup": sample_video}, [reel])
    fb = make_account("facebook", display_name="Magic Lab Page", expires_in=None, extra={"page_id": "p"})
    from repeater.engine import rules as engine_rules

    for account in (ig, fb):
        engine_rules.ensure_rule(db, account)
    db.commit()
    with override("lister", "instagram", lister):
        kinds.enqueue_poll_source(db, source.id)
        db.commit()
        run_jobs(kinds_filter=[kinds.POLL_SOURCE, kinds.IMPORT_VIDEO, kinds.PROCESS_VIDEO])
    db.expire_all()
    duplicate = db.execute(select(Video).where(Video.external_id == "reel-dup")).scalar_one()
    assert duplicate.status == VideoStatus.DUPLICATE and duplicate.duplicate_of_id == original.id

    created = scheduler.plan_workspace(db, workspace.id, now=utcnow())
    db.commit()
    assert [(p.video_id, p.connected_account_id) for p in created] == [(original.id, fb.id)]

    # "Publish now" to the origin account is refused with a clear message as well.
    set_lang(web_owner, "en")
    resp = post(
        web_owner,
        f"/library/{original.id}/publish-now",
        {"account_id": str(ig.id)},
        page=f"/library/{original.id}",
    )
    assert resp.status_code == 303
    assert "publishing it there again would duplicate it" in web_owner.get(f"/library/{original.id}").text
    assert (
        db.execute(select(ScheduledPost).where(ScheduledPost.connected_account_id == ig.id)).first() is None
    )
