"""Accounts page: connect buttons, not-configured state, reconnect and owner-only disconnect."""

from __future__ import annotations

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
    assert "Magic TikTok was disconnected." in web_owner.get("/accounts").text
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
