"""YouTube quota is charged per insert request, independent of the post's retry state."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from repeater.config import get_settings
from repeater.db import get_sessionmaker
from repeater.platforms.base import RateLimitedError

NOW = datetime(2026, 10, 15, 6, 59, tzinfo=UTC)  # just before Pacific midnight


def test_quota_ledger_survives_post_retry_reset_and_counts_pacific_days(db, monkeypatch):
    from repeater import upload_quota
    from repeater.models import YouTubeUploadStart

    monkeypatch.setattr(get_settings(), "youtube_daily_upload_quota", 2)
    upload_quota.reserve_start(None, now=NOW)
    upload_quota.reserve_start(None, now=NOW)
    with pytest.raises(RateLimitedError):
        upload_quota.reserve_start(None, now=NOW)
    upload_quota.reserve_start(None, now=NOW + timedelta(minutes=2))
    assert db.scalar(select(func.count()).select_from(YouTubeUploadStart)) == 3
    assert sorted(row.day.isoformat() for row in db.scalars(select(YouTubeUploadStart))) == [
        "2026-10-14",
        "2026-10-14",
        "2026-10-15",
    ]


def test_project_quota_circuit_blocks_other_workspaces_until_the_next_day(db):
    from repeater import upload_quota

    upload_quota.mark_exhausted(now=NOW)
    with pytest.raises(RateLimitedError):
        upload_quota.reserve_start(None, now=NOW)
    upload_quota.reserve_start(None, now=NOW + timedelta(minutes=2))


def test_concurrent_session_starts_cannot_exceed_the_project_limit(db, monkeypatch):
    from repeater import upload_quota
    from repeater.models import YouTubeUploadStart

    monkeypatch.setattr(get_settings(), "youtube_daily_upload_quota", 1)

    def reserve(_):
        try:
            upload_quota.reserve_start(None, now=NOW)
            return True
        except RateLimitedError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, range(2))) == [False, True]
    with get_sessionmaker()() as check:
        assert check.scalar(select(func.count()).select_from(YouTubeUploadStart)) == 1
