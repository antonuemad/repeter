"""Manual invitation and recovery lifecycle. Callers authorize, commit, and deliver links.

Bearer secrets exist only in the issued result and URL fragment. The database stores digests;
redemption is serialized and never changes a pre-existing user's password via an invitation.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timedelta

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import case, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from repeater.config import get_settings
from repeater.crypto import decrypt_json, encrypt_json, random_token
from repeater.models import (
    AccessThrottle,
    PasswordReset,
    RecoveryRequest,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from repeater.timeutil import utcnow

INVITE_LIFETIME = timedelta(hours=72)
RESET_LIFETIME = timedelta(minutes=30)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class AccessError(ValueError):
    def __init__(self, key: str = "access.invalid_link"):
        self.key = key
        super().__init__(key)


def email_address(value: str) -> str:
    try:
        return validate_email(value, check_deliverability=False, allow_smtputf8=False).ascii_email.lower()
    except EmailNotValidError as exc:
        raise AccessError("access.invalid_email") from exc


def validate_password(password: str, confirmation: str) -> None:
    if not 15 <= len(password) <= 128:
        raise AccessError("access.password_length")
    if password != confirmation:
        raise AccessError("access.password_mismatch")


def digest(token: str) -> str:
    if not TOKEN_RE.fullmatch(token):
        raise AccessError()
    return hashlib.sha256(token.encode()).hexdigest()


def password_version(user: User) -> str:
    return hashlib.sha256(user.password_hash.encode()).hexdigest()


def link(kind: str, token: str) -> str:
    assert kind in ("invite", "reset-password")
    return f"{get_settings().base_url.rstrip('/')}/{kind}#token={token}"


def _lock(db: Session, scope: str) -> None:
    key = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def allow_attempt(
    db: Session, scope: str, value: str, maximum: int, window: timedelta, *, now: datetime | None = None
) -> bool:
    """Atomic persistent fixed-window limiter. Keys are HMACs, never raw addresses or IPs.

    Caller commits even a rejected request; no account lookup is needed for public recovery.
    """
    now = now or utcnow()
    key = hmac.new(
        get_settings().secret_key.encode(), f"{scope}:{value}".encode(), hashlib.sha256
    ).hexdigest()
    expired = AccessThrottle.window_start <= now - window
    stmt = insert(AccessThrottle).values(key_digest=key, window_start=now, count=1)
    stmt = stmt.on_conflict_do_update(
        index_elements=[AccessThrottle.key_digest],
        set_={
            "count": case((expired, 1), else_=func.least(AccessThrottle.count + 1, 100000)),
            "window_start": case((expired, now), else_=AccessThrottle.window_start),
        },
    ).returning(AccessThrottle.count)
    return db.execute(stmt).scalar_one() <= maximum


def issue_invitation(
    db: Session, workspace_id: int, email: str, role: str, actor_id: int
) -> tuple[WorkspaceInvitation, str]:
    email = email_address(email)
    if role not in ("owner", "editor"):
        raise AccessError("access.invalid_role")
    # All issue/redeem/revoke operations lock workspace before invitation. Email serialization
    # also covers invitations for the same address in two different workspaces.
    if (
        db.execute(
            select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()
        ).scalar_one_or_none()
        is None
    ):
        raise AccessError()
    _lock(db, "invite:" + email)
    if db.execute(select(User.id).where(func.lower(User.email) == email)).first():
        raise AccessError("access.email_unavailable")
    now = utcnow()
    db.execute(
        update(WorkspaceInvitation)
        .where(
            WorkspaceInvitation.workspace_id == workspace_id,
            WorkspaceInvitation.email == email,
            WorkspaceInvitation.used_at.is_(None),
            WorkspaceInvitation.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    token = random_token()
    invitation = WorkspaceInvitation(
        workspace_id=workspace_id,
        email=email,
        role=role,
        created_by_id=actor_id,
        token_hash=digest(token),
        expires_at=now + INVITE_LIFETIME,
    )
    db.add(invitation)
    db.flush()
    return invitation, link("invite", token)


def invitation_for(db: Session, token_hash: str) -> WorkspaceInvitation:
    row = db.execute(
        select(WorkspaceInvitation).where(WorkspaceInvitation.token_hash == token_hash)
    ).scalar_one_or_none()
    if row is None or row.used_at or row.revoked_at or row.expires_at <= utcnow():
        raise AccessError()
    return row


def redeem_invitation(db: Session, token_hash: str, hashed_password: str) -> User:
    row = invitation_for(db, token_hash)
    ws_id, email = row.workspace_id, row.email
    db.execute(select(Workspace.id).where(Workspace.id == ws_id).with_for_update()).scalar_one()
    _lock(db, "invite:" + email)
    db.refresh(row, with_for_update=True)
    row = invitation_for(db, token_hash)
    if db.execute(select(User.id).where(func.lower(User.email) == email)).first():
        raise AccessError("access.email_unavailable")
    workspace = db.get(Workspace, ws_id)
    user = User(email=email, password_hash=hashed_password, locale=workspace.locale)
    db.add(user)
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws_id, user_id=user.id, role=row.role))
    row.used_at = utcnow()
    db.flush()
    return user


def revoke_invitation(db: Session, workspace_id: int, invitation_id: int) -> bool:
    db.execute(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()).first()
    row = db.execute(
        select(WorkspaceInvitation)
        .where(WorkspaceInvitation.id == invitation_id, WorkspaceInvitation.workspace_id == workspace_id)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        return False
    if not row.used_at:
        row.revoked_at = utcnow()
    return True


def request_recovery(db: Session, email: str) -> RecoveryRequest:
    # Identical storage path for registered and unknown addresses; no User lookup or email send.
    row = RecoveryRequest(email_enc=encrypt_json({"email": email_address(email)}))
    db.add(row)
    db.flush()
    return row


def recovery_email(row: RecoveryRequest) -> str:
    return decrypt_json(row.email_enc)["email"]


def issue_reset(
    db: Session, user_id: int, actor_id: int | None, *, allow_platform_admin: bool = False
) -> tuple[PasswordReset, str]:
    user = db.execute(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if user is None or not user.is_active or (user.is_platform_admin and not allow_platform_admin):
        raise AccessError()
    now = utcnow()
    db.execute(
        update(PasswordReset)
        .where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None))
        .values(used_at=now)
    )
    token = random_token()
    row = PasswordReset(
        user_id=user.id,
        token_hash=digest(token),
        password_version=password_version(user),
        created_by_id=actor_id,
        expires_at=now + RESET_LIFETIME,
    )
    db.add(row)
    db.flush()
    return row, link("reset-password", token)


def reset_for(db: Session, token_hash: str) -> PasswordReset:
    row = db.execute(select(PasswordReset).where(PasswordReset.token_hash == token_hash)).scalar_one_or_none()
    if row is None or row.used_at or row.expires_at <= utcnow():
        raise AccessError()
    user = db.get(User, row.user_id)
    if (
        user is None
        or not user.is_active
        or not hmac.compare_digest(row.password_version, password_version(user))
    ):
        raise AccessError()
    return row


def redeem_reset(db: Session, token_hash: str, hashed_password: str) -> User:
    row = reset_for(db, token_hash)
    user = db.execute(
        select(User).where(User.id == row.user_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()
    db.refresh(row, with_for_update=True)
    reset_for(db, token_hash)
    user.password_hash = hashed_password
    db.execute(
        update(PasswordReset)
        .where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None))
        .values(used_at=utcnow())
    )
    return user


def purge_access_records(db: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    cutoff = now - timedelta(days=7)
    count = 0
    for model, condition in (
        (WorkspaceInvitation, WorkspaceInvitation.expires_at < cutoff),
        (PasswordReset, PasswordReset.expires_at < cutoff),
        (RecoveryRequest, RecoveryRequest.created_at < cutoff),
        (AccessThrottle, AccessThrottle.window_start < now - timedelta(days=1)),
    ):
        count += db.execute(delete(model).where(condition)).rowcount
    return count
