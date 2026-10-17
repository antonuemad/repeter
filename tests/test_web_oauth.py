"""OAuth connect flow: /connect/{purpose} and /oauth/{provider}/callback."""

from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select, update

from repeater.config import get_settings
from repeater.models import ConnectedAccount, Job, OAuthState, Platform, RotationRule
from repeater.platforms import override
from repeater.platforms.base import ConnectedAccountData, OAuthTokens, PermanentError
from repeater.timeutil import utcnow
from tests.test_web_support import EDITOR_EMAIL, PASSWORD, Recorder, login, set_lang

pytest_plugins = ["tests.test_web_support"]


class FakeProvider:
    def __init__(
        self, name: str, uses_pkce: bool, accounts: list[ConnectedAccountData] | None = None
    ) -> None:
        self.name = name
        self.uses_pkce = uses_pkce
        self.accounts = accounts or []
        self.authorize_calls: list[dict[str, Any]] = []
        self.exchange_calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    def authorize_url(
        self, *, state: str, redirect_uri: str, code_challenge: str | None, purpose: str
    ) -> str:
        self.authorize_calls.append(
            {
                "state": state,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "purpose": purpose,
            }
        )
        return f"https://auth.{self.name}.example/authorize?state={state}"

    def exchange_code(self, *, code: str, redirect_uri: str, code_verifier: str | None, purpose: str):
        self.exchange_calls.append(
            {"code": code, "redirect_uri": redirect_uri, "code_verifier": code_verifier, "purpose": purpose}
        )
        if self.error:
            raise self.error
        return self.accounts

    def refresh(self, account):  # pragma: no cover - not used here
        raise NotImplementedError

    def revoke(self, account):  # pragma: no cover - not used here
        return None


def _tokens() -> OAuthTokens:
    return OAuthTokens(
        access_token="secret-access",
        refresh_token="secret-refresh",
        expires_at=utcnow() + timedelta(hours=24),
    )


def _tiktok_account(external_id: str = "open-1") -> ConnectedAccountData:
    return ConnectedAccountData(
        platform=Platform.TIKTOK,
        external_id=external_id,
        display_name="Magic TikTok",
        tokens=_tokens(),
        extra={"open_id": external_id, "username": "magiclap"},
    )


def _meta_accounts() -> list[ConnectedAccountData]:
    return [
        ConnectedAccountData(
            platform=Platform.FACEBOOK,
            external_id="page-1",
            display_name="Magic Page",
            tokens=_tokens(),
            extra={"page_id": "page-1", "fb_user_id": "fb-9"},
        ),
        ConnectedAccountData(
            platform=Platform.INSTAGRAM,
            external_id="ig-1",
            display_name="magic.ig",
            tokens=_tokens(),
            extra={"ig_user_id": "ig-1", "page_id": "page-1", "username": "magic.ig", "fb_user_id": "fb-9"},
        ),
    ]


def _start(client, purpose: str) -> tuple[Any, str]:
    resp = client.get(f"/connect/{purpose}", follow_redirects=False)
    assert resp.status_code == 303, resp.text[:300]
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    return resp, state


@pytest.mark.parametrize(
    ("purpose", "provider", "pkce"),
    [
        ("tiktok", "tiktok", False),
        ("youtube", "google", True),
        ("gdrive", "google", True),
        ("meta", "meta", False),
        ("instagram", "instagram", False),
    ],
)
def test_connect_creates_state_and_redirects(web_owner, db, admin_user, purpose, provider, pkce):
    fake = FakeProvider(provider, uses_pkce=pkce)
    with override("oauth", provider, fake):
        resp, state = _start(web_owner, purpose)
    assert resp.headers["location"].startswith(f"https://auth.{provider}.example/authorize")
    call = fake.authorize_calls[0]
    assert call["purpose"] == purpose
    assert call["redirect_uri"] == f"https://rotator.test/oauth/{provider}/callback"
    record = db.get(OAuthState, state)
    assert record is not None
    assert (record.provider, record.purpose, record.user_id) == (provider, purpose, admin_user.id)
    assert len(state) >= 40
    if pkce:
        verifier = record.code_verifier
        assert verifier and 43 <= len(verifier) <= 128
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        assert call["code_challenge"] == expected
    else:
        assert record.code_verifier is None and call["code_challenge"] is None


def test_connect_unknown_purpose_is_404(web_owner):
    assert web_owner.get("/connect/myspace", follow_redirects=False).status_code == 404


def test_connect_requires_login(web_client):
    resp = web_client.get("/connect/tiktok", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")


def test_connect_not_configured(web_owner, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "tiktok_client_secret", "")
    resp = web_owner.get("/connect/tiktok", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/accounts"
    set_lang(web_owner, "en")
    assert "TikTok is not configured on this server yet." in web_owner.get("/accounts").text
    assert db.execute(select(OAuthState)).first() is None


def test_connect_provider_failure_is_flashed(web_owner):
    class Broken(FakeProvider):
        def authorize_url(self, **kw):
            raise ValueError("Google sign-in requires a PKCE code_challenge")

    with override("oauth", "google", Broken("google", True)):
        resp = web_owner.get("/connect/youtube", follow_redirects=False)
    assert resp.headers["location"] == "/accounts"
    set_lang(web_owner, "en")
    assert "Could not start connecting YouTube" in web_owner.get("/accounts").text


def test_callback_success_creates_accounts_rules_and_plans(web_owner, db, monkeypatch, workspace):
    from repeater.engine import rules as engine_rules

    ensure = Recorder(result=None)
    monkeypatch.setattr(engine_rules, "ensure_rule", ensure)
    fake = FakeProvider("meta", uses_pkce=False, accounts=_meta_accounts())
    with override("oauth", "meta", fake):
        _, state = _start(web_owner, "meta")
        resp = web_owner.get(f"/oauth/meta/callback?code=the-code&state={state}", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/accounts"
    assert fake.exchange_calls == [
        {
            "code": "the-code",
            "redirect_uri": "https://rotator.test/oauth/meta/callback",
            "code_verifier": None,
            "purpose": "meta",
        }
    ]
    accounts = db.execute(select(ConnectedAccount).order_by(ConnectedAccount.id)).scalars().all()
    assert [(a.platform, a.external_id) for a in accounts] == [
        (Platform.FACEBOOK, "page-1"),
        (Platform.INSTAGRAM, "ig-1"),
    ]
    assert accounts[1].extra["fb_user_id"] == "fb-9"
    assert "secret-access" not in (accounts[0].token_enc or "")
    assert [c[0][1].external_id for c in ensure.calls] == ["page-1", "ig-1"]
    assert db.get(OAuthState, state) is None
    assert db.execute(select(Job).where(Job.kind == "plan_workspace")).scalars().first() is not None
    set_lang(web_owner, "en")
    page = web_owner.get("/accounts").text
    assert "Connected 2 account(s): Magic Page, magic.ig." in page


def test_callback_success_with_real_rules_and_pkce(web_owner, db):
    fake = FakeProvider("google", uses_pkce=True)
    fake.accounts = [
        ConnectedAccountData(
            platform=Platform.YOUTUBE,
            external_id="UC123",
            display_name="Magic Channel",
            tokens=_tokens(),
            extra={"channel_id": "UC123", "uploads_playlist_id": "UU123"},
        )
    ]
    with override("oauth", "google", fake):
        _, state = _start(web_owner, "youtube")
        verifier = db.get(OAuthState, state).code_verifier
        resp = web_owner.get(f"/oauth/google/callback?code=c&state={state}", follow_redirects=False)
    account = db.execute(select(ConnectedAccount)).scalar_one()
    # A new account goes straight to its settings, with publishing off (ux-compliance-1).
    assert resp.headers["location"] == f"/rules/{account.id}"
    assert fake.exchange_calls[0]["code_verifier"] == verifier
    rule = db.execute(select(RotationRule)).scalar_one()
    assert rule.connected_account_id == account.id and rule.platform == Platform.YOUTUBE
    assert rule.enabled is False
    set_lang(web_owner, "en")
    assert "Publishing is off for Magic Channel" in web_owner.get(f"/rules/{account.id}").text


def test_new_tiktok_account_goes_to_read_view_without_rule_or_jobs(web_owner, db):
    fake = FakeProvider("tiktok", uses_pkce=False, accounts=[_tiktok_account()])
    with override("oauth", "tiktok", fake):
        _, state = _start(web_owner, "tiktok")
        resp = web_owner.get(f"/oauth/tiktok/callback?code=c&state={state}", follow_redirects=False)
    account = db.execute(select(ConnectedAccount)).scalar_one()
    assert resp.headers["location"] == f"/accounts/{account.id}/videos"
    assert not list(db.scalars(select(RotationRule)))
    assert not list(db.scalars(select(Job)))


def test_tiktok_read_only_reconnect_stops_legacy_rule(web_owner, db, make_account):
    from repeater.engine.rules import ensure_rule

    existing = make_account("tiktok", external_id="open-1")
    rule = ensure_rule(db, existing)
    rule.enabled = True
    db.commit()
    fake = FakeProvider("tiktok", uses_pkce=False, accounts=[_tiktok_account()])
    with override("oauth", "tiktok", fake):
        _, state = _start(web_owner, "tiktok")
        resp = web_owner.get(f"/oauth/tiktok/callback?code=c&state={state}", follow_redirects=False)
    db.refresh(rule)
    assert resp.headers["location"] == f"/accounts/{existing.id}/videos"
    assert rule.enabled is False
    assert not list(db.scalars(select(Job)))


def test_callback_state_is_single_use(web_owner, db):
    fake = FakeProvider("tiktok", uses_pkce=False, accounts=[_tiktok_account()])
    with override("oauth", "tiktok", fake):
        _, state = _start(web_owner, "tiktok")
        web_owner.get(f"/oauth/tiktok/callback?code=c&state={state}", follow_redirects=False)
        web_owner.get(f"/oauth/tiktok/callback?code=c&state={state}", follow_redirects=False)
    assert len(fake.exchange_calls) == 1
    set_lang(web_owner, "en")
    assert "This connection attempt is not valid" in web_owner.get("/accounts").text


@pytest.mark.parametrize("query", ["code=c", "code=c&state=unknown-state", f"code=c&state={'x' * 200}"])
def test_callback_rejects_missing_or_unknown_state(web_owner, query):
    fake = FakeProvider("tiktok", uses_pkce=False)
    with override("oauth", "tiktok", fake):
        resp = web_owner.get(f"/oauth/tiktok/callback?{query}", follow_redirects=False)
    assert resp.headers["location"] == "/accounts"
    assert fake.exchange_calls == []


def test_callback_rejects_expired_state(web_owner, db):
    fake = FakeProvider("tiktok", uses_pkce=False)
    with override("oauth", "tiktok", fake):
        _, state = _start(web_owner, "tiktok")
        db.execute(update(OAuthState).values(created_at=utcnow() - timedelta(minutes=16)))
        db.commit()
        web_owner.get(f"/oauth/tiktok/callback?code=c&state={state}", follow_redirects=False)
    assert fake.exchange_calls == []
    assert db.get(OAuthState, state) is None
    set_lang(web_owner, "en")
    assert "took too long and expired" in web_owner.get("/accounts").text


def test_callback_rejects_provider_mismatch(web_owner):
    fake = FakeProvider("tiktok", uses_pkce=False)
    meta = FakeProvider("meta", uses_pkce=False)
    with override("oauth", "tiktok", fake), override("oauth", "meta", meta):
        _, state = _start(web_owner, "tiktok")
        web_owner.get(f"/oauth/meta/callback?code=c&state={state}", follow_redirects=False)
    assert fake.exchange_calls == [] and meta.exchange_calls == []


def test_callback_rejects_another_users_state(web_app, web_owner, db, workspace):
    from fastapi.testclient import TestClient

    from repeater.models import User, WorkspaceMember
    from repeater.security import hash_password

    other = User(email=EDITOR_EMAIL, password_hash=hash_password(PASSWORD))
    db.add(other)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=other.id, role="editor"))
    db.commit()
    fake = FakeProvider("tiktok", uses_pkce=False, accounts=[_tiktok_account()])
    with override("oauth", "tiktok", fake):
        _, state = _start(web_owner, "tiktok")
        with TestClient(web_app) as attacker:
            assert login(attacker, EDITOR_EMAIL).status_code == 303
            attacker.get(f"/oauth/tiktok/callback?code=c&state={state}", follow_redirects=False)
    assert fake.exchange_calls == []
    assert db.get(OAuthState, state) is not None  # still usable by its owner


def test_callback_user_cancelled(web_owner, db):
    fake = FakeProvider("tiktok", uses_pkce=False)
    with override("oauth", "tiktok", fake):
        _, state = _start(web_owner, "tiktok")
        web_owner.get(
            f"/oauth/tiktok/callback?error=access_denied&error_description=User+denied&state={state}",
            follow_redirects=False,
        )
    assert fake.exchange_calls == [] and db.get(OAuthState, state) is None
    set_lang(web_owner, "en")
    assert "Connecting TikTok was cancelled." in web_owner.get("/accounts").text


def test_callback_provider_error_is_shown_escaped(web_owner):
    fake = FakeProvider("tiktok", uses_pkce=False)
    with override("oauth", "tiktok", fake):
        _, state = _start(web_owner, "tiktok")
        web_owner.get(
            f"/oauth/tiktok/callback?error=server_error&error_description=%3Cb%3Eboom%3C%2Fb%3E&state={state}",
            follow_redirects=False,
        )
    set_lang(web_owner, "en")
    html = web_owner.get("/accounts").text
    assert "TikTok returned an error: &lt;b&gt;boom&lt;/b&gt;" in html


def test_callback_without_code(web_owner):
    fake = FakeProvider("tiktok", uses_pkce=False)
    with override("oauth", "tiktok", fake):
        _, state = _start(web_owner, "tiktok")
        web_owner.get(f"/oauth/tiktok/callback?state={state}", follow_redirects=False)
    assert fake.exchange_calls == []
    set_lang(web_owner, "en")
    assert "did not complete the sign-in" in web_owner.get("/accounts").text


def test_callback_platform_error_is_flashed(web_owner, db):
    fake = FakeProvider("meta", uses_pkce=False)
    fake.error = PermanentError("Facebook did not return any Page you can publish to.")
    with override("oauth", "meta", fake):
        _, state = _start(web_owner, "meta")
        resp = web_owner.get(f"/oauth/meta/callback?code=c&state={state}", follow_redirects=False)
    assert resp.headers["location"] == "/accounts"
    assert db.execute(select(ConnectedAccount)).first() is None
    set_lang(web_owner, "en")
    assert "Facebook did not return any Page you can publish to." in web_owner.get("/accounts").text


def test_callback_known_platform_failure_is_translated_with_the_platform_text_as_detail(web_owner, db):
    raw = "Facebook did not return any Page you can publish to. Select at least one Page."
    fake = FakeProvider("meta", uses_pkce=False)
    fake.error = PermanentError(raw, code="no_publishable_accounts")
    with override("oauth", "meta", fake):
        _, state = _start(web_owner, "meta")
        web_owner.get(f"/oauth/meta/callback?code=c&state={state}", follow_redirects=False)
    html = web_owner.get("/accounts").text  # Arabic by default
    assert "لم يُرجع فيسبوك أي صفحة يمكنك النشر عليها" in html and raw in html


def test_callback_unexpected_error_hides_details(web_owner, db):
    fake = FakeProvider("tiktok", uses_pkce=False)
    fake.error = RuntimeError("token=secret-value leaked")
    with override("oauth", "tiktok", fake):
        _, state = _start(web_owner, "tiktok")
        web_owner.get(f"/oauth/tiktok/callback?code=c&state={state}", follow_redirects=False)
    set_lang(web_owner, "en")
    html = web_owner.get("/accounts").text
    assert "unexpected error" in html and "secret-value" not in html


def test_callback_requires_login_and_keeps_query(web_client):
    resp = web_client.get("/oauth/tiktok/callback?code=c&state=s", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Foauth%2Ftiktok%2Fcallback%3Fcode%3Dc%26state%3Ds"


def test_expired_states_are_purged_on_connect(web_owner, db, workspace, admin_user):
    db.add(
        OAuthState(
            state="old-state",
            workspace_id=workspace.id,
            user_id=admin_user.id,
            provider="tiktok",
            purpose="tiktok",
            created_at=utcnow() - timedelta(hours=2),
        )
    )
    db.commit()
    with override("oauth", "tiktok", FakeProvider("tiktok", uses_pkce=False)):
        _start(web_owner, "tiktok")
    db.expire_all()
    assert db.get(OAuthState, "old-state") is None
