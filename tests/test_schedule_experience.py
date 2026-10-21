"""Schedule usability acceptance: synthetic people/history, denied provider HTTP, locked publishing."""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
import respx
from sqlalchemy import func, select

from repeater.config import get_settings
from repeater.content_plans import simulate
from repeater.models import (
    AccountStatus,
    ContentPlan,
    CreatorProfile,
    Job,
    PlanSlotOverride,
    ScheduledPost,
    VideoStatus,
    Workspace,
)
from repeater.timeutil import utcnow
from repeater.web.schedule_view import empty_day, gap_summary
from tests.test_content_plans import create_plan, save, video
from tests.test_plan_rotation import NOW, assignments, history, setup_plan
from tests.test_setup_wizard import schedule_data
from tests.test_web_support import post, set_lang

pytest_plugins = ["tests.test_web_support"]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    monkeypatch.setattr(get_settings(), "tiktok_audited", False)
    with respx.mock(assert_all_called=False) as mock:
        yield
        assert not mock.calls, "Schedule previews must never contact any provider"


def draft(**changes):
    return schedule_data(person_hub="1", **changes)


def live_plan(client, db, make_account, **kwargs):
    plan, accounts = setup_plan(client, db, make_account, **kwargs)
    plan.start_date = utcnow().date() + timedelta(days=1)
    db.commit()
    return plan, accounts


def assert_no_work(db):
    assert db.scalar(select(func.count()).select_from(Job)) == 0
    assert db.scalar(select(func.count()).select_from(ScheduledPost)) == 0
    assert not get_settings().publishing_enabled and not get_settings().tiktok_audited


@pytest.mark.parametrize("lang", ["en", "ar"])
@pytest.mark.parametrize("mode,days", [("daily", ["1"]), ("custom", ["1", "3"])])
def test_native_schedule_saves_all_choices_and_only_previews(web_owner, db, make_account, lang, mode, days):
    set_lang(web_owner, lang)
    plan, _ = live_plan(web_owner, db, make_account)
    video(db, plan, "Synthetic schedule item")
    response = save(
        web_owner,
        db,
        plan,
        "schedule",
        draft(day_mode=mode, weekdays=days, times=["19:15", "09:30", ""], ordering="newest"),
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/people/{plan.profile_id}?section=preview"
    db.refresh(plan)
    assert plan.weekdays == (list(range(7)) if mode == "daily" else [1, 3])
    assert plan.times == ["09:30", "19:15"] and plan.timezone == "Africa/Cairo"
    assert plan.ordering == "newest" and plan.repeat_after_days == 7 and not plan.enabled
    assert plan.schedule_reviewed_at
    schedule = web_owner.get(f"/people/{plan.profile_id}?section=schedule").text
    assert 'type="date" name="start_date"' in schedule and '<select name="timezone"' in schedule
    assert "data-schedule-form" in schedule and 'name="day_mode"' in schedule
    assert "data-schedule-summary" in schedule and "{weekly}" in schedule
    page = web_owner.get(response.headers["location"])
    assert page.status_code == 200 and "schedule.gap_" not in page.text
    assert f'lang="{lang}"' in page.text
    assert_no_work(db)


def test_once_does_not_require_inactive_repeat_field_and_preserves_it(web_owner, db, make_account):
    plan, _ = live_plan(web_owner, db, make_account, days=17)
    data = draft(rotation_mode="once", day_mode="daily")
    del data["repeat_after_days"]
    assert save(web_owner, db, plan, "schedule", data).status_code == 303
    db.refresh(plan)
    assert plan.repeat_after_days == 17 and plan.rotation_mode == "once"
    assert save(web_owner, db, plan, "schedule", draft(repeat_after_days="17")).status_code == 303
    db.refresh(plan)
    assert plan.rotation_mode == "repeat" and plan.repeat_after_days == 17
    assert_no_work(db)


@pytest.mark.parametrize(
    "changes,key",
    [
        ({"day_mode": "custom", "weekdays": []}, "schedule.days_invalid"),
        ({"day_mode": "custom", "weekdays": ["7"]}, "schedule.days_invalid"),
        ({"day_mode": "invalid"}, "schedule.days_invalid"),
        ({"times": ["10:00", "10:00"]}, "schedule.times_invalid"),
        ({"times": ["25:00"]}, "schedule.times_invalid"),
        ({"times": [""]}, "schedule.times_invalid"),
        ({"times": [f"{i:02}:00" for i in range(6)]}, "schedule.times_invalid"),
        ({"start_date": "2027-02-30"}, "schedule.start_invalid"),
        ({"timezone": "Mars/Olympus"}, "schedule.zone_invalid"),
        ({"repeat_after_days": "0"}, "plan.rotation_invalid"),
        ({"rotation_mode": "repeat", "repeat_after_days": ""}, "plan.rotation_invalid"),
    ],
)
@pytest.mark.parametrize("lang", ["en", "ar"])
def test_validation_names_problem_keeps_draft_and_is_atomic(web_owner, db, lang, changes, key):
    from repeater.web.plan_i18n import COPY

    set_lang(web_owner, lang)
    plan = create_plan(web_owner, db)
    before = (plan.timezone, plan.start_date, plan.times, plan.weekdays, plan.revision)
    response = save(web_owner, db, plan, "schedule", draft(**changes))
    assert response.status_code == 422 and COPY[key][lang == "ar"] in response.text
    assert 'name="person_hub" value="1"' in response.text
    assert f'action="/plans/{plan.id}/schedule"' in response.text
    db.refresh(plan)
    assert (plan.timezone, plan.start_date, plan.times, plan.weekdays, plan.revision) == before
    assert plan.schedule_reviewed_at is None
    assert_no_work(db)


@pytest.mark.parametrize(
    "case,key",
    [
        ("empty", "empty_library"),
        ("processing", "processing"),
        ("failed", "import_failed"),
        ("incompatible", "incompatible"),
        ("same_origin", "same_origin"),
        ("excluded", "excluded_content"),
        ("missing_file", "review_content"),
    ],
)
def test_empty_library_reasons_are_person_specific(web_owner, db, make_account, case, key):
    plan, (account,) = setup_plan(web_owner, db, make_account)
    foreign = create_plan(web_owner, db, "Other synthetic person")
    video(db, foreign, "Other person's ready item")
    if case != "empty":
        item = video(db, plan, "Synthetic item")
        if case == "processing":
            item.status = VideoStatus.PROCESSING
        if case == "failed":
            item.status = VideoStatus.FAILED
        if case == "incompatible":
            item.duration_s = 100000
        if case == "same_origin":
            item.source_account_id = account.id
        if case == "excluded":
            item.overrides = {"exclude_platforms": ["facebook"]}
        if case == "missing_file":
            item.encoded_path = None
    result = simulate(db, plan, now=NOW)
    assert result["filled"] == 0
    assert {entry.detail for _, entry in assignments(result, account)} == {key}
    summary = gap_summary(result)
    assert len(summary) == 1 and summary[0]["key"] == key and summary[0]["count"] == 30
    assert_no_work(db)


def test_once_used_history_is_exhausted_not_an_empty_library(web_owner, db, make_account):
    plan, (account,) = setup_plan(web_owner, db, make_account)
    plan.rotation_mode = "once"
    item = video(db, plan, "Already used (synthetic history)")
    history(db, plan, account, item, NOW - timedelta(days=30))
    result = simulate(db, plan, now=NOW)
    assert result["ready"] == 1 and result["filled"] == 0
    assert {entry.reason for _, entry in assignments(result, account)} == {"exhausted"}
    assert gap_summary(result)[0]["section"] == "schedule"
    assert db.scalar(select(func.count()).select_from(ScheduledPost)) == 1
    assert not db.scalar(select(func.count()).select_from(Job))


@pytest.mark.parametrize("lang", ["en", "ar"])
@pytest.mark.parametrize(
    "state,key,section",
    [
        ("cooldown", "cooldown", "content"),
        ("once", "exhausted", "schedule"),
        ("reconnect", "account_reconnect", "accounts"),
        ("disabled", "account_disconnected", "accounts"),
        ("processing", "processing", "content"),
        ("incompatible", "incompatible", "content"),
        ("excluded", "excluded", "slot"),
        ("replacement", "replacement_unavailable", "slot"),
    ],
)
def test_gap_recovery_summary_and_pages_in_both_languages(
    web_owner, db, make_account, lang, state, key, section
):
    set_lang(web_owner, lang)
    plan, (account,) = live_plan(web_owner, db, make_account, days=7)
    item = video(db, plan, "Synthetic eligible item")
    if state == "once":
        plan.rotation_mode = "once"
    if state == "reconnect":
        account.status = AccountStatus.NEEDS_RECONNECT
    if state == "disabled":
        account.status = AccountStatus.DISABLED
    if state == "processing":
        item.status = VideoStatus.PROCESSING
    if state == "incompatible":
        item.duration_s = 100000
    if state in {"excluded", "replacement"}:
        slot = simulate(db, plan)["rows"][0].at
        db.add(
            PlanSlotOverride(
                workspace_id=plan.workspace_id,
                plan_id=plan.id,
                account_id=account.id,
                slot_at=slot,
                scheduled_at=slot,
                excluded=state == "excluded",
                video_id=item.id if state == "replacement" else None,
                replacement=state == "replacement",
            )
        )
        if state == "replacement":
            item.status = VideoStatus.ARCHIVED
    db.commit()
    first = simulate(db, plan)
    before = [(r.at, [(e.video.id if e.video else None, e.reason) for e in r.entries]) for r in first["rows"]]
    prefix = f"/people/{plan.profile_id}?section=preview"
    for path in (
        prefix,
        prefix + "&view=gaps",
        prefix + "&view=gaps&page=2",
        f"/plans/{plan.id}",
        f"/plans/{plan.id}/setup/preview?view=gaps",
    ):
        response = web_owner.get(path)
        assert response.status_code == 200 and f'data-gap="{key}"' in response.text
        assert "schedule.gap_" not in response.text
        if section in {"accounts", "content", "schedule"} and path.startswith("/people/"):
            assert f"/people/{plan.profile_id}?section={section}" in response.text
        if section == "slot":
            assert f"/calendar/{plan.id}/{account.id}/edit?slot=" in response.text
    if state == "cooldown":
        text = web_owner.get(prefix + "&view=gaps").text
        assert ("earliest eligibility" if lang == "en" else "أقرب وقت للسماح") in text
        assert "section=preview&amp;view=gaps&amp;page=2" in text
    after = simulate(db, plan)
    assert before == [
        (r.at, [(e.video.id if e.video else None, e.reason) for e in r.entries]) for r in after["rows"]
    ]
    assert plan.repeat_after_days == 7 and not plan.enabled
    assert_no_work(db)


def test_gap_totals_precede_filter_and_paging_and_keep_partial_rows(web_owner, db, make_account):
    plan, (active, disconnected) = live_plan(web_owner, db, make_account, pages=2, days=1)
    disconnected.status = AccountStatus.DISABLED
    item = video(db, plan, "Synthetic filled page")
    db.commit()
    result = simulate(db, plan)
    assert result["filled"] == 30 and result["empty"] == 30
    assert gap_summary(result)[0]["count"] == 30
    response = web_owner.get(f"/people/{plan.profile_id}?section=preview&view=gaps&page=2")
    assert response.status_code == 200 and "30 موعد صفحة فاضي" in response.text
    assert item.title in response.text and active.display_name in response.text
    assert "section=preview&amp;view=gaps&amp;page=3" in response.text
    assert_no_work(db)


def test_no_gap_filter_and_invalid_filter_do_not_mislead(web_owner, db, make_account):
    set_lang(web_owner, "en")
    plan, _ = live_plan(web_owner, db, make_account, days=1)
    video(db, plan, "Synthetic daily item")
    db.commit()
    path = f"/people/{plan.profile_id}?section=preview"
    response = web_owner.get(path + "&view=gaps")
    assert "No gaps in this preview." in response.text
    assert web_owner.get(path + "&view=unexpected").status_code == 400
    assert web_owner.get(f"/plans/{plan.id}?view=unexpected").status_code == 400
    assert_no_work(db)


@pytest.mark.parametrize(
    "day,expected",
    [
        (date(2027, 1, 1), "before_start"),
        (date(2027, 1, 2), "elapsed"),
        (date(2027, 1, 4), "off_day"),
        (date(2027, 2, 3), "outside_window"),
        (date(2027, 1, 5), "no_upcoming"),
    ],
)
def test_blank_day_reasons(day, expected):
    plan = SimpleNamespace(timezone="UTC", start_date=date(2027, 1, 2), weekdays=[1], times=["10:00"])
    assert empty_day(plan, day, datetime(2027, 1, 3, 12, tzinfo=UTC)) == expected


def test_blank_dst_day_and_elapsed_times_are_explicit():
    plan = SimpleNamespace(
        timezone="America/New_York", start_date=date(2027, 3, 13), weekdays=list(range(7)), times=["02:30"]
    )
    now = datetime(2027, 3, 13, tzinfo=UTC)
    assert empty_day(plan, date(2027, 3, 14), now) == "clock_gap"
    plan.times = ["02:30", "03:30"]
    assert empty_day(plan, date(2027, 3, 14), now) == "no_upcoming"
    assert empty_day(plan, date(2027, 3, 13), now + timedelta(hours=12)) == "elapsed"


def test_calendar_uses_selected_person_zone_and_explains_off_days(web_owner, db, make_account, monkeypatch):
    set_lang(web_owner, "en")
    plan, _ = live_plan(web_owner, db, make_account)
    monkeypatch.setattr("repeater.content_plans.utcnow", lambda: NOW)
    monkeypatch.setattr("repeater.web.routes.calendar.utcnow", lambda: NOW)
    tomorrow = NOW.date() + timedelta(days=1)
    plan.start_date = tomorrow
    plan.timezone = "Pacific/Auckland"
    plan.weekdays = [tomorrow.weekday()]
    plan.times = ["00:30"]
    video(db, plan, "Synthetic zone boundary")
    db.commit()
    response = web_owner.get("/calendar", params={"person": plan.profile_id, "day": tomorrow.isoformat()})
    assert response.status_code == 200 and "Pacific/Auckland" in response.text
    assert "This weekday is not selected" in response.text
    assert "Synthetic zone boundary" in response.text
    assert_no_work(db)


def test_schedule_security_stale_tabs_and_foreign_calendar(web_owner, db):
    plan = create_plan(web_owner, db)
    data = {**draft(), "revision": str(plan.revision)}
    assert web_owner.post(f"/plans/{plan.id}/schedule", data=data).status_code == 403
    assert post(web_owner, f"/plans/{plan.id}/schedule", data).status_code == 303
    assert post(web_owner, f"/plans/{plan.id}/schedule", data).status_code == 422
    other = Workspace(name="Other synthetic workspace")
    db.add(other)
    db.flush()
    person = CreatorProfile(workspace_id=other.id, name="Foreign synthetic person")
    db.add(person)
    db.flush()
    foreign = ContentPlan(
        workspace_id=other.id, profile_id=person.id, timezone="UTC", start_date=utcnow().date()
    )
    db.add(foreign)
    db.commit()
    assert web_owner.get("/calendar?" + urlencode({"person": person.id})).status_code == 404
    assert web_owner.get(f"/people/{person.id}?section=preview&view=gaps").status_code == 404
    assert_no_work(db)


def test_no_slots_is_not_reported_as_full_coverage(web_owner, db, make_account, monkeypatch):
    set_lang(web_owner, "en")
    plan, _ = live_plan(web_owner, db, make_account)
    video(db, plan, "Synthetic content")
    db.commit()
    monkeypatch.setattr("repeater.content_plans.slots", lambda *args, **kwargs: [])
    response = web_owner.get(f"/people/{plan.profile_id}?section=preview&view=gaps")
    assert response.status_code == 200
    assert "No gaps in this preview." not in response.text
    assert "Review days and rotation" in response.text
    assert_no_work(db)


def test_calendar_keeps_owned_person_beyond_dropdown_limit(web_owner, db, make_account):
    plan, _ = live_plan(web_owner, db, make_account)
    person = db.get(CreatorProfile, plan.profile_id)
    person.name = "ZZZ synthetic selected person"
    for i in range(101):
        extra = CreatorProfile(workspace_id=plan.workspace_id, name=f"A synthetic {i}")
        db.add(extra)
        db.flush()
        db.add(
            ContentPlan(
                workspace_id=plan.workspace_id,
                profile_id=extra.id,
                timezone="UTC",
                start_date=plan.start_date,
            )
        )
    db.commit()
    response = web_owner.get("/calendar", params={"person": person.id})
    assert response.status_code == 200 and person.name in response.text
    assert f'value="{person.id}" selected' in response.text
    assert_no_work(db)


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_calendar_without_pages_does_not_imply_a_moved_time(web_owner, db, lang):
    set_lang(web_owner, lang)
    plan = create_plan(web_owner, db)
    plan.start_date = utcnow().date() + timedelta(days=1)
    db.commit()
    response = web_owner.get(
        "/calendar", params={"person": plan.profile_id, "day": plan.start_date.isoformat()}
    )
    assert response.status_code == 200
    assert ("Select pages for this person" if lang == "en" else "اختَر صفحات للشخص") in response.text
    assert_no_work(db)
