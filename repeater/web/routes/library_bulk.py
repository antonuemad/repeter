"""Reviewed, bounded library changes. No file deletion, provider calls or publishing jobs."""

import hashlib
import json
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select

from repeater.activity import log_activity
from repeater.config import get_settings
from repeater.engine.people import cancel_pending
from repeater.engine.publisher import reached_platform
from repeater.engine.scheduler import lock_workspace
from repeater.media import content
from repeater.models import (
    PUBLISH_PLATFORMS,
    ActivityLevel,
    ContentPlan,
    CreatorProfile,
    Platform,
    PostStatus,
    ScheduledPost,
    Video,
    VideoStatus,
)
from repeater.web.auth import CurrentWorkspace
from repeater.web.deps import DB, Form, FormError, form_bool, form_int, form_str, query_int
from repeater.web.routes.library import PAGE_SIZE, STATUS_FILTERS, _excluded_platforms, _file, _library_page
from repeater.web.templating import flash, render

router = APIRouter()
ACTIONS = ("exclude", "archive", "include", "restore")
PLATFORMS = sorted(p.value for p in PUBLISH_PLATFORMS)
CONFIRM_SECONDS = 600


def signer():
    return URLSafeTimedSerializer(get_settings().secret_key, salt="library-bulk-v1")


def context(form):
    profile_id = form_int(form, "profile_id", minimum=0, maximum=2**31 - 1)
    return {
        "profile_id": profile_id,
        "status": str(form.get("status", "active")) if form.get("status") in STATUS_FILTERS else "active",
        "page": query_int(str(form.get("page", "1")), default=1),
        "q": form_str(form, "q", max_len=200),
    }


def back_url(state):
    return "/library?" + urlencode({k: v for k, v in state.items() if v != ""})


def owned_scope(db, ctx, profile_id):
    if profile_id and not db.scalar(
        select(CreatorProfile.id).where(
            CreatorProfile.id == profile_id, CreatorProfile.workspace_id == ctx.workspace.id
        )
    ):
        raise HTTPException(404)


def rows(db, ctx, profile_id, ids, *, lock=False):
    query = (
        select(Video)
        .where(
            Video.id.in_(ids),
            Video.workspace_id == ctx.workspace.id,
            Video.profile_id.is_not_distinct_from(profile_id or None),
        )
        .order_by(Video.id)
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    result = list(db.scalars(query))
    if len(result) != len(ids):
        raise HTTPException(404)
    return result


def person_plans(db, ctx, profile_id, *, lock=False):
    if not profile_id:
        return []
    query = (
        select(ContentPlan)
        .where(ContentPlan.workspace_id == ctx.workspace.id, ContentPlan.profile_id == profile_id)
        .order_by(ContentPlan.id)
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return list(db.scalars(query))


def digest(items, plans):
    data = {
        "items": [
            [
                v.id,
                v.profile_id,
                v.status,
                v.updated_at,
                v.title,
                v.caption,
                v.overrides,
                v.media_kind,
                v.content_format,
                v.duplicate_of_id,
                v.encoded_path,
                v.original_path,
            ]
            for v in items
        ],
        "plans": [[p.id, p.revision, p.enabled] for p in plans],
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def proposed(video, action, scope):
    title = (video.title or video.caption or str(video.id))[:80]
    if video.status in (VideoStatus.IMPORTING, VideoStatus.PROCESSING, VideoStatus.DUPLICATE):
        raise FormError("bulk.unavailable", title=title)
    if action == "restore":
        if video.status != VideoStatus.ARCHIVED:
            raise FormError("bulk.archived_only")
        if video.duplicate_of_id or not (
            _file(video.encoded_path)
            or (video.media_kind == "text" and video.profile_id and content.caption(video).strip())
        ):
            raise FormError("bulk.restore_file", title=title)
        return VideoStatus.READY
    if action == "archive":
        return VideoStatus.ARCHIVED
    old = _excluded_platforms(video)
    selected = set(PLATFORMS if scope == "all" else [scope])
    new = old | selected if action == "exclude" else old - selected
    result = dict(video.overrides or {})
    if new == old:
        return result
    if new:
        result["exclude_platforms"] = sorted(new)
    else:
        result.pop("exclude_platforms", None)
    return result


def changes(items, action, scope):
    result = []
    for video in items:
        value = proposed(video, action, scope)
        current = video.status if action in {"archive", "restore"} else video.overrides or {}
        if value != current:
            result.append((video, value))
    return result


@router.post("/library/bulk/review")
def review(request: Request, db: DB, ctx: CurrentWorkspace, form: Form):
    try:
        state = context(form)
    except FormError:
        raise HTTPException(400) from None
    owned_scope(db, ctx, state["profile_id"])
    selected = form.getlist("video_ids")
    draft = {"action": str(form.get("action", ""))[:16], "scope": str(form.get("scope", "all"))[:16]}
    try:
        if (
            not 1 <= len(selected) <= PAGE_SIZE
            or any(
                not isinstance(v, str)
                or not v.isascii()
                or not v.isdigit()
                or len(v) > 10
                or not 1 <= int(v) <= 2**31 - 1
                for v in selected
            )
            or len(set(selected)) != len(selected)
        ):
            raise FormError("bulk.selection_invalid", max=PAGE_SIZE)
        draft["selected"] = [int(v) for v in selected]
        if draft["action"] not in ACTIONS or draft["scope"] not in ["all", *PLATFORMS]:
            raise FormError("bulk.action_invalid")
        items = rows(db, ctx, state["profile_id"], draft["selected"])
        if form_bool(form, "edit"):
            return _library_page(
                request,
                db,
                ctx,
                state["status"],
                str(state["page"]),
                state["profile_id"] or None,
                q=state["q"],
                bulk_draft=draft,
            )
        plans = person_plans(db, ctx, state["profile_id"])
        changed = changes(items, draft["action"], draft["scope"])
    except FormError as exc:
        return _library_page(
            request,
            db,
            ctx,
            state["status"],
            str(state["page"]),
            state["profile_id"] or None,
            q=state["q"],
            bulk_draft=draft,
            bulk_error=exc,
            response_status=422,
        )
    nonce = secrets.token_urlsafe(24)
    request.session["library_bulk_nonce"] = nonce
    token = signer().dumps(
        {
            "workspace": ctx.workspace.id,
            "user": ctx.user.id,
            "nonce": nonce,
            "state": state,
            "ids": draft["selected"],
            "action": draft["action"],
            "scope": draft["scope"],
            "digest": digest(items, plans),
        }
    )
    response = render(
        request,
        "library_bulk_review.html",
        {
            "nav": "library",
            "items": items,
            "confirmation": token,
            "state": state,
            "bulk": draft,
            "back": back_url(state),
            "preference_return": back_url(state),
            "changed_count": len(changed),
            "profile": db.get(CreatorProfile, state["profile_id"]) if state["profile_id"] else None,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def review_error(request, key, state=None, **params):
    back = back_url(state) if state else "/library"
    return render(
        request,
        "library_bulk_error.html",
        {
            "error": key,
            "error_params": params,
            "back": back,
            "preference_return": back,
        },
        status_code=409,
    )


@router.post("/library/bulk/apply")
def apply(request: Request, db: DB, ctx: CurrentWorkspace, form: Form):
    try:
        payload = signer().loads(form_str(form, "confirmation", max_len=8000), max_age=CONFIRM_SECONDS)
    except (BadSignature, FormError):
        return review_error(request, "bulk.expired")
    nonce = request.session.get("library_bulk_nonce", "")
    if (
        not isinstance(payload, dict)
        or payload.get("workspace") != ctx.workspace.id
        or payload.get("user") != ctx.user.id
        or not nonce
        or not secrets.compare_digest(nonce, payload.get("nonce", ""))
    ):
        return review_error(request, "bulk.expired")
    state = payload["state"]
    owned_scope(db, ctx, state["profile_id"])
    lock_workspace(db, ctx.workspace.id)
    plans = person_plans(db, ctx, state["profile_id"], lock=True)
    try:
        items = rows(db, ctx, state["profile_id"], payload["ids"], lock=True)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return review_error(request, "bulk.stale", state)
    if not secrets.compare_digest(payload["digest"], digest(items, plans)):
        return review_error(request, "bulk.stale", state)
    action, scope = payload["action"], payload["scope"]
    try:
        changed = changes(items, action, scope)
    except FormError as exc:
        return review_error(request, exc.key, state, **exc.params)
    for video, value in changed:
        if action in {"archive", "restore"}:
            video.status = value
            if action == "restore":
                video.error = None
                video.error_info = None
        else:
            video.overrides = value
    if changed:
        for plan in plans:
            plan.enabled = False
            plan.revision += 1
            cancel_pending(db, plan)
        if action in {"archive", "exclude"}:
            for record in db.scalars(
                select(ScheduledPost)
                .where(
                    ScheduledPost.workspace_id == ctx.workspace.id,
                    ScheduledPost.video_id.in_([v.id for v, _ in changed]),
                    ScheduledPost.status.in_((PostStatus.SCHEDULED, PostStatus.FAILED)),
                )
                .order_by(ScheduledPost.id)
                .with_for_update()
            ):
                if (
                    action == "archive" or scope == "all" or record.platform == Platform(scope)
                ) and not reached_platform(record.platform_post_id, record.publish_state):
                    record.status = PostStatus.CANCELED
                    record.error = (
                        "Library content was archived or excluded; this untouched reservation was canceled."
                    )
        log_activity(
            db,
            ctx.workspace.id,
            ActivityLevel.INFO,
            "library",
            "Bulk library preferences changed; no publication was requested.",
            actor=ctx.actor,
            msg_key=f"bulk.audit_{action}",
            msg_params={"count": len(changed), "action": action},
            video_ids=[v.id for v, _ in changed],
            action=action,
            scope=scope,
        )
    db.commit()
    request.session.pop("library_bulk_nonce", None)
    flash(
        request,
        "success",
        "bulk.no_change"
        if not changed
        else "bulk.saved_mixed"
        if len(changed) < len(items)
        else "bulk.saved",
        count=len(changed),
        unchanged=len(items) - len(changed),
    )
    # Archive/restore may empty the current page; return to page one of the same view.
    return RedirectResponse(
        back_url({**state, "page": 1 if action in {"archive", "restore"} else state["page"]}), 303
    )
