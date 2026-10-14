"""Rotation planner and rule defaults (repeater.engine.scheduler, repeater.engine.rules)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from repeater.engine import rules, scheduler
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
    Workspace,
)
from repeater.timeutil import utcnow

CAIRO = ZoneInfo("Africa/Cairo")
# 1 December 2026: Cairo is on standard time (UTC+2), far from any DST change.
DEC1_0800 = datetime(2026, 12, 1, 8, 0, tzinfo=CAIRO).astimezone(UTC)
VIDEO_BASE = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def cairo(day: int, hour: int, minute: int = 0, month: int = 12) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=CAIRO).astimezone(UTC)


def local(dt: datetime) -> datetime:
    return dt.astimezone(CAIRO)


@pytest.fixture
def ws(db, workspace) -> Workspace:
    workspace.horizon_hours = 48
    workspace.cross_platform_gap_minutes = 24 * 60
    db.commit()
    return workspace


@pytest.fixture
def video(db, ws):
    counter = {"n": 0}

    def _make(**kw) -> Video:
        counter["n"] += 1
        n = counter["n"]
        v = Video(
            workspace_id=ws.id,
            status=kw.pop("status", VideoStatus.READY),
            title=kw.pop("title", f"Video {n}"),
            caption=kw.pop("caption", f"Caption {n}"),
            duration_s=kw.pop("duration_s", 30.0),
            width=1080,
            height=1920,
            encoded_size_bytes=kw.pop("encoded_size_bytes", 5_000_000),
            encoded_path=kw.pop("encoded_path", f"ws{ws.id}/v{n}/encoded.mp4"),
            created_at=kw.pop("created_at", VIDEO_BASE + timedelta(hours=n)),
            **kw,
        )
        db.add(v)
        db.commit()
        return v

    return _make


@pytest.fixture
def rule_for(db, ws):
    def _make(account, **kw) -> RotationRule:
        r = RotationRule(
            workspace_id=ws.id,
            connected_account_id=account.id,
            platform=account.platform,
            enabled=kw.pop("enabled", True),
            daily_cap=kw.pop("daily_cap", 2),
            min_gap_minutes=kw.pop("min_gap_minutes", 240),
            window_start=kw.pop("window_start", time(10, 0)),
            window_end=kw.pop("window_end", time(22, 0)),
            settings=kw.pop("settings", rules.default_settings(account.platform)),
            **kw,
        )
        db.add(r)
        db.commit()
        return r

    return _make


def add_post(db, ws, video, account, *, status, at, cycle=0, published=None) -> ScheduledPost:
    p = ScheduledPost(
        workspace_id=ws.id,
        video_id=video.id,
        connected_account_id=account.id,
        platform=account.platform,
        cycle=cycle,
        idempotency_key=f"{video.id}:{account.id}:{cycle}",
        scheduled_at=at,
        status=status,
        published_at=published if published is not None else (at if status == PostStatus.PUBLISHED else None),
        created_by="user",
    )
    db.add(p)
    db.commit()
    return p


def plan(db, ws, now=DEC1_0800) -> list[ScheduledPost]:
    created = scheduler.plan_workspace(db, ws.id, now=now)
    db.commit()
    return sorted(created, key=lambda p: p.scheduled_at)


def warnings(db) -> list[ActivityLog]:
    return list(db.execute(select(ActivityLog).where(ActivityLog.level == ActivityLevel.WARNING)).scalars())


# =========================================================================== rules


def test_ensure_rule_defaults_per_platform(db, make_account):
    yt = make_account("youtube")
    tt = make_account("tiktok")
    drive = make_account("gdrive")
    r_yt = rules.ensure_rule(db, yt)
    r_tt = rules.ensure_rule(db, tt)
    db.commit()
    assert rules.ensure_rule(db, drive) is None
    assert r_yt.enabled is True and r_yt.platform == Platform.YOUTUBE
    assert r_yt.settings["privacy_status"] == "public" and r_yt.daily_cap == 2
    assert r_yt.min_gap_minutes == 240 and r_yt.window_start == time(10, 0) and r_yt.window_end == time(22, 0)
    assert r_yt.caption_template == "{caption}" and r_yt.evergreen_days is None
    assert r_tt.enabled is False
    assert r_tt.settings["privacy_level"] is None and r_tt.settings["music_consent_at"] is None
    # Defaults are copies: editing one rule's settings must not leak into the module constant.
    r_tt.settings["privacy_level"] = "PUBLIC_TO_EVERYONE"
    assert rules.default_settings("tiktok")["privacy_level"] is None
    # Idempotent.
    assert rules.ensure_rule(db, yt).id == r_yt.id
    assert db.execute(select(RotationRule)).scalars().all().__len__() == 2
    msgs = [a.message for a in db.execute(select(ActivityLog)).scalars()]
    assert any("Music Usage Confirmation" in m for m in msgs)


def test_rule_problems_and_cap_clamp(db, make_account, rule_for):
    tt = make_account("tiktok")
    r = rule_for(tt, settings=rules.default_settings("tiktok"))
    assert len(rules.rule_problems(r)) == 2
    r.settings = {**r.settings, "mode": "inbox"}
    assert rules.rule_problems(r) == []
    r.settings = {
        **r.settings,
        "mode": "direct",
        "privacy_level": "SELF_ONLY",
        "music_consent_at": "2026-10-01",
    }
    assert rules.rule_problems(r) == []
    r.daily_cap = 50
    assert rules.effective_daily_cap(r) == 10
    r.daily_cap = -1
    assert rules.effective_daily_cap(r) == 0


# =========================================================================== slots and caps


def test_spreads_posts_across_window_with_daily_cap(db, ws, make_account, rule_for, video):
    acc = make_account("youtube")
    rule_for(acc)
    vids = [video() for _ in range(6)]
    posts = plan(db, ws)
    assert len(posts) == 4  # 2 per day over a 48 h horizon
    times = [local(p.scheduled_at) for p in posts]
    for t in times:
        assert time(10, 0) <= t.time() < time(22, 0)
    per_day = Counter(t.date() for t in times)
    assert per_day == {date(2026, 12, 1): 2, date(2026, 12, 2): 2}
    # One post per half of the window (10:00-16:00 and 16:00-22:00), each within the jitter.
    for t in times:
        base = t.replace(hour=10 if t.hour < 16 else 16, minute=0)
        assert timedelta(0) <= t - base <= timedelta(minutes=scheduler.MAX_JITTER_MINUTES)
    assert sorted(t.hour for t in times) == [10, 10, 16, 16]
    for a, b in zip(posts, posts[1:], strict=False):
        assert b.scheduled_at - a.scheduled_at >= timedelta(minutes=240)
    # Newest imported first, in slot order.
    assert [p.video_id for p in posts] == [v.id for v in reversed(vids)][:4]
    assert {p.idempotency_key for p in posts} == {f"{p.video_id}:{acc.id}:0" for p in posts}
    assert all(p.status == PostStatus.SCHEDULED and p.created_by == "scheduler" for p in posts)
    jobs = {j.payload["post_id"]: j for j in db.execute(select(Job)).scalars()}
    for p in posts:
        assert jobs[p.id].kind == "publish_post" and jobs[p.id].run_at == p.scheduled_at
    # Planning again changes nothing.
    assert plan(db, ws) == []
    assert db.execute(select(ScheduledPost)).scalars().all().__len__() == 4


def test_not_earlier_than_two_minutes_from_now(db, ws, make_account, rule_for, video):
    acc = make_account("youtube")
    rule_for(acc, daily_cap=1)
    video()
    now = cairo(1, 13, 30)  # inside the window, after its start
    posts = plan(db, ws, now=now)
    assert len(posts) == 1
    assert posts[0].scheduled_at >= now + timedelta(minutes=2)
    assert posts[0].scheduled_at < now + timedelta(minutes=3)


def test_horizon_limits_planning(db, ws, make_account, rule_for, video):
    ws.horizon_hours = 6
    db.commit()
    acc = make_account("youtube")
    rule_for(acc)
    video(), video()
    posts = plan(db, ws)  # 08:00 + 6 h = 14:00: only the 10:00 segment fits
    assert len(posts) == 1 and local(posts[0].scheduled_at).hour == 10


def test_daily_cap_counts_published_and_active_posts_per_local_day(db, ws, make_account, rule_for, video):
    acc = make_account("youtube")
    rule_for(acc, daily_cap=1, min_gap_minutes=0)
    old = video()
    other = video()
    # 23:30 on 1 Dec in UTC is already 2 Dec 01:30 in Cairo: it fills 2 Dec, not 1 Dec.
    add_post(db, ws, old, acc, status=PostStatus.PUBLISHED, at=datetime(2026, 12, 1, 23, 30, tzinfo=UTC))
    add_post(db, ws, other, acc, status=PostStatus.SCHEDULED, at=cairo(3, 9, 0))
    for _ in range(3):
        video()
    posts = plan(db, ws)
    days = [local(p.scheduled_at).date() for p in posts]
    assert days == [date(2026, 12, 1)]


def test_midnight_crossing_window(db, ws, make_account, rule_for, video):
    acc = make_account("instagram")
    rule_for(acc, window_start=time(22, 0), window_end=time(2, 0), daily_cap=2, min_gap_minutes=60)
    for _ in range(6):
        video()
    posts = plan(db, ws, now=cairo(1, 12, 0))
    times = [local(p.scheduled_at) for p in posts]
    assert [(t.day, t.hour) for t in times] == [(1, 22), (2, 0), (2, 22), (3, 0)]
    for t in times:
        assert t.time() >= time(22, 0) or t.time() < time(2, 0)
    assert max(Counter(t.date() for t in times).values()) <= 2


def test_midnight_crossing_window_cap_is_per_calendar_day(db, ws, make_account, rule_for, video):
    acc = make_account("instagram")
    rule_for(acc, window_start=time(22, 0), window_end=time(2, 0), daily_cap=2, min_gap_minutes=60)
    busy = video()
    add_post(db, ws, busy, acc, status=PostStatus.SCHEDULED, at=cairo(2, 12, 0))
    for _ in range(6):
        video()
    posts = plan(db, ws, now=cairo(1, 12, 0))
    times = [local(p.scheduled_at) for p in posts]
    # 2 Dec already has one post, so only one of its two window parts (00:xx) is used.
    assert [(t.day, t.hour) for t in times] == [(1, 22), (2, 0), (3, 0)]


def test_min_gap_pushes_post_later_in_segment(db, ws, make_account, rule_for, video):
    acc = make_account("youtube")
    rule_for(acc, daily_cap=2, min_gap_minutes=240)
    earlier = video()
    add_post(db, ws, earlier, acc, status=PostStatus.PUBLISHED, at=cairo(1, 9, 0))
    video()
    posts = plan(db, ws)
    assert local(posts[0].scheduled_at) == datetime(2026, 12, 1, 13, 0, tzinfo=CAIRO)


def test_cross_platform_gap_between_accounts(db, ws, make_account, rule_for, video):
    yt = make_account("youtube")
    ig = make_account("instagram")
    rule_for(yt, daily_cap=1)
    rule_for(ig, daily_cap=1)
    v = video()
    posts = plan(db, ws)
    by_account = {p.connected_account_id: p for p in posts if p.video_id == v.id}
    assert set(by_account) == {yt.id, ig.id}
    gap = abs(by_account[ig.id].scheduled_at - by_account[yt.id].scheduled_at)
    assert gap >= timedelta(hours=24)


def test_cross_platform_gap_counts_published_posts_and_source_publish(db, ws, make_account, rule_for, video):
    yt = make_account("youtube")
    fb = make_account("facebook")
    rule_for(yt, daily_cap=1)
    published_elsewhere = video()
    add_post(db, ws, published_elsewhere, fb, status=PostStatus.PUBLISHED, at=cairo(1, 7, 0))
    fresh_on_source = video(source_published_at=cairo(1, 7, 30))
    posts = plan(db, ws, now=cairo(1, 8, 0))
    # Both videos must wait 24 h, so nothing fits on 1 Dec; on 2 Dec the newest goes first.
    assert [local(p.scheduled_at).date() for p in posts] == [date(2026, 12, 2)]
    assert posts[0].video_id == fresh_on_source.id
    assert posts[0].scheduled_at >= cairo(2, 7, 30)


def test_jitter_is_deterministic(db, ws, make_account, rule_for, video):
    acc = make_account("youtube")
    rule_for(acc)
    for _ in range(3):
        video()
    first = [(p.video_id, p.scheduled_at) for p in plan(db, ws)]
    db.execute(ScheduledPost.__table__.delete())
    db.execute(Job.__table__.delete())
    db.commit()
    second = [(p.video_id, p.scheduled_at) for p in plan(db, ws)]
    assert first == second


# =========================================================================== cycles


def test_evergreen_plans_next_cycle(db, ws, make_account, rule_for, video):
    acc = make_account("youtube")
    rule = rule_for(acc, evergreen_days=90)
    v = video()
    add_post(db, ws, v, acc, status=PostStatus.PUBLISHED, at=DEC1_0800 - timedelta(days=100))
    posts = plan(db, ws)
    assert len(posts) == 1 and posts[0].cycle == 1
    assert posts[0].idempotency_key == f"{v.id}:{acc.id}:1"
    # Not yet due / disabled: nothing.
    db.execute(ScheduledPost.__table__.delete().where(ScheduledPost.id == posts[0].id))
    db.commit()
    rule.evergreen_days = 120
    db.commit()
    assert plan(db, ws) == []
    rule.evergreen_days = None
    db.commit()
    assert plan(db, ws) == []


def test_canceled_post_is_reused(db, ws, make_account, rule_for, video):
    acc = make_account("youtube")
    rule_for(acc)
    v = video()
    old = add_post(db, ws, v, acc, status=PostStatus.CANCELED, at=cairo(1, 1, 0))
    old.error, old.attempts, old.publish_state = "account disconnected", 2, {"x": 1}
    db.commit()
    posts = plan(db, ws)
    assert [p.id for p in posts] == [old.id]
    db.refresh(old)
    assert old.status == PostStatus.SCHEDULED and old.error is None and old.attempts == 0
    assert old.publish_state == {} and old.created_by == "scheduler"
    assert old.scheduled_at >= cairo(1, 10, 0)
    job = db.execute(select(Job)).scalar_one()
    assert job.payload == {"post_id": old.id} and job.run_at == old.scheduled_at


@pytest.mark.parametrize(
    "status",
    [
        PostStatus.SKIPPED,
        PostStatus.PUBLISHED,
        PostStatus.NEEDS_ATTENTION,
        PostStatus.SCHEDULED,
        PostStatus.FAILED,
    ],
)
def test_existing_post_blocks_recreation(db, ws, make_account, rule_for, video, status):
    acc = make_account("youtube")
    rule_for(acc)
    v = video()
    add_post(db, ws, v, acc, status=status, at=cairo(5, 12, 0))
    assert plan(db, ws) == []


def test_requeued_canceled_job_moves_existing_job(db, ws, make_account, rule_for, video):
    acc = make_account("youtube")
    rule_for(acc)
    v = video()
    old = add_post(db, ws, v, acc, status=PostStatus.CANCELED, at=cairo(1, 1, 0))
    from repeater.jobs import kinds

    kinds.enqueue_publish_post(db, old.id, run_at=cairo(1, 1, 0))
    db.commit()
    posts = plan(db, ws)
    jobs = db.execute(select(Job).where(Job.status == JobStatus.QUEUED)).scalars().all()
    assert len(jobs) == 1 and jobs[0].run_at == posts[0].scheduled_at


# =========================================================================== eligibility


def test_never_to_source_platform_or_source_account(db, ws, make_account, rule_for, video):
    tt = make_account("tiktok")
    yt = make_account("youtube")
    yt2 = make_account("youtube")
    ready = {"privacy_level": "PUBLIC_TO_EVERYONE", "music_consent_at": "2026-10-01T00:00:00+00:00"}
    rule_for(tt, settings={**rules.default_settings("tiktok"), **ready}, daily_cap=5, min_gap_minutes=0)
    rule_for(yt, daily_cap=5, min_gap_minutes=0)
    rule_for(yt2, daily_cap=5, min_gap_minutes=0)
    from_tiktok = video(source_platform=Platform.TIKTOK)
    from_yt = video(source_platform=Platform.YOUTUBE, source_account_id=yt.id)
    ws.cross_platform_gap_minutes = 0
    db.commit()
    posts = plan(db, ws)
    targets = {(p.video_id, p.connected_account_id) for p in posts}
    assert targets == {(from_tiktok.id, yt.id), (from_tiktok.id, yt2.id), (from_yt.id, tt.id)}


def test_overrides_exclude_platforms(db, ws, make_account, rule_for, video):
    yt = make_account("youtube")
    ig = make_account("instagram")
    rule_for(yt)
    rule_for(ig)
    v = video(overrides={"exclude_platforms": ["Instagram"]})
    posts = plan(db, ws)
    assert [(p.video_id, p.connected_account_id) for p in posts] == [(v.id, yt.id)]


def test_only_after(db, ws, make_account, rule_for, video):
    acc = make_account("youtube")
    rule_for(acc, only_after=VIDEO_BASE + timedelta(hours=1, minutes=30))
    video()  # created at base + 1 h
    newer = video()  # base + 2 h
    posts = plan(db, ws)
    assert [p.video_id for p in posts] == [newer.id]


def test_only_ready_encoded_videos(db, ws, make_account, rule_for, video):
    acc = make_account("youtube")
    rule_for(acc, daily_cap=5, min_gap_minutes=0)
    ok = video()
    video(status=VideoStatus.DUPLICATE)
    video(status=VideoStatus.ARCHIVED)
    video(status=VideoStatus.PROCESSING)
    video(encoded_path=None)
    assert [p.video_id for p in plan(db, ws)] == [ok.id]


def test_limits_skip_logs_one_warning_per_video_platform(db, ws, make_account, rule_for, video):
    fb1 = make_account("facebook")
    fb2 = make_account("facebook")
    yt = make_account("youtube")
    for acc in (fb1, fb2, yt):
        rule_for(acc)
    ws.cross_platform_gap_minutes = 0
    db.commit()
    long_video = video(duration_s=120.0)  # Facebook Reels: at most 90 s; YouTube Shorts: 180 s
    posts = plan(db, ws)
    assert [(p.video_id, p.connected_account_id) for p in posts] == [(long_video.id, yt.id)]
    warns = warnings(db)
    assert len(warns) == 1
    assert "Facebook" in warns[0].message and "2:00" in warns[0].message
    assert warns[0].data["video_id"] == long_video.id and warns[0].data["platform"] == "facebook"
    plan(db, ws, now=DEC1_0800 + timedelta(minutes=5))
    assert len(warnings(db)) == 1


def test_limits_problem_messages():
    v = Video(duration_s=2.0, encoded_size_bytes=10)
    assert "at least 3" in scheduler.limits_problem(v, "instagram")
    v.duration_s = None
    assert scheduler.limits_problem(v, "youtube") == "its duration is unknown"
    v.duration_s, v.encoded_size_bytes = 30.0, 400 * 1024**2
    assert "300 MB" in scheduler.limits_problem(v, "instagram")
    assert scheduler.limits_problem(v, "tiktok") is None


def test_priority_fewest_publishes_breaks_ties(db, ws, make_account, rule_for, video):
    target = make_account("youtube")
    other = make_account("instagram")
    rule_for(target, daily_cap=1)
    same = VIDEO_BASE + timedelta(days=1)
    popular = video(created_at=same)
    fresh = video(created_at=same)
    add_post(db, ws, popular, other, status=PostStatus.PUBLISHED, at=cairo(1, 1, 0, month=11))
    posts = plan(db, ws)
    assert posts[0].video_id == fresh.id
    assert posts[1].video_id == popular.id


# =========================================================================== what is planned at all


def test_skips_paused_disabled_and_inactive(db, ws, make_account, rule_for, video):
    active = make_account("youtube")
    off = make_account("instagram")
    broken = make_account("facebook")
    rule_for(active)
    rule_for(off, enabled=False)
    rule_for(broken)
    broken.status = AccountStatus.NEEDS_RECONNECT
    db.commit()
    video()
    ws.paused = True
    db.commit()
    assert plan(db, ws) == []
    ws.paused = False
    db.commit()
    assert {p.connected_account_id for p in plan(db, ws)} == {active.id}
    assert scheduler.plan_workspace(db, 999_999, now=DEC1_0800) == []


def test_tiktok_rule_without_settings_is_not_planned(db, ws, make_account, rule_for, video):
    tt = make_account("tiktok")
    rule_for(tt, settings=rules.default_settings("tiktok"))
    video()
    assert plan(db, ws) == []
    warns = warnings(db)
    assert len(warns) == 1 and "Music Usage Confirmation" in warns[0].message
    plan(db, ws)
    assert len(warnings(db)) == 1


def test_account_with_repeated_failures_is_suspended(db, ws, make_account, rule_for, video):
    acc = make_account("youtube")
    rule_for(acc, daily_cap=5)
    now = utcnow()
    for _ in range(3):
        add_post(db, ws, video(), acc, status=PostStatus.NEEDS_ATTENTION, at=now - timedelta(hours=1))
    video()
    assert scheduler.plan_workspace(db, ws.id, now=now) == []
    db.commit()
    assert any("on hold" in w.message for w in warnings(db))


def test_youtube_project_budget_across_workspaces(db, ws, make_account, rule_for, video, monkeypatch):
    monkeypatch.setattr(scheduler, "YOUTUBE_DAILY_UPLOAD_BUDGET", 1)
    acc = make_account("youtube")
    rule_for(acc, daily_cap=1)
    other_ws = Workspace(name="Other", timezone="UTC")
    db.add(other_ws)
    db.commit()
    other_acc = make_account("youtube", workspace_id=other_ws.id)
    other_video = Video(workspace_id=other_ws.id, status=VideoStatus.READY, encoded_path="x")
    db.add(other_video)
    db.commit()
    # 1 Dec 12:00 Cairo is 1 Dec 02:00 in Los Angeles: that Pacific day's budget is used up.
    add_post(db, other_ws, other_video, other_acc, status=PostStatus.SCHEDULED, at=cairo(1, 12, 0))
    video(), video()
    posts = plan(db, ws)
    pacific = [p.scheduled_at.astimezone(scheduler.YOUTUBE_QUOTA_TZ).date() for p in posts]
    assert date(2026, 12, 1) not in pacific and len(posts) >= 1


def test_window_bounds_follow_dst_change():
    # Egypt leaves summer time at the end of 29 Oct 2026 (UTC+3 -> UTC+2).
    before = scheduler.window_bounds(date(2026, 10, 29), time(10, 0), time(22, 0), CAIRO)
    after = scheduler.window_bounds(date(2026, 10, 30), time(10, 0), time(22, 0), CAIRO)
    assert before[0] == datetime(2026, 10, 29, 7, 0, tzinfo=UTC)
    assert after[0] == datetime(2026, 10, 30, 8, 0, tzinfo=UTC)
    crossing = scheduler.window_bounds(date(2026, 10, 29), time(22, 0), time(2, 0), CAIRO)
    assert crossing[1] - crossing[0] == timedelta(hours=5)  # the night gains an hour
    full = scheduler.window_bounds(date(2026, 12, 1), time(0, 0), time(0, 0), CAIRO)
    assert full[1] - full[0] == timedelta(hours=24)


def test_summary_activity_lists_new_posts(db, ws, make_account, rule_for, video):
    acc = make_account("youtube")
    rule_for(acc, daily_cap=1)
    video()
    posts = plan(db, ws)
    info = (
        db.execute(
            select(ActivityLog).where(
                ActivityLog.level == ActivityLevel.INFO, ActivityLog.category == "publish"
            )
        )
        .scalars()
        .all()
    )
    assert any(a.data.get("post_ids") == [p.id for p in posts] for a in info)
