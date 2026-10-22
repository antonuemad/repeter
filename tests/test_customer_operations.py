"""Customer operations against real isolated Postgres, with all provider HTTP denied."""

from datetime import timedelta

import pytest
import respx
from sqlalchemy import func, select

from repeater import access, commerce, mail
from repeater.config import get_settings
from repeater.content_plans import simulate
from repeater.crypto import decrypt_json
from repeater.models import (
    Job,
    MailOutbox,
    PlanSlotOverride,
    PlanTarget,
    ScheduledPost,
    User,
    Workspace,
    WorkspaceMember,
)
from repeater.timeutil import utcnow
from tests.test_content_plans import create_plan, video
from tests.test_web_support import PASSWORD, post, set_lang

pytest_plugins = ["tests.test_web_support"]


@pytest.fixture(autouse=True)
def no_provider_network():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def calendar_data(web_owner, db, workspace, make_account):
    p = create_plan(web_owner, db)
    at = (utcnow() + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
    p.timezone = "UTC"
    p.times = ["15:00"]
    p.start_date = at.date()
    p.rotation_mode = "repeat"
    p.repeat_after_days = 7
    a = make_account("facebook")
    a.profile_id = p.profile_id
    db.add(PlanTarget(plan_id=p.id, account_id=a.id, workspace_id=workspace.id))
    first = video(db, p, "Synthetic first")
    second = video(db, p, "Synthetic replacement")
    db.commit()
    return p, a, first, second, at


def edit(client, db, p, a, at, **data):
    db.refresh(p)
    return post(
        client,
        f"/calendar/{p.id}/{a.id}/edit",
        {
            "revision": p.revision,
            "slot": at.isoformat(),
            "scheduled_at": at.strftime("%Y-%m-%dT%H:%M"),
            "video_id": "0",
            "action": "save",
            **data,
        },
    )


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_unified_calendar_and_edit_are_bilingual(calendar_data, web_owner, db, lang):
    p, a, first, second, at = calendar_data
    set_lang(web_owner, lang)
    response = web_owner.get("/calendar")
    assert response.status_code == 200 and "Synthetic first" in response.text
    assert "plan.reason_" not in response.text and "calendar.title" not in response.text
    page = web_owner.get(f"/calendar/{p.id}/{a.id}/edit", params={"slot": at.isoformat()})
    assert page.status_code == 200 and "Synthetic replacement" in page.text
    assert web_owner.get("/settings/billing").status_code == 200


def test_move_replace_and_exclude_share_engine_without_jobs(
    calendar_data, web_owner, db, monkeypatch, no_provider_network
):
    p, a, first, second, at = calendar_data
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    moved = at + timedelta(hours=2)
    assert (
        edit(
            web_owner, db, p, a, at, scheduled_at=moved.strftime("%Y-%m-%dT%H:%M"), video_id=second.id
        ).status_code
        == 303
    )
    db.refresh(p)
    assert not p.enabled
    preview = simulate(db, p, now=at - timedelta(hours=1))
    entry = next(e for row in preview["rows"] if row.at == moved for e in row.entries)
    assert entry.video.id == second.id and entry.original_at == at and entry.customized
    # Moving past the original instant must not make the edited slot disappear.
    assert any(row.at == moved for row in simulate(db, p, now=at + timedelta(minutes=1))["rows"])
    # A later explicit choice cannot violate the repeat interval.
    next_at = at + timedelta(days=1)
    edit(web_owner, db, p, a, next_at, video_id=second.id)
    preview = simulate(db, p, now=at - timedelta(hours=1))
    entry = next(e for row in preview["rows"] if row.at == next_at for e in row.entries)
    assert entry.video is None and entry.reason == "cooldown"
    edit(web_owner, db, p, a, next_at, action="exclude")
    assert (
        next(
            e
            for row in simulate(db, p, now=at - timedelta(hours=1))["rows"]
            if row.at == next_at
            for e in row.entries
        ).reason
        == "excluded"
    )
    assert db.scalar(select(func.count()).select_from(Job)) == 0
    assert db.scalar(select(func.count()).select_from(ScheduledPost)) == 0
    assert not no_provider_network.calls


def test_calendar_stale_conflict_foreign_content_and_csrf(calendar_data, web_owner, db, workspace):
    p, a, first, second, at = calendar_data
    endpoint = f"/calendar/{p.id}/{a.id}/edit"
    assert web_owner.post(endpoint, data={}).status_code == 403
    response = edit(web_owner, db, p, a, at, revision=p.revision + 1)
    assert response.status_code == 303 and db.scalar(select(func.count()).select_from(PlanSlotOverride)) == 0
    response = edit(web_owner, db, p, a, at, scheduled_at=(at + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"))
    assert response.status_code == 303 and db.scalar(select(func.count()).select_from(PlanSlotOverride)) == 0
    other = create_plan(web_owner, db, "Other person")
    wrong = video(db, other, "Foreign person content")
    db.commit()
    edit(web_owner, db, p, a, at, video_id=wrong.id)
    assert db.scalar(select(func.count()).select_from(PlanSlotOverride)) == 0
    foreign = Workspace(name="Other tenant")
    db.add(foreign)
    db.flush()
    db.commit()
    assert web_owner.get("/calendar/999999/99999/edit", params={"slot": at.isoformat()}).status_code == 404


def test_deleted_replacement_does_not_fall_back_to_another_item(calendar_data, web_owner, db):
    p, a, first, second, at = calendar_data
    edit(web_owner, db, p, a, at, video_id=second.id)
    db.delete(second)
    db.commit()
    entry = simulate(db, p, now=at - timedelta(hours=1))["rows"][0].entries[0]
    assert entry.video is None and entry.reason == "replacement_unavailable"


@pytest.fixture
def mail_ready(monkeypatch):
    for key, value in {
        "smtp_host": "smtp.test.invalid",
        "smtp_username": "synthetic",
        "smtp_password": "synthetic-secret",
    }.items():
        monkeypatch.setattr(get_settings(), key, value)


@pytest.mark.parametrize("role", ["owner", "editor"])
@pytest.mark.parametrize("publishing_enabled", [False, True])
def test_invitation_mail_encrypted_delivered_and_wiped(
    db, workspace, admin_user, mail_ready, monkeypatch, no_provider_network, role, publishing_enabled
):
    monkeypatch.setattr(get_settings(), "publishing_enabled", publishing_enabled)
    row, url = access.issue_invitation(db, workspace.id, "invite@example.com", role, admin_user.id)
    db.commit()
    queued = db.scalar(select(MailOutbox))
    assert url not in queued.payload_enc and "invite@example.com" not in queued.payload_enc
    assert decrypt_json(queued.payload_enc)["url"] == url
    sent = []
    monkeypatch.setattr(mail, "send_email", lambda *a, **kw: sent.append((a, kw)))
    mail.deliver(db, {})
    db.refresh(queued)
    assert queued.status == "sent" and queued.payload_enc is None and len(sent) == 1
    body = sent[0][0][2]
    assert body.count(url) == 1
    assert [line[:2] for line in body.splitlines() if line[:1].isdigit()] == [
        "1.",
        "2.",
        "3.",
        "4.",
        "5.",
        "1.",
        "2.",
        "3.",
        "4.",
        "5.",
    ]
    for instruction in (
        "عيّن كلمة مرور",
        "أضف شخصًا",
        "اختر الصفحات",
        "Google Drive",
        "ارفع ملفاتك من جهازك",
        "ابدأ الاستيراد",
        "فاصل إعادة المحتوى",
        "معاينة الجدول",
        "set your password",
        "Add a person",
        "choose your pages",
        "upload files from your device",
        "Start importing",
        "content repeat interval",
        "schedule preview",
    ):
        assert instruction in body
    assert ("النشر ورفع المسودات مقفولان تمامًا" in body) is not publishing_enabled
    assert ("publishing and draft uploads are disabled" in body) is not publishing_enabled
    assert "بدون كلمات مرور أو أكواد تحقق" in body
    assert "without passwords or verification codes" in body
    mail.deliver(db, {})
    assert len(sent) == 1
    assert db.scalar(select(func.count()).select_from(Job)) == 0
    assert db.scalar(select(func.count()).select_from(ScheduledPost)) == 0
    assert not no_provider_network.calls


@pytest.mark.parametrize("smtp_ssl", [False, True])
def test_invitation_steps_survive_smtp_message_encoding(
    db, workspace, admin_user, mail_ready, monkeypatch, smtp_ssl
):
    from email import policy
    from email.parser import BytesParser

    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    monkeypatch.setattr(get_settings(), "smtp_ssl", smtp_ssl)
    _, url = access.issue_invitation(db, workspace.id, "invite@example.com", "owner", admin_user.id)
    db.commit()
    messages = []

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def starttls(self, **kwargs):
            assert not smtp_ssl

        def login(self, username, password):
            assert username == "synthetic" and password == "synthetic-secret"

        def send_message(self, message):
            messages.append(BytesParser(policy=policy.default).parsebytes(message.as_bytes()))
            return {}

    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    mail.deliver(db, {})
    assert len(messages) == 1
    message = messages[0]
    assert str(message["To"]) == "invite@example.com"
    assert str(message["Subject"]) == "Your Repeater invitation | دعوتك إلى Repeater"
    assert message.get_content_type() == "text/plain"
    body = message.get_content()
    assert body.count(url) == 1 and url in body.splitlines()
    assert "5. حدّد أيام ومواعيد النشر" in body
    assert "5. Choose your posting days" in body
    assert "النشر ورفع المسودات مقفولان تمامًا" in body
    assert "publishing and draft uploads are disabled" in body


@pytest.mark.parametrize("kind", ["reset", "alert"])
def test_other_mail_does_not_include_invitation_steps(
    db, workspace, admin_user, mail_ready, monkeypatch, kind
):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    if kind == "reset":
        row, url = access.issue_reset(db, admin_user.id, None)
        mail.queue_link(db, row, url, kind, admin_user.email, workspace.id)
        content = f"\n{url}"
    else:
        mail.queue_operator_alert(db, "synthetic_alert", workspace_id=workspace.id)
        content = f"synthetic_alert: 1. Workspace: {workspace.id}.\n{get_settings().base_url}/activity"
    db.commit()
    sent = []
    monkeypatch.setattr(mail, "send_email", lambda *a, **kw: sent.append(a))
    mail.deliver(db, {})
    assert len(sent) == 1
    assert sent[0][2] == (
        f"{content}\n\nRepeater · Magic Lap\nIf you did not request this, ignore this email.\n"
        "لو لم تطلب هذه الرسالة، تجاهلها."
    )


@pytest.mark.parametrize("reason", ["revoked", "used", "expired"])
def test_invalid_invitation_is_never_sent(db, workspace, admin_user, mail_ready, monkeypatch, reason):
    row, _ = access.issue_invitation(db, workspace.id, "invite@example.com", "editor", admin_user.id)
    if reason == "expired":
        row.expires_at = utcnow() - timedelta(seconds=1)
    else:
        setattr(row, reason + "_at", utcnow())
    db.commit()
    monkeypatch.setattr(mail, "send_email", lambda *a, **kw: pytest.fail("stale link sent"))
    mail.deliver(db, {})
    assert db.scalar(select(MailOutbox)).payload_enc is None


def test_smtp_retry_redacts_error_and_preserves_same_link(
    db, workspace, admin_user, mail_ready, monkeypatch, caplog
):
    row, url = access.issue_invitation(db, workspace.id, "invite@example.com", "editor", admin_user.id)
    db.commit()

    def fail(*a, **kw):
        raise RuntimeError(url + " synthetic-secret")

    monkeypatch.setattr(mail, "send_email", fail)
    mail.deliver(db, {})
    queued = db.scalar(select(MailOutbox))
    assert (
        queued.status == "queued" and queued.attempts == 1 and decrypt_json(queued.payload_enc)["url"] == url
    )
    assert url not in caplog.text and "synthetic-secret" not in caplog.text


def test_automatic_recovery_does_not_recover_operator_or_unknown(
    db, workspace, admin_user, mail_ready, monkeypatch
):
    from repeater.security import hash_password

    user = User(email="customer@example.com", password_hash=hash_password(PASSWORD))
    db.add(user)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="editor"))
    admin_user.is_platform_admin = True
    for email in [user.email, admin_user.email, "unknown@example.com"]:
        access.request_recovery(db, email)
    db.commit()
    sent = []
    monkeypatch.setattr(mail, "send_email", lambda *a, **kw: sent.append(a))
    mail.deliver(db, {})
    assert len(sent) == 1 and sent[0][0] == user.email


def test_recovery_uses_open_membership_when_an_earlier_workspace_is_closed(
    db, workspace, admin_user, mail_ready, monkeypatch
):
    from repeater.security import hash_password

    workspace.closed_at = utcnow()
    open_workspace = Workspace(name="Synthetic open workspace", paused=True)
    db.add(open_workspace)
    user = User(email="multi-workspace@example.com", password_hash=hash_password(PASSWORD))
    db.add(user)
    db.flush()
    for workspace_id in (workspace.id, open_workspace.id):
        db.add(WorkspaceMember(workspace_id=workspace_id, user_id=user.id, role="editor"))
    access.request_recovery(db, user.email)
    db.commit()
    sent = []
    monkeypatch.setattr(mail, "send_email", lambda *a, **kw: sent.append(a))
    mail.deliver(db, {})
    queued = db.scalar(select(MailOutbox))
    assert queued.workspace_id == open_workspace.id and queued.status == "sent"
    assert len(sent) == 1 and sent[0][0] == user.email


def test_bank_transfer_invoice_requires_configuration_and_manual_verification(
    web_owner, db, workspace, admin_user, monkeypatch
):
    admin_user.is_platform_admin = True
    plans = commerce.catalog(db)
    creator = next(p for p in plans if p.code == "creator")
    creator.monthly_price = 25000
    creator.available = True
    db.commit()
    with pytest.raises(commerce.LimitError):
        commerce.request_invoice(db, workspace.id, "creator")
    db.rollback()
    monkeypatch.setattr(get_settings(), "bank_transfer_details", "Synthetic review bank instructions")
    invoice = commerce.request_invoice(db, workspace.id, "creator")
    db.commit()
    assert commerce.request_invoice(db, workspace.id, "creator").id == invoice.id
    db.commit()
    assert (
        post(
            web_owner, f"/settings/billing/{invoice.id}/reference", {"reference": "synthetic-reference"}
        ).status_code
        == 303
    )
    db.refresh(invoice)
    assert invoice.status == "review"
    # A catalog edit cannot rewrite the purchased amount or limits.
    creator.monthly_price = 99000
    creator.limits = {"people": 1}
    db.commit()
    approved = post(
        web_owner,
        f"/admin/billing/{invoice.id}/review",
        {"action": "approve", "current_password": PASSWORD, "verified": "1"},
    )
    assert approved.status_code == 303
    db.refresh(invoice)
    db.refresh(workspace)
    assert (
        invoice.status == "paid" and invoice.amount == 25000 and workspace.entitlement_limits["people"] == 5
    )
    expires = workspace.subscription_until
    post(
        web_owner,
        f"/admin/billing/{invoice.id}/review",
        {"action": "approve", "current_password": PASSWORD, "verified": "1"},
    )
    db.refresh(workspace)
    assert workspace.subscription_until == expires


def test_quota_enforced_and_legacy_workspace_preserved(web_owner, db, workspace):
    commerce.catalog(db)
    workspace.service_plan = "pilot"
    workspace.entitlement_limits = {"people": 1}
    db.commit()
    create_plan(web_owner, db)
    assert post(web_owner, "/plans", {"name": "Second person"}).status_code == 403
    workspace.service_plan = "legacy"
    workspace.entitlement_limits = {}
    db.commit()
    assert post(web_owner, "/plans", {"name": "Second person"}).status_code == 303


def test_member_suspension_revokes_access_and_last_owner_is_protected(web_owner, web_editor, db, workspace):
    from tests.test_web_support import EDITOR_EMAIL

    editor_user = db.scalar(select(User).where(User.email == EDITOR_EMAIL))
    response = post(
        web_owner,
        f"/settings/members/{editor_user.id}/access",
        {"role": "suspended", "current_password": PASSWORD},
    )
    assert response.status_code == 303
    assert web_editor.get("/", follow_redirects=False).status_code == 303
    member = db.get(WorkspaceMember, (workspace.id, editor_user.id), populate_existing=True)
    assert member.role == "suspended"
    owner = db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id, WorkspaceMember.role == "owner"
        )
    )
    post(
        web_owner,
        f"/settings/members/{owner.user_id}/access",
        {"role": "suspended", "current_password": PASSWORD},
    )
    db.refresh(owner)
    assert owner.role == "owner"


def test_close_workspace_cancels_access_without_touching_other_tenant(web_owner, db, workspace, make_account):
    account = make_account("facebook")
    other = Workspace(name="Other workspace", paused=False)
    db.add(other)
    db.commit()
    response = post(
        web_owner, "/settings/workspace/close", {"confirmation": workspace.name, "current_password": PASSWORD}
    )
    assert response.status_code == 303
    db.refresh(workspace)
    db.refresh(account)
    db.refresh(other)
    assert workspace.closed_at and workspace.paused and not account.token_enc
    assert other.closed_at is None and not other.paused
    assert web_owner.get("/", follow_redirects=False).status_code == 303


def test_billing_and_admin_scope(web_editor, web_owner, db, workspace):
    assert post(web_editor, "/settings/billing/request", {"plan_code": "creator"}).status_code == 403
    assert web_owner.get("/admin/billing").status_code == 403
    assert web_owner.get("/admin/operations").status_code == 403
    assert post(web_owner, "/settings/billing/999999/reference", {"reference": "test"}).status_code == 404


def test_suspended_customer_cannot_receive_automatic_recovery(db, workspace, mail_ready, monkeypatch):
    from repeater.security import hash_password

    user = User(email="suspended@example.com", password_hash=hash_password(PASSWORD))
    db.add(user)
    db.flush()
    member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="editor")
    db.add(member)
    db.commit()
    reset, url = access.issue_reset(db, user.id, None)
    mail.queue_link(db, reset, url, "reset", user.email, workspace.id)
    member.role = "suspended"
    access.request_recovery(db, user.email)
    db.commit()
    monkeypatch.setattr(mail, "send_email", lambda *a, **kw: pytest.fail("suspended user received mail"))
    mail.deliver(db, {})
    assert db.scalar(select(MailOutbox)).status == "expired"


def test_quota_lock_allows_foreign_key_readers_but_serializes_concurrent_creation(db, workspace):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from repeater.db import get_engine
    from repeater.models import CreatorProfile, Video

    workspace.service_plan = "pilot"
    workspace.entitlement_limits = {"people": 1}
    db.commit()
    # An unrelated insert holds a FK key-share lock on the workspace until commit.
    db.add(Video(workspace_id=workspace.id, title="Uncommitted synthetic FK reader"))
    db.flush()
    with Session(get_engine()) as second:
        second.execute(text("SET LOCAL lock_timeout = '500ms'"))
        commerce.require_capacity(second, workspace.id, "people")
        second.rollback()
    db.rollback()

    workspace_id = workspace.id

    def create(index):
        with Session(get_engine()) as session:
            try:
                commerce.require_capacity(session, workspace_id, "people")
                session.add(CreatorProfile(workspace_id=workspace_id, name=f"Synthetic concurrent {index}"))
                session.commit()
                return "created"
            except commerce.LimitError:
                session.rollback()
                return "limited"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create, range(2))) == ["created", "limited"]


def test_expired_subscription_keeps_read_access_and_blocks_new_work(web_owner, db, workspace):
    workspace.subscription_until = utcnow() - timedelta(seconds=1)
    db.commit()
    assert web_owner.get("/library").status_code == 200
    assert web_owner.get("/calendar").status_code == 200
    assert post(web_owner, "/plans", {"name": "Expired plan"}).status_code == 403
    assert not commerce.work_allowed(workspace)


def test_calendar_edits_cancel_untouched_actual_reservation(calendar_data, web_owner, db):
    from repeater.models import PostStatus

    p, a, first, second, at = calendar_data
    reservation = ScheduledPost(
        workspace_id=p.workspace_id,
        video_id=first.id,
        connected_account_id=a.id,
        platform=a.platform,
        idempotency_key="synthetic-calendar-reservation",
        scheduled_at=at,
        slot_at=at,
        plan_id=p.id,
        plan_revision=p.revision,
        created_by="plan",
    )
    db.add(reservation)
    db.commit()
    response = web_owner.get("/calendar")
    assert response.status_code == 200 and "حجز فعلي" in response.text
    edit(web_owner, db, p, a, at, action="exclude")
    db.refresh(reservation)
    assert reservation.status == PostStatus.CANCELED and not p.enabled
    assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_closed_workspace_can_only_be_reopened_by_operator_and_stays_paused(web_owner, db, admin_user):
    closed = Workspace(name="Synthetic closed customer", closed_at=utcnow(), paused=True)
    db.add(closed)
    db.commit()
    endpoint = f"/admin/customers/{closed.id}/reopen"
    assert post(web_owner, endpoint, {"current_password": PASSWORD}).status_code == 403
    admin_user.is_platform_admin = True
    db.commit()
    assert post(web_owner, endpoint, {"current_password": "incorrect"}).status_code == 403
    assert post(web_owner, endpoint, {"current_password": PASSWORD}).status_code == 303
    db.refresh(closed)
    assert closed.closed_at is None and closed.paused


def test_calendar_never_reads_or_edits_another_tenant(calendar_data, web_owner, db, make_account):
    from repeater.models import ContentPlan, CreatorProfile

    p, account, first, second, at = calendar_data
    foreign = Workspace(name="Synthetic foreign tenant")
    db.add(foreign)
    db.flush()
    person = CreatorProfile(workspace_id=foreign.id, name="Synthetic private person")
    db.add(person)
    db.flush()
    plan = ContentPlan(
        workspace_id=foreign.id,
        profile_id=person.id,
        timezone="UTC",
        start_date=at.date(),
        times=["15:00"],
        weekdays=list(range(7)),
    )
    db.add(plan)
    db.commit()
    target = make_account("facebook", workspace_id=foreign.id)
    target.profile_id = person.id
    db.add(PlanTarget(workspace_id=foreign.id, plan_id=plan.id, account_id=target.id))
    secret = video(db, plan, "Other tenant synthetic private content")
    db.commit()
    assert secret.title not in web_owner.get("/calendar").text
    url = f"/calendar/{plan.id}/{target.id}/edit"
    assert web_owner.get(url, params={"slot": at.isoformat()}).status_code == 404
    assert post(web_owner, url, {"slot": at.isoformat(), "action": "exclude"}).status_code == 404
    assert db.scalar(select(func.count()).select_from(PlanSlotOverride)) == 0


def test_operator_health_exposes_unknown_backup_evidence_without_claiming_success(web_owner, admin_user, db):
    admin_user.is_platform_admin = True
    db.commit()
    set_lang(web_owner, "en")
    response = web_owner.get("/admin/operations")
    assert response.status_code == 200
    assert "Backup &amp; recovery evidence" in response.text
    assert "Oldest due job age" in response.text
    assert "Not configured" in response.text
    assert "External availability monitor" in response.text
