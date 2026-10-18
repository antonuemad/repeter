"""Bulk disconnection uses synthetic accounts and fake providers; no live account access."""

import re
from contextlib import ExitStack

import pytest
import respx
from itsdangerous import TimestampSigner
from sqlalchemy import select

from repeater.config import get_settings
from repeater.models import (
    AccountStatus,
    Job,
    Platform,
    PostStatus,
    RotationRule,
    ScheduledPost,
    Source,
    SourceKind,
    Video,
    VideoStatus,
    Workspace,
)
from repeater.platforms import override
from repeater.timeutil import utcnow
from repeater.web.routes.accounts import _bulk_signer
from tests.test_web_accounts import FakeRevoke
from tests.test_web_support import post, set_lang

pytest_plugins = ["tests.test_web_support"]


@pytest.fixture(autouse=True)
def fake_network(monkeypatch):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    with respx.mock(assert_all_called=False):
        with ExitStack() as stack:
            providers = {name: FakeRevoke(name) for name in ("google", "meta", "instagram", "tiktok")}
            for name, fake in providers.items():
                stack.enter_context(override("oauth", name, fake))
            yield providers


def review(client, scope="all"):
    response = client.get("/accounts/disconnect", params={"scope": scope})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    match = re.search(r'name="confirmation" value="([^"]+)"', response.text)
    return response, match[1] if match else ""


def confirm(client, token):
    return post(client, "/accounts/disconnect", {"confirmation": token}, page="/accounts")


@pytest.mark.parametrize("scope", ["all", *Platform])
def test_bulk_all_or_one_platform_only(web_owner, db, workspace, make_account, fake_network, scope):
    publishing_enabled = get_settings().publishing_enabled
    rows = [make_account(p) for p in Platform]
    other = Workspace(name="Other tenant")
    db.add(other)
    db.commit()
    foreign = make_account("facebook", workspace_id=other.id)
    page, token = review(web_owner, scope)
    assert token and 'action="/accounts/disconnect"' in page.text
    assert not list(db.scalars(select(Job)))  # Reviewing is read-only.
    assert all(a.status == AccountStatus.ACTIVE for a in rows)
    response = confirm(web_owner, token)
    assert response.status_code == 303 and response.headers["location"] == "/accounts"
    db.expire_all()
    for account in rows:
        selected = scope == "all" or account.platform == scope
        assert (account.status == AccountStatus.DISABLED) == selected
        assert (account.token_enc is None) == selected
    assert foreign.status == AccountStatus.ACTIVE and foreign.token_enc
    jobs = list(db.scalars(select(Job)))
    assert len(jobs) == 1 and jobs[0].kind == "plan_workspace"
    assert get_settings().publishing_enabled == publishing_enabled
    # The same confirmation cannot revoke twice or schedule another job.
    revoked = sum(len(p.revoked) for p in fake_network.values())
    assert confirm(web_owner, token).status_code == 409
    assert sum(len(p.revoked) for p in fake_network.values()) == revoked


def test_bulk_counts_exclude_disconnected_and_include_reconnect(web_owner, db, make_account):
    a = make_account("tiktok")
    a.status = AccountStatus.NEEDS_RECONNECT
    b = make_account("youtube")
    b.status, b.token_enc = AccountStatus.DISABLED, None
    db.commit()
    set_lang(web_owner, "en")
    html = web_owner.get("/accounts").text
    assert '<option value="all">All platforms (1)</option>' in html
    assert '<option value="tiktok">TikTok (1)</option>' in html
    assert '<option value="youtube">' not in html
    page, token = review(web_owner)
    assert a.display_name in page.text and b.display_name not in page.text
    assert confirm(web_owner, token).status_code == 303


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_bulk_confirmation_retention_and_provider_notes(web_owner, make_account, lang):
    for p in Platform:
        make_account(p)
    make_account("instagram", extra={"oauth_provider": "instagram"})
    set_lang(web_owner, lang)
    html = review(web_owner)[0].text
    for text in (
        ("7 أيام", "المحتوى المستورد", "العدد: 6")
        if lang == "ar"
        else ("within 7 days", "Imported content", "6 accounts")
    ):
        assert text in html
    assert "myaccount.google.com/permissions" in html and "Instagram" in html
    assert "confirmation" in html and "access-token" not in html and "refresh-token" not in html


def test_editor_cannot_review_or_submit_bulk(web_editor, db, make_account, fake_network):
    a = make_account("youtube")
    assert 'action="/accounts/disconnect"' not in web_editor.get("/accounts").text
    assert web_editor.get("/accounts/disconnect").status_code == 403
    assert confirm(web_editor, "anything").status_code == 403
    db.refresh(a)
    assert a.status == AccountStatus.ACTIVE
    assert not any(p.revoked for p in fake_network.values())


def test_bulk_requires_csrf(web_owner, db, make_account):
    a = make_account("youtube")
    token = review(web_owner)[1]
    assert web_owner.post("/accounts/disconnect", data={"confirmation": token}).status_code == 403
    db.refresh(a)
    assert a.status == AccountStatus.ACTIVE


@pytest.mark.parametrize("invalid", ["missing", "tampered", "expired", "foreign_workspace", "foreign_user"])
def test_bulk_requires_current_bound_confirmation(web_owner, db, make_account, monkeypatch, invalid):
    a = make_account("tiktok")
    set_lang(web_owner, "en")
    token = review(web_owner)[1]
    if invalid == "expired":
        payload = _bulk_signer().loads(token)
        real_time = TimestampSigner.get_timestamp
        # Age only the action token; the browser's signed session stays current.
        with monkeypatch.context() as patch:
            patch.setattr(TimestampSigner, "get_timestamp", lambda self: real_time(self) - 901)
            token = _bulk_signer().dumps(payload)
    elif invalid == "missing":
        token = ""
    elif invalid == "tampered":
        token += "x"
    elif invalid.startswith("foreign"):
        payload = _bulk_signer().loads(token)
        payload[0 if invalid == "foreign_workspace" else 1] += 999
        token = _bulk_signer().dumps(payload)
    assert confirm(web_owner, token).status_code == 303
    assert "Choose the accounts again" in web_owner.get("/accounts").text
    db.refresh(a)
    assert a.status == AccountStatus.ACTIVE and a.token_enc
    assert not list(db.scalars(select(Job)))


@pytest.mark.parametrize("change", ["new_account", "reconnect", "disconnect"])
def test_bulk_changed_selection_requires_new_review(web_owner, db, make_account, change, fake_network):
    a = make_account("tiktok")
    token = review(web_owner)[1]
    if change == "new_account":
        make_account("youtube")
    elif change == "reconnect":
        from repeater.accounts import save_tokens
        from repeater.platforms.base import OAuthTokens

        save_tokens(a, OAuthTokens(access_token="new-synthetic-token"))
    else:
        a.status, a.token_enc = AccountStatus.DISABLED, None
    db.commit()
    set_lang(web_owner, "en")
    result = confirm(web_owner, token)
    assert result.status_code == 409 and "changed since your review" in result.text
    assert not any(p.revoked for p in fake_network.values())
    assert not list(db.scalars(select(Job)))


def test_platform_disconnect_preserves_shared_google_and_foreign_tiktok(
    web_owner, db, make_account, fake_network
):
    youtube = make_account("youtube", extra={"google_sub": "same-user"})
    drive = make_account("gdrive", extra={"google_sub": "same-user"})
    assert confirm(web_owner, review(web_owner, "youtube")[1]).status_code == 303
    assert fake_network["google"].revoked == []
    other = Workspace(name="Another workspace")
    db.add(other)
    db.commit()
    mine = make_account("tiktok", external_id="same-user")
    theirs = make_account("tiktok", external_id="same-user", workspace_id=other.id)
    assert confirm(web_owner, review(web_owner, "all")[1]).status_code == 303
    assert fake_network["google"].revoked == [drive.external_id]
    assert fake_network["tiktok"].revoked == []
    db.expire_all()
    assert not youtube.token_enc and not mine.token_enc
    assert theirs.status == AccountStatus.ACTIVE and theirs.token_enc


def test_bulk_revokes_google_grant_once_for_selected_siblings(web_owner, make_account, fake_network):
    make_account("youtube", extra={"google_sub": "same-user"})
    drive = make_account("gdrive", extra={"google_sub": "same-user"})
    assert confirm(web_owner, review(web_owner)[1]).status_code == 303
    assert fake_network["google"].revoked == [drive.external_id]


def test_bulk_preserves_google_grant_in_other_workspace(web_owner, db, make_account, fake_network):
    other = Workspace(name="Other Google workspace")
    db.add(other)
    db.commit()
    mine = make_account("youtube", extra={"google_sub": "shared-user"})
    theirs = make_account("gdrive", extra={"google_sub": "shared-user"}, workspace_id=other.id)
    assert confirm(web_owner, review(web_owner)[1]).status_code == 303
    assert fake_network["google"].revoked == []
    db.expire_all()
    assert mine.status == AccountStatus.DISABLED and not mine.token_enc
    assert theirs.status == AccountStatus.ACTIVE and theirs.token_enc


def test_large_batch_uses_one_confirmation_and_one_replan(web_owner, db, make_account, fake_network):
    rows = [make_account("facebook", display_name=f"Synthetic page {i}") for i in range(150)]
    token = review(web_owner)[1]
    assert len(token) < 500
    assert confirm(web_owner, token).status_code == 303
    db.expire_all()
    assert all(a.status == AccountStatus.DISABLED and not a.token_enc for a in rows)
    assert len(fake_network["meta"].revoked) == 150
    assert len(list(db.scalars(select(Job).where(Job.kind == "plan_workspace")))) == 1


def test_bulk_empty_and_invalid_scope(web_owner, db, make_account):
    page, token = review(web_owner)
    assert not token and 'action="/accounts/disconnect"' not in page.text
    assert 'class="account-bulk-controls"' not in web_owner.get("/accounts").text
    assert web_owner.get("/accounts/disconnect?scope=unknown").status_code == 400
    make_account("youtube")
    assert not review(web_owner, "tiktok")[1]
    assert not list(db.scalars(select(Job)))


def test_bulk_stops_work_keeps_library_and_tolerates_remote_revoke_failure(
    web_owner, db, workspace, make_account, fake_network, monkeypatch
):
    a = make_account("tiktok")
    b = make_account("facebook")
    content = Video(workspace_id=workspace.id, title="Synthetic original", status=VideoStatus.READY)
    db.add(content)
    db.flush()
    scheduled = ScheduledPost(
        workspace_id=workspace.id,
        video_id=content.id,
        connected_account_id=a.id,
        platform=a.platform,
        idempotency_key="synthetic-bulk-disconnect-post",
        scheduled_at=utcnow(),
        status=PostStatus.SCHEDULED,
    )
    source = Source(workspace_id=workspace.id, kind=SourceKind.PLATFORM_API, connected_account_id=a.id)
    rule = RotationRule(
        workspace_id=workspace.id, connected_account_id=b.id, platform=b.platform, enabled=True
    )
    db.add_all([scheduled, source, rule])
    db.commit()

    def unavailable(ctx):
        raise RuntimeError("synthetic revoke outage")

    monkeypatch.setattr(fake_network["tiktok"], "revoke", unavailable)
    assert confirm(web_owner, review(web_owner)[1]).status_code == 303
    db.expire_all()
    assert scheduled.status == PostStatus.CANCELED
    assert not source.enabled and not rule.enabled
    assert content.status == VideoStatus.READY
    assert all(a.status == AccountStatus.DISABLED and not a.token_enc for a in (a, b))
    assert all(job.kind == "plan_workspace" for job in db.scalars(select(Job)))
