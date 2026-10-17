"""Customer setup continuity, isolated to synthetic content and mocked platforms."""

import pytest
import respx
from sqlalchemy import func, select

from repeater.config import get_settings
from repeater.models import (
    ContentPlan,
    CreatorProfile,
    Job,
    PlanTarget,
    Platform,
    ScheduledPost,
    Video,
    Workspace,
    WorkspaceMember,
)
from tests.test_content_plans import create_plan, save, video
from tests.test_mixed_media import raster
from tests.test_web_support import csrf_token, post, set_lang

pytest_plugins = ["tests.test_web_support"]


@pytest.fixture(autouse=True)
def locked_mock_platforms(monkeypatch):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    with respx.mock(assert_all_called=False) as mock:
        yield mock
        assert not mock.calls


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_resume_and_navigation_follow_the_selected_person(web_owner, db, make_account, lang):
    set_lang(web_owner, lang)
    first = create_plan(web_owner, db, "First synthetic creator")
    second = create_plan(web_owner, db, "Second synthetic creator")
    video(db, first, "First creator item")
    video(db, second, "Second creator item")
    account = make_account(Platform.FACEBOOK)
    save(web_owner, db, first, "targets", {"targets": [str(account.id)]})
    db.commit()
    web_owner.get(f"/plans/{first.id}/setup/content")
    page = web_owner.get("/library")
    assert "First creator item" in page.text and "Second creator item" not in page.text
    assert f'href="/calendar?person={first.profile_id}"' in page.text
    home = web_owner.get("/")
    assert f'href="/plans/{first.id}/setup/preview"' in home.text
    assert f'href="/plans/{second.id}/setup/preview"' not in home.text
    first.times = []
    db.commit()
    people = web_owner.get("/plans")
    assert "built-in method" not in people.text
    assert f'href="/plans/{first.id}/setup/schedule"' in people.text
    assert f'href="/plans/{second.id}/setup/accounts"' in people.text
    assert f'href="/plans/{first.id}/setup/schedule"' in web_owner.get("/").text
    web_owner.get(f"/calendar?person={second.profile_id}")
    assert "Second creator item" in web_owner.get("/library").text
    assert not db.scalar(select(func.count()).select_from(Job))
    assert not db.scalar(select(func.count()).select_from(ScheduledPost))


@pytest.mark.parametrize("kind", ["text", "upload"])
def test_inline_content_returns_to_owned_setup_without_publishing(web_owner, db, kind):
    plan = create_plan(web_owner, db)
    fields = {
        "profile_id": str(plan.profile_id),
        "setup_plan_id": str(plan.id),
        "title": "Synthetic inline item",
        "caption": "Sample body",
    }
    if kind == "text":
        response = post(web_owner, "/library/text", fields)
    else:
        response = web_owner.post(
            "/library/upload",
            data={
                "csrf_token": csrf_token(web_owner),
                **fields,
                "format": "image_post",
            },
            files={"file": ("example.png", raster(), "image/png")},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert response.headers["location"] == f"/plans/{plan.id}/setup/content"
    item = db.scalar(select(Video).where(Video.profile_id == plan.profile_id))
    assert item.title == fields["title"] and item.workspace_id == plan.workspace_id
    assert not db.scalar(select(func.count()).select_from(ScheduledPost))
    assert not db.scalar(select(func.count()).select_from(Job).where(Job.kind != "process_video"))
    db.refresh(plan)
    assert not plan.enabled and not get_settings().publishing_enabled


@pytest.mark.parametrize("kind", ["text", "upload"])
def test_inline_error_keeps_draft_person_and_recovery_form(web_owner, db, kind):
    plan = create_plan(web_owner, db)
    fields = {
        "profile_id": str(plan.profile_id),
        "setup_plan_id": str(plan.id),
        "title": '<unsafe>" synthetic',
        "caption": "",
    }
    if kind == "text":
        response = post(web_owner, "/library/text", fields)
    else:
        response = web_owner.post(
            "/library/upload",
            data={
                "csrf_token": csrf_token(web_owner),
                **fields,
                "format": "invalid",
            },
            files={"file": ("example.png", raster(), "image/png")},
            follow_redirects=False,
        )
    assert response.status_code == 422
    assert "&lt;unsafe&gt;" in response.text and "<unsafe>" not in response.text
    assert f'name="setup_plan_id" value="{plan.id}"' in response.text
    assert f'name="profile_id" value="{plan.profile_id}"' in response.text
    assert f'href="/plans/{plan.id}/setup/schedule"' in response.text
    assert not db.scalar(select(func.count()).select_from(Video))
    assert not db.scalar(select(func.count()).select_from(Job))


@pytest.mark.parametrize("kind", ["text", "upload"])
def test_inline_return_rejects_other_person_and_foreign_plan(web_owner, db, kind):
    own = create_plan(web_owner, db)
    second = create_plan(web_owner, db, "Other owned person")
    foreign_ws = Workspace(name="Foreign workspace")
    db.add(foreign_ws)
    db.flush()
    person = CreatorProfile(workspace_id=foreign_ws.id, name="Private person")
    db.add(person)
    db.flush()
    foreign = ContentPlan(
        workspace_id=foreign_ws.id, profile_id=person.id, timezone="UTC", start_date=own.start_date
    )
    db.add(foreign)
    db.commit()
    for raw, status in (
        (str(second.id), 404),
        (str(foreign.id), 404),
        ("//evil", 400),
        ("9999999999", 400),
        ("0", 400),
    ):
        fields = {
            "profile_id": str(own.profile_id),
            "setup_plan_id": raw,
            "title": "Synthetic",
            "caption": "Body",
        }
        if kind == "text":
            response = post(web_owner, "/library/text", fields)
        else:
            response = web_owner.post(
                "/library/upload",
                data={
                    "csrf_token": csrf_token(web_owner),
                    **fields,
                    "format": "image_post",
                },
                files={"file": ("example.png", raster(), "image/png")},
                follow_redirects=False,
            )
        assert response.status_code == status
    assert not db.scalar(select(func.count()).select_from(Video))
    assert not db.scalar(select(func.count()).select_from(Job))
    assert not db.scalar(select(func.count()).select_from(PlanTarget))


def test_deleted_selection_never_leaks_or_breaks_library(web_owner, db):
    plan = create_plan(web_owner, db)
    web_owner.get(f"/plans/{plan.id}/setup/content")
    db.delete(db.get(CreatorProfile, plan.profile_id))
    db.commit()
    page = web_owner.get("/library")
    assert page.status_code == 200
    assert f"?profile_id={plan.profile_id}" not in page.text


def test_selection_is_revalidated_when_workspace_membership_changes(web_owner, db, admin_user, workspace):
    plan = create_plan(web_owner, db, "Old workspace private person")
    video(db, plan, "Old workspace private content")
    db.commit()
    web_owner.get(f"/plans/{plan.id}/setup/content")
    other = Workspace(name="New workspace", paused=True)
    db.add(other)
    db.flush()
    member = db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.user_id == admin_user.id,
            WorkspaceMember.workspace_id == workspace.id,
        )
    )
    member.workspace_id = other.id
    db.commit()
    response = web_owner.get("/library")
    assert response.status_code == 200
    assert "Old workspace private" not in response.text
    assert web_owner.get(f"/library?profile_id={plan.profile_id}").status_code == 404


def test_locked_library_and_detail_offer_no_publish_controls(web_owner, db, make_account, workspace):
    make_account(Platform.FACEBOOK)
    from repeater.models import VideoStatus

    item = Video(workspace_id=workspace.id, title="Synthetic unassigned", status=VideoStatus.READY)
    db.add(item)
    db.commit()
    for path in ("/library?profile_id=0", f"/library/{item.id}"):
        response = web_owner.get(path)
        assert response.status_code == 200
        assert f'action="/library/{item.id}/publish-now"' not in response.text
