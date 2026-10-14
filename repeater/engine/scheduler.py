"""Rotation planner (PLAN.md 4.1 and 4.2). Runs every 5 minutes per workspace.

plan_workspace(session, workspace_id, now) fills the next `horizon_hours` with ScheduledPost
rows. Given the database and `now` it is deterministic: nothing depends on randomness or on the
wall clock. It flushes but does not commit; the caller commits.

Algorithm
1. Nothing is planned for a paused workspace. Planning considers rules that are enabled, whose
   account is ACTIVE, whose settings are complete (rules.rule_problems: TikTok needs a privacy
   level and music consent) and whose account is not suspended. An account is suspended when its
   last 3 finished posts (published or needs-attention) since max(now - 24 h, the rule's last
   edit) all needed attention: the planner stops feeding a broken account until the user fixes
   it, edits the rule, or a day passes. Rules are processed in id order.
2. Candidates are READY videos with an encoded file, imported after rule.only_after when set,
   whose source platform differs from the account's platform, that did not come from this
   account, whose duplicates (status DUPLICATE, duplicate_of_id = the video: the same content
   imported again from another source) did not come from this account or platform either,
   whose overrides do not list the platform in "exclude_platforms", and that fit the
   platform LIMITS (duration and encoded size). A video that does not fit is skipped silently
   apart from one WARNING activity per (video, platform) pair, deduplicated across runs.
3. Cycle: one post per (video, account, cycle), idempotency_key "{video}:{account}:{cycle}".
   Cycle 0 is the first publish. When rule.evergreen_days is set and the newest PUBLISHED cycle
   of the pair was published more than that many days ago, cycle + 1 is planned. Any active post
   of the pair blocks planning; a SKIPPED, PUBLISHED or NEEDS_ATTENTION post blocks its own cycle;
   a CANCELED post is reused (reset to scheduled) instead of creating a new row.
4. Priority per account: newest imported first (created_at), then fewest PUBLISHED posts across
   all accounts, then the higher video id.
5. Slots: each day's window [window_start, window_end) in the workspace timezone is split into
   `cap` equal segments (cap = rule.daily_cap clamped by rules.ENGINE_DAILY_CAPS). A window whose
   end is not after its start crosses midnight (22:00-02:00 ends the next day); equal times mean
   a 24-hour window. Each segment holds at most one post of the account. A post goes at the
   segment start plus a deterministic jitter of up to 10 minutes per (video, account), so posts
   do not cluster at window start, and is pushed later inside its segment as needed to satisfy:
     - not earlier than now + 2 minutes and not later than now + horizon_hours;
     - at least min_gap_minutes from any active or published post on the same account;
     - at least workspace.cross_platform_gap_minutes from any active or published post of the
       same video on another account, and from the video's original publish on its source
       (video.source_published_at) when known;
     - at most `cap` active or published posts on the account per local calendar day;
     - for YouTube, at most YOUTUBE_DAILY_UPLOAD_BUDGET uploads per Pacific day across all
       workspaces (the project's videos.insert quota resets at midnight Pacific time).
   The first segment that can take the video wins; videos that fit nowhere wait for a later run.
6. Each new or reused post gets a publish_post job at its scheduled_at.
"""

from __future__ import annotations

import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from repeater.activity import log_activity
from repeater.engine import captions, rules
from repeater.engine.publisher import queue_publish
from repeater.models import (
    ACTIVE_POST_STATUSES,
    PUBLISH_PLATFORMS,
    AccountStatus,
    ActivityLevel,
    ActivityLog,
    ConnectedAccount,
    Platform,
    PostStatus,
    RotationRule,
    ScheduledPost,
    Video,
    VideoStatus,
    Workspace,
)
from repeater.platforms.base import LIMITS
from repeater.timeutil import ensure_aware, tz, utcnow

PLAN_LEAD = timedelta(minutes=2)
MAX_JITTER_MINUTES = 10
SUSPEND_AFTER_FAILURES = 3
SUSPEND_LOOKBACK = timedelta(hours=24)
# Default videos.insert quota is 100 per project per day; keep a margin for manual uploads.
YOUTUBE_PROJECT_DAILY_UPLOADS = 100
YOUTUBE_DAILY_UPLOAD_BUDGET = 90
YOUTUBE_QUOTA_TZ = ZoneInfo("America/Los_Angeles")
COUNTED_STATUSES = (*ACTIVE_POST_STATUSES, PostStatus.PUBLISHED)
_ADVISORY_LOCK_NAMESPACE = 727_001


@dataclass(frozen=True)
class _PostRow:
    id: int
    video_id: int
    account_id: int
    cycle: int
    status: PostStatus
    at: datetime  # published_at for published posts, otherwise scheduled_at


@dataclass
class _Segment:
    start: datetime
    end: datetime
    occupied: bool = False


@dataclass
class _AccountPlan:
    rule: RotationRule
    account: ConnectedAccount
    platform: Platform
    cap: int
    min_gap: timedelta
    times: list[datetime]
    day_counts: Counter[date]
    segments: list[_Segment]


@dataclass
class _Context:
    session: Session
    workspace: Workspace
    now: datetime
    earliest: datetime
    latest: datetime
    zone: ZoneInfo
    cross_gap: timedelta
    videos: list[Video]
    pair_posts: dict[tuple[int, int], list[_PostRow]]
    video_times: dict[int, list[tuple[int, datetime]]]
    youtube_days: Counter[date]
    warned: set[str]
    # Where each video's content already exists, from its duplicates (see duplicate_origins).
    dup_platforms: dict[int, set[Platform]] = field(default_factory=dict)
    dup_accounts: dict[int, set[int]] = field(default_factory=dict)
    created: list[ScheduledPost] = field(default_factory=list)
    limit_notes: dict[tuple[int, Platform], str | None] = field(default_factory=dict)


def plan_workspace(session: Session, workspace_id: int, now: datetime | None = None) -> list[ScheduledPost]:
    """Create (or revive) scheduled posts for the workspace's horizon. Returns them."""
    now = ensure_aware(now) or utcnow()
    workspace = session.get(Workspace, workspace_id)
    if workspace is None or workspace.paused:
        return []
    lock_workspace(session, workspace.id)
    pairs = _active_rules(session, workspace.id)
    if not pairs:
        return []
    ctx = _load_context(session, workspace, now)
    if ctx is None:
        return []
    for rule, account in pairs:
        _plan_account(ctx, rule, account)
    if ctx.created:
        log_activity(
            session,
            workspace.id,
            ActivityLevel.INFO,
            "publish",
            f"Scheduled {len(ctx.created)} new post{'s' if len(ctx.created) != 1 else ''}.",
            post_ids=[p.id for p in ctx.created],
        )
    session.flush()
    return ctx.created


def lock_workspace(session: Session, workspace_id: int) -> None:
    """Serialise schedule changes of one workspace until the end of the transaction."""
    session.execute(select(func.pg_advisory_xact_lock(_ADVISORY_LOCK_NAMESPACE, int(workspace_id))))


def duplicate_origins(
    session: Session, workspace_id: int, video_ids: list[int] | None = None
) -> tuple[dict[int, set[Platform]], dict[int, set[int]]]:
    """Source platforms and source accounts of the DUPLICATE videos of each video: the same content
    is already published there, so it must not be posted there again (PLAN.md 3.6)."""
    q = select(Video.duplicate_of_id, Video.source_platform, Video.source_account_id).where(
        Video.workspace_id == workspace_id,
        Video.status == VideoStatus.DUPLICATE,
        Video.duplicate_of_id.isnot(None),
    )
    if video_ids is not None:
        q = q.where(Video.duplicate_of_id.in_(video_ids))
    platforms: dict[int, set[Platform]] = defaultdict(set)
    accounts: dict[int, set[int]] = defaultdict(set)
    for original_id, platform, account_id in session.execute(q).all():
        if platform is not None:
            platforms[original_id].add(Platform(platform))
        if account_id is not None:
            accounts[original_id].add(int(account_id))
    return dict(platforms), dict(accounts)


def limits_problem(video: Video, platform: Platform | str) -> str | None:
    """Why the platform cannot take the video (duration/size), or None."""
    platform = Platform(platform)
    lim = LIMITS[platform]
    name = captions.platform_name(platform)
    duration = video.duration_s
    if duration is None:
        return "its duration is unknown"
    if duration < lim.min_duration_s:
        return f"it is {duration:.1f} s long and {name} needs at least {lim.min_duration_s:g} s"
    if duration > lim.max_duration_s:
        return f"it is {_mmss(duration)} long and {name} accepts at most {_mmss(lim.max_duration_s)}"
    size = video.encoded_size_bytes
    if size is not None and size > lim.max_size_bytes:
        mb, max_mb = size / 1024**2, lim.max_size_bytes / 1024**2
        return f"the file is {mb:.0f} MB and {name} accepts at most {max_mb:.0f} MB"
    return None


# =========================================================================== loading


def _active_rules(session: Session, workspace_id: int) -> list[tuple[RotationRule, ConnectedAccount]]:
    rows = session.execute(
        select(RotationRule, ConnectedAccount)
        .join(ConnectedAccount, RotationRule.connected_account_id == ConnectedAccount.id)
        .where(
            RotationRule.workspace_id == workspace_id,
            RotationRule.enabled.is_(True),
            ConnectedAccount.workspace_id == workspace_id,
            ConnectedAccount.status == AccountStatus.ACTIVE,
            ConnectedAccount.platform.in_(PUBLISH_PLATFORMS),
        )
        .order_by(RotationRule.id)
    ).all()
    return [(rule, account) for rule, account in rows]


def _load_context(session: Session, workspace: Workspace, now: datetime) -> _Context | None:
    earliest = now + PLAN_LEAD
    latest = now + timedelta(hours=max(int(workspace.horizon_hours or 0), 0))
    if latest < earliest:
        return None
    videos = list(
        session.execute(
            select(Video).where(
                Video.workspace_id == workspace.id,
                Video.status == VideoStatus.READY,
                Video.encoded_path.isnot(None),
            )
        ).scalars()
    )
    posts = _load_posts(session, workspace.id)
    pair_posts: dict[tuple[int, int], list[_PostRow]] = defaultdict(list)
    video_times: dict[int, list[tuple[int, datetime]]] = defaultdict(list)
    publishes: Counter[int] = Counter()
    for p in posts:
        pair_posts[(p.video_id, p.account_id)].append(p)
        if p.status in COUNTED_STATUSES:
            video_times[p.video_id].append((p.account_id, p.at))
        if p.status == PostStatus.PUBLISHED:
            publishes[p.video_id] += 1
    videos.sort(key=lambda v: (-_ts(v.created_at), publishes[v.id], -v.id))
    dup_platforms, dup_accounts = duplicate_origins(session, workspace.id)
    return _Context(
        session=session,
        workspace=workspace,
        now=now,
        earliest=earliest,
        latest=latest,
        zone=tz(workspace.timezone),
        cross_gap=timedelta(minutes=max(int(workspace.cross_platform_gap_minutes or 0), 0)),
        videos=videos,
        pair_posts=pair_posts,
        video_times=video_times,
        youtube_days=_youtube_days(session, earliest - timedelta(days=1), latest + timedelta(days=1)),
        warned=_warned_keys(session, workspace.id),
        dup_platforms=dup_platforms,
        dup_accounts=dup_accounts,
    )


def _load_posts(session: Session, workspace_id: int) -> list[_PostRow]:
    rows = session.execute(
        select(
            ScheduledPost.id,
            ScheduledPost.video_id,
            ScheduledPost.connected_account_id,
            ScheduledPost.cycle,
            ScheduledPost.status,
            ScheduledPost.scheduled_at,
            ScheduledPost.published_at,
        ).where(ScheduledPost.workspace_id == workspace_id)
    ).all()
    return [
        _PostRow(
            id=r.id,
            video_id=r.video_id,
            account_id=r.connected_account_id,
            cycle=int(r.cycle or 0),
            status=PostStatus(r.status),
            at=_post_time(r.status, r.scheduled_at, r.published_at),
        )
        for r in rows
    ]


def _youtube_days(session: Session, start: datetime, end: datetime) -> Counter[date]:
    at = func.coalesce(ScheduledPost.published_at, ScheduledPost.scheduled_at)
    rows = session.execute(
        select(at).where(
            ScheduledPost.platform == Platform.YOUTUBE,
            ScheduledPost.status.in_(COUNTED_STATUSES),
            at >= start,
            at <= end,
        )
    ).scalars()
    return Counter(ensure_aware(t).astimezone(YOUTUBE_QUOTA_TZ).date() for t in rows)


def _warned_keys(session: Session, workspace_id: int) -> set[str]:
    rows = session.execute(
        select(ActivityLog.data["dedupe"].astext).where(
            ActivityLog.workspace_id == workspace_id, ActivityLog.data.has_key("dedupe")
        )
    ).scalars()
    return {r for r in rows if r}


# =========================================================================== per account


def _plan_account(ctx: _Context, rule: RotationRule, account: ConnectedAccount) -> None:
    platform = Platform(account.platform)
    problems = rules.rule_problems(rule)
    if problems:
        _warn_once(
            ctx,
            f"rule_not_ready:{rule.id}:{int(_ts(rule.updated_at))}",
            f"Nothing is scheduled for {captions.platform_name(platform)} ({account.display_name}) until "
            f"you {' and '.join(problems)} in its posting settings.",
            account_id=account.id,
            rule_id=rule.id,
        )
        return
    failing = _suspended_by(ctx, rule, account)
    if failing is not None:
        _warn_once(
            ctx,
            f"suspended:{account.id}:{failing}",
            f"Scheduling for {captions.platform_name(platform)} ({account.display_name}) is on hold: the "
            f"last {SUSPEND_AFTER_FAILURES} posts needed attention. Fix the problem shown on those posts "
            "and publish one again, or edit the account's rule, to resume.",
            account_id=account.id,
        )
        return
    plan = _account_plan(ctx, rule, account, platform)
    if plan is None:
        return
    for video in ctx.videos:
        if not any(not s.occupied for s in plan.segments):
            break
        if not _eligible(ctx, plan, video):
            continue
        nxt = _next_cycle(ctx, plan, video)
        if nxt is None:
            continue
        slot = _find_slot(ctx, plan, video)
        if slot is None:
            continue
        _place(ctx, plan, video, nxt[0], nxt[1], slot)


def _account_plan(
    ctx: _Context, rule: RotationRule, account: ConnectedAccount, platform: Platform
) -> _AccountPlan | None:
    cap = rules.effective_daily_cap(rule)
    if cap <= 0:
        return None
    segments = _segments(ctx, rule, cap)
    if not segments:
        return None
    times = sorted(
        p.at
        for posts in ctx.pair_posts.values()
        for p in posts
        if p.account_id == account.id and p.status in COUNTED_STATUSES
    )
    plan = _AccountPlan(
        rule=rule,
        account=account,
        platform=platform,
        cap=cap,
        min_gap=timedelta(minutes=max(int(rule.min_gap_minutes or 0), 0)),
        times=times,
        day_counts=Counter(t.astimezone(ctx.zone).date() for t in times),
        segments=segments,
    )
    for seg in segments:
        seg.occupied = any(seg.start <= t < seg.end for t in times)
    return plan


def _segments(ctx: _Context, rule: RotationRule, cap: int) -> list[_Segment]:
    first = ctx.earliest.astimezone(ctx.zone).date() - timedelta(days=1)
    last = ctx.latest.astimezone(ctx.zone).date()
    out: list[_Segment] = []
    day = first
    while day <= last:
        start, end = window_bounds(day, rule.window_start, rule.window_end, ctx.zone)
        step = (end - start) / cap
        for i in range(cap):
            seg_start = start + step * i
            seg_end = end if i == cap - 1 else start + step * (i + 1)
            if seg_end > ctx.earliest and seg_start <= ctx.latest:
                out.append(_Segment(seg_start, seg_end))
        day += timedelta(days=1)
    return sorted(out, key=lambda s: s.start)


def window_bounds(
    day: date, start: time | None, end: time | None, zone: ZoneInfo
) -> tuple[datetime, datetime]:
    """UTC bounds of the posting window that opens on local `day`. An end that is not after the
    start belongs to the next day (windows crossing midnight; equal times = 24 hours)."""
    start = start or time(10, 0)
    end = end or time(22, 0)
    end_day = day if end > start else day + timedelta(days=1)
    return (
        datetime.combine(day, start, tzinfo=zone).astimezone(UTC),
        datetime.combine(end_day, end, tzinfo=zone).astimezone(UTC),
    )


def _suspended_by(ctx: _Context, rule: RotationRule, account: ConnectedAccount) -> int | None:
    """Id of the newest failed post when the account is suspended (see module docstring)."""
    since = ctx.now - SUSPEND_LOOKBACK
    edited = ensure_aware(rule.updated_at)
    if edited is not None and edited > since:
        since = edited
    rows = ctx.session.execute(
        select(ScheduledPost.id, ScheduledPost.status)
        .where(
            ScheduledPost.connected_account_id == account.id,
            ScheduledPost.status.in_([PostStatus.PUBLISHED, PostStatus.NEEDS_ATTENTION]),
            ScheduledPost.updated_at >= since,
        )
        .order_by(ScheduledPost.updated_at.desc(), ScheduledPost.id.desc())
        .limit(SUSPEND_AFTER_FAILURES)
    ).all()
    if len(rows) == SUSPEND_AFTER_FAILURES and all(r.status == PostStatus.NEEDS_ATTENTION for r in rows):
        return int(rows[0].id)
    return None


# =========================================================================== per video


def _eligible(ctx: _Context, plan: _AccountPlan, video: Video) -> bool:
    only_after = ensure_aware(plan.rule.only_after)
    if only_after is not None and _ts(video.created_at) <= only_after.timestamp():
        return False
    if video.source_platform is not None and Platform(video.source_platform) == plan.platform:
        return False
    if video.source_account_id is not None and video.source_account_id == plan.account.id:
        return False
    if plan.platform in ctx.dup_platforms.get(video.id, ()) or plan.account.id in ctx.dup_accounts.get(
        video.id, ()
    ):
        return False
    if plan.platform.value in _excluded_platforms(video):
        return False
    key = (video.id, plan.platform)
    if key not in ctx.limit_notes:
        ctx.limit_notes[key] = limits_problem(video, plan.platform)
        if ctx.limit_notes[key]:
            _warn_once(
                ctx,
                f"limits:{video.id}:{plan.platform.value}",
                f"'{captions.video_label(video)}' will not be published to "
                f"{captions.platform_name(plan.platform)}: {ctx.limit_notes[key]}.",
                video_id=video.id,
                platform=plan.platform.value,
            )
    return ctx.limit_notes[key] is None


def _excluded_platforms(video: Video) -> set[str]:
    raw = (video.overrides or {}).get("exclude_platforms") or []
    if not isinstance(raw, list | tuple):
        return set()
    return {str(x).strip().lower() for x in raw}


def _next_cycle(ctx: _Context, plan: _AccountPlan, video: Video) -> tuple[int, int | None] | None:
    """(cycle, id of a CANCELED post to reuse) to plan for the pair, or None."""
    posts = ctx.pair_posts.get((video.id, plan.account.id), [])
    if any(p.status in ACTIVE_POST_STATUSES for p in posts):
        return None
    published = [p for p in posts if p.status == PostStatus.PUBLISHED]
    if published:
        newest = max(published, key=lambda p: p.cycle)
        days = plan.rule.evergreen_days
        if not days or days <= 0 or newest.at > ctx.now - timedelta(days=days):
            return None
        cycle = newest.cycle + 1
    else:
        cycle = 0
    existing = next((p for p in posts if p.cycle == cycle), None)
    if existing is None:
        return cycle, None
    if existing.status == PostStatus.CANCELED:
        return cycle, existing.id
    return None


def _find_slot(ctx: _Context, plan: _AccountPlan, video: Video) -> datetime | None:
    others = [t for account_id, t in ctx.video_times.get(video.id, []) if account_id != plan.account.id]
    source_at = ensure_aware(video.source_published_at)
    if source_at is not None:
        others.append(source_at)
    for seg in plan.segments:
        if seg.occupied:
            continue
        t = max(seg.start + _jitter(video.id, plan.account.id, seg), ctx.earliest)
        t = _push(t, seg.end, plan.times, plan.min_gap, others, ctx.cross_gap)
        if t is None or t > ctx.latest:
            continue
        if plan.day_counts[t.astimezone(ctx.zone).date()] >= plan.cap:
            continue
        if (
            plan.platform == Platform.YOUTUBE
            and ctx.youtube_days[t.astimezone(YOUTUBE_QUOTA_TZ).date()] >= YOUTUBE_DAILY_UPLOAD_BUDGET
        ):
            continue
        return t
    return None


def _push(
    t: datetime,
    limit: datetime,
    account_times: list[datetime],
    min_gap: timedelta,
    video_times: list[datetime],
    cross_gap: timedelta,
) -> datetime | None:
    """Earliest time >= t and < limit that keeps both gaps, or None. Every conflict moves t
    strictly forward past that post, so the loop ends."""
    while t < limit:
        bump: datetime | None = None
        for times, gap in ((account_times, min_gap), (video_times, cross_gap)):
            for p in times:
                if abs(t - p) < gap and (bump is None or p + gap > bump):
                    bump = p + gap
        if bump is None:
            return t
        t = bump
    return None


def _jitter(video_id: int, account_id: int, seg: _Segment) -> timedelta:
    span = min(MAX_JITTER_MINUTES, int((seg.end - seg.start).total_seconds() // 60 // 4))
    if span <= 0:
        return timedelta(0)
    return timedelta(minutes=zlib.crc32(f"{video_id}:{account_id}".encode()) % (span + 1))


def _place(
    ctx: _Context, plan: _AccountPlan, video: Video, cycle: int, reuse_id: int | None, at: datetime
) -> None:
    session = ctx.session
    post = session.get(ScheduledPost, reuse_id) if reuse_id is not None else None
    if post is None:
        post = ScheduledPost(
            workspace_id=ctx.workspace.id,
            video_id=video.id,
            connected_account_id=plan.account.id,
            cycle=cycle,
            idempotency_key=f"{video.id}:{plan.account.id}:{cycle}",
        )
        session.add(post)
    post.platform = plan.platform
    post.scheduled_at = at
    post.status = PostStatus.SCHEDULED
    post.attempts = 0
    post.caption = None
    post.publish_state = {}
    post.platform_post_id = None
    post.permalink = None
    post.error = None
    post.published_at = None
    post.created_by = "scheduler"
    session.flush()
    queue_publish(session, post.id, at)
    ctx.created.append(post)
    row = _PostRow(post.id, video.id, plan.account.id, cycle, PostStatus.SCHEDULED, at)
    ctx.pair_posts[(video.id, plan.account.id)] = [
        p for p in ctx.pair_posts.get((video.id, plan.account.id), []) if p.id != post.id
    ] + [row]
    ctx.video_times[video.id].append((plan.account.id, at))
    plan.times.append(at)
    plan.day_counts[at.astimezone(ctx.zone).date()] += 1
    for seg in plan.segments:
        if seg.start <= at < seg.end:
            seg.occupied = True
    if plan.platform == Platform.YOUTUBE:
        ctx.youtube_days[at.astimezone(YOUTUBE_QUOTA_TZ).date()] += 1


# =========================================================================== helpers


def _warn_once(ctx: _Context, key: str, message: str, **data: Any) -> None:
    if key in ctx.warned:
        return
    ctx.warned.add(key)
    log_activity(ctx.session, ctx.workspace.id, ActivityLevel.WARNING, "publish", message, dedupe=key, **data)


def _post_time(status: Any, scheduled_at: datetime, published_at: datetime | None) -> datetime:
    if PostStatus(status) == PostStatus.PUBLISHED and published_at is not None:
        return ensure_aware(published_at)
    return ensure_aware(scheduled_at)


def _ts(dt: datetime | None) -> float:
    aware = ensure_aware(dt)
    return aware.timestamp() if aware is not None else 0.0


def _mmss(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"
