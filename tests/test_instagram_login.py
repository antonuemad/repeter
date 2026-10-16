"""Direct Instagram Login: synthetic tokens, accounts and mocked HTTP only."""

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from sqlalchemy import func, select

from repeater.accounts import (
    account_context,
    delete_account_records,
    fresh_context,
    load_tokens,
    save_tokens,
    upsert_connected_accounts,
)
from repeater.config import get_settings
from repeater.engine.jobs import delete_user_data
from repeater.models import (
    AccountStatus,
    ConnectedAccount,
    DataDeletionRequest,
    OAuthState,
    Platform,
    RotationRule,
    ScheduledPost,
    Video,
)
from repeater.platforms import get_provider_for_account, instagram
from repeater.platforms import instagram_login as ig
from repeater.platforms.base import AuthError, PlatformError, RetryableError
from repeater.timeutil import utcnow
from tests.test_content_plans import create_plan
from tests.test_platform_instagram import request as publish_request
from tests.test_web_meta import _payload, signed_request
from tests.test_web_support import set_lang

pytest_plugins = ["tests.test_web_support"]
GRAPH = ig.GRAPH_URL + "/v26.0"
SCOPED = "101234"
IG_ID = "17841401234"
SHORT = "synthetic-short-SECRET"
LONG = "synthetic-long-SECRET"
REDIRECT = "https://rotator.test/oauth/instagram/callback"


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def exchange(mock, *, scopes=None, wrapped=True, profile=None):
    grant = {
        "access_token": SHORT,
        "user_id": SCOPED,
        "permissions": ",".join(ig.SCOPES if scopes is None else scopes),
    }
    mock.post(ig.TOKEN_URL).respond(200, json={"data": [grant]} if wrapped else grant)
    mock.get(ig.GRAPH_URL + "/access_token").respond(200, json={"access_token": LONG, "expires_in": 5184000})
    mock.get(GRAPH + "/me").respond(
        200,
        json=profile
        or {
            "id": SCOPED,
            "user_id": IG_ID,
            "username": "synthetic.creator",
            "account_type": "BUSINESS",
        },
    )


def run_exchange():
    return ig.oauth.exchange_code(
        code="synthetic-code", redirect_uri=REDIRECT, code_verifier=None, purpose="instagram"
    )[0]


def test_authorize_scopes_and_instagram_credentials():
    url = urlparse(
        ig.oauth.authorize_url(
            state="csrf-state", redirect_uri=REDIRECT, code_challenge=None, purpose="instagram"
        )
    )
    assert url.netloc == "www.instagram.com"
    q = parse_qs(url.query)
    assert q["client_id"] == ["ig-id"] and q["state"] == ["csrf-state"]
    assert set(q["scope"][0].split(",")) == set(ig.SCOPES)
    assert q["redirect_uri"] == [REDIRECT] and q["enable_fb_login"] == ["false"]
    assert not ig.oauth.uses_pkce


@pytest.mark.parametrize("wrapped", [True, False])
def test_exchange_validates_grant_and_keeps_both_identities(mock, wrapped):
    exchange(mock, wrapped=wrapped)
    a = run_exchange()
    assert a.external_id == IG_ID and a.extra["ig_login_user_id"] == SCOPED
    assert a.extra["oauth_provider"] == "instagram" and "page_id" not in a.extra
    assert a.tokens.access_token == LONG and a.tokens.expires_at > utcnow() + timedelta(days=59)
    assert get_provider_for_account(a) is ig.oauth
    form = parse_qs(mock.calls[0].request.content.decode())
    assert form["client_secret"] == ["ig-secret"] and form["code"] == ["synthetic-code"]
    profile = mock.calls[-1].request
    assert profile.headers["authorization"] == f"Bearer {LONG}"
    assert "appsecret_proof" not in profile.url.params and LONG not in str(profile.url)


@pytest.mark.parametrize("bad", ["declined", "personal", "mismatch", "missing_id"])
def test_invalid_grant_is_not_saved(mock, bad):
    profile = {"id": SCOPED, "user_id": IG_ID, "account_type": "BUSINESS"}
    if bad == "personal":
        profile["account_type"] = "PERSONAL"
    if bad == "mismatch":
        profile["id"] = "99"
    if bad == "missing_id":
        profile.pop("user_id")
    exchange(mock, scopes=[] if bad == "declined" else None, profile=profile)
    with pytest.raises(AuthError):
        run_exchange()
    if bad == "declined":
        assert len(mock.calls) == 1


@pytest.mark.parametrize("status", [400, 429, 500])
def test_oauth_errors_never_echo_credentials(mock, status):
    mock.post(ig.TOKEN_URL).respond(
        status, json={"error": {"code": 190, "message": "ig-secret synthetic-code"}}
    )
    with pytest.raises(PlatformError) as err:
        run_exchange()
    assert "ig-secret" not in str(err.value) and "synthetic-code" not in str(err.value)


def test_network_error_is_sanitized(mock):
    mock.post(ig.TOKEN_URL).mock(side_effect=httpx.ConnectError("synthetic-code ig-secret"))
    with pytest.raises(RetryableError, match="ConnectError") as err:
        run_exchange()
    assert "ig-secret" not in str(err.value)


def test_refresh_routes_by_account_and_preserves_scopes(mock, db, workspace):
    exchange(mock)
    a = upsert_connected_accounts(db, workspace.id, [run_exchange()])[0]
    tokens = load_tokens(a)
    tokens.expires_at = utcnow() + timedelta(minutes=1)
    tokens.extra["issued_at"] = (utcnow() - timedelta(days=59)).isoformat()
    save_tokens(a, tokens)
    db.commit()
    mock.get(ig.GRAPH_URL + "/refresh_access_token").respond(
        200, json={"access_token": "new-synthetic", "expires_in": 5184000}
    )
    refreshed = fresh_context(db, a)
    assert refreshed.tokens.access_token == "new-synthetic"
    assert refreshed.tokens.scopes == list(ig.SCOPES)
    assert mock.calls[-1].request.url.params["grant_type"] == "ig_refresh_token"


def test_refresh_young_and_expired_tokens_do_not_call_api(mock):
    exchange(mock)
    a = run_exchange()
    from repeater.platforms.base import AccountContext

    ctx = AccountContext(1, a.platform, a.external_id, a.tokens, a.extra)
    calls = len(mock.calls)
    assert ig.oauth.refresh(ctx) is ctx.tokens
    ctx.tokens.expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(AuthError):
        ig.oauth.refresh(ctx)
    assert len(mock.calls) == calls


def test_import_uses_instagram_host_and_safe_cursor(mock, db, workspace):
    exchange(mock)
    a = upsert_connected_accounts(db, workspace.id, [run_exchange()])[0]
    route = mock.get(GRAPH + f"/{IG_ID}/media").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "99",
                            "media_type": "VIDEO",
                            "media_url": "https://cdn.example/a.mp4",
                            "timestamp": "2026-10-01T10:00:00+0000",
                        }
                    ],
                    "paging": {"next": "https://evil.example/?access_token=bad&after=cursor"},
                },
            ),
            httpx.Response(200, json={"data": []}),
        ]
    )
    result = instagram.lister.list_recent(account_context(a), limit=30)
    assert len(result) == 1 and result[0].external_id == "99"
    assert route.call_count == 2
    assert all(c.request.url.host == "graph.instagram.com" for c in route.calls)
    assert route.calls[-1].request.url.params["after"] == "cursor"
    assert "access_token" not in route.calls[-1].request.url.params


def test_publish_lock_blocks_direct_publish_and_poll_before_http(mock, db, workspace, monkeypatch):
    exchange(mock)
    a = upsert_connected_accounts(db, workspace.id, [run_exchange()])[0]
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    calls = len(mock.calls)
    for action in [
        lambda: instagram.publisher.publish(account_context(a), publish_request()),
        lambda: instagram.publisher.poll(account_context(a), {"container_id": "99"}),
    ]:
        with pytest.raises(PlatformError):
            action()
    assert len(mock.calls) == calls


def test_callback_keeps_wizard_context_encrypts_tokens_and_cannot_replay(
    web_owner, db, workspace, mock, monkeypatch
):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    plan = create_plan(web_owner, db)
    target = f"/plans/{plan.id}/setup/accounts"
    exchange(mock)
    start = web_owner.get("/connect/instagram", params={"return_to": target}, follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    assert db.get(OAuthState, state).provider == "instagram"
    callback = web_owner.get(
        "/oauth/instagram/callback", params={"state": state, "code": "synthetic-code"}, follow_redirects=False
    )
    assert callback.headers["location"] == target
    a = db.scalar(select(ConnectedAccount).where(ConnectedAccount.external_id == IG_ID))
    assert a and LONG not in a.token_enc and load_tokens(a).access_token == LONG
    assert not db.scalar(select(RotationRule)).enabled
    assert db.scalar(select(func.count()).select_from(ScheduledPost)) == 0
    calls = len(mock.calls)
    web_owner.get("/oauth/instagram/callback", params={"state": state, "code": "synthetic-code"})
    assert len(mock.calls) == calls
    set_lang(web_owner, "en")
    assert "Instagram directly" in web_owner.get(target).text
    a.status = AccountStatus.NEEDS_RECONNECT
    db.commit()
    assert 'href="/connect/instagram"' in web_owner.get("/accounts").text


def test_existing_login_cannot_be_overwritten(mock, db, workspace, make_account):
    a = make_account("instagram", external_id=IG_ID, extra={"fb_user_id": "old"})
    original = a.token_enc
    exchange(mock)
    with pytest.raises(AuthError, match="existing Accounts entry"):
        upsert_connected_accounts(db, workspace.id, [run_exchange()])
    assert a.token_enc == original and "oauth_provider" not in a.extra


@pytest.mark.parametrize("path", ["deauthorize", "data-deletion"])
def test_callbacks_require_instagram_secret_and_isolate_facebook(web_client, db, make_account, path):
    direct = make_account(
        "instagram",
        external_id=IG_ID,
        extra={"oauth_provider": "instagram", "ig_login_user_id": SCOPED, "fb_user_id": SCOPED},
    )
    legacy = make_account("facebook", extra={"fb_user_id": SCOPED})
    wrong = web_client.post(f"/instagram/{path}", data={"signed_request": signed_request(_payload(SCOPED))})
    assert wrong.status_code == 400
    response = web_client.post(
        f"/instagram/{path}", data={"signed_request": signed_request(_payload(SCOPED), "ig-secret")}
    )
    assert response.status_code == 200
    direct_id, legacy_id = direct.id, legacy.id
    if path == "data-deletion":
        record = db.scalar(select(DataDeletionRequest))
        assert record.provider == "instagram"
        assert delete_user_data(db, record.id)
        assert db.get(ConnectedAccount, direct_id) is None
    else:
        db.expire_all()
        assert db.get(ConnectedAccount, direct_id).status == AccountStatus.DISABLED
    db.expire_all()
    assert db.get(ConnectedAccount, legacy_id).status == AccountStatus.ACTIVE


def test_facebook_callback_does_not_disconnect_direct(web_client, db, make_account):
    direct = make_account(
        "instagram", extra={"oauth_provider": "instagram", "ig_login_user_id": SCOPED, "fb_user_id": SCOPED}
    )
    response = web_client.post("/meta/deauthorize", data={"signed_request": signed_request(_payload(SCOPED))})
    assert response.status_code == 200
    db.expire_all()
    assert direct.status == AccountStatus.ACTIVE


def test_deletion_survives_retention_without_cross_provider_collision(
    db, workspace, make_account, sample_video
):
    direct = make_account("instagram", extra={"oauth_provider": "instagram", "ig_login_user_id": SCOPED})
    from tests.test_worker import ready_video

    sample_video = ready_video(db, workspace, sample_video, source_account_id=direct.id)
    db.commit()
    video_id = sample_video.id
    delete_account_records(db, direct)
    db.commit()
    db.expire_all()
    assert db.get(Video, video_id).meta_deletion_user_id == f"instagram:{SCOPED}"
    wrong = DataDeletionRequest(provider="meta", external_user_id=SCOPED, confirmation_code="synthetic-wrong")
    db.add(wrong)
    db.commit()
    delete_user_data(db, wrong.id)
    assert db.get(Video, video_id) is not None
    right = DataDeletionRequest(
        provider="instagram", external_user_id=SCOPED, confirmation_code="synthetic-right"
    )
    db.add(right)
    db.commit()
    delete_user_data(db, right.id)
    assert db.get(Video, video_id) is None


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_public_review_homepage_needs_no_login(web_client, lang):
    response = web_client.get(f"/about?lang={lang}")
    assert response.status_code == 200
    assert 'href="/privacy"' in response.text and 'href="/terms"' in response.text
    assert "instagram_business" not in response.text
    assert 'name="robots" content="index, follow"' in response.text


@pytest.mark.parametrize(
    ("kind", "fmt"), [("video", "reel"), ("image", "post"), ("image", "story"), ("video", "story")]
)
def test_simulated_media_pipeline_uses_only_instagram_host(mock, db, workspace, kind, fmt):
    exchange(mock)
    a = upsert_connected_accounts(db, workspace.id, [run_exchange()])[0]
    mock.get(GRAPH + f"/{IG_ID}/content_publishing_limit").respond(200, json={"data": []})
    create = mock.post(GRAPH + f"/{IG_ID}/media").respond(200, json={"id": "555"})
    mock.get(GRAPH + "/555").respond(200, json={"status_code": "FINISHED"})
    publish = mock.post(GRAPH + f"/{IG_ID}/media_publish").respond(200, json={"id": "777"})
    mock.get(GRAPH + "/777").respond(
        200, json={"id": "777", "permalink": "https://www.instagram.com/p/synthetic/"}
    )
    req = publish_request(
        media_kind=kind,
        content_format=fmt,
        size_bytes=1024,
        height=1080 if (kind, fmt) == ("image", "post") else 1920,
        public_url="https://rotator.test/m/1/999/synthetic.jpg"
        if kind == "image"
        else "https://rotator.test/m/1/999/synthetic.mp4",
    )
    result = instagram.publisher.publish(account_context(a), req)
    assert result.state["container_id"] == "555"
    completed = instagram.publisher.poll(account_context(a), result.state)
    assert completed.platform_post_id == "777" and publish.call_count == 1
    fields = parse_qs(create.calls[0].request.content.decode())
    if fmt == "story":
        assert fields["media_type"] == ["STORIES"] and "caption" not in fields
    assert "appsecret_proof" not in fields
    for call in mock.calls:
        assert call.request.url.host in {"api.instagram.com", "graph.instagram.com"}


def test_declined_direct_permission_does_not_suggest_ads(mock, db, workspace):
    exchange(mock)
    a = upsert_connected_accounts(db, workspace.id, [run_exchange()])[0]
    mock.get(GRAPH + f"/{IG_ID}/content_publishing_limit").respond(
        403, json={"error": {"code": 200, "message": "Missing permission"}}
    )
    with pytest.raises(PlatformError) as err:
        instagram.publisher.publish(account_context(a), publish_request())
    assert "ads_read" not in str(err.value)


def test_facebook_pages_can_connect_after_direct_instagram_without_replacing_grant(mock, db, workspace):
    from tests.test_web_oauth import _meta_accounts

    exchange(mock)
    direct = upsert_connected_accounts(db, workspace.id, [run_exchange()])[0]
    db.commit()
    original = direct.token_enc
    facebook = _meta_accounts()
    facebook[1].external_id = IG_ID
    result = upsert_connected_accounts(db, workspace.id, facebook)
    db.commit()
    assert {a.platform for a in result} == {Platform.FACEBOOK, Platform.INSTAGRAM}
    assert direct.token_enc == original and direct.extra["oauth_provider"] == "instagram"
    assert "fb_user_id" not in direct.extra
    assert db.scalar(select(func.count()).select_from(ConnectedAccount)) == 2
