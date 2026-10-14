"""Schedule page and its skip / publish-now actions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from repeater.engine import actions
from repeater.models import PostStatus, ScheduledPost, Workspace
from repeater.timeutil import utcnow
from tests.test_web_support import Recorder, post, set_lang

pytest_plugins = ["tests.test_web_support"]


def _post(db, workspace_id, video, account, *, at, status=PostStatus.SCHEDULED, key=None, **fields):
    row = ScheduledPost(
        workspace_id=workspace_id,
        video_id=video.id,
        connected_account_id=account.id,
        platform=account.platform,
        idempotency_key=key or f"{video.id}:{account.id}:{at.timestamp()}",
        scheduled_at=at,
        status=status,
        **fields,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def setup(db, workspace, make_account, web_make_video):
    account = make_account("tiktok", display_name="TT")
    video = web_make_video(title="Rope trick")
    return account, video


def test_schedule_groups_next_48h_by_local_day(web_owner, db, workspace, setup, monkeypatch):
    account, video = setup
    fixed = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)  # 23:00 in Cairo (UTC+3)
    monkeypatch.setattr("repeater.web.routes.schedule.utcnow", lambda: fixed)
    _post(db, workspace.id, video, account, at=fixed + timedelta(minutes=30))  # 23:30 Wed (Cairo)
    _post(db, workspace.id, video, account, at=fixed + timedelta(hours=2))  # 01:00 Thu (Cairo)
    _post(db, workspace.id, video, account, at=fixed + timedelta(hours=60))  # outside 48 h
    set_lang(web_owner, "en")
    html = web_owner.get("/schedule").text
    assert "Wednesday 2026-10-14 · today" in html
    assert "Thursday 2026-10-15" in html
    assert "23:30" in html and "01:00" in html
    assert html.count('class="schedule-item"') == 2
    assert "Times are shown in Africa/Cairo." in html


def test_schedule_history_with_permalinks(web_owner, db, workspace, setup):
    account, video = setup
    now = utcnow()
    _post(
        db,
        workspace.id,
        video,
        account,
        at=now - timedelta(days=1),
        status=PostStatus.PUBLISHED,
        published_at=now - timedelta(days=1),
        permalink="https://www.tiktok.com/@magiclap/video/1",
    )
    _post(
        db,
        workspace.id,
        video,
        account,
        at=now - timedelta(days=2),
        status=PostStatus.NEEDS_ATTENTION,
        error="spam risk",
    )
    _post(
        db,
        workspace.id,
        video,
        account,
        at=now - timedelta(days=30),
        status=PostStatus.PUBLISHED,
        published_at=now - timedelta(days=30),
        permalink="https://www.tiktok.com/@magiclap/video/old",
    )
    set_lang(web_owner, "en")
    html = web_owner.get("/schedule").text
    assert 'href="https://www.tiktok.com/@magiclap/video/1"' in html
    assert "video/old" not in html
    assert "spam risk" in html and "Needs attention" in html


def test_schedule_ignores_unsafe_permalinks(web_owner, db, workspace, setup):
    account, video = setup
    _post(
        db,
        workspace.id,
        video,
        account,
        at=utcnow(),
        status=PostStatus.PUBLISHED,
        published_at=utcnow(),
        permalink="javascript:alert(1)",
    )
    assert "javascript:alert" not in web_owner.get("/schedule").text


def test_skip_calls_engine(web_owner, db, workspace, setup, monkeypatch):
    account, video = setup
    row = _post(db, workspace.id, video, account, at=utcnow() + timedelta(hours=1))
    recorder = Recorder()
    monkeypatch.setattr(actions, "skip_post", recorder)
    resp = post(web_owner, f"/schedule/{row.id}/skip", {"next": "/schedule"}, page="/schedule")
    assert resp.status_code == 303 and resp.headers["location"] == "/schedule"
    args, kwargs = recorder.calls[0]
    assert args[1].id == row.id and kwargs == {"actor": "admin@magic-lap.com"}
    set_lang(web_owner, "en")
    assert "The post was skipped." in web_owner.get("/schedule").text


def test_publish_now_calls_engine(web_owner, db, workspace, setup, monkeypatch):
    account, video = setup
    row = _post(db, workspace.id, video, account, at=utcnow() + timedelta(hours=5))
    recorder = Recorder()
    monkeypatch.setattr(actions, "publish_now", recorder)
    resp = post(web_owner, f"/schedule/{row.id}/publish-now", page="/schedule")
    assert resp.status_code == 303
    assert recorder.calls[0][0][1].id == row.id


def test_engine_refusal_is_shown(web_owner, db, workspace, setup, monkeypatch):
    account, video = setup
    row = _post(db, workspace.id, video, account, at=utcnow() + timedelta(hours=5))
    monkeypatch.setattr(
        actions,
        "publish_now",
        Recorder(error=ValueError("This post is uploading and cannot be published again from here.")),
    )
    post(web_owner, f"/schedule/{row.id}/publish-now", page="/schedule")
    # A known engine refusal is shown in the user's language (ux-compliance-8).
    assert "لا يمكن نشر هذا المنشور" in web_owner.get("/schedule").text
    post(web_owner, f"/schedule/{row.id}/publish-now", page="/schedule")
    set_lang(web_owner, "en")
    assert "cannot be published again from here" in web_owner.get("/schedule").text


def test_schedule_actions_are_workspace_scoped(web_owner, db, make_account, web_make_video, monkeypatch):
    other = Workspace(name="Other")
    db.add(other)
    db.commit()
    account = make_account("tiktok", workspace_id=other.id)
    video = web_make_video(workspace_id=other.id)
    row = _post(db, other.id, video, account, at=utcnow())
    recorder = Recorder()
    monkeypatch.setattr(actions, "skip_post", recorder)
    resp = post(web_owner, f"/schedule/{row.id}/skip", page="/schedule")
    assert resp.status_code == 404 and recorder.calls == []
    assert post(web_owner, "/schedule/999999/publish-now", page="/schedule").status_code == 404


def test_actions_need_csrf(web_owner, db, workspace, setup, monkeypatch):
    account, video = setup
    row = _post(db, workspace.id, video, account, at=utcnow())
    recorder = Recorder()
    monkeypatch.setattr(actions, "skip_post", recorder)
    assert web_owner.post(f"/schedule/{row.id}/skip", follow_redirects=False).status_code == 403
    assert recorder.calls == []


def test_skip_and_publish_buttons_only_for_actionable_posts(web_owner, db, workspace, setup):
    account, video = setup
    _post(db, workspace.id, video, account, at=utcnow() + timedelta(hours=1), status=PostStatus.UPLOADING)
    html = web_owner.get("/schedule").text
    assert "/skip" not in html
    _post(db, workspace.id, video, account, at=utcnow() + timedelta(hours=2))
    assert "/skip" in web_owner.get("/schedule").text


def test_platform_posts_render(web_owner, db, workspace, make_account, web_make_video):
    video = web_make_video()
    for platform in ("youtube", "instagram", "facebook"):
        account = make_account(platform)
        _post(db, workspace.id, video, account, at=utcnow() + timedelta(hours=1), key=f"k-{platform}")
    html = web_owner.get("/schedule").text
    for name in ("YouTube", "Instagram", "Facebook"):
        assert name in html
