"""Owned-person navigation and the next useful setup step; never schedules work."""

from sqlalchemy import func, select

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
    pages = dict(
        db.execute(
            select(PlanTarget.plan_id, func.count())
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
            .group_by(PlanTarget.plan_id)
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
    result = {}
    for plan in plans:
        steps = {
            "accounts": pages.get(plan.id, 0) > 0,
            "content": items.get(plan.profile_id, 0) > 0,
            "schedule": bool(plan.times and plan.weekdays),
        }
        next_step = next((key for key, done in steps.items() if not done), "preview")
        result[plan.id] = {
            **steps,
            "done": all(steps.values()),
            "completed": sum(steps.values()),
            "pages": pages.get(plan.id, 0),
            "items": items.get(plan.profile_id, 0),
            "next_step": next_step,
            "next_url": f"/plans/{plan.id}/setup/{next_step}",
            "links": {key: f"/plans/{plan.id}/setup/{key}" for key in steps},
        }
    return result
