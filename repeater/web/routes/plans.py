"""Person setup: connect → folder → pages → schedule → read-only preview."""

from __future__ import annotations

import re
from datetime import date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, func, select

from repeater.accounts import AccountUnavailable, fresh_context
from repeater.activity import log_activity
from repeater.config import get_settings
from repeater.content_plans import simulate, targets
from repeater.engine.rules import rule_problems
from repeater.jobs import kinds
from repeater.models import (
    ACTIVE_POST_STATUSES,
    PUBLISH_PLATFORMS,
    AccountStatus,
    ConnectedAccount,
    ContentPlan,
    CreatorProfile,
    PlanTarget,
    Platform,
    RotationRule,
    ScheduledPost,
    Source,
    SourceKind,
    Video,
)
from repeater.platforms import provider_name_for_account
from repeater.platforms.base import PlatformError
from repeater.sources.drive import browse_folders, folder_details, parse_folder_id
from repeater.timeutil import utcnow
from repeater.web.auth import CurrentWorkspace
from repeater.web.deps import DB, Form, FormError, Page, form_bool, form_int, form_str, query_int
from repeater.web.journey import progress, remember_person
from repeater.web.templating import flash, render

router = APIRouter()
IMPORT_PLATFORMS = (Platform.FACEBOOK, Platform.INSTAGRAM)
WIZARD_STEPS = ("accounts", "content", "schedule", "preview")


def _wizard_step(form, section):
    step = form.get("wizard_step")
    if step and step != {
        "targets": "accounts",
        "source": "content",
        "folders": "content",
        "imports": "content",
        "schedule": "schedule",
    }.get(section):
        raise HTTPException(400)
    return step


def get_plan(db, ctx, plan_id, *, lock=False):
    if lock:
        from repeater.engine.scheduler import lock_workspace

        lock_workspace(db, ctx.workspace.id)
    plan = db.scalar(
        select(ContentPlan)
        .where(ContentPlan.id == plan_id, ContentPlan.workspace_id == ctx.workspace.id)
        .with_for_update()
        if lock
        else select(ContentPlan).where(
            ContentPlan.id == plan_id, ContentPlan.workspace_id == ctx.workspace.id
        )
    )
    if plan is None:
        raise HTTPException(404)
    return plan


def _source(db, plan):
    source = db.get(Source, plan.source_id) if plan.source_id else None
    if source and (source.workspace_id != plan.workspace_id or source.profile_id != plan.profile_id):
        raise HTTPException(404)
    return source


def _drive(db, ctx, form):
    account_id = form_int(form, "drive_id", minimum=1, maximum=2**31 - 1)
    account = db.scalar(
        select(ConnectedAccount).where(
            ConnectedAccount.id == account_id,
            ConnectedAccount.workspace_id == ctx.workspace.id,
            ConnectedAccount.platform == Platform.GDRIVE,
            ConnectedAccount.status == AccountStatus.ACTIVE,
        )
    )
    if account is None:
        raise FormError("plan.drive_invalid")
    return account


def _context(db, ctx, plan):
    accounts = list(
        db.scalars(
            select(ConnectedAccount)
            .where(ConnectedAccount.workspace_id == ctx.workspace.id)
            .order_by(ConnectedAccount.platform, ConnectedAccount.display_name, ConnectedAccount.id)
        )
    )
    social_sources = list(
        db.scalars(
            select(Source)
            .where(
                Source.workspace_id == plan.workspace_id,
                Source.profile_id == plan.profile_id,
                Source.kind == SourceKind.PLATFORM_API,
            )
            .order_by(Source.id)
        )
    )
    source_counts = {}
    for source_id, status, count in db.execute(
        select(Video.source_id, Video.status, func.count())
        .where(
            Video.workspace_id == plan.workspace_id,
            Video.profile_id == plan.profile_id,
        )
        .group_by(Video.source_id, Video.status)
    ):
        source_counts.setdefault(source_id, {})[status.value] = count
    used = {s.connected_account_id for s in social_sources}
    return {
        "social_sources": social_sources,
        "source_counts": source_counts,
        "import_accounts": [
            a
            for a in accounts
            if a.platform in IMPORT_PLATFORMS
            and a.status == AccountStatus.ACTIVE
            and a.profile_id in (None, plan.profile_id)
            and a.id not in used
        ],
        "nav": "plans",
        "plan": plan,
        "person": db.get(CreatorProfile, plan.profile_id),
        "source": _source(db, plan),
        "selected": {a.id for a in targets(db, plan)},
        "accounts": [
            a for a in accounts if a.platform in PUBLISH_PLATFORMS and a.profile_id in (None, plan.profile_id)
        ],
        "drives": [a for a in accounts if a.platform == Platform.GDRIVE and a.status == AccountStatus.ACTIVE],
        "account_providers": {a.id: provider_name_for_account(a) for a in accounts},
        "connections": [
            (p, get_settings().platform_configured(provider))
            for p, provider in [
                ("gdrive", "google"),
                ("youtube", "google"),
                ("instagram", "instagram"),
                ("meta", "meta"),
                ("tiktok", "tiktok"),
            ]
        ],
        "zones": sorted(available_timezones()),
        "error": None,
        "submitted": {},
        "folders": None,
        "folder_drive_id": None,
        "folder_page": "",
    }


def _edit(request, db, ctx, plan, *, error=None, submitted=None, **extra):
    data = _context(db, ctx, plan)
    remember_person(request, data["person"], plan)
    data.update(error=error, submitted=submitted or {}, **extra)
    if data["submitted"].get("wizard_step") in WIZARD_STEPS:
        return _wizard(request, db, ctx, plan, data["submitted"]["wizard_step"], data=data)
    return render(request, "plans/edit.html", data, status_code=422 if error else 200)


def _wizard(request, db, ctx, plan, step, *, data=None, page="1"):
    data = data or _context(db, ctx, plan)
    remember_person(request, data["person"], plan)
    data.setdefault("draft", {})
    data["max_upload_mb"] = get_settings().max_upload_mb
    counts = {}
    for statuses in data["source_counts"].values():
        for status, count in statuses.items():
            counts[status] = counts.get(status, 0) + count
    data.update(wizard_step=step, wizard_steps=WIZARD_STEPS, library_counts=counts)
    if step == "preview":
        data.update(_preview_context(db, ctx, plan, page))
        data["preview_path"] = f"/plans/{plan.id}/setup/preview"
    return render(request, "plans/wizard.html", data, status_code=422 if data["error"] else 200)


@router.get("/setup")
def setup_start(request: Request, ctx: CurrentWorkspace) -> Response:
    return render(request, "plans/start.html", {"nav": "plans", "error": None, "name": ""})


@router.get("/plans/{plan_id}/setup/{step}")
def setup(
    request: Request, db: DB, ctx: CurrentWorkspace, plan_id: int, step: str, page: str = "1"
) -> Response:
    if step not in WIZARD_STEPS:
        raise HTTPException(404)
    return _wizard(request, db, ctx, get_plan(db, ctx, plan_id), step, page=page)


def _revision(form, plan):
    if form_int(form, "revision", minimum=1, maximum=2**31 - 1) != plan.revision:
        raise FormError("plan.stale")


@router.get("/plans")
def index(request: Request, db: DB, ctx: CurrentWorkspace) -> Response:
    rows = db.execute(
        select(CreatorProfile, ContentPlan)
        .join(ContentPlan, ContentPlan.profile_id == CreatorProfile.id)
        .where(CreatorProfile.workspace_id == ctx.workspace.id)
        .order_by(CreatorProfile.id)
    ).all()
    return render(
        request,
        "plans/index.html",
        {
            "nav": "plans",
            "people": rows,
            "error": None,
            "progress": progress(db, ctx.workspace.id, [plan for _, plan in rows]),
        },
    )


@router.post("/plans")
def create(request: Request, db: DB, ctx: CurrentWorkspace, form: Form) -> Response:
    try:
        name = form_str(form, "name", max_len=200, required=True)
    except FormError as exc:
        if form.get("wizard_step") == "accounts":
            return render(
                request,
                "plans/start.html",
                {
                    "nav": "plans",
                    "error": exc,
                    "name": str(form.get("name", ""))[:200],
                },
                status_code=422,
            )
        flash(request, "error", exc.key, **exc.params)
        return RedirectResponse("/plans#new", 303)
    from repeater.commerce import require_capacity

    require_capacity(db, ctx.workspace.id, "people")
    person = CreatorProfile(workspace_id=ctx.workspace.id, name=name)
    db.add(person)
    db.flush()
    plan = ContentPlan(
        workspace_id=ctx.workspace.id,
        profile_id=person.id,
        timezone=ctx.workspace.timezone,
        start_date=utcnow().astimezone(ZoneInfo(ctx.workspace.timezone)).date(),
        rotation_mode="repeat" if form.get("wizard_step") == "accounts" else "once",
    )
    db.add(plan)
    db.commit()
    remember_person(request, person, plan)
    destination = "setup/accounts" if form.get("wizard_step") == "accounts" else "edit"
    return RedirectResponse(f"/plans/{plan.id}/{destination}", 303)


@router.get("/plans/{plan_id}/edit")
def edit(request: Request, db: DB, ctx: CurrentWorkspace, plan_id: int) -> Response:
    return _edit(request, db, ctx, get_plan(db, ctx, plan_id))


@router.post("/plans/{plan_id}/folders")
def folders(request: Request, db: DB, ctx: CurrentWorkspace, plan_id: int, form: Form) -> Response:
    plan = get_plan(db, ctx, plan_id)
    _wizard_step(form, "folders")
    try:
        account = _drive(db, ctx, form)
        page = form_str(form, "page_token", max_len=2000)
        items, next_page = browse_folders(fresh_context(db, account), page_token=page)
        db.commit()  # persist a refreshed credential, if any
        return _edit(
            request,
            db,
            ctx,
            plan,
            submitted=dict(form),
            folders=items,
            folder_drive_id=account.id,
            folder_page=next_page,
        )
    except (PlatformError, AccountUnavailable):
        db.commit()
        return _edit(request, db, ctx, plan, error=FormError("plan.drive_failed"), submitted=dict(form))
    except FormError as exc:
        return _edit(request, db, ctx, plan, error=exc, submitted=dict(form))


@router.post("/plans/{plan_id}/execution")
def execution(request: Request, db: DB, ctx: CurrentWorkspace, plan_id: int, form: Form) -> Response:
    from repeater.engine.people import cancel_pending

    if not ctx.is_owner:
        raise HTTPException(403)
    plan = get_plan(db, ctx, plan_id, lock=True)
    try:
        _revision(form, plan)
        action = form_str(form, "action", max_len=12, required=True)
        if action not in {"start", "pause"}:
            raise FormError("plan.schedule_invalid")
        if action == "start":
            from repeater.commerce import work_allowed

            if not work_allowed(ctx.workspace):
                raise FormError("commerce.expired")
            if not get_settings().publishing_enabled:
                raise FormError("plan.execution_locked")
            db.refresh(ctx.workspace)
            if ctx.workspace.paused:
                raise FormError("plan.workspace_paused")
            if not targets(db, plan):
                raise FormError("plan.no_targets")
        cancel_pending(db, plan)
        plan.enabled = action == "start"
        plan.revision += 1
        if plan.enabled:
            kinds.enqueue_plan_workspace(db, plan.workspace_id)
        log_activity(
            db,
            plan.workspace_id,
            "info",
            "plans",
            f"Person plan {plan.id}: {action}.",
            actor=ctx.actor,
            plan_id=plan.id,
        )
        db.commit()
        flash(request, "success", "plan.started" if plan.enabled else "plan.paused")
        return RedirectResponse(f"/plans/{plan.id}#execution", 303)
    except FormError as exc:
        db.rollback()
        return _edit(request, db, ctx, get_plan(db, ctx, plan_id), error=exc)


@router.post("/plans/{plan_id}/{section}")
def save(request: Request, db: DB, ctx: CurrentWorkspace, plan_id: int, section: str, form: Form) -> Response:
    if section not in {"person", "source", "targets", "schedule", "rotation", "imports", "platforms"}:
        raise HTTPException(404)
    plan = get_plan(db, ctx, plan_id, lock=True)
    wizard_step = _wizard_step(form, section)
    try:
        _revision(form, plan)
        if section == "person":
            db.get(CreatorProfile, plan.profile_id).name = form_str(form, "name", max_len=200, required=True)
        elif section == "platforms":
            _add_platform_source(db, ctx, plan, form)
        elif section == "source":
            account = _drive(db, ctx, form)
            folder_id = parse_folder_id(form_str(form, "folder", max_len=1000, required=True))
            backfill = form_int(form, "backfill", minimum=0, maximum=200, default=30)
            source = _source(db, plan)
            if source is not None:
                # A worker may have advanced seen IDs/baseline while this form was open.
                # Keep those checkpoints when changing the image discovery option.
                db.refresh(source, with_for_update=True)
            if source and (source.enabled or source.last_polled_at):
                if source.connected_account_id != account.id or source.config.get("folder_id") != folder_id:
                    raise FormError("plan.source_locked")
            details = folder_details(fresh_context(db, account), folder_id)
            if source is None:
                source = Source(
                    workspace_id=ctx.workspace.id,
                    profile_id=plan.profile_id,
                    kind=SourceKind.DRIVE,
                    platform=Platform.GDRIVE,
                    enabled=False,
                    poll_interval_minutes=15,
                    config={},
                )
                db.add(source)
                db.flush()
                plan.source_id = source.id
            source.connected_account_id = account.id
            source.name = details["name"]
            if not source.last_polled_at and not source.enabled:
                source.config = {**source.config, "folder_id": folder_id, "backfill_count": backfill}
            source.config = {**source.config, "include_images": form_bool(form, "include_images")}
        elif section == "targets":
            raw = form.getlist("targets")
            if wizard_step and not raw:
                raise FormError("wizard.pick_pages")
            if len(raw) > 100 or any(
                not isinstance(v, str) or not v.isascii() or not v.isdecimal() or len(v) > 10 for v in raw
            ):
                raise FormError("plan.targets_invalid")
            ids = {int(v) for v in raw}
            accounts = list(
                db.scalars(
                    select(ConnectedAccount)
                    .where(ConnectedAccount.id.in_(ids), ConnectedAccount.workspace_id == ctx.workspace.id)
                    .order_by(ConnectedAccount.id)
                    .with_for_update()
                )
            )
            if len(accounts) != len(ids) or any(
                a.platform not in PUBLISH_PLATFORMS
                or a.status != AccountStatus.ACTIVE
                or a.profile_id not in (None, plan.profile_id)
                for a in accounts
            ):
                raise FormError("plan.targets_invalid")
            busy = db.scalar(
                select(ScheduledPost.id)
                .where(
                    ScheduledPost.connected_account_id.in_(ids),
                    ScheduledPost.status.in_(ACTIVE_POST_STATUSES),
                )
                .limit(1)
            )
            if busy:
                raise FormError("plan.account_busy")
            db.execute(delete(PlanTarget).where(PlanTarget.plan_id == plan.id))
            for a in accounts:
                a.profile_id = plan.profile_id
                db.add(PlanTarget(plan_id=plan.id, account_id=a.id, workspace_id=plan.workspace_id))
        elif section == "schedule":
            zone = form_str(form, "timezone", max_len=64, required=True)
            try:
                ZoneInfo(zone)
                start = date.fromisoformat(form_str(form, "start_date", max_len=10, required=True))
            except (ValueError, ZoneInfoNotFoundError):
                raise FormError("plan.schedule_invalid") from None
            today = utcnow().astimezone(ZoneInfo(zone)).date()
            # An ongoing plan keeps its original start date when its hours/days change.
            if start != plan.start_date and not today <= start <= today + timedelta(days=365):
                raise FormError("plan.date_invalid")
            days = form.getlist("weekdays")
            times = [str(t).strip() for t in form.getlist("times") if str(t).strip()]
            if (
                not days
                or any(d not in [str(i) for i in range(7)] for d in days)
                or not 1 <= len(times) <= 5
                or any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in times)
                or len(set(times)) != len(times)
            ):
                raise FormError("plan.schedule_invalid")
            ordering = form_str(form, "ordering", max_len=16)
            if ordering not in {"oldest", "newest"}:
                raise FormError("plan.schedule_invalid")
            plan.timezone, plan.start_date = zone, start
            plan.weekdays, plan.times, plan.ordering = sorted({int(d) for d in days}), sorted(times), ordering
            if wizard_step:
                _rotation(form, plan)
        elif section == "rotation":
            _rotation(form, plan)
        elif section == "imports":
            source = _source(db, plan)
            if source is None:
                raise FormError("plan.drive_invalid")
            action = form_str(form, "action", max_len=16)
            if action not in {"start", "pause"}:
                raise FormError("plan.schedule_invalid")
            if action == "start":
                account = db.get(ConnectedAccount, source.connected_account_id)
                if (
                    account is None
                    or account.workspace_id != ctx.workspace.id
                    or account.status != AccountStatus.ACTIVE
                ):
                    raise FormError("plan.drive_invalid")
                source.enabled = True
                kinds.enqueue_poll_source(db, source.id)
            else:
                source.enabled = False
        log_activity(
            db,
            plan.workspace_id,
            "info",
            "plans",
            f"Draft plan {plan.id}: saved {section}.",
            actor=ctx.actor,
            plan_id=plan.id,
            profile_id=plan.profile_id,
            section=section,
        )
        from repeater.engine.people import cancel_pending

        cancel_pending(db, plan)
        if section in {"targets", "schedule", "rotation"}:
            plan.enabled = False
        plan.revision += 1
        db.commit()
        flash(request, "success", "plan.saved")
        if wizard_step:
            next_step = {"targets": "content", "schedule": "preview"}.get(section, wizard_step)
            return RedirectResponse(f"/plans/{plan.id}/setup/{next_step}", 303)
        return RedirectResponse(
            f"/plans/{plan.id}"
            if section in {"schedule", "rotation"}
            else f"/plans/{plan.id}/edit#{section}",
            303,
        )
    except (PlatformError, AccountUnavailable):
        # Only credential refresh can have changed persistent state before Drive lookup fails.
        # Keep rotated credentials rather than rolling them back to an invalid refresh token.
        db.commit()
        return _edit(
            request,
            db,
            ctx,
            get_plan(db, ctx, plan_id),
            error=FormError("plan.drive_failed"),
            submitted=dict(form),
        )
    except FormError as exc:
        db.rollback()
        submitted = dict(form)
        if section == "targets":
            submitted["targets"] = form.getlist("targets")
        if section == "schedule":
            submitted["weekdays"] = form.getlist("weekdays")
            submitted["times"] = form.getlist("times")
        return _edit(request, db, ctx, get_plan(db, ctx, plan_id), error=exc, submitted=submitted)


def _rotation(form, plan):
    mode = form_str(form, "rotation_mode", max_len=16, required=True)
    if mode not in {"once", "repeat"}:
        raise FormError("plan.rotation_invalid")
    try:
        days = form_int(form, "repeat_after_days", minimum=1, maximum=365)
    except FormError:
        raise FormError("plan.rotation_invalid") from None
    plan.rotation_mode, plan.repeat_after_days = mode, days


def _add_platform_source(db, ctx, plan, form):
    account_id = form_int(form, "source_account_id", minimum=1, maximum=2**31 - 1)
    mode = form_str(form, "import_mode", max_len=16, required=True)
    account = db.scalar(
        select(ConnectedAccount)
        .where(
            ConnectedAccount.id == account_id,
            ConnectedAccount.workspace_id == ctx.workspace.id,
        )
        .with_for_update()
    )
    if (
        mode not in {"latest30", "new"}
        or account is None
        or account.platform not in IMPORT_PLATFORMS
        or account.status != AccountStatus.ACTIVE
        or account.profile_id not in (None, plan.profile_id)
    ):
        raise FormError("plan.platform_invalid")
    existing = db.scalar(
        select(Source.id).where(
            Source.profile_id == plan.profile_id,
            Source.connected_account_id == account.id,
            Source.kind == SourceKind.PLATFORM_API,
        )
    )
    if existing:
        raise FormError("plan.platform_exists")
    busy = db.scalar(
        select(ScheduledPost.id)
        .where(
            ScheduledPost.connected_account_id == account.id,
            ScheduledPost.status.in_(ACTIVE_POST_STATUSES),
        )
        .limit(1)
    )
    legacy = db.scalar(
        select(Source.id)
        .where(
            Source.connected_account_id == account.id,
            Source.profile_id.is_(None),
            Source.enabled.is_(True),
        )
        .limit(1)
    )
    if busy or legacy:
        raise FormError("plan.source_busy")
    account.profile_id = plan.profile_id
    db.add(
        Source(
            workspace_id=plan.workspace_id,
            profile_id=plan.profile_id,
            connected_account_id=account.id,
            kind=SourceKind.PLATFORM_API,
            platform=account.platform,
            name=account.display_name or account.external_id,
            config={"backfill_count": 30 if mode == "latest30" else 0},
            poll_interval_minutes=15,
            enabled=False,
        )
    )


@router.post("/plans/{plan_id}/platforms/{source_id}")
def control_platform(
    request: Request, db: DB, ctx: CurrentWorkspace, plan_id: int, source_id: int, form: Form
) -> Response:
    plan = get_plan(db, ctx, plan_id, lock=True)
    source = db.scalar(
        select(Source)
        .where(
            Source.id == source_id,
            Source.workspace_id == ctx.workspace.id,
            Source.profile_id == plan.profile_id,
            Source.kind == SourceKind.PLATFORM_API,
        )
        .with_for_update()
    )
    if source is None:
        raise HTTPException(404)
    try:
        _revision(form, plan)
        action = form_str(form, "action", max_len=16)
        if action not in {"start", "pause", "check"}:
            raise FormError("plan.platform_invalid")
        if action == "pause":
            source.enabled = False
        else:
            account = db.get(ConnectedAccount, source.connected_account_id)
            if (
                account is None
                or account.workspace_id != ctx.workspace.id
                or account.profile_id != plan.profile_id
                or account.platform != source.platform
                or account.status != AccountStatus.ACTIVE
                or source.platform not in IMPORT_PLATFORMS
                or (action == "check" and not source.enabled)
            ):
                raise FormError("plan.platform_invalid")
            source.enabled = True
            source.consecutive_failures = 0
            config = dict(source.config or {})
            config.setdefault("watch_from", utcnow().isoformat())
            source.config = config
            kinds.enqueue_poll_source(db, source.id)
        # Reject a stale in-flight listing even if pause and resume both happen before it returns.
        source.config = {**source.config, "control_revision": plan.revision + 1}
        from repeater.engine.people import cancel_pending

        cancel_pending(db, plan)
        plan.revision += 1
        log_activity(
            db,
            plan.workspace_id,
            "info",
            "plans",
            f"Person source {source.id}: {action}.",
            actor=ctx.actor,
            plan_id=plan.id,
            source_id=source.id,
            action=action,
        )
        db.commit()
        flash(request, "success", "plan.saved")
        return RedirectResponse(f"/plans/{plan.id}/edit#platforms", 303)
    except FormError as exc:
        db.rollback()
        return _edit(request, db, ctx, get_plan(db, ctx, plan_id), error=exc)


@router.get("/plans/{plan_id}")
def preview(request: Request, db: DB, ctx: CurrentWorkspace, plan_id: int, page: str = "1") -> Response:
    plan = get_plan(db, ctx, plan_id)
    remember_person(request, db.get(CreatorProfile, plan.profile_id), plan)
    return render(
        request,
        "plans/preview.html",
        {
            **_context(db, ctx, plan),
            **_preview_context(db, ctx, plan, page),
            "preview_path": f"/plans/{plan.id}",
        },
    )


def _preview_context(db, ctx, plan, page):
    result = simulate(db, plan)
    pager = Page(number=query_int(page, default=1), size=10, total=len(result["rows"]))
    result["rows"] = result["rows"][pager.offset : pager.offset + pager.size]
    rules = {
        r.connected_account_id: r
        for r in db.scalars(select(RotationRule).where(RotationRule.workspace_id == ctx.workspace.id))
    }
    warnings = [
        a
        for a in result["accounts"]
        if a.status != AccountStatus.ACTIVE or a.id not in rules or rule_problems(rules[a.id])
    ]
    return {"preview": result, "warnings": warnings, "pager": pager}
