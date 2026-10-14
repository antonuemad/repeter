"""Facebook Page Reels publisher and Page video lister."""

from __future__ import annotations

import hashlib
import hmac
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from repeater.models import Platform
from repeater.platforms import facebook, get_lister, get_publisher
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
    UnknownOutcomeError,
)
from repeater.timeutil import utcnow
from tests import test_platform_meta as meta_tests

GRAPH = "https://graph.facebook.com/v26.0"
TOKEN = "fb-page-token-SECRET"
PAGE_ID = "111"
VIDEO_ID = "990001"
REELS_URL = f"{GRAPH}/{PAGE_ID}/video_reels"
VIDEOS_URL = f"{GRAPH}/{PAGE_ID}/videos"
VIDEO_URL = f"{GRAPH}/{VIDEO_ID}"
RUPLOAD = f"https://rupload.facebook.com/video-upload/v26.0/{VIDEO_ID}"
CDN_URL = "https://video.xx.fbcdn.test/v/raw.mp4?oh=1"


@pytest.fixture
def mock() -> Any:
    with respx.mock(assert_all_called=False) as m:
        yield m
        assert_signed(m)


@pytest.fixture
def engine_post(db, workspace, make_account, sample_video, monkeypatch):
    return meta_tests.build_engine_post(db, workspace, make_account, sample_video, monkeypatch)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(facebook, "_sleep", sleeps.append)
    return sleeps


@pytest.fixture(scope="session")
def low_fps_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("fb") / "low_fps.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=540x960:rate=15:duration=3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        check=True,
    )
    return out


def proof(token: str) -> str:
    return hmac.new(b"m-secret", token.encode(), hashlib.sha256).hexdigest()


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def assert_signed(mock: Any) -> None:
    for call in mock.calls:
        request = call.request
        assert TOKEN not in str(request.url)
        host = request.url.host
        if host == "rupload.facebook.com":
            assert request.headers["authorization"] == f"OAuth {TOKEN}"
            continue
        if host != "graph.facebook.com":
            assert "authorization" not in request.headers
            continue
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        params = dict(request.url.params) if request.method == "GET" else form(request)
        assert params["appsecret_proof"] == proof(TOKEN)


def ctx(**extra: Any) -> AccountContext:
    return AccountContext(
        account_id=7,
        platform=Platform.FACEBOOK,
        external_id=PAGE_ID,
        tokens=OAuthTokens(access_token=TOKEN),
        extra={"page_id": PAGE_ID, "fb_user_id": "u1", **extra},
    )


def request(video: Path, **overrides: Any) -> PublishRequest:
    values: dict[str, Any] = {
        "post_id": 9,
        "video_path": video,
        "public_url": "https://rotator.test/m/1/2/s.mp4",
        "size_bytes": video.stat().st_size if video.exists() else 1000,
        "duration_s": 30.0,
        "width": 1080,
        "height": 1920,
        "title": "Magic trick",
        "caption": "Watch this #magic",
        "settings": {"is_ai_generated": False},
    }
    values.update(overrides)
    return PublishRequest(**values)


def graph_error(status: int, code: int, subcode: int | None = None, message: str = "error") -> httpx.Response:
    error: dict[str, Any] = {"message": message, "type": "OAuthException", "code": code, "fbtrace_id": "T"}
    if subcode is not None:
        error["error_subcode"] = subcode
    return httpx.Response(status, json={"error": error})


def reels_endpoint(finish: Any = None) -> Any:
    """video_reels POST handler: start returns the video id; finish uses `finish` (default success)."""
    responses = list(finish) if isinstance(finish, list) else None

    def handler(request: httpx.Request) -> httpx.Response:
        body = form(request)
        if body["upload_phase"] == "start":
            return httpx.Response(
                200,
                json={
                    "video_id": VIDEO_ID,
                    "upload_url": f"https://rupload.facebook.com/video-upload/{VIDEO_ID}",
                },
            )
        if responses:
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return httpx.Response(200, json={"success": True})

    return handler


def status_body(
    video_status: str = "processing",
    uploading: dict | None = None,
    processing: dict | None = None,
    publishing: dict | None = None,
) -> dict[str, Any]:
    return {
        "id": VIDEO_ID,
        "status": {
            "video_status": video_status,
            "uploading_phase": uploading or {"status": "complete"},
            "processing_phase": processing or {"status": "in_progress"},
            "publishing_phase": publishing or {"status": "not_started"},
        },
    }


# --------------------------------------------------------------------------- contract & validate


def test_contract_and_registry():
    assert isinstance(facebook.publisher, Publisher)
    assert isinstance(facebook.lister, MediaLister)
    assert get_publisher("facebook") is facebook.publisher
    assert get_lister("facebook") is facebook.lister


def test_probe_fps(sample_video, sample_video_landscape, tmp_path):
    assert facebook.probe_fps(sample_video) == pytest.approx(30.0)
    assert facebook.probe_fps(sample_video_landscape) == pytest.approx(25.0)
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")
    assert facebook.probe_fps(junk) is None
    assert facebook.probe_fps(tmp_path / "missing.mp4") is None


def test_probe_fps_without_ffprobe(monkeypatch, sample_video):
    from repeater.config import get_settings

    monkeypatch.setattr(get_settings(), "ffprobe_bin", "/nonexistent/ffprobe")
    assert facebook.probe_fps(sample_video) is None


def test_validate_ok(sample_video):
    assert facebook.publisher.validate(ctx(), request(sample_video)) == []
    landscape = request(sample_video, width=1920, height=1080, duration_s=3, caption="")
    assert facebook.publisher.validate(ctx(), landscape) == []
    assert (
        facebook.publisher.validate(ctx(), request(sample_video, width=540, height=960, duration_s=90)) == []
    )


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"duration_s": 2.5}, "shorter"),
        ({"duration_s": 91}, "90 seconds"),
        ({"size_bytes": 0}, "empty"),
        ({"size_bytes": 1024**3 + 1}, "larger than"),
        ({"width": 480, "height": 854}, "at least 540x960"),
        ({"width": 1080, "height": 2700}, "between 16:9 and 9:16"),
        ({"width": 2700, "height": 1080}, "between 16:9 and 9:16"),
        ({"width": 0, "height": 0}, "dimensions are unknown"),
        ({"caption": "x" * 63207}, "description is longer"),
    ],
)
def test_validate_problems(sample_video, overrides, fragment):
    problems = facebook.publisher.validate(ctx(), request(sample_video, **overrides))
    assert len(problems) == 1 and fragment in problems[0], problems


def test_validate_frame_rate(low_fps_video):
    req = request(low_fps_video, width=540, height=960, duration_s=3)
    problems = facebook.publisher.validate(ctx(), req)
    assert len(problems) == 1 and "15.00 fps" in problems[0]


def test_validate_unknown_fps_is_not_a_problem(tmp_path):
    missing = tmp_path / "missing.mp4"
    assert facebook.publisher.validate(ctx(), request(missing)) == []


# --------------------------------------------------------------------------- publish


def test_publish_start_upload_finish(mock, sample_video):
    reels = mock.post(REELS_URL).mock(side_effect=reels_endpoint())
    upload = mock.post(RUPLOAD).respond(json={"success": True})
    long_title = "T" * 300
    result = facebook.publisher.publish(ctx(), request(sample_video, title=f"  {long_title}  "))

    assert result.outcome == PublishOutcome.PROCESSING
    assert result.state == {"video_id": VIDEO_ID, "page_id": PAGE_ID}
    assert result.platform_post_id == VIDEO_ID
    assert result.poll_after_seconds == 30
    start, finish = (form(c.request) for c in reels.calls)
    assert start == {"upload_phase": "start", "appsecret_proof": proof(TOKEN)}
    assert finish == {
        "upload_phase": "finish",
        "video_id": VIDEO_ID,
        "video_state": "PUBLISHED",
        "description": "Watch this #magic",
        "title": "T" * 255,
        "appsecret_proof": proof(TOKEN),
    }
    up = upload.calls.last.request
    size = sample_video.stat().st_size
    assert up.headers["authorization"] == f"OAuth {TOKEN}"
    assert up.headers["offset"] == "0"
    assert up.headers["file_size"] == str(size)
    assert up.headers["content-type"] == "application/octet-stream"
    assert up.headers["content-length"] == str(size)
    assert up.content == sample_video.read_bytes()


def test_publish_ai_flag_post_id_and_no_caption(mock, sample_video):
    reels = mock.post(REELS_URL).mock(
        side_effect=reels_endpoint([httpx.Response(200, json={"success": True, "post_id": "111_222"})])
    )
    mock.post(RUPLOAD).respond(json={"success": True})
    result = facebook.publisher.publish(
        ctx(), request(sample_video, caption="", title="", settings={"is_ai_generated": True})
    )
    finish = form(reels.calls.last.request)
    assert finish["is_ai_generated"] == "true"
    assert "description" not in finish and "title" not in finish
    assert result.state["post_id"] == "111_222"


def test_publish_resumes_interrupted_upload(mock, sample_video, no_sleep):
    mock.post(REELS_URL).mock(side_effect=reels_endpoint())
    upload = mock.post(RUPLOAD).mock(
        side_effect=[httpx.WriteTimeout("cut"), httpx.Response(200, json={"success": True})]
    )
    status = mock.get(VIDEO_URL).respond(
        json=status_body("uploading", uploading={"status": "in_progress", "bytes_transfered": 50002})
    )
    facebook.publisher.publish(ctx(), request(sample_video))
    data = sample_video.read_bytes()
    second = upload.calls[1].request
    assert second.headers["offset"] == "50002"
    assert second.headers["file_size"] == str(len(data))
    assert second.headers["content-length"] == str(len(data) - 50002)
    assert second.content == data[50002:]
    assert dict(status.calls.last.request.url.params)["fields"] == "status"
    assert no_sleep == [facebook.RETRY_BACKOFF_SECONDS]


def test_publish_upload_already_complete_after_error(mock, sample_video):
    size = sample_video.stat().st_size
    reels = mock.post(REELS_URL).mock(side_effect=reels_endpoint())
    upload = mock.post(RUPLOAD).mock(side_effect=httpx.ReadTimeout("lost response"))
    mock.get(VIDEO_URL).respond(
        json=status_body("upload_complete", uploading={"status": "complete", "bytes_transfered": size})
    )
    facebook.publisher.publish(ctx(), request(sample_video))
    assert upload.call_count == 1 and reels.call_count == 2


def test_publish_upload_retries_exhausted(mock, sample_video):
    reels = mock.post(REELS_URL).mock(side_effect=reels_endpoint())
    upload = mock.post(RUPLOAD).respond(
        status_code=400,
        json={"debug_info": {"retriable": False, "type": "ProcessingFailedError", "message": "failed"}},
    )
    mock.get(VIDEO_URL).respond(
        json=status_body("uploading", uploading={"status": "in_progress", "bytes_transfered": 0})
    )
    with pytest.raises(RetryableError) as exc:
        facebook.publisher.publish(ctx(), request(sample_video))
    assert "ProcessingFailedError" in exc.value.message
    assert upload.call_count == facebook.UPLOAD_ATTEMPTS
    assert reels.call_count == 1  # never finished


@pytest.mark.parametrize(
    ("response", "cls"),
    [
        (
            httpx.Response(
                401, json={"debug_info": {"retriable": False, "type": "AuthError", "message": "bad"}}
            ),
            AuthError,
        ),
        (
            httpx.Response(
                400, json={"debug_info": {"retriable": False, "type": "FileTypeError", "message": "no"}}
            ),
            PermanentError,
        ),
        (graph_error(400, 190, 463), AuthError),
        (graph_error(400, 100, 1363024), PermanentError),
        (httpx.Response(429, text="slow"), RateLimitedError),
        (httpx.Response(404, text="gone"), PermanentError),
    ],
)
def test_publish_upload_rejections(mock, sample_video, response, cls):
    reels = mock.post(REELS_URL).mock(side_effect=reels_endpoint())
    upload = mock.post(RUPLOAD).mock(return_value=response)
    with pytest.raises(cls) as exc:
        facebook.publisher.publish(ctx(), request(sample_video))
    assert type(exc.value) is cls
    assert TOKEN not in exc.value.message
    assert upload.call_count == 1 and reels.call_count == 1


def test_publish_upload_unconfirmed_success_body_retries(mock, sample_video):
    mock.post(REELS_URL).mock(side_effect=reels_endpoint())
    upload = mock.post(RUPLOAD).mock(
        side_effect=[
            httpx.Response(200, json={"success": False}),
            httpx.Response(200, json={"success": True}),
        ]
    )
    mock.get(VIDEO_URL).respond(json=status_body("uploading", uploading={"status": "in_progress"}))
    facebook.publisher.publish(ctx(), request(sample_video))
    assert upload.call_count == 2
    assert upload.calls[1].request.headers["offset"] == "0"


def test_publish_finish_retried_on_same_video(mock, sample_video):
    reels = mock.post(REELS_URL).mock(
        side_effect=reels_endpoint([httpx.ConnectError("reset"), httpx.Response(200, json={"success": True})])
    )
    mock.post(RUPLOAD).respond(json={"success": True})
    mock.get(VIDEO_URL).respond(json=status_body("upload_complete"))
    result = facebook.publisher.publish(ctx(), request(sample_video))
    finishes = [form(c.request) for c in reels.calls if form(c.request)["upload_phase"] == "finish"]
    assert len(finishes) == 2 and {f["video_id"] for f in finishes} == {VIDEO_ID}
    assert result.outcome == PublishOutcome.PROCESSING


def test_publish_finish_lost_responses_but_publishing_started(mock, sample_video):
    mock.post(REELS_URL).mock(
        side_effect=reels_endpoint(
            [httpx.ReadTimeout("x"), httpx.Response(502, text="x"), httpx.ReadTimeout("x")]
        )
    )
    mock.post(RUPLOAD).respond(json={"success": True})
    mock.get(VIDEO_URL).respond(json=status_body("processing", publishing={"status": "in_progress"}))
    result = facebook.publisher.publish(ctx(), request(sample_video))
    assert result.outcome == PublishOutcome.PROCESSING


def test_publish_finish_failure_after_landing(mock, sample_video):
    mock.post(REELS_URL).mock(
        side_effect=reels_endpoint(
            [httpx.ReadTimeout("x"), graph_error(400, 100, None, "Video already published")]
        )
    )
    mock.post(RUPLOAD).respond(json={"success": True})
    mock.get(VIDEO_URL).respond(
        json=status_body("ready", publishing={"status": "complete", "publish_status": "published"})
    )
    assert facebook.publisher.publish(ctx(), request(sample_video)).outcome == PublishOutcome.PROCESSING


def test_publish_finish_never_confirmed_is_an_unknown_outcome(mock, sample_video):
    """api-meta-1: three lost finish responses must not become a RetryableError, which the engine
    answers with a brand-new upload (and so a second reel)."""
    reels = mock.post(REELS_URL).mock(
        side_effect=reels_endpoint([httpx.ReadTimeout("x"), httpx.ReadTimeout("x"), httpx.ReadTimeout("x")])
    )
    mock.post(RUPLOAD).respond(json={"success": True})
    mock.get(VIDEO_URL).respond(json=status_body("upload_complete"))
    with pytest.raises(UnknownOutcomeError) as exc:
        facebook.publisher.publish(ctx(), request(sample_video))
    assert reels.call_count == 1 + facebook.FINISH_ATTEMPTS  # one start, never a second
    state = exc.value.state
    assert state["video_id"] == VIDEO_ID and state["page_id"] == PAGE_ID
    assert state["finish_unconfirmed"] is True
    assert state["finish_options"] == {"description": "Watch this #magic", "title": "Magic trick"}
    assert datetime.fromisoformat(state["finish_sent_at"]).tzinfo is not None
    assert TOKEN not in exc.value.message and TOKEN not in str(state)


def test_publish_finish_lost_then_4xx_is_an_unknown_outcome(mock, sample_video):
    """The first finish landed but its answer was lost; the second is refused ("already finished")
    while the status still reads not started."""
    mock.post(REELS_URL).mock(
        side_effect=reels_endpoint([httpx.ReadTimeout("x"), graph_error(400, 100, None, "Invalid phase")])
    )
    mock.post(RUPLOAD).respond(json={"success": True})
    mock.get(VIDEO_URL).respond(json=status_body("processing"))
    with pytest.raises(UnknownOutcomeError) as exc:
        facebook.publisher.publish(ctx(), request(sample_video))
    assert exc.value.state["video_id"] == VIDEO_ID


def test_publish_finish_unknown_even_when_the_status_read_fails_too(mock, sample_video):
    mock.post(REELS_URL).mock(
        side_effect=reels_endpoint([httpx.Response(502, text="x")] * facebook.FINISH_ATTEMPTS)
    )
    mock.post(RUPLOAD).respond(json={"success": True})
    mock.get(VIDEO_URL).respond(status_code=500)  # the same incident breaks the status read
    with pytest.raises(UnknownOutcomeError) as exc:
        facebook.publisher.publish(ctx(), request(sample_video))
    assert exc.value.state["video_id"] == VIDEO_ID


def test_publish_finish_rate_limit_after_a_lost_attempt_stops_retrying(mock, sample_video):
    reels = mock.post(REELS_URL).mock(
        side_effect=reels_endpoint([httpx.ReadTimeout("x"), graph_error(400, 613, None, "rate limit")])
    )
    mock.post(RUPLOAD).respond(json={"success": True})
    mock.get(VIDEO_URL).respond(json=status_body("processing"))
    with pytest.raises(UnknownOutcomeError):  # not RateLimitedError: the engine would upload again
        facebook.publisher.publish(ctx(), request(sample_video))
    assert reels.call_count == 1 + 2


def test_publish_finish_rejected_every_time_is_still_retryable(mock, sample_video):
    """Facebook answered 4xx each time: nothing was finished, so a later fresh upload is safe."""
    reels = mock.post(REELS_URL).mock(
        side_effect=reels_endpoint(
            [graph_error(400, 6000, None, "upload problem")] * facebook.FINISH_ATTEMPTS
        )
    )
    mock.post(RUPLOAD).respond(json={"success": True})
    mock.get(VIDEO_URL).respond(json=status_body("upload_complete"))
    with pytest.raises(RetryableError) as exc:
        facebook.publisher.publish(ctx(), request(sample_video))
    assert not isinstance(exc.value, UnknownOutcomeError)
    assert reels.call_count == 1 + facebook.FINISH_ATTEMPTS


def test_publish_with_saved_video_never_uploads_again(mock, sample_video):
    saved = {"video_id": VIDEO_ID, "page_id": PAGE_ID, "finish_unconfirmed": True}
    result = facebook.publisher.publish(ctx(), request(sample_video, state=dict(saved)))
    assert result.outcome == PublishOutcome.PROCESSING and result.state == saved
    assert result.platform_post_id == VIDEO_ID
    assert mock.calls.call_count == 0


@pytest.mark.parametrize(
    ("finish", "cls"),
    [
        (graph_error(400, 613, None, "rate limit"), RateLimitedError),
        (graph_error(400, 100, 1363128), PermanentError),
        (graph_error(400, 190), AuthError),
        (httpx.Response(200, json={"success": False, "message": "Not allowed"}), PermanentError),
    ],
)
def test_publish_finish_rejections(mock, sample_video, finish, cls):
    reels = mock.post(REELS_URL).mock(side_effect=reels_endpoint([finish]))
    mock.post(RUPLOAD).respond(json={"success": True})
    with pytest.raises(cls) as exc:
        facebook.publisher.publish(ctx(), request(sample_video))
    assert type(exc.value) is cls
    assert reels.call_count == 2
    if cls is RateLimitedError:
        assert exc.value.retry_after == 3600


def test_publish_start_without_video_id(mock, sample_video):
    mock.post(REELS_URL).respond(json={"upload_url": "x"})
    with pytest.raises(RetryableError):
        facebook.publisher.publish(ctx(), request(sample_video))


def test_publish_missing_file(mock, tmp_path):
    with pytest.raises(PermanentError) as exc:
        facebook.publisher.publish(ctx(), request(tmp_path / "gone.mp4"))
    assert exc.value.code == "file_missing"
    assert mock.calls.call_count == 0


def test_publish_invalid_request(mock, sample_video):
    with pytest.raises(PermanentError) as exc:
        facebook.publisher.publish(ctx(), request(sample_video, duration_s=120))
    assert exc.value.code == "invalid_video"
    assert mock.calls.call_count == 0


# --------------------------------------------------------------------------- poll


def test_poll_processing(mock):
    route = mock.get(VIDEO_URL).respond(json=status_body("processing"))
    result = facebook.publisher.poll(ctx(), {"video_id": VIDEO_ID, "page_id": PAGE_ID})
    assert result.outcome == PublishOutcome.PROCESSING
    assert "processing in_progress" in result.message
    assert dict(route.calls.last.request.url.params)["fields"] == "status"


@pytest.mark.parametrize(
    ("publishing", "permalink", "expected"),
    [
        (
            {"status": "complete", "publish_status": "published"},
            "/reel/990001/",
            "https://www.facebook.com/reel/990001/",
        ),
        (
            {"status": "completed"},
            "https://www.facebook.com/magic/videos/990001/",
            "https://www.facebook.com/magic/videos/990001/",
        ),
        ({"status": "in_progress", "publish_status": "published"}, None, None),
    ],
)
def test_poll_published(mock, publishing, permalink, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        fields = dict(request.url.params)["fields"]
        if fields == "status":
            return httpx.Response(200, json=status_body("ready", publishing=publishing))
        assert fields == "permalink_url"
        return httpx.Response(
            200, json={"id": VIDEO_ID, **({"permalink_url": permalink} if permalink else {})}
        )

    mock.get(VIDEO_URL).mock(side_effect=handler)
    result = facebook.publisher.poll(ctx(), {"video_id": VIDEO_ID})
    assert result.outcome == PublishOutcome.PUBLISHED
    assert result.platform_post_id == VIDEO_ID
    assert result.permalink == expected


def test_poll_published_permalink_failure(mock):
    mock.get(VIDEO_URL).mock(
        side_effect=[
            httpx.Response(200, json=status_body("ready", publishing={"status": "complete"})),
            graph_error(400, 100, None, "nonexisting field"),
        ]
    )
    result = facebook.publisher.poll(ctx(), {"video_id": VIDEO_ID})
    assert result.outcome == PublishOutcome.PUBLISHED and result.permalink is None


@pytest.mark.parametrize(
    ("body", "cls", "fragment"),
    [
        (
            status_body(
                "processing",
                processing={
                    "status": "not_started",
                    "error": {"message": "Resolution too low. Video must have a minimum resolution of 540p."},
                },
            ),
            PermanentError,
            "Resolution too low",
        ),
        (
            status_body("error", publishing={"status": "error", "error": {"message": "Copyright"}}),
            PermanentError,
            "Copyright",
        ),
        (status_body("error"), PermanentError, "no reason given"),
        (status_body("processing", processing={"status": "error"}), PermanentError, "no reason given"),
        (status_body("expired"), RestartPublish, "expired"),
        (
            status_body(
                "upload_failed", uploading={"status": "error", "errors": [{"message": "Partial request"}]}
            ),
            RestartPublish,
            "Partial request",
        ),
        (
            status_body("ready", publishing={"status": "complete", "publish_status": "draft"}),
            PermanentError,
            "draft",
        ),
    ],
)
def test_poll_failures(mock, body, cls, fragment):
    mock.get(VIDEO_URL).respond(json=body)
    with pytest.raises(cls) as exc:
        facebook.publisher.poll(ctx(), {"video_id": VIDEO_ID})
    assert type(exc.value) is cls
    assert fragment in exc.value.message


def test_poll_codes():
    assert facebook._check_failures({"video_status": "ready"}) is None
    with pytest.raises(RestartPublish) as exc:
        facebook._check_failures({"video_status": "expired"})
    assert exc.value.code == "video_expired"
    with pytest.raises(RestartPublish) as exc:
        facebook._check_failures({"video_status": "upload_failed"})
    assert exc.value.code == "upload_failed"
    with pytest.raises(PermanentError) as exc:
        facebook._check_failures({"processing_phase": {"error": {"message": "bad"}}})
    assert exc.value.code == "processing_error"


def test_poll_missing_video_id():
    with pytest.raises(PermanentError):
        facebook.publisher.poll(ctx(), {})


@pytest.mark.parametrize(
    ("response", "cls"),
    [
        (graph_error(400, 190), AuthError),
        (graph_error(400, 80001), RateLimitedError),
        (httpx.Response(500), RetryableError),
    ],
)
def test_poll_request_errors(mock, response, cls):
    mock.get(VIDEO_URL).mock(return_value=response)
    with pytest.raises(cls):
        facebook.publisher.poll(ctx(), {"video_id": VIDEO_ID})


SENT_AT = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock(monkeypatch):
    now = {"t": SENT_AT}
    monkeypatch.setattr(facebook, "utcnow", lambda: now["t"])
    return now


def unconfirmed(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "video_id": VIDEO_ID,
        "page_id": PAGE_ID,
        "finish_unconfirmed": True,
        "finish_sent_at": SENT_AT.isoformat(),
        "finish_options": {"description": "Watch this #magic", "title": "Magic trick"},
    }
    state.update(overrides)
    return state


def test_poll_unconfirmed_finish_waits_during_the_grace_period(mock, clock):
    reels = mock.post(REELS_URL).respond(json={"success": True})
    mock.get(VIDEO_URL).respond(json=status_body("ready"))
    clock["t"] = SENT_AT + facebook.FINISH_RESEND_GRACE - timedelta(seconds=1)
    result = facebook.publisher.poll(ctx(), unconfirmed())
    assert result.outcome == PublishOutcome.PROCESSING and result.state == unconfirmed()
    assert reels.call_count == 0


def test_poll_unconfirmed_finish_is_confirmed_once_publishing_started(mock, clock):
    reels = mock.post(REELS_URL).respond(json={"success": True})
    mock.get(VIDEO_URL).respond(json=status_body("processing", publishing={"status": "in_progress"}))
    clock["t"] = SENT_AT + timedelta(hours=1)
    result = facebook.publisher.poll(ctx(), unconfirmed())
    assert result.outcome == PublishOutcome.PROCESSING
    assert "finish_unconfirmed" not in result.state and result.state["video_id"] == VIDEO_ID
    assert reels.call_count == 0


def test_poll_unconfirmed_finish_is_resent_for_the_same_video_after_the_grace_period(mock, clock):
    reels = mock.post(REELS_URL).respond(json={"success": True})
    mock.get(VIDEO_URL).respond(json=status_body("ready"))
    clock["t"] = SENT_AT + facebook.FINISH_RESEND_GRACE
    result = facebook.publisher.poll(ctx(), unconfirmed())
    assert reels.call_count == 1  # never a "start": no second upload
    assert form(reels.calls.last.request) == {
        "upload_phase": "finish",
        "video_id": VIDEO_ID,
        "video_state": "PUBLISHED",
        "description": "Watch this #magic",
        "title": "Magic trick",
        "appsecret_proof": proof(TOKEN),
    }
    assert result.outcome == PublishOutcome.PROCESSING
    assert result.state["finish_resends"] == 1
    assert result.state["finish_sent_at"] == clock["t"].isoformat()
    assert "finish_unconfirmed" not in result.state  # Facebook accepted the finish this time


def test_poll_unconfirmed_finish_resend_failure_keeps_waiting(mock, clock):
    reels = mock.post(REELS_URL).respond(status_code=502)
    mock.get(VIDEO_URL).respond(
        json=status_body("ready", publishing={"status": "not_started", "publish_status": "draft"})
    )
    clock["t"] = SENT_AT + timedelta(hours=1)
    result = facebook.publisher.poll(ctx(), unconfirmed())
    assert reels.call_count == 1
    assert result.outcome == PublishOutcome.PROCESSING
    assert result.state["finish_unconfirmed"] is True and result.state["finish_resends"] == 1


def test_poll_unconfirmed_finish_auth_error_is_raised(mock, clock):
    mock.post(REELS_URL).mock(return_value=graph_error(400, 190, 463))
    mock.get(VIDEO_URL).respond(json=status_body("ready"))
    clock["t"] = SENT_AT + timedelta(hours=1)
    with pytest.raises(AuthError):
        facebook.publisher.poll(ctx(), unconfirmed())


def test_poll_unconfirmed_finish_resends_are_bounded(mock, clock):
    reels = mock.post(REELS_URL).respond(json={"success": True})
    mock.get(VIDEO_URL).respond(json=status_body("ready"))
    clock["t"] = SENT_AT + timedelta(days=1)
    result = facebook.publisher.poll(ctx(), unconfirmed(finish_resends=facebook.MAX_FINISH_RESENDS))
    assert result.outcome == PublishOutcome.PROCESSING and reels.call_count == 0


def test_poll_unconfirmed_finish_is_not_resent_while_facebook_is_processing(mock, clock):
    reels = mock.post(REELS_URL).respond(json={"success": True})
    mock.get(VIDEO_URL).respond(json=status_body("processing"))
    clock["t"] = SENT_AT + timedelta(hours=1)
    facebook.publisher.poll(ctx(), unconfirmed())
    assert reels.call_count == 0


def test_poll_unconfirmed_finish_without_a_sent_time_starts_the_clock(mock, clock):
    reels = mock.post(REELS_URL).respond(json={"success": True})
    mock.get(VIDEO_URL).respond(json=status_body("ready"))
    state = unconfirmed()
    del state["finish_sent_at"]
    clock["t"] = SENT_AT + timedelta(hours=1)
    result = facebook.publisher.poll(ctx(), state)
    assert result.state["finish_sent_at"] == clock["t"].isoformat() and reels.call_count == 0


def test_poll_unconfirmed_finish_published_drops_the_finish_bookkeeping(mock, clock):
    mock.get(VIDEO_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json=status_body("ready", publishing={"status": "complete", "publish_status": "published"}),
            ),
            httpx.Response(200, json={"permalink_url": "/reel/990001/"}),
        ]
    )
    result = facebook.publisher.poll(ctx(), unconfirmed(finish_resends=1))
    assert result.outcome == PublishOutcome.PUBLISHED
    assert result.state == {"video_id": VIDEO_ID, "page_id": PAGE_ID}


@pytest.mark.parametrize(
    ("body", "code"),
    [(status_body("expired"), "video_expired"), (status_body("upload_failed"), "upload_failed")],
)
def test_poll_unconfirmed_finish_of_a_dead_upload_restarts(mock, clock, body, code):
    """Facebook confirms there is nothing to publish: the only case that uploads again."""
    mock.get(VIDEO_URL).respond(json=body)
    with pytest.raises(RestartPublish) as exc:
        facebook.publisher.poll(ctx(), unconfirmed())
    assert exc.value.code == code


def test_publish_then_poll_lifecycle(mock, sample_video):
    mock.post(REELS_URL).mock(side_effect=reels_endpoint())
    mock.post(RUPLOAD).respond(json={"success": True})
    mock.get(VIDEO_URL).mock(
        side_effect=[
            httpx.Response(200, json=status_body("processing")),
            httpx.Response(
                200,
                json=status_body("ready", publishing={"status": "complete", "publish_status": "published"}),
            ),
            httpx.Response(200, json={"permalink_url": "/reel/990001/"}),
        ]
    )
    first = facebook.publisher.publish(ctx(), request(sample_video))
    second = facebook.publisher.poll(ctx(), first.state)
    third = facebook.publisher.poll(ctx(), second.state)
    assert second.outcome == PublishOutcome.PROCESSING
    assert third.outcome == PublishOutcome.PUBLISHED
    assert third.permalink == "https://www.facebook.com/reel/990001/"


# --------------------------------------------------------------------------- lister


def video(video_id: str, created: str, **extra: Any) -> dict[str, Any]:
    item = {
        "id": video_id,
        "description": f"desc {video_id}",
        "created_time": created,
        "permalink_url": f"/reel/{video_id}/",
        "length": 12.5,
        "source": f"https://video.fbcdn.test/{video_id}.mp4",
    }
    item.update(extra)
    return {k: v for k, v in item.items() if v is not None}


def test_list_recent_merges_edges(mock):
    since = datetime(2026, 10, 1, tzinfo=UTC)
    reels = mock.get(REELS_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": [
                        video("r1", "2026-10-13T10:00:00+0000"),
                        video("r2", "2026-10-10T10:00:00+0000"),
                    ],
                    "paging": {"cursors": {"after": "RC1"}, "next": "https://graph.facebook.com/next"},
                },
            ),
            httpx.Response(200, json={"data": [video("r-old", "2026-09-01T10:00:00+0000")]}),
        ]
    )
    videos_route = mock.get(VIDEOS_URL).respond(
        json={
            "data": [
                video("v1", "2026-10-12T10:00:00+0000", length=None, source=None),
                video("r2", "2026-10-10T10:00:00+0000", description="dup"),
                video("v-old", "2026-09-02T10:00:00+0000"),
            ]
        }
    )
    result = facebook.lister.list_recent(ctx(), since=since, limit=10)

    assert [v.external_id for v in result] == ["r1", "v1", "r2"]
    r1, v1, r2 = result
    assert r1.permalink == "https://www.facebook.com/reel/r1/"
    assert r1.duration_s == 12.5 and r1.caption == "desc r1"
    assert r1.download_url == "https://video.fbcdn.test/r1.mp4"
    assert r1.created_at == datetime(2026, 10, 13, 10, tzinfo=UTC)
    assert r1.extra == {"edge": "video_reels"}
    assert v1.duration_s is None and v1.download_url is None and v1.extra == {"edge": "videos"}
    assert r2.caption == "desc r2"  # the Reels edge entry wins
    first = dict(reels.calls[0].request.url.params)
    assert first["fields"] == facebook.LIST_FIELDS
    assert first["since"] == str(int(since.timestamp()))
    assert dict(reels.calls[1].request.url.params)["after"] == "RC1"
    assert dict(videos_route.calls.last.request.url.params)["since"] == str(int(since.timestamp()))


def test_list_recent_limit_and_unix_times(mock):
    mock.get(REELS_URL).respond(
        json={
            "data": [
                {"id": "a", "updated_time": 1760000000},
                {"id": "b", "updated_time": "1759000000"},
                {"no": "id"},
            ]
        }
    )
    mock.get(VIDEOS_URL).respond(json={"data": [video("c", "2026-10-14T00:00:00+0000")]})
    result = facebook.lister.list_recent(ctx(), limit=2)
    assert [v.external_id for v in result] == ["c", "a"]
    assert result[1].created_at == datetime.fromtimestamp(1760000000, UTC)
    assert facebook.lister.list_recent(ctx(), limit=0) == []


def test_list_recent_errors(mock):
    mock.get(REELS_URL).mock(return_value=graph_error(400, 190))
    with pytest.raises(AuthError):
        facebook.lister.list_recent(ctx())


def test_list_recent_without_the_manage_task_fails_with_a_clear_message(mock):
    """api-meta-missed-2: listing /videos needs the MANAGE task (Video API 'Get Videos' guide)."""
    with pytest.raises(PermanentError) as exc:
        facebook.lister.list_recent(ctx(can_manage=False))
    assert exc.value.code == "import_permission"
    assert "MANAGE" in exc.value.message and "Publishing to this Page is not affected" in exc.value.message
    assert mock.calls.call_count == 0
    # Accounts connected before the flag existed (or with the task) still try.
    mock.get(REELS_URL).respond(json={"data": []})
    mock.get(VIDEOS_URL).respond(json={"data": []})
    assert facebook.lister.list_recent(ctx()) == []
    assert facebook.lister.list_recent(ctx(can_manage=True)) == []


@pytest.mark.parametrize(
    ("reels_response", "videos_response"),
    [
        (graph_error(403, 200, None, "(#200) Requires pages_read_engagement permission"), None),
        (graph_error(400, 10, None, "(#10) Application does not have permission"), None),
        (httpx.Response(200, json={"data": []}), graph_error(403, 200)),
    ],
)
def test_list_recent_permission_error_is_not_an_auth_error(mock, reels_response, videos_response):
    """A permission error on the read path must not mark the account needs-reconnect (the importer
    does that for AuthError), which would stop publishing to a Page that can publish."""
    mock.get(REELS_URL).mock(return_value=reels_response)
    mock.get(VIDEOS_URL).mock(return_value=videos_response or httpx.Response(200, json={"data": []}))
    with pytest.raises(PermanentError) as exc:
        facebook.lister.list_recent(ctx())
    assert not isinstance(exc.value, AuthError)
    assert exc.value.code == "import_permission" and TOKEN not in exc.value.message


def test_download_permission_error_is_not_an_auth_error(mock, tmp_path):
    mock.get(f"{GRAPH}/r1").mock(return_value=graph_error(403, 200))
    with pytest.raises(PermanentError) as exc:
        facebook.lister.download(ctx(), RemoteVideo(external_id="r1"), tmp_path / "x.mp4")
    assert exc.value.code == "import_permission"


def test_download_refetches_source(mock, tmp_path):
    node = mock.get(f"{GRAPH}/r1").respond(json={"id": "r1", "source": CDN_URL})
    cdn = mock.get(CDN_URL).respond(content=b"raw-video")
    dest = tmp_path / "r1.mp4"
    remote = RemoteVideo(external_id="r1", download_url="https://video.fbcdn.test/stale.mp4")
    assert facebook.lister.download(ctx(), remote, dest) == dest
    assert dest.read_bytes() == b"raw-video"
    assert dict(node.calls.last.request.url.params)["fields"] == "source"
    assert "authorization" not in cdn.calls.last.request.headers


def test_download_without_source(mock, tmp_path):
    mock.get(f"{GRAPH}/r1").respond(json={"id": "r1"})
    with pytest.raises(PermanentError) as exc:
        facebook.lister.download(ctx(), RemoteVideo(external_id="r1"), tmp_path / "x.mp4")
    assert exc.value.code == "source_unavailable"


def test_download_rejects_non_https_source(mock, tmp_path):
    mock.get(f"{GRAPH}/r1").respond(json={"source": "http://video.test/x.mp4"})
    with pytest.raises(PermanentError):
        facebook.lister.download(ctx(), RemoteVideo(external_id="r1"), tmp_path / "x.mp4")


def test_download_cdn_error(mock, tmp_path):
    mock.get(f"{GRAPH}/r1").respond(json={"source": CDN_URL})
    mock.get(CDN_URL).respond(status_code=500)
    with pytest.raises(RetryableError):
        facebook.lister.download(ctx(), RemoteVideo(external_id="r1"), tmp_path / "x.mp4")
    assert not (tmp_path / "x.mp4").exists()


# --------------------------------------------------------------------------- engine (end to end)


def engine_run(account, finish_script, status_script):
    """Mock Facebook for the real engine + adapter. Returns (starts, finishes, mock, publisher):
    the video ids started and finished so far, the respx router and the engine module. Finish
    answers come from `finish_script` (then success), status answers from status_script["now"]."""
    from repeater.engine import publisher

    page = account.external_id
    starts: list[str] = []
    finishes: list[str] = []
    videos = iter(["V1", "V2", "V3"])
    script = list(finish_script)

    def reels(request: httpx.Request) -> httpx.Response:
        body = form(request)
        if body["upload_phase"] == "start":
            starts.append(next(videos))
            return httpx.Response(200, json={"video_id": starts[-1]})
        finishes.append(body["video_id"])
        item = script.pop(0) if script else httpx.Response(200, json={"success": True})
        if isinstance(item, Exception):
            raise item
        return item

    def status(request: httpx.Request) -> httpx.Response:
        if dict(request.url.params).get("fields") == "permalink_url":
            return httpx.Response(200, json={"permalink_url": "/reel/V1/"})
        return httpx.Response(200, json=status_script["now"])

    mock = respx.mock(assert_all_called=False)
    mock.post(f"{GRAPH}/{page}/video_reels").mock(side_effect=reels)
    mock.post(url__regex=r"https://rupload\.facebook\.com/video-upload/v26\.0/V\d").respond(
        json={"success": True}
    )
    mock.get(url__regex=rf"{GRAPH}/V\d.*").mock(side_effect=status)
    return starts, finishes, mock, publisher


def test_engine_lost_finish_responses_never_upload_a_second_reel(db, engine_post):
    """api-meta-1 end to end: all finish responses lost -> the post waits (PROCESSING, video_id
    saved), polls, and ends PUBLISHED with exactly one upload."""
    from repeater.models import PostStatus

    post, account = engine_post("facebook")
    status = {"now": status_body("processing")}  # processing; publishing not started yet
    lost = [httpx.ReadTimeout("response lost")] * facebook.FINISH_ATTEMPTS
    starts, finishes, mock, publisher = engine_run(account, lost, status)
    now = utcnow()
    with mock:
        publisher.publish_post(db, post.id, now=now)
        db.refresh(post)
        assert post.status == PostStatus.PROCESSING
        assert post.publish_state["video_id"] == "V1" and post.publish_state["finish_unconfirmed"] is True

        publisher.poll_post(db, post.id, now=now + timedelta(minutes=1))
        db.refresh(post)
        assert post.status == PostStatus.PROCESSING and starts == ["V1"]

        status["now"] = status_body("ready", publishing={"status": "complete", "publish_status": "published"})
        publisher.poll_post(db, post.id, now=now + timedelta(minutes=2))
        db.refresh(post)
    assert post.status == PostStatus.PUBLISHED and post.platform_post_id == "V1"
    assert post.permalink == "https://www.facebook.com/reel/V1/"
    assert starts == ["V1"] and finishes == ["V1"] * facebook.FINISH_ATTEMPTS


def test_engine_lost_finish_then_4xx_is_not_needs_attention(db, engine_post):
    """The variant: attempt 1 landed (response lost), attempt 2 got a 4xx. Not a failure."""
    from repeater.models import PostStatus

    post, account = engine_post("facebook")
    status = {"now": status_body("processing")}
    script = [httpx.ReadTimeout("lost"), graph_error(400, 100, None, "Invalid upload phase")]
    starts, _finishes, mock, publisher = engine_run(account, script, status)
    with mock:
        now = utcnow()
        publisher.publish_post(db, post.id, now=now)
        db.refresh(post)
    assert post.status == PostStatus.PROCESSING
    assert post.publish_state["video_id"] == "V1" and starts == ["V1"]


def test_engine_resends_the_finish_for_the_same_video(db, engine_post, clock):
    """The finish never landed: after the grace period poll() finishes the SAME video again."""
    from repeater.models import PostStatus

    post, account = engine_post("facebook")
    status = {"now": status_body("ready")}  # processed, but never published
    lost = [httpx.ReadTimeout("x")] * facebook.FINISH_ATTEMPTS
    starts, finishes, mock, publisher = engine_run(account, lost, status)
    with mock:
        now = utcnow()
        publisher.publish_post(db, post.id, now=now)
        clock["t"] = SENT_AT + facebook.FINISH_RESEND_GRACE
        publisher.poll_post(db, post.id, now=now + facebook.FINISH_RESEND_GRACE)
        db.refresh(post)
        assert post.status == PostStatus.PROCESSING
        assert finishes == ["V1"] * (facebook.FINISH_ATTEMPTS + 1) and starts == ["V1"]
        status["now"] = status_body("ready", publishing={"status": "complete", "publish_status": "published"})
        publisher.poll_post(db, post.id, now=now + facebook.FINISH_RESEND_GRACE + timedelta(minutes=1))
        db.refresh(post)
    assert post.status == PostStatus.PUBLISHED and starts == ["V1"]
