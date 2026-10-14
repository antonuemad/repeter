"""Meta's mandatory callbacks (PLAN.md 7.5): deauthorize and data deletion.

Meta calls these server-to-server with a form field `signed_request`
(base64url(HMAC-SHA256 signature) "." base64url(JSON payload), signed with the app secret).
There is no session and no CSRF token: the signature is the authentication."""

from __future__ import annotations

import importlib
import logging
import re
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from repeater.accounts import disconnect
from repeater.config import get_settings
from repeater.jobs import kinds
from repeater.models import AccountStatus, ConnectedAccount, DataDeletionRequest, Platform
from repeater.web.deps import DB, Form
from repeater.web.templating import render

log = logging.getLogger("repeater.web.meta")
router = APIRouter()

META_PLATFORMS = (Platform.FACEBOOK, Platform.INSTAGRAM)
_CODE_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")


def _verified_user_id(form: Any) -> str:
    """Verify signed_request with the app secret and return the app-scoped Facebook user id."""
    signed_request = form.get("signed_request")
    if not isinstance(signed_request, str) or not signed_request:
        raise _bad_request("missing signed_request")
    meta = importlib.import_module("repeater.platforms.meta")
    try:
        data = meta.parse_signed_request(signed_request)
    except ValueError as exc:
        log.warning("rejected Meta callback: %s", exc)
        raise _bad_request("invalid signed_request") from None
    user_id = str(data.get("user_id") or "").strip()
    if not user_id or len(user_id) > 128:
        raise _bad_request("signed_request has no user_id")
    return user_id


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _accounts_for(db: Session, fb_user_id: str) -> list[ConnectedAccount]:
    return list(
        db.execute(
            select(ConnectedAccount)
            .where(
                ConnectedAccount.platform.in_(META_PLATFORMS),
                ConnectedAccount.extra["fb_user_id"].astext == fb_user_id,
            )
            .order_by(ConnectedAccount.id)
        ).scalars()
    )


@router.post("/meta/deauthorize", include_in_schema=False)
def deauthorize(db: DB, form: Form) -> JSONResponse:
    """The user removed the app in Facebook: mark their Pages and Instagram accounts
    disconnected and stop all their work. Meta already revoked the grant, so no revoke call."""
    fb_user_id = _verified_user_id(form)
    workspaces: set[int] = set()
    count = 0
    for account in _accounts_for(db, fb_user_id):
        if account.status == AccountStatus.DISABLED and not account.token_enc:
            continue
        disconnect(db, account, actor="meta", revoke=False)
        workspaces.add(account.workspace_id)
        count += 1
    for workspace_id in sorted(workspaces):
        kinds.enqueue_plan_workspace(db, workspace_id)
    db.commit()
    log.info("Meta deauthorize callback: %d account(s) disconnected", count)
    return JSONResponse({"ok": True})


@router.post("/meta/data-deletion", include_in_schema=False)
def data_deletion(db: DB, form: Form) -> JSONResponse:
    """Record the request, queue the deletion job and return where the user can follow it."""
    fb_user_id = _verified_user_id(form)
    code = secrets.token_hex(12)
    request = DataDeletionRequest(provider="meta", external_user_id=fb_user_id, confirmation_code=code)
    db.add(request)
    db.flush()
    kinds.enqueue_delete_user_data(db, request.id)
    db.commit()
    url = f"{get_settings().base_url.rstrip('/')}/meta/deletion-status/{code}"
    return JSONResponse({"url": url, "confirmation_code": code})


@router.get("/meta/deletion-status/{code}")
def deletion_status(request: Request, db: DB, code: str) -> Response:
    record = None
    if _CODE_RE.match(code):
        record = db.execute(
            select(DataDeletionRequest).where(DataDeletionRequest.confirmation_code == code)
        ).scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404)
    return render(request, "deletion_status.html", {"record": record, "nav": "legal"})
