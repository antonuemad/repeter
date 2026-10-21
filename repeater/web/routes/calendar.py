"""Unified person previews and actual reservations; editing never queues a publication."""

from datetime import UTC, datetime, timedelta
from itertools import groupby
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from repeater.activity import log_activity
from repeater.content_plans import simulate, targets
from repeater.engine.people import cancel_pending
from repeater.engine.publisher import reached_platform
from repeater.media.content import problem
from repeater.models import (
    ActivityLevel,
    ContentPlan,
    CreatorProfile,
    PlanSlotOverride,
    PostStatus,
    ScheduledPost,
    Video,
    VideoStatus,
)
from repeater.timeutil import ensure_aware, utcnow
from repeater.web.auth import CurrentWorkspace
from repeater.web.deps import DB, Form, FormError, form_int
from repeater.web.journey import remember_person
from repeater.web.routes.plans import get_plan
from repeater.web.schedule_view import empty_day
from repeater.web.templating import flash, render

router = APIRouter()


def original_time(value):
    try:
        at = datetime.fromisoformat(value)
        if not at.tzinfo:
            raise ValueError
        return at.astimezone(UTC)
    except (ValueError, TypeError):
        raise HTTPException(400) from None


def slot_context(db, ctx, plan_id, account_id, slot, *, lock=False):
    plan = get_plan(db, ctx, plan_id, lock=lock)
    account = next((a for a in targets(db, plan) if a.id == account_id), None)
    original = original_time(slot)
    local = original.astimezone(ZoneInfo(plan.timezone))
    if (
        account is None
        or local.date() < plan.start_date
        or local.weekday() not in plan.weekdays
        or local.strftime("%H:%M") not in plan.times
        or local.second
        or local.microsecond
    ):
        raise HTTPException(404)
    edit = db.scalar(
        select(PlanSlotOverride).where(
            PlanSlotOverride.plan_id == plan.id,
            PlanSlotOverride.workspace_id == ctx.workspace.id,
            PlanSlotOverride.account_id == account.id,
            PlanSlotOverride.slot_at == original,
        )
    )
    at = ensure_aware(edit.scheduled_at) if edit else original
    records = db.scalars(
        select(ScheduledPost).where(
            ScheduledPost.workspace_id == ctx.workspace.id,
            ScheduledPost.plan_id == plan.id,
            ScheduledPost.connected_account_id == account.id,
            ScheduledPost.slot_at.in_([original, at]),
        )
    )
    if at <= utcnow() or any(
        p.status in (PostStatus.UPLOADING, PostStatus.PROCESSING, PostStatus.PUBLISHED)
        or reached_platform(p.platform_post_id, p.publish_state)
        for p in records
    ):
        raise HTTPException(409)
    return plan, account, original, edit, at


@router.get("/calendar")
def calendar(request: Request, db: DB, ctx: CurrentWorkspace, day: str = "", person: int = 0):
    people = db.execute(
        select(CreatorProfile, ContentPlan)
        .join(ContentPlan, ContentPlan.profile_id == CreatorProfile.id)
        .where(CreatorProfile.workspace_id == ctx.workspace.id)
        .order_by(CreatorProfile.name)
        .limit(101)
    ).all()
    selected = [(profile, plan) for profile, plan in people if not person or profile.id == person]
    if person and not selected:
        selected = db.execute(
            select(CreatorProfile, ContentPlan)
            .join(ContentPlan, ContentPlan.profile_id == CreatorProfile.id)
            .where(CreatorProfile.workspace_id == ctx.workspace.id, CreatorProfile.id == person)
        ).all()
        if not selected:
            raise HTTPException(404)
        people = [*people[:99], selected[0]]
    if person and selected:
        remember_person(request, *selected[0])
    zone = ZoneInfo(selected[0][1].timezone if person else ctx.workspace.timezone)
    now = utcnow()
    today = now.astimezone(zone).date()
    try:
        first = datetime.fromisoformat(day).date() if day else today
        if abs((first - today).days) > 366:
            raise ValueError
    except ValueError:
        raise HTTPException(400) from None
    start = datetime.combine(first, datetime.min.time(), zone)
    end = start + timedelta(days=7)
    events = []
    selected_has_targets = True
    for profile, plan in selected[:20]:
        preview = simulate(db, plan)
        if person:
            selected_has_targets = bool(preview["accounts"])
        for row in preview["rows"]:
            if start <= row.at < end:
                for entry in row.entries:
                    events.append(
                        {"at": row.at, "person": profile, "plan": plan, "entry": entry, "post": None}
                    )
    actual = list(
        db.scalars(
            select(ScheduledPost)
            .options(selectinload(ScheduledPost.video), selectinload(ScheduledPost.connected_account))
            .where(
                ScheduledPost.workspace_id == ctx.workspace.id,
                ScheduledPost.scheduled_at >= start,
                ScheduledPost.scheduled_at < end,
            )
            .order_by(ScheduledPost.scheduled_at)
            .limit(501)
        )
    )
    for record in actual[:500]:
        if person and record.video.profile_id != person:
            continue
        events.append(
            {
                "at": ensure_aware(record.scheduled_at),
                "post": record,
                "entry": None,
                "person": None,
                "plan": None,
                "edit_slot": (
                    db.scalar(
                        select(PlanSlotOverride.slot_at).where(
                            PlanSlotOverride.workspace_id == ctx.workspace.id,
                            PlanSlotOverride.plan_id == record.plan_id,
                            PlanSlotOverride.account_id == record.connected_account_id,
                            PlanSlotOverride.scheduled_at == record.slot_at,
                        )
                    )
                    or record.slot_at
                )
                if record.plan_id
                and record.slot_at
                and record.status == PostStatus.SCHEDULED
                and not reached_platform(record.platform_post_id, record.publish_state)
                and ensure_aware(record.scheduled_at) > utcnow()
                else None,
            }
        )
    events.sort(key=lambda e: e["at"])
    grouped = {
        date: list(items)
        for date, items in groupby(events[:1000], key=lambda e: e["at"].astimezone(zone).date())
    }
    return render(
        request,
        "calendar.html",
        {
            "nav": "calendar",
            "days": [
                (first + timedelta(days=i), grouped.get(first + timedelta(days=i), [])) for i in range(7)
            ],
            "previous": (first - timedelta(days=7)).isoformat(),
            "following": (first + timedelta(days=7)).isoformat(),
            "first": first,
            "zone": zone,
            "empty_day_reasons": {
                first + timedelta(days=i): empty_day(
                    selected[0][1], first + timedelta(days=i), now, has_targets=selected_has_targets
                )
                for i in range(7)
            }
            if person
            else {},
            "person_filter": person,
            "people": people[:100],
            "selected_plan": selected[0][1] if person and selected else None,
            "limited": len(events) > 1000 or len(selected) > 20 or len(actual) > 500,
        },
    )


@router.get("/calendar/{plan_id}/{account_id}/edit")
def edit_slot(request: Request, db: DB, ctx: CurrentWorkspace, plan_id: int, account_id: int, slot: str):
    plan, account, original, edit, at = slot_context(db, ctx, plan_id, account_id, slot)
    remember_person(request, db.get(CreatorProfile, plan.profile_id), plan)
    choices = list(
        db.scalars(
            select(Video)
            .where(
                Video.workspace_id == ctx.workspace.id,
                Video.profile_id == plan.profile_id,
                Video.status == VideoStatus.READY,
            )
            .order_by(Video.id)
            .limit(1001)
        )
    )
    choices = [
        v
        for v in choices[:1000]
        if not problem(v, account)
        and v.source_account_id != account.id
        and account.platform.value not in (v.overrides or {}).get("exclude_platforms", [])
    ]
    return render(
        request,
        "calendar_edit.html",
        {
            "nav": "calendar",
            "plan": plan,
            "account": account,
            "slot": original.isoformat(),
            "edit": edit,
            "local_at": at.astimezone(ZoneInfo(plan.timezone)).strftime("%Y-%m-%dT%H:%M"),
            "choices": choices,
        },
    )


@router.post("/calendar/{plan_id}/{account_id}/edit")
def save_slot(request: Request, db: DB, ctx: CurrentWorkspace, form: Form, plan_id: int, account_id: int):
    plan, account, original, edit, old_at = slot_context(
        db, ctx, plan_id, account_id, str(form.get("slot", "")), lock=True
    )
    try:
        if form_int(form, "revision", minimum=1, maximum=2**31 - 1) != plan.revision:
            raise FormError("plan.stale")
        action = str(form.get("action", ""))
        if action not in ("save", "exclude", "reset"):
            raise FormError("calendar.invalid")
        if action == "reset":
            if original <= utcnow():
                raise FormError("calendar.invalid")
            if edit:
                db.delete(edit)
        else:
            try:
                naive = datetime.fromisoformat(str(form.get("scheduled_at", "")))
                if naive.tzinfo or naive.second or naive.microsecond:
                    raise ValueError
                at = naive.replace(tzinfo=ZoneInfo(plan.timezone), fold=0).astimezone(UTC)
                if at.astimezone(ZoneInfo(plan.timezone)).replace(
                    tzinfo=None
                ) != naive or not utcnow() < at < utcnow() + timedelta(days=366):
                    raise ValueError
            except ValueError:
                raise FormError("calendar.invalid") from None
            # Never create two publications for one page at the same effective instant.
            if action != "exclude" and any(
                row.at == at
                and any(e.account.id == account.id and e.original_at != original for e in row.entries)
                for row in simulate(db, plan)["rows"]
            ):
                raise FormError("calendar.conflict")
            video_id = (
                form_int(form, "video_id", minimum=0, maximum=2**31 - 1)
                if str(form.get("video_id", "0"))
                else 0
            )
            video = db.get(Video, video_id) if video_id else None
            if video_id and (
                not video
                or video.workspace_id != ctx.workspace.id
                or video.profile_id != plan.profile_id
                or video.status != VideoStatus.READY
                or problem(video, account)
                or video.source_account_id == account.id
                or account.platform.value in (video.overrides or {}).get("exclude_platforms", [])
            ):
                raise FormError("calendar.invalid_content")
            if edit is None:
                edit = PlanSlotOverride(
                    workspace_id=ctx.workspace.id, plan_id=plan.id, account_id=account.id, slot_at=original
                )
                db.add(edit)
            edit.scheduled_at, edit.excluded = at, action == "exclude"
            edit.video_id, edit.replacement = video_id or None, bool(video_id)
        cancel_pending(db, plan)
        plan.enabled = False
        plan.revision += 1
        log_activity(
            db,
            ctx.workspace.id,
            ActivityLevel.INFO,
            "schedule",
            "A calendar slot was edited; the plan was stopped for review.",
            actor=ctx.actor,
            msg_key="calendar.audit_saved",
            plan_id=plan.id,
            account_id=account.id,
            action=action,
            slot=original.isoformat(),
        )
        db.commit()
        flash(request, "success", "calendar.saved")
    except FormError as exc:
        db.rollback()
        flash(request, "error", exc.key)
    return RedirectResponse("/calendar", 303)
