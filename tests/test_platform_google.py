"""Google OAuth provider: authorize URL, code exchange (YouTube + Drive), refresh, revoke."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from repeater.models import AccountStatus, Platform
from repeater.platforms import get_oauth_provider, get_provider_for_platform, google
from repeater.platforms.base import (
    AccountContext,
    AuthError,
    OAuthProvider,
    OAuthTokens,
    PermanentError,
    RateLimitedError,
    RetryableError,
)
from repeater.timeutil import utcnow

ACCESS = "ya29.access-SECRET"
REFRESH = "1//refresh-SECRET"
NEW_ACCESS = "ya29.new-access-SECRET"
REDIRECT = "https://rotator.test/oauth/google/callback"
VERIFIER = "v" * 64
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
YT_SCOPES = f"openid {google.YOUTUBE_UPLOAD} {google.YOUTUBE_READONLY}"
GOOGLE_SUB = "117000000000000000001"
DRIVE_SCOPES = f"openid https://www.googleapis.com/auth/userinfo.email {google.DRIVE_READONLY}"


@pytest.fixture
def mock() -> Any:
    with respx.mock(assert_all_called=False) as m:
        yield m


def make_id_token(
    sub: str = GOOGLE_SUB, *, aud: Any = "g-id", iss: str = "https://accounts.google.com"
) -> str:
    """An unsigned JWT shaped like Google's id_token (the adapter reads the claims only)."""

    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part({'iss': iss, 'aud': aud, 'sub': sub})}.signature"


def token_response(**overrides: Any) -> httpx.Response:
    body = {
        "access_token": ACCESS,
        "expires_in": 3599,
        "refresh_token": REFRESH,
        "scope": YT_SCOPES,
        "token_type": "Bearer",
        "id_token": make_id_token(),
    }
    body.update(overrides)
    return httpx.Response(200, json={k: v for k, v in body.items() if v is not None})


def oauth_error(status: int, error: str, description: str = "") -> httpx.Response:
    return httpx.Response(status, json={"error": error, "error_description": description})


def channel(
    channel_id: str = "UCabc", title: str = "Magic Lap", custom_url: str | None = "@magiclap"
) -> dict:
    snippet: dict[str, Any] = {
        "title": title,
        "thumbnails": {
            "default": {"url": f"https://yt3.ggpht.com/{channel_id}=s88"},
            "medium": {"url": f"https://yt3.ggpht.com/{channel_id}=s240"},
        },
    }
    if custom_url:
        snippet["customUrl"] = custom_url
    return {
        "id": channel_id,
        "snippet": snippet,
        "contentDetails": {"relatedPlaylists": {"uploads": "UU" + channel_id[2:]}},
    }


def ctx(tokens: OAuthTokens | None = None, platform: Platform = Platform.YOUTUBE) -> AccountContext:
    return AccountContext(
        account_id=3,
        platform=platform,
        external_id="UCabc",
        tokens=tokens
        or OAuthTokens(
            access_token=ACCESS,
            refresh_token=REFRESH,
            expires_at=utcnow() + timedelta(minutes=5),
            scopes=[google.YOUTUBE_UPLOAD, google.YOUTUBE_READONLY],
        ),
    )


def form(request: httpx.Request) -> dict[str, str]:
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def assert_no_secrets(exc: BaseException) -> None:
    text = f"{exc} {getattr(exc, 'message', '')}"
    for secret in (ACCESS, REFRESH, NEW_ACCESS, "g-secret"):
        assert secret not in text


# --------------------------------------------------------------------------- authorize URL


def test_provider_contract_and_registry():
    assert isinstance(google.oauth, OAuthProvider)
    assert google.oauth.name == "google" and google.oauth.uses_pkce is True
    assert get_oauth_provider("google") is google.oauth
    assert get_provider_for_platform("youtube") is google.oauth
    assert get_provider_for_platform("gdrive") is google.oauth


@pytest.mark.parametrize(
    ("purpose", "scopes"),
    [
        ("youtube", ["openid", google.YOUTUBE_UPLOAD, google.YOUTUBE_READONLY]),
        ("gdrive", ["openid", "email", google.DRIVE_READONLY]),
    ],
)
def test_authorize_url(purpose: str, scopes: list[str]):
    url = google.oauth.authorize_url(
        state="st4te", redirect_uri=REDIRECT, code_challenge="ch4llenge", purpose=purpose
    )
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q == {
        "client_id": "g-id",
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "state": "st4te",
        "code_challenge": "ch4llenge",
        "code_challenge_method": "S256",
    }
    assert "+" not in parts.query  # spaces encoded as %20
    assert "g-secret" not in url


def test_authorize_url_rejects_unknown_purpose_and_missing_pkce():
    with pytest.raises(ValueError):
        google.oauth.authorize_url(state="s", redirect_uri=REDIRECT, code_challenge="c", purpose="tiktok")
    with pytest.raises(ValueError):
        google.oauth.authorize_url(state="s", redirect_uri=REDIRECT, code_challenge=None, purpose="youtube")


def test_not_configured_is_a_clear_permanent_error(monkeypatch: pytest.MonkeyPatch):
    from repeater.config import get_settings

    monkeypatch.setattr(get_settings(), "google_client_secret", "")
    with pytest.raises(PermanentError) as ei:
        google.oauth.authorize_url(state="s", redirect_uri=REDIRECT, code_challenge="c", purpose="youtube")
    assert ei.value.code == "not_configured" and "REPEATER_GOOGLE_CLIENT_SECRET" in ei.value.message


# --------------------------------------------------------------------------- exchange: YouTube


def test_exchange_youtube_returns_one_account_per_channel(mock: Any):
    token = mock.post(google.TOKEN_URL).mock(return_value=token_response())
    channels = mock.get(CHANNELS_URL).mock(
        return_value=httpx.Response(200, json={"items": [channel(), channel("UCxyz", "Second", None)]})
    )

    accounts = google.oauth.exchange_code(
        code="4/code", redirect_uri=REDIRECT, code_verifier=VERIFIER, purpose="youtube"
    )

    sent = form(token.calls.last.request)
    assert sent == {
        "code": "4/code",
        "client_id": "g-id",
        "client_secret": "g-secret",
        "redirect_uri": REDIRECT,
        "grant_type": "authorization_code",
        "code_verifier": VERIFIER,
    }
    req = channels.calls.last.request
    assert req.url.params["part"] == "snippet,contentDetails" and req.url.params["mine"] == "true"
    assert req.headers["authorization"] == f"Bearer {ACCESS}"

    first, second = accounts
    assert first.platform == Platform.YOUTUBE and first.external_id == "UCabc"
    assert first.display_name == "Magic Lap"
    assert first.avatar_url == "https://yt3.ggpht.com/UCabc=s240"
    assert first.extra == {
        "channel_id": "UCabc",
        "uploads_playlist_id": "UUabc",
        "custom_url": "@magiclap",
        "google_sub": GOOGLE_SUB,
    }
    assert second.extra == {
        "channel_id": "UCxyz",
        "uploads_playlist_id": "UUxyz",
        "custom_url": None,
        "google_sub": GOOGLE_SUB,
    }
    tokens = first.tokens
    assert tokens.access_token == ACCESS and tokens.refresh_token == REFRESH
    assert tokens.scopes == ["openid", google.YOUTUBE_UPLOAD, google.YOUTUBE_READONLY]
    assert timedelta(minutes=58) < tokens.expires_at - utcnow() <= timedelta(seconds=3599)
    assert "id_token" not in tokens.extra
    assert first.tokens is not second.tokens and first.tokens.scopes is not second.tokens.scopes


def test_exchange_youtube_without_channel_is_permanent(mock: Any):
    mock.post(google.TOKEN_URL).mock(return_value=token_response())
    mock.get(CHANNELS_URL).mock(return_value=httpx.Response(200, json={"pageInfo": {"totalResults": 0}}))
    with pytest.raises(PermanentError) as ei:
        google.oauth.exchange_code(code="c", redirect_uri=REDIRECT, code_verifier=VERIFIER, purpose="youtube")
    assert "no YouTube channel" in ei.value.message and ei.value.code == "no_channel"


def test_exchange_youtube_signup_required_is_permanent(mock: Any):
    mock.post(google.TOKEN_URL).mock(return_value=token_response())
    mock.get(CHANNELS_URL).mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {
                    "code": 401,
                    "message": "Unauthorized",
                    "errors": [{"reason": "youtubeSignupRequired"}],
                }
            },
        )
    )
    with pytest.raises(PermanentError) as ei:
        google.oauth.exchange_code(code="c", redirect_uri=REDIRECT, code_verifier=VERIFIER, purpose="youtube")
    assert "no YouTube channel" in ei.value.message
    assert_no_secrets(ei.value)


def test_exchange_youtube_with_unticked_upload_scope_is_rejected(mock: Any):
    mock.post(google.TOKEN_URL).mock(return_value=token_response(scope=google.YOUTUBE_READONLY))
    channels = mock.get(CHANNELS_URL)
    with pytest.raises(PermanentError) as ei:
        google.oauth.exchange_code(code="c", redirect_uri=REDIRECT, code_verifier=VERIFIER, purpose="youtube")
    assert ei.value.code == "scope_not_granted" and "upload videos" in ei.value.message
    assert not channels.called


def test_exchange_without_scope_field_assumes_requested_scopes(mock: Any):
    mock.post(google.TOKEN_URL).mock(return_value=token_response(scope=None))
    mock.get(CHANNELS_URL).mock(return_value=httpx.Response(200, json={"items": [channel()]}))
    (account,) = google.oauth.exchange_code(
        code="c", redirect_uri=REDIRECT, code_verifier=VERIFIER, purpose="youtube"
    )
    assert account.tokens.scopes == ["openid", google.YOUTUBE_UPLOAD, google.YOUTUBE_READONLY]


def test_exchange_without_refresh_token_is_rejected(mock: Any):
    mock.post(google.TOKEN_URL).mock(return_value=token_response(refresh_token=None))
    with pytest.raises(PermanentError) as ei:
        google.oauth.exchange_code(code="c", redirect_uri=REDIRECT, code_verifier=VERIFIER, purpose="youtube")
    assert "myaccount.google.com/permissions" in ei.value.message


def test_exchange_invalid_grant_is_auth_error(mock: Any):
    mock.post(google.TOKEN_URL).mock(return_value=oauth_error(400, "invalid_grant", "Malformed auth code."))
    with pytest.raises(AuthError) as ei:
        google.oauth.exchange_code(code="c", redirect_uri=REDIRECT, code_verifier=VERIFIER, purpose="youtube")
    assert "Start the connection again" in ei.value.message and ei.value.code == "invalid_grant"


def test_exchange_omits_verifier_when_none(mock: Any):
    token = mock.post(google.TOKEN_URL).mock(return_value=token_response())
    mock.get(CHANNELS_URL).mock(return_value=httpx.Response(200, json={"items": [channel()]}))
    google.oauth.exchange_code(code="c", redirect_uri=REDIRECT, code_verifier=None, purpose="youtube")
    assert "code_verifier" not in form(token.calls.last.request)


# --------------------------------------------------------------------------- exchange: Drive


def test_exchange_gdrive_uses_userinfo(mock: Any):
    mock.post(google.TOKEN_URL).mock(return_value=token_response(scope=DRIVE_SCOPES))
    userinfo = mock.get(google.USERINFO_URL).mock(
        return_value=httpx.Response(
            200, json={"sub": "1098765", "email": "studio@magic-lap.com", "email_verified": True}
        )
    )

    (account,) = google.oauth.exchange_code(
        code="c", redirect_uri=REDIRECT, code_verifier=VERIFIER, purpose="gdrive"
    )

    assert userinfo.calls.last.request.headers["authorization"] == f"Bearer {ACCESS}"
    assert account.platform == Platform.GDRIVE
    assert account.external_id == "1098765"
    assert account.display_name == "studio@magic-lap.com"
    assert account.extra == {"email": "studio@magic-lap.com", "google_sub": "1098765"}
    assert account.avatar_url is None
    assert google.DRIVE_READONLY in account.tokens.scopes


def test_exchange_gdrive_without_drive_scope_is_rejected(mock: Any):
    mock.post(google.TOKEN_URL).mock(return_value=token_response(scope="openid email"))
    with pytest.raises(PermanentError) as ei:
        google.oauth.exchange_code(code="c", redirect_uri=REDIRECT, code_verifier=VERIFIER, purpose="gdrive")
    assert "Drive" in ei.value.message


@pytest.mark.parametrize(
    ("response", "exc_type"),
    [
        (httpx.Response(401, json={"error": "invalid_token"}), AuthError),
        (httpx.Response(503, text="unavailable"), RetryableError),
        (httpx.Response(200, json={"email": "x@y.z"}), PermanentError),
    ],
)
def test_exchange_gdrive_userinfo_failures(mock: Any, response: httpx.Response, exc_type: type):
    mock.post(google.TOKEN_URL).mock(return_value=token_response(scope=DRIVE_SCOPES))
    mock.get(google.USERINFO_URL).mock(return_value=response)
    with pytest.raises(exc_type) as ei:
        google.oauth.exchange_code(code="c", redirect_uri=REDIRECT, code_verifier=VERIFIER, purpose="gdrive")
    assert_no_secrets(ei.value)


# --------------------------------------------------------------------------- separate grants (api-tiktok-youtube-6)


@pytest.mark.parametrize("purpose", ["youtube", "gdrive"])
def test_authorize_url_never_merges_earlier_grants(purpose: str):
    url = google.oauth.authorize_url(state="s", redirect_uri=REDIRECT, code_challenge="c", purpose=purpose)
    assert "include_granted_scopes" not in parse_qs(urlsplit(url).query)
    assert "include_granted_scopes" not in url


def test_purposes_request_disjoint_resource_scopes():
    youtube_scopes, drive_scopes = set(google.PURPOSE_SCOPES["youtube"]), set(google.PURPOSE_SCOPES["gdrive"])
    assert google.DRIVE_READONLY not in youtube_scopes
    assert not {google.YOUTUBE_UPLOAD, google.YOUTUBE_READONLY} & drive_scopes
    assert "openid" in youtube_scopes  # the id_token carries the Google account id


def test_youtube_and_drive_accounts_of_one_google_user_share_the_google_sub(mock: Any):
    token = mock.post(google.TOKEN_URL)
    mock.get(CHANNELS_URL).mock(return_value=httpx.Response(200, json={"items": [channel()]}))
    mock.get(google.USERINFO_URL).mock(
        return_value=httpx.Response(200, json={"sub": GOOGLE_SUB, "email": "studio@magic-lap.com"})
    )
    token.mock(return_value=token_response())
    (yt,) = google.oauth.exchange_code(
        code="c", redirect_uri=REDIRECT, code_verifier=VERIFIER, purpose="youtube"
    )
    token.mock(return_value=token_response(scope=DRIVE_SCOPES, refresh_token="1//another-refresh"))
    (drive,) = google.oauth.exchange_code(
        code="c", redirect_uri=REDIRECT, code_verifier=VERIFIER, purpose="gdrive"
    )

    # Different refresh tokens (prompt=consent issues a new one each time) but one Google identity:
    # this id, not the token string, tells the dashboard that revoking one revokes both.
    assert yt.tokens.refresh_token != drive.tokens.refresh_token
    assert yt.extra["google_sub"] == drive.extra["google_sub"] == GOOGLE_SUB


def test_youtube_exchange_reads_the_sub_from_the_id_token_without_extra_requests(mock: Any):
    mock.post(google.TOKEN_URL).mock(return_value=token_response(id_token=make_id_token("42")))
    mock.get(CHANNELS_URL).mock(return_value=httpx.Response(200, json={"items": [channel()]}))
    userinfo = mock.get(google.USERINFO_URL)
    (account,) = google.oauth.exchange_code(
        code="c", redirect_uri=REDIRECT, code_verifier=VERIFIER, purpose="youtube"
    )
    assert account.extra["google_sub"] == "42" and not userinfo.called


@pytest.mark.parametrize(
    "id_token",
    [
        None,
        "not-a-jwt",
        "a.b.c",
        make_id_token(aud="someone-else"),
        make_id_token(iss="https://evil.example"),
    ],
)
def test_youtube_exchange_falls_back_to_userinfo_for_an_unusable_id_token(mock: Any, id_token: str | None):
    mock.post(google.TOKEN_URL).mock(return_value=token_response(id_token=id_token))
    mock.get(CHANNELS_URL).mock(return_value=httpx.Response(200, json={"items": [channel()]}))
    userinfo = mock.get(google.USERINFO_URL).mock(return_value=httpx.Response(200, json={"sub": "777"}))
    (account,) = google.oauth.exchange_code(
        code="c", redirect_uri=REDIRECT, code_verifier=VERIFIER, purpose="youtube"
    )
    assert account.extra["google_sub"] == "777"
    assert userinfo.calls.last.request.headers["authorization"] == f"Bearer {ACCESS}"


def test_youtube_exchange_accepts_an_audience_list(mock: Any):
    mock.post(google.TOKEN_URL).mock(
        return_value=token_response(id_token=make_id_token("8", aud=["other", "g-id"]))
    )
    mock.get(CHANNELS_URL).mock(return_value=httpx.Response(200, json={"items": [channel()]}))
    (account,) = google.oauth.exchange_code(
        code="c", redirect_uri=REDIRECT, code_verifier=VERIFIER, purpose="youtube"
    )
    assert account.extra["google_sub"] == "8"


@pytest.mark.parametrize(
    "userinfo",
    [
        httpx.Response(503, text="down"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"email": "x@y.z"}),
        httpx.ConnectError("offline"),
    ],
)
def test_youtube_exchange_connects_without_a_sub_when_nothing_provides_one(mock: Any, userinfo: Any):
    mock.post(google.TOKEN_URL).mock(return_value=token_response(id_token=None))
    mock.get(CHANNELS_URL).mock(return_value=httpx.Response(200, json={"items": [channel()]}))
    route = mock.get(google.USERINFO_URL)
    route.mock(side_effect=userinfo) if isinstance(userinfo, Exception) else route.mock(return_value=userinfo)

    (account,) = google.oauth.exchange_code(
        code="c", redirect_uri=REDIRECT, code_verifier=VERIFIER, purpose="youtube"
    )

    assert "google_sub" not in account.extra  # a legacy-style account: the dashboard stays cautious
    assert account.external_id == "UCabc"


# --------------------------------------------------------------------------- refresh


def test_refresh_keeps_existing_refresh_token_when_none_returned(mock: Any):
    token = mock.post(google.TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": NEW_ACCESS, "expires_in": 3599, "token_type": "Bearer"}
        )
    )
    old = ctx()
    old.tokens.refresh_expires_at = utcnow() + timedelta(days=100)

    tokens = google.oauth.refresh(old)

    assert form(token.calls.last.request) == {
        "client_id": "g-id",
        "client_secret": "g-secret",
        "grant_type": "refresh_token",
        "refresh_token": REFRESH,
    }
    assert tokens.access_token == NEW_ACCESS
    assert tokens.refresh_token == REFRESH
    assert tokens.refresh_expires_at == old.tokens.refresh_expires_at
    assert tokens.scopes == old.tokens.scopes
    assert tokens.expires_at - utcnow() > timedelta(minutes=55)


def test_refresh_uses_rotated_refresh_token_and_new_scopes(mock: Any):
    mock.post(google.TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": NEW_ACCESS,
                "expires_in": 3599,
                "refresh_token": "1//rotated",
                "refresh_token_expires_in": 604799,
                "scope": google.YOUTUBE_UPLOAD,
            },
        )
    )
    tokens = google.oauth.refresh(ctx())
    assert tokens.refresh_token == "1//rotated"
    assert timedelta(days=6) < tokens.refresh_expires_at - utcnow() <= timedelta(days=7)
    assert tokens.scopes == [google.YOUTUBE_UPLOAD]


def test_refresh_without_expires_in_still_expires(mock: Any):
    mock.post(google.TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": NEW_ACCESS}))
    tokens = google.oauth.refresh(ctx())
    assert tokens.expires_at is not None and tokens.expires_at - utcnow() <= timedelta(hours=1)


def test_refresh_invalid_grant_is_auth_error_mentioning_testing_mode(mock: Any):
    mock.post(google.TOKEN_URL).mock(
        return_value=oauth_error(400, "invalid_grant", "Token has been expired or revoked.")
    )
    with pytest.raises(AuthError) as ei:
        google.oauth.refresh(ctx())
    assert "Testing" in ei.value.message and "7 days" in ei.value.message
    assert ei.value.code == "invalid_grant" and ei.value.status == 400
    assert_no_secrets(ei.value)


def test_refresh_without_refresh_token_is_auth_error(mock: Any):
    route = mock.post(google.TOKEN_URL)
    with pytest.raises(AuthError):
        google.oauth.refresh(ctx(OAuthTokens(access_token=ACCESS)))
    assert not route.called


@pytest.mark.parametrize(
    ("response", "exc_type"),
    [
        (oauth_error(401, "invalid_client", "The OAuth client was not found."), PermanentError),
        (oauth_error(400, "unauthorized_client"), PermanentError),
        (httpx.Response(503, text="<html>oops</html>"), RetryableError),
        (httpx.Response(429, headers={"Retry-After": "12"}), RateLimitedError),
        (oauth_error(400, "invalid_request", "Missing parameter"), PermanentError),
        (httpx.Response(200, text="not json"), RetryableError),
        (httpx.Response(200, json={"expires_in": 3599}), RetryableError),
    ],
)
def test_refresh_other_failures(mock: Any, response: httpx.Response, exc_type: type):
    mock.post(google.TOKEN_URL).mock(return_value=response)
    with pytest.raises(exc_type) as ei:
        google.oauth.refresh(ctx())
    assert not isinstance(ei.value, AuthError)
    assert_no_secrets(ei.value)


def test_refresh_network_error_is_retryable(mock: Any):
    mock.post(google.TOKEN_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(RetryableError) as ei:
        google.oauth.refresh(ctx())
    assert_no_secrets(ei.value)


# --------------------------------------------------------------------------- revoke


def test_revoke_posts_refresh_token_in_body(mock: Any):
    route = mock.post(google.REVOKE_URL).mock(return_value=httpx.Response(200))
    google.oauth.revoke(ctx())
    request = route.calls.last.request
    assert form(request) == {"token": REFRESH}
    assert REFRESH not in str(request.url)


def test_revoke_falls_back_to_access_token(mock: Any):
    route = mock.post(google.REVOKE_URL).mock(return_value=httpx.Response(200))
    google.oauth.revoke(ctx(OAuthTokens(access_token=ACCESS)))
    assert form(route.calls.last.request) == {"token": ACCESS}


def test_revoke_already_invalid_token_is_fine(mock: Any):
    mock.post(google.REVOKE_URL).mock(
        return_value=oauth_error(400, "invalid_token", "Token expired or revoked")
    )
    google.oauth.revoke(ctx())


@pytest.mark.parametrize(
    ("response", "exc_type"),
    [(httpx.Response(503), RetryableError), (oauth_error(400, "invalid_request"), PermanentError)],
)
def test_revoke_failures_raise(mock: Any, response: httpx.Response, exc_type: type):
    mock.post(google.REVOKE_URL).mock(return_value=response)
    with pytest.raises(exc_type) as ei:
        google.oauth.revoke(ctx())
    assert_no_secrets(ei.value)


def test_revoke_network_error_is_retryable(mock: Any):
    mock.post(google.REVOKE_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(RetryableError):
        google.oauth.revoke(ctx())


def test_revoke_with_no_tokens_does_nothing(mock: Any):
    route = mock.post(google.REVOKE_URL)
    google.oauth.revoke(ctx(OAuthTokens(access_token="")))
    assert not route.called


# --------------------------------------------------------------------------- integration with accounts


def test_fresh_context_refreshes_expiring_youtube_account(db: Any, make_account: Any, mock: Any):
    from repeater.accounts import fresh_context, load_tokens

    acc = make_account("youtube", expires_in=timedelta(minutes=2), refresh_token=REFRESH, access_token=ACCESS)
    mock.post(google.TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": NEW_ACCESS, "expires_in": 3599, "scope": YT_SCOPES}
        )
    )

    context = fresh_context(db, acc)

    assert context.tokens.access_token == NEW_ACCESS
    stored = load_tokens(acc)
    assert stored.access_token == NEW_ACCESS and stored.refresh_token == REFRESH


def test_fresh_context_marks_reconnect_on_invalid_grant(db: Any, make_account: Any, mock: Any):
    from repeater.accounts import AccountUnavailable, fresh_context

    acc = make_account("gdrive", expires_in=timedelta(minutes=2), refresh_token=REFRESH, access_token=ACCESS)
    mock.post(google.TOKEN_URL).mock(return_value=oauth_error(400, "invalid_grant"))

    with pytest.raises(AccountUnavailable):
        fresh_context(db, acc)

    assert acc.status == AccountStatus.NEEDS_RECONNECT
    assert "7 days" in acc.last_error and REFRESH not in acc.last_error
