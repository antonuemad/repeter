"""YouTube adapter: resumable upload, status polling, error mapping, metadata limits, listing."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from repeater.models import Platform
from repeater.platforms import get_lister, get_publisher, youtube
from repeater.platforms.base import (
    AccountContext,
    AuthError,
    MediaLister,
    NotSupportedError,
    OAuthTokens,
    PermanentError,
    PlatformError,
    Publisher,
    PublishOutcome,
    PublishRequest,
    RateLimitedError,
    RemoteVideo,
    RetryableError,
)
from repeater.timeutil import utcnow

ACCESS = "ya29.access-SECRET-token"
REFRESH = "1//refresh-SECRET-token"
API = "https://www.googleapis.com/youtube/v3"
SESSION_URI = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=SESSION1"
KIB256 = 256 * 1024


# --------------------------------------------------------------------------- helpers


def ctx(extra: dict | None = None) -> AccountContext:
    return AccountContext(
        account_id=7,
        platform=Platform.YOUTUBE,
        external_id="UCchan",
        tokens=OAuthTokens(
            access_token=ACCESS, refresh_token=REFRESH, expires_at=utcnow() + timedelta(hours=1)
        ),
        extra={"channel_id": "UCchan", "uploads_playlist_id": "UUchan", "custom_url": "@magiclap"}
        if extra is None
        else extra,
        display_name="Magic Lap",
    )


def make_req(path: Path, **kw: Any) -> PublishRequest:
    defaults: dict[str, Any] = {
        "post_id": 11,
        "video_path": path,
        "public_url": "https://rotator.test/m/1/x.mp4",
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "duration_s": 30.0,
        "width": 1080,
        "height": 1920,
        "title": "My Short",
        "caption": "Line one\nLine two #magic",
        "settings": {},
    }
    defaults.update(kw)
    return PublishRequest(**defaults)


def gerr(
    status: int, reason: str | None = None, message: str = "Something failed", **kw: Any
) -> httpx.Response:
    errors = [{"domain": "youtube", "reason": reason, "message": message}] if reason else []
    return httpx.Response(
        status, json={"error": {"code": status, "message": message, "errors": errors}}, **kw
    )


def assert_no_secrets(exc: BaseException) -> None:
    text = f"{exc} {getattr(exc, 'message', '')} {getattr(exc, 'code', '')}"
    assert ACCESS not in text and REFRESH not in text and "SESSION1" not in text


def binary_file(tmp_path: Path, size: int) -> Path:
    p = tmp_path / "encoded.mp4"
    p.write_bytes(os.urandom(size))
    return p


class FakeUploadServer:
    """Simulates the resumable-upload endpoint. `script` maps a 1-based PUT call number to an
    action: an int status (bytes not stored), "network" (nothing stored), "network_after"
    (stored, then the connection drops), "partial" (stores only the first 256 KiB of a chunk),
    "lose" (stores nothing, answers 308), "complete_body" (answers 200 with the video)."""

    def __init__(self, total: int, *, video: dict | None = None, script: dict[int, Any] | None = None):
        self.total = total
        self.received = bytearray()
        self.calls: list[tuple[str, str, int]] = []
        self.script = dict(script or {})
        self.video = video or {
            "id": "vid123",
            "status": {"uploadStatus": "uploaded", "privacyStatus": "public"},
        }

    def _range_response(self) -> httpx.Response:
        if len(self.received) >= self.total:
            return httpx.Response(201, json=self.video)
        headers = {"Range": f"bytes=0-{len(self.received) - 1}"} if self.received else {}
        return httpx.Response(308, headers=headers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        content_range = request.headers["content-range"]
        assert request.headers["authorization"] == f"Bearer {ACCESS}"
        kind = "probe" if content_range.startswith("bytes */") else "chunk"
        self.calls.append((kind, content_range, len(request.content)))
        action = self.script.get(len(self.calls))
        if action == "network":
            raise httpx.ConnectError("connection reset")
        if isinstance(action, int):
            return gerr(action, "backendError")
        if kind == "probe":
            assert content_range == f"bytes */{self.total}"
            assert request.headers["content-length"] == "0"
            return self._range_response()
        start, end, total = map(int, re.match(r"bytes (\d+)-(\d+)/(\d+)", content_range).groups())
        assert total == self.total and end - start + 1 == len(request.content)
        assert start == len(self.received), "client resent from the wrong offset"
        if action == "lose":
            return self._range_response()
        self.received += request.content[:KIB256] if action == "partial" else request.content
        if action == "network_after":
            raise httpx.ReadTimeout("timed out")
        return self._range_response()

    @property
    def chunk_calls(self) -> list[tuple[str, str, int]]:
        return [c for c in self.calls if c[0] == "chunk"]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(youtube, "_sleep", recorded.append)
    return recorded


@pytest.fixture
def mock() -> Any:
    with respx.mock(assert_all_called=False) as m:
        yield m


def upload_routes(mock: Any, server: FakeUploadServer, start: Any = None) -> tuple[Any, Any]:
    start_route = mock.route(method="POST", host="www.googleapis.com", path="/upload/youtube/v3/videos")
    if start is None:
        start_route.mock(return_value=httpx.Response(200, headers={"Location": SESSION_URI}))
    else:
        start_route.mock(side_effect=start)
    put_route = mock.route(method="PUT", host="www.googleapis.com", path="/upload/youtube/v3/videos")
    put_route.mock(side_effect=server)
    return start_route, put_route


# --------------------------------------------------------------------------- contracts


def test_module_objects_satisfy_protocols():
    assert isinstance(youtube.publisher, Publisher)
    assert isinstance(youtube.lister, MediaLister)
    assert get_publisher("youtube") is youtube.publisher
    assert get_lister(Platform.YOUTUBE) is youtube.lister
    assert youtube.publisher.limits.max_duration_s == 180


def test_chunk_size_is_a_multiple_of_256_kib():
    assert youtube.CHUNK_SIZE == 8 * 1024 * 1024
    assert youtube.CHUNK_SIZE % youtube.CHUNK_GRANULARITY == 0


# --------------------------------------------------------------------------- titles, descriptions, tags


def utf16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def test_title_strips_angle_brackets_and_newlines():
    assert youtube.build_title("  Hello <b>world</b>\n again ", "") == "Hello bworld/b again"


def test_title_falls_back_to_first_caption_line_then_short():
    assert youtube.build_title("", "\n  \n<>\nFirst real line\nsecond") == "First real line"
    assert youtube.build_title(None, "<<>>") == "Short"
    assert youtube.build_title("   ", None) == "Short"


def test_title_truncates_arabic_at_word_boundary():
    words = ["مرحبا", "بكم", "في", "ماجيك", "لاب"] * 10
    title = " ".join(words)
    assert len(title) > 100
    out = youtube.build_title(title, "")
    assert utf16(out) <= 100 and out.endswith("…")
    body = out[:-1]
    assert title.startswith(body) and title[len(body)] == " "


def test_title_counts_emoji_as_two_units():
    out = youtube.build_title("\U0001f600" * 80, "")
    assert utf16(out) <= 100
    assert out == "\U0001f600" * 49 + "…"


FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
THUMBS_MEDIUM = "\U0001f44d\U0001f3fd"
FLAG_EG = "\U0001f1ea\U0001f1ec"


@pytest.mark.parametrize(
    ("prefix", "unit"), [("", FAMILY), ("", THUMBS_MEDIUM), ("", FLAG_EG), ("x", FLAG_EG), ("ab", FAMILY)]
)
def test_title_never_splits_emoji_sequences_or_flags(prefix: str, unit: str):
    text = prefix + unit * 60
    out = youtube.build_title(text, "")
    body = out[:-1]
    assert utf16(out) <= 100 and out.endswith("\u2026")
    assert text.startswith(body) and body.startswith(prefix)
    assert body.removeprefix(prefix).replace(unit, "") == ""


def test_title_keeps_arabic_diacritics_with_their_letter():
    word = "\u0645\u064f\u062d\u064e\u0645\u0651\u064e\u062f\u064c"  # Arabic name with harakat
    text = word * 20
    out = youtube.build_title(text, "")
    body = out[:-1]
    assert utf16(out) <= 100 and text.startswith(body)
    assert unicodedata.category(text[len(body)]) != "Mn"  # the cut never strands a mark


def test_description_never_splits_an_emoji_sequence():
    out = youtube.build_description(FAMILY * 400)
    assert len(out.encode("utf-8")) <= 5000 and out.endswith(FAMILY + "\u2026")


def test_description_truncates_arabic_by_bytes():
    text = "م" * 3000  # 2 bytes each = 6000 bytes
    out = youtube.build_description(text)
    raw = out.encode("utf-8")
    assert len(raw) <= 5000 and out.endswith("…")
    assert out[:-1] == "م" * ((5000 - 3) // 2)


@pytest.mark.parametrize("prefix", ["", "a", "ab", "abc"])
def test_description_never_splits_a_four_byte_emoji(prefix: str):
    text = prefix + "\U0001f600" * 1300
    out = youtube.build_description(text)
    assert len(out.encode("utf-8")) <= 5000
    body = out[:-1]
    assert body.startswith(prefix) and set(body[len(prefix) :]) == {"\U0001f600"}
    assert len(body.encode("utf-8")) > 5000 - 3 - 4


def test_description_keeps_text_at_exact_limit_and_strips_brackets():
    exact = "é" * 2500  # 5000 bytes
    assert youtube.build_description(exact) == exact
    assert youtube.build_description("a <tag> b\nline 2") == "a tag b\nline 2"
    assert youtube.build_description(None) == ""


def test_tags_respect_500_character_budget_with_quotes_and_commas():
    tags = youtube.build_tags(["#magic", "Magic", "card trick", "", 5, "a<b>c", "x,y"] + ["t" * 100] * 6)
    assert tags[:5] == ["magic", "card trick", "abc", "x y", "t" * 100]
    cost = sum(len(t) + (2 if " " in t else 0) for t in tags) + len(tags) - 1
    assert cost <= 500
    assert youtube.build_tags("one, two ,one") == ["one", "two"]
    assert youtube.build_tags(None) == []


# --------------------------------------------------------------------------- helpers: durations, quota reset


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("PT15M33S", 933.0),
        ("PT1H2M3S", 3723.0),
        ("P1DT2H", 93600.0),
        ("PT59.5S", 59.5),
        ("PT0S", 0.0),
        ("P0D", 0.0),
        ("pt45s", 45.0),
        ("", None),
        (None, None),
        ("PT", None),
        ("garbage", None),
        ("1M5S", None),
    ],
)
def test_parse_iso_duration(value: str | None, expected: float | None):
    assert youtube.parse_iso_duration(value) == expected


@pytest.mark.parametrize(
    ("now", "until"),
    [
        (datetime(2026, 10, 14, 12, 0, tzinfo=UTC), timedelta(hours=19)),  # PDT (UTC-7)
        (datetime(2026, 1, 10, 12, 0, tzinfo=UTC), timedelta(hours=20)),  # PST (UTC-8)
        (datetime(2026, 11, 1, 6, 30, tzinfo=UTC), timedelta(minutes=30)),  # 23:30 PDT the night before
        (datetime(2026, 11, 1, 8, 30, tzinfo=UTC), timedelta(hours=23, minutes=30)),  # 25-hour day
    ],
)
def test_seconds_until_quota_reset_uses_pacific_midnight(now: datetime, until: timedelta):
    expected = (until + youtube.QUOTA_RESET_MARGIN).total_seconds()
    assert youtube.seconds_until_quota_reset(now) == pytest.approx(expected)


# --------------------------------------------------------------------------- validate


def test_validate_accepts_vertical_and_square(sample_video: Path):
    assert youtube.publisher.validate(ctx(), make_req(sample_video)) == []
    assert youtube.publisher.validate(ctx(), make_req(sample_video, width=1080, height=1080)) == []


def test_validate_rejects_landscape_long_short_and_unknown_dims(sample_video: Path):
    def problems(**kw: Any) -> str:
        return " | ".join(youtube.publisher.validate(ctx(), make_req(sample_video, **kw)))

    assert "landscape" in problems(width=1920, height=1080)
    assert "180" in problems(duration_s=181)
    assert "at least" in problems(duration_s=0.4)
    assert "unknown" in problems(width=0, height=0)
    assert "256 GB" in problems(size_bytes=257 * 1024**3)
    assert youtube.publisher.validate(ctx(), make_req(sample_video, duration_s=180)) == []


@pytest.mark.parametrize(
    ("settings", "needle"),
    [
        ({"privacy_status": "friends"}, "visibility"),
        ({"self_declared_made_for_kids": None}, "made for kids"),
        ({"self_declared_made_for_kids": "false"}, "made for kids"),
        ({"notify_subscribers": "yes"}, "Notify subscribers"),
        ({"contains_synthetic_media": 1}, "synthetic"),
        ({"category_id": "people"}, "category"),
        ({"category_id": True}, "category"),
        ({"tags": 5}, "tags"),
    ],
)
def test_validate_settings(sample_video: Path, settings: dict, needle: str):
    problems = youtube.publisher.validate(ctx(), make_req(sample_video, settings=settings))
    assert len(problems) == 1 and needle in problems[0]


def test_validate_accepts_explicit_settings(sample_video: Path):
    settings = {
        "privacy_status": "unlisted",
        "self_declared_made_for_kids": True,
        "category_id": 24,
        "notify_subscribers": True,
        "contains_synthetic_media": True,
        "tags": "a, b",
    }
    assert youtube.publisher.validate(ctx(), make_req(sample_video, settings=settings)) == []


# --------------------------------------------------------------------------- publish


def test_publish_single_chunk_with_real_video(mock: Any, sample_video: Path, sleeps: list[float]):
    size = sample_video.stat().st_size
    server = FakeUploadServer(size)
    start, _ = upload_routes(mock, server)
    settings = {
        "privacy_status": "unlisted",
        "notify_subscribers": False,
        "tags": ["magic", "trick"],
        "contains_synthetic_media": True,
    }
    req = make_req(sample_video, title="", caption="<Best> trick ever\nmore text", settings=settings)

    result = youtube.publisher.publish(ctx(), req)

    assert result.outcome == PublishOutcome.PROCESSING
    assert result.platform_post_id == "vid123"
    assert result.state == {"video_id": "vid123", "requested_privacy": "unlisted"}
    assert result.permalink is None
    sent = start.calls.last.request
    assert sent.url.params["uploadType"] == "resumable"
    assert sent.url.params["part"] == "snippet,status"
    assert sent.url.params["notifySubscribers"] == "false"
    assert sent.headers["x-upload-content-length"] == str(size)
    assert sent.headers["x-upload-content-type"] == "video/mp4"
    assert sent.headers["content-type"].startswith("application/json")
    assert sent.headers["authorization"] == f"Bearer {ACCESS}"
    body = json.loads(sent.content)
    assert body == {
        "snippet": {
            "title": "Best trick ever",
            "description": "Best trick ever\nmore text",
            "categoryId": "22",
            "tags": ["magic", "trick"],
        },
        "status": {
            "privacyStatus": "unlisted",
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
    }
    assert server.calls == [("chunk", f"bytes 0-{size - 1}/{size}", size)]
    assert bytes(server.received) == sample_video.read_bytes()
    assert sleeps == []


def test_publish_notify_subscribers_true_and_tags_omitted(mock: Any, tmp_path: Path, sleeps: list[float]):
    path = binary_file(tmp_path, 1000)
    start, _ = upload_routes(mock, FakeUploadServer(1000))
    youtube.publisher.publish(ctx(), make_req(path, settings={"notify_subscribers": True}))
    req = start.calls.last.request
    assert req.url.params["notifySubscribers"] == "true"
    assert "tags" not in json.loads(req.content)["snippet"]


def test_publish_sends_256k_multiple_chunks_with_308_continuation(
    mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
):
    monkeypatch.setattr(youtube, "CHUNK_SIZE", 2 * KIB256)
    size = 5 * KIB256 + 1234
    path = binary_file(tmp_path, size)
    server = FakeUploadServer(size)
    upload_routes(mock, server)

    result = youtube.publisher.publish(ctx(), make_req(path))

    assert result.platform_post_id == "vid123"
    sizes = [c[2] for c in server.chunk_calls]
    assert sizes == [2 * KIB256, 2 * KIB256, KIB256 + 1234]
    assert all(s % KIB256 == 0 for s in sizes[:-1])
    assert server.chunk_calls[1][1] == f"bytes {2 * KIB256}-{4 * KIB256 - 1}/{size}"
    assert bytes(server.received) == path.read_bytes()
    assert sleeps == []


def test_publish_resumes_after_5xx_by_querying_status(
    mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
):
    monkeypatch.setattr(youtube, "CHUNK_SIZE", KIB256)
    size = 3 * KIB256
    path = binary_file(tmp_path, size)
    server = FakeUploadServer(size, script={2: 503})
    upload_routes(mock, server)

    result = youtube.publisher.publish(ctx(), make_req(path))

    assert result.platform_post_id == "vid123"
    kinds = [c[0] for c in server.calls]
    assert kinds == ["chunk", "chunk", "probe", "chunk", "chunk"]
    assert server.calls[3][1].startswith(f"bytes {KIB256}-")
    assert len(sleeps) == 1
    assert bytes(server.received) == path.read_bytes()


def test_publish_resumes_after_network_error_without_resending_stored_bytes(
    mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
):
    monkeypatch.setattr(youtube, "CHUNK_SIZE", KIB256)
    size = 3 * KIB256 + 10
    path = binary_file(tmp_path, size)
    server = FakeUploadServer(size, script={2: "network_after", 3: "network"})
    upload_routes(mock, server)

    youtube.publisher.publish(ctx(), make_req(path))

    # chunk2 stored then dropped; first probe fails on the network; second probe resumes at 2*256K.
    kinds = [c[0] for c in server.calls]
    assert kinds == ["chunk", "chunk", "probe", "probe", "chunk", "chunk"]
    assert server.calls[4][1].startswith(f"bytes {2 * KIB256}-")
    assert len(sleeps) == 2
    assert bytes(server.received) == path.read_bytes()


def test_publish_follows_partial_range_from_308(
    mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
):
    monkeypatch.setattr(youtube, "CHUNK_SIZE", 2 * KIB256)
    size = 4 * KIB256
    path = binary_file(tmp_path, size)
    server = FakeUploadServer(size, script={1: "partial"})
    upload_routes(mock, server)

    youtube.publisher.publish(ctx(), make_req(path))

    ranges = [c[1] for c in server.chunk_calls]
    assert ranges[0] == f"bytes 0-{2 * KIB256 - 1}/{size}"
    assert ranges[1].startswith(f"bytes {KIB256}-")
    assert bytes(server.received) == path.read_bytes()


def test_publish_restarts_from_zero_when_308_has_no_range(mock: Any, tmp_path: Path, sleeps: list[float]):
    path = binary_file(tmp_path, 5000)
    server = FakeUploadServer(5000, script={1: "lose"})
    upload_routes(mock, server)

    youtube.publisher.publish(ctx(), make_req(path))

    assert [c[1] for c in server.chunk_calls] == ["bytes 0-4999/5000"] * 2
    assert len(sleeps) == 1


def test_publish_completes_when_status_query_shows_finished_upload(
    mock: Any, tmp_path: Path, sleeps: list[float]
):
    path = binary_file(tmp_path, 4000)
    server = FakeUploadServer(4000, script={1: "network_after"})
    upload_routes(mock, server)

    result = youtube.publisher.publish(ctx(), make_req(path))

    assert [c[0] for c in server.calls] == ["chunk", "probe"]
    assert result.platform_post_id == "vid123"


def test_publish_gives_up_after_repeated_5xx(
    mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
):
    path = binary_file(tmp_path, 4000)
    server = FakeUploadServer(4000, script={i: 503 for i in range(1, 20)})
    upload_routes(mock, server)

    with pytest.raises(RetryableError) as ei:
        youtube.publisher.publish(ctx(), make_req(path))

    assert not isinstance(ei.value, RateLimitedError)
    assert len(sleeps) == youtube.MAX_UPLOAD_RETRIES
    assert len(server.calls) == youtube.MAX_UPLOAD_RETRIES + 1
    assert sleeps == sorted(sleeps)  # exponential backoff
    assert_no_secrets(ei.value)


def test_publish_long_retry_after_is_returned_instead_of_sleeping(
    mock: Any, tmp_path: Path, sleeps: list[float]
):
    path = binary_file(tmp_path, 4000)
    upload_routes(mock, FakeUploadServer(4000))
    put = mock.route(method="PUT", host="www.googleapis.com", path="/upload/youtube/v3/videos")
    put.mock(return_value=gerr(503, "backendError", headers={"Retry-After": "600"}))

    with pytest.raises(RetryableError) as ei:
        youtube.publisher.publish(ctx(), make_req(path))

    assert ei.value.retry_after == 600
    assert sleeps == []


def test_publish_retries_session_start_on_5xx_and_network_errors(
    mock: Any, tmp_path: Path, sleeps: list[float]
):
    path = binary_file(tmp_path, 100)
    responses = iter(
        [
            gerr(503, "backendError"),
            httpx.ConnectError("refused"),
            httpx.Response(200, headers={"Location": SESSION_URI}),
        ]
    )

    def start(request: httpx.Request) -> httpx.Response:
        item = next(responses)
        if isinstance(item, Exception):
            raise item
        return item

    start_route, _ = upload_routes(mock, FakeUploadServer(100), start=start)
    youtube.publisher.publish(ctx(), make_req(path))
    assert start_route.call_count == 3 and len(sleeps) == 2


def test_publish_session_start_gives_up_after_repeated_5xx(mock: Any, tmp_path: Path, sleeps: list[float]):
    path = binary_file(tmp_path, 100)
    start_route, put = upload_routes(mock, FakeUploadServer(100), start=lambda r: gerr(500, "backendError"))
    with pytest.raises(RetryableError):
        youtube.publisher.publish(ctx(), make_req(path))
    assert start_route.call_count == youtube.MAX_UPLOAD_RETRIES + 1
    assert not put.called


def test_publish_session_start_without_location_is_retryable(mock: Any, tmp_path: Path, sleeps: list[float]):
    path = binary_file(tmp_path, 100)
    upload_routes(mock, FakeUploadServer(100), start=lambda r: httpx.Response(200))
    with pytest.raises(RetryableError):
        youtube.publisher.publish(ctx(), make_req(path))


def test_publish_restarts_once_when_session_expires(mock: Any, tmp_path: Path, sleeps: list[float]):
    path = binary_file(tmp_path, 3000)
    server = FakeUploadServer(3000, script={1: 404})
    start_route, _ = upload_routes(mock, server)

    result = youtube.publisher.publish(ctx(), make_req(path))

    assert start_route.call_count == 2
    assert result.platform_post_id == "vid123"


def test_publish_session_expiring_twice_is_retryable(mock: Any, tmp_path: Path, sleeps: list[float]):
    path = binary_file(tmp_path, 3000)
    start_route, _ = upload_routes(mock, FakeUploadServer(3000, script={1: 404, 2: 404}))
    with pytest.raises(RetryableError) as ei:
        youtube.publisher.publish(ctx(), make_req(path))
    assert start_route.call_count == 2 and "expired" in ei.value.message


def test_publish_quota_error_during_chunk_is_rate_limited(mock: Any, tmp_path: Path, sleeps: list[float]):
    path = binary_file(tmp_path, 3000)
    upload_routes(mock, FakeUploadServer(3000))
    mock.route(method="PUT", host="www.googleapis.com", path="/upload/youtube/v3/videos").mock(
        return_value=gerr(403, "quotaExceeded")
    )
    with pytest.raises(RateLimitedError) as ei:
        youtube.publisher.publish(ctx(), make_req(path))
    assert ei.value.code == "quotaExceeded"


def test_publish_rejects_invalid_settings_before_any_request(mock: Any, tmp_path: Path):
    path = binary_file(tmp_path, 100)
    start_route, put = upload_routes(mock, FakeUploadServer(100))
    with pytest.raises(PermanentError) as ei:
        youtube.publisher.publish(ctx(), make_req(path, settings={"privacy_status": "everyone"}))
    assert ei.value.code == "invalid_settings"
    assert not start_route.called and not put.called


@pytest.mark.parametrize("size", [None, 0])
def test_publish_missing_or_empty_file_is_permanent(mock: Any, tmp_path: Path, size: int | None):
    path = tmp_path / "encoded.mp4"
    if size is not None:
        path.write_bytes(b"")
    start_route, _ = upload_routes(mock, FakeUploadServer(1))
    with pytest.raises(PermanentError):
        youtube.publisher.publish(ctx(), make_req(path))
    assert not start_route.called


def test_publish_without_video_id_in_response_is_permanent(mock: Any, tmp_path: Path, sleeps: list[float]):
    path = binary_file(tmp_path, 100)
    upload_routes(mock, FakeUploadServer(100, video={"status": {"uploadStatus": "uploaded"}}))
    with pytest.raises(PermanentError) as ei:
        youtube.publisher.publish(ctx(), make_req(path))
    assert ei.value.code == "no_video_id"


def test_publish_reports_immediate_rejection(mock: Any, tmp_path: Path, sleeps: list[float]):
    path = binary_file(tmp_path, 100)
    video = {"id": "v9", "status": {"uploadStatus": "rejected", "rejectionReason": "duplicate"}}
    upload_routes(mock, FakeUploadServer(100, video=video))
    with pytest.raises(PermanentError) as ei:
        youtube.publisher.publish(ctx(), make_req(path))
    assert ei.value.code == "duplicate" and "duplicate" in ei.value.message


# --------------------------------------------------------------------------- error mapping


@pytest.mark.parametrize(
    ("status", "reason", "exc_type", "not_type"),
    [
        (401, "authError", AuthError, None),
        (401, None, AuthError, None),
        (401, "youtubeSignupRequired", PermanentError, None),
        (403, "quotaExceeded", RateLimitedError, None),
        (403, "dailyLimitExceeded", RateLimitedError, None),
        (400, "uploadLimitExceeded", RateLimitedError, None),
        (403, "forbidden", PermanentError, None),
        (403, "insufficientPermissions", AuthError, None),
        (403, "rateLimitExceeded", RateLimitedError, None),
        (429, None, RateLimitedError, None),
        (400, "invalidTitle", PermanentError, None),
        (403, "forbiddenPrivacySetting", PermanentError, None),
        (500, "backendError", RetryableError, RateLimitedError),
    ],
)
def test_error_mapping(status: int, reason: str | None, exc_type: type, not_type: type | None):
    resp = gerr(status, reason, message='Quota <a href="/x">docs</a> exceeded')
    err = youtube.youtube_api_error(resp, "upload")
    assert isinstance(err, exc_type)
    if not_type:
        assert not isinstance(err, not_type)
    assert err.status == status and err.code == reason
    assert "<a" not in err.message


def test_quota_errors_wait_until_pacific_midnight():
    err = youtube.youtube_api_error(gerr(403, "quotaExceeded"), "upload")
    assert err.retry_after == pytest.approx(youtube.seconds_until_quota_reset(), abs=5)
    assert "midnight Pacific" in err.message


def test_upload_limit_waits_a_day_and_rate_limit_uses_retry_after():
    assert youtube.youtube_api_error(gerr(400, "uploadLimitExceeded"), "upload").retry_after == 86400
    err = youtube.youtube_api_error(gerr(403, "rateLimitExceeded", headers={"Retry-After": "30"}), "x")
    assert err.retry_after == 30


def test_error_reason_found_even_when_not_first():
    resp = httpx.Response(
        403,
        json={
            "error": {
                "code": 403,
                "message": "m",
                "errors": [{"reason": "other"}, {"reason": "quotaExceeded"}],
            }
        },
    )
    assert isinstance(youtube.youtube_api_error(resp, "x"), RateLimitedError)


def test_signup_required_message_is_actionable():
    err = youtube.youtube_api_error(gerr(401, "youtubeSignupRequired"), "upload")
    assert "no YouTube channel" in err.message


def test_non_json_error_body_is_handled():
    err = youtube.youtube_api_error(httpx.Response(502, text="<html>Bad gateway</html>"), "upload")
    assert isinstance(err, RetryableError) and err.code is None


# --------------------------------------------------------------------------- poll


def poll_with(mock: Any, payload: dict | httpx.Response, state: dict | None = None):
    route = mock.get(f"{API}/videos")
    route.mock(
        return_value=payload if isinstance(payload, httpx.Response) else httpx.Response(200, json=payload)
    )
    result = youtube.publisher.poll(ctx(), state or {"video_id": "vid123", "requested_privacy": "public"})
    return result, route


def test_poll_processed_is_published_with_shorts_permalink(mock: Any):
    item = {"id": "vid123", "status": {"uploadStatus": "processed", "privacyStatus": "public"}}
    result, route = poll_with(mock, {"items": [item]})
    assert result.outcome == PublishOutcome.PUBLISHED
    assert result.permalink == "https://www.youtube.com/shorts/vid123"
    assert result.platform_post_id == "vid123" and result.message == ""
    req = route.calls.last.request
    assert req.url.params["part"] == "status,processingDetails"
    assert req.url.params["id"] == "vid123"
    assert req.headers["authorization"] == f"Bearer {ACCESS}"


def test_poll_processed_but_forced_private_explains(mock: Any):
    item = {"id": "vid123", "status": {"uploadStatus": "processed", "privacyStatus": "private"}}
    result, _ = poll_with(mock, {"items": [item]})
    assert result.outcome == PublishOutcome.PUBLISHED and "private" in result.message


def test_poll_uploaded_is_processing_with_estimated_delay(mock: Any):
    item = {
        "id": "vid123",
        "status": {"uploadStatus": "uploaded"},
        "processingDetails": {
            "processingStatus": "processing",
            "processingProgress": {"timeLeftMs": "120000"},
        },
    }
    result, _ = poll_with(mock, {"items": [item]})
    assert result.outcome == PublishOutcome.PROCESSING and result.poll_after_seconds == 120
    result, _ = poll_with(mock, {"items": [{"id": "vid123", "status": {"uploadStatus": "uploaded"}}]})
    assert result.poll_after_seconds == 60


@pytest.mark.parametrize(
    ("status", "processing", "needle"),
    [
        ({"uploadStatus": "rejected", "rejectionReason": "duplicate"}, {}, "duplicate"),
        ({"uploadStatus": "rejected", "rejectionReason": "copyright"}, {}, "copyright"),
        ({"uploadStatus": "failed", "failureReason": "codec"}, {}, "codec"),
        ({"uploadStatus": "deleted"}, {}, "deleted"),
        (
            {"uploadStatus": "uploaded"},
            {"processingStatus": "failed", "processingFailureReason": "transcodeFailed"},
            "transcodeFailed",
        ),
    ],
)
def test_poll_terminal_failures_are_permanent(mock: Any, status: dict, processing: dict, needle: str):
    with pytest.raises(PermanentError) as ei:
        poll_with(mock, {"items": [{"id": "vid123", "status": status, "processingDetails": processing}]})
    assert needle in ei.value.message


def test_poll_missing_video_gives_up_after_three_polls(mock: Any):
    state = {"video_id": "vid123", "requested_privacy": "public"}
    for expected in (1, 2):
        result, _ = poll_with(mock, {"items": []}, state)
        assert result.outcome == PublishOutcome.PROCESSING
        assert result.state["not_found_polls"] == expected
        state = result.state
    with pytest.raises(PermanentError) as ei:
        poll_with(mock, {"items": []}, state)
    assert ei.value.code == "videoNotFound"


def test_poll_clears_not_found_counter_once_found(mock: Any):
    item = {"id": "vid123", "status": {"uploadStatus": "uploaded"}}
    result, _ = poll_with(mock, {"items": [item]}, {"video_id": "vid123", "not_found_polls": 2})
    assert "not_found_polls" not in result.state


def test_poll_http_errors(mock: Any):
    with pytest.raises(PermanentError):
        poll_with(mock, gerr(404, "videoNotFound"))
    with pytest.raises(AuthError):
        poll_with(mock, gerr(401, "authError"))
    with pytest.raises(RetryableError):
        poll_with(mock, gerr(503, "backendError"))
    with pytest.raises(RetryableError):
        poll_with(mock, httpx.Response(200, text="not json"))


def test_poll_network_error_is_retryable(mock: Any):
    mock.get(f"{API}/videos").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(RetryableError) as ei:
        youtube.publisher.poll(ctx(), {"video_id": "vid123"})
    assert_no_secrets(ei.value)


def test_poll_without_video_id_is_permanent():
    with pytest.raises(PermanentError):
        youtube.publisher.poll(ctx(), {})


# --------------------------------------------------------------------------- lister


def playlist_item(video_id: str, published: str, privacy: str = "public") -> dict:
    return {
        "snippet": {"publishedAt": published, "resourceId": {"kind": "youtube#video", "videoId": video_id}},
        "contentDetails": {"videoId": video_id, "videoPublishedAt": published},
        "status": {"privacyStatus": privacy},
    }


def video_item(video_id: str, duration: str, published: str, live: str = "none") -> dict:
    return {
        "id": video_id,
        "snippet": {
            "publishedAt": published,
            "channelId": "UCchan",
            "title": f"Title {video_id}",
            "description": f"Caption {video_id}",
            "liveBroadcastContent": live,
        },
        "contentDetails": {"duration": duration},
    }


class FakeYouTubeApi:
    """playlistItems.list + videos.list over an in-memory list (newest first)."""

    def __init__(self, items: list[dict], videos: dict[str, dict], page_size: int = 3):
        self.items = items
        self.videos = videos
        self.page_size = page_size
        self.playlist_requests: list[httpx.Request] = []
        self.video_requests: list[httpx.Request] = []

    def playlist(self, request: httpx.Request) -> httpx.Response:
        self.playlist_requests.append(request)
        assert request.url.params["playlistId"] == "UUchan"
        assert request.url.params["maxResults"] == "50"
        start = int(request.url.params.get("pageToken", "0"))
        page = self.items[start : start + self.page_size]
        body: dict[str, Any] = {"items": page}
        if start + self.page_size < len(self.items):
            body["nextPageToken"] = str(start + self.page_size)
        return httpx.Response(200, json=body)

    def video_list(self, request: httpx.Request) -> httpx.Response:
        self.video_requests.append(request)
        assert request.url.params["part"] == "contentDetails,snippet"
        ids = request.url.params["id"].split(",")
        assert len(ids) <= 50
        return httpx.Response(200, json={"items": [self.videos[i] for i in ids if i in self.videos]})

    def install(self, mock: Any) -> None:
        mock.get(f"{API}/playlistItems").mock(side_effect=self.playlist)
        mock.get(f"{API}/videos").mock(side_effect=self.video_list)


def ts(day: int) -> str:
    return f"2026-10-{day:02d}T12:00:00Z"


@pytest.fixture
def channel_api(mock: Any) -> FakeYouTubeApi:
    items = [
        playlist_item("v10", ts(10)),
        playlist_item("v9", ts(9), privacy="private"),
        playlist_item("v8", ts(8)),
        playlist_item("v7", ts(7), privacy="unlisted"),
        playlist_item("v6", ts(6)),
        playlist_item("v5", ts(5)),
        playlist_item("v4", ts(4)),
    ]
    videos = {
        "v10": video_item("v10", "PT45S", ts(10)),
        "v9": video_item("v9", "PT30S", ts(9)),
        "v8": video_item("v8", "PT3M", ts(8)),
        "v7": video_item("v7", "PT3M1S", ts(7)),
        # v6 deleted between the two calls: missing from videos.list
        "v5": video_item("v5", "P0D", ts(5), live="upcoming"),
        "v4": video_item("v4", "PT1H", ts(4)),
    }
    api = FakeYouTubeApi(items, videos)
    api.install(mock)
    return api


def test_list_recent_pages_parses_durations_and_permalinks(channel_api: FakeYouTubeApi):
    videos = youtube.lister.list_recent(ctx())
    assert [v.external_id for v in videos] == ["v10", "v8", "v7", "v4"]
    v10, v8, v7, v4 = videos
    assert v10.duration_s == 45 and v10.permalink == "https://www.youtube.com/shorts/v10"
    assert v8.duration_s == 180 and v8.permalink == "https://www.youtube.com/shorts/v8"
    assert v7.duration_s == 181 and v7.permalink == "https://www.youtube.com/watch?v=v7"
    assert v4.duration_s == 3600 and v4.permalink == "https://www.youtube.com/watch?v=v4"
    assert all(v.download_url is None for v in videos)
    assert v10.title == "Title v10" and v10.caption == "Caption v10"
    assert v10.created_at == datetime(2026, 10, 10, 12, tzinfo=UTC)
    assert v7.extra == {"channel_id": "UCchan", "privacy_status": "unlisted"}
    assert len(channel_api.playlist_requests) == 3
    assert channel_api.video_requests[0].headers["authorization"] == f"Bearer {ACCESS}"


def test_list_recent_stops_at_since(channel_api: FakeYouTubeApi):
    videos = youtube.lister.list_recent(ctx(), since=datetime(2026, 10, 7, 12, tzinfo=UTC))
    assert [v.external_id for v in videos] == ["v10", "v8", "v7"]
    assert len(channel_api.playlist_requests) == 2  # stopped inside page 2 at v6


def test_list_recent_respects_limit_and_include_private(channel_api: FakeYouTubeApi):
    videos = youtube.lister.list_recent(ctx(), limit=2, config={"include_private": True})
    assert [v.external_id for v in videos] == ["v10", "v9"]
    assert len(channel_api.playlist_requests) == 1


def test_list_recent_zero_limit_makes_no_calls(channel_api: FakeYouTubeApi):
    assert youtube.lister.list_recent(ctx(), limit=0) == []
    assert channel_api.playlist_requests == []


def test_list_recent_batches_video_details_by_50(mock: Any):
    items = [
        playlist_item(f"id{i:03d}", f"2026-09-01T00:{i // 60:02d}:{i % 60:02d}Z") for i in range(119, 59, -1)
    ]
    videos = {
        f"id{i:03d}": video_item(f"id{i:03d}", "PT20S", f"2026-09-01T00:{i // 60:02d}:{i % 60:02d}Z")
        for i in range(60, 120)
    }
    api = FakeYouTubeApi(items, videos, page_size=50)
    api.install(mock)
    result = youtube.lister.list_recent(ctx(), limit=100)
    assert len(result) == 60
    assert [len(r.url.params["id"].split(",")) for r in api.video_requests] == [50, 10]
    assert [v.external_id for v in result] == [f"id{i:03d}" for i in range(119, 59, -1)]


def test_list_recent_looks_up_uploads_playlist_when_missing(mock: Any):
    channels = mock.get(f"{API}/channels").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"id": "UCchan", "contentDetails": {"relatedPlaylists": {"uploads": "UUchan"}}}]},
        )
    )
    api = FakeYouTubeApi([playlist_item("v1", ts(1))], {"v1": video_item("v1", "PT10S", ts(1))})
    api.install(mock)
    videos = youtube.lister.list_recent(ctx(extra={"channel_id": "UCchan"}))
    assert [v.external_id for v in videos] == ["v1"]
    params = channels.calls.last.request.url.params
    assert params["id"] == "UCchan" and params["part"] == "contentDetails"


def test_list_recent_channel_not_found(mock: Any):
    mock.get(f"{API}/channels").mock(return_value=httpx.Response(200, json={"items": []}))
    with pytest.raises(PermanentError):
        youtube.lister.list_recent(ctx(extra={}))


def test_list_recent_maps_api_errors(mock: Any):
    mock.get(f"{API}/playlistItems").mock(return_value=gerr(404, "playlistNotFound"))
    with pytest.raises(PermanentError) as ei:
        youtube.lister.list_recent(ctx())
    assert ei.value.code == "playlistNotFound"


def test_download_is_not_supported(tmp_path: Path):
    with pytest.raises(NotSupportedError) as ei:
        youtube.lister.download(ctx(), RemoteVideo(external_id="v1"), tmp_path / "x.mp4")
    assert "Developer Policies" in ei.value.message
    assert isinstance(ei.value, PlatformError)
