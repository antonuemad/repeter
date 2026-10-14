"""One-time consent screen: the user accepts the Terms of Service and the Privacy Policy before
using the dashboard (see repeater/web/consent.py)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from repeater.web import consent
from repeater.web.auth import SignedInUser
from repeater.web.deps import DB, Form, form_bool, safe_next
from repeater.web.templating import render

router = APIRouter()


def _next(value: object) -> str:
    return safe_next(value if isinstance(value, str) else None)


@router.get("/consent")
def consent_page(request: Request, db: DB, user: SignedInUser, next: str = "/") -> Response:  # noqa: A002
    target = safe_next(next)
    if consent.has_consented(request.session, db, user):
        return RedirectResponse(target, status_code=303)
    return render(request, "consent.html", {"next": target, "version": consent.LEGAL_VERSION, "error": None})


@router.post("/consent")
def accept(request: Request, db: DB, user: SignedInUser, form: Form) -> Response:
    target = _next(form.get("next"))
    if not form_bool(form, "accept"):
        return render(
            request,
            "consent.html",
            {"next": target, "version": consent.LEGAL_VERSION, "error": "consent.required"},
            status_code=400,
        )
    if not consent.has_consented(request.session, db, user):
        consent.record_consent(request.session, db, user)
        db.commit()
    return RedirectResponse(target, status_code=303)
