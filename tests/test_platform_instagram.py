"""Instagram Reels publisher and media lister (Instagram API with Facebook Login)."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from repeater.models import Platform
from repeater.platforms import get_lister, get_publisher, instagram, meta
from repeater.platforms.base import (
    AccountContext,
    AuthError,
    MediaLister,
    OAuthTokens,
    PermanentError,
    Publisher,
    PublishOutcome,
    PublishRequest,
    RateLimitedError,
    RemoteVideo,
    RestartPublish,
    RetryableError,
)

GRAPH = "https://graph.facebook.com/v26.0"
TOKEN = "ig-page-token-SECRET"
IG_ID = "17841400000000001"
CONTAINER = "17889455560051444"
MEDIA_ID = "17920238422030506"
PUBLIC_URL = "https://rotator.test/m/7/1999999999/sig.mp4"
CDN_URL = "https://scontent.cdninstagram.test/v/video.mp4?oe=abc"
LIMIT_URL = f"{GRAPH}/{IG_ID}/content_publishing_limit"
MEDIA_URL = f"{GRAPH}/{IG_ID}/media"
PUBLISH_URL = f"{GRAPH}/{IG_ID}/media_publish"
CONTAINER_URL = f"{GRAPH}/{CONTAINER}"
MEDIA_NODE_URL = f"{GRAPH}/{MEDIA_ID}"


@pytest.fixture
def mock() -> Any:
    with respx.mock(assert_all_called=False) as m:
        yield m
        assert_signed(m)


def proof(token: str) -> str:
    return hmac.new(b"m-secret", token.encode(), hashlib.sha256).hexdigest()


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def assert_signed(mock: Any) -> None:
    for call in mock.calls:
        request = call.request
        assert TOKEN not in str(request.url)
        if request.url.host != "graph.facebook.com":
            assert "authorization" not in request.headers
            continue
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        params = dict(request.url.params) if request.method == "GET" else form(request)
        assert params["appsecret_proof"] == proof(TOKEN)


def ctx(**extra: Any) -> AccountContext:
    return AccountContext(
        account_id=5,
        platform=Platform.INSTAGRAM,
        external_id=IG_ID,
        tokens=OAuthTokens(access_token=TOKEN),
        extra={"ig_user_id": IG_ID, "page_id": "111", "username": "magiclap", "fb_user_id": "u1", **extra},
    )


def request(**overrides: Any) -> PublishRequest:
    values: dict[str, Any] = {
        "post_id": 42,
        "video_path": Path("/nonexistent/encoded.mp4"),
        "public_url": PUBLIC_URL,
        "size_bytes": 20 * 1024**2,
        "duration_s": 30.0,
        "width": 1080,
        "height": 1920,
        "title": "Magic trick",
        "caption": "Watch this @friend #magic #trick",
        "settings": {"share_to_feed": True, "is_ai_generated": False},
    }
    values.update(overrides)
    return PublishRequest(**values)


def graph_error(status: int, code: int, subcode: int | None = None, message: str = "error") -> httpx.Response:
    error: dict[str, Any] = {"message": message, "type": "OAuthException", "code": code, "fbtrace_id": "T"}
    if subcode is not None:
        error["error_subcode"] = subcode
    return httpx.Response(status, json={"error": error})


def quota(usage: int, total: int = 50) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": [{"quota_usage": usage, "config": {"quota_total": total, "quota_duration": 86400}}]},
    )


def state(**overrides: Any) -> dict[str, Any]:
    values = {
        "container_id": CONTAINER,
        "created_at": "2026-10-14T10:00:00+00:00",
        "ig_user_id": IG_ID,
        "caption_key": instagram.caption_key("Watch this @friend #magic #trick"),
    }
    values.update(overrides)
    return values


# --------------------------------------------------------------------------- contract & validate


def test_contract_and_registry():
    assert isinstance(instagram.publisher, Publisher)
    assert isinstance(instagram.lister, MediaLister)
    assert get_publisher("instagram") is instagram.publisher
    assert get_lister("instagram") is instagram.lister
    assert instagram.publisher.limits.max_size_bytes == 300 * 1024**2


def test_validate_ok():
    assert instagram.publisher.validate(ctx(), request()) == []
    # Extreme but allowed aspect ratios and limits.
    assert (
        instagram.publisher.validate(ctx(), request(width=100, height=10000, duration_s=3, caption="")) == []
    )
    assert instagram.publisher.validate(ctx(), request(width=1000, height=100, duration_s=900)) == []


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"duration_s": 2.9}, "shorter"),
        ({"duration_s": 900.5}, "15 minutes"),
        ({"size_bytes": 300 * 1024**2 + 1}, "300 MB"),
        ({"size_bytes": 0}, "empty"),
        ({"width": 1100, "height": 100}, "aspect ratio"),
        ({"width": 10, "height": 1001}, "aspect ratio"),
        ({"width": 0, "height": 1920}, "dimensions are unknown"),
        ({"caption": "x" * 2201}, "2200"),
        ({"caption": " ".join(f"#tag{i}" for i in range(31))}, "31 hashtags"),
        ({"caption": " ".join(f"#وسم{i}" for i in range(31))}, "31 hashtags"),
        ({"caption": " ".join(f"@user{i}" for i in range(21))}, "21 @ tags"),
        ({"public_url": "http://localhost:8000/m/1/2/s.mp4"}, "public address"),
        ({"public_url": "ftp://rotator.test/x.mp4"}, "public address"),
    ],
)
def test_validate_problems(overrides, fragment):
    problems = instagram.publisher.validate(ctx(), request(**overrides))
    assert len(problems) == 1 and fragment in problems[0]


def test_validate_counts_only_real_hashtags_and_mentions():
    caption = "C# and a#b and email me@example.com " + " ".join(f"#t{i}" for i in range(30))
    assert instagram.publisher.validate(ctx(), request(caption=caption)) == []


def test_validate_missing_ig_id():
    account = AccountContext(
        account_id=1, platform=Platform.INSTAGRAM, external_id="", tokens=OAuthTokens(access_token=TOKEN)
    )
    assert any("reconnect" in p for p in instagram.publisher.validate(account, request()))


# --------------------------------------------------------------------------- publish


def test_publish_creates_container(mock):
    limit_route = mock.get(LIMIT_URL).mock(return_value=quota(3))
    media_route = mock.post(MEDIA_URL).respond(json={"id": CONTAINER})
    before = datetime.now(UTC)
    result = instagram.publisher.publish(ctx(), request())

    assert result.outcome == PublishOutcome.PROCESSING
    assert result.poll_after_seconds == 30
    assert result.state["container_id"] == CONTAINER
    assert result.state["ig_user_id"] == IG_ID
    assert datetime.fromisoformat(result.state["created_at"]) >= before - timedelta(seconds=1)
    assert result.state["caption_key"] == "watch this friend #magic #trick"
    assert dict(limit_route.calls.last.request.url.params)["fields"] == "quota_usage,config"
    body = form(media_route.calls.last.request)
    assert body == {
        "media_type": "REELS",
        "video_url": PUBLIC_URL,
        "caption": "Watch this @friend #magic #trick",
        "share_to_feed": "true",
        "appsecret_proof": proof(TOKEN),
    }


def test_publish_flags_and_empty_caption(mock):
    mock.get(LIMIT_URL).mock(return_value=quota(0))
    media_route = mock.post(MEDIA_URL).respond(json={"id": CONTAINER})
    instagram.publisher.publish(
        ctx(), request(caption="", settings={"share_to_feed": False, "is_ai_generated": True})
    )
    body = form(media_route.calls.last.request)
    assert body["share_to_feed"] == "false" and body["is_ai_generated"] == "true"
    assert "caption" not in body


def test_publish_quota_exhausted(mock):
    mock.get(LIMIT_URL).mock(return_value=quota(50, 50))
    media_route = mock.post(MEDIA_URL).respond(json={"id": CONTAINER})
    with pytest.raises(RateLimitedError) as exc:
        instagram.publisher.publish(ctx(), request())
    assert exc.value.code == "publishing_limit"
    assert exc.value.retry_after == meta.IG_PUBLISH_LIMIT_RETRY
    assert "50" in exc.value.message
    assert media_route.call_count == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {"data": [{"quota_usage": "x"}]},
        {},
        {"data": [{"quota_usage": 5, "config": {"quota_total": 0}}]},
    ],
)
def test_publish_unreadable_quota_does_not_block(mock, payload):
    mock.get(LIMIT_URL).respond(json=payload)
    mock.post(MEDIA_URL).respond(json={"id": CONTAINER})
    assert instagram.publisher.publish(ctx(), request()).state["container_id"] == CONTAINER


def test_publish_quota_as_strings(mock):
    mock.get(LIMIT_URL).respond(json={"data": [{"quota_usage": "100", "config": {"quota_total": "100"}}]})
    with pytest.raises(RateLimitedError):
        instagram.publisher.publish(ctx(), request())


def test_publish_invalid_request_makes_no_calls(mock):
    with pytest.raises(PermanentError) as exc:
        instagram.publisher.publish(ctx(), request(duration_s=1))
    assert exc.value.code == "invalid_video"
    assert mock.calls.call_count == 0


@pytest.mark.parametrize(
    ("response", "cls"),
    [
        (
            graph_error(400, 9004, 2207052, f"The media could not be fetched from this uri: {PUBLIC_URL}"),
            RetryableError,
        ),
        (graph_error(400, 352, 2207026), PermanentError),
        (graph_error(400, 190), AuthError),
        (graph_error(400, 25, 2207050), PermanentError),
        (httpx.Response(200, json={}), RetryableError),
        (httpx.Response(503, text="unavailable"), RetryableError),
    ],
)
def test_publish_container_errors(mock, response, cls):
    mock.get(LIMIT_URL).mock(return_value=quota(1))
    mock.post(MEDIA_URL).mock(return_value=response)
    with pytest.raises(cls) as exc:
        instagram.publisher.publish(ctx(), request())
    assert TOKEN not in exc.value.message and PUBLIC_URL not in exc.value.message


def test_publish_network_error(mock):
    mock.get(LIMIT_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(RetryableError):
        instagram.publisher.publish(ctx(), request())


# --------------------------------------------------------------------------- poll


def container(status_code: str, status: str | None = None) -> httpx.Response:
    body = {"id": CONTAINER, "status_code": status_code}
    if status is not None:
        body["status"] = status
    return httpx.Response(200, json=body)


def test_poll_in_progress(mock):
    route = mock.get(CONTAINER_URL).mock(return_value=container("IN_PROGRESS"))
    result = instagram.publisher.poll(ctx(), state())
    assert result.outcome == PublishOutcome.PROCESSING
    assert result.poll_after_seconds == 60
    assert result.state == state()
    assert dict(route.calls.last.request.url.params)["fields"] == "status_code,status"


def test_poll_unknown_status_keeps_processing(mock):
    mock.get(CONTAINER_URL).mock(return_value=container("SOMETHING_NEW"))
    result = instagram.publisher.poll(ctx(), state())
    assert result.outcome == PublishOutcome.PROCESSING and "SOMETHING_NEW" in result.message


def test_poll_finished_publishes_and_saves_media_id_first(mock):
    mock.get(CONTAINER_URL).mock(return_value=container("FINISHED"))
    publish_route = mock.post(PUBLISH_URL).respond(json={"id": MEDIA_ID})
    permalink_route = mock.get(MEDIA_NODE_URL).respond(
        json={"id": MEDIA_ID, "permalink": "https://www.instagram.com/reel/abc/"}
    )
    current = state()
    result = instagram.publisher.poll(ctx(), current)

    assert result.outcome == PublishOutcome.PUBLISHED
    assert result.platform_post_id == MEDIA_ID
    assert result.permalink == "https://www.instagram.com/reel/abc/"
    assert result.state["media_id"] == MEDIA_ID
    assert current["media_id"] == MEDIA_ID  # stored in the engine's dict before the permalink call
    assert form(publish_route.calls.last.request)["creation_id"] == CONTAINER
    assert dict(permalink_route.calls.last.request.url.params)["fields"] == "permalink"


def test_poll_permalink_failure_still_published(mock):
    mock.get(CONTAINER_URL).mock(return_value=container("FINISHED"))
    mock.post(PUBLISH_URL).respond(json={"id": MEDIA_ID})
    mock.get(MEDIA_NODE_URL).mock(return_value=httpx.Response(500, text="oops"))
    result = instagram.publisher.poll(ctx(), state())
    assert result.outcome == PublishOutcome.PUBLISHED
    assert result.platform_post_id == MEDIA_ID and result.permalink is None


def test_poll_with_saved_media_id_never_publishes_again(mock):
    status_route = mock.get(CONTAINER_URL).mock(return_value=container("FINISHED"))
    publish_route = mock.post(PUBLISH_URL).respond(json={"id": "other"})
    mock.get(MEDIA_NODE_URL).respond(json={"permalink": "https://www.instagram.com/reel/abc/"})
    result = instagram.publisher.poll(ctx(), state(media_id=MEDIA_ID))
    assert result.outcome == PublishOutcome.PUBLISHED and result.platform_post_id == MEDIA_ID
    assert status_route.call_count == 0 and publish_route.call_count == 0


@pytest.mark.parametrize(
    "publish_response",
    [
        httpx.ConnectError("reset"),
        httpx.Response(502, text="bad gateway"),
        graph_error(400, 9007, 2207027, "The media is not ready for publishing"),
        graph_error(400, 24, 2207008),
        httpx.Response(200, json={}),
    ],
)
def test_poll_unconfirmed_publish_keeps_processing(mock, publish_response):
    mock.get(CONTAINER_URL).mock(return_value=container("FINISHED"))
    route = mock.post(PUBLISH_URL)
    if isinstance(publish_response, Exception):
        route.mock(side_effect=publish_response)
    else:
        route.mock(return_value=publish_response)
    current = state()
    result = instagram.publisher.poll(ctx(), current)
    assert result.outcome == PublishOutcome.PROCESSING
    assert "media_id" not in current and result.state["container_id"] == CONTAINER


@pytest.mark.parametrize(
    ("publish_response", "cls"),
    [
        (graph_error(400, 9, 2207042), RateLimitedError),
        (graph_error(400, 190, 463), AuthError),
        (graph_error(400, 4, 2207051), PermanentError),
        (graph_error(400, 25, 2207050), PermanentError),
    ],
)
def test_poll_publish_rejections(mock, publish_response, cls):
    mock.get(CONTAINER_URL).mock(return_value=container("FINISHED"))
    mock.post(PUBLISH_URL).mock(return_value=publish_response)
    with pytest.raises(cls) as exc:
        instagram.publisher.poll(ctx(), state())
    assert type(exc.value) is cls


def test_poll_repeated_refusals_start_over(mock):
    mock.get(CONTAINER_URL).mock(return_value=container("FINISHED"))
    publish_route = mock.post(PUBLISH_URL).mock(return_value=graph_error(400, 24, 2207008))
    current = state()
    for _ in range(instagram.MAX_PUBLISH_ATTEMPTS - 1):
        current = instagram.publisher.poll(ctx(), current).state
    assert current["publish_attempts"] == instagram.MAX_PUBLISH_ATTEMPTS - 1
    with pytest.raises(RestartPublish) as exc:
        instagram.publisher.poll(ctx(), current)
    assert exc.value.code == "publish_refused"
    assert "container_id" not in current and "publish_attempts" not in current
    assert publish_route.call_count == instagram.MAX_PUBLISH_ATTEMPTS


def test_poll_ambiguous_publish_failures_never_start_over(mock):
    mock.get(CONTAINER_URL).mock(return_value=container("FINISHED"))
    mock.post(PUBLISH_URL).mock(side_effect=httpx.ReadTimeout("lost"))
    current = state()
    for _ in range(instagram.MAX_PUBLISH_ATTEMPTS + 2):
        result = instagram.publisher.poll(ctx(), current)
        assert result.outcome == PublishOutcome.PROCESSING
        current = result.state
    assert current["container_id"] == CONTAINER


def recent_media(*items: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"data": list(items)})


def test_poll_published_recovers_media_id(mock):
    mock.get(CONTAINER_URL).mock(return_value=container("PUBLISHED"))
    route = mock.get(MEDIA_URL).mock(
        return_value=recent_media(
            {"id": "newer-other", "caption": "Different", "timestamp": "2026-10-14T10:09:00+0000"},
            {
                "id": "ours-late",
                "caption": "Watch this @friend #magic #trick",
                "timestamp": "2026-10-14T10:08:00+0000",
                "permalink": "https://www.instagram.com/reel/late/",
            },
            {
                "id": "ours",
                # Instagram drops '@' for apps without admin-equivalent tasks.
                "caption": "Watch this friend  #magic #trick",
                "timestamp": "2026-10-14T10:02:00+0000",
                "permalink": "https://www.instagram.com/reel/ours/",
            },
            {
                "id": "old-copy",
                "caption": "Watch this @friend #magic #trick",
                "timestamp": "2026-10-13T10:00:00+0000",
            },
        )
    )
    publish_route = mock.post(PUBLISH_URL).respond(json={"id": "dup"})
    current = state()
    result = instagram.publisher.poll(ctx(), current)
    assert result.outcome == PublishOutcome.PUBLISHED
    assert result.platform_post_id == "ours"
    assert result.permalink == "https://www.instagram.com/reel/ours/"
    assert current["media_id"] == "ours"
    assert publish_route.call_count == 0
    params = dict(route.calls.last.request.url.params)
    assert params["limit"] == "5" and "timestamp" in params["fields"] and "caption" in params["fields"]


def test_poll_published_recovery_without_match(mock):
    mock.get(CONTAINER_URL).mock(return_value=container("PUBLISHED"))
    mock.get(MEDIA_URL).mock(
        return_value=recent_media({"id": "x", "caption": "other", "timestamp": "2026-10-14T10:05:00+0000"})
    )
    result = instagram.publisher.poll(ctx(), state())
    assert result.outcome == PublishOutcome.PUBLISHED
    assert result.platform_post_id is None and result.permalink is None


def test_poll_published_recovery_lookup_failure(mock):
    mock.get(CONTAINER_URL).mock(return_value=container("PUBLISHED"))
    mock.get(MEDIA_URL).mock(return_value=httpx.Response(500, text="err"))
    result = instagram.publisher.poll(ctx(), state())
    assert result.outcome == PublishOutcome.PUBLISHED and result.platform_post_id is None


def test_poll_published_recovery_without_caption_key(mock):
    mock.get(CONTAINER_URL).mock(return_value=container("PUBLISHED"))
    mock.get(MEDIA_URL).mock(
        return_value=recent_media(
            {"id": "ours", "caption": "anything", "timestamp": "2026-10-14T10:01:00+0000"}
        )
    )
    current = state()
    current.pop("caption_key")
    assert instagram.publisher.poll(ctx(), current).platform_post_id == "ours"


@pytest.mark.parametrize(
    ("status", "fragment", "code"),
    [
        ("2207026", "does not support this video format", "2207026"),
        (
            "Error: Media upload has failed with error code 2207026",
            "does not support this video format",
            "2207026",
        ),
        ("Error: something odd happened", "something odd happened", "container_error"),
        (None, "no reason given", "container_error"),
    ],
)
def test_poll_error_is_permanent(mock, status, fragment, code):
    mock.get(CONTAINER_URL).mock(return_value=container("ERROR", status))
    with pytest.raises(PermanentError) as exc:
        instagram.publisher.poll(ctx(), state())
    assert fragment in exc.value.message and exc.value.code == code


def test_poll_retryable_error_resets_container(mock):
    mock.get(CONTAINER_URL).mock(return_value=container("ERROR", "Error 2207053"))
    current = state()
    with pytest.raises(RestartPublish) as exc:
        instagram.publisher.poll(ctx(), current)
    assert exc.value.code == "2207053"
    assert "container_id" not in current


def test_poll_expired_resets_state(mock):
    mock.get(CONTAINER_URL).mock(return_value=container("EXPIRED"))
    current = state()
    with pytest.raises(RestartPublish) as exc:
        instagram.publisher.poll(ctx(), current)
    assert exc.value.code == "container_expired"
    assert not isinstance(exc.value, RateLimitedError)
    assert "container_id" not in current and "created_at" not in current


def test_poll_missing_container():
    with pytest.raises(PermanentError) as exc:
        instagram.publisher.poll(ctx(), {})
    assert exc.value.code == "missing_container"


@pytest.mark.parametrize(
    ("response", "cls"),
    [
        (graph_error(400, 190), AuthError),
        (graph_error(400, 4), RateLimitedError),
        (httpx.Response(500, text="x"), RetryableError),
    ],
)
def test_poll_status_errors(mock, response, cls):
    mock.get(CONTAINER_URL).mock(return_value=response)
    with pytest.raises(cls):
        instagram.publisher.poll(ctx(), state())


def test_full_lifecycle(mock):
    mock.get(LIMIT_URL).mock(return_value=quota(1))
    mock.post(MEDIA_URL).respond(json={"id": CONTAINER})
    mock.get(CONTAINER_URL).mock(side_effect=[container("IN_PROGRESS"), container("FINISHED")])
    mock.post(PUBLISH_URL).respond(json={"id": MEDIA_ID})
    mock.get(MEDIA_NODE_URL).respond(json={"permalink": "https://www.instagram.com/reel/z/"})
    first = instagram.publisher.publish(ctx(), request())
    second = instagram.publisher.poll(ctx(), first.state)
    assert second.outcome == PublishOutcome.PROCESSING
    third = instagram.publisher.poll(ctx(), second.state)
    assert third.outcome == PublishOutcome.PUBLISHED and third.platform_post_id == MEDIA_ID


# --------------------------------------------------------------------------- lister


def media_item(media_id: str, timestamp: str, media_type: str = "VIDEO", **extra: Any) -> dict[str, Any]:
    item = {
        "id": media_id,
        "media_type": media_type,
        "media_product_type": "REELS",
        "media_url": f"https://scontent.test/{media_id}.mp4",
        "thumbnail_url": f"https://scontent.test/{media_id}.jpg",
        "permalink": f"https://www.instagram.com/reel/{media_id}/",
        "timestamp": timestamp,
        "caption": f"caption {media_id}",
    }
    item.update(extra)
    return {k: v for k, v in item.items() if v is not None}


def test_list_recent_filters_videos_paginates_and_stops_at_since(mock):
    since = datetime(2026, 10, 1, tzinfo=UTC)
    pages = [
        {
            "data": [
                media_item("v3", "2026-10-13T09:00:00+0000"),
                media_item("img", "2026-10-12T09:00:00+0000", media_type="IMAGE"),
                media_item("album", "2026-10-11T09:00:00+0000", media_type="CAROUSEL_ALBUM"),
                media_item("v2", "2026-10-10T09:00:00+0000", media_url=None, media_product_type="FEED"),
            ],
            "paging": {"cursors": {"after": "AFTER1"}, "next": "https://graph.facebook.com/next"},
        },
        {
            "data": [
                media_item("v1", "2026-10-02T09:00:00+0000"),
                media_item("old", "2026-09-20T09:00:00+0000"),
                media_item("older", "2026-09-10T09:00:00+0000"),
            ],
            "paging": {"cursors": {"after": "AFTER2"}, "next": "https://graph.facebook.com/next2"},
        },
    ]
    route = mock.get(MEDIA_URL).mock(side_effect=[httpx.Response(200, json=p) for p in pages])
    videos = instagram.lister.list_recent(ctx(), since=since, limit=10)

    assert [v.external_id for v in videos] == ["v3", "v2", "v1"]
    assert route.call_count == 2
    first, second = (dict(c.request.url.params) for c in route.calls)
    assert first["fields"] == instagram.MEDIA_FIELDS
    assert first["since"] == str(int(since.timestamp()))
    assert "after" not in first and second["after"] == "AFTER1"
    v3, v2, _ = videos
    assert v3.download_url == "https://scontent.test/v3.mp4"
    assert v3.permalink == "https://www.instagram.com/reel/v3/"
    assert v3.caption == "caption v3"
    assert v3.created_at == datetime(2026, 10, 13, 9, tzinfo=UTC)
    assert v3.extra["media_product_type"] == "REELS" and "media_url_missing" not in v3.extra
    assert v2.download_url is None and v2.extra["media_url_missing"] is True


def test_list_recent_respects_limit_and_skips_stories(mock):
    mock.get(MEDIA_URL).respond(
        json={
            "data": [
                media_item("story", "2026-10-13T09:00:00+0000", media_product_type="STORY"),
                media_item("ad", "2026-10-13T08:00:00+0000", media_product_type="AD"),
                media_item("a", "2026-10-12T09:00:00+0000"),
                media_item("b", "2026-10-11T09:00:00+0000"),
            ]
        }
    )
    videos = instagram.lister.list_recent(ctx(), limit=1)
    assert [v.external_id for v in videos] == ["a"]
    assert instagram.lister.list_recent(ctx(), limit=0) == []


def test_list_recent_auth_error(mock):
    mock.get(MEDIA_URL).mock(return_value=graph_error(400, 190))
    with pytest.raises(AuthError):
        instagram.lister.list_recent(ctx())


def remote(media_id: str = MEDIA_ID) -> RemoteVideo:
    return RemoteVideo(external_id=media_id, download_url="https://scontent.test/stale.mp4")


def test_download_refetches_media_url(mock, tmp_path):
    node_route = mock.get(MEDIA_NODE_URL).respond(json={"id": MEDIA_ID, "media_url": CDN_URL})
    cdn_route = mock.get(CDN_URL).respond(content=b"video-bytes")
    stale = mock.get("https://scontent.test/stale.mp4").respond(status_code=403)
    dest = tmp_path / "orig.mp4"
    assert instagram.lister.download(ctx(), remote(), dest) == dest
    assert dest.read_bytes() == b"video-bytes"
    assert dict(node_route.calls.last.request.url.params)["fields"] == "media_url"
    assert "authorization" not in cdn_route.calls.last.request.headers
    assert stale.call_count == 0


def test_download_without_media_url(mock, tmp_path):
    mock.get(MEDIA_NODE_URL).respond(json={"id": MEDIA_ID})
    with pytest.raises(PermanentError) as exc:
        instagram.lister.download(ctx(), remote(), tmp_path / "x.mp4")
    assert exc.value.code == "media_url_unavailable"
    assert "upload the original" in exc.value.message.lower()
    assert not (tmp_path / "x.mp4").exists()


def test_download_rejects_non_https(mock, tmp_path):
    mock.get(MEDIA_NODE_URL).respond(json={"media_url": "http://insecure.test/v.mp4"})
    with pytest.raises(PermanentError):
        instagram.lister.download(ctx(), remote(), tmp_path / "x.mp4")


@pytest.mark.parametrize(("status", "cls"), [(403, PermanentError), (503, RetryableError)])
def test_download_cdn_failures(mock, tmp_path, status, cls):
    mock.get(MEDIA_NODE_URL).respond(json={"media_url": CDN_URL})
    mock.get(CDN_URL).respond(status_code=status)
    with pytest.raises(cls):
        instagram.lister.download(ctx(), remote(), tmp_path / "x.mp4")
    assert not (tmp_path / "x.mp4").exists()
