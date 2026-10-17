"""Tests for the TikTok adapter (OAuth, creator_info, chunked publishing, polling, listing)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from repeater.models import Platform
from repeater.platforms import base, get_lister, get_oauth_provider, get_publisher
from repeater.platforms import tiktok as tt
from repeater.platforms.base import (
    AccountContext,
    AuthError,
    NotSupportedError,
    OAuthTokens,
    PermanentError,
    PublishOutcome,
    PublishRequest,
    RateLimitedError,
    RemoteVideo,
    RestartPublish,
    RetryableError,
    UnknownOutcomeError,
)
from repeater.timeutil import utcnow

MIB = 1024 * 1024
GIB = 1024 * MIB
ACCESS = "act.SECRET-access-token"
REFRESH = "rft.SECRET-refresh-token"
UPLOAD_URL = "https://open-upload.tiktokapis.com/video/?upload_id=67890&upload_token=SECRET-upload"
PUBLIC_URL = "https://rotator.test/m/1/2/sig.mp4"


# --------------------------------------------------------------------------- fixtures & helpers


@pytest.fixture
def router():
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as r:
        yield r


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(tt, "_sleep", sleeps.append)
    return sleeps


@pytest.fixture(autouse=True)
def _fresh_pull_cache():
    """url_ownership_unverified is remembered per process: start every test without it."""
    tt._pull_cache.unverified_until = 0.0
    yield
    tt._pull_cache.unverified_until = 0.0


def ctx(
    *,
    access: str = ACCESS,
    refresh: str | None = REFRESH,
    expires_in: timedelta = timedelta(hours=12),
    refresh_expires_at: datetime | None = None,
    username: str | None = "magiclap",
) -> AccountContext:
    extra = {"open_id": "open-123"}
    if username:
        extra["username"] = username
    return AccountContext(
        account_id=7,
        platform=Platform.TIKTOK,
        external_id="open-123",
        tokens=OAuthTokens(
            access_token=access,
            refresh_token=refresh,
            expires_at=utcnow() + expires_in,
            refresh_expires_at=refresh_expires_at or utcnow() + timedelta(days=300),
            scopes=list(tt.SCOPES + tt.PUBLISHING_SCOPES),
            extra={"open_id": "open-123"},
        ),
        extra=extra,
        display_name="Magic Lap",
    )


def ok(data: dict | None = None, status: int = 200, **kw) -> httpx.Response:
    return httpx.Response(
        status, json={"data": data or {}, "error": {"code": "ok", "message": "", "log_id": "L1"}}, **kw
    )


def api_error(code: str, status: int, message: str = "boom", **kw) -> httpx.Response:
    return httpx.Response(
        status, json={"data": {}, "error": {"code": code, "message": message, "log_id": "LOG42"}}, **kw
    )


def creator_payload(**over) -> dict:
    data = {
        "creator_avatar_url": "https://p16.tiktokcdn.com/avatar.jpg",
        "creator_username": "magiclap",
        "creator_nickname": "Magic Lap",
        "privacy_level_options": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
        "comment_disabled": False,
        "duet_disabled": False,
        "stitch_disabled": True,
        "max_video_post_duration_sec": 300,
    }
    data.update(over)
    return data


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def body(request: httpx.Request) -> dict:
    return json.loads(request.content)


def direct_settings(**over) -> dict:
    s = dict(base.RULE_SETTINGS_DEFAULTS["tiktok"])
    s.update(privacy_level="PUBLIC_TO_EVERYONE", music_consent_at="2026-10-14T10:00:00+00:00")
    s.update(over)
    return s


def make_request(
    path: Path,
    settings: dict,
    *,
    caption: str = "Hello #magic",
    duration: float = 4.0,
    public_url: str = "",
    state: dict | None = None,
):
    """A request without a public_url, i.e. the FILE_UPLOAD path; pass PUBLIC_URL to let TikTok pull."""
    return PublishRequest(
        post_id=99,
        video_path=path,
        public_url=public_url,
        size_bytes=path.stat().st_size if path.exists() else 1000,
        duration_s=duration,
        width=1080,
        height=1920,
        title="Hello",
        caption=caption,
        settings=settings,
        state=state or {},
    )


def write_file(tmp_path: Path, size: int, name: str = "video.mp4") -> Path:
    p = tmp_path / name
    p.write_bytes(os.urandom(size))
    return p


def mock_publish_flow(router, *, creator: dict | None = None, upload_statuses=(201,)):
    routes = {
        "creator": router.post(tt.CREATOR_INFO_URL).mock(return_value=ok(creator or creator_payload())),
        "init": router.post(tt.DIRECT_INIT_URL).mock(
            return_value=ok({"publish_id": "v_pub_file~v2-1.1", "upload_url": UPLOAD_URL})
        ),
        "inbox": router.post(tt.INBOX_INIT_URL).mock(
            return_value=ok({"publish_id": "v_inbox_file~v2-1.2", "upload_url": UPLOAD_URL})
        ),
        "upload": router.put(url__startswith="https://open-upload.tiktokapis.com/video/").mock(
            side_effect=[httpx.Response(s) for s in upload_statuses]
        ),
    }
    return routes


# --------------------------------------------------------------------------- registry & contracts


def test_module_objects_satisfy_protocols():
    assert get_oauth_provider("tiktok") is tt.oauth
    assert get_publisher("tiktok") is tt.publisher
    assert get_lister("tiktok") is tt.lister
    assert isinstance(tt.oauth, base.OAuthProvider)
    assert isinstance(tt.publisher, base.Publisher)
    assert isinstance(tt.lister, base.MediaLister)
    assert tt.oauth.name == "tiktok"
    assert tt.oauth.uses_pkce is False
    assert tt.publisher.platform == Platform.TIKTOK
    assert tt.publisher.limits == base.LIMITS[Platform.TIKTOK]


# --------------------------------------------------------------------------- OAuth


@pytest.mark.parametrize("publishing_enabled", [False, True])
def test_authorize_url_has_only_read_scopes_regardless_of_global_lock(monkeypatch, publishing_enabled):
    from repeater.config import get_settings

    monkeypatch.setattr(get_settings(), "publishing_enabled", publishing_enabled)
    url = tt.oauth.authorize_url(
        state="st-1",
        redirect_uri="https://rotator.test/oauth/tiktok/callback",
        code_challenge=None,
        purpose="tiktok",
    )
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://www.tiktok.com/v2/auth/authorize/"
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q == {
        "client_key": "tt-key",
        "response_type": "code",
        "scope": "user.info.basic,video.list",
        "redirect_uri": "https://rotator.test/oauth/tiktok/callback",
        "state": "st-1",
    }
    assert "tt-secret" not in url


def test_authorize_url_rejects_other_purposes():
    with pytest.raises(ValueError):
        tt.oauth.authorize_url(state="s", redirect_uri="https://x/cb", code_challenge=None, purpose="youtube")


def test_authorize_url_requires_configuration(monkeypatch):
    from repeater.config import get_settings

    monkeypatch.setattr(get_settings(), "tiktok_client_key", "")
    with pytest.raises(PermanentError, match="REPEATER_TIKTOK_CLIENT_KEY"):
        tt.oauth.authorize_url(state="s", redirect_uri="https://x/cb", code_challenge=None, purpose="tiktok")


TOKEN_RESPONSE = {
    "access_token": "act.NEW-access",
    "expires_in": 86400,
    "open_id": "open-123",
    "refresh_expires_in": 31536000,
    "refresh_token": "rft.NEW-refresh",
    "scope": "user.info.basic,video.list,video.publish,video.upload",
    "token_type": "Bearer",
}


def test_exchange_code_reads_profile_without_creator_info_even_with_legacy_scopes(router):
    token = router.post(tt.TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    user = router.get(tt.USER_INFO_URL).mock(
        return_value=ok(
            {
                "user": {
                    "open_id": "open-123",
                    "union_id": "union-9",
                    "avatar_url": "https://p16.tiktokcdn.com/a.jpg",
                    "display_name": "Magic Lap",
                }
            }
        )
    )
    creator = router.post(tt.CREATOR_INFO_URL).mock(return_value=ok(creator_payload()))

    before = utcnow()
    [acc] = tt.oauth.exchange_code(
        code="auth-code",
        redirect_uri="https://rotator.test/oauth/tiktok/callback",
        code_verifier=None,
        purpose="tiktok",
    )

    sent = form(token.calls.last.request)
    assert sent == {
        "client_key": "tt-key",
        "client_secret": "tt-secret",
        "code": "auth-code",
        "grant_type": "authorization_code",
        "redirect_uri": "https://rotator.test/oauth/tiktok/callback",
    }
    assert token.calls.last.request.headers["content-type"] == "application/x-www-form-urlencoded"
    user_req = user.calls.last.request
    assert user_req.url.params["fields"] == "open_id,union_id,avatar_url,display_name"
    assert user_req.headers["authorization"] == "Bearer act.NEW-access"
    assert not creator.called

    assert acc.platform == Platform.TIKTOK
    assert acc.external_id == "open-123"
    assert acc.display_name == "Magic Lap"
    assert acc.avatar_url == "https://p16.tiktokcdn.com/a.jpg"
    assert acc.extra == {"open_id": "open-123"}
    t = acc.tokens
    assert t.access_token == "act.NEW-access" and t.refresh_token == "rft.NEW-refresh"
    assert timedelta(hours=23, minutes=59) < t.expires_at - before <= timedelta(hours=24, seconds=5)
    assert timedelta(days=364) < t.refresh_expires_at - before <= timedelta(days=365, seconds=5)
    assert t.scopes == ["user.info.basic", "video.list", "video.publish", "video.upload"]
    assert t.extra == {"open_id": "open-123"}
    # survives the encrypted round trip used by accounts.save_tokens
    assert OAuthTokens.from_dict(t.to_dict()) == t


def test_exchange_without_publish_scope_skips_creator_info(router):
    payload = dict(TOKEN_RESPONSE, scope="user.info.basic,video.list")
    router.post(tt.TOKEN_URL).mock(return_value=httpx.Response(200, json=payload))
    router.get(tt.USER_INFO_URL).mock(
        return_value=ok({"user": {"open_id": "open-123", "display_name": "ML"}})
    )
    creator = router.post(tt.CREATOR_INFO_URL).mock(return_value=ok(creator_payload()))

    [acc] = tt.oauth.exchange_code(
        code="c", redirect_uri="https://x/cb", code_verifier=None, purpose="tiktok"
    )

    assert not creator.called
    assert acc.extra == {"open_id": "open-123"}
    assert acc.tokens.scopes == ["user.info.basic", "video.list"]


def test_exchange_still_connects_when_username_lookup_fails(router):
    router.post(tt.TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    router.get(tt.USER_INFO_URL).mock(
        return_value=ok({"user": {"open_id": "open-123", "display_name": "ML"}})
    )
    router.post(tt.CREATOR_INFO_URL).mock(return_value=httpx.Response(503, text="unavailable"))

    [acc] = tt.oauth.exchange_code(
        code="c", redirect_uri="https://x/cb", code_verifier=None, purpose="tiktok"
    )
    assert acc.extra == {"open_id": "open-123"}
    assert acc.display_name == "ML"


def test_exchange_falls_back_to_token_open_id(router):
    router.post(tt.TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    router.get(tt.USER_INFO_URL).mock(return_value=ok({"user": {"display_name": "ML"}}))
    router.post(tt.CREATOR_INFO_URL).mock(return_value=ok(creator_payload()))
    [acc] = tt.oauth.exchange_code(
        code="c", redirect_uri="https://x/cb", code_verifier=None, purpose="tiktok"
    )
    assert acc.external_id == "open-123"


def test_exchange_invalid_code_is_auth_error_without_secrets(router):
    router.post(tt.TOKEN_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "Authorization code auth-code-XYZ is expired.",
                "log_id": "L9",
            },
        )
    )
    with pytest.raises(AuthError) as ei:
        tt.oauth.exchange_code(
            code="auth-code-XYZ", redirect_uri="https://x/cb", code_verifier=None, purpose="tiktok"
        )
    assert ei.value.code == "invalid_grant"
    assert "auth-code-XYZ" not in str(ei.value)
    assert "tt-secret" not in str(ei.value)


def test_exchange_error_in_200_body_is_detected(router):
    router.post(tt.TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"error": "invalid_request", "error_description": "bad"})
    )
    with pytest.raises(AuthError):
        tt.oauth.exchange_code(code="c", redirect_uri="https://x/cb", code_verifier=None, purpose="tiktok")


@pytest.mark.parametrize(
    ("response", "exc_type"),
    [
        (httpx.Response(502, text="bad gateway"), RetryableError),
        (httpx.Response(429, json={"error": "rate_limit_exceeded"}), RateLimitedError),
        (
            httpx.Response(401, json={"error": "invalid_client", "error_description": "bad key"}),
            PermanentError,
        ),
    ],
)
def test_exchange_token_endpoint_failures(router, response, exc_type):
    router.post(tt.TOKEN_URL).mock(return_value=response)
    with pytest.raises(exc_type):
        tt.oauth.exchange_code(code="c", redirect_uri="https://x/cb", code_verifier=None, purpose="tiktok")


def test_exchange_network_error_is_retryable(router):
    router.post(tt.TOKEN_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(RetryableError):
        tt.oauth.exchange_code(code="c", redirect_uri="https://x/cb", code_verifier=None, purpose="tiktok")


def test_exchange_user_info_rejected(router):
    router.post(tt.TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    router.get(tt.USER_INFO_URL).mock(return_value=api_error("access_token_invalid", 401))
    with pytest.raises(AuthError):
        tt.oauth.exchange_code(code="c", redirect_uri="https://x/cb", code_verifier=None, purpose="tiktok")


def test_refresh_persists_rotated_refresh_token(router):
    route = router.post(tt.TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "act.ROTATED",
                "expires_in": 86400,
                "open_id": "open-123",
                "refresh_token": "rft.ROTATED",
                "refresh_expires_in": 1000,
                "scope": "user.info.basic,video.publish",
                "token_type": "Bearer",
            },
        )
    )
    before = utcnow()
    tokens = tt.oauth.refresh(ctx())
    assert form(route.calls.last.request) == {
        "client_key": "tt-key",
        "client_secret": "tt-secret",
        "grant_type": "refresh_token",
        "refresh_token": REFRESH,
    }
    assert tokens.access_token == "act.ROTATED"
    assert tokens.refresh_token == "rft.ROTATED"
    assert timedelta(seconds=990) < tokens.refresh_expires_at - before <= timedelta(seconds=1005)
    assert tokens.scopes == ["user.info.basic", "video.publish"]
    assert tokens.extra["open_id"] == "open-123"


def test_refresh_keeps_old_refresh_token_when_not_returned(router):
    router.post(tt.TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "act.N", "expires_in": 86400})
    )
    account = ctx()
    tokens = tt.oauth.refresh(account)
    assert tokens.refresh_token == REFRESH
    assert tokens.refresh_expires_at == account.tokens.refresh_expires_at
    assert tokens.scopes == list(tt.SCOPES + tt.PUBLISHING_SCOPES)


@pytest.mark.parametrize(
    ("response", "exc_type"),
    [
        (
            httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "refresh token revoked"}
            ),
            AuthError,
        ),
        (httpx.Response(401, json={"error": "invalid_client"}), PermanentError),
        (httpx.Response(500, text="oops"), RetryableError),
    ],
)
def test_refresh_errors(router, response, exc_type):
    router.post(tt.TOKEN_URL).mock(return_value=response)
    with pytest.raises(exc_type) as ei:
        tt.oauth.refresh(ctx())
    assert REFRESH not in str(ei.value) and "tt-secret" not in str(ei.value)


def test_refresh_error_echoing_token_is_scrubbed(router):
    router.post(tt.TOKEN_URL).mock(
        return_value=httpx.Response(
            400, json={"error": "invalid_grant", "error_description": f"bad {REFRESH}"}
        )
    )
    with pytest.raises(AuthError) as ei:
        tt.oauth.refresh(ctx())
    assert REFRESH not in str(ei.value) and REFRESH not in ei.value.message


def test_refresh_without_usable_refresh_token_needs_reconnect(router):
    with pytest.raises(AuthError):
        tt.oauth.refresh(ctx(refresh=None))
    with pytest.raises(AuthError, match="365 days"):
        tt.oauth.refresh(ctx(refresh_expires_at=utcnow() - timedelta(minutes=1)))
    assert not router.calls


def test_fresh_context_refreshes_and_persists_rotation(db, make_account, router):
    from repeater.accounts import fresh_context, load_tokens

    router.post(tt.TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "act.FRESH",
                "expires_in": 86400,
                "refresh_token": "rft.FRESH",
                "refresh_expires_in": 31536000,
                "open_id": "open-1",
                "scope": "video.publish",
            },
        )
    )
    acc = make_account("tiktok", external_id="open-1", expires_in=timedelta(minutes=5))
    context = fresh_context(db, acc)
    assert context.tokens.access_token == "act.FRESH"
    stored = load_tokens(acc)
    assert stored.refresh_token == "rft.FRESH"
    assert stored.refresh_expires_at is not None


def test_revoke_posts_access_token(router):
    route = router.post(tt.REVOKE_URL).mock(return_value=httpx.Response(200, content=b""))
    tt.oauth.revoke(ctx())
    assert form(route.calls.last.request) == {
        "client_key": "tt-key",
        "client_secret": "tt-secret",
        "token": ACCESS,
    }


def test_revoke_error_raises_without_token(router):
    router.post(tt.REVOKE_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_request", "error_description": "bad"})
    )
    with pytest.raises(AuthError) as ei:
        tt.oauth.revoke(ctx())
    assert ACCESS not in str(ei.value)


def test_revoke_refreshes_expired_access_token_first(router):
    router.post(tt.TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "act.FOR-REVOKE", "expires_in": 86400})
    )
    revoke = router.post(tt.REVOKE_URL).mock(return_value=httpx.Response(200))
    tt.oauth.revoke(ctx(expires_in=timedelta(hours=-1)))
    assert form(revoke.calls.last.request)["token"] == "act.FOR-REVOKE"


def test_revoke_skips_when_grant_already_gone(router):
    router.post(tt.TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    revoke = router.post(tt.REVOKE_URL).mock(return_value=httpx.Response(200))
    tt.oauth.revoke(ctx(expires_in=timedelta(hours=-1)))
    assert not revoke.called


# --------------------------------------------------------------------------- creator_info


def test_creator_info_parsing(router):
    route = router.post(tt.CREATOR_INFO_URL).mock(return_value=ok(creator_payload()))
    info = tt.creator_info(ctx())
    req = route.calls.last.request
    assert req.headers["authorization"] == f"Bearer {ACCESS}"
    assert req.headers["content-type"] == "application/json; charset=UTF-8"
    assert info == base.TikTokCreatorInfo(
        nickname="Magic Lap",
        avatar_url="https://p16.tiktokcdn.com/avatar.jpg",
        privacy_level_options=["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
        comment_disabled=False,
        duet_disabled=False,
        stitch_disabled=True,
        max_video_post_duration_sec=300,
        username="magiclap",
    )


def test_creator_info_defaults_for_missing_fields(router):
    router.post(tt.CREATOR_INFO_URL).mock(return_value=ok({"creator_nickname": "X"}))
    info = tt.creator_info(ctx())
    assert info.privacy_level_options == []
    assert info.username is None and info.avatar_url is None
    assert info.max_video_post_duration_sec == 600
    assert not (info.comment_disabled or info.duet_disabled or info.stitch_disabled)


def test_creator_info_spam_cap_returned_with_http_200(router):
    router.post(tt.CREATOR_INFO_URL).mock(return_value=api_error("spam_risk_too_many_posts", 200))
    with pytest.raises(RateLimitedError) as ei:
        tt.creator_info(ctx())
    assert ei.value.retry_after >= 6 * 3600
    assert ei.value.code == "spam_risk_too_many_posts"
    assert "LOG42" in str(ei.value)


# --------------------------------------------------------------------------- chunk math


def check_plan(plan: tt.ChunkPlan) -> list[tuple[int, int]]:
    ranges = list(plan.ranges())
    assert len(ranges) == plan.total_chunk_count
    assert 1 <= plan.total_chunk_count <= tt.MAX_CHUNKS
    assert ranges[0][0] == 0 and ranges[-1][1] == plan.video_size - 1
    for (_, a_last), (b_first, _) in zip(ranges, ranges[1:], strict=False):
        assert b_first == a_last + 1
    sizes = [last - first + 1 for first, last in ranges]
    assert sum(sizes) == plan.video_size
    if plan.total_chunk_count == 1:
        assert plan.video_size <= tt.MAX_CHUNK_BYTES  # over 64 MB must be chunked
        assert plan.chunk_size <= plan.video_size
    else:
        assert plan.total_chunk_count == plan.video_size // plan.chunk_size
        assert tt.MIN_CHUNK_BYTES <= plan.chunk_size <= tt.MAX_CHUNK_BYTES
        assert all(s == plan.chunk_size for s in sizes[:-1])
        assert plan.chunk_size <= sizes[-1] <= tt.MAX_FINAL_CHUNK_BYTES
    return ranges


def test_chunks_1mb_uploads_whole():
    plan = tt.plan_chunks(1 * MIB)
    assert plan == tt.ChunkPlan(MIB, MIB, 1)
    assert check_plan(plan) == [(0, MIB - 1)]


def test_chunks_exactly_5mb_uploads_whole():
    plan = tt.plan_chunks(5 * MIB)
    assert plan == tt.ChunkPlan(5 * MIB, 5 * MIB, 1)
    check_plan(plan)


def test_chunks_just_under_and_over_5mb():
    assert tt.plan_chunks(5 * MIB - 1).total_chunk_count == 1
    plan = tt.plan_chunks(5 * MIB + 1, 5 * MIB)
    assert plan == tt.ChunkPlan(5 * MIB + 1, 5 * MIB, 1)
    check_plan(plan)


def test_chunks_64mb():
    plan = tt.plan_chunks(64 * MIB)
    assert plan == tt.ChunkPlan(64 * MIB, 16 * MIB, 4)
    check_plan(plan)


def test_chunks_130mb_last_absorbs_remainder():
    plan = tt.plan_chunks(130 * MIB)
    assert plan.chunk_size == 16 * MIB and plan.total_chunk_count == 8
    ranges = check_plan(plan)
    assert ranges[-1] == (7 * 16 * MIB, 130 * MIB - 1)  # 18 MiB final chunk


def test_chunks_with_largest_allowed_chunk_size():
    # 130 MiB with 64 MB chunks: 2 chunks, final one carries the remainder (< 128 MB).
    plan = tt.plan_chunks(130 * MIB, tt.MAX_CHUNK_BYTES)
    assert plan.total_chunk_count == 2
    check_plan(plan)
    # 100 MB would floor to one 100 MB chunk, but files over 64 MB must be split.
    plan = tt.plan_chunks(100_000_000, tt.MAX_CHUNK_BYTES)
    assert plan == tt.ChunkPlan(100_000_000, 50_000_000, 2)
    check_plan(plan)


def test_chunks_4gb_boundary():
    plan = tt.plan_chunks(4 * GIB)
    assert plan == tt.ChunkPlan(4 * GIB, 16 * MIB, 256)
    check_plan(plan)
    plan = tt.plan_chunks(4 * GIB, 5 * MIB)
    assert plan.total_chunk_count == 819  # stays under the 1000-chunk ceiling
    check_plan(plan)
    with pytest.raises(PermanentError):
        tt.plan_chunks(4 * GIB + 1)


def test_chunks_match_media_transfer_guide_example():
    plan = tt.plan_chunks(50_000_123, 10_000_000)
    assert plan == tt.ChunkPlan(50_000_123, 10_000_000, 5)
    assert check_plan(plan) == [
        (0, 9_999_999),
        (10_000_000, 19_999_999),
        (20_000_000, 29_999_999),
        (30_000_000, 39_999_999),
        (40_000_000, 50_000_122),
    ]


@pytest.mark.parametrize(
    "size",
    [
        1,
        5 * MIB - 1,
        16 * MIB,
        16 * MIB + 1,
        32 * MIB - 1,
        32 * MIB,
        64_000_001,
        128 * MIB + 7,
        999 * MIB,
        3 * GIB + 12345,
        4 * GIB - 1,
    ],
)
@pytest.mark.parametrize("preferred", [5 * MIB, 10_000_000, 16 * MIB, 64_000_000])
def test_chunk_plan_invariants(size, preferred):
    check_plan(tt.plan_chunks(size, preferred))


def test_chunk_plan_rejects_bad_input():
    with pytest.raises(PermanentError):
        tt.plan_chunks(0)
    with pytest.raises(ValueError):
        tt.plan_chunks(10 * MIB, 4 * MIB)
    with pytest.raises(ValueError):
        tt.plan_chunks(10 * MIB, 65 * MIB)


def test_truncate_utf16_counts_code_units_and_keeps_surrogate_pairs():
    assert tt.truncate_utf16("abc", 5) == "abc"
    assert tt.truncate_utf16("abcdef", 3) == "abc"
    text = "a" + "😀" * 3  # 1 + 3*2 units
    assert tt.truncate_utf16(text, 4) == "a😀"
    assert tt.truncate_utf16(text, 6) == "a😀😀"
    assert tt.truncate_utf16(text, 7) == text
    arabic = "س" * 2300
    assert len(tt.truncate_utf16(arabic, 2200)) == 2200


# --------------------------------------------------------------------------- validate


def test_validate_ok_for_direct_and_inbox(sample_video):
    req = make_request(sample_video, direct_settings())
    assert tt.publisher.validate(ctx(), req) == []
    assert tt.publisher.validate(ctx(), make_request(sample_video, {"mode": "inbox"})) == []


@pytest.mark.parametrize(
    ("settings", "duration", "needle"),
    [
        (direct_settings(privacy_level=None), 4.0, "privacy level"),
        (direct_settings(privacy_level="EVERYONE"), 4.0, "Unknown TikTok privacy level"),
        (direct_settings(music_consent_at=None), 4.0, "Music Usage Confirmation"),
        (direct_settings(privacy_level="SELF_ONLY", brand_content_toggle=True), 4.0, "branded content"),
        (direct_settings(mode="draft"), 4.0, "posting mode"),
        (direct_settings(), 601.0, "at most 600"),
        (direct_settings(), 0.5, "shorter"),
    ],
)
def test_validate_problems(sample_video, settings, duration, needle):
    problems = tt.publisher.validate(ctx(), make_request(sample_video, settings, duration=duration))
    assert any(needle.lower() in p.lower() for p in problems), problems


def test_validate_default_settings_refuse_without_privacy(sample_video):
    problems = tt.publisher.validate(ctx(), make_request(sample_video, {}))
    assert len(problems) == 2  # privacy level + music consent


def test_validate_size_limits(tmp_path):
    req = make_request(tmp_path / "missing.mp4", direct_settings())
    req.size_bytes = 4 * GIB + 1
    assert any("4 GB" in p for p in tt.publisher.validate(ctx(), req))
    req.size_bytes = 0
    assert any("empty" in p for p in tt.publisher.validate(ctx(), req))


def test_publish_refuses_invalid_settings_without_network(router, sample_video):
    with pytest.raises(PermanentError, match="privacy level"):
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings(privacy_level=None)))
    assert not router.calls


# --------------------------------------------------------------------------- publish


def test_direct_publish_single_chunk(router, sample_video):
    routes = mock_publish_flow(router)
    req = make_request(sample_video, direct_settings(allow_comment=True, is_aigc=True))
    result = tt.publisher.publish(ctx(), req)

    size = sample_video.stat().st_size
    init_req = routes["init"].calls.last.request
    assert init_req.headers["authorization"] == f"Bearer {ACCESS}"
    assert init_req.headers["content-type"] == "application/json; charset=UTF-8"
    assert body(init_req) == {
        "post_info": {
            "title": "Hello #magic",
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "disable_comment": False,
            "disable_duet": True,
            "disable_stitch": True,
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
            "is_aigc": True,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": size,
            "total_chunk_count": 1,
        },
    }
    put = routes["upload"].calls.last.request
    assert put.method == "PUT"
    assert str(put.url) == UPLOAD_URL
    assert put.headers["content-type"] == "video/mp4"
    assert put.headers["content-range"] == f"bytes 0-{size - 1}/{size}"
    assert put.headers["content-length"] == str(size)
    assert "authorization" not in put.headers
    assert put.content == sample_video.read_bytes()

    assert result.outcome == PublishOutcome.PROCESSING
    assert result.state == {
        "publish_id": "v_pub_file~v2-1.1",
        "mode": "direct",
        "username": "magiclap",
        "source": "FILE_UPLOAD",
        "init_at": result.state["init_at"],
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "video_size": size,
    }
    assert abs(datetime.fromisoformat(result.state["init_at"]) - utcnow()) < timedelta(seconds=30)
    assert result.poll_after_seconds == 30
    assert routes["creator"].call_count == 1


def test_direct_publish_multi_chunk_put_headers(router, tmp_path):
    size = 12 * MIB + 123
    video = write_file(tmp_path, size)
    routes = mock_publish_flow(router, upload_statuses=(206, 201))
    publisher = tt.TikTokPublisher(chunk_bytes=5 * MIB)
    publisher.publish(ctx(), make_request(video, direct_settings()))

    assert body(routes["init"].calls.last.request)["source_info"] == {
        "source": "FILE_UPLOAD",
        "video_size": size,
        "chunk_size": 5 * MIB,
        "total_chunk_count": 2,
    }
    calls = routes["upload"].calls
    assert len(calls) == 2
    data = video.read_bytes()
    first, second = calls[0].request, calls[1].request
    assert first.headers["content-range"] == f"bytes 0-{5 * MIB - 1}/{size}"
    assert first.headers["content-length"] == str(5 * MIB)
    assert first.content == data[: 5 * MIB]
    assert second.headers["content-range"] == f"bytes {5 * MIB}-{size - 1}/{size}"
    assert second.headers["content-length"] == str(size - 5 * MIB)
    assert second.content == data[5 * MIB :]
    assert all(c.request.headers["content-type"] == "video/mp4" for c in calls)


def test_direct_publish_respects_creator_disabled_interactions(router, sample_video):
    routes = mock_publish_flow(
        router, creator=creator_payload(comment_disabled=True, duet_disabled=False, stitch_disabled=False)
    )
    settings = direct_settings(
        allow_comment=True, allow_duet=True, allow_stitch="false", brand_organic_toggle=True
    )
    tt.publisher.publish(ctx(), make_request(sample_video, settings))
    post = body(routes["init"].calls.last.request)["post_info"]
    assert post["disable_comment"] is True  # creator turned comments off
    assert post["disable_duet"] is False
    assert post["disable_stitch"] is True  # string "false" is not consent
    assert post["brand_organic_toggle"] is True


def test_direct_publish_truncates_caption_to_2200_utf16_units(router, sample_video):
    routes = mock_publish_flow(router)
    caption = "😀" * 1500  # 3000 UTF-16 units
    tt.publisher.publish(ctx(), make_request(sample_video, direct_settings(), caption=caption))
    title = body(routes["init"].calls.last.request)["post_info"]["title"]
    assert len(title.encode("utf-16-le")) // 2 == 2200
    assert title == "😀" * 1100


def test_direct_publish_omits_empty_title(router, sample_video):
    routes = mock_publish_flow(router)
    tt.publisher.publish(ctx(), make_request(sample_video, direct_settings(), caption=""))
    assert "title" not in body(routes["init"].calls.last.request)["post_info"]


def test_direct_publish_uses_stored_username_when_creator_has_none(router, sample_video):
    mock_publish_flow(router, creator=creator_payload(creator_username=None))
    result = tt.publisher.publish(ctx(username="stored_name"), make_request(sample_video, direct_settings()))
    assert result.state["username"] == "stored_name"


def test_direct_publish_fails_when_privacy_no_longer_offered(router, sample_video):
    routes = mock_publish_flow(router, creator=creator_payload(privacy_level_options=["SELF_ONLY"]))
    with pytest.raises(PermanentError, match="SELF_ONLY") as ei:
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))
    assert ei.value.code == "privacy_level_option_mismatch"
    assert not routes["init"].called and not routes["upload"].called


def test_direct_publish_fails_when_longer_than_creator_limit(router, sample_video):
    routes = mock_publish_flow(router, creator=creator_payload(max_video_post_duration_sec=180))
    with pytest.raises(PermanentError, match="at most 180"):
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings(), duration=181))
    assert not routes["init"].called


def test_publish_missing_file_is_permanent(router, tmp_path):
    with pytest.raises(PermanentError, match="missing"):
        tt.publisher.publish(ctx(), make_request(tmp_path / "gone.mp4", direct_settings()))
    assert not router.calls


def test_inbox_publish_sends_source_info_only(router, sample_video):
    routes = mock_publish_flow(router)
    result = tt.publisher.publish(ctx(username="ml_user"), make_request(sample_video, {"mode": "inbox"}))
    size = sample_video.stat().st_size
    assert body(routes["inbox"].calls.last.request) == {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": size,
            "total_chunk_count": 1,
        }
    }
    assert not routes["creator"].called and not routes["init"].called
    put = routes["upload"].calls.last.request
    assert put.headers["content-range"] == f"bytes 0-{size - 1}/{size}"
    assert put.headers["content-type"] == "video/mp4"
    assert result.state == {
        "publish_id": "v_inbox_file~v2-1.2",
        "mode": "inbox",
        "username": "ml_user",
        "source": "FILE_UPLOAD",
        "init_at": result.state["init_at"],
        "video_size": size,
    }


INIT_ERRORS = [
    ("access_token_invalid", 401, AuthError),
    ("scope_not_authorized", 401, AuthError),
    ("rate_limit_exceeded", 429, RateLimitedError),
    ("spam_risk_too_many_posts", 403, RateLimitedError),
    ("reached_active_user_cap", 403, RateLimitedError),
    ("unaudited_client_can_only_post_to_private_accounts", 403, PermanentError),
    ("privacy_level_option_mismatch", 403, PermanentError),
    ("spam_risk_user_banned_from_posting", 403, PermanentError),
    ("invalid_param", 400, PermanentError),
    ("internal_error", 500, RetryableError),
]


@pytest.mark.parametrize(("code", "status", "exc_type"), INIT_ERRORS)
def test_direct_init_error_mapping(router, sample_video, code, status, exc_type):
    routes = mock_publish_flow(router)
    routes["init"].mock(return_value=api_error(code, status, message=f"detail with {ACCESS}"))
    with pytest.raises(exc_type) as ei:
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))
    exc = ei.value
    assert type(exc) is exc_type
    assert exc.code == code and exc.status == status
    assert ACCESS not in str(exc) and ACCESS not in exc.message
    assert not routes["upload"].called


def test_daily_caps_have_long_retry_after(router, sample_video):
    routes = mock_publish_flow(router)
    for code in ("spam_risk_too_many_posts", "reached_active_user_cap"):
        routes["init"].mock(return_value=api_error(code, 403))
        with pytest.raises(RateLimitedError) as ei:
            tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))
        assert ei.value.retry_after >= 6 * 3600


def test_rate_limit_uses_retry_after_header(router, sample_video):
    routes = mock_publish_flow(router)
    routes["init"].mock(return_value=api_error("rate_limit_exceeded", 429, headers={"Retry-After": "17"}))
    with pytest.raises(RateLimitedError) as ei:
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))
    assert ei.value.retry_after == 17


def test_unaudited_error_explains_self_only(router, sample_video):
    routes = mock_publish_flow(router)
    routes["init"].mock(return_value=api_error("unaudited_client_can_only_post_to_private_accounts", 403))
    with pytest.raises(PermanentError) as ei:
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))
    assert "SELF_ONLY" in ei.value.message and "audit" in ei.value.message
    routes["init"].mock(return_value=api_error("privacy_level_option_mismatch", 403))
    with pytest.raises(PermanentError) as ei:
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))
    assert "SELF_ONLY" in ei.value.message


def test_init_5xx_without_json_and_network_errors_are_retryable(router, sample_video):
    routes = mock_publish_flow(router)
    routes["init"].mock(return_value=httpx.Response(503, text="<html>down</html>"))
    with pytest.raises(RetryableError) as ei:
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))
    assert type(ei.value) is RetryableError
    routes["init"].mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(RetryableError, match="ReadTimeout"):
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))


def test_init_without_upload_url_is_retryable(router, sample_video):
    routes = mock_publish_flow(router)
    routes["init"].mock(return_value=ok({"publish_id": "p1"}))
    with pytest.raises(RetryableError):
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))


def test_inbox_pending_share_cap(router, sample_video):
    routes = mock_publish_flow(router)
    routes["inbox"].mock(return_value=api_error("spam_risk_too_many_pending_share", 403))
    with pytest.raises(RateLimitedError) as ei:
        tt.publisher.publish(ctx(), make_request(sample_video, {"mode": "inbox"}))
    assert ei.value.retry_after >= 6 * 3600


def test_creator_info_auth_error_stops_publish(router, sample_video):
    routes = mock_publish_flow(router)
    routes["creator"].mock(return_value=api_error("scope_not_authorized", 401))
    with pytest.raises(AuthError):
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))
    assert not routes["init"].called


def test_chunk_5xx_is_retried_then_succeeds(router, sample_video, _no_sleep):
    routes = mock_publish_flow(router, upload_statuses=(502, 201))
    result = tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))
    assert result.outcome == PublishOutcome.PROCESSING
    assert routes["upload"].call_count == 2
    assert _no_sleep == [tt.CHUNK_RETRY_BACKOFF]
    a, b = (c.request for c in routes["upload"].calls)
    assert a.headers["content-range"] == b.headers["content-range"]


def test_chunk_persistent_failure_before_the_last_chunk_is_retryable(router, tmp_path):
    video = write_file(tmp_path, 12 * MIB)
    routes = mock_publish_flow(router, upload_statuses=(500, 500, 500))
    with pytest.raises(RetryableError) as ei:
        tt.TikTokPublisher(chunk_bytes=5 * MIB).publish(ctx(), make_request(video, direct_settings()))
    assert not isinstance(ei.value, UnknownOutcomeError)  # the last byte never left: nothing can be live
    assert routes["upload"].call_count == tt.CHUNK_ATTEMPTS


def test_chunk_network_errors_are_retried(router, sample_video):
    routes = mock_publish_flow(router)
    routes["upload"].mock(side_effect=[httpx.WriteError("reset"), httpx.Response(201)])
    tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))
    assert routes["upload"].call_count == 2


def test_chunk_lost_response_then_416_with_progress_continues(router, tmp_path):
    size = 11 * MIB
    video = write_file(tmp_path, size)
    routes = mock_publish_flow(router)
    routes["upload"].mock(
        side_effect=[
            httpx.ReadTimeout("lost"),
            httpx.Response(416, headers={"Content-Range": f"bytes 0-{5 * MIB - 1}/{size}"}),
            httpx.Response(201),
        ]
    )
    tt.TikTokPublisher(chunk_bytes=5 * MIB).publish(ctx(), make_request(video, direct_settings()))
    ranges = [c.request.headers["content-range"] for c in routes["upload"].calls]
    assert ranges == [
        f"bytes 0-{5 * MIB - 1}/{size}",
        f"bytes 0-{5 * MIB - 1}/{size}",
        f"bytes {5 * MIB}-{size - 1}/{size}",
    ]


@pytest.mark.parametrize(
    ("status", "exc_type"),
    [
        (403, RetryableError),
        (404, RetryableError),
        (416, RetryableError),
        (400, PermanentError),
        (429, RateLimitedError),
    ],
)
def test_chunk_client_errors(router, sample_video, status, exc_type):
    routes = mock_publish_flow(router, upload_statuses=(status,))
    with pytest.raises(exc_type) as ei:
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))
    assert routes["upload"].call_count == 1
    assert "SECRET-upload" not in str(ei.value)


def test_final_chunk_must_be_confirmed(router, sample_video):
    mock_publish_flow(router, upload_statuses=(206,))
    with pytest.raises(RetryableError, match="confirm"):
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))


# --------------------------------------------------------------------------- poll


def status_response(**data) -> httpx.Response:
    return ok(data)


STATE = {"publish_id": "v_pub_file~v2-1.1", "mode": "direct", "username": "magiclap"}


def test_poll_publish_complete_with_post_id(router):
    route = router.post(tt.STATUS_URL).mock(
        return_value=status_response(
            status="PUBLISH_COMPLETE", publicaly_available_post_id=[7312345678901234567]
        )
    )
    result = tt.publisher.poll(ctx(), STATE)
    req = route.calls.last.request
    assert body(req) == {"publish_id": "v_pub_file~v2-1.1"}
    assert req.headers["authorization"] == f"Bearer {ACCESS}"
    assert req.headers["content-type"] == "application/json; charset=UTF-8"
    assert result.outcome == PublishOutcome.PUBLISHED
    assert result.platform_post_id == "7312345678901234567"
    assert result.permalink == "https://www.tiktok.com/@magiclap/video/7312345678901234567"


def test_poll_publish_complete_without_public_id(router):
    router.post(tt.STATUS_URL).mock(
        return_value=status_response(status="PUBLISH_COMPLETE", publicaly_available_post_id=[])
    )
    result = tt.publisher.poll(ctx(), STATE)
    assert result.outcome == PublishOutcome.PUBLISHED
    assert result.platform_post_id is None and result.permalink is None
    assert "public" in result.message


def test_poll_publish_complete_without_username_has_no_permalink(router):
    router.post(tt.STATUS_URL).mock(
        return_value=status_response(status="PUBLISH_COMPLETE", publicaly_available_post_id=[123])
    )
    result = tt.publisher.poll(ctx(), {**STATE, "username": None})
    assert result.platform_post_id == "123" and result.permalink is None


def test_poll_inbox_delivery_counts_as_published(router):
    router.post(tt.STATUS_URL).mock(return_value=status_response(status="SEND_TO_USER_INBOX"))
    result = tt.publisher.poll(ctx(), {**STATE, "mode": "inbox"})
    assert result.outcome == PublishOutcome.PUBLISHED
    assert "inbox" in result.message.lower()
    assert result.platform_post_id is None


@pytest.mark.parametrize("status", ["PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD", "SOMETHING_NEW"])
def test_poll_processing(router, status):
    router.post(tt.STATUS_URL).mock(return_value=status_response(status=status, uploaded_bytes=100))
    result = tt.publisher.poll(ctx(), STATE)
    assert result.outcome == PublishOutcome.PROCESSING
    assert result.state == STATE
    assert result.poll_after_seconds == 30


@pytest.mark.parametrize(
    ("reason", "exc_type"),
    [
        ("internal", RestartPublish),
        ("video_pull_failed", RestartPublish),
        ("publish_cancelled", RestartPublish),
        ("file_format_check_failed", PermanentError),
        ("duration_check_failed", PermanentError),
        ("frame_rate_check_failed", PermanentError),
        ("picture_size_check_failed", PermanentError),
        ("spam_risk_text", PermanentError),
        ("spam_risk", PermanentError),
        ("spam_risk_user_banned_from_posting", PermanentError),
        ("auth_removed", AuthError),
        ("spam_risk_too_many_posts", RestartPublish),
        ("brand_new_reason", PermanentError),
        ("", PermanentError),
    ],
)
def test_poll_failed_reasons(router, reason, exc_type):
    router.post(tt.STATUS_URL).mock(return_value=status_response(status="FAILED", fail_reason=reason))
    with pytest.raises(exc_type) as ei:
        tt.publisher.poll(ctx(), STATE)
    assert type(ei.value) is exc_type
    assert ei.value.code == (reason or None)


def test_poll_status_api_errors(router):
    route = router.post(tt.STATUS_URL).mock(return_value=api_error("invalid_publish_id", 400))
    with pytest.raises(PermanentError):
        tt.publisher.poll(ctx(), STATE)
    route.mock(return_value=api_error("access_token_invalid", 401))
    with pytest.raises(AuthError):
        tt.publisher.poll(ctx(), STATE)
    route.mock(return_value=api_error("rate_limit_exceeded", 429))
    with pytest.raises(RateLimitedError):
        tt.publisher.poll(ctx(), STATE)
    route.mock(side_effect=httpx.ConnectTimeout("t"))
    with pytest.raises(RetryableError):
        tt.publisher.poll(ctx(), STATE)


def test_poll_without_publish_id(router):
    with pytest.raises(PermanentError):
        tt.publisher.poll(ctx(), {"mode": "direct"})
    assert not router.calls


# --------------------------------------------------------------------------- PULL_FROM_URL (api-tiktok-youtube-3)


def pull_flow(router, *, creator: dict | None = None):
    return {
        "creator": router.post(tt.CREATOR_INFO_URL).mock(return_value=ok(creator or creator_payload())),
        "init": router.post(tt.DIRECT_INIT_URL).mock(return_value=ok({"publish_id": "v_pub_url~v2-1.9"})),
        "inbox": router.post(tt.INBOX_INIT_URL).mock(return_value=ok({"publish_id": "v_inbox_url~v2-1.8"})),
        "upload": router.put(url__startswith="https://open-upload.tiktokapis.com/video/").mock(
            return_value=httpx.Response(201)
        ),
    }


def test_direct_publish_lets_tiktok_pull_the_signed_url(router, sample_video):
    routes = pull_flow(router)
    req = make_request(sample_video, direct_settings(), public_url=PUBLIC_URL)

    result = tt.publisher.publish(ctx(), req)

    init = body(routes["init"].calls.last.request)
    assert init["source_info"] == {"source": "PULL_FROM_URL", "video_url": PUBLIC_URL}
    assert init["post_info"]["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert not routes["upload"].called  # no byte of the file is sent by Repeater
    assert result.outcome == PublishOutcome.PROCESSING and result.poll_after_seconds == 30
    assert result.state["publish_id"] == "v_pub_url~v2-1.9" and result.state["source"] == "PULL_FROM_URL"
    assert "video_size" not in result.state


def test_inbox_publish_pulls_from_the_signed_url_too(router, sample_video):
    routes = pull_flow(router)
    result = tt.publisher.publish(
        ctx(username="ml_user"), make_request(sample_video, {"mode": "inbox"}, public_url=PUBLIC_URL)
    )
    assert body(routes["inbox"].calls.last.request) == {
        "source_info": {"source": "PULL_FROM_URL", "video_url": PUBLIC_URL}
    }
    assert not routes["upload"].called and not routes["creator"].called
    assert result.state["mode"] == "inbox" and result.state["source"] == "PULL_FROM_URL"


@pytest.mark.parametrize("url", ["", "http://localhost:8000/m/1/2/sig.mp4"])
def test_urls_tiktok_cannot_pull_use_file_upload(router, sample_video, url):
    routes = mock_publish_flow(router)
    tt.publisher.publish(ctx(), make_request(sample_video, direct_settings(), public_url=url))
    assert body(routes["init"].calls.last.request)["source_info"]["source"] == "FILE_UPLOAD"
    assert routes["upload"].called


def unverified_then_file_flow(router):
    routes = mock_publish_flow(router)
    routes["init"].mock(
        side_effect=[
            api_error("url_ownership_unverified", 403),
            ok({"publish_id": "v_pub_file~v2-1.1", "upload_url": UPLOAD_URL}),
            ok({"publish_id": "v_pub_file~v2-1.5", "upload_url": UPLOAD_URL}),
            ok({"publish_id": "v_pub_url~v2-1.7"}),
        ]
    )
    routes["upload"].mock(return_value=httpx.Response(201))
    return routes


def test_unverified_url_prefix_falls_back_to_file_upload_and_is_remembered(router, sample_video, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(tt, "_monotonic", lambda: clock[0])
    routes = unverified_then_file_flow(router)
    req = make_request(sample_video, direct_settings(), public_url=PUBLIC_URL)

    first = tt.publisher.publish(ctx(), req)

    sources = [body(c.request)["source_info"]["source"] for c in routes["init"].calls]
    assert sources == ["PULL_FROM_URL", "FILE_UPLOAD"]
    assert routes["upload"].call_count == 1 and routes["creator"].call_count == 1
    assert first.state["publish_id"] == "v_pub_file~v2-1.1" and first.state["source"] == "FILE_UPLOAD"

    clock[0] += 60  # later posts of this process go straight to FILE_UPLOAD
    tt.publisher.publish(ctx(), req)
    sources = [body(c.request)["source_info"]["source"] for c in routes["init"].calls]
    assert sources == ["PULL_FROM_URL", "FILE_UPLOAD", "FILE_UPLOAD"]

    clock[0] += tt.PULL_UNVERIFIED_RETRY_SECONDS  # verifying the prefix needs no restart
    again = tt.publisher.publish(ctx(), req)
    assert body(routes["init"].calls.last.request)["source_info"]["source"] == "PULL_FROM_URL"
    assert again.state["source"] == "PULL_FROM_URL"


def test_other_init_refusals_do_not_trigger_the_file_fallback(router, sample_video):
    routes = pull_flow(router)
    routes["init"].mock(return_value=api_error("spam_risk_too_many_posts", 403))
    with pytest.raises(RateLimitedError):
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings(), public_url=PUBLIC_URL))
    assert routes["init"].call_count == 1 and not routes["upload"].called
    assert tt._pull_cache.unverified_until == 0.0


def test_lost_answer_to_a_pull_init_may_be_live_and_is_not_retried(router, sample_video):
    routes = pull_flow(router)
    routes["init"].mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(UnknownOutcomeError) as ei:
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings(), public_url=PUBLIC_URL))
    assert ei.value.state == {}  # nothing to poll with: the engine reports "may be live"
    assert "ReadTimeout" not in ei.value.message or PUBLIC_URL not in ei.value.message
    assert PUBLIC_URL not in ei.value.message and ACCESS not in ei.value.message


def test_pull_init_that_provably_never_arrived_is_a_plain_retry(router, sample_video):
    routes = pull_flow(router)
    routes["init"].mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(RetryableError) as ei:
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings(), public_url=PUBLIC_URL))
    assert not isinstance(ei.value, UnknownOutcomeError)


def test_pull_init_answered_without_a_publish_id_may_be_live(router, sample_video):
    routes = pull_flow(router)
    routes["init"].mock(return_value=ok({}))
    with pytest.raises(UnknownOutcomeError):
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings(), public_url=PUBLIC_URL))


# --------------------------------------------------------------------------- unknown final chunk (api-tiktok-youtube-2)

LOST_FINAL = [
    pytest.param([httpx.ReadTimeout("lost"), httpx.Response(404)], id="reply-lost-then-404"),
    pytest.param([httpx.ReadTimeout("lost"), httpx.Response(403)], id="reply-lost-then-403"),
    pytest.param([httpx.ReadTimeout("lost"), httpx.Response(416)], id="reply-lost-then-416-without-range"),
    pytest.param([httpx.ReadTimeout("lost"), httpx.Response(400)], id="reply-lost-then-400"),
    pytest.param([httpx.ReadTimeout("lost")] * 3, id="reply-lost-three-times"),
    pytest.param([httpx.Response(503)] * 3, id="server-error-three-times"),
    pytest.param([httpx.ReadError("reset"), httpx.Response(429)], id="reply-lost-then-throttled"),
]


@pytest.mark.parametrize("answers", LOST_FINAL)
def test_unclear_final_chunk_is_an_unknown_outcome_never_a_second_post(router, sample_video, answers):
    routes = mock_publish_flow(router)
    routes["upload"].mock(side_effect=answers)

    with pytest.raises(UnknownOutcomeError) as ei:
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))

    err = ei.value
    size = sample_video.stat().st_size
    assert err.state == {
        "publish_id": "v_pub_file~v2-1.1",
        "mode": "direct",
        "username": "magiclap",
        "source": "FILE_UPLOAD",
        "init_at": err.state["init_at"],
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "video_size": size,
    }
    assert routes["init"].call_count == 1
    assert "SECRET-upload" not in err.message and "SECRET-upload" not in str(err)

    # The engine saves the state and polls: TikTok says what happened, no second init.
    router.post(tt.STATUS_URL).mock(
        return_value=status_response(status="PUBLISH_COMPLETE", publicaly_available_post_id=[55])
    )
    result = tt.publisher.poll(ctx(), err.state)
    assert result.outcome == PublishOutcome.PUBLISHED and result.platform_post_id == "55"
    assert routes["init"].call_count == 1


@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"), httpx.WriteError("reset")])
def test_final_chunk_that_never_left_is_a_plain_retry(router, sample_video, failure):
    routes = mock_publish_flow(router)
    routes["upload"].mock(side_effect=[failure] * 3)
    with pytest.raises(RetryableError) as ei:
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))
    assert not isinstance(ei.value, UnknownOutcomeError)


def test_unclear_last_chunk_of_a_multi_chunk_upload_is_an_unknown_outcome(router, tmp_path):
    video = write_file(tmp_path, 11 * MIB)
    routes = mock_publish_flow(router)
    routes["upload"].mock(side_effect=[httpx.Response(206)] + [httpx.ReadTimeout("lost")] * 3)
    with pytest.raises(UnknownOutcomeError) as ei:
        tt.TikTokPublisher(chunk_bytes=5 * MIB).publish(ctx(), make_request(video, direct_settings()))
    assert ei.value.state["video_size"] == 11 * MIB and routes["init"].call_count == 1


def test_publish_with_an_earlier_publish_id_asks_tiktok_instead_of_posting_again(router, sample_video):
    routes = mock_publish_flow(router)
    status = router.post(tt.STATUS_URL).mock(return_value=status_response(status="PROCESSING_UPLOAD"))
    state = {**STATE, "source": "FILE_UPLOAD", "video_size": 100, "init_at": utcnow().isoformat()}

    result = tt.publisher.publish(ctx(), make_request(sample_video, direct_settings(), state=state))

    assert result.outcome == PublishOutcome.PROCESSING and result.state == state
    assert body(status.calls.last.request) == {"publish_id": STATE["publish_id"]}
    assert not routes["init"].called and not routes["upload"].called


def test_publish_starts_over_only_when_tiktok_dropped_the_earlier_upload(router, sample_video):
    routes = mock_publish_flow(router)
    router.post(tt.STATUS_URL).mock(
        return_value=status_response(status="FAILED", fail_reason="video_pull_failed")
    )
    result = tt.publisher.publish(
        ctx(), make_request(sample_video, direct_settings(), state={**STATE, "source": "PULL_FROM_URL"})
    )
    assert routes["init"].call_count == 1 and result.state["publish_id"] == "v_pub_file~v2-1.1"


def test_publish_does_not_start_over_after_a_final_failure_of_the_earlier_upload(router, sample_video):
    routes = mock_publish_flow(router)
    router.post(tt.STATUS_URL).mock(return_value=status_response(status="FAILED", fail_reason="spam_risk"))
    with pytest.raises(PermanentError):
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings(), state=dict(STATE)))
    assert not routes["init"].called


def upload_state(age: timedelta, **over) -> dict:
    return {
        **STATE,
        "source": "FILE_UPLOAD",
        "video_size": 1000,
        "init_at": (utcnow() - age).isoformat(),
        **over,
    }


def test_poll_restarts_an_upload_that_never_received_all_bytes_after_the_url_lifetime(router):
    router.post(tt.STATUS_URL).mock(
        return_value=status_response(status="PROCESSING_UPLOAD", uploaded_bytes=0)
    )
    with pytest.raises(RestartPublish) as ei:
        tt.publisher.poll(ctx(), upload_state(timedelta(minutes=61)))
    assert ei.value.code == "upload_incomplete" and ei.value.retry_after == tt.FAILED_RETRY_AFTER
    assert "SECRET" not in ei.value.message


def test_poll_keeps_waiting_while_the_upload_url_is_still_valid_or_all_bytes_arrived(router):
    route = router.post(tt.STATUS_URL).mock(
        return_value=status_response(status="PROCESSING_UPLOAD", uploaded_bytes=0)
    )
    assert tt.publisher.poll(ctx(), upload_state(timedelta(minutes=59))).outcome == PublishOutcome.PROCESSING
    route.mock(return_value=status_response(status="PROCESSING_UPLOAD", uploaded_bytes=1000))
    assert tt.publisher.poll(ctx(), upload_state(timedelta(hours=3))).outcome == PublishOutcome.PROCESSING
    route.mock(return_value=status_response(status="PROCESSING_UPLOAD"))
    state = upload_state(timedelta(hours=3))
    del state["init_at"]  # an old state without init_at is never restarted on a guess
    assert tt.publisher.poll(ctx(), state).outcome == PublishOutcome.PROCESSING
    route.mock(return_value=status_response(status="PROCESSING_DOWNLOAD"))
    pulled = upload_state(timedelta(hours=3), source="PULL_FROM_URL")
    assert (
        tt.publisher.poll(ctx(), pulled).outcome == PublishOutcome.PROCESSING
    )  # TikTok times pulls out itself


# --------------------------------------------------------------------------- daily cap at status time (api-tiktok-youtube-4)


def test_failed_with_the_daily_cap_restarts_the_post_after_the_cap_window(router):
    router.post(tt.STATUS_URL).mock(
        return_value=status_response(status="FAILED", fail_reason="spam_risk_too_many_posts")
    )
    with pytest.raises(RestartPublish) as ei:
        tt.publisher.poll(ctx(), STATE)
    err = ei.value
    assert not isinstance(err, RateLimitedError)  # FAILED is final: "poll again" would hide a lost post
    assert err.code == "spam_risk_too_many_posts" and err.retry_after == tt.DAILY_CAP_RETRY_AFTER
    assert "daily posting limit" in err.message


# --------------------------------------------------------------------------- post id after moderation (api-tiktok-youtube-missed-3)

MODERATION_T0 = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
PUBLIC_STATE = {
    **STATE,
    "source": "PULL_FROM_URL",
    "init_at": MODERATION_T0.isoformat(),
    "privacy_level": "PUBLIC_TO_EVERYONE",
}


@pytest.fixture
def clock(monkeypatch):
    now = [MODERATION_T0]
    monkeypatch.setattr(tt, "utcnow", lambda: now[0])

    def at(minutes: float) -> None:
        now[0] = MODERATION_T0 + timedelta(minutes=minutes)

    return at


def test_public_post_waits_for_moderation_then_reports_the_post_id_and_link(router, clock):
    route = router.post(tt.STATUS_URL).mock(
        return_value=status_response(status="PUBLISH_COMPLETE", publicaly_available_post_id=[])
    )
    clock(3)
    waiting = tt.publisher.poll(ctx(), PUBLIC_STATE)
    assert waiting.outcome == PublishOutcome.PROCESSING
    assert waiting.platform_post_id is None and waiting.permalink is None
    assert waiting.poll_after_seconds == tt.POST_ID_POLL_SECONDS
    assert waiting.state == {
        **PUBLIC_STATE,
        "complete_seen_at": (MODERATION_T0 + timedelta(minutes=3)).isoformat(),
    }
    assert "moderation" in waiting.message

    clock(8)  # moderation finished
    route.mock(
        return_value=status_response(
            status="PUBLISH_COMPLETE", publicaly_available_post_id=[7312345678901234567]
        )
    )
    done = tt.publisher.poll(ctx(), waiting.state)
    assert done.outcome == PublishOutcome.PUBLISHED
    assert done.platform_post_id == "7312345678901234567"
    assert done.permalink == "https://www.tiktok.com/@magiclap/video/7312345678901234567"


def test_the_wait_for_the_post_id_is_bounded_and_ends_published(router, clock):
    router.post(tt.STATUS_URL).mock(
        return_value=status_response(status="PUBLISH_COMPLETE", publicaly_available_post_id=[])
    )
    clock(3)
    state = tt.publisher.poll(ctx(), PUBLIC_STATE).state
    clock(3 + 29)
    assert tt.publisher.poll(ctx(), state).outcome == PublishOutcome.PROCESSING
    clock(3 + 31)
    final = tt.publisher.poll(ctx(), state)
    assert final.outcome == PublishOutcome.PUBLISHED  # live, just without an id
    assert final.platform_post_id is None and "moderation" in final.message


def test_the_post_id_wait_never_outlasts_the_engines_two_hour_limit(router, clock):
    router.post(tt.STATUS_URL).mock(
        return_value=status_response(status="PUBLISH_COMPLETE", publicaly_available_post_id=[])
    )
    clock(95)  # TikTok took 95 minutes to finish the upload and publish
    state = tt.publisher.poll(ctx(), PUBLIC_STATE).state
    clock(99)
    assert tt.publisher.poll(ctx(), state).outcome == PublishOutcome.PROCESSING
    clock(101)  # 100 minutes after init: stop, well before the 2 h limit
    assert tt.publisher.poll(ctx(), state).outcome == PublishOutcome.PUBLISHED


def test_post_id_in_the_completing_poll_is_reported_at_once(router, clock):
    router.post(tt.STATUS_URL).mock(
        return_value=status_response(status="PUBLISH_COMPLETE", publicaly_available_post_id=[321])
    )
    clock(2)
    result = tt.publisher.poll(ctx(), PUBLIC_STATE)
    assert result.outcome == PublishOutcome.PUBLISHED and result.platform_post_id == "321"


@pytest.mark.parametrize("privacy", ["SELF_ONLY", "FOLLOWER_OF_CREATOR", "MUTUAL_FOLLOW_FRIENDS", None])
def test_posts_that_cannot_get_an_id_are_published_without_waiting(router, clock, privacy):
    router.post(tt.STATUS_URL).mock(
        return_value=status_response(status="PUBLISH_COMPLETE", publicaly_available_post_id=[])
    )
    clock(2)
    result = tt.publisher.poll(ctx(), {**PUBLIC_STATE, "privacy_level": privacy})
    assert result.outcome == PublishOutcome.PUBLISHED and result.platform_post_id is None


def test_waiting_for_the_post_id_never_posts_again(router, clock, sample_video):
    routes = mock_publish_flow(router)
    router.post(tt.STATUS_URL).mock(
        return_value=status_response(status="PUBLISH_COMPLETE", publicaly_available_post_id=[])
    )
    clock(3)
    waiting = tt.publisher.publish(ctx(), make_request(sample_video, direct_settings(), state=PUBLIC_STATE))
    assert waiting.outcome == PublishOutcome.PROCESSING and waiting.state["complete_seen_at"]
    assert not routes["init"].called and not routes["upload"].called


# --------------------------------------------------------------------------- unaudited apps (api-tiktok-youtube-7)


def test_unaudited_guidance_requires_a_private_account_and_only_me():
    text = tt._CODE_EXPLANATIONS["unaudited_client_can_only_post_to_private_accounts"]
    assert "private" in text and "SELF_ONLY" in text and "both" in text
    assert " or " not in text  # it used to offer one of the two conditions as enough


def test_unaudited_init_refusal_names_both_conditions(router, sample_video):
    routes = mock_publish_flow(router)
    routes["init"].mock(return_value=api_error("unaudited_client_can_only_post_to_private_accounts", 403))
    with pytest.raises(PermanentError) as ei:
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings(privacy_level="SELF_ONLY")))
    assert "private" in ei.value.message and "SELF_ONLY" in ei.value.message


def test_unaudited_app_accepts_only_the_only_me_privacy_level(sample_video, monkeypatch):
    monkeypatch.setattr(tt, "_client_audited", lambda: False)
    problems = tt.publisher.validate(ctx(), make_request(sample_video, direct_settings()))
    assert len(problems) == 1 and "SELF_ONLY" in problems[0] and "private" in problems[0]
    assert (
        tt.publisher.validate(ctx(), make_request(sample_video, direct_settings(privacy_level="SELF_ONLY")))
        == []
    )
    assert tt.publisher.validate(ctx(), make_request(sample_video, {"mode": "inbox"})) == []
    monkeypatch.setattr(tt, "_client_audited", lambda: True)
    assert tt.publisher.validate(ctx(), make_request(sample_video, direct_settings())) == []


def test_audit_state_follows_the_setting_and_defaults_to_unaudited(monkeypatch):
    from repeater.config import Settings, get_settings

    assert Settings.model_fields["tiktok_audited"].default is False
    monkeypatch.setattr(get_settings(), "tiktok_audited", False)
    assert tt._client_audited() is False
    monkeypatch.setattr(get_settings(), "tiktok_audited", True)
    assert tt._client_audited() is True


def test_pull_publish_logs_no_signed_url(router, sample_video, caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="repeater.platforms.tiktok")
    pull_flow(router)
    tt.publisher.publish(ctx(), make_request(sample_video, direct_settings(), public_url=PUBLIC_URL))
    lines = [r.getMessage() for r in caplog.records if r.name == "repeater.platforms.tiktok"]
    assert lines and all("sig.mp4" not in line and ACCESS not in line for line in lines)


# --------------------------------------------------------------------------- listing


def video(vid: str, created: datetime, **over) -> dict:
    data = {
        "id": vid,
        "create_time": int(created.timestamp()),
        "cover_image_url": f"https://p16.tiktokcdn.com/{vid}.jpg",
        "share_url": f"https://www.tiktok.com/@magiclap/video/{vid}",
        "video_description": f"desc {vid}",
        "duration": 15,
        "height": 1920,
        "width": 1080,
        "title": "",
    }
    data.update(over)
    return data


T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_list_recent_paginates_and_stops_at_since(router):
    route = router.post(tt.VIDEO_LIST_URL).mock(
        side_effect=[
            ok(
                {
                    "videos": [video("5", T0 + timedelta(days=5)), video("4", T0 + timedelta(days=4))],
                    "cursor": 1700000000000,
                    "has_more": True,
                }
            ),
            ok(
                {
                    "videos": [video("3", T0 + timedelta(days=3)), video("2", T0 + timedelta(days=1))],
                    "cursor": 1600000000000,
                    "has_more": True,
                }
            ),
            ok({"videos": [video("1", T0)], "cursor": 1500000000000, "has_more": False}),
        ]
    )
    items = tt.lister.list_recent(ctx(), since=T0 + timedelta(days=2), limit=50)

    assert [v.external_id for v in items] == ["5", "4", "3"]
    assert route.call_count == 2  # stopped inside page 2, never asked for page 3
    first, second = (c.request for c in route.calls)
    assert first.url.params["fields"] == tt.VIDEO_FIELDS
    assert first.headers["authorization"] == f"Bearer {ACCESS}"
    assert body(first) == {"max_count": 20}
    assert body(second) == {"max_count": 20, "cursor": 1700000000000}

    v = items[0]
    assert v == RemoteVideo(
        external_id="5",
        created_at=T0 + timedelta(days=5),
        title="",
        caption="desc 5",
        permalink="https://www.tiktok.com/@magiclap/video/5",
        duration_s=15.0,
        width=1080,
        height=1920,
        download_url=None,
        is_video=True,
        extra={"cover_image_url": "https://p16.tiktokcdn.com/5.jpg"},
    )


def test_list_recent_includes_video_at_exact_since(router):
    router.post(tt.VIDEO_LIST_URL).mock(
        return_value=ok(
            {"videos": [video("2", T0), video("1", T0 - timedelta(seconds=1))], "has_more": False}
        )
    )
    items = tt.lister.list_recent(ctx(), since=T0.replace(tzinfo=None))  # naive since treated as UTC
    assert [v.external_id for v in items] == ["2"]


def test_list_recent_respects_limit(router):
    route = router.post(tt.VIDEO_LIST_URL).mock(
        return_value=ok(
            {
                "videos": [video(str(i), T0 - timedelta(days=i)) for i in range(5)],
                "cursor": 1,
                "has_more": True,
            }
        )
    )
    items = tt.lister.list_recent(ctx(), limit=3)
    assert len(items) == 3
    assert body(route.calls.last.request) == {"max_count": 3}
    assert route.call_count == 1


def test_list_recent_until_has_more_false(router):
    route = router.post(tt.VIDEO_LIST_URL).mock(
        side_effect=[
            ok({"videos": [video("2", T0)], "cursor": 10, "has_more": True}),
            ok({"videos": [video("1", T0 - timedelta(days=1))], "cursor": 5, "has_more": False}),
        ]
    )
    assert [v.external_id for v in tt.lister.list_recent(ctx())] == ["2", "1"]
    assert route.call_count == 2


def test_list_recent_guards_against_stuck_cursor(router):
    route = router.post(tt.VIDEO_LIST_URL).mock(
        side_effect=[
            ok({"videos": [video("2", T0)], "cursor": 10, "has_more": True}),
            ok({"videos": [video("1", T0)], "cursor": 10, "has_more": True}),
        ]
    )
    assert len(tt.lister.list_recent(ctx())) == 2
    assert route.call_count == 2


def test_list_recent_empty_and_malformed_entries(router):
    router.post(tt.VIDEO_LIST_URL).mock(
        return_value=ok(
            {"videos": [{"title": "no id"}, video("1", T0, duration=None, width=None)], "has_more": False}
        )
    )
    [item] = tt.lister.list_recent(ctx())
    assert item.external_id == "1" and item.duration_s is None and item.width is None


def test_list_recent_errors(router):
    route = router.post(tt.VIDEO_LIST_URL).mock(return_value=api_error("scope_not_authorized", 401))
    with pytest.raises(AuthError):
        tt.lister.list_recent(ctx())
    route.mock(return_value=httpx.Response(500, text="err"))
    with pytest.raises(RetryableError):
        tt.lister.list_recent(ctx())


def test_download_not_supported(tmp_path):
    remote = RemoteVideo(external_id="1", permalink="https://www.tiktok.com/@magiclap/video/1")
    with pytest.raises(NotSupportedError):
        tt.lister.download(ctx(), remote, tmp_path / "out.mp4")
    assert not (tmp_path / "out.mp4").exists()


def test_adapter_logs_never_contain_secrets(router, sample_video, caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="repeater.platforms.tiktok")
    routes = mock_publish_flow(router)
    tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))
    routes["init"].mock(return_value=api_error("invalid_param", 400, message=f"bad {ACCESS}"))
    with pytest.raises(PermanentError):
        tt.publisher.publish(ctx(), make_request(sample_video, direct_settings()))
    router.post(tt.TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(AuthError):
        tt.oauth.refresh(ctx())
    ours = [r.getMessage() for r in caplog.records if r.name == "repeater.platforms.tiktok"]
    assert ours, "expected adapter log lines"
    for line in ours:
        assert ACCESS not in line and REFRESH not in line and "SECRET-upload" not in line
        assert "tt-secret" not in line
