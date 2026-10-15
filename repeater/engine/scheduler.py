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
   it, edits the rule, or a day passes. Rules are processed in id order. (A rule that the web
   module's rules.rule_problems reports as incomplete is skipped with one warning.)
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
   a CANCELED post is reused (reset to scheduled) instead of creating a new row, unless it got
   as far as the platform (platform id or adapter state: an interrupted disconnect): then it blocks
   its cycle like a needs-attention post.
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
     - for YouTube, at most youtube_daily_budget() videos.insert calls per Pacific day across all
       workspaces (the project's quota resets at midnight Pacific time): 90% of
       settings.youtube_daily_upload_quota, using the durable insert ledger plus pending reservations
       (legacy posts without ledger entries retain a conservative attempt estimate).
   Posts that ended as needs-attention (or canceled) but got as far as the platform probably went
   live: they count as active posts for all of the above, at the time of their upload.
   The first segment that can take the video wins; videos that fit nowhere wait for a later run.
6. Each new or reused post gets a publish_post job at its scheduled_at.

Cost: one query per kind of data per run (videos, posts, duplicates, YouTube budget, warnings),
indexed in memory; the gap checks bisect sorted time lists, and a segment that cannot take any
video of the account (gap, cap, budget, time) is dropped, so a run costs O(posts + videos) plus
the work for the posts it creates, whatever the history length.
"""

from __future__ import annotations

import bisect
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Text, and_, case, func, literal, null, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, defer

from repeater.activity import log_activity
from repeater.config import get_settings
from repeater.engine import captions, rules
from repeater.engine.publisher import engine_state, queue_publish, reached_platform
from repeater.messages import Message
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
    YouTubeQuotaDay,
    YouTubeUploadStart,
)
from repeater.platforms.base import LIMITS
from repeater.timeutil import ensure_aware, tz, utcnow

PLAN_LEAD = timedelta(minutes=2)
MAX_JITTER_MINUTES = 10
SUSPEND_AFTER_FAILURES = 3
SUSPEND_LOOKBACK = timedelta(hours=24)
# The planner uses this share of the project's videos.insert quota (settings.youtube_daily_upload_quota,
# 100 by default) and keeps the rest as a margin for manual uploads and retries.
YOUTUBE_BUDGET_FRACTION = 0.9
YOUTUBE_QUOTA_TZ = ZoneInfo("America/Los_Angeles")
COUNTED_STATUSES = (*ACTIVE_POST_STATUSES, PostStatus.PUBLISHED)
# Ended without a confirmed publish, but possibly live: counted when the post reached the platform.
LIVE_HINT_STATUSES = (PostStatus.NEEDS_ATTENTION, PostStatus.CANCELED)
_ADVISORY_LOCK_NAMESPACE = 727_001
_ZERO = timedelta(0)


def youtube_daily_budget() -> int:
    """videos.insert calls the planner may use per Pacific day, across all workspaces."""
    return max(0, int(get_settings().youtube_daily_upload_quota * YOUTUBE_BUDGET_FRACTION))


@dataclass(frozen=True)
class _PostRow:
    id: int
    video_id: int
    account_id: int
    cycle: int
    status: PostStatus
    at: datetime  # published_at for published posts, the upload time of a possibly-live post
    # Occupies time for gaps, caps and quotas: active, published, or possibly live.
    counted: bool
    # Got as far as the platform (platform id or adapter state), whatever its status says now.
    reached: bool = False


@dataclass
class _Segment:
    start: datetime
    end: datetime
    occupied: bool = False
    # No video can be placed here any more (account-level gap, cap, budget or time); only grows.
    dead: bool = False


@dataclass
class _AccountPlan:
    rule: RotationRule
    account: ConnectedAccount
    platform: Platform
    cap: int
    min_gap: timedelta
    times: list[datetime]  # sorted
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
    account_times: dict[int, list[datetime]]  # counted posts per account, sorted
    youtube_days: Counter[date]
    youtube_budget: int
    warned: set[str]
    # Where each video's content already exists, from its duplicates (see duplicate_origins).
    dup_platforms: dict[int, set[Platform]] = field(default_factory=dict)
    dup_accounts: dict[int, set[int]] = field(default_factory=dict)
    created: list[ScheduledPost] = field(default_factory=list)
    limit_notes: dict[tuple[int, Platform], str | None] = field(default_factory=dict)


def plan_workspace(session: Session, workspace_id: int, now: datetime | None = None) -> list[ScheduledPost]:
    """Create (or revive) scheduled posts for the workspace's horizon. Returns them."""
    if not get_settings().publishing_enabled:
        return []
    now = ensure_aware(now) or utcnow()
    workspace = session.get(Workspace, workspace_id)
    if workspace is None or workspace.paused:
        return []
    lock_workspace(session, workspace.id)
    pairs = _active_rules(session, workspace.id)
    if not pairs:
        return []
    if any(account.platform == Platform.YOUTUBE for _, account in pairs):
        # All channels share one Google project, including plans from different workspaces.
        session.execute(select(func.pg_advisory_xact_lock(727002, 0)))
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
            Message(
                "publish.scheduled",
                f"Scheduled {len(ctx.created)} new post{'s' if len(ctx.created) != 1 else ''}.",
                {"p0": len(ctx.created), "p1": "s" if len(ctx.created) != 1 else ""},
            ),
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
            ConnectedAccount.profile_id.is_(None),
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
            select(Video)
            .where(
                Video.workspace_id == workspace.id,
                Video.profile_id.is_(None),
                Video.status == VideoStatus.READY,
                Video.encoded_path.isnot(None),
            )
            .options(defer(Video.phash))
        ).scalars()
    )
    posts = _load_posts(session, workspace.id)
    pair_posts: dict[tuple[int, int], list[_PostRow]] = defaultdict(list)
    video_times: dict[int, list[tuple[int, datetime]]] = defaultdict(list)
    account_times: dict[int, list[datetime]] = defaultdict(list)
    publishes: Counter[int] = Counter()
    for p in posts:
        pair_posts[(p.video_id, p.account_id)].append(p)
        if p.counted:
            video_times[p.video_id].append((p.account_id, p.at))
            account_times[p.account_id].append(p.at)
        if p.status == PostStatus.PUBLISHED:
            publishes[p.video_id] += 1
    for times in account_times.values():
        times.sort()
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
        account_times=dict(account_times),
        youtube_days=_youtube_days(session, earliest - timedelta(days=1), latest + timedelta(days=1)),
        youtube_budget=youtube_daily_budget(),
        warned=_warned_keys(session, workspace.id),
        dup_platforms=dup_platforms,
        dup_accounts=dup_accounts,
    )


def _load_posts(session: Session, workspace_id: int) -> list[_PostRow]:
    """Every post of the workspace in one query (light columns only). publish_state is read only
    for the statuses that may hide a live upload."""
    hint_state = case(
        (ScheduledPost.status.in_(LIVE_HINT_STATUSES), ScheduledPost.publish_state), else_=null()
    )
    rows = session.execute(
        select(
            ScheduledPost.id,
            ScheduledPost.video_id,
            ScheduledPost.connected_account_id,
            ScheduledPost.cycle,
            ScheduledPost.status,
            ScheduledPost.scheduled_at,
            ScheduledPost.published_at,
            ScheduledPost.platform_post_id,
            hint_state.label("hint_state"),
        ).where(ScheduledPost.workspace_id == workspace_id)
    ).all()
    out: list[_PostRow] = []
    for r in rows:
        status = PostStatus(r.status)
        reached = False
        at = _post_time(status, r.scheduled_at, r.published_at)
        if status in LIVE_HINT_STATUSES:
            state = r.hint_state if isinstance(r.hint_state, dict) else {}
            reached = reached_platform(r.platform_post_id, state)
            uploaded = _uploaded_at(state)
            if uploaded is not None:
                at = uploaded
        counted = status in COUNTED_STATUSES or reached
        out.append(
            _PostRow(
                id=r.id,
                video_id=r.video_id,
                account_id=r.connected_account_id,
                cycle=int(r.cycle or 0),
                status=status,
                at=at,
                counted=counted,
                reached=reached,
            )
        )
    return out


def _uploaded_at(state: dict[str, Any]) -> datetime | None:
    raw = engine_state(state).get("processing_since")
    try:
        return ensure_aware(datetime.fromisoformat(raw)) if raw else None
    except (TypeError, ValueError):
        return None


def _youtube_days(session: Session, start: datetime, end: datetime) -> Counter[date]:
    """Actual insert reservations plus pending work, shared by every workspace.

    Old posts without ledger entries retain the conservative attempts estimate. New entries
    count the actual HTTP starts even after manual retry, needs-attention, or post deletion.
    """
    first, last = start.astimezone(YOUTUBE_QUOTA_TZ).date(), end.astimezone(YOUTUBE_QUOTA_TZ).date()
    days: Counter[date] = Counter(
        dict(
            session.execute(
                select(YouTubeUploadStart.day, func.count())
                .where(YouTubeUploadStart.day >= first, YouTubeUploadStart.day <= last)
                .group_by(YouTubeUploadStart.day)
            ).all()
        )
    )
    at = func.coalesce(ScheduledPost.published_at, ScheduledPost.scheduled_at)
    # The SQL twin of publisher.reached_platform: a platform id, adapter state, or the engine's
    # own record of an upload (processing_since) or of a possibly live video (may_be_live).
    adapter_keys = ScheduledPost.publish_state.op("-", return_type=JSONB)(literal("_engine", type_=Text))
    engine = ScheduledPost.publish_state["_engine"]
    possibly_live = and_(
        ScheduledPost.status.in_(LIVE_HINT_STATUSES),
        or_(
            ScheduledPost.platform_post_id.isnot(None),
            adapter_keys != {},
            engine.has_key("processing_since"),
            engine.has_key("may_be_live"),
        ),
    )
    rows = session.execute(
        select(
            at,
            ScheduledPost.attempts,
            select(YouTubeUploadStart.id).where(YouTubeUploadStart.post_id == ScheduledPost.id).exists(),
        ).where(
            ScheduledPost.platform == Platform.YOUTUBE,
            or_(
                ScheduledPost.status.in_((PostStatus.SCHEDULED, PostStatus.FAILED)),
                and_(
                    or_(ScheduledPost.status.in_(COUNTED_STATUSES), possibly_live),
                    ~select(YouTubeUploadStart.id)
                    .where(YouTubeUploadStart.post_id == ScheduledPost.id)
                    .exists(),
                ),
            ),
            at >= start,
            at <= end,
        )
    ).all()
    for when, attempts, recorded in rows:
        aware = when if when.tzinfo else when.replace(tzinfo=UTC)
        days[aware.astimezone(YOUTUBE_QUOTA_TZ).date()] += 1 if recorded else 1 + max(int(attempts or 0), 0)
    for day in session.scalars(
        select(YouTubeQuotaDay.day).where(
            YouTubeQuotaDay.day >= first, YouTubeQuotaDay.day <= last, YouTubeQuotaDay.exhausted.is_(True)
        )
    ):
        days[day] = max(days[day], youtube_daily_budget())
    return days


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
        needed = " and ".join(rules.PROBLEM_MESSAGES.get(code, code) for code in problems)
        _warn_once(
            ctx,
            f"rule_not_ready:{rule.id}:{int(_ts(rule.updated_at))}",
            Message(
                "publish.rule_incomplete",
                f"Nothing is scheduled for {captions.platform_name(platform)} ({account.display_name}) until "
                f"you {needed} in its posting settings.",
                {
                    "p0": captions.platform_name(platform),
                    "p1": account.display_name,
                    "p2": [
                        Message("rule.problem." + code, rules.PROBLEM_MESSAGES.get(code, code))
                        for code in problems
                    ],
                },
            ),
            account_id=account.id,
            rule_id=rule.id,
        )
        return
    failing = _suspended_by(ctx, rule, account)
    if failing is not None:
        _warn_once(
            ctx,
            f"suspended:{account.id}:{failing}",
            Message(
                "publish.suspended",
                f"Scheduling for {captions.platform_name(platform)} ({account.display_name}) is on hold: the "
                f"last {SUSPEND_AFTER_FAILURES} posts needed attention. Fix the problem shown on those posts "
                "and publish one again, or edit the account's rule, to resume.",
                {
                    "p0": captions.platform_name(platform),
                    "p1": account.display_name,
                    "p2": SUSPEND_AFTER_FAILURES,
                },
            ),
            account_id=account.id,
        )
        return
    plan = _account_plan(ctx, rule, account, platform)
    if plan is None:
        return
    for video in ctx.videos:
        if not any(not s.occupied and not s.dead for s in plan.segments):
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
    times = list(ctx.account_times.get(account.id, []))  # sorted
    # Only days that overlap a segment can matter for the daily cap.
    near = times[
        bisect.bisect_left(times, ctx.earliest - timedelta(days=2)) : bisect.bisect_right(
            times, ctx.latest + timedelta(days=2)
        )
    ]
    plan = _AccountPlan(
        rule=rule,
        account=account,
        platform=platform,
        cap=cap,
        min_gap=timedelta(minutes=max(int(rule.min_gap_minutes or 0), 0)),
        times=times,
        day_counts=Counter(t.astimezone(ctx.zone).date() for t in near),
        segments=segments,
    )
    for seg in segments:
        i = bisect.bisect_left(times, seg.start)
        seg.occupied = i < len(times) and times[i] < seg.end
    _drop_dead_segments(ctx, plan)
    return plan


def _drop_dead_segments(ctx: _Context, plan: _AccountPlan) -> None:
    """Mark segments that no video can take any more, from the account's own constraints only
    (never wrongly: jitter and the video's cross-platform gap can only push a slot later). Every
    later placement only tightens these constraints, so a dead segment stays dead."""
    for seg in plan.segments:
        if seg.occupied or seg.dead:
            continue
        t = _push(max(seg.start, ctx.earliest), seg.end, plan.times, plan.min_gap, [], _ZERO)
        if t is None or t > ctx.latest:
            seg.dead = True
            continue
        last = min(seg.end, ctx.latest) - timedelta(microseconds=1)
        if _days_full(plan.day_counts, plan.cap, ctx.zone, t, last) or (
            plan.platform == Platform.YOUTUBE
            and _days_full(ctx.youtube_days, ctx.youtube_budget, YOUTUBE_QUOTA_TZ, t, last)
        ):
            seg.dead = True


def _days_full(counts: Counter[date], limit: int, zone: ZoneInfo, first: datetime, last: datetime) -> bool:
    """True when every calendar day (in `zone`) between the two instants has reached `limit`."""
    day = first.astimezone(zone).date()
    end = last.astimezone(zone).date()
    while day <= end:
        if counts[day] < limit:
            return False
        day += timedelta(days=1)
    return True


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
                Message(
                    "publish.limit",
                    f"'{captions.video_label(video)}' will not be published to "
                    f"{captions.platform_name(plan.platform)}: {ctx.limit_notes[key]}.",
                    {
                        "p0": captions.video_label(video),
                        "p1": captions.platform_name(plan.platform),
                        "p2": ctx.limit_notes[key],
                    },
                ),
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
    if existing.status == PostStatus.CANCELED and not existing.reached:
        return cycle, existing.id
    return None


def _find_slot(ctx: _Context, plan: _AccountPlan, video: Video) -> datetime | None:
    others = sorted(t for account_id, t in ctx.video_times.get(video.id, []) if account_id != plan.account.id)
    source_at = ensure_aware(video.source_published_at)
    if source_at is not None:
        bisect.insort(others, source_at)
    for seg in plan.segments:
        if seg.occupied or seg.dead:
            continue
        t = max(seg.start + _jitter(video.id, plan.account.id, seg), ctx.earliest)
        t = _push(t, seg.end, plan.times, plan.min_gap, others, ctx.cross_gap)
        if t is None or t > ctx.latest:
            continue
        if plan.day_counts[t.astimezone(ctx.zone).date()] >= plan.cap:
            continue
        if (
            plan.platform == Platform.YOUTUBE
            and ctx.youtube_days[t.astimezone(YOUTUBE_QUOTA_TZ).date()] >= ctx.youtube_budget
        ):
            continue
        return t
    return None


def _bump(times: list[datetime], t: datetime, gap: timedelta) -> datetime | None:
    """When `t` is closer than `gap` to a time in the sorted list: the end of the last such gap."""
    if gap <= _ZERO or not times:
        return None
    lo = bisect.bisect_right(times, t - gap)  # first time after t - gap
    hi = bisect.bisect_left(times, t + gap)  # first time at or after t + gap
    return times[hi - 1] + gap if lo < hi else None


def _push(
    t: datetime,
    limit: datetime,
    account_times: list[datetime],
    min_gap: timedelta,
    video_times: list[datetime],
    cross_gap: timedelta,
) -> datetime | None:
    """Earliest time >= t and < limit that keeps both gaps, or None. Both lists are sorted. Every
    conflict moves t strictly forward past that post, so the loop ends."""
    while t < limit:
        bumps = [
            b for b in (_bump(account_times, t, min_gap), _bump(video_times, t, cross_gap)) if b is not None
        ]
        if not bumps:
            return t
        t = max(bumps)
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
    row = _PostRow(post.id, video.id, plan.account.id, cycle, PostStatus.SCHEDULED, at, counted=True)
    ctx.pair_posts[(video.id, plan.account.id)] = [
        p for p in ctx.pair_posts.get((video.id, plan.account.id), []) if p.id != post.id
    ] + [row]
    ctx.video_times[video.id].append((plan.account.id, at))
    bisect.insort(plan.times, at)
    plan.day_counts[at.astimezone(ctx.zone).date()] += 1
    for seg in plan.segments:
        if seg.start <= at < seg.end:
            seg.occupied = True
    if plan.platform == Platform.YOUTUBE:
        ctx.youtube_days[at.astimezone(YOUTUBE_QUOTA_TZ).date()] += 1
    _drop_dead_segments(ctx, plan)


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
