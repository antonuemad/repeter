"""Owned-person navigation and the next useful setup step; never schedules work."""

from sqlalchemy import func, select

from repeater.media.content import problem
from repeater.models import (
    PUBLISH_PLATFORMS,
    AccountStatus,
    ConnectedAccount,
    ContentPlan,
    CreatorProfile,
    PlanTarget,
    Video,
    VideoStatus,
)

PERSON_KEY = "active_person"


def person_url(plan, section="accounts"):
    return f"/people/{plan.profile_id}?section={section}"


def remember_person(request, person, plan):
    value = {"id": person.id, "workspace_id": person.workspace_id, "name": person.name, "plan_id": plan.id}
    request.session[PERSON_KEY] = person.id
    request.state.active_person = value


def restore_person(request, db, workspace_id):
    """Revalidate the cookie's selection against the current workspace on every request."""
    person_id = request.session.get(PERSON_KEY)
    if type(person_id) is not int:
        return
    row = db.execute(
        select(CreatorProfile, ContentPlan)
        .join(ContentPlan, ContentPlan.profile_id == CreatorProfile.id)
        .where(
            CreatorProfile.workspace_id == workspace_id,
            CreatorProfile.id == person_id,
            ContentPlan.workspace_id == workspace_id,
        )
    ).first()
    if row:
        remember_person(request, *row)
    else:
        request.session.pop(PERSON_KEY, None)


def progress(db, workspace_id, plans):
    """Batch readiness for lists, using only selected active pages and ready owned items."""
    ids = [p.id for p in plans]
    totals = dict(
        db.execute(
            select(PlanTarget.plan_id, func.count())
            .where(PlanTarget.workspace_id == workspace_id, PlanTarget.plan_id.in_(ids))
            .group_by(PlanTarget.plan_id)
        ).all()
    )
    selected = list(
        db.execute(
            select(PlanTarget.plan_id, ConnectedAccount)
            .join(ConnectedAccount, ConnectedAccount.id == PlanTarget.account_id)
            .join(ContentPlan, ContentPlan.id == PlanTarget.plan_id)
            .where(
                PlanTarget.workspace_id == workspace_id,
                PlanTarget.plan_id.in_(ids),
                ConnectedAccount.workspace_id == workspace_id,
                ConnectedAccount.profile_id == ContentPlan.profile_id,
                ConnectedAccount.platform.in_(PUBLISH_PLATFORMS),
                ConnectedAccount.status == AccountStatus.ACTIVE,
            )
        ).all()
    )
    items = dict(
        db.execute(
            select(Video.profile_id, func.count())
            .where(
                Video.workspace_id == workspace_id,
                Video.profile_id.in_([p.profile_id for p in plans]),
                Video.status == VideoStatus.READY,
            )
            .group_by(Video.profile_id)
        ).all()
    )
    ready = list(
        db.scalars(
            select(Video).where(
                Video.workspace_id == workspace_id,
                Video.profile_id.in_([p.profile_id for p in plans]),
                Video.status == VideoStatus.READY,
            )
        )
    )
    by_person = {}
    for item in ready:
        by_person.setdefault(item.profile_id, []).append(item)
    result = {}
    for plan in plans:
        accounts = [account for plan_id, account in selected if plan_id == plan.id]
        usable = set()
        covered = 0
        for account in accounts:
            eligible = [
                item.id
                for item in by_person.get(plan.profile_id, [])
                if item.source_account_id != account.id
                and account.platform.value not in (item.overrides or {}).get("exclude_platforms", [])
                and problem(item, account) is None
            ]
            usable.update(eligible)
            covered += bool(eligible)
        steps = {
            "accounts": bool(accounts) and len(accounts) == totals.get(plan.id, 0),
            "content": bool(accounts) and covered == len(accounts),
            "schedule": bool(plan.schedule_reviewed_at and plan.times and plan.weekdays),
        }
        next_step = next((key for key, done in steps.items() if not done), "preview")
        result[plan.id] = {
            **steps,
            "done": all(steps.values()),
            "completed": sum(steps.values()),
            "pages": len(accounts),
            "items": items.get(plan.profile_id, 0),
            "usable_items": len(usable),
            "uncovered_pages": len(accounts) - covered,
            "next_step": next_step,
            "next_url": person_url(plan, next_step),
            "links": {key: person_url(plan, key) for key in steps},
        }
    return result
