"""Read-only operator preflight. Configuration, connection, and live acceptance are distinct."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from repeater import backups
from repeater.config import get_settings
from repeater.models import (
    AccountStatus,
    ConnectedAccount,
    Job,
    JobStatus,
    Platform,
    PostStatus,
    ScheduledPost,
)
from repeater.timeutil import utcnow

PROVIDERS = {
    "youtube": "google",
    "gdrive": "google",
    "instagram": "meta",
    "facebook": "meta",
    "tiktok": "tiktok",
}


def report(db: Session, platforms: list[str] | None = None) -> dict:
    settings = get_settings()
    platforms = platforms or ["youtube", "instagram", "facebook", "tiktok"]
    entries = []
    for name in platforms:
        platform = Platform(name)
        provider = PROVIDERS[name]
        connected = db.scalar(
            select(func.count())
            .select_from(ConnectedAccount)
            .where(
                ConnectedAccount.platform == platform,
                ConnectedAccount.status == AccountStatus.ACTIVE,
                ConnectedAccount.token_enc.is_not(None),
            )
        )
        reconnect = db.scalar(
            select(func.count())
            .select_from(ConnectedAccount)
            .where(
                ConnectedAccount.platform == platform,
                ConnectedAccount.status == AccountStatus.NEEDS_RECONNECT,
            )
        )
        published = db.scalar(
            select(func.count())
            .select_from(ScheduledPost)
            .where(ScheduledPost.platform == platform, ScheduledPost.status == PostStatus.PUBLISHED)
        )
        configured = settings.platform_configured(provider)
        entries.append(
            {
                "platform": name,
                "provider": provider,
                "configured": configured,
                "active_accounts": connected,
                "needs_reconnect": reconnect,
                "confirmed_publications": published,
                "redirect_uri": settings.oauth_redirect_uri(provider),
                "state": "connected"
                if configured and connected
                else "needs_connection"
                if configured
                else "missing_credentials",
                "visibility_limit": "private_only"
                if name == "tiktok" and not settings.tiktok_audited
                else None,
                "acceptance": "manual_platform_test_required",
            }
        )
    backup_ok, backup_text = backups.status()
    oldest = db.scalar(
        select(func.min(Job.run_at)).where(Job.status == JobStatus.QUEUED, Job.run_at <= utcnow())
    )
    return {
        "platforms": entries,
        "telegram": {
            "configured": bool(settings.telegram_bot_token and settings.telegram_chat_id),
            "delivery": "run_test_alert_to_verify",
        },
        "backups": {
            "ok": backup_ok and bool(settings.backup_status_file),
            "summary": backup_text or "not configured",
            "receipts": backups.receipts(),
        },
        "queue": {
            "oldest_due_age_seconds": max(0, int((utcnow() - oldest).total_seconds())) if oldest else 0
        },
        "live_publishing_verified": False,
        "scope": "operator_preflight_not_launch_certification",
    }
