"""Job handlers of the rotation engine (kinds and payloads in repeater.jobs.kinds).

Handlers receive a fresh session; the worker commits it after a handler returns normally.
publish_post and poll_post commit their own progress and raise RetryLater when their own job has
to run again (a running job blocks re-enqueueing the same dedupe key).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from repeater import notify as notify_mod
from repeater.accounts import AccountUnavailable, accounts_due_for_refresh, disconnect, fresh_context
from repeater.activity import log_activity
from repeater.engine import captions, publisher, scheduler
from repeater.jobs import kinds
from repeater.jobs import maintenance as _maintenance  # noqa: F401 - registers the "maintenance" handler
from repeater.jobs.registry import handler
from repeater.media import storage
from repeater.models import (
    AccountStatus,
    ActivityLevel,
    ConnectedAccount,
    DataDeletionRequest,
    ScheduledPost,
    Source,
    Video,
)
from repeater.queue import RetryLater
from repeater.timeutil import utcnow

log = logging.getLogger("repeater.engine.jobs")

# Refresh tokens that expire within this window (accounts.accounts_due_for_refresh).
REFRESH_AHEAD = timedelta(hours=2)
# accounts.fresh_context refreshes tokens that expire within 15 minutes of `now`.
_FRESH_CONTEXT_MARGIN = timedelta(minutes=15)


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


def refresh_tokens(session: Session) -> dict[str, int]:
    """Refresh access tokens that expire soon. A revoked grant marks the account
    needs-reconnect (inside accounts.fresh_context) and sends a notification."""
    counts = {"refreshed": 0, "needs_reconnect": 0, "failed": 0}
    account_ids = [a.id for a in accounts_due_for_refresh(session, within=REFRESH_AHEAD)]
    session.commit()
    for account_id in account_ids:
        account = session.get(ConnectedAccount, account_id)
        if account is None or account.status != AccountStatus.ACTIVE:
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


# --------------------------------------------------------------------------- delete_user_data


@handler(kinds.DELETE_USER_DATA)
def handle_delete_user_data(session: Session, payload: dict) -> None:
    delete_user_data(session, int(payload["request_id"]))


def delete_user_data(session: Session, request_id: int) -> bool:
    """Carry out a Meta data-deletion request (PLAN.md 7.5): disconnect every account the
    Facebook user authorized (without calling Meta, which already removed the app), delete the
    videos imported from those accounts (files and rows) and the Meta data stored about them,
    then mark the request completed. Commits. Safe to run again after a failure."""
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
        ).scalars()
    )
    for account in accounts:
        if account.status != AccountStatus.DISABLED or account.token_enc:
            disconnect(session, account, actor="meta", revoke=False)
        removed = _delete_account_videos(session, account)
        _scrub_account(session, account)
        log_activity(
            session,
            account.workspace_id,
            ActivityLevel.INFO,
            "account",
            f"Deleted the data of a {captions.platform_name(account.platform)} account at Meta's request "
            f"({removed} imported video{'s' if removed != 1 else ''} removed).",
            actor="meta",
            account_id=account.id,
            request_id=request.id,
        )
    request.status = "completed"
    request.completed_at = utcnow()
    session.commit()
    return True


def _delete_account_videos(session: Session, account: ConnectedAccount) -> int:
    source_ids = select(Source.id).where(Source.connected_account_id == account.id)
    videos = session.execute(
        select(Video.id, Video.workspace_id).where(
            or_(Video.source_account_id == account.id, Video.source_id.in_(source_ids))
        )
    ).all()
    for video_id, workspace_id in videos:
        storage.delete_video_dir(workspace_id, video_id)
    if videos:
        session.execute(delete(Video).where(Video.id.in_([v.id for v in videos])))
    session.execute(delete(Source).where(Source.connected_account_id == account.id))
    return len(videos)


def _scrub_account(session: Session, account: ConnectedAccount) -> None:
    """Remove identifiers and profile data that came from Meta; keep the row so the history of
    what was published stays consistent (posts reference the account)."""
    account.external_id = f"deleted-{account.id}"
    account.display_name = f"Deleted {account.platform} account"
    account.avatar_url = None
    account.extra = {}
    account.scopes = ""
    account.last_error = None
    session.execute(
        ScheduledPost.__table__.update()
        .where(ScheduledPost.connected_account_id == account.id)
        .values(platform_post_id=None, permalink=None, publish_state={}, caption=None)
    )
