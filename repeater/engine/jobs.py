"""Job handlers of the rotation engine (kinds and payloads in repeater.jobs.kinds).

Handlers receive a fresh session; the worker commits it after a handler returns normally.
publish_post and poll_post commit their own progress and raise RetryLater when their own job has
to run again (a running job blocks re-enqueueing the same dedupe key).

refresh_tokens also keeps the YouTube channel data fresh (YouTube Developer Policies III.E.4.c:
stored API data is refreshed or deleted within 30 days): every REFRESH_YOUTUBE_EVERY the channel
title, avatar and ids are re-read with youtube.refresh_account_info; maintenance scrubs what could
not be refreshed in time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from repeater import notify as notify_mod
from repeater.accounts import (
    REFRESHED_AT_KEY,
    AccountUnavailable,
    accounts_due_for_refresh,
    channel_data_refreshed_at,
    delete_account_records,
    fresh_context,
    mark_needs_reconnect,
    redact_name_in_activity,
)
from repeater.activity import log_activity
from repeater.engine import captions, publisher, scheduler
from repeater.jobs import kinds
from repeater.jobs import maintenance as _maintenance  # noqa: F401 - registers the "maintenance" handler
from repeater.jobs.registry import handler
from repeater.media import storage
from repeater.models import (
    AccountStatus,
    ActivityLevel,
    ActivityLog,
    ConnectedAccount,
    DataDeletionRequest,
    Job,
    Platform,
    Source,
    SourceKind,
    Video,
)
from repeater.platforms.base import AuthError
from repeater.queue import RetryLater
from repeater.timeutil import ensure_aware, utcnow

log = logging.getLogger("repeater.engine.jobs")

# Refresh tokens that expire within this window (accounts.accounts_due_for_refresh).
REFRESH_AHEAD = timedelta(hours=2)
# accounts.fresh_context refreshes tokens that expire within 15 minutes of `now`.
_FRESH_CONTEXT_MARGIN = timedelta(minutes=15)
# YouTube channel data is re-read this often; the policy limit is 30 days.
REFRESH_YOUTUBE_EVERY = timedelta(days=25)


@handler(kinds.PLAN_WORKSPACE)
def handle_plan_workspace(session: Session, payload: dict) -> None:
    scheduler.plan_workspace(session, int(payload["workspace_id"]))
    session.commit()


@handler(kinds.PUBLISH_POST)
def handle_publish_post(session: Session, payload: dict) -> None:
    _raise_if_again(publisher.publish_post(session, int(payload["post_id"])))


@handler(kinds.POLL_POST)
def handle_poll_post(session: Session, payload: dict) -> None:
    _raise_if_again(publisher.poll_post(session, int(payload["post_id"])))


def _raise_if_again(step: publisher.StepResult) -> None:
    if step.retry_in is not None:
        raise RetryLater(step.retry_in, step.message[:500])


# --------------------------------------------------------------------------- refresh_tokens


@handler(kinds.REFRESH_TOKENS)
def handle_refresh_tokens(session: Session, payload: dict) -> None:
    refresh_tokens(session)
    refresh_youtube_channels(session)


def refresh_tokens(session: Session) -> dict[str, int]:
    """Refresh access tokens that expire soon. A revoked grant marks the account
    needs-reconnect (inside accounts.fresh_context) and sends a notification. Each account is
    re-read under a row lock first, so one that was disconnected (or refreshed by a publish)
    since the list was made is skipped instead of getting a token written back."""
    counts = {"refreshed": 0, "needs_reconnect": 0, "failed": 0}
    account_ids = [a.id for a in accounts_due_for_refresh(session, within=REFRESH_AHEAD)]
    session.commit()
    for account_id in account_ids:
        account = session.get(ConnectedAccount, account_id, with_for_update=True, populate_existing=True)
        if account is None or account.status != AccountStatus.ACTIVE or not account.token_enc:
            session.rollback()  # releases the lock
            continue
        try:
            fresh_context(session, account, now=utcnow() + REFRESH_AHEAD - _FRESH_CONTEXT_MARGIN)
        except AccountUnavailable:
            session.commit()
            counts["needs_reconnect"] += 1
            reason = account.last_error or "its authorization is no longer valid"
            notify_mod.notify(
                f"The {captions.platform_name(account.platform)} account '{account.display_name}' must be "
                f"reconnected: {reason}",
                workspace_id=account.workspace_id,
                level="error",
                category="account_reconnect",
            )
            continue
        except Exception as exc:  # noqa: BLE001 - one account's failure must not stop the others
            session.rollback()
            counts["failed"] += 1
            log.warning("token refresh for account %s failed (%s)", account_id, type(exc).__name__)
            continue
        session.commit()
        counts["refreshed"] += 1
    return counts


def refresh_youtube_channels(session: Session, now: datetime | None = None) -> dict[str, int]:
    """Re-read the channel title, avatar and ids of active YouTube accounts whose data is older
    than REFRESH_YOUTUBE_EVERY (youtube.refresh_account_info, i.e. channels.list(mine)), so the
    stored API data never ages past the 30 days the YouTube Developer Policies allow. When it keeps
    failing, maintenance scrubs the data at 30 days instead."""
    from repeater.platforms import youtube as youtube_mod

    counts = {"refreshed": 0, "failed": 0}
    refresher = youtube_mod.refresh_account_info
    now = ensure_aware(now) or utcnow()
    candidates = session.execute(
        select(ConnectedAccount.id, ConnectedAccount.extra, ConnectedAccount.created_at).where(
            ConnectedAccount.platform == Platform.YOUTUBE, ConnectedAccount.status == AccountStatus.ACTIVE
        )
    ).all()
    session.commit()
    due = [
        account_id
        for account_id, extra, created_at in candidates
        if channel_data_refreshed_at(extra, created_at) <= now - REFRESH_YOUTUBE_EVERY
    ]
    for account_id in due:
        account = session.get(ConnectedAccount, account_id, with_for_update=True, populate_existing=True)
        if account is None or account.status != AccountStatus.ACTIVE or not account.token_enc:
            session.rollback()
            continue
        try:
            info = refresher(fresh_context(session, account, now=now))
            _store_channel_info(session, account, info, now)
        except AccountUnavailable:
            session.commit()  # fresh_context already marked it needs-reconnect
            counts["failed"] += 1
            continue
        except AuthError as exc:
            mark_needs_reconnect(session, account, exc.message)
            session.commit()
            counts["failed"] += 1
            continue
        except Exception as exc:  # noqa: BLE001 - one channel's failure must not stop the others
            session.rollback()
            counts["failed"] += 1
            log.warning("channel refresh for account %s failed (%s)", account_id, type(exc).__name__)
            continue
        session.commit()
        counts["refreshed"] += 1
    return counts


def _store_channel_info(session: Session, account: ConnectedAccount, info: dict, now: datetime) -> None:
    """Apply a refresh_account_info result: display_name, avatar_url and extra. When the title
    changed, the old title is removed from the account's activity messages at once."""
    if not isinstance(info, dict) or not str(info.get("display_name") or "").strip():
        raise ValueError("channel info without a title")
    old_name = account.display_name
    account.display_name = str(info["display_name"]).strip()[:255]
    account.avatar_url = info.get("avatar_url") or None
    extra = {**(account.extra or {}), **{k: v for k, v in (info.get("extra") or {}).items() if v}}
    extra[REFRESHED_AT_KEY] = now.isoformat()
    account.extra = extra
    if old_name and old_name != account.display_name:
        redact_name_in_activity(session, account, old_name)


# --------------------------------------------------------------------------- delete_user_data


@handler(kinds.DELETE_USER_DATA)
def handle_delete_user_data(session: Session, payload: dict) -> None:
    delete_user_data(session, int(payload["request_id"]))


def delete_user_data(session: Session, request_id: int) -> bool:
    """Carry out a Meta data-deletion request (PLAN.md 7.5; retention C5(d)): delete every account
    the Facebook user authorized (the app is already removed at Meta, so nothing is revoked) with
    its posts and rotation rule, the videos imported from those accounts (files and rows), their
    sources, every activity-log entry that names the accounts or the deleted videos and the jobs
    whose payload references them (import jobs keep Graph download URLs), then mark the request
    completed. Commits. Safe to run again after a failure."""
    request = session.get(DataDeletionRequest, request_id, with_for_update=True)
    if request is None or request.status == "completed":
        session.rollback()
        return False
    external_user_id = str(request.external_user_id)
    accounts = list(
        session.execute(
            select(ConnectedAccount)
            .where(ConnectedAccount.extra["fb_user_id"].astext == external_user_id)
            .order_by(ConnectedAccount.id)
            .with_for_update()
        ).scalars()
    )
    for account in accounts:
        platform, workspace_id = account.platform, account.workspace_id
        video_ids = _delete_account_videos(session, account)
        _delete_jobs_about(session, account, video_ids)
        delete_account_records(session, account, video_ids=video_ids)
        # The summary names no account and carries no account id: it is not personal data.
        log_activity(
            session,
            workspace_id,
            ActivityLevel.INFO,
            "account",
            f"Deleted the data of a {captions.platform_name(platform)} account at Meta's request "
            f"({len(video_ids)} imported video{'s' if len(video_ids) != 1 else ''} removed).",
            actor="meta",
            request_id=request.id,
        )
    # Account retention deliberately keeps the imported library, with this minimal deletion
    # key. The account and source FKs may already be NULL when Meta sends its callback.
    detached = session.execute(
        select(Video.id, Video.workspace_id).where(Video.meta_deletion_user_id == external_user_id)
    ).all()
    _delete_videos(session, detached)
    request.status = "completed"
    request.completed_at = utcnow()
    session.commit()
    return True


def _delete_account_videos(session: Session, account: ConnectedAccount) -> list[int]:
    """Delete the videos imported from the account (files and rows); returns their ids."""
    source_ids = select(Source.id).where(Source.connected_account_id == account.id)
    videos = session.execute(
        select(Video.id, Video.workspace_id).where(
            or_(Video.source_account_id == account.id, Video.source_id.in_(source_ids))
        )
    ).all()
    return _delete_videos(session, videos)


def _delete_videos(session: Session, videos) -> list[int]:
    """Erase files, rows and references, including notices quoting another video's caption."""
    for video_id, workspace_id in videos:
        storage.delete_video_dir(workspace_id, video_id)
    ids = [int(video_id) for video_id, _ in videos]
    if ids:
        text_ids = [str(i) for i in ids]
        session.execute(
            delete(ActivityLog).where(
                or_(
                    ActivityLog.data["video_id"].astext.in_(text_ids),
                    ActivityLog.data["duplicate_of_id"].astext.in_(text_ids),
                )
            )
        )
        session.execute(delete(Job).where(Job.payload["video_id"].astext.in_(text_ids)))
        session.execute(delete(Video).where(Video.id.in_(ids)))  # their posts cascade
    return ids


def _delete_jobs_about(session: Session, account: ConnectedAccount, video_ids: list[int]) -> None:
    """Delete the jobs (any status) whose payload points at the deleted videos or sources."""
    source_ids = [
        str(i)
        for i in session.execute(
            select(Source.id).where(
                Source.connected_account_id == account.id, Source.kind != SourceKind.YTDLP
            )
        ).scalars()
    ]
    conditions = []
    if video_ids:
        conditions.append(Job.payload["video_id"].astext.in_([str(v) for v in video_ids]))
    if source_ids:
        conditions.append(Job.payload["source_id"].astext.in_(source_ids))
    if conditions:
        session.execute(delete(Job).where(or_(*conditions)))
