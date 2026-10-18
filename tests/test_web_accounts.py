"""Accounts page: connect buttons, not-configured state, reconnect and owner-only disconnect."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from repeater.config import get_settings
from repeater.models import AccountStatus, ConnectedAccount, Job, RotationRule, Workspace
from repeater.platforms import override
from tests.test_web_support import post, set_lang

pytest_plugins = ["tests.test_web_support"]


class FakeRevoke:
    def __init__(self, name: str) -> None:
        self.name = name
        self.uses_pkce = False
        self.revoked: list[str] = []

    def revoke(self, ctx) -> None:
        self.revoked.append(ctx.external_id)


def test_connect_buttons(web_owner):
    set_lang(web_owner, "en")
    html = web_owner.get("/accounts").text
    for purpose in ("tiktok", "youtube", "meta", "gdrive"):
        assert f'href="/connect/{purpose}"' in html, purpose
    assert "Instagram &amp; Facebook" in html
    # Connect buttons are links (the provider is reached by redirect), never forms.
    assert 'action="/connect' not in html


def test_not_configured_platform(web_owner, monkeypatch):
    monkeypatch.setattr(get_settings(), "meta_app_id", "")
    set_lang(web_owner, "en")
    html = web_owner.get("/accounts").text
    assert 'href="/connect/meta"' not in html and "Not configured" in html
    assert 'href="/connect/tiktok"' in html


def test_accounts_list_and_reconnect(web_owner, db, make_account):
    acc = make_account(
        "instagram", display_name="magic.ig", extra={"username": "magic.ig", "fb_user_id": "1"}
    )
    acc.status = AccountStatus.NEEDS_RECONNECT
    acc.last_error = "Error validating access token"
    db.commit()
    set_lang(web_owner, "en")
    html = web_owner.get("/accounts").text
    assert "magic.ig" in html and "Needs reconnecting" in html and "Error validating access token" in html
    assert html.count('href="/connect/meta"') == 2  # the connect card and this account's reconnect


@pytest.mark.parametrize("client_fixture", ["web_owner", "web_editor"])
@pytest.mark.parametrize("lang", ["ar", "en"])
def test_account_list_hides_disconnected_but_keeps_expired_connections(
    request, db, make_account, client_fixture, lang
):
    client = request.getfixturevalue(client_fixture)
    disconnected = make_account("facebook", display_name="Explicitly disconnected page")
    disconnected.status, disconnected.token_enc = AccountStatus.DISABLED, None
    expired = make_account("youtube", display_name="Channel needing renewed authorization")
    expired.status = AccountStatus.NEEDS_RECONNECT
    active = make_account("instagram", display_name="Connected Instagram")
    db.commit()
    set_lang(client, lang)
    html = client.get("/accounts").text
    assert disconnected.display_name not in html
    assert expired.display_name in html and active.display_name in html
    assert 'href="/connect/youtube"' in html
    db.refresh(disconnected)
    assert disconnected.status == AccountStatus.DISABLED  # Hidden, not deleted early.


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_only_disconnected_accounts_show_empty_state_and_open_connection_choices(
    web_owner, db, make_account, lang
):
    account = make_account("tiktok", display_name="Disconnected synthetic creator")
    account.status, account.token_enc = AccountStatus.DISABLED, None
    db.commit()
    set_lang(web_owner, lang)
    html = web_owner.get("/accounts").text
    assert account.display_name not in html
    assert 'class="account-list"' not in html and 'class="account-bulk-controls"' not in html
    assert '<details class="card account-connections" open>' in html
    assert ("لا توجد حسابات مربوطة" if lang == "ar" else "No accounts connected") in html
    assert 'href="/connect/tiktok"' in html
    assert db.get(ConnectedAccount, account.id) is not None


def test_hidden_account_can_be_connected_again_from_platform_choices(web_owner, db, make_account):
    from repeater.models import CreatorProfile
    from tests.test_web_oauth import FakeProvider, _meta_accounts, _start

    account = make_account("facebook", external_id="page-1", display_name="Hidden old page")
    person = CreatorProfile(workspace_id=account.workspace_id, name="Synthetic owner")
    db.add(person)
    db.flush()
    account.profile_id = person.id
    account.status, account.token_enc = AccountStatus.DISABLED, None
    db.commit()
    assert account.display_name not in web_owner.get("/accounts").text
    with override("oauth", "meta", FakeProvider("meta", False, accounts=_meta_accounts())):
        _, state = _start(web_owner, "meta")
        response = web_owner.get(f"/oauth/meta/callback?code=synthetic&state={state}", follow_redirects=False)
    assert response.status_code == 303
    db.refresh(account)
    assert account.status == AccountStatus.ACTIVE and account.token_enc
    assert account.profile_id == person.id and "_disconnected_at" not in account.extra
    assert account.display_name in web_owner.get("/accounts").text
    assert all(job.kind not in ("publish_post", "poll_post") for job in db.scalars(select(Job)))


def test_owner_disconnects_with_confirmation(web_owner, db, workspace, make_account):
    account = make_account("tiktok", display_name="Magic TikTok")
    db.add(
        RotationRule(
            workspace_id=workspace.id,
            connected_account_id=account.id,
            platform=account.platform,
            enabled=True,
        )
    )
    db.commit()
    set_lang(web_owner, "en")
    page = web_owner.get(f"/accounts/{account.id}/disconnect")
    assert page.status_code == 200 and "Disconnect this account?" in page.text
    fake = FakeRevoke("tiktok")
    with override("oauth", "tiktok", fake):
        resp = post(web_owner, f"/accounts/{account.id}/disconnect", page="/accounts")
    assert resp.status_code == 303 and resp.headers["location"] == "/accounts"
    assert fake.revoked == [account.external_id]
    db.expire_all()
    refreshed = db.get(ConnectedAccount, account.id)
    assert refreshed.status == AccountStatus.DISABLED and refreshed.token_enc is None
    assert db.execute(select(RotationRule)).scalar_one().enabled is False
    assert db.execute(select(Job).where(Job.kind == "plan_workspace")).scalars().first() is not None
    html = web_owner.get("/accounts").text
    assert "Magic TikTok was disconnected." in html
    assert 'class="account-list"' not in html
    assert f'href="/accounts/{account.id}/disconnect"' not in html
    # Disconnecting twice is harmless.
    resp = post(web_owner, f"/accounts/{account.id}/disconnect", page="/accounts")
    assert resp.status_code == 303


def test_meta_disconnect_explains_facebook_settings(web_owner, make_account):
    account = make_account("facebook", display_name="Magic Page")
    set_lang(web_owner, "en")
    assert "Apps and Websites" in web_owner.get(f"/accounts/{account.id}/disconnect").text
    with override("oauth", "meta", FakeRevoke("meta")):
        post(web_owner, f"/accounts/{account.id}/disconnect", page="/accounts")
    assert "Facebook Settings" in web_owner.get("/accounts").text


def test_editor_cannot_disconnect(web_editor, db, make_account):
    account = make_account("youtube", display_name="Channel")
    set_lang(web_editor, "en")
    html = web_editor.get("/accounts").text
    assert f"/accounts/{account.id}/disconnect" not in html
    assert "Only the workspace owner can disconnect accounts." in html
    assert web_editor.get(f"/accounts/{account.id}/disconnect").status_code == 403
    fake = FakeRevoke("google")
    with override("oauth", "google", fake):
        resp = post(web_editor, f"/accounts/{account.id}/disconnect", page="/accounts")
    assert resp.status_code == 403
    assert "Only the workspace owner can do this." in resp.text
    assert fake.revoked == []
    db.expire_all()
    assert db.get(ConnectedAccount, account.id).status == AccountStatus.ACTIVE


def test_disconnect_needs_csrf(web_owner, db, make_account):
    account = make_account("youtube")
    assert web_owner.post(f"/accounts/{account.id}/disconnect", follow_redirects=False).status_code == 403
    db.expire_all()
    assert db.get(ConnectedAccount, account.id).status == AccountStatus.ACTIVE


def test_disconnect_other_workspace_is_404(web_owner, db, make_account):
    other = Workspace(name="Other")
    db.add(other)
    db.commit()
    foreign = make_account("youtube", workspace_id=other.id)
    assert web_owner.get(f"/accounts/{foreign.id}/disconnect").status_code == 404
    assert post(web_owner, f"/accounts/{foreign.id}/disconnect", page="/accounts").status_code == 404


# --------------------------------------------------------------------------- TikTok grant sharing


def test_tiktok_disconnect_keeps_remote_grant_while_connected_in_another_workspace(
    web_owner, db, make_account
):
    """Revoking at TikTok removes the whole user-to-app authorization, and the same open_id can be
    connected in several workspaces: only the local tokens go while another connection is live."""
    other = Workspace(name="Creator workspace")
    db.add(other)
    db.commit()
    mine = make_account("tiktok", external_id="open-1", display_name="Magic TikTok")
    theirs = make_account("tiktok", external_id="open-1", workspace_id=other.id)
    fake = FakeRevoke("tiktok")
    with override("oauth", "tiktok", fake):
        resp = post(web_owner, f"/accounts/{mine.id}/disconnect", page="/accounts")
    assert resp.status_code == 303
    assert fake.revoked == []  # TikTok was not asked to revoke
    db.expire_all()
    assert db.get(ConnectedAccount, mine.id).token_enc is None
    assert db.get(ConnectedAccount, mine.id).status == AccountStatus.DISABLED
    assert db.get(ConnectedAccount, theirs.id).status == AccountStatus.ACTIVE
    assert db.get(ConnectedAccount, theirs.id).token_enc is not None


def test_tiktok_disconnect_revokes_when_the_other_connection_is_gone(web_owner, db, make_account):
    other = Workspace(name="Creator workspace")
    db.add(other)
    db.commit()
    mine = make_account("tiktok", external_id="open-1")
    theirs = make_account("tiktok", external_id="open-1", workspace_id=other.id)
    theirs.token_enc, theirs.status = None, AccountStatus.DISABLED
    unrelated = make_account("tiktok", external_id="open-2", workspace_id=other.id)
    db.commit()
    fake = FakeRevoke("tiktok")
    with override("oauth", "tiktok", fake):
        post(web_owner, f"/accounts/{mine.id}/disconnect", page="/accounts")
    assert fake.revoked == ["open-1"]
    db.expire_all()
    assert db.get(ConnectedAccount, unrelated.id).status == AccountStatus.ACTIVE


# --------------------------------------------------------------------------- Google grant sharing


def _google_pair(make_account, *, yt_sub, drive_sub, drive_workspace_id=None):
    """A YouTube channel and a Drive login with DIFFERENT refresh tokens (every connect issues a
    new one) and the given Google identities."""
    youtube = make_account(
        "youtube",
        refresh_token="rt-youtube",
        extra={"channel_id": "UC1", **({"google_sub": yt_sub} if yt_sub else {})},
    )
    drive = make_account(
        "gdrive",
        refresh_token="rt-drive",
        workspace_id=drive_workspace_id,
        extra={"email": "a@b.c", **({"google_sub": drive_sub} if drive_sub else {})},
    )
    return youtube, drive


def test_disconnecting_drive_keeps_the_youtube_grant_of_the_same_google_user(web_owner, db, make_account):
    """Google revokes every scope the user gave the project when any one token is revoked."""
    youtube, drive = _google_pair(make_account, yt_sub="g-1", drive_sub="g-1")
    fake = FakeRevoke("google")
    with override("oauth", "google", fake):
        resp = post(web_owner, f"/accounts/{drive.id}/disconnect", page="/accounts")
    assert resp.status_code == 303
    assert fake.revoked == []  # Google was not asked to revoke
    db.expire_all()
    assert db.get(ConnectedAccount, drive.id).token_enc is None
    assert db.get(ConnectedAccount, youtube.id).status == AccountStatus.ACTIVE
    assert db.get(ConnectedAccount, youtube.id).token_enc is not None


def test_google_grant_is_revoked_when_the_other_account_is_another_google_user(web_owner, db, make_account):
    _, drive = _google_pair(make_account, yt_sub="g-1", drive_sub="g-2")
    fake = FakeRevoke("google")
    with override("oauth", "google", fake):
        post(web_owner, f"/accounts/{drive.id}/disconnect", page="/accounts")
    assert fake.revoked == [drive.external_id]


def test_google_grant_is_shared_across_workspaces(web_owner, db, make_account):
    other = Workspace(name="Other workspace")
    db.add(other)
    db.commit()
    _, drive = _google_pair(make_account, yt_sub="g-1", drive_sub="g-1")
    foreign_channel = make_account("youtube", workspace_id=other.id, extra={"google_sub": "g-1"})
    fake = FakeRevoke("google")
    with override("oauth", "google", fake):
        post(web_owner, f"/accounts/{drive.id}/disconnect", page="/accounts")
    assert fake.revoked == []
    db.expire_all()
    assert db.get(ConnectedAccount, foreign_channel.id).status == AccountStatus.ACTIVE


def test_legacy_google_accounts_without_an_identity_are_never_revoked_while_another_one_is_active(
    web_owner, db, make_account
):
    youtube, drive = _google_pair(make_account, yt_sub=None, drive_sub=None)
    fake = FakeRevoke("google")
    with override("oauth", "google", fake):
        post(web_owner, f"/accounts/{drive.id}/disconnect", page="/accounts")
    assert fake.revoked == []
    db.expire_all()
    assert db.get(ConnectedAccount, drive.id).token_enc is None
    assert db.get(ConnectedAccount, youtube.id).status == AccountStatus.ACTIVE
    # The last Google connection does revoke (nothing else depends on the grant any more).
    with override("oauth", "google", fake):
        post(web_owner, f"/accounts/{youtube.id}/disconnect", page="/accounts")
    assert fake.revoked == [youtube.external_id]


def test_disconnect_page_states_the_retention(web_owner, make_account):
    account = make_account("tiktok")
    set_lang(web_owner, "en")
    html = web_owner.get(f"/accounts/{account.id}/disconnect").text
    assert "Within 7 days the account record" in html
    set_lang(web_owner, "ar")
    assert "خلال 7 أيام" in web_owner.get(f"/accounts/{account.id}/disconnect").text
