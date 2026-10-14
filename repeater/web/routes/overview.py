"""Overview page: counts, the next 48 hours, what needs attention, disk usage, recent activity."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from repeater.engine.rules import rule_problems
from repeater.media import storage
from repeater.models import (
    ACTIVE_POST_STATUSES,
    PUBLISH_PLATFORMS,
    AccountStatus,
    ActivityLog,
    ConnectedAccount,
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
        "activity": _recent_activity(db, ws_id),
    }
    return render(request, "overview.html", context)


def _setup_steps(db: Session, ws_id: int) -> dict[str, bool]:
    """The first-run checklist: connect an account, bring in videos, review a rule and turn it on."""
    accounts = _count(
        db,
        select(func.count())
        .select_from(ConnectedAccount)
        .where(
            ConnectedAccount.workspace_id == ws_id,
            ConnectedAccount.platform.in_(PUBLISH_PLATFORMS),
            ConnectedAccount.status != AccountStatus.DISABLED,
        ),
    )
    videos = _count(
        db,
        select(func.count())
        .select_from(Video)
        .where(Video.workspace_id == ws_id, Video.status != VideoStatus.ARCHIVED),
    )
    sources = _count(
        db,
        select(func.count())
        .select_from(Source)
        .where(Source.workspace_id == ws_id, Source.enabled.is_(True), Source.kind != SourceKind.MANUAL),
    )
    rules = _count(
        db,
        select(func.count())
        .select_from(RotationRule)
        .join(ConnectedAccount, ConnectedAccount.id == RotationRule.connected_account_id)
        .where(
            RotationRule.workspace_id == ws_id,
            RotationRule.enabled.is_(True),
            ConnectedAccount.status == AccountStatus.ACTIVE,
        ),
    )
    steps = {"accounts": accounts > 0, "content": videos > 0 or sources > 0, "rules": rules > 0}
    steps["done"] = all(steps.values())
    return steps


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
        .where(RotationRule.workspace_id == ws_id, ConnectedAccount.status == AccountStatus.ACTIVE)
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
