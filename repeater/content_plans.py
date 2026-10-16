"""Deterministic per-person rotation, simulated without queue or provider calls.

Each page has its own least-recently-used queue. Unused videos come first, then the
oldest actual/simulated use. A repeat needs a full N * 24 hours since that use. Draft
choices never become publication history; reopening a preview cannot consume content.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from heapq import heapify, heappop, heappush
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from repeater.engine.publisher import reached_platform
from repeater.media.content import problem
from repeater.models import (
    ACTIVE_POST_STATUSES,
    AccountStatus,
    ConnectedAccount,
    ContentPlan,
    PlanSlotOverride,
    PlanTarget,
    PostStatus,
    ScheduledPost,
    Video,
    VideoStatus,
)
from repeater.timeutil import ensure_aware, utcnow


@dataclass(frozen=True)
class PreviewEntry:
    account: ConnectedAccount
    video: Video | None
    previous_at: datetime | None = None
    previous_is_preview: bool = False
    reason: str | None = None
    available_at: datetime | None = None
    original_at: datetime | None = None
    customized: bool = False

    @property
    def repeated(self) -> bool:
        return self.previous_at is not None


@dataclass(frozen=True)
class PreviewSlot:
    at: datetime
    entries: list[PreviewEntry]

    @property
    def first_video(self) -> Video | None:
        return next((entry.video for entry in self.entries if entry.video is not None), None)

    @property
    def filled(self) -> int:
        return sum(entry.video is not None for entry in self.entries)


def targets(db: Session, plan: ContentPlan) -> list[ConnectedAccount]:
    return list(
        db.scalars(
            select(ConnectedAccount)
            .join(PlanTarget, PlanTarget.account_id == ConnectedAccount.id)
            .where(
                PlanTarget.plan_id == plan.id,
                PlanTarget.workspace_id == plan.workspace_id,
                ConnectedAccount.workspace_id == plan.workspace_id,
            )
            .order_by(ConnectedAccount.platform, ConnectedAccount.display_name, ConnectedAccount.id)
        )
    )


def slots(plan: ContentPlan, *, now: datetime) -> list[datetime]:
    """30 local calendar days. Skip nonexistent DST times; use the first occurrence of a fold."""
    zone = ZoneInfo(plan.timezone)
    first = max(plan.start_date, now.astimezone(zone).date())
    result = []
    for offset in range(30):
        day = first + timedelta(days=offset)
        if day.weekday() not in plan.weekdays:
            continue
        for value in sorted(plan.times):
            naive = datetime.combine(day, time.fromisoformat(value))
            local = naive.replace(tzinfo=zone, fold=0)
            at = local.astimezone(UTC)
            if at.astimezone(zone).replace(tzinfo=None) != naive or at <= now:
                continue
            result.append(at)
    return result


def _history(db, plan, accounts, videos):
    """Confirmed publication timestamps and unsafe pairs, in one tenant-scoped read.

    Pending/retrying/uncertain work cannot be treated as a successful old publication
    or silently recycled. In particular canceling a possibly-live upload is not a reset.
    """
    latest: dict[tuple[int, int], datetime] = {}
    blocked: set[tuple[int, int]] = set()
    if not accounts or not videos:
        return latest, blocked
    rows = db.execute(
        select(
            ScheduledPost.video_id,
            ScheduledPost.connected_account_id,
            ScheduledPost.status,
            ScheduledPost.scheduled_at,
            ScheduledPost.published_at,
            ScheduledPost.platform_post_id,
            ScheduledPost.publish_state,
        ).where(
            ScheduledPost.workspace_id == plan.workspace_id,
            ScheduledPost.connected_account_id.in_([a.id for a in accounts]),
            ScheduledPost.video_id.in_([v.id for v in videos]),
        )
    )
    for row in rows:
        pair = (row.video_id, row.connected_account_id)
        if row.status == PostStatus.PUBLISHED:
            at = ensure_aware(row.published_at or row.scheduled_at)
            latest[pair] = max(at, latest.get(pair, at))
        elif (
            row.status in ACTIVE_POST_STATUSES
            or row.status == PostStatus.NEEDS_ATTENTION
            or reached_platform(
                row.platform_post_id, row.publish_state if isinstance(row.publish_state, dict) else {}
            )
        ):
            blocked.add(pair)
    return latest, blocked


def simulate(db: Session, plan: ContentPlan, *, now: datetime | None = None) -> dict:
    now = ensure_aware(now or utcnow())
    accounts = targets(db, plan)
    order = [Video.source_published_at.asc().nullslast(), Video.created_at.asc(), Video.id.asc()]
    if plan.ordering == "newest":
        order = [Video.source_published_at.desc().nullslast(), Video.created_at.desc(), Video.id.desc()]
    videos = list(
        db.scalars(
            select(Video)
            .where(
                Video.workspace_id == plan.workspace_id,
                Video.profile_id == plan.profile_id,
                Video.status == VideoStatus.READY,
                (Video.encoded_path.isnot(None) | (Video.media_kind == "text")),
            )
            .order_by(*order)
            .limit(1001)
        )
    )
    truncated = len(videos) > 1000
    videos = videos[:1000]
    latest, blocked = _history(db, plan, accounts, videos)
    repeating = plan.rotation_mode == "repeat"
    cooldown = timedelta(days=plan.repeat_after_days)
    queues: dict[int, list] = {}
    empty_reasons: dict[int, str] = {}
    excluded = 0
    blocked_count = 0
    incompatibilities = []
    for account in accounts:
        choices = []
        blocked_here = 0
        if account.status == AccountStatus.ACTIVE and account.profile_id == plan.profile_id:
            for rank, video in enumerate(videos):
                pair = (video.id, account.id)
                last = latest.get(pair)
                issue = problem(video, account)
                if issue:
                    incompatibilities.append({"video": video, "account": account, "reason": issue})
                if (
                    video.source_account_id == account.id
                    or account.platform.value in (video.overrides or {}).get("exclude_platforms", [])
                    or issue
                ):
                    excluded += 1
                    continue
                if pair in blocked:
                    blocked_here += 1
                    excluded += 1
                    continue
                if last is not None and not repeating:
                    excluded += 1
                    continue
                # Rank breaks ties using the saved library order; unused always wins.
                heappush(choices, (last.timestamp() if last else float("-inf"), rank, video, last, False))
            empty_reasons[account.id] = (
                "pending" if blocked_here else "exhausted" if choices else "no_eligible"
            )
        else:
            empty_reasons[account.id] = "account_unavailable"
        blocked_count += blocked_here
        queues[account.id] = choices
    rows = []
    filled = 0
    repeated = 0
    waiting = 0
    overrides = list(
        db.scalars(
            select(PlanSlotOverride).where(
                PlanSlotOverride.plan_id == plan.id, PlanSlotOverride.workspace_id == plan.workspace_id
            )
        )
    )
    edits = {(o.account_id, ensure_aware(o.slot_at)): o for o in overrides}
    events = []
    original_slots = slots(plan, now=now)
    seen = set()
    end = now.astimezone(ZoneInfo(plan.timezone)).date() + timedelta(days=30)
    for original in original_slots:
        for account in accounts:
            edit = edits.get((account.id, original))
            at = ensure_aware(edit.scheduled_at) if edit else original
            seen.add((account.id, original))
            if at > now:
                events.append((at, account.id, original, account, edit))
    # A moved slot must survive passing its original time, without becoming a new automatic slot.
    for edit in overrides:
        at, original = ensure_aware(edit.scheduled_at), ensure_aware(edit.slot_at)
        account = next((a for a in accounts if a.id == edit.account_id), None)
        local = original.astimezone(ZoneInfo(plan.timezone))
        if (
            account
            and (account.id, original) not in seen
            and now < at
            and at.astimezone(ZoneInfo(plan.timezone)).date() < end
            and local.date() >= plan.start_date
            and local.weekday() in plan.weekdays
            and local.strftime("%H:%M") in plan.times
            and original <= now
        ):
            events.append((at, account.id, original, account, edit))
    from itertools import groupby

    for at, group in groupby(sorted(events, key=lambda e: (e[0], e[1], e[2])), key=lambda e: e[0]):
        entries = []
        for _, _, original, account, edit in group:
            queue = queues[account.id]
            meta = {"original_at": original, "customized": edit is not None}
            if edit and edit.excluded:
                entries.append(PreviewEntry(account, None, reason="excluded", **meta))
                continue
            if not queue:
                entries.append(PreviewEntry(account, None, reason=empty_reasons[account.id], **meta))
                continue
            index = 0
            if edit and edit.replacement:
                index = next((i for i, choice in enumerate(queue) if choice[2].id == edit.video_id), -1)
                if index < 0:
                    entries.append(PreviewEntry(account, None, reason="replacement_unavailable", **meta))
                    continue
            _, rank, video, last, simulated = queue[index]
            if last is not None and at < last + cooldown:
                waiting += 1
                entries.append(
                    PreviewEntry(account, None, reason="cooldown", available_at=last + cooldown, **meta)
                )
                continue
            if index == 0:
                heappop(queue)
            else:
                queue.pop(index)
                heapify(queue)
            filled += 1
            repeated += int(last is not None)
            entries.append(
                PreviewEntry(account, video, previous_at=last, previous_is_preview=simulated, **meta)
            )
            if repeating:
                heappush(queue, (at.timestamp(), rank, video, at, True))
        rows.append(PreviewSlot(at, entries))
    return {
        "rows": rows,
        "accounts": accounts,
        "ready": len(videos),
        "filled": filled,
        "empty": sum(len(row.entries) for row in rows) - filled,
        "excluded": excluded,
        "blocked": blocked_count,
        "repeated": repeated,
        "waiting": waiting,
        "truncated": truncated,
        "incompatibilities": incompatibilities[:100],
        "incompatibilities_total": len(incompatibilities),
    }
