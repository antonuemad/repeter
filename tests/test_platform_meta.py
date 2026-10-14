"""Meta (Facebook Login) OAuth, Graph client, error mapping and signed_request parsing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from repeater.config import get_settings
from repeater.models import Platform
from repeater.platforms import get_oauth_provider, get_provider_for_platform, meta
from repeater.platforms.base import (
    AccountContext,
    AuthError,
    OAuthProvider,
    OAuthTokens,
    PermanentError,
    PlatformError,
    RateLimitedError,
    RetryableError,
)
from repeater.timeutil import utcnow

GRAPH = "https://graph.facebook.com/v26.0"
TOKEN_URL = f"{GRAPH}/oauth/access_token"
APP_SECRET = "m-secret"
REDIRECT = "https://rotator.test/oauth/meta/callback"
CODE = "auth-code-SECRET"
SHORT_TOKEN = "short-user-token-SECRET"
USER_TOKEN = "long-user-token-SECRET"
PAGE_A_TOKEN = "page-a-token-SECRET"
PAGE_B_TOKEN = "page-b-token-SECRET"
PAGE_C_TOKEN = "page-c-token-SECRET"
ALL_SECRETS = (APP_SECRET, CODE, SHORT_TOKEN, USER_TOKEN, PAGE_A_TOKEN, PAGE_B_TOKEN, PAGE_C_TOKEN)


@pytest.fixture
def mock() -> Any:
    with respx.mock(assert_all_called=False) as m:
        yield m


def proof(token: str) -> str:
    return hmac.new(APP_SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def query(request: httpx.Request) -> dict[str, str]:
    return dict(request.url.params)


def assert_signed(mock: Any, token_for_request: Any = None) -> int:
    """Every token-bearing Graph call has a Bearer header and the matching appsecret_proof, and no
    token or secret ever appears in a URL. Returns the number of signed calls."""
    signed = 0
    for call in mock.calls:
        request = call.request
        url = str(request.url)
        for secret in (USER_TOKEN, PAGE_A_TOKEN, PAGE_B_TOKEN, PAGE_C_TOKEN):
            assert secret not in url
        if request.url.host != "graph.facebook.com" or request.url.path.endswith("/oauth/access_token"):
            continue
        auth = request.headers.get("authorization", "")
        assert auth.startswith("Bearer "), url
        token = auth.removeprefix("Bearer ")
        params = query(request) if request.method == "GET" else form(request)
        assert params.get("appsecret_proof") == proof(token), url
        assert "access_token" not in params
        signed += 1
    return signed


def assert_no_secrets(exc: BaseException) -> None:
    text = f"{exc} {getattr(exc, 'message', '')}"
    for secret in ALL_SECRETS:
        assert secret not in text


def graph_error_response(
    status: int, code: int, subcode: int | None = None, message: str = "boom", **extra: Any
) -> httpx.Response:
    error: dict[str, Any] = {
        "message": message,
        "type": "OAuthException",
        "code": code,
        "fbtrace_id": "TRACE1",
    }
    if subcode is not None:
        error["error_subcode"] = subcode
    error.update(extra)
    return httpx.Response(status, json={"error": error})


def page(
    page_id: str, name: str, token: str, tasks: list[str], ig: dict[str, Any] | None = None
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": page_id,
        "name": name,
        "access_token": token,
        "tasks": tasks,
        "picture": {"data": {"url": f"https://scontent.test/{page_id}.jpg"}},
    }
    if ig:
        item["instagram_business_account"] = ig
    return item


IG_A = {
    "id": "17841400000000001",
    "username": "magiclap",
    "name": "Magic Lap",
    "profile_picture_url": "https://scontent.test/ig1.jpg",
}
IG_B = {"id": "17841400000000002", "username": "otherbrand", "name": "Other"}


def token_endpoint(request: httpx.Request) -> httpx.Response:
    params = query(request)
    assert params["client_id"] == "m-id"
    assert params["client_secret"] == APP_SECRET
    assert "authorization" not in request.headers
    if params.get("grant_type") == "fb_exchange_token":
        assert params["fb_exchange_token"] == SHORT_TOKEN
        return httpx.Response(
            200, json={"access_token": USER_TOKEN, "token_type": "bearer", "expires_in": 5183944}
        )
    assert params["code"] == CODE and params["redirect_uri"] == REDIRECT
    return httpx.Response(200, json={"access_token": SHORT_TOKEN, "token_type": "bearer", "expires_in": 3600})


def accounts_endpoint(request: httpx.Request) -> httpx.Response:
    params = query(request)
    assert params["fields"] == meta.PAGE_FIELDS
    if params.get("after") == "CURSOR1":
        return httpx.Response(
            200,
            json={
                "data": [page("222", "No Publish Page", PAGE_B_TOKEN, ["ANALYZE", "ADVERTISE"], IG_B)],
                "paging": {"cursors": {"before": "CURSOR1", "after": "CURSOR2"}},
            },
        )
    return httpx.Response(
        200,
        json={
            "data": [
                page("111", "Magic Lap Page", PAGE_A_TOKEN, ["MANAGE", "CREATE_CONTENT", "MODERATE"], IG_A)
            ],
            "paging": {
                "cursors": {"before": "CURSOR0", "after": "CURSOR1"},
                "next": f"{GRAPH}/me/accounts?access_token={USER_TOKEN}&after=CURSOR1",
            },
        },
    )


GRANTED = [
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "instagram_basic",
    "public_profile",
]


def mock_login(
    mock: Any, *, accounts: Any = accounts_endpoint, permissions: list[dict] | None = None
) -> None:
    mock.get(TOKEN_URL).mock(side_effect=token_endpoint)
    mock.get(f"{GRAPH}/me").respond(json={"id": "fbuser1", "name": "Admin"})
    perms = permissions or [{"permission": p, "status": "granted"} for p in GRANTED] + [
        {"permission": "instagram_content_publish", "status": "granted"},
        {"permission": "business_management", "status": "declined"},
    ]
    mock.get(f"{GRAPH}/me/permissions").respond(json={"data": perms})
    mock.get(f"{GRAPH}/me/accounts").mock(side_effect=accounts)


def ctx(tokens: OAuthTokens | None = None, platform: Platform = Platform.FACEBOOK) -> AccountContext:
    return AccountContext(
        account_id=1,
        platform=platform,
        external_id="111",
        tokens=tokens or OAuthTokens(access_token=PAGE_A_TOKEN),
        extra={"page_id": "111", "fb_user_id": "fbuser1"},
    )


def build_engine_post(db, workspace, make_account, sample_video, monkeypatch):
    """Factory for engine-level tests: make("facebook") -> (post, account). A real READY video with
    an encoded file, an enabled rule, one post due now, notifications swallowed. The Facebook and
    Instagram test modules wrap it in an `engine_post` fixture to run the real publish_post /
    poll_post against the real adapters with respx-mocked HTTP."""
    import shutil

    from repeater.engine import publisher, rules
    from repeater.media import storage
    from repeater.models import ScheduledPost, Video, VideoStatus

    monkeypatch.setattr(publisher.notify_mod, "notify", lambda text, **kw: True)

    def _make(platform: str):
        account = make_account(platform, display_name=f"Magic Lab {platform}")
        rule = rules.ensure_rule(db, account)
        rule.enabled = True
        video = Video(
            workspace_id=workspace.id,
            status=VideoStatus.READY,
            title="Coin trick",
            caption="Watch this coin vanish",
            duration_s=4.0,
            width=1080,
            height=1920,
            fps=30.0,
        )
        db.add(video)
        db.flush()
        folder = storage.ensure_video_dir(workspace.id, video.id)
        shutil.copy(sample_video, folder / storage.ENCODED_NAME)
        video.encoded_path = storage.rel_path(folder / storage.ENCODED_NAME)
        video.encoded_size_bytes = (folder / storage.ENCODED_NAME).stat().st_size
        post = ScheduledPost(
            workspace_id=workspace.id,
            video_id=video.id,
            connected_account_id=account.id,
            platform=Platform(platform),
            cycle=0,
            idempotency_key=f"{video.id}:{account.id}:0",
            scheduled_at=utcnow(),
            created_by="scheduler",
        )
        db.add(post)
        db.commit()
        return post, account

    return _make


# --------------------------------------------------------------------------- contract & authorize URL


def test_provider_contract_and_registry():
    assert isinstance(meta.oauth, OAuthProvider)
    assert meta.oauth.name == "meta" and meta.oauth.uses_pkce is False
    assert get_oauth_provider("meta") is meta.oauth
    assert get_provider_for_platform("instagram") is meta.oauth
    assert get_provider_for_platform("facebook") is meta.oauth


@pytest.mark.parametrize("purpose", ["meta", "facebook", "instagram"])
def test_authorize_url(purpose):
    url = meta.oauth.authorize_url(state="st-1", redirect_uri=REDIRECT, code_challenge=None, purpose=purpose)
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "www.facebook.com", "/v26.0/dialog/oauth")
    params = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert params["client_id"] == "m-id"
    assert params["redirect_uri"] == REDIRECT
    assert params["state"] == "st-1"
    assert params["response_type"] == "code"
    assert params["auth_type"] == "rerequest"
    assert params["scope"].split(",") == [
        "pages_show_list",
        "pages_read_engagement",
        "pages_manage_posts",
        "instagram_basic",
        "instagram_content_publish",
        "ads_read",
        "business_management",
    ]
    assert APP_SECRET not in url


def test_scopes_cover_business_portfolio_publishing():
    """api-meta-2: a user whose Page role comes from a business portfolio needs ads_read or
    ads_management on every Instagram publishing endpoint; the smaller one is requested."""
    assert "ads_read" in meta.SCOPES and "ads_management" not in meta.SCOPES
    assert set(meta.PLATFORM_PERMISSIONS[Platform.INSTAGRAM]) <= set(meta.SCOPES)
    assert set(meta.PLATFORM_PERMISSIONS[Platform.FACEBOOK]) <= set(meta.SCOPES)


def test_authorize_url_rejects_unknown_purpose():
    with pytest.raises(ValueError):
        meta.oauth.authorize_url(state="s", redirect_uri=REDIRECT, code_challenge=None, purpose="youtube")


def test_not_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "meta_app_secret", "")
    with pytest.raises(PermanentError) as exc:
        meta.oauth.authorize_url(state="s", redirect_uri=REDIRECT, code_challenge=None, purpose="meta")
    assert exc.value.code == "not_configured"
    with pytest.raises(PermanentError):
        meta.appsecret_proof("token")


def test_graph_version_setting(monkeypatch):
    assert meta.graph_version() == "v26.0"
    monkeypatch.setattr(get_settings(), "meta_graph_version", "v27.0")
    url = meta.oauth.authorize_url(state="s", redirect_uri=REDIRECT, code_challenge=None, purpose="meta")
    assert "/v27.0/dialog/oauth" in url
    monkeypatch.setattr(get_settings(), "meta_graph_version", "26")
    with pytest.raises(PermanentError):
        meta.graph_version()


# --------------------------------------------------------------------------- exchange_code


def test_exchange_code_pages_ig_and_pagination(mock):
    mock_login(mock)
    accounts = meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="meta")

    assert [(a.platform, a.external_id) for a in accounts] == [
        (Platform.FACEBOOK, "111"),
        (Platform.INSTAGRAM, IG_A["id"]),
    ]
    fb, ig = accounts
    assert fb.display_name == "Magic Lap Page"
    assert fb.avatar_url == "https://scontent.test/111.jpg"
    assert fb.extra == {"page_id": "111", "fb_user_id": "fbuser1", "can_manage": True}
    assert fb.tokens.access_token == PAGE_A_TOKEN and fb.tokens.expires_at is None
    assert fb.tokens.refresh_token is None
    assert "pages_manage_posts" in fb.tokens.scopes and "business_management" not in fb.tokens.scopes
    assert ig.display_name == "magiclap"
    assert ig.avatar_url == IG_A["profile_picture_url"]
    assert ig.extra == {
        "ig_user_id": IG_A["id"],
        "page_id": "111",
        "username": "magiclap",
        "fb_user_id": "fbuser1",
    }
    assert ig.tokens.access_token == PAGE_A_TOKEN and ig.tokens.expires_at is None
    assert ig.tokens is not fb.tokens

    account_calls = [c.request for c in mock.calls if c.request.url.path == "/v26.0/me/accounts"]
    assert len(account_calls) == 2
    assert "after" not in query(account_calls[0]) and query(account_calls[1])["after"] == "CURSOR1"
    assert query(account_calls[1])["fields"] == meta.PAGE_FIELDS
    me_call = next(c.request for c in mock.calls if c.request.url.path == "/v26.0/me")
    assert query(me_call)["fields"] == "id,name"
    # /me, /me/permissions and two /me/accounts pages, all with the long-lived user token.
    assert assert_signed(mock) == 4
    token_calls = [c.request for c in mock.calls if c.request.url.path == "/v26.0/oauth/access_token"]
    assert len(token_calls) == 2


@pytest.mark.parametrize(
    ("purpose", "expected"),
    [("facebook", [Platform.FACEBOOK]), ("instagram", [Platform.INSTAGRAM])],
)
def test_exchange_code_filters_by_purpose(mock, purpose, expected):
    mock_login(mock)
    accounts = meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose=purpose)
    assert [a.platform for a in accounts] == expected


def test_exchange_code_skips_pages_without_token_and_dedupes(mock):
    def accounts(request: httpx.Request) -> httpx.Response:
        no_token = page("333", "Live contributor", "", ["CREATE_CONTENT"])
        no_token.pop("access_token")
        return httpx.Response(
            200,
            json={
                "data": [
                    page("111", "Magic Lap Page", PAGE_A_TOKEN, ["CREATE_CONTENT"], IG_A),
                    page("111", "Magic Lap Page", PAGE_A_TOKEN, ["CREATE_CONTENT"], IG_A),
                    no_token,
                    page("444", "Plain Page", PAGE_C_TOKEN, ["CREATE_CONTENT"]),
                ]
            },
        )

    mock_login(mock, accounts=accounts)
    result = meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="meta")
    assert [(a.platform.value, a.external_id) for a in result] == [
        ("facebook", "111"),
        ("instagram", IG_A["id"]),
        ("facebook", "444"),
    ]
    assert result[2].avatar_url == "https://scontent.test/444.jpg"


def test_exchange_code_without_publishable_page(mock):
    def accounts(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [page("222", "Analyst", PAGE_B_TOKEN, ["ANALYZE"], IG_B)]})

    perms = [
        {"permission": "pages_show_list", "status": "granted"},
        {"permission": "pages_manage_posts", "status": "declined"},
    ]
    mock_login(mock, accounts=accounts, permissions=perms)
    with pytest.raises(PermanentError) as exc:
        meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="meta")
    assert exc.value.code == "no_publishable_accounts"
    assert "Page you can publish to" in exc.value.message
    assert "pages_manage_posts" in exc.value.message
    assert_no_secrets(exc.value)


def test_exchange_code_instagram_purpose_without_linked_account(mock):
    def accounts(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [page("444", "Plain Page", PAGE_C_TOKEN, ["CREATE_CONTENT"])]}
        )

    mock_login(mock, accounts=accounts)
    with pytest.raises(PermanentError) as exc:
        meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="instagram")
    assert "Instagram professional" in exc.value.message


def test_exchange_code_used_code(mock):
    mock.get(TOKEN_URL).mock(
        return_value=graph_error_response(400, 100, 36009, "This authorization code has been used.")
    )
    with pytest.raises(PermanentError) as exc:
        meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="meta")
    assert "authorization code has been used" in exc.value.message
    assert exc.value.code == "36009"
    assert_no_secrets(exc.value)


def test_exchange_code_echoed_secret_is_scrubbed(mock):
    mock.get(TOKEN_URL).mock(
        return_value=graph_error_response(400, 100, None, f"bad code {CODE} for {APP_SECRET}")
    )
    with pytest.raises(PermanentError) as exc:
        meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="meta")
    assert_no_secrets(exc.value)
    assert "***" in exc.value.message


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(502, text="<html>bad gateway</html>"), RetryableError),
        (httpx.Response(200, json={"token_type": "bearer"}), RetryableError),
        (httpx.Response(200, text="not json"), RetryableError),
        (
            graph_error_response(500, 2, None, "Service temporarily unavailable", is_transient=True),
            RetryableError,
        ),
    ],
)
def test_exchange_code_token_endpoint_failures(mock, response, error):
    mock.get(TOKEN_URL).mock(return_value=response)
    with pytest.raises(error) as exc:
        meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="meta")
    assert_no_secrets(exc.value)


def test_exchange_code_network_error(mock):
    mock.get(TOKEN_URL).mock(side_effect=httpx.ConnectTimeout("timeout"))
    with pytest.raises(RetryableError) as exc:
        meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="meta")
    assert "ConnectTimeout" in exc.value.message


def test_exchange_code_profile_auth_error(mock):
    mock.get(TOKEN_URL).mock(side_effect=token_endpoint)
    mock.get(f"{GRAPH}/me").mock(return_value=graph_error_response(400, 190, 460, "Session invalidated"))
    with pytest.raises(AuthError) as exc:
        meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="meta")
    assert "password changed" in exc.value.message
    assert_no_secrets(exc.value)


def test_exchange_code_missing_user_id(mock):
    mock.get(TOKEN_URL).mock(side_effect=token_endpoint)
    mock.get(f"{GRAPH}/me").respond(json={"name": "No id"})
    with pytest.raises(PermanentError):
        meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="meta")


def permissions(**status: str) -> list[dict[str, str]]:
    """Granted permissions of mock_login's default login with `name=status` overrides."""
    names = {p: "granted" for p in (*GRANTED, "instagram_content_publish")}
    names.update(status)
    return [{"permission": name, "status": value} for name, value in names.items()]


def test_exchange_code_declined_instagram_publish_is_not_connected(mock, caplog):
    """api-meta-3: an account that can never publish must not be added to the rotation."""
    mock_login(mock, permissions=permissions(instagram_content_publish="declined"))
    with caplog.at_level("WARNING", logger=meta.log.name):
        accounts = meta.oauth.exchange_code(
            code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="meta"
        )
    assert [(a.platform, a.external_id) for a in accounts] == [(Platform.FACEBOOK, "111")]
    assert "instagram_content_publish" in caplog.text and PAGE_A_TOKEN not in caplog.text


def test_exchange_code_declined_instagram_publish_names_the_permission(mock):
    mock_login(mock, permissions=permissions(instagram_content_publish="declined"))
    with pytest.raises(PermanentError) as exc:
        meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="instagram")
    assert exc.value.code == "permissions_declined"
    assert "instagram_content_publish" in exc.value.message and "Instagram" in exc.value.message
    assert_no_secrets(exc.value)


def test_exchange_code_declined_pages_manage_posts_is_not_connected(mock):
    mock_login(mock, permissions=permissions(pages_manage_posts="declined"))
    with pytest.raises(PermanentError) as exc:
        meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="facebook")
    assert exc.value.code == "permissions_declined" and "pages_manage_posts" in exc.value.message
    # The Instagram account of the same login does not need pages_manage_posts.
    mock_login(mock, permissions=permissions(pages_manage_posts="declined"))
    accounts = meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="meta")
    assert [a.platform for a in accounts] == [Platform.INSTAGRAM]


def test_exchange_code_everything_declined_lists_both_platforms(mock):
    mock_login(
        mock,
        permissions=permissions(pages_manage_posts="declined", instagram_content_publish="declined"),
    )
    with pytest.raises(PermanentError) as exc:
        meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="meta")
    assert exc.value.code == "permissions_declined"
    assert "pages_manage_posts" in exc.value.message and "instagram_content_publish" in exc.value.message


def test_exchange_code_ads_read_is_not_required(mock, caplog):
    """ads_read only matters for Page roles granted through a business portfolio, which cannot be
    told at connect time: connecting without it works, and publishing explains the failure."""
    mock_login(mock, permissions=permissions(ads_read="declined"))
    with caplog.at_level("INFO", logger=meta.log.name):
        accounts = meta.oauth.exchange_code(
            code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="meta"
        )
    assert {a.platform for a in accounts} == {Platform.FACEBOOK, Platform.INSTAGRAM}
    assert "ads_read" in caplog.text


def test_exchange_code_records_whether_the_user_can_manage_the_page(mock):
    def accounts(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [page("555", "Contributor Page", PAGE_C_TOKEN, ["CREATE_CONTENT"])]}
        )

    mock_login(mock, accounts=accounts)
    (fb,) = meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="facebook")
    assert fb.extra["can_manage"] is False and fb.extra["page_id"] == "555"


# --------------------------------------------------------------------------- refresh & revoke


def test_refresh_returns_tokens_unchanged():
    tokens = OAuthTokens(access_token=PAGE_A_TOKEN, scopes=["pages_manage_posts"], extra={"k": "v"})
    fresh = meta.oauth.refresh(ctx(tokens))
    assert fresh == tokens and fresh is not tokens
    assert fresh.scopes is not tokens.scopes


def test_refresh_with_future_expiry_is_kept():
    expires = utcnow() + timedelta(minutes=10)
    fresh = meta.oauth.refresh(ctx(OAuthTokens(access_token=PAGE_A_TOKEN, expires_at=expires)))
    assert fresh.expires_at == expires


def test_refresh_expired_token_needs_reconnect():
    tokens = OAuthTokens(access_token=PAGE_A_TOKEN, expires_at=utcnow() - timedelta(seconds=1))
    with pytest.raises(AuthError) as exc:
        meta.oauth.refresh(ctx(tokens))
    assert exc.value.code == "token_expired"
    assert_no_secrets(exc.value)


def test_revoke_is_a_no_op(mock):
    meta.oauth.revoke(ctx())
    assert mock.calls.call_count == 0


# --------------------------------------------------------------------------- signed_request


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def signed(payload: dict[str, Any] | bytes, secret: str = APP_SECRET) -> str:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    encoded = b64url(raw)
    sig = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{b64url(sig)}.{encoded}"


PAYLOAD = {"algorithm": "HMAC-SHA256", "expires": 1291840400, "issued_at": 1291836800, "user_id": "218471"}


def test_parse_signed_request_valid():
    assert meta.parse_signed_request(signed(PAYLOAD)) == PAYLOAD
    lower = {**PAYLOAD, "algorithm": "hmac-sha256"}
    assert meta.parse_signed_request(signed(lower))["user_id"] == "218471"


def test_parse_signed_request_with_padding_and_whitespace():
    sig, payload = signed(PAYLOAD).split(".")
    padded = payload + "=" * (-len(payload) % 4)
    sig_bytes = hmac.new(APP_SECRET.encode(), padded.encode(), hashlib.sha256).digest()
    value = f"  {b64url(sig_bytes)}.{padded}\n"
    assert meta.parse_signed_request(value)["user_id"] == "218471"


def tampered() -> str:
    sig, _ = signed(PAYLOAD).split(".")
    return f"{sig}.{b64url(json.dumps({**PAYLOAD, 'user_id': '999'}).encode())}"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(lambda: signed(PAYLOAD, secret="other-secret"), id="wrong-secret"),
        pytest.param(tampered, id="tampered-payload"),
        pytest.param(lambda: signed({**PAYLOAD, "algorithm": "HMAC-SHA1"}), id="wrong-algorithm"),
        pytest.param(
            lambda: signed({k: v for k, v in PAYLOAD.items() if k != "algorithm"}), id="no-algorithm"
        ),
        pytest.param(lambda: signed(b"not json"), id="not-json"),
        pytest.param(lambda: signed(b"[1, 2]"), id="not-object"),
        pytest.param(lambda: signed(b"\xff\xfe"), id="not-utf8"),
        pytest.param(lambda: "", id="empty"),
        pytest.param(lambda: "onlyonepart", id="one-part"),
        pytest.param(lambda: signed(PAYLOAD) + ".extra", id="three-parts"),
        pytest.param(lambda: "!!!." + signed(PAYLOAD).split(".")[1], id="bad-base64"),
        pytest.param(lambda: "." + signed(PAYLOAD).split(".")[1], id="empty-signature"),
        pytest.param(lambda: "a" * 10000 + "." + "b", id="too-long"),
    ],
)
def test_parse_signed_request_rejects(value):
    with pytest.raises(ValueError):
        meta.parse_signed_request(value())


def test_parse_signed_request_requires_secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "meta_app_secret", "")
    with pytest.raises(ValueError):
        meta.parse_signed_request(signed(PAYLOAD))


def test_parse_signed_request_rejects_non_string():
    with pytest.raises(ValueError):
        meta.parse_signed_request(None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- error mapping


def classify(status: int, error: dict[str, Any], headers: dict[str, str] | None = None) -> PlatformError:
    resp = httpx.Response(status, json={"error": error}, headers=headers or {})
    with pytest.raises(PlatformError) as exc:
        meta.parse_response(resp, "testing", (PAGE_A_TOKEN,))
    return exc.value


def err(code: int, subcode: int | None = None, **extra: Any) -> dict[str, Any]:
    error: dict[str, Any] = {
        "message": "Meta says no",
        "type": "OAuthException",
        "code": code,
        "fbtrace_id": "T",
    }
    if subcode is not None:
        error["error_subcode"] = subcode
    error.update(extra)
    return error


@pytest.mark.parametrize(
    ("status", "error", "cls", "code"),
    [
        (400, err(190), AuthError, "190"),
        (400, err(190, 463), AuthError, "463"),
        (400, err(102), AuthError, "102"),
        (400, err(100, 460), AuthError, "460"),
        (400, err(100, 492), AuthError, "492"),
        (403, err(10), meta.PermissionDeniedError, "10"),
        (403, err(200), meta.PermissionDeniedError, "200"),
        (403, err(299), meta.PermissionDeniedError, "299"),
        (400, err(100, 1363042), meta.PermissionDeniedError, "1363042"),
        (400, err(4), RateLimitedError, "4"),
        (400, err(17), RateLimitedError, "17"),
        (400, err(32), RateLimitedError, "32"),
        (400, err(613), RateLimitedError, "613"),
        (400, err(80001), RateLimitedError, "80001"),
        (400, err(80002), RateLimitedError, "80002"),
        (400, err(9, 2207042), RateLimitedError, "2207042"),
        (400, err(1), RetryableError, "1"),
        (500, err(2), RetryableError, "2"),
        (400, err(100, is_transient=True), RetryableError, "100"),
        (400, err(368), RetryableError, "368"),
        (400, err(-1, 2207001), RetryableError, "2207001"),
        (400, err(-2, 2207003), RetryableError, "2207003"),
        (400, err(-2, 2207020), RetryableError, "2207020"),
        (400, err(24, 2207008), RetryableError, "2207008"),
        (400, err(-1, 2207053), RetryableError, "2207053"),
        (400, err(9004, 2207052), RetryableError, "2207052"),
        (400, err(9007, 2207027), RetryableError, "2207027"),
        (400, err(100, 6000), RetryableError, "6000"),
        (400, err(6000), RetryableError, "6000"),
        (400, err(6000, 1363019), RetryableError, "1363019"),
        (400, err(6000, 1363999), RetryableError, "1363999"),
        (400, err(6000, 1363024), PermanentError, "1363024"),
        (400, err(6000, 1363128), PermanentError, "1363128"),
        (500, err(6000), RetryableError, "6000"),
        (400, err(100, 1363030), RetryableError, "1363030"),
        (500, err(12345), RetryableError, "12345"),
        (400, err(352, 2207026), PermanentError, "2207026"),
        (400, err(36000, 2207004), PermanentError, "2207004"),
        (400, err(36004, 2207010), PermanentError, "2207010"),
        (400, err(4, 2207051), PermanentError, "2207051"),
        (400, err(25, 2207050), PermanentError, "2207050"),
        (400, err(100, 1363040), PermanentError, "1363040"),
        (400, err(100, 1363128), PermanentError, "1363128"),
        (400, err(506), PermanentError, "506"),
        (400, err(100), PermanentError, "100"),
        (400, err(100, 460, type="GraphMethodException"), PermanentError, "460"),
        (401, err(99999), AuthError, "99999"),
    ],
)
def test_graph_error_mapping(status, error, cls, code):
    exc = classify(status, error)
    assert type(exc) is cls
    assert exc.code == code
    assert exc.status == status
    assert "Meta error" in exc.message and "trace T" in exc.message


def test_rate_limit_retry_after_defaults_and_headers():
    assert classify(400, err(4)).retry_after == meta.DEFAULT_RATE_LIMIT_RETRY
    assert classify(400, err(613)).retry_after == meta.CUSTOM_RATE_LIMIT_RETRY
    assert classify(400, err(9, 2207042)).retry_after == meta.IG_PUBLISH_LIMIT_RETRY
    buc = json.dumps(
        {
            "111": [{"type": "pages", "call_count": 100, "estimated_time_to_regain_access": 19}],
            "222": [{"type": "pages", "estimated_time_to_regain_access": 3}],
        }
    )
    assert classify(400, err(80001), {"x-business-use-case-usage": buc}).retry_after == 19 * 60
    assert classify(400, err(32), {"retry-after": "42"}).retry_after == 42
    assert classify(400, err(32), {"x-business-use-case-usage": "garbage"}).retry_after == 15 * 60


def test_graph_error_prefers_user_message_and_scrubs_tokens():
    exc = classify(
        400,
        err(100, message=f"token {PAGE_A_TOKEN} invalid", error_user_msg=f"Friendly text {PAGE_A_TOKEN}"),
    )
    assert "Friendly text" in exc.message
    assert PAGE_A_TOKEN not in exc.message and PAGE_A_TOKEN not in str(exc)


def test_auth_subcode_text_only_for_auth_errors():
    exc = classify(400, err(100, 460, type="GraphMethodException", message="Unsupported get request"))
    assert type(exc) is PermanentError
    assert "password" not in exc.message and "Unsupported get request" in exc.message


def test_known_explanation_replaces_meta_text():
    exc = classify(400, err(352, 2207026, message="raw"))
    assert "does not support this video format" in exc.message


def test_permission_errors_are_auth_errors_with_their_own_class():
    exc = classify(403, err(200))
    assert isinstance(exc, AuthError) and type(exc) is meta.PermissionDeniedError
    assert type(classify(400, err(190))) is AuthError  # a bad token is not a missing permission


PERMISSION_FRAGMENT = "permission is missing or was removed"


@pytest.mark.parametrize("permission", ["ads_management", "ads_read"])
def test_ads_permission_failure_names_ads_read(permission):
    """api-meta-2: the documented 403/200 error for a business-portfolio Page role."""
    exc = classify(403, err(200, message=f"(#200) Requires {permission} permission to manage the object"))
    assert type(exc) is meta.PermissionDeniedError
    assert "ads_read" in exc.message and "Business portfolio" in exc.message
    assert "Reconnect" in exc.message
    assert PERMISSION_FRAGMENT not in exc.message


def test_other_permission_failures_keep_the_generic_text():
    exc = classify(403, err(10, message="(#10) Application does not have permission for this action"))
    assert PERMISSION_FRAGMENT in exc.message and "ads_read" not in exc.message


def test_code_6000_without_subcode_is_retryable_with_backoff():
    exc = classify(400, err(6000, message="There was a problem uploading your video file."))
    assert type(exc) is RetryableError and exc.retry_after == meta.TRANSIENT_RETRY
    assert "6000" in exc.message and "try again" in exc.message


@pytest.mark.parametrize(
    ("response", "cls"),
    [
        (httpx.Response(502, text="<html>"), RetryableError),
        (httpx.Response(401, text="nope"), AuthError),
        (httpx.Response(429, text="slow down", headers={"retry-after": "7"}), RateLimitedError),
        (httpx.Response(404, text="missing"), PermanentError),
        (httpx.Response(200, text="not json"), RetryableError),
        (httpx.Response(200, json=[1, 2]), RetryableError),
        (httpx.Response(200, json={"error": {"code": 190, "message": "x"}}), AuthError),
    ],
)
def test_parse_response_without_error_object(response, cls):
    with pytest.raises(PlatformError) as exc:
        meta.parse_response(response, "testing")
    assert type(exc.value) is cls


def test_parse_response_success():
    assert meta.parse_response(httpx.Response(200, json={"id": "1"}), "testing") == {"id": "1"}


# --------------------------------------------------------------------------- Graph client


def test_graph_get_and_post_are_signed(mock):
    route_get = mock.get(f"{GRAPH}/123").respond(json={"id": "123"})
    route_post = mock.post(f"{GRAPH}/123/things").respond(json={"id": "9"})
    with meta.Graph(PAGE_A_TOKEN) as graph:
        assert graph.get("123", {"fields": "id", "skip": None}, action="reading")["id"] == "123"
        assert graph.post("123/things", {"a": "b", "none": None}, action="writing")["id"] == "9"
    get_req = route_get.calls.last.request
    assert query(get_req) == {"fields": "id", "appsecret_proof": proof(PAGE_A_TOKEN)}
    post_req = route_post.calls.last.request
    assert form(post_req) == {"a": "b", "appsecret_proof": proof(PAGE_A_TOKEN)}
    assert "appsecret_proof" not in query(post_req)
    assert assert_signed(mock) == 2


def test_graph_network_error_is_retryable(mock):
    mock.get(f"{GRAPH}/123").mock(side_effect=httpx.ReadTimeout("slow"))
    with meta.Graph(PAGE_A_TOKEN) as graph, pytest.raises(RetryableError) as exc:
        graph.get("123", action="reading")
    assert "ReadTimeout" in exc.value.message


def test_paginate_follows_time_based_next_without_credentials(mock):
    pages = [
        {
            "data": [{"id": "1"}, {"id": "2"}],
            "paging": {
                "next": f"{GRAPH}/9/media?access_token={PAGE_A_TOKEN}&appsecret_proof=x&until=100&limit=2"
            },
        },
        {"data": [{"id": "3"}], "paging": {"previous": "p"}},
    ]
    route = mock.get(f"{GRAPH}/9/media").mock(side_effect=[httpx.Response(200, json=p) for p in pages])
    with meta.Graph(PAGE_A_TOKEN) as graph:
        items = list(graph.paginate("9/media", {"fields": "id", "limit": 2}, action="listing"))
    assert [i["id"] for i in items] == ["1", "2", "3"]
    second = query(route.calls[1].request)
    assert second["until"] == "100" and second["fields"] == "id"
    assert second["appsecret_proof"] == proof(PAGE_A_TOKEN)
    assert assert_signed(mock) == 2


def test_paginate_stops_on_repeated_params_and_max_pages(mock):
    same = {"data": [{"id": "1"}], "paging": {"cursors": {"after": "A"}, "next": "x"}}
    route = mock.get(f"{GRAPH}/9/media").respond(json=same)
    with meta.Graph(PAGE_A_TOKEN) as graph:
        assert len(list(graph.paginate("9/media", action="listing"))) == 2
    assert route.call_count == 2
    counter = {"n": 0}

    def endless(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(
            200,
            json={
                "data": [{"id": str(counter["n"])}],
                "paging": {"cursors": {"after": f"C{counter['n']}"}, "next": "x"},
            },
        )

    mock.get(f"{GRAPH}/8/media").mock(side_effect=endless)
    with meta.Graph(PAGE_A_TOKEN) as graph:
        assert len(list(graph.paginate("8/media", action="listing", max_pages=3))) == 3


def test_paginate_ignores_non_dict_items(mock):
    mock.get(f"{GRAPH}/9/media").respond(json={"data": [{"id": "1"}, "junk", None]})
    with meta.Graph(PAGE_A_TOKEN) as graph:
        assert list(graph.paginate("9/media", action="listing")) == [{"id": "1"}]


# --------------------------------------------------------------------------- small helpers


def test_node_escapes_and_requires_id():
    assert meta.node("123") == "123"
    assert meta.node("../me") == "..%2Fme"
    with pytest.raises(PermanentError):
        meta.node("")
    with pytest.raises(PermanentError):
        meta.node(None)


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("/reel/123/", "https://www.facebook.com/reel/123/"),
        ("magiclap/videos/1/", "https://www.facebook.com/magiclap/videos/1/"),
        ("https://www.facebook.com/reel/9", "https://www.facebook.com/reel/9"),
        ("", None),
        (None, None),
        (5, None),
    ],
)
def test_absolute_facebook_url(link, expected):
    assert meta.absolute_facebook_url(link) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2019-09-25T17:18:30+0000", datetime(2019, 9, 25, 17, 18, 30, tzinfo=UTC)),
        ("2019-09-25T17:18:30", datetime(2019, 9, 25, 17, 18, 30, tzinfo=UTC)),
        (1676057525, datetime.fromtimestamp(1676057525, UTC)),
        ("1676057525", datetime.fromtimestamp(1676057525, UTC)),
        ("garbage", None),
        (None, None),
        (True, None),
        ([], None),
    ],
)
def test_parse_graph_time(value, expected):
    assert meta.parse_graph_time(value) == expected


def test_flag():
    assert meta.flag(True) and meta.flag("true") and meta.flag("1")
    assert not meta.flag(False) and not meta.flag("no") and not meta.flag(None)
