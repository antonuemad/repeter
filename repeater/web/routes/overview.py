"""Overview page: counts, the next 48 hours, what needs attention, disk usage, recent activity."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from repeater import backups
from repeater.engine.rules import rule_problems
from repeater.media import storage
from repeater.models import (
    ACTIVE_POST_STATUSES,
    PUBLISH_PLATFORMS,
    AccountStatus,
    ActivityLog,
    ConnectedAccount,
    ContentPlan,
    CreatorProfile,
    PlanTarget,
    PostStatus,
    RotationRule,
    ScheduledPost,
    Source,
    SourceKind,
    Video,
    VideoStatus,
)
from repeater.timeutil import utcnow
from repeater.web.auth import CurrentWorkspace
from repeater.web.deps import DB
from repeater.web.templating import render

log = logging.getLogger("repeater.web")
router = APIRouter()

UPCOMING_HOURS = 48
ATTENTION_DAYS = 14
LIST_LIMIT = 8


@router.get("/")
def overview(request: Request, db: DB, ctx: CurrentWorkspace) -> Response:
    ws_id = ctx.workspace.id
    now = utcnow()
    context: dict[str, Any] = {
        "nav": "overview",
        "counts": _counts(db, ws_id, now),
        "upcoming": _upcoming(db, ws_id, now),
        "attention_posts": _attention_posts(db, ws_id, now),
        "failed_videos": _failed_videos(db, ws_id),
        "failing_sources": _failing_sources(db, ws_id),
        "incomplete_rules": _incomplete_rules(db, ws_id),
        "reconnect_accounts": _reconnect_accounts(db, ws_id),
        "setup": _setup_steps(db, ws_id),
        "disk": _disk_usage(),
        # Only owners can act on the server's backup job.
        "backup_stale_hours": backups.stale_hours() if ctx.is_owner else None,
        "activity": _recent_activity(db, ws_id),
    }
    return render(request, "overview.html", context)


def _setup_steps(db: Session, ws_id: int) -> dict[str, Any]:
    """Prepare the newest person's plan, without requiring any publishing switches."""
    plan = db.scalar(
        select(ContentPlan).where(ContentPlan.workspace_id == ws_id).order_by(ContentPlan.id.desc()).limit(1)
    )
    profile = db.get(CreatorProfile, plan.profile_id) if plan else None
    edit_url = f"/plans/{plan.id}/setup/accounts" if plan else "/setup"
    library_url = f"/plans/{plan.id}/setup/content" if plan else "/setup"
    accounts = (
        _count(
            db,
            select(func.count())
            .select_from(ConnectedAccount)
            .join(PlanTarget, PlanTarget.account_id == ConnectedAccount.id)
            .where(
                PlanTarget.plan_id == (plan.id if plan else None),
                PlanTarget.workspace_id == ws_id,
                ConnectedAccount.workspace_id == ws_id,
                ConnectedAccount.profile_id == (plan.profile_id if plan else None),
                ConnectedAccount.platform.in_(PUBLISH_PLATFORMS),
                ConnectedAccount.status == AccountStatus.ACTIVE,
            ),
        )
        if plan
        else 0
    )
    videos = (
        _count(
            db,
            select(func.count())
            .select_from(Video)
            .where(
                Video.workspace_id == ws_id,
                Video.profile_id == (plan.profile_id if plan else None),
                Video.status == VideoStatus.READY,
            ),
        )
        if plan
        else 0
    )
    steps = {"person": plan is not None, "accounts": accounts > 0, "content": videos > 0}
    done = all(steps.values())
    links = {
        "person": edit_url,
        "accounts": edit_url,
        "content": library_url,
    }
    next_url = f"/plans/{plan.id}/setup/preview" if plan else edit_url
    for key, href in links.items():
        if not steps[key]:
            next_url = href
            break
    return {
        **steps,
        "done": done,
        "name": profile.name if profile else None,
        "links": links,
        "next_url": next_url,
    }


def _count(db: Session, stmt: Select) -> int:
    return int(db.execute(stmt).scalar_one() or 0)


def _counts(db: Session, ws_id: int, now: datetime) -> dict[str, int]:
    videos = dict(
        db.execute(
            select(Video.status, func.count()).where(Video.workspace_id == ws_id).group_by(Video.status)
        ).all()
    )
    return {
        "ready": int(videos.get(VideoStatus.READY, 0)),
        "processing": int(videos.get(VideoStatus.IMPORTING, 0) + videos.get(VideoStatus.PROCESSING, 0)),
        "failed": int(videos.get(VideoStatus.FAILED, 0)),
        "accounts": _count(
            db,
            select(func.count())
            .select_from(ConnectedAccount)
            .where(ConnectedAccount.workspace_id == ws_id, ConnectedAccount.status == AccountStatus.ACTIVE),
        ),
        "sources": _count(
            db,
            select(func.count())
            .select_from(Source)
            .where(Source.workspace_id == ws_id, Source.enabled.is_(True), Source.kind != SourceKind.MANUAL),
        ),
        "upcoming": _count(
            db,
            select(func.count())
            .select_from(ScheduledPost)
            .where(
                ScheduledPost.workspace_id == ws_id,
                ScheduledPost.status.in_(ACTIVE_POST_STATUSES),
                ScheduledPost.scheduled_at <= now + timedelta(hours=UPCOMING_HOURS),
            ),
        ),
        "published_week": _count(
            db,
            select(func.count())
            .select_from(ScheduledPost)
            .where(
                ScheduledPost.workspace_id == ws_id,
                ScheduledPost.status == PostStatus.PUBLISHED,
                ScheduledPost.published_at >= now - timedelta(days=7),
            ),
        ),
    }


def _upcoming(db: Session, ws_id: int, now: datetime) -> list[ScheduledPost]:
    return list(
        db.execute(
            select(ScheduledPost)
            .options(selectinload(ScheduledPost.video), selectinload(ScheduledPost.connected_account))
            .where(
                ScheduledPost.workspace_id == ws_id,
                ScheduledPost.status.in_(ACTIVE_POST_STATUSES),
                ScheduledPost.scheduled_at <= now + timedelta(hours=UPCOMING_HOURS),
            )
            .order_by(ScheduledPost.scheduled_at, ScheduledPost.id)
            .limit(LIST_LIMIT)
        ).scalars()
    )


def _attention_posts(db: Session, ws_id: int, now: datetime) -> list[ScheduledPost]:
    return list(
        db.execute(
            select(ScheduledPost)
            .options(selectinload(ScheduledPost.video), selectinload(ScheduledPost.connected_account))
            .where(
                ScheduledPost.workspace_id == ws_id,
                ScheduledPost.status == PostStatus.NEEDS_ATTENTION,
                ScheduledPost.updated_at >= now - timedelta(days=ATTENTION_DAYS),
            )
            .order_by(ScheduledPost.updated_at.desc())
            .limit(LIST_LIMIT)
        ).scalars()
    )


def _failed_videos(db: Session, ws_id: int) -> list[Video]:
    return list(
        db.execute(
            select(Video)
            .where(Video.workspace_id == ws_id, Video.status == VideoStatus.FAILED)
            .order_by(Video.updated_at.desc())
            .limit(LIST_LIMIT)
        ).scalars()
    )


def _failing_sources(db: Session, ws_id: int) -> list[Source]:
    return list(
        db.execute(
            select(Source)
            .where(
                Source.workspace_id == ws_id, Source.kind != SourceKind.MANUAL, Source.last_error.isnot(None)
            )
            .order_by(Source.id)
            .limit(LIST_LIMIT)
        ).scalars()
    )


def _incomplete_rules(db: Session, ws_id: int) -> list[tuple[RotationRule, list[str]]]:
    rules = db.execute(
        select(RotationRule)
        .join(ConnectedAccount, ConnectedAccount.id == RotationRule.connected_account_id)
        .options(selectinload(RotationRule.connected_account))
        .where(
            RotationRule.workspace_id == ws_id,
            RotationRule.enabled.is_(True),
            ConnectedAccount.status == AccountStatus.ACTIVE,
        )
        .order_by(RotationRule.id)
    ).scalars()
    return [(r, problems) for r in rules if (problems := rule_problems(r))]


def _reconnect_accounts(db: Session, ws_id: int) -> list[ConnectedAccount]:
    return list(
        db.execute(
            select(ConnectedAccount)
            .where(
                ConnectedAccount.workspace_id == ws_id,
                ConnectedAccount.status == AccountStatus.NEEDS_RECONNECT,
            )
            .order_by(ConnectedAccount.id)
        ).scalars()
    )


def _disk_usage() -> dict[str, int | float] | None:
    try:
        total, used, free = storage.disk_usage()
    except OSError as exc:
        log.warning("disk usage unavailable: %s", type(exc).__name__)
        return None
    percent = round(used * 100 / total, 1) if total else 0.0
    return {"total": total, "used": used, "free": free, "percent": percent}


def _recent_activity(db: Session, ws_id: int) -> list[ActivityLog]:
    return list(
        db.execute(
            select(ActivityLog)
            .where(ActivityLog.workspace_id == ws_id)
            .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
            .limit(10)
        ).scalars()
    )
