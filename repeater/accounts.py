"""Connected-account service: token storage, refresh, connect and disconnect."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from repeater.activity import log_activity
from repeater.crypto import decrypt_json, encrypt_json
from repeater.messages import Message, diagnostic
from repeater.models import (
    AccountStatus,
    ActivityLevel,
    ActivityLog,
    ConnectedAccount,
    Platform,
    PostStatus,
    RotationRule,
    ScheduledPost,
    Source,
    SourceKind,
    Video,
)
from repeater.platforms import get_provider_for_platform
from repeater.platforms.base import (
    AccountContext,
    AuthError,
    ConnectedAccountData,
    OAuthTokens,
    PlatformError,
)
from repeater.timeutil import ensure_aware, utcnow

REFRESH_MARGIN = timedelta(minutes=15)
# Engine-owned keys in ConnectedAccount.extra (never overwritten by an OAuth exchange).
DISCONNECTED_AT_KEY = "_disconnected_at"  # when the user disconnected; retention deletes the row later
REFRESHED_AT_KEY = "_refreshed_at"  # when platform API data (YouTube channel title, avatar) was fetched
GENERIC_CHANNEL_NAME = "YouTube channel"
DISCONNECTED_WHILE_PUBLISHING = Message(
    "account.disconnected_live",
    (
        "The account was disconnected while this post was being published; the video may be live. "
        "Check the account before publishing again."
    ),
)


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
    reason = diagnostic(reason, "account.reconnect_reason")
    if account.status != AccountStatus.NEEDS_RECONNECT:
        log_activity(
            session,
            account.workspace_id,
            ActivityLevel.ERROR,
            "account",
            Message(
                "account.reconnect",
                f"{account.platform} account '{account.display_name}' needs to be reconnected: {reason}",
                {"p0": account.platform, "p1": account.display_name, "p2": diagnostic(reason)},
            ),
            account_id=account.id,
        )
    account.status = AccountStatus.NEEDS_RECONNECT
    account.last_error = reason[:2000]


def fresh_context(
    session: Session, account: ConnectedAccount, *, now: datetime | None = None
) -> AccountContext:
    """Return a usable context, refreshing the access token when it is about to expire.
    Raises AccountUnavailable when the account is disabled or the grant is gone.

    A refresh runs under a row lock on the account, re-reading it first: a concurrent refresh
    (providers may rotate refresh tokens, so a second refresh with the old token would fail) or
    disconnect is seen instead of overwritten. Lock order: post rows first, account row second
    (disconnect takes them in the same order)."""
    if account.status == AccountStatus.DISABLED:
        raise AccountUnavailable(f"account {account.id} is disconnected")
    ctx = account_context(account)
    if not ctx.tokens.expires_within(REFRESH_MARGIN, now):
        return ctx
    locked = session.get(ConnectedAccount, account.id, with_for_update=True, populate_existing=True)
    if locked is None or locked.status == AccountStatus.DISABLED:
        raise AccountUnavailable(f"account {account.id} is disconnected")
    account = locked
    ctx = account_context(account)
    if not ctx.tokens.expires_within(REFRESH_MARGIN, now):
        return ctx  # someone else refreshed it while we waited for the lock
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
        extra = {**(acc.extra or {}), **item.extra}
        extra.pop(DISCONNECTED_AT_KEY, None)  # reconnected: retention no longer applies
        if Platform(item.platform) == Platform.YOUTUBE:
            extra[REFRESHED_AT_KEY] = utcnow().isoformat()  # the channel data was just fetched
        acc.extra = extra
        acc.status = AccountStatus.ACTIVE
        acc.last_error = None
        save_tokens(acc, item.tokens)
        session.flush()
        log_activity(
            session,
            workspace_id,
            ActivityLevel.INFO,
            "account",
            Message(
                "account.connected",
                f"{'Connected' if created else 'Reconnected'} {item.platform} account '{acc.display_name}'",
                {
                    "p0": "Connected" if created else "Reconnected",
                    "p1": item.platform,
                    "p2": acc.display_name,
                },
            ),
            actor=actor,
            account_id=acc.id,
        )
        out.append(acc)
    return out


def disconnect(
    session: Session, account: ConnectedAccount, *, actor: str = "user", revoke: bool = True
) -> None:
    """Revoke at the provider (best effort), wipe tokens, stop all work that uses the account.

    Posts that have not reached the platform (scheduled, failed and waiting for a retry) are
    canceled. Posts that are uploading or processing may already be live, so they become
    needs-attention ("may be live") and keep their platform id and state: they must block their
    cycle, or a reconnect would post the same video again. The account row itself is deleted by
    maintenance a few days later (see repeater.jobs.maintenance)."""
    if revoke and account.token_enc:
        try:
            get_provider_for_platform(account.platform).revoke(account_context(account))
        except (PlatformError, AccountUnavailable, Exception) as exc:  # noqa: BLE001 - best effort
            log_activity(
                session,
                account.workspace_id,
                ActivityLevel.WARNING,
                "account",
                Message(
                    "account.revoke_failed",
                    f"Revocation at {account.platform} failed ({type(exc).__name__}); tokens deleted locally",
                    {"p0": account.platform, "p1": type(exc).__name__},
                ),
                account_id=account.id,
            )
    _stop_posts(session, account)
    account.token_enc = None
    account.token_expires_at = None
    account.status = AccountStatus.DISABLED
    account.extra = {**(account.extra or {}), DISCONNECTED_AT_KEY: utcnow().isoformat()}
    session.execute(
        update(RotationRule).where(RotationRule.connected_account_id == account.id).values(enabled=False)
    )
    # yt-dlp sources never use an account (contract C6): disconnecting does not stop them.
    session.execute(
        update(Source)
        .where(Source.connected_account_id == account.id, Source.kind != SourceKind.YTDLP)
        .values(enabled=False)
    )
    log_activity(
        session,
        account.workspace_id,
        ActivityLevel.INFO,
        "account",
        Message(
            "account.disconnected",
            f"Disconnected {account.platform} account '{account.display_name}'",
            {"p0": account.platform, "p1": account.display_name},
        ),
        actor=actor,
        account_id=account.id,
    )


def _stop_posts(session: Session, account: ConnectedAccount) -> None:
    """Cancel the account's unstarted posts and flag the in-flight ones. Runs before the account
    row is changed so the lock order matches publish_post (post rows first, account second)."""
    from repeater.engine.publisher import mark_may_be_live

    posts = session.execute(
        select(ScheduledPost)
        .where(
            ScheduledPost.connected_account_id == account.id,
            ScheduledPost.status.in_(
                [
                    PostStatus.SCHEDULED,
                    PostStatus.FAILED,
                    PostStatus.UPLOADING,
                    PostStatus.PROCESSING,
                ]
            ),
        )
        .order_by(ScheduledPost.id)
        .with_for_update()
    ).scalars()
    for post in posts:
        if post.status in (PostStatus.UPLOADING, PostStatus.PROCESSING):
            post.status = PostStatus.NEEDS_ATTENTION
            post.error = DISCONNECTED_WHILE_PUBLISHING
            mark_may_be_live(post)
            log_activity(
                session,
                post.workspace_id,
                ActivityLevel.WARNING,
                "publish",
                Message(
                    "account.post_interrupted",
                    f"Post #{post.id}: {DISCONNECTED_WHILE_PUBLISHING}",
                    {"p0": post.id, "p1": diagnostic(DISCONNECTED_WHILE_PUBLISHING)},
                ),
                post_id=post.id,
                video_id=post.video_id,
                account_id=account.id,
                platform=str(post.platform),
            )
        else:
            post.status = PostStatus.CANCELED
            post.error = Message("account.disabled", "account disconnected", {})
    session.flush()


def disconnected_at(account: ConnectedAccount) -> datetime | None:
    """When the user disconnected the account (recorded by disconnect(); older rows fall back
    to the last change of the row)."""
    raw = (account.extra or {}).get(DISCONNECTED_AT_KEY)
    try:
        return ensure_aware(datetime.fromisoformat(raw)) if raw else ensure_aware(account.updated_at)
    except (TypeError, ValueError):
        return ensure_aware(account.updated_at)


def channel_data_refreshed_at(extra: dict | None, created_at: datetime | None) -> datetime:
    """When the YouTube channel data (title, avatar) of an account was last fetched from the API:
    stamped at connect and at each refresh; older rows count from their creation."""
    raw = (extra or {}).get(REFRESHED_AT_KEY)
    try:
        if raw:
            stamped = datetime.fromisoformat(raw)
            return stamped if stamped.tzinfo else stamped.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        pass
    return ensure_aware(created_at) or datetime.min.replace(tzinfo=UTC)


def redact_name_in_activity(
    session: Session, account: ConnectedAccount, name: str, *, before: datetime | None = None
) -> int:
    """Replace `name` in the activity entries about the account (those older than `before`, when
    given) with a generic label, in the stored English sentence and nested translation parameters: the title of a YouTube channel is API data that may
    not outlive its refresh. Returns the number of rows changed."""
    if len(name.strip()) < 2:
        return 0
    about_account = [
        ActivityLog.workspace_id == account.workspace_id,
        ActivityLog.data["account_id"].astext == str(account.id),
    ]
    if before is not None:
        about_account.append(ActivityLog.created_at < before)

    def redact(value):
        if isinstance(value, str):
            return value.replace(name, GENERIC_CHANNEL_NAME)
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    count = 0
    for entry in session.scalars(select(ActivityLog).where(*about_account).with_for_update()):
        text = entry.message.replace(name, GENERIC_CHANNEL_NAME)
        data = dict(entry.data or {})
        if "msg_params" in data:
            data["msg_params"] = redact(data["msg_params"])
        if text != entry.message or data != entry.data:
            entry.message, entry.data = text, data
            count += 1
    session.flush()
    return count


def delete_account_records(
    session: Session, account: ConnectedAccount, *, video_ids: list[int] | None = None
) -> dict[str, int]:
    """Erase what the account row and its history hold about the user: its activity-log entries
    (those carrying its id, those about the listed `video_ids`, and those whose text quotes its
    display name), its sources that need it, and the account row itself, which takes the account's
    posts (platform post ids, permalinks, adapter state) and rotation rule with it (foreign key
    cascade). Imported library videos are not touched (the caller passes `video_ids` when they are
    deleted too). Used by the retention job a few days after a disconnect and by Meta data
    deletion. Returns what was removed."""
    # Library imports outlive a disconnect. Preserve only the deletion key before the FK to
    # this account (and its sources) is lost, so a later callback can still erase the imports.
    meta_user = (account.extra or {}).get("fb_user_id")
    if account.platform in (Platform.INSTAGRAM, Platform.FACEBOOK) and meta_user:
        imported_sources = select(Source.id).where(Source.connected_account_id == account.id)
        session.execute(
            update(Video)
            .where(or_(Video.source_account_id == account.id, Video.source_id.in_(imported_sources)))
            .values(meta_deletion_user_id=str(meta_user))
        )
    conditions = [ActivityLog.data["account_id"].astext == str(account.id)]
    source_ids = list(
        session.execute(
            select(Source.id).where(
                Source.connected_account_id == account.id, Source.kind != SourceKind.YTDLP
            )
        ).scalars()
    )
    if video_ids:
        conditions.append(ActivityLog.data["video_id"].astext.in_([str(v) for v in video_ids]))
        conditions.append(ActivityLog.data["duplicate_of_id"].astext.in_([str(v) for v in video_ids]))
    if source_ids:
        conditions.append(ActivityLog.data["source_id"].astext.in_([str(i) for i in source_ids]))
    name = (account.display_name or "").strip()
    activity = session.execute(delete(ActivityLog).where(or_(*conditions))).rowcount or 0
    # Never infer row ownership from a name substring. Older unstructured messages can still
    # quote the name: redact the complete name there, without deleting another account's history.
    if len(name) >= 3:
        pattern = re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])")
        legacy = session.execute(
            select(ActivityLog).where(
                ActivityLog.workspace_id == account.workspace_id,
                ActivityLog.data["account_id"].astext.is_(None),
                ActivityLog.message.contains(name, autoescape=True),
            )
        ).scalars()
        for entry in legacy:
            entry.message = pattern.sub("[deleted account]", entry.message)
            params = (entry.data or {}).get("msg_params") or {}
            if params.get("name") == name:
                entry.data = {**entry.data, "msg_params": {**params, "name": "[deleted account]"}}
    posts = session.execute(
        select(ScheduledPost.id).where(ScheduledPost.connected_account_id == account.id)
    ).all()
    if source_ids:
        session.execute(delete(Source).where(Source.id.in_(source_ids)))
    session.execute(delete(ConnectedAccount).where(ConnectedAccount.id == account.id))
    session.expunge(account)
    return {"activity": int(activity), "posts": len(posts), "sources": len(source_ids)}


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
