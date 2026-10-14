"""End-to-end flows across the web dashboard, the worker, the importer and the rotation engine.

Real pieces: the FastAPI app (login, upload, OAuth callback, rule form, pages), the Postgres
queue and worker loop, ffmpeg (probe, fingerprint, encode, thumbnail), the planner and the
publish/poll pipeline. Fakes: the OAuth providers, TikTok's creator_info, the platform
publishers and the Instagram lister (no platform API is ever called).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from sqlalchemy import select, update

from repeater.db import get_sessionmaker
from repeater.engine import publisher as engine_publisher
from repeater.engine import scheduler
from repeater.jobs import kinds, worker
from repeater.media import storage
from repeater.media.probe import probe
from repeater.models import (
    ActivityLog,
    ConnectedAccount,
    DataDeletionRequest,
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
from repeater.platforms import override, youtube
from repeater.platforms.base import (
    LIMITS,
    ConnectedAccountData,
    OAuthTokens,
    PublishOutcome,
    PublishResult,
    RemoteVideo,
    TikTokCreatorInfo,
    UnknownOutcomeError,
)
from repeater.timeutil import utcnow
from tests.test_platform_youtube import ACCESS as YT_ACCESS
from tests.test_platform_youtube import SESSION_URI as YT_SESSION_URI
from tests.test_platform_youtube import lost_final_response_server
from tests.test_web_meta import signed_request
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


def enable_rule(client, account, monkeypatch=None, **extra) -> None:
    """Review an account's posting settings and turn publishing on through the real rule form.
    Every new rule starts OFF, and YouTube needs an explicit audience answer (no default)."""
    if account.platform == Platform.TIKTOK:
        configure_tiktok_rule(client, account.id, monkeypatch)
        return
    form = {
        "enabled": "on",
        "daily_cap": "2",
        "min_gap_minutes": "60",
        "window_start": "10:00",
        "window_end": "22:00",
        "caption_template": "{caption}",
        "hashtags": "",
    }
    if account.platform == Platform.YOUTUBE:
        form.update(privacy_status="public", made_for_kids="no", category_id="22")
    elif account.platform == Platform.INSTAGRAM:
        form["share_to_feed"] = "on"
    form.update(extra)
    resp = post(client, f"/rules/{account.id}", form, page=f"/rules/{account.id}")
    assert resp.status_code == 303 and resp.headers["location"] == f"/rules/{account.id}", resp.text[:500]


def make_due(kind: str) -> None:
    """Pull the queued jobs of one kind forward to now (the follow-ups are queued seconds ahead)."""
    with get_sessionmaker()() as s:
        s.execute(update(Job).where(Job.kind == kind, Job.status == JobStatus.QUEUED).values(run_at=utcnow()))
        s.commit()


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
        youtube_redirect = connect(web_owner, "youtube", "google")
        meta_redirect = connect(web_owner, "meta", "meta")
    assert providers["meta"].exchanged[0]["purpose"] == "meta"
    db.expire_all()
    accounts = {a.platform: a for a in db.execute(select(ConnectedAccount)).scalars()}
    assert set(accounts) == set(ALL_PLATFORMS)
    # A single new account goes straight to its own settings; a Meta login brings two (a Page and
    # its Instagram account), so it goes to the rules list.
    assert tiktok_redirect == f"/rules/{accounts[Platform.TIKTOK].id}"
    assert youtube_redirect == f"/rules/{accounts[Platform.YOUTUBE].id}"
    assert meta_redirect == "/rules"
    # Connecting never starts publishing: every rule exists, switched off.
    rules = {r.platform: r for r in db.execute(select(RotationRule)).scalars()}
    assert set(rules) == set(ALL_PLATFORMS)
    assert not any(r.enabled for r in rules.values())
    assert rules[Platform.YOUTUBE].settings["self_declared_made_for_kids"] is None  # no default answer
    ws = db.get(Workspace, workspace.id)
    ws.cross_platform_gap_minutes = CROSS_GAP_MINUTES
    db.commit()
    assert scheduler.plan_workspace(db, workspace.id, now=utcnow() - timedelta(hours=49)) == []
    db.rollback()

    # 4. The owner reviews each account's settings and turns publishing on through the rule form.
    html = web_owner.get(f"/rules/{accounts[Platform.YOUTUBE].id}").text
    assert not re.search(r'name="made_for_kids" value="(yes|no)"[^>]*checked', html)
    refused = post(  # YouTube's audience question has no default: it must be answered explicitly
        web_owner,
        f"/rules/{accounts[Platform.YOUTUBE].id}",
        {
            "enabled": "on",
            "daily_cap": "2",
            "min_gap_minutes": "60",
            "window_start": "10:00",
            "window_end": "22:00",
            "privacy_status": "public",
            "category_id": "22",
        },
        page=f"/rules/{accounts[Platform.YOUTUBE].id}",
    )
    assert refused.status_code == 400 and "made for kids" in refused.text
    db.expire_all()
    assert db.get(RotationRule, rules[Platform.YOUTUBE].id).enabled is False
    for platform in (Platform.TIKTOK, Platform.YOUTUBE, Platform.INSTAGRAM, Platform.FACEBOOK):
        enable_rule(web_owner, accounts[platform], monkeypatch)
    db.expire_all()
    rules = {r.platform: r for r in db.execute(select(RotationRule)).scalars()}
    assert all(rules[p].enabled for p in ALL_PLATFORMS)
    tiktok_rule = rules[Platform.TIKTOK]
    assert tiktok_rule.settings["privacy_level"] == "SELF_ONLY"
    assert tiktok_rule.settings["music_consent_at"]
    assert tiktok_rule.settings["allow_duet"] is False  # creator_info disables duets
    assert rules[Platform.YOUTUBE].settings["self_declared_made_for_kids"] is False

    # 5. The planner fills the horizon, keeping the cross-platform gap.
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
        engine_rules.ensure_rule(db, account).enabled = True  # new rules start off
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


def test_duplicate_found_after_planning_cancels_the_post_to_its_origin_account(
    web_owner, db, workspace, make_account, sample_video
):
    """Contract C2: the original is already planned for Instagram and Facebook when the importer
    finds the same clip on Instagram. The Instagram post is canceled (the content is already
    there), the Facebook one stays, and the planner does not bring the Instagram one back."""
    assert upload(web_owner, sample_video, title="Card force", caption="Pick a card").status_code == 303
    run_jobs(kinds_filter=[kinds.PROCESS_VIDEO])
    original = db.execute(select(Video)).scalar_one()
    db.refresh(original)
    assert original.status == VideoStatus.READY
    reel = RemoteVideo(external_id="reel-dup2", created_at=utcnow(), caption="Pick a card (reel)")
    ig, source, lister = _instagram_setup(db, workspace, make_account, {"reel-dup2": sample_video}, [reel])
    fb = make_account("facebook", display_name="Magic Lab Page", expires_in=None, extra={"page_id": "p"})
    for account in (ig, fb):
        enable_rule(web_owner, account)
    workspace.cross_platform_gap_minutes = 0
    db.commit()
    created = scheduler.plan_workspace(db, workspace.id, now=utcnow())
    db.commit()
    by_account = {p.connected_account_id: p.id for p in created if p.video_id == original.id}
    assert set(by_account) == {ig.id, fb.id}

    with override("lister", "instagram", lister):
        kinds.enqueue_poll_source(db, source.id)
        db.commit()
        run_jobs(kinds_filter=[kinds.POLL_SOURCE, kinds.IMPORT_VIDEO, kinds.PROCESS_VIDEO])
    db.expire_all()
    duplicate = db.execute(select(Video).where(Video.external_id == "reel-dup2")).scalar_one()
    assert duplicate.status == VideoStatus.DUPLICATE and duplicate.duplicate_of_id == original.id
    ig_post, fb_post = db.get(ScheduledPost, by_account[ig.id]), db.get(ScheduledPost, by_account[fb.id])
    assert ig_post.status == PostStatus.CANCELED and "already exists" in ig_post.error
    assert fb_post.status == PostStatus.SCHEDULED
    assert any(
        "canceled 1 scheduled post" in a.message
        for a in db.execute(select(ActivityLog).where(ActivityLog.category == "publish")).scalars()
    )
    assert scheduler.plan_workspace(db, workspace.id, now=utcnow()) == []
    db.rollback()
    assert db.get(ScheduledPost, by_account[ig.id]).status == PostStatus.CANCELED


# =========================================================================== rule edits re-plan


def _ready_video(web_make_video, **kw) -> Video:
    return web_make_video(**kw)


def test_editing_a_rule_cancels_and_replans_the_accounts_unstarted_posts(
    web_owner, db, workspace, make_account, web_make_video, web_plan_calls
):
    """Posts the planner already put on the calendar keep the window they were planned under, so a
    rule edit cancels that account's unstarted planner posts (engine.actions.invalidate_plan,
    contract C2) and the worker's next plan_workspace creates them again under the new window."""
    video = web_make_video()
    workspace.cross_platform_gap_minutes = 0  # only the rule's own window decides the slot
    db.commit()
    youtube_account = make_account("youtube", extra={"channel_id": "UC-e"})
    instagram = make_account("instagram", expires_in=None, extra={"ig_user_id": "i", "page_id": "p"})
    for account in (youtube_account, instagram):
        enable_rule(web_owner, account)
    web_plan_calls.calls.clear()
    created = scheduler.plan_workspace(db, workspace.id, now=utcnow())
    db.commit()
    assert {p.connected_account_id for p in created} == {youtube_account.id, instagram.id}
    posts = {p.connected_account_id: p for p in created}
    yt_post_id, ig_post_id = posts[youtube_account.id].id, posts[instagram.id].id

    # The owner narrows the YouTube window to 03:00-05:00 (the workspace is on Cairo time).
    enable_rule(web_owner, youtube_account, window_start="03:00", window_end="05:00")
    assert [c[1]["account_id"] for c in web_plan_calls.calls] == [youtube_account.id]
    db.expire_all()
    assert db.get(ScheduledPost, yt_post_id).status == PostStatus.CANCELED
    assert db.get(ScheduledPost, ig_post_id).status == PostStatus.SCHEDULED  # another account: untouched
    assert db.execute(
        select(Job).where(Job.kind == kinds.PLAN_WORKSPACE, Job.status == JobStatus.QUEUED)
    ).first()

    # The worker plans again: the same row comes back inside the new window.
    assert kinds.PLAN_WORKSPACE in run_jobs(kinds_filter=[kinds.PLAN_WORKSPACE])
    db.expire_all()
    replanned = db.get(ScheduledPost, yt_post_id)
    assert replanned.status == PostStatus.SCHEDULED and replanned.video_id == video.id
    local = replanned.scheduled_at.astimezone(ZoneInfo("Africa/Cairo")).time()
    assert time(3, 0) <= local <= time(5, 0), local
    # A caption-only edit does not cancel anything.
    web_plan_calls.calls.clear()
    enable_rule(
        web_owner, youtube_account, window_start="03:00", window_end="05:00", caption_template="x {caption}"
    )
    assert [c for c in web_plan_calls.calls] == []
    db.expire_all()
    assert db.get(ScheduledPost, yt_post_id).status == PostStatus.SCHEDULED


# =========================================================================== lost answers


@dataclass
class LostAnswerPublisher:
    """publish() uploads the video and then loses the platform's answer (UnknownOutcomeError with
    the state needed to ask); poll() finds the video live. A second upload is a test failure."""

    platform: Platform
    uploads: list[int] = field(default_factory=list)
    polled: list[dict[str, Any]] = field(default_factory=list)

    @property
    def limits(self):
        return LIMITS[self.platform]

    def validate(self, account, req) -> list[str]:
        return []

    def publish(self, account, req) -> PublishResult:
        assert not req.state, "the engine republished instead of polling the earlier upload"
        self.uploads.append(req.post_id)
        raise UnknownOutcomeError(
            "The connection dropped after the upload.",
            state={"upload_id": f"lost-{req.post_id}"},
            code="lost_answer",
        )

    def poll(self, account, state) -> PublishResult:
        self.polled.append(dict(state))
        return PublishResult(
            PublishOutcome.PUBLISHED,
            platform_post_id="live-1",
            permalink="https://instagram.example/p/live-1",
            state=state,
        )


def test_lost_answer_is_polled_and_the_video_is_uploaded_exactly_once(
    web_owner, db, make_account, web_make_video
):
    """Contract C1 end to end: the dashboard's 'Publish now', the worker, the engine and a publisher
    whose first publish() raises UnknownOutcomeError. The engine stores the adapter's state, the
    post waits in PROCESSING, poll() resolves it to PUBLISHED, and nothing is uploaded twice."""
    video = web_make_video(title="Rope trick")
    account = make_account("instagram", expires_in=None, extra={"ig_user_id": "i", "page_id": "p"})
    enable_rule(web_owner, account)
    fake = LostAnswerPublisher(Platform.INSTAGRAM)
    set_lang(web_owner, "en")
    with override("publisher", "instagram", fake):
        resp = post(
            web_owner, f"/library/{video.id}/publish-now", {"account_id": str(account.id)}, page="/library"
        )
        assert resp.status_code == 303
        assert run_jobs(kinds_filter=[kinds.PUBLISH_POST]) == [kinds.PUBLISH_POST]
        db.expire_all()
        row = db.execute(select(ScheduledPost)).scalar_one()
        assert row.status == PostStatus.PROCESSING and row.attempts == 0
        assert row.publish_state["upload_id"] == f"lost-{row.id}"
        assert "processing_since" in row.publish_state["_engine"]
        assert fake.uploads == [row.id] and fake.polled == []
        assert "may be live" not in (row.error or "")
        # The status check is queued shortly after; it resolves the post. No retry was queued.
        assert db.execute(
            select(Job).where(Job.kind == kinds.POLL_POST, Job.status == JobStatus.QUEUED)
        ).one()
        assert (
            db.execute(
                select(Job).where(Job.kind == kinds.PUBLISH_POST, Job.status == JobStatus.QUEUED)
            ).first()
            is None
        )
        make_due(kinds.POLL_POST)
        assert run_jobs(kinds_filter=[kinds.POLL_POST]) == [kinds.POLL_POST]
    db.expire_all()
    row = db.execute(select(ScheduledPost)).scalar_one()
    assert row.status == PostStatus.PUBLISHED and row.platform_post_id == "live-1"
    assert fake.uploads == [row.id], "exactly one upload"
    assert fake.polled == [{"upload_id": f"lost-{row.id}"}]  # the engine's own keys never reach the adapter
    page = web_owner.get("/schedule").text
    assert "https://instagram.example/p/live-1" in page


def test_real_youtube_adapter_lost_final_response_is_resolved_by_polling_one_videos_insert(
    web_owner, db, make_account, web_make_video, monkeypatch
):
    """The same flow with the real YouTube adapter on a mocked API: the last chunk is stored but
    its answer and every status query are lost. publish() raises UnknownOutcomeError carrying the
    resumable session; the engine polls the session, finds the created video and confirms it. One
    videos.insert call and one data upload in total (the daily quota is 100 calls)."""
    monkeypatch.setattr(youtube, "_sleep", lambda seconds: None)
    video = web_make_video(title="Coin vanish")
    account = make_account(
        "youtube", access_token=YT_ACCESS, extra={"channel_id": "UCchan", "uploads_playlist_id": "UUchan"}
    )
    enable_rule(web_owner, account)
    size = storage.abs_path(video.encoded_path).stat().st_size
    server = lost_final_response_server(size)
    status_item = {"id": "vid123", "status": {"uploadStatus": "processed", "privacyStatus": "public"}}
    with respx.mock(assert_all_called=False) as mock:
        start = mock.route(method="POST", host="www.googleapis.com", path="/upload/youtube/v3/videos")
        start.mock(return_value=httpx.Response(200, headers={"Location": YT_SESSION_URI}))
        mock.route(method="PUT", host="www.googleapis.com", path="/upload/youtube/v3/videos").mock(
            side_effect=server
        )
        listing = mock.get("https://www.googleapis.com/youtube/v3/videos").mock(
            return_value=httpx.Response(200, json={"items": [status_item]})
        )
        post(web_owner, f"/library/{video.id}/publish-now", {"account_id": str(account.id)}, page="/library")
        run_jobs(kinds_filter=[kinds.PUBLISH_POST])
        db.expire_all()
        row = db.execute(select(ScheduledPost)).scalar_one()
        assert row.status == PostStatus.PROCESSING and row.attempts == 0
        assert row.publish_state["session_uri"] == YT_SESSION_URI and row.publish_state["total"] == size
        # The activity log and the post's error never carry the resumable session URI.
        texts = [row.error or ""] + [
            a.message + json.dumps(a.data) for a in db.execute(select(ActivityLog)).scalars()
        ]
        assert not any("upload_id=" in t or YT_ACCESS in t for t in texts)
        # Poll 1 asks the upload session and finds the video; poll 2 is the ordinary status check.
        for _ in range(2):
            make_due(kinds.POLL_POST)
            assert run_jobs(kinds_filter=[kinds.POLL_POST]) == [kinds.POLL_POST]
        assert start.call_count == 1, "a second videos.insert would post the video twice"
        assert len(server.chunk_calls) == 1 and listing.call_count == 1
    db.expire_all()
    row = db.execute(select(ScheduledPost)).scalar_one()
    assert row.status == PostStatus.PUBLISHED and row.platform_post_id == "vid123"
    assert row.permalink == "https://www.youtube.com/shorts/vid123"
    assert (
        db.execute(select(Job).where(Job.kind == kinds.PUBLISH_POST, Job.status == JobStatus.QUEUED)).first()
        is None
    )


# =========================================================================== disconnect while publishing


def test_disconnect_while_a_post_is_processing_keeps_it_flagged_and_never_reposts(
    web_owner, db, workspace, make_account, web_make_video
):
    video = web_make_video(title="Cups and balls")
    account = make_account(
        "instagram", external_id="ig-d", display_name="magic.d", expires_in=None,
        extra={"ig_user_id": "ig-d", "page_id": "page-d", "fb_user_id": "fb-d"},
    )  # fmt: skip
    enable_rule(web_owner, account)
    fake = FakePublisher(Platform.INSTAGRAM)
    set_lang(web_owner, "en")
    with override("publisher", "instagram", fake), override("oauth", "meta", FakeOAuth("meta", [])):
        post(web_owner, f"/library/{video.id}/publish-now", {"account_id": str(account.id)}, page="/library")
        run_jobs(kinds_filter=[kinds.PUBLISH_POST])
        db.expire_all()
        row = db.execute(select(ScheduledPost)).scalar_one()
        assert row.status == PostStatus.PROCESSING and len(fake.requests) == 1

        # The owner disconnects the account while the platform is still processing the upload.
        resp = post(web_owner, f"/accounts/{account.id}/disconnect", page="/accounts")
        assert resp.status_code == 303
        db.expire_all()
        row = db.get(ScheduledPost, row.id)
        assert row.status == PostStatus.NEEDS_ATTENTION, "never canceled: the video may be live"
        assert row.publish_state["upload_id"] == f"instagram-upload-{row.id}"
        assert row.publish_state["_engine"]["may_be_live"] is True
        assert "may be live" in row.error
        gone = db.get(ConnectedAccount, account.id)
        assert gone.status.value == "disabled" and gone.token_enc is None
        assert db.execute(select(RotationRule)).scalar_one().enabled is False

        # The queued status check does nothing: no poll, no publish, no state change.
        make_due(kinds.POLL_POST)
        run_jobs(kinds_filter=[kinds.POLL_POST])
        assert fake.polled == []
        db.expire_all()
        assert db.get(ScheduledPost, row.id).status == PostStatus.NEEDS_ATTENTION

        # The schedule says it may be live and asks before anything is published again.
        page = web_owner.get("/schedule").text
        assert "Needs attention" in page or "needs attention" in page.lower()
        confirm = web_owner.get(f"/schedule/{row.id}/publish-now")
        assert confirm.status_code == 200 and "already be live" in confirm.text.lower().replace("\n", " ")
        refused = post(web_owner, f"/schedule/{row.id}/publish-now", {}, page="/schedule")
        assert refused.status_code == 303
        db.expire_all()
        assert db.get(ScheduledPost, row.id).status == PostStatus.NEEDS_ATTENTION
        assert fake.requests and len(fake.requests) == 1

        # Reconnecting the same account and turning its rule on again must not plan the video back
        # onto it: the post that may be live still owns that video/account cycle.
        again = FakeOAuth(
            "meta",
            [
                ConnectedAccountData(
                    Platform.INSTAGRAM,
                    "ig-d",
                    "magic.d",
                    _tokens(expires=False),
                    extra={"ig_user_id": "ig-d", "page_id": "page-d", "fb_user_id": "fb-d"},
                )
            ],
        )
        with override("oauth", "meta", again):
            connect(web_owner, "meta", "meta")
        enable_rule(web_owner, account)
        created = scheduler.plan_workspace(db, workspace.id, now=utcnow())
        db.commit()
        assert created == []
    db.expire_all()
    assert len(db.execute(select(ScheduledPost)).scalars().all()) == 1
    assert len(fake.requests) == 1


# =========================================================================== Meta data deletion


def test_meta_data_deletion_removes_every_trace_of_the_accounts_and_their_videos(
    web_client, db, workspace, sample_video, sample_video_landscape, make_account, web_make_video
):
    """Retention C5(d): a signed Meta deletion request deletes the matching accounts, the videos
    imported from them (rows and files), their posts, sources, rules, jobs and every activity-log
    entry that names them, within the job's run; other data of the workspace is left alone."""
    from tests.test_web_support import login

    assert login(web_client).status_code == 303
    client = web_client
    # Two accounts of one Facebook user, an Instagram source and one imported video.
    page = make_account(
        "facebook", external_id="page-del", display_name="Zorro Page", expires_in=None,
        extra={"page_id": "page-del", "fb_user_id": "fb-del"},
    )  # fmt: skip
    ig = make_account(
        "instagram", external_id="ig-del", display_name="zorro.reels", expires_in=None,
        extra={"ig_user_id": "ig-del", "page_id": "page-del", "username": "zorro.reels", "fb_user_id": "fb-del"},
    )  # fmt: skip
    keep_account = make_account("facebook", external_id="page-keep", display_name="Keeper Page", expires_in=None,
                                extra={"page_id": "page-keep", "fb_user_id": "fb-other"})  # fmt: skip
    for account in (page, ig, keep_account):
        enable_rule(client, account)
    source = Source(
        workspace_id=workspace.id, kind=SourceKind.PLATFORM_API, platform=Platform.INSTAGRAM,
        connected_account_id=ig.id, name="Zorro reels", config={"backfill_count": 1}, enabled=True,
    )  # fmt: skip
    db.add(source)
    db.commit()
    remote = RemoteVideo(
        external_id="reel-del", created_at=utcnow(), caption="Zorro secret reel",
        permalink="https://www.instagram.com/reel/del/", download_url="https://cdn.instagram.test/del.mp4",
    )  # fmt: skip
    lister = FakeInstagramLister(files={"reel-del": sample_video_landscape}, items=[remote])
    with override("lister", "instagram", lister):
        kinds.enqueue_poll_source(db, source.id)
        db.commit()
        run_jobs(kinds_filter=[kinds.POLL_SOURCE, kinds.IMPORT_VIDEO, kinds.PROCESS_VIDEO])
    db.expire_all()
    imported = db.execute(select(Video).where(Video.external_id == "reel-del")).scalar_one()
    assert imported.status == VideoStatus.READY, imported.error
    folder = storage.abs_path(storage.video_dir_rel(workspace.id, imported.id))
    assert folder.is_dir()
    kept_video = web_make_video(title="Unrelated upload")

    # The imported video is published to the Facebook Page (never back to its origin account).
    created = scheduler.plan_workspace(db, workspace.id, now=utcnow() - timedelta(hours=49))
    db.commit()
    imported_posts = [p for p in created if p.video_id == imported.id]
    assert imported_posts and all(p.connected_account_id != ig.id for p in imported_posts)
    fake = FakePublisher(Platform.FACEBOOK)
    with override("publisher", "facebook", fake):
        for p in sorted(imported_posts, key=lambda p: p.scheduled_at):
            if p.platform != Platform.FACEBOOK or p.connected_account_id != page.id:
                continue
            engine_publisher.publish_post(db, p.id, now=p.scheduled_at)
            engine_publisher.poll_post(db, p.id, now=p.scheduled_at + timedelta(seconds=30))
    db.expire_all()
    published = [p for p in db.execute(select(ScheduledPost)).scalars() if p.video_id == imported.id]
    assert any(p.status == PostStatus.PUBLISHED for p in published)
    assert db.execute(select(ActivityLog).where(ActivityLog.message.contains("Zorro Page"))).first(), (
        "the log names the account before the deletion"
    )

    imported_id, source_id, page_id, ig_id = imported.id, source.id, page.id, ig.id
    keep_account_id, kept_video_id = keep_account.id, kept_video.id
    kept_file = storage.abs_path(kept_video.encoded_path)

    # Meta calls the data deletion callback; the job runs at once.
    resp = client.post(
        "/meta/data-deletion",
        data={
            "signed_request": signed_request(
                {"algorithm": "HMAC-SHA256", "expires": 4_000_000_000, "issued_at": 1, "user_id": "fb-del"}
            )
        },
    )
    assert resp.status_code == 200
    status_url = resp.json()["url"]
    path = "/" + status_url.split("/", 3)[3] + "?lang=en"
    assert "In progress" in client.get(path).text
    assert run_jobs(kinds_filter=[kinds.DELETE_USER_DATA]) == [kinds.DELETE_USER_DATA]

    db.expire_all()
    names = ("Zorro", "zorro", "page-del", "ig-del", "reel-del", "secret reel")
    assert (
        db.execute(
            select(ConnectedAccount).where(ConnectedAccount.extra["fb_user_id"].astext == "fb-del")
        ).first()
        is None
    )
    assert db.get(ConnectedAccount, keep_account_id) is not None
    assert db.get(Video, imported_id) is None and not folder.exists()
    assert db.get(Video, kept_video_id) is not None and kept_file.is_file()
    assert db.execute(select(ScheduledPost).where(ScheduledPost.video_id == imported_id)).first() is None
    assert (
        db.execute(
            select(ScheduledPost).where(ScheduledPost.connected_account_id.in_([page_id, ig_id]))
        ).first()
        is None
    )
    assert db.get(Source, source_id) is None
    assert (
        db.execute(
            select(RotationRule).where(RotationRule.connected_account_id.in_([page_id, ig_id]))
        ).first()
        is None
    )
    assert db.execute(
        select(RotationRule).where(RotationRule.connected_account_id == keep_account_id)
    ).scalar_one()
    for entry in db.execute(select(ActivityLog)).scalars():
        blob = entry.message + json.dumps(entry.data)
        assert not any(n in blob for n in names), (entry.message, entry.data)
        assert entry.data.get("account_id") not in (page_id, ig_id)
        assert entry.data.get("video_id") != imported_id and entry.data.get("source_id") != source_id
    for job in db.execute(select(Job)).scalars():
        assert job.payload.get("video_id") != imported_id and job.payload.get("source_id") != source_id
        assert not any(n in json.dumps(job.payload) + (job.last_error or "") for n in names)
    summary = db.execute(select(ActivityLog).where(ActivityLog.actor == "meta")).scalars().all()
    assert summary and all("account_id" not in e.data for e in summary)
    record = db.execute(select(DataDeletionRequest)).scalar_one()
    assert record.status == "completed" and record.completed_at is not None
    done = client.get(path).text
    assert "Completed" in done and "In progress" not in done
