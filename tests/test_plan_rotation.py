"""Rotation acceptance uses synthetic libraries and history, with all HTTP mocked."""

from datetime import UTC, date, datetime, timedelta

import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from repeater.config import get_settings
from repeater.content_plans import simulate
from repeater.models import (
    AccountStatus,
    ContentPlan,
    Job,
    Platform,
    PostStatus,
    ScheduledPost,
    VideoStatus,
    Workspace,
)
from tests.test_content_plans import create_plan, save, video
from tests.test_web_support import post, set_lang

pytest_plugins = ["tests.test_web_support"]
NOW = datetime(2027, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_real_http():
    with respx.mock(assert_all_called=False) as mock:
        yield mock
        assert not mock.calls, "Rotation must never contact a provider"


def setup_plan(client, db, make_account, *, pages=1, days=7):
    plan = create_plan(client, db)
    accounts = [make_account(Platform.FACEBOOK) for _ in range(pages)]
    assert save(client, db, plan, "targets", {"targets": [str(a.id) for a in accounts]}).status_code == 303
    plan.timezone = "UTC"
    plan.start_date = NOW.date()
    plan.times = ["10:00"]
    plan.rotation_mode = "repeat"
    plan.repeat_after_days = days
    db.commit()
    return plan, accounts


def history(db, plan, account, item, at, *, status=PostStatus.PUBLISHED, **kwargs):
    record = ScheduledPost(
        workspace_id=plan.workspace_id,
        video_id=item.id,
        connected_account_id=account.id,
        platform=account.platform,
        scheduled_at=at,
        status=status,
        idempotency_key=f"rotation-{item.id}-{account.id}-{status}-{at.isoformat()}",
        **kwargs,
    )
    db.add(record)
    db.flush()
    return record


def assignments(result, account):
    return [(r.at, e) for r in result["rows"] for e in r.entries if e.account.id == account.id]


def test_fair_rotation_uses_every_video_before_repeating(web_owner, db, make_account):
    p, (a,) = setup_plan(web_owner, db, make_account, days=2)
    items = [video(db, p, label) for label in ["A", "B", "C"]]
    result = simulate(db, p, now=NOW)
    choices = assignments(result, a)
    assert [e.video.id for _, e in choices] == [v.id for v in items] * 10
    assert result["filled"] == 30 and result["repeated"] == 27 and result["waiting"] == 0
    assert all(not e.repeated for _, e in choices[:3])
    assert choices[3][1].previous_at == choices[0][0]
    assert choices[3][1].previous_is_preview
    assert db.scalar(select(func.count()).select_from(ScheduledPost)) == 0
    assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_short_library_leaves_gaps_and_restarts_at_exact_cooldown(web_owner, db, make_account):
    p, accounts = setup_plan(web_owner, db, make_account, pages=2, days=7)
    v = video(db, p, "Only one")
    result = simulate(db, p, now=NOW)
    assert result["filled"] == 10 and result["repeated"] == 8 and result["waiting"] == 50
    for account in accounts:
        choices = assignments(result, account)
        assert [i for i, (_, e) in enumerate(choices) if e.video] == [0, 7, 14, 21, 28]
        assert all(e.video.id == v.id for _, e in choices if e.video)
        assert choices[1][1].reason == "cooldown"
        assert choices[1][1].available_at == choices[7][0]


def test_cooldown_is_independent_for_each_page_and_uses_actual_publish_time(web_owner, db, make_account):
    p, (a, b) = setup_plan(web_owner, db, make_account, pages=2, days=3)
    v = video(db, p, "Shared library")
    published = NOW - timedelta(days=1) + timedelta(hours=10)
    history(db, p, a, v, NOW - timedelta(days=90), published_at=published)
    # A larger cycle does not trump a more recent actual publication time.
    history(db, p, a, v, NOW - timedelta(days=10), published_at=NOW - timedelta(days=10), cycle=90)
    result = simulate(db, p, now=NOW)
    a_choices, b_choices = assignments(result, a), assignments(result, b)
    assert a_choices[0][1].video is None and b_choices[0][1].video.id == v.id
    assert a_choices[2][1].video.id == v.id
    assert a_choices[2][1].previous_at == published
    assert not a_choices[2][1].previous_is_preview


@pytest.mark.parametrize("ordering", ["oldest", "newest"])
def test_unused_first_then_least_recent_use_and_stable_order(web_owner, db, make_account, ordering):
    p, (a,) = setup_plan(web_owner, db, make_account, days=1)
    p.ordering = ordering
    old = video(db, p, "Old unused", source_published_at=NOW - timedelta(days=100))
    new = video(db, p, "New unused", source_published_at=NOW - timedelta(days=90))
    older_use = video(db, p, "Used a month ago")
    newer_use = video(db, p, "Used a week ago")
    history(db, p, a, older_use, NOW - timedelta(days=30))
    history(db, p, a, newer_use, NOW - timedelta(days=7))
    choices = assignments(simulate(db, p, now=NOW), a)
    unused = [old.id, new.id] if ordering == "oldest" else [new.id, old.id]
    assert [e.video.id for _, e in choices[:4]] == unused + [older_use.id, newer_use.id]


@pytest.mark.parametrize(
    "status,state,remote_id",
    [
        (PostStatus.SCHEDULED, {}, None),
        (PostStatus.UPLOADING, {}, None),
        (PostStatus.PROCESSING, {}, None),
        (PostStatus.FAILED, {}, None),
        (PostStatus.NEEDS_ATTENTION, {}, None),
        (PostStatus.CANCELED, {"_engine": {"may_be_live": True}}, None),
        (PostStatus.CANCELED, {"container": "fake-only"}, None),
        (PostStatus.CANCELED, {}, "synthetic-id"),
        (PostStatus.SKIPPED, {"_engine": {"processing_since": NOW.isoformat()}}, None),
    ],
)
def test_pending_and_unknown_results_never_get_recycled(
    web_owner, db, make_account, status, state, remote_id
):
    p, (a,) = setup_plan(web_owner, db, make_account, days=1)
    v = video(db, p, "Blocked")
    history(
        db, p, a, v, NOW - timedelta(days=100), status=status, publish_state=state, platform_post_id=remote_id
    )
    result = simulate(db, p, now=NOW)
    assert result["filled"] == 0 and result["blocked"] == 1
    assert all(e.reason == "pending" for _, e in assignments(result, a))


@pytest.mark.parametrize("status", [PostStatus.CANCELED, PostStatus.SKIPPED])
def test_confirmed_unsent_cancellation_or_skip_does_not_consume_video(web_owner, db, make_account, status):
    p, (a,) = setup_plan(web_owner, db, make_account)
    v = video(db, p, "Unsent")
    history(db, p, a, v, NOW - timedelta(hours=1), status=status)
    result = simulate(db, p, now=NOW)
    assert result["rows"][0].entries[0].video.id == v.id
    assert result["blocked"] == 0


@pytest.mark.parametrize(
    "day,times", [(date(2027, 3, 13), ["01:30", "03:30"]), (date(2027, 11, 6), ["01:30", "02:30"])]
)
def test_repeat_interval_is_elapsed_time_across_dst_and_multiple_daily_times(
    web_owner, db, make_account, day, times
):
    p, (a,) = setup_plan(web_owner, db, make_account, days=1)
    p.timezone, p.start_date, p.times = "America/New_York", day, times
    video(db, p, "DST test")
    now = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    choices = [(at, e) for at, e in assignments(simulate(db, p, now=now), a) if e.video]
    assert len(choices) > 20
    assert all(
        at - previous[0] >= timedelta(hours=24)
        for previous, (at, _) in zip(choices, choices[1:], strict=False)
    )


def test_repeat_off_remains_one_pass_and_preview_does_not_consume_history(web_owner, db, make_account):
    p, (a,) = setup_plan(web_owner, db, make_account, days=1)
    v = video(db, p, "One video")
    first = simulate(db, p, now=NOW)
    again = simulate(db, p, now=NOW)
    assert [(at, e.video.id) for at, e in assignments(first, a)] == [
        (at, e.video.id) for at, e in assignments(again, a)
    ]
    p.rotation_mode = "once"
    result = simulate(db, p, now=NOW)
    assert result["filled"] == 1 and result["repeated"] == 0
    assert result["rows"][1].entries[0].reason == "exhausted"
    history(db, p, a, v, NOW - timedelta(days=365))
    assert simulate(db, p, now=NOW)["filled"] == 0


def test_new_content_and_removed_or_paused_content_are_reflected_without_cached_assignments(
    web_owner, db, make_account
):
    p, (a,) = setup_plan(web_owner, db, make_account, days=1)
    old = video(db, p, "Existing")
    history(db, p, a, old, NOW - timedelta(days=30))
    assert simulate(db, p, now=NOW)["rows"][0].entries[0].video.id == old.id
    new = video(db, p, "Just prepared")
    assert simulate(db, p, now=NOW)["rows"][0].entries[0].video.id == new.id
    new.status = VideoStatus.ARCHIVED
    assert simulate(db, p, now=NOW)["rows"][0].entries[0].video.id == old.id
    db.delete(new)
    assert simulate(db, p, now=NOW)["ready"] == 1


def test_rotation_keeps_person_isolation_and_format_exclusions(web_owner, db, make_account):
    p, (a,) = setup_plan(web_owner, db, make_account, days=1)
    q = create_plan(web_owner, db, "Other person")
    video(db, q, "Private to the other person")
    good = video(db, p, "Allowed")
    video(db, p, "Origin page", source_account_id=a.id)
    video(db, p, "Excluded", overrides={"exclude_platforms": ["facebook"]})
    too_long = video(db, p, "Wrong format")
    too_long.duration_s = 100000
    result = simulate(db, p, now=NOW)
    assert result["ready"] == 4 and result["excluded"] == 3
    assert all(e.video.id == good.id for _, e in assignments(result, a))
    a.status = AccountStatus.NEEDS_RECONNECT
    result = simulate(db, p, now=NOW)
    assert result["filled"] == 0
    assert all(e.reason == "account_unavailable" for _, e in assignments(result, a))


def test_foreign_workspace_history_cannot_affect_rotation(web_owner, db, make_account):
    p, (a,) = setup_plan(web_owner, db, make_account, days=1)
    v = video(db, p, "Own content")
    ws = Workspace(name="Foreign workspace")
    db.add(ws)
    db.flush()
    # Malformed legacy rows are still filtered by the history reader's tenant boundary.
    row = history(db, p, a, v, NOW, status=PostStatus.NEEDS_ATTENTION)
    row.workspace_id = ws.id
    assert simulate(db, p, now=NOW)["filled"] == 30


@pytest.mark.parametrize("days", [1, 365])
def test_repeat_interval_boundaries_save_without_resetting_other_choices(web_owner, db, make_account, days):
    p, _ = setup_plan(web_owner, db, make_account)
    p.times, p.weekdays, p.ordering = ["19:45"], [2, 5], "newest"
    db.commit()
    assert (
        save(
            web_owner, db, p, "rotation", {"rotation_mode": "repeat", "repeat_after_days": str(days)}
        ).status_code
        == 303
    )
    db.refresh(p)
    assert (
        p.repeat_after_days == days
        and p.times == ["19:45"]
        and p.weekdays == [2, 5]
        and p.ordering == "newest"
    )


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("lang", ["ar", "en"])
def test_save_preview_and_disable_flow_is_safe_in_both_languages(
    web_owner, db, make_account, monkeypatch, enabled, lang
):
    monkeypatch.setattr(get_settings(), "publishing_enabled", enabled)
    p, _ = setup_plan(web_owner, db, make_account)
    video(db, p, "Synthetic rotation")
    db.commit()
    set_lang(web_owner, lang)
    r = save(web_owner, db, p, "rotation", {"rotation_mode": "repeat", "repeat_after_days": "2"})
    assert r.status_code == 303 and r.headers["location"] == f"/plans/{p.id}"
    db.refresh(p)
    assert p.rotation_mode == "repeat" and p.repeat_after_days == 2
    for path in [f"/plans/{p.id}", f"/plans/{p.id}?page=2", f"/plans/{p.id}/edit", "/plans"]:
        r = web_owner.get(path)
        assert r.status_code == 200 and f'lang="{lang}"' in r.text
        assert "plan.rotation" not in r.text
    assert (
        save(web_owner, db, p, "rotation", {"rotation_mode": "once", "repeat_after_days": "2"}).status_code
        == 303
    )
    db.refresh(p)
    assert p.rotation_mode == "once"
    assert db.scalar(select(func.count()).select_from(ScheduledPost)) == 0
    assert db.scalar(select(func.count()).select_from(Job)) == 0


@pytest.mark.parametrize(
    "data",
    [
        {"rotation_mode": "invalid", "repeat_after_days": "30"},
        {"rotation_mode": "repeat", "repeat_after_days": "0"},
        {"rotation_mode": "repeat", "repeat_after_days": "366"},
        {"rotation_mode": "repeat", "repeat_after_days": "1.5"},
        {"rotation_mode": "repeat", "repeat_after_days": ""},
        {"rotation_mode": "repeat", "repeat_after_days": "999999999999999999"},
    ],
)
def test_invalid_rotation_is_atomic_and_preserves_submitted_form(web_owner, db, data):
    p = create_plan(web_owner, db)
    response = save(web_owner, db, p, "rotation", data)
    assert response.status_code == 422
    db.refresh(p)
    assert p.rotation_mode == "once" and p.repeat_after_days == 30 and p.revision == 1
    assert f'value="{data["repeat_after_days"]}"' in response.text


def test_rotation_requires_csrf_and_rejects_stale_tab(web_owner, db):
    p = create_plan(web_owner, db)
    data = {"rotation_mode": "repeat", "repeat_after_days": "3", "revision": "1"}
    assert web_owner.post(f"/plans/{p.id}/rotation", data=data).status_code == 403
    assert post(web_owner, f"/plans/{p.id}/rotation", data).status_code == 303
    assert post(web_owner, f"/plans/{p.id}/rotation", {**data, "repeat_after_days": "1"}).status_code == 422
    db.refresh(p)
    assert p.repeat_after_days == 3


@pytest.mark.parametrize(
    "field,value", [("rotation_mode", "surprise"), ("repeat_after_days", 0), ("repeat_after_days", 366)]
)
def test_database_rejects_invalid_rotation(web_owner, db, field, value):
    p = create_plan(web_owner, db)
    setattr(p, field, value)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    assert db.get(ContentPlan, p.id).rotation_mode == "once"
