"""Folder and progress acceptance tests: synthetic tenants, no publisher and mocked HTTP only."""

from datetime import timedelta

import pytest
import respx
from sqlalchemy import func, select

from repeater.config import get_settings
from repeater.jobs import kinds
from repeater.models import (
    AccountStatus,
    ContentPlan,
    CreatorProfile,
    Job,
    JobStatus,
    Platform,
    ScheduledPost,
    Source,
    Video,
    VideoStatus,
    Workspace,
)
from repeater.platforms import override
from repeater.platforms.base import RemoteVideo, RetryableError
from repeater.sources import drive, importer
from repeater.timeutil import utcnow
from tests.test_content_plans import FOLDER, create_plan, folder_mock, save, video
from tests.test_sources import FakeLister
from tests.test_web_support import post, set_lang

pytest_plugins = ["tests.test_web_support"]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    monkeypatch.setattr(get_settings(), "tiktok_audited", False)
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def setup_source(client, db, make_account, mock):
    plan = create_plan(client, db)
    account = make_account(Platform.GDRIVE)
    folder_mock(mock)
    assert (
        save(
            client, db, plan, "source", {"drive_id": account.id, "folder": FOLDER, "include_images": "1"}
        ).status_code
        == 303
    )
    db.refresh(plan)
    return plan, db.get(Source, plan.source_id), account


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_folder_search_choices_and_paging_preserve_options(web_owner, db, make_account, isolated, lang):
    set_lang(web_owner, lang)
    plan = create_plan(web_owner, db)
    account = make_account(Platform.GDRIVE)
    route = isolated.get(f"{drive.API_BASE}/files").respond(
        200, json={"files": [{"id": FOLDER, "name": "<مكتبة>"}], "nextPageToken": "opaque-next"}
    )
    data = {
        "wizard_step": "content",
        "person_hub": "1",
        "drive_id": account.id,
        "folder_search": "Laila's \\ videos",
        "include_images": "1",
        "backfill": "12",
    }
    response = post(web_owner, f"/plans/{plan.id}/folders", data)
    assert response.status_code == 200
    assert "&lt;مكتبة&gt;" in response.text
    assert "name contains 'Laila\\'s \\\\ videos'" in route.calls.last.request.url.params["q"]
    # Native controls share one form: changes to options also apply when choosing a result.
    search_form = response.text.split('class="stack folder-search"', 1)[1].split("</form>", 1)[0]
    for field in ('name="include_images"', 'name="backfill"', 'name="folder"', 'name="page_token"'):
        assert field in search_form
    assert 'value="opaque-next"' in search_form
    assert 'name="person_hub" value="1"' in search_form
    response = post(web_owner, f"/plans/{plan.id}/folders", {**data, "page_token": "opaque-next"})
    assert route.calls.last.request.url.params["pageToken"] == "opaque-next"
    assert "name contains" in route.calls.last.request.url.params["q"]
    folder_mock(isolated)
    response = save(web_owner, db, plan, "source", {**data, "folder": FOLDER, "backfill": "7"})
    assert response.headers["location"] == f"/people/{plan.profile_id}?section=content"
    db.refresh(plan)
    source = db.get(Source, plan.source_id)
    assert source.config == {"folder_id": FOLDER, "backfill_count": 7, "include_images": True}
    assert not source.enabled
    assert not db.scalar(select(func.count()).select_from(Job))
    assert not db.scalar(select(func.count()).select_from(ScheduledPost))


def test_search_empty_and_new_query_reset_page(web_owner, db, make_account, isolated):
    plan = create_plan(web_owner, db)
    account = make_account(Platform.GDRIVE)
    route = isolated.get(f"{drive.API_BASE}/files").respond(200, json={"files": []})
    response = post(
        web_owner,
        f"/plans/{plan.id}/folders",
        {"drive_id": account.id, "folder_search": "No match", "wizard_step": "content", "person_hub": "1"},
    )
    assert response.status_code == 200 and "import.no_matches" not in response.text
    assert "لا توجد فولدرات مطابقة" in response.text
    assert "pageToken" not in route.calls.last.request.url.params
    response = post(
        web_owner, f"/plans/{plan.id}/folders", {"drive_id": account.id, "folder_search": "x" * 201}
    )
    assert response.status_code == 422 and route.call_count == 1


@pytest.mark.parametrize(
    "status,key",
    [
        (401, "access expired"),
        (403, "unavailable"),
        (404, "unavailable"),
        (429, "limiting requests"),
        (503, "Could not reach"),
    ],
)
def test_drive_errors_are_actionable_and_keep_input(web_owner, db, make_account, isolated, status, key):
    set_lang(web_owner, "en")
    plan = create_plan(web_owner, db)
    account = make_account(Platform.GDRIVE)
    isolated.get(f"{drive.API_BASE}/files/{FOLDER}").respond(
        status, json={"error": {"message": "mock failure"}}
    )
    response = save(
        web_owner,
        db,
        plan,
        "source",
        {
            "wizard_step": "content",
            "person_hub": "1",
            "drive_id": account.id,
            "folder_link": f"https://drive.google.com/drive/folders/{FOLDER}",
            "backfill": "17",
        },
    )
    assert response.status_code == 422 and key in response.text
    assert FOLDER in response.text and 'value="17"' in response.text
    db.refresh(plan)
    assert plan.source_id is None
    assert not db.scalar(select(func.count()).select_from(Job))


@pytest.mark.parametrize(
    "state",
    [
        "saved",
        "queued",
        "checking",
        "scheduled",
        "importing",
        "processing",
        "watching",
        "empty",
        "failed",
        "paused",
        "error",
        "reconnect",
    ],
)
@pytest.mark.parametrize("lang", ["ar", "en"])
def test_progress_states_and_counts_are_local_read_only(web_owner, db, make_account, isolated, state, lang):
    set_lang(web_owner, lang)
    plan, source, account = setup_source(web_owner, db, make_account, isolated)
    source.enabled = state != "saved"
    if state not in {"saved", "queued", "checking", "scheduled"}:
        source.last_polled_at = source.last_success_at = utcnow()
        source.config = {**source.config, "last_listed_count": 0, "last_created_count": 0}
    if state in {"queued", "checking", "scheduled"}:
        kinds.enqueue_poll_source(db, source.id)
        job = db.scalar(select(Job))
        if state == "checking":
            job.status = JobStatus.RUNNING
        if state == "scheduled":
            job.run_at = utcnow() + timedelta(minutes=2)
    if state in {"importing", "processing", "watching", "failed"}:
        video(db, plan, "Only this source", source_id=source.id)
        item = video(db, plan, "<fixture> needs review", source_id=source.id)
        item.status = {
            "importing": VideoStatus.IMPORTING,
            "processing": VideoStatus.PROCESSING,
            "watching": VideoStatus.READY,
            "failed": VideoStatus.FAILED,
        }[state]
        if state == "failed":
            item.error = "Synthetic file error"
    if state == "paused":
        source.enabled = False
    if state == "error":
        source.last_error = "Synthetic access problem"
    if state == "reconnect":
        account.status = AccountStatus.DISABLED
    # Neither manual items nor foreign persons may inflate source counts or expose titles.
    video(db, plan, "Manual item must not count")
    other = CreatorProfile(workspace_id=plan.workspace_id, name="Other person")
    db.add(other)
    db.flush()
    db.add(
        Video(
            workspace_id=plan.workspace_id,
            profile_id=other.id,
            title="PRIVATE foreign title",
            source_id=None,
            status=VideoStatus.FAILED,
        )
    )
    db.commit()
    db.refresh(plan)
    before_jobs = db.scalar(select(func.count()).select_from(Job))
    revision = plan.revision
    calls = len(isolated.calls)
    response = web_owner.get(f"/plans/{plan.id}/import-status")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Repeater-Import-Status"] == "1"
    assert f'data-import-state="{state}"' in response.text
    assert "PRIVATE foreign title" not in response.text and "Manual item" not in response.text
    assert "import." not in response.text
    if state == "failed":
        assert "&lt;fixture&gt;" in response.text and f"/library/{item.id}" in response.text
    db.refresh(plan)
    assert plan.revision == revision
    assert len(isolated.calls) == calls
    assert db.scalar(select(func.count()).select_from(Job)) == before_jobs
    assert not db.scalar(select(func.count()).select_from(ScheduledPost))


def test_progress_tenant_and_csrf_boundaries(web_owner, web_app, db, make_account, isolated):
    plan, source, _ = setup_source(web_owner, db, make_account, isolated)
    ws = Workspace(name="Foreign", paused=True)
    db.add(ws)
    db.flush()
    person = CreatorProfile(workspace_id=ws.id, name="Foreign")
    db.add(person)
    db.flush()
    foreign = ContentPlan(
        workspace_id=ws.id, profile_id=person.id, timezone="UTC", start_date=utcnow().date()
    )
    db.add(foreign)
    db.commit()
    assert web_owner.get(f"/plans/{foreign.id}/import-status").status_code == 404
    from fastapi.testclient import TestClient

    with TestClient(web_app, base_url="https://rotator.test") as anon:
        assert anon.get(f"/plans/{plan.id}/import-status", follow_redirects=False).status_code == 303
    assert (
        web_owner.post(
            f"/plans/{plan.id}/folders", data={"drive_id": source.connected_account_id}
        ).status_code
        == 403
    )
    assert web_owner.post(f"/plans/{plan.id}/imports", data={"action": "start"}).status_code == 403


def test_success_scan_counters_and_failure_keep_last_completed_result(web_owner, db, make_account, isolated):
    plan, source, _ = setup_source(web_owner, db, make_account, isolated)
    assert save(web_owner, db, plan, "imports", {"action": "start"}).status_code == 303
    db.refresh(source)
    remote = RemoteVideo(
        external_id="synthetic-file",
        title="Fixture",
        download_url="https://fixture.invalid/file.mp4",
    )
    fake = FakeLister(Platform.GDRIVE, [remote])
    with override("lister", "gdrive", fake):
        first = importer.poll_source(db, source.id)
        second = importer.poll_source(db, source.id)
    db.refresh(source)
    assert (first.listed, first.created, second.created) == (1, 1, 0)
    assert source.config["last_listed_count"] == 1 and source.config["last_created_count"] == 0
    success = source.last_success_at
    fake.list_error = RetryableError("synthetic network failure")
    with override("lister", "gdrive", fake):
        assert importer.poll_source(db, source.id).status == "failed"
    db.refresh(source)
    assert source.last_success_at == success and source.config["last_listed_count"] == 1
    assert not db.scalar(select(func.count()).select_from(ScheduledPost))
    assert all(j.kind not in {"publish_post", "poll_post"} for j in db.scalars(select(Job)))


@pytest.mark.parametrize("link", ["", "https://example.invalid/folder", "not a folder"])
def test_invalid_folder_link_has_local_recovery_without_network(web_owner, db, make_account, isolated, link):
    plan = create_plan(web_owner, db)
    account = make_account(Platform.GDRIVE)
    response = save(
        web_owner,
        db,
        plan,
        "source",
        {"wizard_step": "content", "person_hub": "1", "drive_id": account.id, "folder_link": link},
    )
    assert response.status_code == 422 and "اختَر فولدرًا من نتائج البحث" in response.text
    assert not isolated.calls
    assert not db.scalar(select(func.count()).select_from(Job))


@pytest.mark.parametrize("new_only", [False, True])
def test_completed_empty_scan_explains_mode_and_keeps_flash(web_owner, db, make_account, isolated, new_only):
    plan, source, _ = setup_source(web_owner, db, make_account, isolated)
    source.enabled = True
    source.last_success_at = source.last_polled_at = utcnow()
    source.config = {
        **source.config,
        "backfill_count": 0 if new_only else 30,
        "last_listed_count": 0,
        "last_created_count": 0,
    }
    db.commit()
    response = web_owner.get(f"/plans/{plan.id}/import-status")
    assert ("الملفات الموجودة سابقًا لم تُستورد" if new_only else "لم يجد ملفات مدعومة") in response.text
    response = web_owner.get(f"/people/{plan.profile_id}?section=content")
    assert "تم حفظ الفولدر" in response.text  # background refresh did not consume the flash
    assert not db.scalar(select(func.count()).select_from(ScheduledPost))


@pytest.mark.parametrize("hub", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_preferences_after_folder_post_return_to_readable_content(
    web_owner, db, make_account, isolated, hub, failure
):
    plan = create_plan(web_owner, db)
    account = make_account(Platform.GDRIVE)
    isolated.get(f"{drive.API_BASE}/files").respond(503 if failure else 200, json={"files": []})
    data = {"drive_id": account.id, "wizard_step": "content", **({"person_hub": "1"} if hub else {})}
    response = post(web_owner, f"/plans/{plan.id}/folders", data)
    expected = f"/people/{plan.profile_id}?section=content" if hub else f"/plans/{plan.id}/setup/content"
    assert response.status_code == (422 if failure else 200)
    assert response.text.count(f'name="next" value="{expected}"') == 2
    for endpoint, options in [("/lang", {"lang": "en"}), ("/theme", {"theme": "dark"})]:
        result = post(web_owner, endpoint, {**options, "next": expected})
        assert result.status_code == 303 and result.headers["location"] == expected
        assert web_owner.get(expected).status_code == 200


@pytest.mark.parametrize("enabled", [False, True])
def test_retry_failed_folder_shows_queued_instead_of_stale_error(
    web_owner, db, make_account, isolated, enabled
):
    plan, source, _ = setup_source(web_owner, db, make_account, isolated)
    source.enabled = enabled
    source.last_polled_at = utcnow()
    source.last_error = "Synthetic network failure"
    db.commit()
    response = web_owner.get(f"/people/{plan.profile_id}?section=content")
    assert "إعادة محاولة فحص الفولدر" in response.text
    calls = len(isolated.calls)
    response = save(
        web_owner, db, plan, "imports", {"wizard_step": "content", "person_hub": "1", "action": "start"}
    )
    assert response.status_code == 303
    response = web_owner.get(f"/plans/{plan.id}/import-status")
    assert 'data-import-state="queued"' in response.text and "خطأ الفحص السابق" in response.text
    assert len(isolated.calls) == calls
    assert all(j.kind == "poll_source" for j in db.scalars(select(Job)))
    assert not db.scalar(select(func.count()).select_from(ScheduledPost))


def test_saved_platform_progress_points_to_monitoring_not_drive(web_owner, db, make_account, isolated):
    plan = create_plan(web_owner, db)
    account = make_account(Platform.FACEBOOK)
    assert (
        save(
            web_owner, db, plan, "platforms", {"source_account_id": account.id, "import_mode": "latest30"}
        ).status_code
        == 303
    )
    result = web_owner.get(f"/plans/{plan.id}/import-status")
    assert "اضغط بدء المتابعة في قسم المنصة المربوطة" in result.text
    assert "اضغط بدء الاستيراد في قسم Google Drive" not in result.text
    assert not isolated.calls
    assert not db.scalar(select(func.count()).select_from(Job))
