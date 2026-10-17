"""Authenticated, read-only user guide using revalidated person navigation."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from repeater.web import i18n
from repeater.web.auth import CurrentWorkspace
from repeater.web.guide_content import chapters_for, groups_for
from repeater.web.templating import render

router = APIRouter()


@router.get("/guide")
def guide(request: Request, ctx: CurrentWorkspace, topic: str = "access") -> Response:
    lang = i18n.request_lang(request)
    chapters = chapters_for(lang, is_admin=ctx.user.is_platform_admin)
    position = next((i for i, chapter in enumerate(chapters) if chapter["id"] == topic), None)
    if position is None:
        raise HTTPException(404)
    person = getattr(request.state, "active_person", None)
    setup = f"/plans/{person['plan_id']}/setup" if person else None
    links = {
        "home": "/",
        "people": "/plans",
        "new_person": "/setup",
        "accounts": f"{setup}/accounts" if setup else "/accounts",
        "connected_accounts": "/accounts",
        "content": f"{setup}/content" if setup else "/plans",
        "library": f"/library?profile_id={person['id']}" if person else "/library",
        "times": f"{setup}/schedule" if setup else "/plans",
        "preview": f"{setup}/preview" if setup else "/plans",
        "calendar": f"/calendar?person={person['id']}" if person else "/calendar",
        "queue": "/schedule",
        "sources": "/sources",
        "activity": "/activity",
        "workspace": "/settings/workspace",
        "members": "/settings/members",
        "billing": "/settings/billing",
        "customers": "/admin/customers",
        "operations": "/admin/operations",
    }
    response = render(
        request,
        "guide.html",
        {
            "nav": "guide",
            "chapter": chapters[position],
            "guide_groups": groups_for(lang, chapters),
            "chapter_number": position + 1,
            "chapter_count": len(chapters),
            "previous_chapter": chapters[position - 1] if position else None,
            "next_chapter": chapters[position + 1] if position + 1 < len(chapters) else None,
            "guide_links": links,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response
