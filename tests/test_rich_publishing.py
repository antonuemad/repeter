"""Official API-shaped HTTP mocks only. No real accounts, uploads or publications."""

from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from repeater.config import get_settings
from repeater.models import Platform
from repeater.platforms import facebook, instagram
from repeater.platforms.base import (
    AccountContext,
    OAuthTokens,
    PermanentError,
    PublishOutcome,
    PublishRequest,
    RateLimitedError,
    UnknownOutcomeError,
)

GRAPH = "https://graph.facebook.com/v26.0"


@pytest.fixture(autouse=True)
def mock_http(monkeypatch):
    monkeypatch.setattr(facebook, "_sleep", lambda _: None)
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def req(tmp_path):
    path = tmp_path / "synthetic.jpg"
    path.write_bytes(b"\xff\xd8\xffsynthetic-image")
    return PublishRequest(
        post_id=1,
        video_path=path,
        public_url="https://repeater.test/m/1/9999999999/fake.jpg",
        size_bytes=path.stat().st_size,
        duration_s=10,
        width=1080,
        height=1080,
        title="Synthetic title",
        caption="Synthetic caption",
        settings={},
        fps=30,
        media_kind="image",
        content_format="post",
    )


def account(platform="facebook", **extra):
    return AccountContext(
        1,
        Platform(platform),
        "123",
        OAuthTokens("synthetic-token"),
        extra={"account_type": "BUSINESS", **extra},
    )


def data(call):
    return {key: value[0] for key, value in parse_qs(call.request.content.decode()).items()}


@pytest.mark.parametrize("kind,placement", [("image", "post"), ("image", "story"), ("video", "story")])
def test_instagram_create_process_publish_and_recover(req, mock_http, kind, placement):
    req = replace(
        req, media_kind=kind, content_format=placement, height=1920 if placement == "story" else 1080
    )
    mock_http.get(f"{GRAPH}/123/content_publishing_limit").respond(
        200, json={"data": [{"quota_usage": 0, "config": {"quota_total": 50}}]}
    )
    create = mock_http.post(f"{GRAPH}/123/media").respond(200, json={"id": "container"})
    status = mock_http.get(f"{GRAPH}/container").respond(200, json={"status_code": "IN_PROGRESS"})
    publish = mock_http.post(f"{GRAPH}/123/media_publish").respond(200, json={"id": "posted"})
    mock_http.get(f"{GRAPH}/posted").respond(200, json={"permalink": "https://instagram.test/synthetic"})
    result = instagram.publisher.publish(account("instagram"), req)
    assert result.outcome == PublishOutcome.PROCESSING
    sent = data(create.calls[0])
    assert sent["image_url" if kind == "image" else "video_url"] == req.public_url
    if placement == "story":
        assert sent["media_type"] == "STORIES" and "caption" not in sent and "share_to_feed" not in sent
    else:
        assert sent["caption"] == req.caption and "media_type" not in sent
    waiting = instagram.publisher.poll(account("instagram"), result.state)
    assert waiting.outcome == PublishOutcome.PROCESSING and not publish.called
    status.respond(200, json={"status_code": "FINISHED"})
    published = instagram.publisher.poll(account("instagram"), waiting.state)
    assert published.outcome == PublishOutcome.PUBLISHED and published.platform_post_id == "posted"
    instagram.publisher.poll(account("instagram"), published.state)
    assert create.call_count == 1 and publish.call_count == 1


def test_instagram_story_lost_publish_response_never_creates_second_container(req, mock_http):
    mock_http.get(f"{GRAPH}/123/content_publishing_limit").respond(200, json={"data": []})
    create = mock_http.post(f"{GRAPH}/123/media").respond(200, json={"id": "container"})
    status = mock_http.get(f"{GRAPH}/container").respond(200, json={"status_code": "FINISHED"})
    finish = mock_http.post(f"{GRAPH}/123/media_publish").mock(side_effect=httpx.ReadTimeout("simulated"))
    result = instagram.publisher.publish(
        account("instagram"), replace(req, content_format="story", height=1920)
    )
    result = instagram.publisher.poll(account("instagram"), result.state)
    assert result.state["publish_unknown"] == 1
    status.respond(200, json={"status_code": "PUBLISHED"})
    mock_http.get(f"{GRAPH}/123/stories").respond(200, json={"data": []})
    assert instagram.publisher.poll(account("instagram"), result.state).outcome == PublishOutcome.PUBLISHED
    assert create.call_count == 1 and finish.call_count == 1


@pytest.mark.parametrize(
    "kind,placement,edge",
    [("text", "post", "feed"), ("image", "post", "photos"), ("image", "story", "photo_stories")],
)
def test_facebook_text_image_and_photo_story(req, mock_http, kind, placement, edge):
    req = replace(
        req, media_kind=kind, content_format=placement, height=1920 if placement == "story" else 1080
    )
    upload = mock_http.post(f"{GRAPH}/123/photos").respond(200, json={"id": "unpublished-photo"})
    final = mock_http.post(f"{GRAPH}/123/{edge}").respond(200, json={"id": "posted", "success": True})
    result = facebook.publisher.publish(account(), req)
    assert result.outcome == PublishOutcome.PUBLISHED and result.platform_post_id == "posted"
    sent = data(final.calls[0])
    if kind == "text":
        assert sent["message"] == req.caption and "url" not in sent
    elif placement == "post":
        assert sent["url"] == req.public_url and sent["published"] == "true"
    else:
        assert data(upload.calls[0])["published"] == "false"
        assert sent["photo_id"] == "unpublished-photo" and "caption" not in sent


@pytest.mark.parametrize(
    "kind,placement,edge",
    [("text", "post", "feed"), ("image", "post", "photos"), ("image", "story", "photo_stories")],
)
@pytest.mark.parametrize("failure", ["timeout", "server", "missing_id"])
def test_facebook_ambiguous_direct_write_is_not_retryable(req, mock_http, kind, placement, edge, failure):
    req = replace(
        req, media_kind=kind, content_format=placement, height=1920 if placement == "story" else 1080
    )
    if placement == "story":
        mock_http.post(f"{GRAPH}/123/photos").respond(200, json={"id": "photo"})
    call = mock_http.post(f"{GRAPH}/123/{edge}")
    if failure == "timeout":
        call.mock(side_effect=httpx.ReadTimeout("simulated"))
    elif failure == "server":
        call.respond(503, json={"error": {"message": "simulated", "code": 2}})
    else:
        call.respond(200, json={})
    with pytest.raises(UnknownOutcomeError) as exc:
        facebook.publisher.publish(account(), req)
    assert not exc.value.state and call.call_count == 1


def test_definitive_rate_limit_is_safe_to_retry(req, mock_http):
    call = mock_http.post(f"{GRAPH}/123/feed").respond(
        429, json={"error": {"code": 4, "message": "synthetic quota"}}
    )
    with pytest.raises(RateLimitedError):
        facebook.publisher.publish(account(), replace(req, media_kind="text"))
    assert call.call_count == 1


@pytest.mark.parametrize("lost", [False, True])
def test_facebook_video_story_finish_and_read_only_resolution(req, mock_http, lost):
    req = replace(req, media_kind="video", content_format="story", height=1920)
    upload_url = "https://rupload.facebook.com/video-upload/v26.0/777"

    def endpoint(request):
        if parse_qs(request.content.decode()).get("upload_phase") == ["start"]:
            return httpx.Response(200, json={"video_id": "777", "upload_url": upload_url})
        if lost:
            raise httpx.ReadTimeout("simulated lost finish")
        return httpx.Response(200, json={"success": True})

    edge = mock_http.post(f"{GRAPH}/123/video_stories").mock(side_effect=endpoint)
    upload = mock_http.post(upload_url).respond(200, json={"success": True})
    status = mock_http.get(f"{GRAPH}/777").respond(
        200,
        json={
            "status": {
                "video_status": "ready",
                "publishing_phase": {"status": "complete", "publish_status": "published"},
            }
        },
    )
    if lost:
        with pytest.raises(UnknownOutcomeError) as exc:
            facebook.publisher.publish(account(), req)
        state = exc.value.state
    else:
        state = facebook.publisher.publish(account(), req).state
    result = facebook.publisher.poll(account(), state)
    assert result.outcome == PublishOutcome.PUBLISHED and result.platform_post_id == "777"
    assert edge.call_count == 2 and upload.call_count == 1 and status.call_count == 1
    assert upload.calls[0].request.content == req.video_path.read_bytes()
    assert "description" not in data(edge.calls[-1])  # Story notes are never posted as overlays/captions.


def test_story_upload_rejects_credential_exfiltration_url(req, mock_http):
    mock_http.post(f"{GRAPH}/123/video_stories").respond(
        200, json={"video_id": "777", "upload_url": "https://attacker.test/777"}
    )
    with pytest.raises(PermanentError, match="unexpected"):
        facebook.publisher.publish(
            account(), replace(req, media_kind="video", content_format="story", height=1920)
        )
    assert len(mock_http.calls) == 1


@pytest.mark.parametrize("platform", ["facebook", "instagram"])
def test_adapter_entry_points_respect_global_lock(req, mock_http, monkeypatch, platform):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    adapter = facebook.publisher if platform == "facebook" else instagram.publisher
    with pytest.raises(PermanentError, match="disabled"):
        adapter.publish(account(platform), req)
    with pytest.raises(PermanentError, match="disabled"):
        adapter.poll(account(platform), {"video_id": "fake", "container_id": "fake"})
    assert not mock_http.calls


@pytest.mark.parametrize(
    "change",
    [{"media_kind": "text"}, {"content_format": "story", "height": 1080}, {"size_bytes": 9 * 1024**2}],
)
def test_invalid_instagram_formats_do_not_make_requests(req, mock_http, change):
    with pytest.raises(PermanentError):
        instagram.publisher.publish(account("instagram"), replace(req, **change))
    assert not mock_http.calls


def test_instagram_creator_cannot_publish_stories(req, mock_http):
    with pytest.raises(PermanentError):
        instagram.publisher.publish(
            account("instagram", account_type="MEDIA_CREATOR"),
            replace(req, content_format="story", height=1920),
        )
    assert not mock_http.calls
