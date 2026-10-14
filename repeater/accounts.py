"""Connected-account service: token storage, refresh, connect and disconnect."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from repeater.activity import log_activity
from repeater.crypto import decrypt_json, encrypt_json
from repeater.models import (
    ACTIVE_POST_STATUSES,
    AccountStatus,
    ActivityLevel,
    ConnectedAccount,
    PostStatus,
    RotationRule,
    ScheduledPost,
    Source,
)
from repeater.platforms import get_provider_for_platform
from repeater.platforms.base import (
    AccountContext,
    AuthError,
    ConnectedAccountData,
    OAuthTokens,
    PlatformError,
)
from repeater.timeutil import utcnow

REFRESH_MARGIN = timedelta(minutes=15)


class AccountUnavailable(Exception):  # noqa: N818
    """The account cannot be used until the user reconnects it."""


def save_tokens(account: ConnectedAccount, tokens: OAuthTokens) -> None:
    account.token_enc = encrypt_json(tokens.to_dict())
    account.token_expires_at = tokens.expires_at
    if tokens.scopes:
        account.scopes = " ".join(sorted(set(tokens.scopes)))


def load_tokens(account: ConnectedAccount) -> OAuthTokens:
    if not account.token_enc:
        raise AccountUnavailable(f"account {account.id} has no stored tokens")
    return OAuthTokens.from_dict(decrypt_json(account.token_enc))


def account_context(account: ConnectedAccount) -> AccountContext:
    return AccountContext(
        account_id=account.id,
        platform=account.platform,
        external_id=account.external_id,
        tokens=load_tokens(account),
        extra=dict(account.extra or {}),
        display_name=account.display_name,
    )


def mark_needs_reconnect(session: Session, account: ConnectedAccount, reason: str) -> None:
    if account.status != AccountStatus.NEEDS_RECONNECT:
        log_activity(
            session,
            account.workspace_id,
            ActivityLevel.ERROR,
            "account",
            f"{account.platform} account '{account.display_name}' needs to be reconnected: {reason}",
            account_id=account.id,
        )
    account.status = AccountStatus.NEEDS_RECONNECT
    account.last_error = reason[:2000]


def fresh_context(
    session: Session, account: ConnectedAccount, *, now: datetime | None = None
) -> AccountContext:
    """Return a usable context, refreshing the access token when it is about to expire.
    Raises AccountUnavailable when the account is disabled or the grant is gone."""
    if account.status == AccountStatus.DISABLED:
        raise AccountUnavailable(f"account {account.id} is disconnected")
    ctx = account_context(account)
    if not ctx.tokens.expires_within(REFRESH_MARGIN, now):
        return ctx
    provider = get_provider_for_platform(account.platform)
    try:
        tokens = provider.refresh(ctx)
    except AuthError as exc:
        mark_needs_reconnect(session, account, exc.message)
        raise AccountUnavailable(exc.message) from exc
    save_tokens(account, tokens)
    if account.status == AccountStatus.NEEDS_RECONNECT:
        account.status = AccountStatus.ACTIVE
        account.last_error = None
    ctx.tokens = tokens
    return ctx


def upsert_connected_accounts(
    session: Session, workspace_id: int, items: list[ConnectedAccountData], *, actor: str = "user"
) -> list[ConnectedAccount]:
    """Create or update accounts returned by an OAuth exchange."""
    out: list[ConnectedAccount] = []
    for item in items:
        acc = session.execute(
            select(ConnectedAccount).where(
                ConnectedAccount.workspace_id == workspace_id,
                ConnectedAccount.platform == item.platform,
                ConnectedAccount.external_id == item.external_id,
            )
        ).scalar_one_or_none()
        created = acc is None
        if created:
            acc = ConnectedAccount(
                workspace_id=workspace_id, platform=item.platform, external_id=item.external_id
            )
            session.add(acc)
        acc.display_name = item.display_name[:255]
        acc.avatar_url = item.avatar_url
        acc.extra = {**(acc.extra or {}), **item.extra}
        acc.status = AccountStatus.ACTIVE
        acc.last_error = None
        save_tokens(acc, item.tokens)
        session.flush()
        log_activity(
            session,
            workspace_id,
            ActivityLevel.INFO,
            "account",
            f"{'Connected' if created else 'Reconnected'} {item.platform} account '{acc.display_name}'",
            actor=actor,
            account_id=acc.id,
        )
        out.append(acc)
    return out


def disconnect(
    session: Session, account: ConnectedAccount, *, actor: str = "user", revoke: bool = True
) -> None:
    """Revoke at the provider (best effort), wipe tokens, stop all work that uses the account."""
    if revoke and account.token_enc:
        try:
            get_provider_for_platform(account.platform).revoke(account_context(account))
        except (PlatformError, AccountUnavailable, Exception) as exc:  # noqa: BLE001 - best effort
            log_activity(
                session,
                account.workspace_id,
                ActivityLevel.WARNING,
                "account",
                f"Revocation at {account.platform} failed ({type(exc).__name__}); tokens deleted locally",
                account_id=account.id,
            )
    account.token_enc = None
    account.token_expires_at = None
    account.status = AccountStatus.DISABLED
    session.execute(
        update(ScheduledPost)
        .where(
            ScheduledPost.connected_account_id == account.id, ScheduledPost.status.in_(ACTIVE_POST_STATUSES)
        )
        .values(status=PostStatus.CANCELED, error="account disconnected")
    )
    session.execute(
        update(RotationRule).where(RotationRule.connected_account_id == account.id).values(enabled=False)
    )
    session.execute(update(Source).where(Source.connected_account_id == account.id).values(enabled=False))
    log_activity(
        session,
        account.workspace_id,
        ActivityLevel.INFO,
        "account",
        f"Disconnected {account.platform} account '{account.display_name}'",
        actor=actor,
        account_id=account.id,
    )


def accounts_due_for_refresh(
    session: Session, within: timedelta = timedelta(hours=2)
) -> list[ConnectedAccount]:
    cutoff = utcnow() + within
    return list(
        session.execute(
            select(ConnectedAccount).where(
                ConnectedAccount.status == AccountStatus.ACTIVE,
                ConnectedAccount.token_expires_at.isnot(None),
                ConnectedAccount.token_expires_at <= cutoff,
            )
        ).scalars()
    )
