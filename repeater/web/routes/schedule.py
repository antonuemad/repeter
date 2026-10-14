"""Schedule: the next 48 hours grouped by local day, and the last 7 days with permalinks."""

from __future__ import annotations

from datetime import date, timedelta
from itertools import groupby
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from repeater.engine import actions
from repeater.models import ACTIVE_POST_STATUSES, PostStatus, ScheduledPost
from repeater.timeutil import ensure_aware, tz, utcnow
from repeater.web import messages
from repeater.web.auth import CurrentWorkspace, WorkspaceContext
from repeater.web.deps import DB, Form, form_bool, safe_next
from repeater.web.templating import flash, render

router = APIRouter()

UPCOMING_HOURS = 48
HISTORY_DAYS = 7
HISTORY_LIMIT = 300
FINISHED_STATUSES = (
    PostStatus.PUBLISHED,
    PostStatus.NEEDS_ATTENTION,
    PostStatus.SKIPPED,
    PostStatus.CANCELED,
)
# Statuses where the user may still skip or push the post out now (engine.actions.NOT_STARTED).
ACTIONABLE = frozenset({PostStatus.SCHEDULED, PostStatus.FAILED, PostStatus.NEEDS_ATTENTION})


@router.get("/schedule")
def schedule(request: Request, db: DB, ctx: CurrentWorkspace) -> Response:
    zone = tz(ctx.workspace.timezone)
    now = utcnow()
    upcoming = list(
        db.execute(
            select(ScheduledPost)
            .options(selectinload(ScheduledPost.video), selectinload(ScheduledPost.connected_account))
            .where(
                ScheduledPost.workspace_id == ctx.workspace.id,
                ScheduledPost.status.in_(ACTIVE_POST_STATUSES),
                ScheduledPost.scheduled_at <= now + timedelta(hours=UPCOMING_HOURS),
            )
            .order_by(ScheduledPost.scheduled_at, ScheduledPost.id)
        ).scalars()
    )
    days: list[tuple[date, list[ScheduledPost]]] = [
        (day, list(posts))
        for day, posts in groupby(
            upcoming, key=lambda p: ensure_aware(p.scheduled_at).astimezone(zone).date()
        )
    ]
    since = now - timedelta(days=HISTORY_DAYS)
    history = list(
        db.execute(
            select(ScheduledPost)
            .options(selectinload(ScheduledPost.video), selectinload(ScheduledPost.connected_account))
            .where(
                ScheduledPost.workspace_id == ctx.workspace.id,
                ScheduledPost.status.in_(FINISHED_STATUSES),
                func.coalesce(ScheduledPost.published_at, ScheduledPost.updated_at) >= since,
            )
            .order_by(func.coalesce(ScheduledPost.published_at, ScheduledPost.updated_at).desc())
            .limit(HISTORY_LIMIT)
        ).scalars()
    )
    context: dict[str, Any] = {
        "nav": "schedule",
        "days": days,
        "today": now.astimezone(zone).date(),
        "now": now,
        "history": history,
        "actionable": ACTIONABLE,
        "upcoming_hours": UPCOMING_HOURS,
        "history_days": HISTORY_DAYS,
    }
    return render(request, "schedule.html", context)


def _get_post(db: Session, ctx: WorkspaceContext, post_id: int) -> ScheduledPost:
    post = db.get(ScheduledPost, post_id)
    if post is None or post.workspace_id != ctx.workspace.id:
        raise HTTPException(status_code=404)
    return post


def _redirect(form: Any) -> RedirectResponse:
    raw = form.get("next")
    return RedirectResponse(safe_next(raw if isinstance(raw, str) else None, "/schedule"), status_code=303)


@router.post("/schedule/{post_id}/skip")
def skip(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, post_id: int) -> Response:
    post = _get_post(db, ctx, post_id)
    try:
        actions.skip_post(db, post, actor=ctx.actor)
        db.commit()
    except ValueError as exc:
        db.rollback()
        messages.flash_error(request, exc)
    else:
        flash(request, "success", "schedule.skipped")
    return _redirect(form)


def may_be_live(post: ScheduledPost) -> bool:
    """A post that needs attention after bytes may have reached the platform (it kept a platform
    post id or an upload state): publishing it again can create a second copy."""
    return post.status == PostStatus.NEEDS_ATTENTION and bool(post.platform_post_id or post.publish_state)


@router.get("/schedule/{post_id}/publish-now")
def confirm_publish_now(
    request: Request, db: DB, ctx: CurrentWorkspace, post_id: int, next: str = ""
) -> Response:  # noqa: A002
    """Asks before publishing a post again whose upload may already be live."""
    post = _get_post(db, ctx, post_id)
    if post.status not in ACTIONABLE:
        return RedirectResponse(safe_next(next, "/schedule"), status_code=303)
    context = {
        "nav": "schedule",
        "post": post,
        "account": post.connected_account,
        "next": safe_next(next, "/schedule"),
    }
    return render(request, "schedule_republish.html", context)


@router.post("/schedule/{post_id}/publish-now")
def publish_now(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, post_id: int) -> Response:
    post = _get_post(db, ctx, post_id)
    if may_be_live(post) and not form_bool(form, "confirm_duplicate"):
        flash(request, "error", "schedule.confirm_required")
        return _redirect(form)
    try:
        actions.publish_now(db, post, actor=ctx.actor)
        db.commit()
    except ValueError as exc:
        db.rollback()
        messages.flash_error(request, exc)
    else:
        flash(request, "success", "schedule.publishing_now")
    return _redirect(form)
