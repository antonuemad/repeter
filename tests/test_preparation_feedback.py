"""Preparation feedback uses synthetic accounts only, with every provider request denied."""

import re
from datetime import timedelta
from html import unescape

import pytest
import respx
from sqlalchemy import func, select

from repeater.config import get_settings
from repeater.content_plans import simulate
from repeater.models import (
    AccountStatus,
    ConnectedAccount,
    CreatorProfile,
    Job,
    PlanSlotOverride,
    PlanTarget,
    PostStatus,
    ScheduledPost,
    Workspace,
)
from repeater.platforms import override
from repeater.timeutil import utcnow
from tests.test_content_plans import create_plan, save, video
from tests.test_plan_rotation import history
from tests.test_web_accounts import FakeRevoke
from tests.test_web_accounts_bulk import confirm, review
from tests.test_web_oauth import FakeProvider, _meta_accounts, _start
from tests.test_web_support import post, set_lang

pytest_plugins = ["tests.test_web_support"]


@pytest.fixture(autouse=True)
def isolated_preparation(monkeypatch):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    with respx.mock(assert_all_called=False) as network:
        yield
        assert not network.calls


def assert_no_publishing(db):
    assert not get_settings().publishing_enabled
    assert not db.scalar(select(func.count()).select_from(Job))
    assert not db.scalar(select(func.count()).select_from(ScheduledPost))


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_page_choices_hide_disabled_but_retain_expired_and_tenant_boundaries(
    web_owner, db, make_account, lang
):
    plan = create_plan(web_owner, db)
    other_plan = create_plan(web_owner, db, "Other synthetic person")
    active = make_account("facebook", display_name="Available synthetic page")
    expired = make_account("youtube", display_name="Expired synthetic authorization")
    expired.status = AccountStatus.NEEDS_RECONNECT
    hidden = make_account("facebook", display_name="Disconnected selected page")
    hidden.status, hidden.token_enc, hidden.profile_id = AccountStatus.DISABLED, None, plan.profile_id
    unassigned = make_account("instagram", display_name="Disconnected unassigned page")
    unassigned.status, unassigned.token_enc = AccountStatus.DISABLED, None
    assigned = make_account("facebook", display_name="Another person's page")
    assigned.profile_id = other_plan.profile_id
    other_workspace = Workspace(name="Foreign synthetic workspace")
    db.add(other_workspace)
    db.flush()
    foreign = make_account("facebook", display_name="Foreign tenant page", workspace_id=other_workspace.id)
    db.add(PlanTarget(workspace_id=plan.workspace_id, plan_id=plan.id, account_id=hidden.id))
    item = video(db, plan, "Retained synthetic library")
    db.commit()
    set_lang(web_owner, lang)
    for path in (f"/plans/{plan.id}/setup/accounts", f"/plans/{plan.id}/edit"):
        response = web_owner.get(path)
        assert response.status_code == 200
        assert active.display_name in response.text and expired.display_name in response.text
        for account in (hidden, unassigned, assigned, foreign):
            assert account.display_name not in response.text
            assert f'name="targets" value="{account.id}"' not in response.text
    assert db.get(ConnectedAccount, hidden.id) is not None
    assert db.get(PlanTarget, (plan.id, hidden.id)) is not None
    db.refresh(item)
    assert item.title == "Retained synthetic library"
    assert_no_publishing(db)


@pytest.mark.parametrize("scope", ["single", "facebook", "all"])
def test_single_and_bulk_disconnect_hide_pages_from_existing_person_setup(web_owner, db, make_account, scope):
    plan = create_plan(web_owner, db)
    facebook = make_account("facebook", display_name="Disconnect this synthetic Page")
    instagram = make_account("instagram", display_name="Separate synthetic Instagram")
    save(web_owner, db, plan, "targets", {"targets": [str(facebook.id), str(instagram.id)]})
    with override("oauth", "meta", FakeRevoke("meta")):
        if scope == "single":
            response = post(web_owner, f"/accounts/{facebook.id}/disconnect", page="/accounts")
        else:
            _, token = review(web_owner, scope)
            response = confirm(web_owner, token)
    assert response.status_code == 303
    web_owner.get("/accounts")  # Consume the disconnection confirmation flash at its redirect target.
    for path in (f"/plans/{plan.id}/setup/accounts", f"/plans/{plan.id}/edit"):
        html = web_owner.get(path).text
        assert facebook.display_name not in html
        assert (instagram.display_name in html) == (scope != "all")
    # Disconnection may invalidate planning; neither it nor viewing setup can publish.
    assert not db.scalar(select(func.count()).select_from(ScheduledPost))
    assert not list(db.scalars(select(Job).where(Job.kind.in_(("publish_post", "poll_post")))))
    assert not get_settings().publishing_enabled


def test_hidden_page_reappears_after_mocked_reconnection_and_cannot_be_selected_while_disabled(
    web_owner, db, make_account
):
    plan = create_plan(web_owner, db)
    account = make_account("facebook", external_id="page-1", display_name="Hidden synthetic Page")
    account.status, account.token_enc, account.profile_id = AccountStatus.DISABLED, None, plan.profile_id
    db.commit()
    response = save(web_owner, db, plan, "targets", {"wizard_step": "accounts", "targets": [str(account.id)]})
    assert response.status_code == 422 and account.display_name not in response.text
    assert not db.scalar(select(func.count()).select_from(PlanTarget))
    with override("oauth", "meta", FakeProvider("meta", False, accounts=_meta_accounts())):
        _, state = _start(web_owner, "meta")
        response = web_owner.get(f"/oauth/meta/callback?code=synthetic&state={state}", follow_redirects=False)
    assert response.status_code == 303
    db.refresh(account)
    assert account.status == AccountStatus.ACTIVE and account.profile_id == plan.profile_id
    html = web_owner.get(f"/plans/{plan.id}/setup/accounts").text
    assert account.display_name in html and f'name="targets" value="{account.id}"' in html
    assert not list(db.scalars(select(Job).where(Job.kind.in_(("publish_post", "poll_post")))))
    assert not db.scalar(select(func.count()).select_from(ScheduledPost))


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_tiktok_connection_describes_reading_only_in_accounts_and_setup(web_owner, db, lang):
    plan = create_plan(web_owner, db)
    set_lang(web_owner, lang)
    expected = (
        "الربط الحالي للقراءة فقط، بدون نشر أو رفع مسودات."
        if lang == "ar"
        else "This connection is read-only, without publishing or draft uploads."
    )
    for path in ("/accounts", f"/plans/{plan.id}/setup/accounts"):
        html = web_owner.get(path).text
        assert expected in html
        assert "Publish videos to your TikTok profile." not in html
        assert "نشر الفيديوهات على ملفك في تيك توك." not in html
    assert_no_publishing(db)


MESSAGES = {
    "account_disconnected": ("Account disconnected — connect it again", "الحساب مفصول — اربطه من جديد"),
    "account_reconnect": (
        "Authorization needs renewal — reconnect this account",
        "التفويض محتاج تجديد — أعد ربط الحساب",
    ),
    "account_unavailable": ("This page is no longer assigned to this person", "الصفحة لم تعد مخصّصة للشخص ده"),
    "cooldown": ("Waiting for the repeat interval", "في انتظار انتهاء فاصل الإعادة"),
    "pending": (
        "Remaining items have pending or unresolved publications",
        "المحتوى المتبقي عليه نشر معلّق أو نتيجة غير محسومة",
    ),
    "no_eligible": ("No eligible content in this library", "مفيش محتوى مناسب في المكتبة"),
    "excluded": ("Excluded for this slot", "مستبعد لهذه الخانة"),
}


@pytest.mark.parametrize("reason", MESSAGES)
@pytest.mark.parametrize("lang", ["ar", "en"])
def test_preview_heading_and_calendar_explain_the_actual_blocker(web_owner, db, make_account, reason, lang):
    plan = create_plan(web_owner, db)
    account = make_account("facebook")
    save(web_owner, db, plan, "targets", {"targets": [str(account.id)]})
    at = (utcnow() + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
    plan.timezone, plan.start_date, plan.times = "UTC", at.date(), ["15:00"]
    plan.rotation_mode, plan.repeat_after_days = "repeat", 365
    item = video(db, plan, "Ready synthetic original")
    if reason == "account_disconnected":
        account.status, account.token_enc = AccountStatus.DISABLED, None
    elif reason == "account_reconnect":
        account.status = AccountStatus.NEEDS_RECONNECT
    elif reason == "account_unavailable":
        person = CreatorProfile(workspace_id=plan.workspace_id, name="Different synthetic person")
        db.add(person)
        db.flush()
        account.profile_id = person.id
    elif reason in ("cooldown", "pending"):
        history(
            db,
            plan,
            account,
            item,
            utcnow() - timedelta(hours=1),
            status=PostStatus.PUBLISHED if reason == "cooldown" else PostStatus.SCHEDULED,
        )
    elif reason == "no_eligible":
        item.duration_s = 100000
    elif reason == "excluded":
        db.add(
            PlanSlotOverride(
                workspace_id=plan.workspace_id,
                plan_id=plan.id,
                account_id=account.id,
                slot_at=at,
                scheduled_at=at,
                excluded=True,
            )
        )
    db.commit()
    original_jobs = db.scalar(select(func.count()).select_from(Job))
    original_posts = db.scalar(select(func.count()).select_from(ScheduledPost))
    set_lang(web_owner, lang)
    expected = MESSAGES[reason][lang == "ar"]
    for path in (f"/plans/{plan.id}", f"/plans/{plan.id}/setup/preview"):
        response = web_owner.get(path)
        assert response.status_code == 200
        summaries = [
            unescape(re.sub("<[^>]+>", "", value))
            for value in re.findall(r"<summary>(.*?)</summary>", response.text, re.S)
        ]
        assert any(expected in summary for summary in summaries), reason
        assert (
            "Waiting for eligible content" not in response.text and "بانتظار محتوى مناسب" not in response.text
        )
        if reason.startswith("account_"):
            assert f'href="/plans/{plan.id}/setup/accounts"' in response.text
    calendar = web_owner.get("/calendar", params={"day": at.date().isoformat(), "person": plan.profile_id})
    assert calendar.status_code == 200 and expected in calendar.text
    if reason.startswith("account_"):
        assert f'href="/people/{plan.profile_id}?section=accounts"' in calendar.text
    assert db.scalar(select(func.count()).select_from(Job)) == original_jobs
    assert db.scalar(select(func.count()).select_from(ScheduledPost)) == original_posts


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_mixed_empty_slot_does_not_blame_content_for_every_page(web_owner, db, make_account, lang):
    plan = create_plan(web_owner, db)
    disconnected, connected = make_account("facebook"), make_account("facebook")
    save(web_owner, db, plan, "targets", {"targets": [str(disconnected.id), str(connected.id)]})
    disconnected.status = AccountStatus.DISABLED
    db.commit()
    set_lang(web_owner, lang)
    html = web_owner.get(f"/plans/{plan.id}/setup/preview").text
    expected = (
        "راجع سبب عدم التجهيز لكل صفحة" if lang == "ar" else "Check why each page has no content assigned"
    )
    assert expected in html
    result = simulate(db, plan)
    assert result["rows"][0].empty_reason is None
    assert {e.reason for e in result["rows"][0].entries} == {"account_disconnected", "no_eligible"}
    assert_no_publishing(db)
