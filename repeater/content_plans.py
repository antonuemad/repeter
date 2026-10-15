"""Read-only draft simulation. No queue, provider or publication calls belong here."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from repeater.engine.scheduler import limits_problem
from repeater.models import (
    AccountStatus,
    ConnectedAccount,
    ContentPlan,
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
                Video.encoded_path.isnot(None),
            )
            .order_by(*order)
            .limit(1001)
        )
    )
    truncated = len(videos) > 1000
    videos = videos[:1000]
    used = set(
        db.execute(
            select(ScheduledPost.video_id, ScheduledPost.connected_account_id).where(
                ScheduledPost.workspace_id == plan.workspace_id,
                ScheduledPost.connected_account_id.in_([a.id for a in accounts]),
                ScheduledPost.video_id.in_([v.id for v in videos]),
                ScheduledPost.status.in_(
                    [
                        PostStatus.PUBLISHED,
                        PostStatus.SCHEDULED,
                        PostStatus.UPLOADING,
                        PostStatus.PROCESSING,
                        PostStatus.FAILED,
                    ]
                ),
            )
        ).all()
    )
    queues: dict[int, list[Video]] = {}
    excluded = 0
    for account in accounts:
        choices = []
        if account.status == AccountStatus.ACTIVE and account.profile_id == plan.profile_id:
            for video in videos:
                if (
                    (video.id, account.id) in used
                    or video.source_account_id == account.id
                    or account.platform.value in (video.overrides or {}).get("exclude_platforms", [])
                    or limits_problem(video, account.platform)
                ):
                    excluded += 1
                    continue
                choices.append(video)
        queues[account.id] = choices
    rows = []
    filled = 0
    for index, at in enumerate(slots(plan, now=now)):
        entries = []
        for account in accounts:
            queue = queues[account.id]
            video = queue[index] if index < len(queue) else None
            filled += int(video is not None)
            entries.append(PreviewEntry(account, video))
        rows.append(PreviewSlot(at, entries))
    return {
        "rows": rows,
        "accounts": accounts,
        "ready": len(videos),
        "filled": filled,
        "empty": len(rows) * len(accounts) - filled,
        "excluded": excluded,
        "truncated": truncated,
    }
