"""Acceptance of the Terms of Service and the Privacy Policy before first use.

YouTube Developer Policies III.A.2 require an API client to make users agree to a privacy policy
before they can access it. Dashboard users are created by the operator (CLI), so there is no sign-up
form: after the first sign-in every user sees a one-time consent screen (routes/consent.py), and no
other page works until it has been accepted.

Storage: User.terms_accepted_at and User.terms_version are the record (the version of the legal
texts that was accepted). Each acceptance also leaves an activity-log entry of the user's workspace
(category "auth", data event="terms_accepted") as an audit trail; the log is purged after 90 days,
the columns are not. A verified acceptance is cached in the signed session cookie only as a
speed-up; the stored record is authoritative.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from repeater.activity import log_activity
from repeater.models import ActivityLevel, User, WorkspaceMember
from repeater.timeutil import utcnow

# Version of the legal texts (the effective date shown on /privacy and /terms). Change it
# whenever either text changes in substance: every user is then asked to accept it again.
LEGAL_VERSION = "2026-10-15"
SESSION_KEY = "consent"
EVENT = "terms_accepted"


def _marker(user: User) -> str:
    return f"{user.id}:{LEGAL_VERSION}"


def _stored(user: User) -> bool:
    return user.terms_accepted_at is not None and user.terms_version == LEGAL_VERSION


def has_consented(session: MutableMapping[str, Any], db: Session, user: User) -> bool:
    """True when this user accepted the current version of the terms and privacy policy."""
    if not _stored(user):
        session.pop(SESSION_KEY, None)
        return False
    session[SESSION_KEY] = _marker(user)
    return True


def record_consent(session: MutableMapping[str, Any], db: Session, user: User) -> None:
    """Store the acceptance (timestamp + policy version) and commit nothing: the caller commits."""
    user.terms_accepted_at = utcnow()
    user.terms_version = LEGAL_VERSION
    workspace_id = db.execute(
        select(WorkspaceMember.workspace_id)
        .where(WorkspaceMember.user_id == user.id)
        .order_by(WorkspaceMember.workspace_id)
        .limit(1)
    ).scalar_one_or_none()
    log_activity(
        db,
        workspace_id,
        ActivityLevel.INFO,
        "auth",
        f"Accepted the Terms of Service and the Privacy Policy (version {LEGAL_VERSION}).",
        actor=user.email[:64],
        event=EVENT,
        user_id=user.id,
        policy_version=LEGAL_VERSION,
        accepted_at=utcnow().isoformat(),
        msg_key="activity.terms_accepted",
        msg_params={"version": LEGAL_VERSION},
    )
    session[SESSION_KEY] = _marker(user)
