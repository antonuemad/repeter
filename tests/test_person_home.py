"""One-person customer journey: synthetic identities, mocked HTTP, no publishing or drafts."""

from urllib.parse import parse_qs, urlparse

import pytest
import respx
from sqlalchemy import func, select

from repeater.config import get_settings
from repeater.models import AccountStatus, ContentPlan, CreatorProfile, Job, ScheduledPost, Source, Workspace
from repeater.platforms import override
from repeater.timeutil import utcnow
from repeater.web.journey import progress
from tests.test_content_plans import FOLDER, create_plan, folder_mock, save, video
from tests.test_mixed_media import raster
from tests.test_setup_wizard import schedule_data
from tests.test_web_oauth import FakeProvider, _meta_accounts
from tests.test_web_support import csrf_token, post, set_lang

pytest_plugins = ["tests.test_web_support"]


@pytest.fixture(autouse=True)
def locked_simulation(monkeypatch):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    monkeypatch.setattr(get_settings(), "tiktok_audited", False)
    with respx.mock(assert_all_called=False) as mock:
        yield mock
    assert not get_settings().publishing_enabled


def hub(plan, section="accounts"):
    return f"/people/{plan.profile_id}?section={section}"


def safe(db):
    assert not db.scalar(select(func.count()).select_from(ScheduledPost))
    assert not list(
        db.scalars(select(Job).where(Job.kind.in_(["publish_post", "poll_post", "plan_workspace"])))
    )
    assert not any(db.scalars(select(ContentPlan.enabled)))


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_person_home_connect_select_content_review_and_preview(web_owner, db, lang, locked_simulation):
    set_lang(web_owner, lang)
    start = web_owner.get("/setup")
    assert 'name="person_hub" value="1"' in start.text
    response = post(
        web_owner, "/plans", {"name": "Synthetic ليلى", "wizard_step": "accounts", "person_hub": "1"}
    )
    plan = db.scalar(select(ContentPlan))
    assert response.headers["location"] == hub(plan)
    fake = FakeProvider("meta", False, _meta_accounts())
    with override("oauth", "meta", fake):
        response = web_owner.get("/connect/meta", params={"return_to": hub(plan)}, follow_redirects=False)
        state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
        response = web_owner.get(
            "/oauth/meta/callback", params={"state": state, "code": "synthetic"}, follow_redirects=False
        )
    assert response.headers["location"] == hub(plan)
    account_id = _meta_accounts()[0].external_id
    from repeater.models import ConnectedAccount

    account = db.scalar(select(ConnectedAccount).where(ConnectedAccount.external_id == account_id))
    response = save(
        web_owner,
        db,
        plan,
        "targets",
        {"wizard_step": "accounts", "person_hub": "1", "targets": [str(account.id)]},
    )
    assert response.headers["location"] == hub(plan, "content")
    response = post(
        web_owner,
        "/library/text",
        {
            "profile_id": plan.profile_id,
            "setup_plan_id": plan.id,
            "person_hub": "1",
            "title": "Synthetic post",
            "caption": "Never publish this.",
        },
    )
    assert response.headers["location"] == hub(plan, "content")
    state = progress(db, plan.workspace_id, [plan])[plan.id]
    assert state["content"] and not state["schedule"] and not state["done"]
    response = save(web_owner, db, plan, "schedule", schedule_data(person_hub="1"))
    assert response.headers["location"] == hub(plan, "preview")
    db.refresh(plan)
    assert plan.schedule_reviewed_at and progress(db, plan.workspace_id, [plan])[plan.id]["done"]
    for section in ("accounts", "content", "schedule", "preview"):
        page = web_owner.get(hub(plan, section))
        assert page.status_code == 200
        assert '<nav class="person-tabs"' in page.text and 'aria-current="page"' in page.text
        assert 'action="/plans/' + str(plan.id) + '/execution"' not in page.text
        assert f'<html lang="{lang}"' in page.text
    preview = web_owner.get(hub(plan, "preview"))
    assert "Synthetic post" in preview.text
    assert f"/people/{plan.profile_id}?section=preview&amp;page=2" in preview.text
    assert web_owner.get(hub(plan, "preview") + "&page=2").status_code == 200
    assert not locked_simulation.calls
    safe(db)


@pytest.mark.parametrize("section", ["accounts", "content", "schedule", "preview"])
def test_hub_is_tenant_scoped_and_does_not_mutate(web_owner, db, section):
    plan = create_plan(web_owner, db)
    other = Workspace(name="Private tenant")
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
    assert web_owner.get(hub(foreign, section)).status_code == 404
    assert web_owner.get(hub(plan, section)).status_code == 200
    assert web_owner.get(hub(plan, "unknown")).status_code == 404
    assert not db.scalar(select(func.count()).select_from(Job))
    safe(db)


@pytest.mark.parametrize(
    "invalid",
    [
        "https://evil.example/",
        "//evil.example/",
        "/people/999999?section=accounts",
        "/people/1?section=preview",
        "/people/1?section=content&next=//evil.example/",
    ],
)
def test_oauth_hub_return_has_no_open_redirect(web_owner, db, invalid):
    create_plan(web_owner, db)
    from repeater.models import OAuthState

    with override("oauth", "meta", FakeProvider("meta", False, [])):
        web_owner.get("/connect/meta", params={"return_to": invalid}, follow_redirects=False)
    assert db.scalar(select(OAuthState)).return_path is None
    safe(db)


def test_platform_and_drive_controls_remain_in_person_content(web_owner, db, make_account, locked_simulation):
    plan = create_plan(web_owner, db)
    account, gdrive = make_account("facebook"), make_account("gdrive")
    data = {"person_hub": "1", "wizard_step": "content"}
    response = save(
        web_owner, db, plan, "platforms", {**data, "source_account_id": account.id, "import_mode": "latest30"}
    )
    assert response.headers["location"] == hub(plan, "content")
    source = db.scalar(select(Source).where(Source.connected_account_id == account.id))
    assert not source.enabled and not db.scalar(select(func.count()).select_from(Job))
    db.refresh(plan)
    response = post(
        web_owner,
        f"/plans/{plan.id}/platforms/{source.id}",
        {**data, "revision": plan.revision, "action": "start"},
    )
    assert response.headers["location"] == hub(plan, "content")
    assert set(db.scalars(select(Job.kind))) == {"poll_source"}
    folder_mock(locked_simulation)
    response = save(web_owner, db, plan, "source", {**data, "drive_id": gdrive.id, "folder": FOLDER})
    assert response.headers["location"] == hub(plan, "content")
    response = save(web_owner, db, plan, "source", {**data, "drive_id": gdrive.id, "folder": "invalid"})
    assert response.status_code == 422 and 'class="person-home"' in response.text
    html = web_owner.get(hub(plan, "content")).text
    assert f'action="/plans/{plan.id}/platforms/{source.id}"' in html
    assert f'href="/plans/{plan.id}/edit#platforms"' not in html
    safe(db)


def test_hub_preserves_text_and_upload_errors_and_rejects_stale_saves(web_owner, db, make_account):
    plan = create_plan(web_owner, db)
    fields = {
        "profile_id": str(plan.profile_id),
        "setup_plan_id": str(plan.id),
        "person_hub": "1",
        "title": "<unsafe>",
    }
    error = post(web_owner, "/library/text", {**fields, "caption": ""})
    assert error.status_code == 422 and 'class="person-home"' in error.text and "&lt;unsafe&gt;" in error.text
    response = web_owner.post(
        "/library/upload",
        data={**fields, "csrf_token": csrf_token(web_owner), "format": "image_post"},
        files={"file": ("local.png", raster(), "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == hub(plan, "content")
    bad = web_owner.post(
        "/library/upload",
        data={**fields, "csrf_token": csrf_token(web_owner), "format": "invalid"},
        files={"file": ("local.png", raster(), "image/png")},
        follow_redirects=False,
    )
    assert bad.status_code == 422 and 'class="person-home"' in bad.text
    original = plan.schedule_reviewed_at
    response = save(web_owner, db, plan, "schedule", schedule_data(person_hub="1", repeat_after_days="0"))
    db.refresh(plan)
    assert response.status_code == 422 and plan.schedule_reviewed_at == original
    assert 'class="person-home"' in response.text
    response = post(
        web_owner,
        f"/plans/{plan.id}/schedule",
        {**schedule_data(person_hub="1"), "revision": plan.revision + 1},
    )
    assert response.status_code == 422 and 'class="person-home"' in response.text
    assert web_owner.post(f"/plans/{plan.id}/targets", data={"person_hub": "1"}).status_code == 403
    safe(db)


@pytest.mark.parametrize("reason", ["unsupported", "missing_original", "excluded", "source", "disconnected"])
def test_ready_state_requires_compatible_content_for_selected_pages(web_owner, db, make_account, reason):
    plan = create_plan(web_owner, db)
    page = make_account("youtube" if reason == "unsupported" else "facebook")
    save(web_owner, db, plan, "targets", {"targets": [str(page.id)]})
    item = video(db, plan, "Synthetic original")
    if reason == "unsupported":
        item.media_kind, item.content_format, item.caption = "text", "post", "Text"
    elif reason == "missing_original":
        item.encoded_path = None
    elif reason == "excluded":
        item.overrides = {"exclude_platforms": ["facebook"]}
    elif reason == "source":
        item.source_account_id = page.id
    else:
        page.status = AccountStatus.DISABLED
    db.commit()
    state = progress(db, plan.workspace_id, [plan])[plan.id]
    assert not state["content"] and not state["done"] and state["usable_items"] == 0
    assert not state["schedule"]
    safe(db)


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_format_choices_and_automatic_recovery_copy(web_owner, db, make_account, monkeypatch, lang):
    set_lang(web_owner, lang)
    plan = create_plan(web_owner, db)
    item = video(db, plan, "Synthetic text", media_kind="text", content_format="post", caption="Text")
    db.commit()
    html = web_owner.get(f"/library/{item.id}").text
    assert 'type="checkbox" name="platform_facebook"' in html
    assert 'type="checkbox" name="platform_tiktok"' not in html
    assert 'type="checkbox" name="platform_youtube"' not in html
    # Incompatible platform preferences are retained when another field is edited.
    assert 'type="hidden" name="platform_youtube" value="1"' in html
    monkeypatch.setattr(type(get_settings()), "mail_configured", property(lambda self: True))
    html = web_owner.get("/forgot-password").text
    assert ("طلب رابط استعادة" if lang == "ar" else "request a password-reset link") in html
    response = post(web_owner, "/forgot-password", {"email": "unknown@example.com"}, page="/forgot-password")
    assert ("راجع الوارد" if lang == "ar" else "Check your inbox") in response.text
    assert "No email is sent automatically" not in response.text
    safe(db)


def test_preview_ignores_legacy_rules_and_home_renews_direct_instagram(web_owner, db, make_account):
    plan = create_plan(web_owner, db)
    account = make_account("instagram", extra={"oauth_provider": "instagram"})
    save(web_owner, db, plan, "targets", {"targets": [str(account.id)]})
    video(db, plan, "Synthetic video")
    db.commit()
    html = web_owner.get(hub(plan, "preview")).text
    assert "Pages that need reconnection" not in html
    account.status = AccountStatus.NEEDS_RECONNECT
    db.commit()
    html = web_owner.get("/").text
    assert "/connect/instagram?return_to=" in html
    assert "/connect/meta?return_to=" not in html
    safe(db)


@pytest.mark.parametrize("platform", ["youtube", "gdrive"])
def test_google_reconnect_uses_account_purpose_and_its_own_person(web_owner, db, make_account, platform):
    plan = create_plan(web_owner, db)
    account = make_account(platform)
    account.profile_id, account.status = plan.profile_id, AccountStatus.NEEDS_RECONNECT
    create_plan(web_owner, db, "Different selected person")
    db.commit()
    html = web_owner.get("/").text
    assert f"/connect/{platform}?return_to=/people/{plan.profile_id}%3Fsection%3Daccounts" in html
    assert "/connect/google" not in html
    safe(db)


def test_mixed_disconnected_selection_needs_review_and_expired_page_has_recovery(web_owner, db, make_account):
    plan = create_plan(web_owner, db)
    first, second = make_account("facebook"), make_account("youtube")
    save(web_owner, db, plan, "targets", {"targets": [str(first.id), str(second.id)]})
    second.status = AccountStatus.NEEDS_RECONNECT
    db.commit()
    assert not progress(db, plan.workspace_id, [plan])[plan.id]["accounts"]
    html = web_owner.get(hub(plan)).text
    assert f"/connect/youtube?return_to=/people/{plan.profile_id}%3Fsection%3Daccounts" in html
    assert f'name="targets" value="{second.id}" checked disabled' in html
    safe(db)


def test_secondary_content_entries_return_to_person_home(web_owner, db, make_account, locked_simulation):
    from tests.test_tiktok_read_view import api, reader

    plan = create_plan(web_owner, db)
    empty = web_owner.get(f"/library?profile_id={plan.profile_id}").text.split('class="empty-state"', 1)[1]
    assert f'href="{hub(plan, "content")}"' in empty
    account = make_account("facebook")
    save(web_owner, db, plan, "platforms", {"source_account_id": account.id, "import_mode": "latest30"})
    source = db.scalar(select(Source))
    assert web_owner.get(f"/sources/{source.id}/edit", follow_redirects=False).headers["location"] == hub(
        plan, "content"
    )
    assert f'href="{hub(plan, "content")}"' in web_owner.get("/sources").text
    tiktok = reader(db, make_account)
    tiktok.profile_id = plan.profile_id
    db.commit()
    api(locked_simulation, tiktok)
    response = web_owner.get(f"/accounts/{tiktok.id}/videos")
    assert response.status_code == 200 and f'href="{hub(plan, "preview")}"' in response.text
    assert all(
        str(call.request.url).startswith("https://open.tiktokapis.com/") for call in locked_simulation.calls
    )
    safe(db)
