"""Acceptance of the Terms of Service and the Privacy Policy before first use.

YouTube Developer Policies III.A.2 require an API client to make users agree to a privacy policy
before they can access it. Dashboard users are created by the operator (CLI), so there is no sign-up
form: after the first sign-in every user sees a one-time consent screen (routes/consent.py), and no
other page works until it has been accepted.

Storage: the consent timestamp and the policy version belong on the user. When the User model
has the columns `terms_accepted_at` and `terms_version` they are used. Until then (no schema
change is needed for this to work) the acceptance is an activity-log entry of the user's
workspace (category "auth", data event="terms_accepted", user_id, policy_version). The log is
purged after 90 days, which at worst asks the user to accept again. A verified acceptance is
cached in the signed session cookie only as a speed-up; the stored record is authoritative.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from repeater.activity import log_activity
from repeater.models import ActivityLevel, ActivityLog, User, WorkspaceMember
from repeater.timeutil import utcnow

# Version of the legal texts (the effective date shown on /privacy and /terms). Change it
# whenever either text changes in substance: every user is then asked to accept it again.
LEGAL_VERSION = "2026-10-14"
SESSION_KEY = "consent"
EVENT = "terms_accepted"


def _has_columns() -> bool:
    return hasattr(User, "terms_accepted_at") and hasattr(User, "terms_version")


def _marker(user: User) -> str:
    return f"{user.id}:{LEGAL_VERSION}"


def _stored(db: Session, user: User) -> bool:
    if _has_columns():
        return getattr(user, "terms_accepted_at", None) is not None and (
            getattr(user, "terms_version", None) == LEGAL_VERSION
        )
    found = db.execute(
        select(ActivityLog.id)
        .where(
            ActivityLog.category == "auth",
            ActivityLog.data["event"].astext == EVENT,
            ActivityLog.data["user_id"].astext == str(user.id),
            ActivityLog.data["policy_version"].astext == LEGAL_VERSION,
        )
        .limit(1)
    ).first()
    return found is not None


def has_consented(session: MutableMapping[str, Any], db: Session, user: User) -> bool:
    """True when this user accepted the current version of the terms and privacy policy."""
    if session.get(SESSION_KEY) == _marker(user):
        return True
    if not _stored(db, user):
        return False
    session[SESSION_KEY] = _marker(user)
    return True


def record_consent(session: MutableMapping[str, Any], db: Session, user: User) -> None:
    """Store the acceptance (timestamp + policy version) and commit nothing: the caller commits."""
    if _has_columns():
        user.terms_accepted_at = utcnow()  # type: ignore[attr-defined]
        user.terms_version = LEGAL_VERSION  # type: ignore[attr-defined]
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
