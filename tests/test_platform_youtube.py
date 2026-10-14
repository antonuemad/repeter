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
    RestartPublish,
    RetryableError,
    UnknownOutcomeError,
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
    """A valid request. YouTube has no default audience answer, so the rule settings always carry
    an explicit self_declared_made_for_kids unless a test overrides it."""
    kw["settings"] = {"self_declared_made_for_kids": False, **kw.get("settings", {})}
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


def test_title_falls_back_to_first_caption_line_and_never_to_a_constant():
    assert youtube.build_title("", "\n  \n<>\nFirst real line\nsecond") == "First real line"
    # No title and no caption text: no invented default (Developer Policies III.C.3).
    assert youtube.build_title(None, "<<>>") == ""
    assert youtube.build_title("   ", None) == ""
    assert not hasattr(youtube, "FALLBACK_TITLE")


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


def test_publish_gives_up_after_repeated_5xx_before_the_last_chunk(
    mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
):
    monkeypatch.setattr(youtube, "CHUNK_SIZE", KIB256)
    path = binary_file(tmp_path, 3 * KIB256)
    server = FakeUploadServer(3 * KIB256, script={i: 503 for i in range(1, 20)})
    upload_routes(mock, server)

    with pytest.raises(RetryableError) as ei:  # the last byte was never sent: nothing can exist
        youtube.publisher.publish(ctx(), make_req(path))

    assert not isinstance(ei.value, RateLimitedError | UnknownOutcomeError)
    assert len(sleeps) == youtube.MAX_UPLOAD_RETRIES
    assert len(server.calls) == youtube.MAX_UPLOAD_RETRIES + 1
    assert sleeps == sorted(sleeps)  # exponential backoff
    assert_no_secrets(ei.value)


def test_publish_long_retry_after_is_returned_instead_of_sleeping(
    mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
):
    monkeypatch.setattr(youtube, "CHUNK_SIZE", KIB256)
    path = binary_file(tmp_path, 3 * KIB256)
    upload_routes(mock, FakeUploadServer(3 * KIB256))
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


def test_publish_without_video_id_in_response_may_be_live(mock: Any, tmp_path: Path, sleeps: list[float]):
    path = binary_file(tmp_path, 100)
    upload_routes(mock, FakeUploadServer(100, video={"status": {"uploadStatus": "uploaded"}}))
    with pytest.raises(UnknownOutcomeError) as ei:
        youtube.publisher.publish(ctx(), make_req(path))
    # No state: the engine ends in needs-attention ("may be live") instead of retrying.
    assert ei.value.code == "no_video_id" and ei.value.state == {}


def test_publish_reports_immediate_rejection(mock: Any, tmp_path: Path, sleeps: list[float]):
    path = binary_file(tmp_path, 100)
    video = {"id": "v9", "status": {"uploadStatus": "rejected", "rejectionReason": "duplicate"}}
    upload_routes(mock, FakeUploadServer(100, video=video))
    with pytest.raises(PermanentError) as ei:
        youtube.publisher.publish(ctx(), make_req(path))
    assert ei.value.code == "duplicate" and "duplicate" in ei.value.message


# --------------------------------------------------------------------------- unknown outcome and resume


def lost_final_response_server(size: int) -> FakeUploadServer:
    """The only chunk is stored, its 201 is lost, and every status query fails: YouTube has
    created the video but the adapter cannot find out within one publish() call."""
    script: dict[int, Any] = {
        1: "network_after",
        **{i: "network" for i in range(2, 2 + youtube.MAX_UPLOAD_RETRIES)},
    }
    return FakeUploadServer(size, script=script)


def test_lost_final_response_is_an_unknown_outcome_carrying_the_session(
    mock: Any, tmp_path: Path, sleeps: list[float]
):
    path = binary_file(tmp_path, 4000)
    server = lost_final_response_server(4000)
    start_route, _ = upload_routes(mock, server)

    with pytest.raises(UnknownOutcomeError) as ei:
        youtube.publisher.publish(ctx(), make_req(path, settings={"privacy_status": "unlisted"}))

    err = ei.value
    assert err.state == {"session_uri": SESSION_URI, "total": 4000, "requested_privacy": "unlisted"}
    assert [c[0] for c in server.calls] == ["chunk"] + ["probe"] * youtube.MAX_UPLOAD_RETRIES
    assert start_route.call_count == 1
    assert_no_secrets(err)  # the session URI is state, never message text


def test_poll_resolves_the_session_to_the_created_video_without_a_second_upload(
    mock: Any, tmp_path: Path, sleeps: list[float]
):
    path = binary_file(tmp_path, 4000)
    server = lost_final_response_server(4000)
    start_route, _ = upload_routes(mock, server)
    with pytest.raises(UnknownOutcomeError) as ei:
        youtube.publisher.publish(ctx(), make_req(path))

    result = youtube.publisher.poll(ctx(), ei.value.state)  # the network is back: 201 replays the video

    assert result.outcome == PublishOutcome.PROCESSING and result.platform_post_id == "vid123"
    assert result.state == {"video_id": "vid123", "requested_privacy": "public"}
    assert start_route.call_count == 1  # one videos.insert call in total
    assert server.calls[-1] == ("probe", "bytes */4000", 0)
    # and the next poll is the ordinary videos.list status check
    item = {"id": "vid123", "status": {"uploadStatus": "processed", "privacyStatus": "public"}}
    done, _ = poll_with(mock, {"items": [item]}, result.state)
    assert done.outcome == PublishOutcome.PUBLISHED


def test_unreadable_2xx_body_after_the_final_chunk_is_an_unknown_outcome(
    mock: Any, tmp_path: Path, sleeps: list[float]
):
    path = binary_file(tmp_path, 3000)
    start_route, _ = upload_routes(mock, FakeUploadServer(3000))
    mock.route(method="PUT", host="www.googleapis.com", path="/upload/youtube/v3/videos").mock(
        return_value=httpx.Response(201, text="<html>proxy page</html>")
    )

    with pytest.raises(UnknownOutcomeError) as ei:
        youtube.publisher.publish(ctx(), make_req(path))

    assert ei.value.state["session_uri"] == SESSION_URI and ei.value.state["total"] == 3000
    assert start_route.call_count == 1


def test_lost_reply_then_rate_limited_probe_is_still_unknown(mock: Any, tmp_path: Path, sleeps: list[float]):
    path = binary_file(tmp_path, 3000)
    script: dict[int, Any] = {1: "network_after", **{i: 429 for i in range(2, 20)}}
    upload_routes(mock, FakeUploadServer(3000, script=script))
    with pytest.raises(UnknownOutcomeError) as ei:
        youtube.publisher.publish(ctx(), make_req(path))
    assert ei.value.state["session_uri"] == SESSION_URI


def test_lost_reply_then_rejected_token_on_a_probe_is_still_unknown(
    mock: Any, tmp_path: Path, sleeps: list[float]
):
    path = binary_file(tmp_path, 3000)
    script: dict[int, Any] = {1: "network_after", 2: 401}
    server = FakeUploadServer(3000, script=script)
    upload_routes(mock, server)
    with pytest.raises(UnknownOutcomeError) as ei:
        youtube.publisher.publish(ctx(), make_req(path))
    assert ei.value.state["session_uri"] == SESSION_URI


def test_clear_error_answer_to_the_final_chunk_stays_a_plain_error(
    mock: Any, tmp_path: Path, sleeps: list[float]
):
    path = binary_file(tmp_path, 3000)
    upload_routes(mock, FakeUploadServer(3000))
    mock.route(method="PUT", host="www.googleapis.com", path="/upload/youtube/v3/videos").mock(
        return_value=gerr(429, "rateLimitExceeded")
    )
    with pytest.raises(RateLimitedError):
        youtube.publisher.publish(ctx(), make_req(path))


def test_failure_before_the_final_chunk_is_retryable_and_uses_a_new_session_next_time(
    mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
):
    monkeypatch.setattr(youtube, "CHUNK_SIZE", KIB256)
    path = binary_file(tmp_path, 3 * KIB256)
    script: dict[int, Any] = {i: "network" for i in range(2, 20)}
    upload_routes(mock, FakeUploadServer(3 * KIB256, script=script))
    with pytest.raises(RetryableError) as ei:
        youtube.publisher.publish(ctx(), make_req(path))
    assert not isinstance(ei.value, UnknownOutcomeError)


def poll_session(mock: Any, response: httpx.Response | Exception, state: dict | None = None):
    route = mock.route(method="PUT", host="www.googleapis.com", path="/upload/youtube/v3/videos")
    route.mock(side_effect=response) if isinstance(response, Exception) else route.mock(return_value=response)
    session_state = state or {"session_uri": SESSION_URI, "total": 4000, "requested_privacy": "public"}
    try:
        return youtube.publisher.poll(ctx(), session_state), route
    finally:
        assert route.calls.last.request.headers["content-range"] == "bytes */4000"
        assert route.calls.last.request.headers["authorization"] == f"Bearer {ACCESS}"


def test_poll_session_404_proves_nothing_was_created(mock: Any):
    with pytest.raises(RestartPublish) as ei:
        poll_session(mock, httpx.Response(404))
    assert ei.value.code == "session_expired"
    assert "SESSION1" not in ei.value.message


def test_poll_session_partial_308_proves_nothing_was_created(mock: Any):
    with pytest.raises(RestartPublish) as ei:
        poll_session(mock, httpx.Response(308, headers={"Range": "bytes=0-1999"}))
    assert ei.value.code == "upload_incomplete"
    with pytest.raises(RestartPublish):
        poll_session(mock, httpx.Response(308))  # no Range header: nothing received


def test_poll_session_308_with_every_byte_keeps_waiting(mock: Any):
    state = {"session_uri": SESSION_URI, "total": 4000, "requested_privacy": "public"}
    result, _ = poll_session(mock, httpx.Response(308, headers={"Range": "bytes=0-3999"}), state)
    assert result.outcome == PublishOutcome.PROCESSING and result.state == state


def test_poll_session_transient_failures_poll_again(mock: Any):
    with pytest.raises(RetryableError) as ei:
        poll_session(mock, httpx.ConnectError("down"))
    assert not isinstance(ei.value, RestartPublish) and "SESSION1" not in ei.value.message
    with pytest.raises(RetryableError) as ei:
        poll_session(mock, gerr(503, "backendError", headers={"Retry-After": "90"}))
    assert not isinstance(ei.value, RestartPublish) and ei.value.retry_after == 90
    with pytest.raises(RetryableError):
        poll_session(mock, httpx.Response(200, text="not json"))


def test_poll_session_replays_the_original_error_of_a_finished_upload(mock: Any):
    with pytest.raises(PermanentError) as ei:
        poll_session(mock, gerr(400, "invalidTitle"))
    assert ei.value.code == "invalidTitle"
    with pytest.raises(RateLimitedError):
        poll_session(mock, gerr(403, "quotaExceeded"))
    with pytest.raises(PermanentError) as ei:
        poll_session(mock, httpx.Response(201, json={"status": {}}))
    assert ei.value.code == "no_video_id"


def test_poll_session_state_without_a_usable_uri_is_permanent():
    with pytest.raises(PermanentError):
        youtube.publisher.poll(ctx(), {"session_uri": "http://insecure/x", "total": 10})
    with pytest.raises(PermanentError):
        youtube.publisher.poll(ctx(), {"session_uri": SESSION_URI})


def test_publish_resumes_a_session_from_state_instead_of_starting_a_new_upload(
    mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
):
    monkeypatch.setattr(youtube, "CHUNK_SIZE", KIB256)
    size = 3 * KIB256
    path = binary_file(tmp_path, size)
    server = FakeUploadServer(size)
    server.received += path.read_bytes()[: 2 * KIB256]  # an earlier attempt delivered two chunks
    start_route, _ = upload_routes(mock, server)
    state = {"session_uri": SESSION_URI, "total": size, "requested_privacy": "public"}

    result = youtube.publisher.publish(ctx(), make_req(path, state=state))

    assert start_route.call_count == 0
    assert [c[0] for c in server.calls] == ["probe", "chunk"]
    assert server.calls[1][1] == f"bytes {2 * KIB256}-{size - 1}/{size}"
    assert result.platform_post_id == "vid123"
    assert bytes(server.received) == path.read_bytes()


def test_publish_resume_of_a_finished_session_returns_the_video_without_sending_bytes(
    mock: Any, tmp_path: Path, sleeps: list[float]
):
    path = binary_file(tmp_path, 4000)
    server = FakeUploadServer(4000)
    server.received += path.read_bytes()
    start_route, _ = upload_routes(mock, server)
    state = {"session_uri": SESSION_URI, "total": 4000, "requested_privacy": "public"}

    result = youtube.publisher.publish(ctx(), make_req(path, state=state))

    assert result.platform_post_id == "vid123" and start_route.call_count == 0
    assert [c[0] for c in server.calls] == ["probe"]


def test_publish_resume_of_an_expired_session_starts_one_new_session(
    mock: Any, tmp_path: Path, sleeps: list[float]
):
    path = binary_file(tmp_path, 3000)
    server = FakeUploadServer(3000, script={1: 404})
    start_route, _ = upload_routes(mock, server)
    state = {"session_uri": SESSION_URI, "total": 3000}

    result = youtube.publisher.publish(ctx(), make_req(path, state=state))

    assert start_route.call_count == 1 and result.platform_post_id == "vid123"
    assert [c[0] for c in server.calls] == ["probe", "chunk"]


def test_publish_resume_failure_keeps_the_outcome_unknown(mock: Any, tmp_path: Path, sleeps: list[float]):
    path = binary_file(tmp_path, 3000)
    server = FakeUploadServer(3000, script={i: "network" for i in range(1, 20)})
    start_route, _ = upload_routes(mock, server)
    state = {"session_uri": SESSION_URI, "total": 3000, "requested_privacy": "public"}

    with pytest.raises(UnknownOutcomeError) as ei:
        youtube.publisher.publish(ctx(), make_req(path, state=state))

    assert ei.value.state == state and start_route.call_count == 0


def test_publish_ignores_a_session_of_a_different_file(mock: Any, tmp_path: Path, sleeps: list[float]):
    path = binary_file(tmp_path, 3000)
    server = FakeUploadServer(3000)
    start_route, _ = upload_routes(mock, server)
    youtube.publisher.publish(ctx(), make_req(path, state={"session_uri": SESSION_URI, "total": 999}))
    assert start_route.call_count == 1 and [c[0] for c in server.calls] == ["chunk"]


def test_publish_with_a_video_in_state_only_checks_its_status(mock: Any, tmp_path: Path):
    path = binary_file(tmp_path, 3000)
    start_route, put = upload_routes(mock, FakeUploadServer(3000))
    item = {"id": "vid123", "status": {"uploadStatus": "processed", "privacyStatus": "public"}}
    mock.get(f"{API}/videos").mock(return_value=httpx.Response(200, json={"items": [item]}))

    result = youtube.publisher.publish(
        ctx(), make_req(path, state={"video_id": "vid123", "requested_privacy": "public"})
    )

    assert result.outcome == PublishOutcome.PUBLISHED and result.platform_post_id == "vid123"
    assert not start_route.called and not put.called


# --------------------------------------------------------------------------- titles are never invented


def test_validate_requires_a_title_instead_of_inventing_one(sample_video: Path):
    problems = youtube.publisher.validate(ctx(), make_req(sample_video, title="", caption=""))
    assert problems == [youtube.TITLE_REQUIRED_MESSAGE]
    assert "title" in problems[0].lower()
    assert youtube.publisher.validate(ctx(), make_req(sample_video, title="", caption="Real line")) == []


def test_publish_without_any_title_never_sends_a_default(mock: Any, tmp_path: Path):
    path = binary_file(tmp_path, 100)
    start_route, put = upload_routes(mock, FakeUploadServer(100))
    with pytest.raises(PermanentError) as ei:
        youtube.publisher.publish(ctx(), make_req(path, title="", caption="<>"))
    assert ei.value.code == "invalid_settings" and "title" in ei.value.message.lower()
    assert not start_route.called and not put.called


# --------------------------------------------------------------------------- quota reset and refresh


def test_quota_exhaustion_waits_until_the_next_pacific_midnight_plus_margin(
    mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(youtube, "utcnow", lambda: datetime(2026, 10, 14, 22, 30, tzinfo=UTC))  # 15:30 PDT
    path = binary_file(tmp_path, 100)
    upload_routes(mock, FakeUploadServer(100), start=lambda r: gerr(403, "quotaExceeded"))

    with pytest.raises(RateLimitedError) as ei:
        youtube.publisher.publish(ctx(), make_req(path))

    # midnight PDT = 07:00 UTC the next day: 8.5 h away, plus the 5 minute safety margin
    assert ei.value.retry_after == pytest.approx(8.5 * 3600 + 300)
    assert ei.value.code == "quotaExceeded"


def test_quota_exhaustion_on_a_status_check_carries_the_same_wait(mock: Any, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(youtube, "utcnow", lambda: datetime(2026, 10, 14, 23, 55, tzinfo=UTC))  # 16:55 PDT
    with pytest.raises(RateLimitedError) as ei:
        poll_with(mock, gerr(403, "quotaExceeded"))
    assert ei.value.retry_after == pytest.approx(7 * 3600 + 5 * 60 + 300)


def channel_item(channel_id: str, title: str, **extra: Any) -> dict:
    return {
        "id": channel_id,
        "snippet": {
            "title": title,
            "customUrl": extra.get("custom_url"),
            "thumbnails": {"default": {"url": f"https://yt3.example/{channel_id}-d.jpg"}}
            | ({"medium": {"url": f"https://yt3.example/{channel_id}-m.jpg"}} if extra.get("medium") else {}),
        },
        "contentDetails": {"relatedPlaylists": {"uploads": "UU" + channel_id[2:]}},
    }


def test_refresh_account_info_reads_the_connected_channel_with_mine(mock: Any):
    route = mock.get(f"{API}/channels").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    channel_item("UCother", "Other channel"),
                    channel_item("UCchan", "Magic Lap Renamed", custom_url="@magiclap", medium=True),
                ]
            },
        )
    )

    info = youtube.refresh_account_info(ctx())

    assert info == {
        "display_name": "Magic Lap Renamed",
        "avatar_url": "https://yt3.example/UCchan-m.jpg",
        "extra": {"channel_id": "UCchan", "uploads_playlist_id": "UUchan", "custom_url": "@magiclap"},
    }
    params = route.calls.last.request.url.params
    assert params["mine"] == "true" and "snippet" in params["part"] and "contentDetails" in params["part"]
    assert route.calls.last.request.headers["authorization"] == f"Bearer {ACCESS}"


def test_refresh_account_info_falls_back_to_the_external_id(mock: Any):
    mock.get(f"{API}/channels").mock(
        return_value=httpx.Response(200, json={"items": [channel_item("UCchan", "Only channel")]})
    )
    info = youtube.refresh_account_info(ctx(extra={}))
    assert info["display_name"] == "Only channel" and info["avatar_url"] == "https://yt3.example/UCchan-d.jpg"


def test_refresh_account_info_reports_a_channel_that_is_gone(mock: Any):
    mock.get(f"{API}/channels").mock(
        return_value=httpx.Response(200, json={"items": [channel_item("UCother", "Someone else")]})
    )
    with pytest.raises(PermanentError) as ei:
        youtube.refresh_account_info(ctx())
    assert ei.value.code == "channelNotFound"
    mock.get(f"{API}/channels").mock(return_value=httpx.Response(200, json={"items": []}))
    with pytest.raises(PermanentError):
        youtube.refresh_account_info(ctx())


def test_refresh_account_info_maps_api_errors(mock: Any):
    route = mock.get(f"{API}/channels")
    route.mock(return_value=gerr(401, "authError"))
    with pytest.raises(AuthError):
        youtube.refresh_account_info(ctx())
    route.mock(return_value=gerr(403, "quotaExceeded"))
    with pytest.raises(RateLimitedError):
        youtube.refresh_account_info(ctx())
    route.mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(RetryableError) as ei:
        youtube.refresh_account_info(ctx())
    assert_no_secrets(ei.value)


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


def test_every_insert_attempt_reserves_quota_including_internal_retries_and_restarts(mock, tmp_path, sleeps):
    path = binary_file(tmp_path, 3000)
    start, _ = upload_routes(mock, FakeUploadServer(3000, script={1: 404}))
    reserved = []

    def reply(request):
        assert len(reserved) == start.call_count + 1
        if len(reserved) == 1:
            return httpx.Response(503)
        return httpx.Response(200, headers={"Location": SESSION_URI})

    start.mock(side_effect=reply)
    youtube.publisher.publish(ctx(), make_req(path, on_session_start=lambda: reserved.append(True)))
    assert start.call_count == len(reserved) == 3


def test_quota_reservation_refusal_happens_before_any_insert_request(mock, tmp_path):
    path = binary_file(tmp_path, 3000)
    start, _ = upload_routes(mock, FakeUploadServer(3000))

    def refuse():
        raise RateLimitedError("project exhausted", code="quotaExceeded", retry_after=3600)

    with pytest.raises(RateLimitedError):
        youtube.publisher.publish(ctx(), make_req(path, on_session_start=refuse))
    assert not start.called
