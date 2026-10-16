"""Guided setup uses synthetic accounts and denies all unmocked platform HTTP."""

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import Response
from sqlalchemy import func, select

from repeater.config import get_settings
from repeater.models import (
    AccountStatus,
    ContentPlan,
    CreatorProfile,
    Job,
    OAuthState,
    PlanTarget,
    Platform,
    ScheduledPost,
    Source,
    Workspace,
)
from repeater.platforms import override
from repeater.sources import drive
from repeater.timeutil import utcnow
from tests.test_content_plans import FOLDER, create_plan, folder_mock, save, video
from tests.test_web_oauth import FakeProvider, _meta_accounts
from tests.test_web_support import post, set_lang

pytest_plugins = ["tests.test_web_support"]


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def schedule_data(**changes):
    return {
        "wizard_step": "schedule",
        "timezone": "Africa/Cairo",
        "start_date": (utcnow().date() + timedelta(days=1)).isoformat(),
        "weekdays": [str(i) for i in range(7)],
        "times": ["10:00"],
        "ordering": "oldest",
        "rotation_mode": "repeat",
        "repeat_after_days": "7",
        **changes,
    }


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_guided_flow_keeps_context_and_never_publishes(web_owner, db, make_account, lang, no_external_http):
    set_lang(web_owner, lang)
    assert 'href="/setup"' in web_owner.get("/").text
    assert web_owner.get("/setup").status_code == 200
    response = post(web_owner, "/plans", {"name": "Synthetic ليلى", "wizard_step": "accounts"})
    plan = db.get(ContentPlan, int(response.headers["location"].split("/")[2]))
    assert response.headers["location"] == f"/plans/{plan.id}/setup/accounts"
    assert plan.rotation_mode == "repeat"
    account = make_account(Platform.FACEBOOK)
    gdrive = make_account(Platform.GDRIVE)
    assert 'aria-current="step"' in web_owner.get(response.headers["location"]).text
    response = save(web_owner, db, plan, "targets", {"wizard_step": "accounts", "targets": [str(account.id)]})
    assert response.headers["location"] == f"/plans/{plan.id}/setup/content"
    assert web_owner.get(response.headers["location"]).status_code == 200
    folder_mock(no_external_http)
    response = save(
        web_owner, db, plan, "source", {"wizard_step": "content", "drive_id": gdrive.id, "folder": FOLDER}
    )
    assert response.headers["location"] == f"/plans/{plan.id}/setup/content"
    db.refresh(plan)
    assert not db.get(Source, plan.source_id).enabled
    item = video(db, plan, "Synthetic first item")
    db.commit()
    assert f"/plans/{plan.id}/setup/content" in web_owner.get(f"/library/{item.id}").text
    response = save(web_owner, db, plan, "schedule", schedule_data())
    assert response.headers["location"] == f"/plans/{plan.id}/setup/preview"
    for step in ("accounts", "content", "schedule", "preview"):
        page = web_owner.get(f"/plans/{plan.id}/setup/{step}")
        assert page.status_code == 200
        assert f'<html lang="{lang}"' in page.text
        assert f'action="/plans/{plan.id}/execution"' not in page.text
    preview = web_owner.get(response.headers["location"])
    assert "Synthetic first item" in preview.text
    assert f"/plans/{plan.id}/setup/preview?page=2" in preview.text
    assert web_owner.get(f"/plans/{plan.id}/setup/preview?page=2").status_code == 200
    assert f"/plans/{plan.id}/setup/content" in web_owner.get(f"/library?profile_id={plan.profile_id}").text
    db.refresh(plan)
    assert plan.times == ["10:00"] and plan.repeat_after_days == 7 and not plan.enabled
    assert not db.scalar(select(func.count()).select_from(Job))
    assert not db.scalar(select(func.count()).select_from(ScheduledPost))
    assert not get_settings().publishing_enabled


@pytest.mark.parametrize(
    "step,purpose,provider", [("accounts", "meta", "meta"), ("content", "gdrive", "google")]
)
@pytest.mark.parametrize("cancel", [False, True])
def test_oauth_success_or_cancel_returns_to_same_step(web_owner, db, step, purpose, provider, cancel):
    plan = create_plan(web_owner, db)
    path = f"/plans/{plan.id}/setup/{step}"
    fake = FakeProvider(provider, False, _meta_accounts() if provider == "meta" else [])
    with override("oauth", provider, fake):
        response = web_owner.get(f"/connect/{purpose}", params={"return_to": path}, follow_redirects=False)
        state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
        response = web_owner.get(
            f"/oauth/{provider}/callback",
            params={"state": state, **({"error": "access_denied"} if cancel else {"code": "synthetic"})},
            follow_redirects=False,
        )
    assert response.headers["location"] == path
    assert web_owner.get(path).status_code == 200
    assert not db.scalar(select(func.count()).select_from(Job))
    assert not db.scalar(select(func.count()).select_from(PlanTarget))


def test_validation_keeps_step_and_schedule_is_atomic(web_owner, db, make_account):
    set_lang(web_owner, "en")
    plan = create_plan(web_owner, db)
    original = (plan.times, plan.weekdays, plan.rotation_mode, plan.revision)
    response = save(
        web_owner,
        db,
        plan,
        "schedule",
        schedule_data(times=["12:15", "18:30"], weekdays=["1", "3"], repeat_after_days="0"),
    )
    assert response.status_code == 422
    assert 'value="12:15"' in response.text and 'value="18:30"' in response.text
    assert 'name="repeat_after_days"' in response.text and 'value="0"' in response.text
    assert 'name="wizard_step" value="schedule"' in response.text
    db.refresh(plan)
    assert (plan.times, plan.weekdays, plan.rotation_mode, plan.revision) == original
    account = make_account(Platform.FACEBOOK)
    save(web_owner, db, plan, "targets", {"targets": [str(account.id)]})
    response = save(web_owner, db, plan, "targets", {"wizard_step": "accounts"})
    assert response.status_code == 422
    assert "Select at least one active page" in response.text
    assert 'name="targets" value="' + str(account.id) + '" checked' not in response.text
    assert db.scalar(select(func.count()).select_from(PlanTarget)) == 1
    old_revision = plan.revision
    save(web_owner, db, plan, "person", {"name": "Updated"})
    response = post(web_owner, f"/plans/{plan.id}/schedule", {"revision": old_revision, **schedule_data()})
    assert response.status_code == 422 and 'name="wizard_step" value="schedule"' in response.text


def test_folder_browser_errors_and_pagination_stay_in_content(web_owner, db, make_account, no_external_http):
    plan = create_plan(web_owner, db)
    gdrive = make_account(Platform.GDRIVE)
    set_lang(web_owner, "en")
    no_external_http.get(f"{drive.API_BASE}/files").mock(
        return_value=Response(
            200, json={"files": [{"id": FOLDER, "name": "<unsafe>"}], "nextPageToken": "next-page"}
        )
    )
    response = post(
        web_owner,
        f"/plans/{plan.id}/folders",
        {"wizard_step": "content", "drive_id": gdrive.id, "include_images": "1", "backfill": "12"},
    )
    assert response.status_code == 200
    assert "&lt;unsafe&gt;" in response.text and "next-page" in response.text
    assert response.text.count('name="wizard_step" value="content"') >= 3
    folder_mock(no_external_http, status=403)
    response = save(
        web_owner,
        db,
        plan,
        "source",
        {"wizard_step": "content", "drive_id": gdrive.id, "folder": FOLDER, "backfill": "12"},
    )
    assert response.status_code == 422 and FOLDER in response.text
    assert 'value="12"' in response.text
    db.refresh(plan)
    assert plan.source_id is None and not db.scalar(select(func.count()).select_from(Job))


def test_import_is_explicit_and_return_stays_in_wizard(web_owner, db, make_account, no_external_http):
    plan = create_plan(web_owner, db)
    gdrive = make_account(Platform.GDRIVE)
    folder_mock(no_external_http)
    save(web_owner, db, plan, "source", {"wizard_step": "content", "drive_id": gdrive.id, "folder": FOLDER})
    assert not db.scalar(select(func.count()).select_from(Job))
    response = save(web_owner, db, plan, "imports", {"wizard_step": "content", "action": "start"})
    assert response.headers["location"] == f"/plans/{plan.id}/setup/content"
    assert set(db.scalars(select(Job.kind))) == {"poll_source"}
    assert not db.scalar(select(func.count()).select_from(ScheduledPost))
    save(web_owner, db, plan, "imports", {"wizard_step": "content", "action": "pause"})
    db.refresh(plan)
    assert not db.get(Source, plan.source_id).enabled


def test_wizard_tenant_csrf_and_redirect_boundaries(web_owner, db, make_account):
    own = create_plan(web_owner, db)
    other = Workspace(name="Foreign")
    db.add(other)
    db.flush()
    person = CreatorProfile(workspace_id=other.id, name="Private person")
    db.add(person)
    db.flush()
    foreign = ContentPlan(
        workspace_id=other.id, profile_id=person.id, timezone="UTC", start_date=utcnow().date()
    )
    db.add(foreign)
    db.commit()
    for step in ("accounts", "content", "schedule", "preview"):
        assert web_owner.get(f"/plans/{foreign.id}/setup/{step}").status_code == 404
    assert web_owner.get(f"/plans/{own.id}/setup/unknown").status_code == 404
    assert web_owner.post(f"/plans/{own.id}/schedule", data=schedule_data()).status_code == 403
    assert save(web_owner, db, own, "schedule", schedule_data(wizard_step="accounts")).status_code == 400
    for path in (
        f"/plans/{foreign.id}/setup/accounts",
        f"/plans/{own.id}/setup/content?next=//evil.example",
        f"/plans/{own.id}/setup/unknown",
    ):
        with override("oauth", "meta", FakeProvider("meta", False, [])):
            web_owner.get("/connect/meta", params={"return_to": path}, follow_redirects=False)
        state = db.scalar(select(OAuthState).order_by(OAuthState.created_at.desc()))
        assert state.return_path is None
    inactive = make_account(Platform.FACEBOOK)
    inactive.status = AccountStatus.NEEDS_RECONNECT
    db.commit()
    assert (
        save(
            web_owner, db, own, "targets", {"wizard_step": "accounts", "targets": [str(inactive.id)]}
        ).status_code
        == 422
    )


def test_empty_preview_and_name_validation_do_not_fake_completion(web_owner, db):
    set_lang(web_owner, "en")
    response = post(web_owner, "/plans", {"wizard_step": "accounts", "name": " "})
    assert response.status_code == 422 and 'action="/plans"' in response.text
    assert not db.scalar(select(func.count()).select_from(ContentPlan))
    plan = create_plan(web_owner, db)
    page = web_owner.get(f"/plans/{plan.id}/setup/preview")
    assert page.status_code == 200
    assert "No ready content yet" in page.text
    assert f"/plans/{plan.id}/setup/accounts" in page.text
    assert f"/plans/{plan.id}/setup/content" in page.text
    assert not db.scalar(select(func.count()).select_from(Job))
