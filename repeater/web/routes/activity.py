"""Activity log with level/category filters and pagination."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import func, select

from repeater.models import ActivityLevel, ActivityLog
from repeater.web.auth import CurrentWorkspace
from repeater.web.deps import DB, Page, query_int
from repeater.web.templating import render

router = APIRouter()

PAGE_SIZE = 50
CATEGORIES = ("import", "publish", "account", "source", "system", "auth")
LEVELS = tuple(level.value for level in ActivityLevel)


@router.get("/activity")
def activity(
    request: Request, db: DB, ctx: CurrentWorkspace, level: str = "", category: str = "", page: str = "1"
) -> Response:
    level = level if level in LEVELS else ""
    category = category if category in CATEGORIES else ""
    where = [ActivityLog.workspace_id == ctx.workspace.id]
    if level:
        where.append(ActivityLog.level == ActivityLevel(level))
    if category:
        where.append(ActivityLog.category == category)
    total = int(db.execute(select(func.count()).select_from(ActivityLog).where(*where)).scalar_one())
    pager = Page(number=query_int(page, default=1), size=PAGE_SIZE, total=total)
    entries = list(
        db.execute(
            select(ActivityLog)
            .where(*where)
            .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
            .offset(pager.offset)
            .limit(PAGE_SIZE)
        ).scalars()
    )
    context = {
        "nav": "activity",
        "entries": entries,
        "pager": pager,
        "level": level,
        "category": category,
        "levels": LEVELS,
        "categories": CATEGORIES,
    }
    return render(request, "activity.html", context)
