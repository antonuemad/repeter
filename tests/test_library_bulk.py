"""Bulk library acceptance: synthetic people and files, mocked HTTP, no publishing or drafts."""

import re
from datetime import timedelta

import pytest
import respx
from sqlalchemy import func, select

from repeater.config import get_settings
from repeater.content_plans import simulate
from repeater.media import storage
from repeater.models import (
    ActivityLog,
    CreatorProfile,
    Job,
    Platform,
    PostStatus,
    ScheduledPost,
    Video,
    VideoStatus,
    Workspace,
)
from tests.test_content_plans import create_plan
from tests.test_plan_rotation import NOW, setup_plan
from tests.test_web_support import post, set_lang

pytest_plugins = ["tests.test_web_support"]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    monkeypatch.setattr(get_settings(), "tiktok_audited", False)
    with respx.mock(assert_all_called=False) as mock:
        yield
        assert not mock.calls, "Library management must never contact a provider"


def item(db, plan=None, *, workspace_id=None, title="Synthetic item", status=VideoStatus.READY, **kwargs):
    row = Video(
        workspace_id=workspace_id or plan.workspace_id,
        profile_id=plan.profile_id if plan else None,
        title=title,
        media_kind="text" if plan else "video",
        content_format="post" if plan else "reel",
        caption="Synthetic library content only",
        status=status,
        **kwargs,
    )
    db.add(row)
    db.flush()
    return row


def review(client, plan, selected, action="exclude", scope="all", **state):
    data = {
        "profile_id": plan.profile_id if plan else 0,
        "video_ids": [str(v.id) for v in selected],
        "action": action,
        "scope": scope,
        **state,
    }
    response = post(client, "/library/bulk/review", data)
    match = re.search(r'name="confirmation" value="([^"]+)"', response.text)
    return response, match[1] if match else ""


def apply(client, token, **extra):
    return post(client, "/library/bulk/apply", {"confirmation": token, **extra})


def no_work(db):
    assert db.scalar(select(func.count()).select_from(Job)) == 0
    assert not get_settings().publishing_enabled and not get_settings().tiktok_audited


@pytest.mark.parametrize("lang", ["ar", "en"])
@pytest.mark.parametrize("scope", ["all", "tiktok", "youtube", "facebook", "instagram"])
def test_exclude_then_include_preserves_other_preferences_and_preview(
    web_owner, db, make_account, lang, scope
):
    set_lang(web_owner, lang)
    plan, _ = setup_plan(web_owner, db, make_account, days=1)
    selected = [
        item(
            db,
            plan,
            title=f"Synthetic {i}",
            overrides={"title": f"Override {i}", "keep": {"nested": True}, "exclude_platforms": ["youtube"]},
        )
        for i in range(2)
    ]
    untouched = item(db, plan, title="Untouched")
    plan.enabled = True
    db.commit()
    db.refresh(plan)
    before = plan.revision
    response, token = review(web_owner, plan, selected, scope=scope)
    assert response.status_code == 200 and token
    assert response.headers["cache-control"] == "no-store"
    assert "bulk.help_" not in response.text
    db.refresh(plan)
    assert plan.enabled and plan.revision == before  # review is read-only
    result = apply(web_owner, token)
    assert result.status_code == 303
    db.expire_all()
    expected = {"facebook", "instagram", "youtube", "tiktok"} if scope == "all" else {"youtube", scope}
    for row in selected:
        assert set(row.overrides["exclude_platforms"]) == expected
        assert row.overrides["keep"] == {"nested": True} and row.overrides["title"].startswith("Override")
        assert row.status == VideoStatus.READY
    assert untouched.overrides == {}
    if scope != "youtube":
        assert not plan.enabled and plan.revision == before + 1
    else:
        assert plan.enabled and plan.revision == before  # an actual no-op never stops a plan
    assert simulate(db, plan, now=NOW)["ready"] == 3
    if scope in {"all", "facebook"}:
        assert {e.video.id for r in simulate(db, plan, now=NOW)["rows"] for e in r.entries if e.video} == {
            untouched.id
        }
    response, token = review(web_owner, plan, selected, action="include", scope=scope)
    assert response.status_code == 200 and apply(web_owner, token).status_code == 303
    db.expire_all()
    for row in selected:
        expected = set() if scope in {"all", "youtube"} else {"youtube"}
        assert set(row.overrides.get("exclude_platforms", [])) == expected
        assert row.overrides["keep"] == {"nested": True}
    assert db.scalar(select(func.count()).select_from(ScheduledPost)) == 0
    no_work(db)


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_archive_and_bulk_restore_keep_files_and_saved_exclusions(web_owner, db, make_account, lang):
    set_lang(web_owner, lang)
    plan, _ = setup_plan(web_owner, db, make_account)
    selected = [
        item(db, plan, title=f"Synthetic text {i}", overrides={"exclude_platforms": ["tiktok"]})
        for i in range(2)
    ]
    folder = storage.ensure_video_dir(plan.workspace_id, selected[0].id)
    path = folder / "fixture.bin"
    path.write_bytes(b"synthetic local file, never uploaded")
    selected[0].encoded_path = storage.rel_path(path)
    db.commit()
    response, token = review(web_owner, plan, selected, action="archive", page="2", q="Synthetic")
    assert response.status_code == 200 and apply(web_owner, token).status_code == 303
    db.expire_all()
    assert all(v.status == VideoStatus.ARCHIVED for v in selected)
    assert path.read_bytes() == b"synthetic local file, never uploaded"
    assert simulate(db, plan, now=NOW)["ready"] == 0
    archived = web_owner.get(f"/library?profile_id={plan.profile_id}&status=archived")
    assert all(v.title in archived.text for v in selected)
    response, token = review(web_owner, plan, selected, action="restore", status="archived")
    assert response.status_code == 200 and apply(web_owner, token).status_code == 303
    db.expire_all()
    assert all(
        v.status == VideoStatus.READY and v.overrides == {"exclude_platforms": ["tiktok"]} for v in selected
    )
    assert simulate(db, plan, now=NOW)["ready"] == 2
    assert db.scalar(select(func.count()).select_from(Video)) == 2
    assert path.exists()
    no_work(db)


@pytest.mark.parametrize("status", [VideoStatus.IMPORTING, VideoStatus.PROCESSING, VideoStatus.DUPLICATE])
@pytest.mark.parametrize("action", ["archive", "exclude", "include", "restore"])
def test_unavailable_batch_is_rejected_atomically(web_owner, db, status, action):
    plan = create_plan(web_owner, db)
    ready = item(db, plan)
    busy = item(db, plan, title="Synthetic busy", status=status)
    db.commit()
    revision = plan.revision
    response, token = review(web_owner, plan, [ready, busy], action=action)
    assert response.status_code == 422 and not token
    db.expire_all()
    assert ready.status == VideoStatus.READY and ready.overrides == {}
    assert busy.status == status and plan.revision == revision
    no_work(db)


@pytest.mark.parametrize("case", ["ready", "missing_file", "duplicate"])
def test_restore_rejection_cannot_partially_restore_batch(web_owner, db, case):
    plan = create_plan(web_owner, db)
    good = item(db, plan, status=VideoStatus.ARCHIVED)
    bad = item(db, plan, status=VideoStatus.ARCHIVED)
    if case == "ready":
        bad.status = VideoStatus.READY
    if case == "missing_file":
        bad.media_kind = "video"
        bad.content_format = "reel"
    if case == "duplicate":
        bad.duplicate_of_id = good.id
    db.commit()
    response, token = review(web_owner, plan, [good, bad], action="restore")
    assert response.status_code == 422 and not token
    db.refresh(good)
    assert good.status == VideoStatus.ARCHIVED
    no_work(db)


@pytest.mark.parametrize(
    "change",
    ["title", "caption", "status", "overrides", "profile", "plan_revision", "plan_enabled", "delete"],
)
def test_review_staleness_requires_a_new_review(web_owner, db, change):
    plan = create_plan(web_owner, db)
    row = item(db, plan)
    db.commit()
    _, token = review(web_owner, plan, [row])
    if change == "title":
        row.title = "Edited title"
    if change == "caption":
        row.caption = "Edited caption"
    if change == "status":
        row.status = VideoStatus.FAILED
    if change == "overrides":
        row.overrides = {"keep": 1}
    if change == "profile":
        other = CreatorProfile(workspace_id=plan.workspace_id, name="Other synthetic person")
        db.add(other)
        db.flush()
        row.profile_id = other.id
    if change == "plan_revision":
        plan.revision += 1
    if change == "plan_enabled":
        plan.enabled = True
    if change == "delete":
        db.delete(row)
    db.commit()
    response = apply(web_owner, token)
    assert response.status_code == 409
    if change not in {"delete", "profile"}:
        db.refresh(row)
        assert not row.overrides.get("exclude_platforms")
    no_work(db)


def test_expired_tampered_replayed_and_superseded_reviews(web_owner, db, monkeypatch):
    plan = create_plan(web_owner, db)
    row = item(db, plan)
    db.commit()
    _, first = review(web_owner, plan, [row])
    _, second = review(web_owner, plan, [row])
    assert apply(web_owner, first).status_code == 409
    assert apply(web_owner, second + "tampered").status_code == 409
    monkeypatch.setattr("repeater.web.routes.library_bulk.CONFIRM_SECONDS", -1)
    assert apply(web_owner, second).status_code == 409
    monkeypatch.setattr("repeater.web.routes.library_bulk.CONFIRM_SECONDS", 600)
    assert apply(web_owner, second).status_code == 303
    assert apply(web_owner, second).status_code == 409
    no_work(db)


def test_signed_selection_cannot_be_expanded_by_form_fields(web_owner, db):
    plan = create_plan(web_owner, db)
    first, second = item(db, plan), item(db, plan)
    db.commit()
    _, token = review(web_owner, plan, [first], scope="tiktok")
    assert apply(web_owner, token, video_ids=[second.id], action="archive", scope="all").status_code == 303
    db.expire_all()
    assert first.status == VideoStatus.READY and first.overrides == {"exclude_platforms": ["tiktok"]}
    assert second.status == VideoStatus.READY and second.overrides == {}
    no_work(db)


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "duplicate",
        "bad_id",
        "negative",
        "huge",
        "too_many",
        "bad_action",
        "bad_scope",
        "bad_profile",
        "long_search",
    ],
)
def test_invalid_inputs_are_bounded_and_do_not_change_data(web_owner, db, case):
    plan = create_plan(web_owner, db)
    row = item(db, plan)
    db.commit()
    data = {
        "profile_id": str(plan.profile_id),
        "video_ids": [str(row.id)],
        "action": "archive",
        "scope": "all",
    }
    if case == "empty":
        data["video_ids"] = []
    if case == "duplicate":
        data["video_ids"] = [str(row.id)] * 2
    if case == "bad_id":
        data["video_ids"] = ["not-an-id"]
    if case == "negative":
        data["video_ids"] = ["-1"]
    if case == "huge":
        data["video_ids"] = ["9" * 50]
    if case == "too_many":
        data["video_ids"] = [str(i) for i in range(1, 26)]
    if case == "bad_action":
        data["action"] = "delete"
    if case == "bad_scope":
        data["scope"] = "gdrive"
    if case == "bad_profile":
        data["profile_id"] = "bad"
    if case == "long_search":
        data["q"] = "x" * 201
    response = post(web_owner, "/library/bulk/review", data)
    assert response.status_code == (400 if case in {"bad_profile", "long_search"} else 422)
    db.refresh(row)
    assert row.status == VideoStatus.READY
    no_work(db)


def test_tenant_person_csrf_and_actor_boundaries(web_owner, web_editor, db):
    plan = create_plan(web_owner, db)
    own = item(db, plan)
    other_plan = create_plan(web_owner, db, "Other person")
    other = item(db, other_plan)
    ws = Workspace(name="Foreign synthetic workspace")
    db.add(ws)
    db.flush()
    foreign = item(db, workspace_id=ws.id)
    db.commit()
    for selected in ([own, other], [own, foreign]):
        response, token = review(web_owner, plan, selected)
        assert response.status_code == 404 and not token
    assert web_owner.post("/library/bulk/review", data={"profile_id": plan.profile_id}).status_code == 403
    _, token = review(web_owner, plan, [own])
    assert web_owner.post("/library/bulk/apply", data={"confirmation": token}).status_code == 403
    assert apply(web_editor, token).status_code == 409
    _, token = review(web_editor, plan, [own])
    assert apply(web_editor, token).status_code == 303  # editors already manage workspace content
    db.expire_all()
    assert not other.overrides and not foreign.overrides
    no_work(db)


def test_edit_selection_retains_scope_search_page_and_native_checkbox_association(web_owner, db):
    plan = create_plan(web_owner, db)
    first, second = item(db, plan, title="Needle first"), item(db, plan, title="Needle second")
    db.commit()
    response, token = review(web_owner, plan, [first, second], scope="facebook", q="Needle", page="1")
    assert response.status_code == 200 and token
    edit = post(
        web_owner,
        "/library/bulk/review",
        {
            "profile_id": plan.profile_id,
            "action": "exclude",
            "scope": "facebook",
            "q": "Needle",
            "page": "1",
            "video_ids": [str(first.id), str(second.id)],
            "edit": "1",
        },
    )
    assert edit.status_code == 200 and 'value="Needle"' in edit.text
    assert '<option value="facebook" selected>' in edit.text
    for row in (first, second):
        assert f'form="library-bulk" type="checkbox" name="video_ids" value="{row.id}"' in edit.text
        assert f'value="{row.id}" aria-describedby="bulk-selection-help" checked' in edit.text
    assert 'name="q" value="Needle"' in edit.text
    db.expire_all()
    assert not first.overrides and not second.overrides
    no_work(db)


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_excluded_filter_search_and_pagination_are_person_scoped(web_owner, db, lang):
    set_lang(web_owner, lang)
    plan = create_plan(web_owner, db)
    chosen = [
        item(db, plan, title=f"Needle {i}", overrides={"exclude_platforms": ["facebook"]}) for i in range(27)
    ]
    item(db, plan, title="Not selected")
    item(
        db,
        plan,
        title="Archived needle",
        status=VideoStatus.ARCHIVED,
        overrides={"exclude_platforms": ["facebook"]},
    )
    other = create_plan(web_owner, db, "Other person")
    item(db, other, title="Foreign needle", overrides={"exclude_platforms": ["facebook"]})
    db.commit()
    url = f"/library?profile_id={plan.profile_id}&status=excluded&q=Needle"
    response = web_owner.get(url)
    assert (
        response.status_code == 200
        and "Foreign needle" not in response.text
        and "Archived needle" not in response.text
    )
    assert response.text.count('form="library-bulk" type="checkbox"') == 24
    assert "q=Needle" in response.text and "page=2" in response.text
    page2 = web_owner.get(url + "&page=2")
    assert page2.status_code == 200 and page2.text.count('form="library-bulk" type="checkbox"') == 3
    assert all(
        "bulk." not in web_owner.get(f"/library?profile_id={plan.profile_id}&q={q}").text
        for q in ["NoMatch", "Needle"]
    )
    _, token = review(
        web_owner, plan, chosen[:2], action="include", scope="facebook", status="excluded", q="Needle"
    )
    assert apply(web_owner, token).status_code == 303
    assert web_owner.get(url).text.count('form="library-bulk" type="checkbox"') == 24
    no_work(db)


@pytest.mark.parametrize(
    "query,field,value",
    [
        ("50%_\\", "title", "literal 50%_\\ example"),
        ("Override", "override_title", "Override title"),
        ("Caption needle", "override_caption", "Caption needle"),
        ("بحث", "caption", "نص بحث تجريبي"),
    ],
)
def test_search_handles_literal_wildcards_and_visible_overrides(web_owner, db, query, field, value):
    plan = create_plan(web_owner, db)
    selected = item(db, plan, title="Selected")
    distractor = item(db, plan, title="50aXX unrelated")
    if field in {"title", "caption"}:
        setattr(selected, field, value)
    else:
        selected.overrides = {field.removeprefix("override_"): value}
    db.commit()
    response = web_owner.get("/library", params={"profile_id": plan.profile_id, "q": query})
    assert response.status_code == 200 and f'href="/library/{selected.id}"' in response.text
    assert f'href="/library/{distractor.id}"' not in response.text
    no_work(db)


def record(db, workspace_id, video, account, status, *, state=None, remote=None, plan=None, index=0):
    row = ScheduledPost(
        workspace_id=workspace_id,
        video_id=video.id,
        connected_account_id=account.id,
        platform=account.platform,
        status=status,
        scheduled_at=NOW + timedelta(days=1),
        idempotency_key=f"bulk-fixture-{video.id}-{account.id}-{index}",
        publish_state=state or {},
        platform_post_id=remote,
        plan_id=plan.id if plan else None,
    )
    db.add(row)
    db.flush()
    return row


@pytest.mark.parametrize("action", ["archive", "exclude"])
def test_plan_pauses_and_preserves_published_inflight_and_uncertain_history(
    web_owner, db, make_account, action
):
    plan, (account,) = setup_plan(web_owner, db, make_account)
    selected = item(db, plan)
    untouched = item(db, plan)
    other = create_plan(web_owner, db, "Unchanged plan")
    plan.enabled = True
    scheduled = record(db, plan.workspace_id, selected, account, PostStatus.SCHEDULED, plan=plan, index=0)
    failed = record(db, plan.workspace_id, selected, account, PostStatus.FAILED, plan=plan, index=1)
    another = record(db, plan.workspace_id, untouched, account, PostStatus.SCHEDULED, plan=plan, index=2)
    protected = []
    for i, status in enumerate(
        [PostStatus.PUBLISHED, PostStatus.UPLOADING, PostStatus.PROCESSING, PostStatus.NEEDS_ATTENTION], 3
    ):
        protected.append(
            (record(db, plan.workspace_id, selected, account, status, plan=plan, index=i), status)
        )
    ambiguous = record(
        db,
        plan.workspace_id,
        selected,
        account,
        PostStatus.FAILED,
        remote="synthetic-existing-attempt",
        plan=plan,
        index=9,
    )
    db.commit()
    db.refresh(plan)
    revision = plan.revision
    _, token = review(web_owner, plan, [selected], action=action)
    assert apply(web_owner, token).status_code == 303
    db.expire_all()
    assert not plan.enabled and plan.revision == revision + 1 and other.revision == 1
    assert all(p.status == PostStatus.CANCELED for p in (scheduled, failed, another))
    assert all(p.status == status for p, status in protected)
    assert (
        ambiguous.status == PostStatus.FAILED and ambiguous.platform_post_id == "synthetic-existing-attempt"
    )
    no_work(db)


def test_unassigned_bulk_exclusion_only_cancels_selected_platform(web_owner, db, workspace, make_account):
    video = item(db, workspace_id=workspace.id)
    tiktok, facebook = make_account(Platform.TIKTOK), make_account(Platform.FACEBOOK)
    tt = record(db, workspace.id, video, tiktok, PostStatus.SCHEDULED)
    fb = record(db, workspace.id, video, facebook, PostStatus.SCHEDULED)
    db.commit()
    _, token = review(web_owner, None, [video], scope="tiktok")
    assert apply(web_owner, token).status_code == 303
    db.expire_all()
    assert tt.status == PostStatus.CANCELED and fb.status == PostStatus.SCHEDULED
    no_work(db)


def test_no_op_does_not_log_or_pause_or_enqueue(web_owner, db):
    plan = create_plan(web_owner, db)
    plan.enabled = True
    video = item(db, plan, overrides={"exclude_platforms": ["tiktok"]})
    db.commit()
    count = db.scalar(select(func.count()).select_from(ActivityLog))
    revision = plan.revision
    _, token = review(web_owner, plan, [video], scope="tiktok")
    assert apply(web_owner, token).status_code == 303
    db.refresh(plan)
    assert plan.enabled and plan.revision == revision
    assert db.scalar(select(func.count()).select_from(ActivityLog)) == count
    no_work(db)


def test_escaped_titles_and_search_do_not_add_markup(web_owner, db):
    plan = create_plan(web_owner, db)
    video = item(db, plan, title="<script>fixture</script>")
    db.commit()
    response, _ = review(web_owner, plan, [video])
    assert (
        "&lt;script&gt;fixture&lt;/script&gt;" in response.text
        and "<script>fixture</script>" not in response.text
    )
    page = web_owner.get(
        "/library", params={"profile_id": plan.profile_id, "q": '"><script>fixture</script>'}
    )
    assert "&lt;script&gt;" in page.text and "<script>fixture</script>" not in page.text
    no_work(db)


def test_two_review_confirmations_cannot_apply_the_same_change_twice(web_owner, web_app, db):
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from tests.test_web_support import csrf_from

    plan = create_plan(web_owner, db)
    row = item(db, plan)
    db.commit()
    _, token = review(web_owner, plan, [row], scope="tiktok")
    cookie = dict(web_owner.cookies)
    csrf = csrf_from(web_owner.get("/library", params={"profile_id": plan.profile_id}).text)
    count = db.scalar(select(func.count()).select_from(ActivityLog))

    def submit(_):
        with TestClient(web_app, base_url="https://rotator.test") as client:
            client.cookies.update(cookie)
            return client.post(
                "/library/bulk/apply",
                data={"confirmation": token, "csrf_token": csrf},
                follow_redirects=False,
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, range(2))) == [303, 409]
    db.expire_all()
    assert row.overrides == {"exclude_platforms": ["tiktok"]}
    assert db.scalar(select(func.count()).select_from(ActivityLog)) == count + 1
    no_work(db)


@pytest.mark.parametrize("case", ["review", "invalid_selection", "stale_review"])
def test_language_and_theme_return_to_readable_library_after_post(web_owner, db, case):
    from html import unescape

    plan = create_plan(web_owner, db)
    row = item(db, plan, title="Needle item")
    db.commit()
    page, token = review(web_owner, plan, [row], scope="facebook", q="Needle", page="2")
    if case == "invalid_selection":
        page = post(
            web_owner,
            "/library/bulk/review",
            {
                "profile_id": plan.profile_id,
                "action": "exclude",
                "scope": "facebook",
                "video_ids": [],
                "q": "Needle",
                "page": "2",
            },
        )
    if case == "stale_review":
        row.caption = "Changed synthetic text"
        db.commit()
        page = apply(web_owner, token)
    next_values = re.findall(r'name="next" value="([^"]+)"', page.text)
    expected = f"/library?profile_id={plan.profile_id}&status=active&page={1 if case == 'invalid_selection' else 2}&q=Needle"
    assert expected in [unescape(v) for v in next_values]
    assert web_owner.get(expected).status_code == 200
    no_work(db)


def test_restore_rechecks_files_after_review(web_owner, db):
    plan = create_plan(web_owner, db)
    row = item(db, plan, status=VideoStatus.ARCHIVED)
    row.media_kind = "video"
    row.content_format = "reel"
    path = storage.ensure_video_dir(plan.workspace_id, row.id) / "fixture.bin"
    path.write_bytes(b"synthetic-local-only")
    row.encoded_path = storage.rel_path(path)
    db.commit()
    _, token = review(web_owner, plan, [row], action="restore")
    path.unlink()  # disposable local fixture, never a production file
    response = apply(web_owner, token)
    assert response.status_code == 409
    db.refresh(row)
    assert row.status == VideoStatus.ARCHIVED
    no_work(db)


def test_clearing_exclusions_on_last_page_returns_to_remaining_results(web_owner, db):
    plan = create_plan(web_owner, db)
    selected = [
        item(db, plan, title=f"Synthetic {i}", overrides={"exclude_platforms": ["tiktok"]}) for i in range(26)
    ]
    db.commit()
    _, token = review(
        web_owner, plan, selected[:2], action="include", scope="tiktok", status="excluded", page="2"
    )
    response = apply(web_owner, token)
    assert response.status_code == 303
    page = web_owner.get(response.headers["location"])
    assert page.status_code == 200 and page.text.count('form="library-bulk" type="checkbox"') == 24
    assert "bulk.no_matches" not in page.text
    no_work(db)
