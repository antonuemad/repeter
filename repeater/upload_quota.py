"""Durable project-wide YouTube insert accounting, charged before every HTTP attempt.

Reservations commit independently of the publishing transaction. A process crash between the
reservation and the request can conservatively consume a slot, but can never refund a request
that reached Google. No credentials, channel identifiers or media are stored here.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from repeater.config import get_settings
from repeater.db import session_scope
from repeater.models import YouTubeQuotaDay, YouTubeUploadStart
from repeater.platforms.base import RateLimitedError
from repeater.timeutil import utcnow

QUOTA_TZ = ZoneInfo("America/Los_Angeles")
EXHAUSTED_CODES = frozenset({"quotaExceeded", "dailyLimitExceeded"})


def _locked_day(session: Session, now: datetime) -> YouTubeQuotaDay:
    day = now.astimezone(QUOTA_TZ).date()
    session.execute(insert(YouTubeQuotaDay).values(day=day, exhausted=False).on_conflict_do_nothing())
    return session.scalars(select(YouTubeQuotaDay).where(YouTubeQuotaDay.day == day).with_for_update()).one()


def reserve_start(post_id: int | None, *, now: datetime | None = None) -> None:
    """Reserve exactly one insert attempt, or refuse it before any platform request."""
    now = now or utcnow()
    with session_scope() as session:
        quota = _locked_day(session, now)
        used = session.scalar(
            select(func.count()).select_from(YouTubeUploadStart).where(YouTubeUploadStart.day == quota.day)
        )
        if quota.exhausted or used >= get_settings().youtube_daily_upload_quota:
            reset = datetime.combine(quota.day + timedelta(days=1), time.min, QUOTA_TZ)
            wait = (reset.astimezone(UTC) - now).total_seconds() + 300
            raise RateLimitedError(
                "The YouTube project's daily upload quota is exhausted; waiting for Pacific midnight.",
                code="quotaExceeded",
                retry_after=max(300, wait),
            )
        session.add(YouTubeUploadStart(day=quota.day, post_id=post_id, started_at=now))


def mark_exhausted(*, now: datetime | None = None) -> None:
    """Google's exhaustion response stops new inserts for every workspace until reset."""
    with session_scope() as session:
        _locked_day(session, now or utcnow()).exhausted = True
