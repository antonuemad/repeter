"""Tests for import sources (Drive, yt-dlp), the poll/import/process jobs and manual uploads."""

from __future__ import annotations

import io
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select
from yt_dlp.networking import Response as YtResponse
from yt_dlp.networking.exceptions import HTTPError as YtHTTPError
from yt_dlp.networking.exceptions import TransportError
from yt_dlp.utils import DownloadError, ExtractorError, GeoRestrictedError, UnsupportedError

from repeater import queue
from repeater.config import get_settings
from repeater.jobs import kinds
from repeater.jobs.registry import HANDLERS
from repeater.media import encode, storage
from repeater.media.fingerprint import phash_frames, sha256_file
from repeater.media.probe import ProbeResult, probe
from repeater.models import (
    AccountStatus,
    ActivityLevel,
    ActivityLog,
    Job,
    JobStatus,
    Platform,
    Source,
    SourceKind,
    Video,
    VideoStatus,
)
from repeater.platforms import override
from repeater.platforms.base import (
    AccountContext,
    AuthError,
    NotSupportedError,
    PermanentError,
    RateLimitedError,
    RemoteVideo,
    RetryableError,
)
from repeater.queue import RetryLater
from repeater.sources import drive, importer, manual, ytdlp
from repeater.sources import jobs as source_jobs
from repeater.timeutil import utcnow

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
FOLDER_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
FILES_URL = "https://www.googleapis.com/drive/v3/files"


# =========================================================================== fixtures & fakes


@pytest.fixture(autouse=True)
def clean_media() -> None:
    root = storage.media_root()
    for child in root.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()


@pytest.fixture
def fast_encode(monkeypatch):
    """Same profile, ultrafast preset: keeps end-to-end tests quick."""
    args = list(encode.VIDEO_CODEC_ARGS)
    args[args.index("slow")] = "ultrafast"
    monkeypatch.setattr(encode, "VIDEO_CODEC_ARGS", tuple(args))


@pytest.fixture
def notifications(monkeypatch) -> list[tuple[str, dict]]:
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(importer.notify_mod, "notify", lambda text, **kw: sent.append((text, kw)) or True)
    return sent


@pytest.fixture
def make_source(db, workspace):
    def _make(kind: SourceKind, platform: Platform | None, *, account=None, config=None, **kw) -> Source:
        src = Source(
            workspace_id=workspace.id,
            kind=kind,
            platform=platform,
            connected_account_id=account.id if account else None,
            name=kw.pop("name", f"{kind} source"),
            config=config if config is not None else {},
            **kw,
        )
        db.add(src)
        db.commit()
        return src

    return _make


def remote(ext_id: str, created: datetime | None, **kw) -> RemoteVideo:
    return RemoteVideo(
        external_id=ext_id,
        created_at=created,
        title=kw.pop("title", f"title {ext_id}"),
        caption=kw.pop("caption", f"caption {ext_id}"),
        permalink=kw.pop("permalink", f"https://example.test/{ext_id}"),
        download_url=kw.pop("download_url", f"https://cdn.example.test/{ext_id}.mp4"),
        **kw,
    )


class FakeLister:
    def __init__(
        self, platform: Platform, items: list[RemoteVideo] | None = None, sample: Path | None = None
    ):
        self.platform = platform
        self.items = items or []
        self.sample = sample
        self.calls: list[dict[str, Any]] = []
        self.downloads: list[RemoteVideo] = []
        self.list_error: Exception | None = None
        self.download_error: Exception | None = None

    def list_recent(self, account, *, since=None, limit=50, config=None):
        self.calls.append({"account": account, "since": since, "limit": limit, "config": config})
        if self.list_error:
            raise self.list_error
        return list(self.items)[:limit]

    def download(self, account, remote_video, dest):
        self.downloads.append(remote_video)
        if self.download_error:
            raise self.download_error
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.sample, dest)
        return dest


def videos(db) -> list[Video]:
    db.expire_all()
    return list(db.execute(select(Video).order_by(Video.id)).scalars())


def jobs_of(db, kind: str) -> list[Job]:
    db.expire_all()
    return list(db.execute(select(Job).where(Job.kind == kind).order_by(Job.id)).scalars())


def activities(db, level: ActivityLevel | None = None) -> list[ActivityLog]:
    db.expire_all()
    q = select(ActivityLog).order_by(ActivityLog.id)
    if level:
        q = q.where(ActivityLog.level == level)
    return list(db.execute(q).scalars())


def run_next_job(db, kind: str) -> tuple[Job, RetryLater | None]:
    """Minimal worker: claim, run the registered handler, complete or reschedule."""
    job = queue.claim_next(db, "test-worker", kinds=[kind])
    assert job is not None, f"no runnable {kind} job"
    db.commit()
    retry = None
    try:
        HANDLERS[job.kind](db, dict(job.payload))
        queue.complete(db, job)
    except RetryLater as exc:
        retry = exc
        db.rollback()
        queue.reschedule(db, job, exc.seconds, exc.reason)
        job.run_at = utcnow()  # make it runnable right away for the test
    db.commit()
    return job, retry


def ctx(token: str = "drive-token") -> AccountContext:
    from repeater.platforms.base import OAuthTokens

    return AccountContext(account_id=1, platform=Platform.GDRIVE, external_id="g", tokens=OAuthTokens(token))


# =========================================================================== Google Drive


@pytest.mark.parametrize(
    "value",
    [
        FOLDER_ID,
        f"https://drive.google.com/drive/folders/{FOLDER_ID}?usp=sharing",
        f"https://drive.google.com/drive/u/0/folders/{FOLDER_ID}",
        f"https://drive.google.com/open?id={FOLDER_ID}",
        f"  https://drive.google.com/drive/folders/{FOLDER_ID}/  ",
    ],
)
def test_parse_folder_id(value):
    assert drive.parse_folder_id(value) == FOLDER_ID


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        "short",
        "https://evil.example/drive/folders/" + FOLDER_ID,
        "abc' or 1=1 or 'x' in parents and 'x",
    ],
)
def test_parse_folder_id_rejects(value):
    with pytest.raises(PermanentError):
        drive.parse_folder_id(value)


def _drive_file(i: int, created: datetime, **kw) -> dict:
    return {
        "id": f"file{i}xxxxxxxxxx",
        "name": kw.get("name", f"clip {i}.MOV"),
        "mimeType": kw.get("mimeType", "video/quicktime"),
        "createdTime": created.isoformat().replace("+00:00", "Z"),
        "size": "12345",
        "webViewLink": f"https://drive.google.com/file/d/file{i}/view",
        **({"description": kw["description"]} if "description" in kw else {}),
    }


@respx.mock
def test_drive_list_recent_paginates_and_maps_files():
    route = respx.get(FILES_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "files": [
                        _drive_file(3, T0, description="  Big launch #magic "),
                        _drive_file(2, T0 - timedelta(hours=1)),
                    ],
                    "nextPageToken": "page-2",
                },
            ),
            httpx.Response(200, json={"files": [_drive_file(1, T0 - timedelta(days=1), name="noext")]}),
        ]
    )
    items = drive.lister.list_recent(
        ctx(), limit=10, config={"folder_id": f"https://drive.google.com/drive/folders/{FOLDER_ID}"}
    )

    assert [i.external_id for i in items] == ["file3xxxxxxxxxx", "file2xxxxxxxxxx", "file1xxxxxxxxxx"]
    first = items[0]
    assert first.caption == "Big launch #magic" and first.title == "clip 3"
    assert items[1].caption == "clip 2"  # no description: file name without extension
    assert first.created_at == T0
    assert first.permalink == "https://drive.google.com/file/d/file3/view"
    assert first.download_url == f"{FILES_URL}/file3xxxxxxxxxx?alt=media&supportsAllDrives=true"
    assert first.extra == {"name": "clip 3.MOV", "mime_type": "video/quicktime", "ext": "mov", "size": 12345}
    assert "ext" not in items[2].extra

    req = route.calls[0].request
    params = dict(req.url.params)
    assert params["q"] == f"'{FOLDER_ID}' in parents and trashed = false and mimeType contains 'video/'"
    assert params["orderBy"] == "createdTime desc"
    assert (
        params["fields"] == "nextPageToken,files(id,name,mimeType,createdTime,size,description,webViewLink)"
    )
    assert params["supportsAllDrives"] == "true" and params["includeItemsFromAllDrives"] == "true"
    assert params["pageSize"] == "10"
    assert req.headers["Authorization"] == "Bearer drive-token"
    assert route.calls[1].request.url.params["pageToken"] == "page-2"


@respx.mock
def test_drive_list_recent_stops_at_since_and_limit():
    files = [_drive_file(i, T0 - timedelta(hours=i)) for i in range(5)]
    respx.get(FILES_URL).mock(return_value=httpx.Response(200, json={"files": files}))
    since_items = drive.lister.list_recent(
        ctx(), since=T0 - timedelta(hours=2, minutes=30), config={"folder_id": FOLDER_ID}
    )
    assert len(since_items) == 3
    assert len(drive.lister.list_recent(ctx(), limit=2, config={"folder_id": FOLDER_ID})) == 2
    assert drive.lister.list_recent(ctx(), limit=0, config={"folder_id": FOLDER_ID}) == []


@respx.mock
@pytest.mark.parametrize(
    ("folder_meta", "error"),
    [
        ({"id": FOLDER_ID, "mimeType": drive.FOLDER_MIME, "trashed": False}, None),
        ({"id": FOLDER_ID, "mimeType": "video/mp4", "trashed": False}, "not a folder"),
        ({"id": FOLDER_ID, "mimeType": drive.FOLDER_MIME, "trashed": True}, "trash"),
    ],
)
def test_drive_empty_listing_checks_the_folder(folder_meta, error):
    respx.get(FILES_URL).mock(return_value=httpx.Response(200, json={"files": []}))
    meta = respx.get(f"{FILES_URL}/{FOLDER_ID}").mock(return_value=httpx.Response(200, json=folder_meta))
    if error is None:
        assert drive.lister.list_recent(ctx(), config={"folder_id": FOLDER_ID}) == []
    else:
        with pytest.raises(PermanentError, match=error):
            drive.lister.list_recent(ctx(), config={"folder_id": FOLDER_ID})
    assert meta.calls[0].request.url.params["supportsAllDrives"] == "true"


def _google_error(status: int, reason: str, message: str = "msg") -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "error": {"code": status, "message": message, "errors": [{"reason": reason, "message": message}]}
        },
    )


@respx.mock
@pytest.mark.parametrize(
    ("response", "exc_type", "fragment"),
    [
        (_google_error(401, "authError", "Invalid Credentials"), AuthError, "Reconnect Google Drive"),
        (_google_error(404, "notFound", "File not found"), PermanentError, "folder was not found"),
        (_google_error(403, "userRateLimitExceeded"), RateLimitedError, "rate limit"),
        (_google_error(429, "rateLimitExceeded"), RateLimitedError, "rate limit"),
        (_google_error(403, "insufficientFilePermissions", "No access"), PermanentError, "No access"),
        (_google_error(500, "backendError"), RetryableError, "HTTP 500"),
        (httpx.Response(400, text="bad"), PermanentError, "HTTP 400"),
    ],
)
def test_drive_list_errors(response, exc_type, fragment):
    respx.get(FILES_URL).mock(return_value=response)
    with pytest.raises(exc_type, match=fragment) as exc:
        drive.lister.list_recent(ctx("secret-token-value"), config={"folder_id": FOLDER_ID})
    assert type(exc.value) is exc_type or issubclass(type(exc.value), exc_type)
    assert "secret-token-value" not in str(exc.value)


@respx.mock
def test_drive_folder_lookup_404_and_network_error():
    respx.get(FILES_URL).mock(return_value=httpx.Response(200, json={"files": []}))
    respx.get(f"{FILES_URL}/{FOLDER_ID}").mock(return_value=_google_error(404, "notFound"))
    with pytest.raises(PermanentError, match="not found"):
        drive.lister.list_recent(ctx(), config={"folder_id": FOLDER_ID})
    respx.get(FILES_URL).mock(side_effect=httpx.ConnectTimeout("boom"))
    with pytest.raises(RetryableError, match="network error"):
        drive.lister.list_recent(ctx(), config={"folder_id": FOLDER_ID})


def test_drive_missing_folder_config():
    with pytest.raises(PermanentError, match="No Google Drive folder"):
        drive.lister.list_recent(ctx(), config={})


@respx.mock
def test_drive_download(tmp_path):
    route = respx.get(f"{FILES_URL}/file1xxxxxxxxxx").mock(
        return_value=httpx.Response(200, content=b"video-bytes")
    )
    rv = RemoteVideo(external_id="file1xxxxxxxxxx", download_url=None)
    out = drive.lister.download(ctx(), rv, tmp_path / "original.mov")
    assert out.read_bytes() == b"video-bytes"
    req = route.calls[0].request
    assert req.url.params["alt"] == "media" and req.url.params["supportsAllDrives"] == "true"
    assert req.headers["Authorization"] == "Bearer drive-token"


@respx.mock
@pytest.mark.parametrize(
    ("status", "exc_type"),
    [(404, PermanentError), (401, AuthError), (403, RetryableError), (503, RetryableError)],
)
def test_drive_download_errors(tmp_path, status, exc_type):
    respx.get(f"{FILES_URL}/file1xxxxxxxxxx").mock(return_value=httpx.Response(status, text="no"))
    rv = RemoteVideo(external_id="file1xxxxxxxxxx", download_url=drive.file_download_url("file1xxxxxxxxxx"))
    with pytest.raises(exc_type) as exc:
        drive.lister.download(ctx("secret-token-value"), rv, tmp_path / "o.mp4")
    assert "secret-token-value" not in str(exc.value)
    assert not (tmp_path / "o.mp4").exists() and not (tmp_path / "o.mp4.part").exists()


def test_drive_download_refuses_foreign_urls(tmp_path):
    rv = RemoteVideo(external_id="x", download_url="https://attacker.example/steal")
    with pytest.raises(PermanentError, match="unexpected address"):
        drive.lister.download(ctx(), rv, tmp_path / "o.mp4")


def test_drive_lister_is_registered():
    from repeater.platforms import get_lister
    from repeater.platforms.base import MediaLister

    assert get_lister("gdrive") is drive.lister
    assert isinstance(drive.lister, MediaLister)


# =========================================================================== yt-dlp


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: records params, returns `info` or raises `error`."""

    instances: list[FakeYDL] = []
    info: Any = None
    error: BaseException | None = None
    write_file: str | None = None

    def __init__(self, params):
        self.params = params
        FakeYDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        self.url, self.download = url, download
        if FakeYDL.error is not None:
            raise FakeYDL.error
        info = FakeYDL.info
        if download and FakeYDL.write_file:
            target = Path(self.params["paths"]["home"]) / FakeYDL.write_file
            target.write_bytes(b"downloaded")
            info = {**info, "requested_downloads": [{"filepath": str(target)}]}
        return info


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYDL.instances, FakeYDL.info, FakeYDL.error, FakeYDL.write_file = [], None, None, None
    monkeypatch.setattr(ytdlp, "YoutubeDL", FakeYDL)
    return FakeYDL


def test_ytdlp_list_profile(fake_ydl, tmp_path):
    fake_ydl.info = {
        "_type": "playlist",
        "entries": [
            {
                "id": "7400000000000000002",
                "url": "https://www.tiktok.com/@magiclab/video/7400000000000000002",
                "title": "New",
                "timestamp": 1_790_000_000,
                "duration": 21,
            },
            {
                "id": "7400000000000000001",
                "url": "https://www.tiktok.com/@magiclab/video/7400000000000000001",
                "description": "Older one",
            },
            {"_type": "playlist", "id": "nested", "url": "https://www.tiktok.com/@x"},
            {"url": "https://www.tiktok.com/@magiclab/video/none"},
            {"id": "yt1", "url": "yt1", "ie_key": "Youtube", "title": "Short"},
        ],
    }
    cookies = tmp_path / "c.txt"
    items = ytdlp.list_profile(
        "https://www.tiktok.com/@magiclab", limit=5, proxy="http://u:p@proxy:8080", cookies_file=cookies
    )

    assert [i.external_id for i in items] == ["7400000000000000002", "7400000000000000001", "yt1"]
    assert items[0].created_at == datetime.fromtimestamp(1_790_000_000, UTC) and items[0].duration_s == 21
    assert items[0].caption == "New" and items[1].caption == "Older one" and items[1].created_at is None
    assert items[2].permalink == "https://www.youtube.com/watch?v=yt1"
    ydl = fake_ydl.instances[0]
    assert ydl.download is False and ydl.url == "https://www.tiktok.com/@magiclab"
    p = ydl.params
    assert p["extract_flat"] == "in_playlist" and p["playlist_items"] == "1:5" and p["skip_download"]
    assert p["proxy"] == "http://u:p@proxy:8080" and p["cookiefile"] == str(cookies)
    assert p["socket_timeout"] == ytdlp.SOCKET_TIMEOUT_S and p["sleep_interval_requests"] >= 1
    assert p["allowed_extractors"] == ytdlp.ALLOWED_EXTRACTORS


def test_ytdlp_list_profile_single_video_link(fake_ydl):
    fake_ydl.info = {"id": "1", "title": "one video"}
    with pytest.raises(PermanentError, match="single video"):
        ytdlp.list_profile("https://www.tiktok.com/@magiclab/video/1")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/@MagicLab", "https://www.youtube.com/@MagicLab/shorts"),
        ("https://youtube.com/channel/UCabc/", "https://youtube.com/channel/UCabc/shorts"),
        ("https://www.youtube.com/@MagicLab/videos", "https://www.youtube.com/@MagicLab/videos"),
        ("https://www.tiktok.com/@magiclab", "https://www.tiktok.com/@magiclab"),
    ],
)
def test_normalize_profile_url(url, expected):
    assert ytdlp.normalize_profile_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://instagram.com/magiclab",
        "file:///etc/passwd",
        "http://localhost/x",
        "https://tiktok.com.evil.example/@a",
        "ftp://youtube.com/x",
    ],
)
def test_ytdlp_rejects_other_hosts(fake_ydl, url, tmp_path):
    with pytest.raises(PermanentError, match="Only TikTok and YouTube"):
        ytdlp.list_profile(url)
    with pytest.raises(PermanentError):
        ytdlp.download(url, tmp_path)
    assert fake_ydl.instances == []


def test_ytdlp_download(fake_ydl, tmp_path):
    fake_ydl.info = {"id": "1", "ext": "mp4"}
    fake_ydl.write_file = "original.mp4"
    out = ytdlp.download("https://www.tiktok.com/@magiclab/video/1", tmp_path / "v1", proxy=None)
    assert out == (tmp_path / "v1" / "original.mp4").resolve() and out.read_bytes() == b"downloaded"
    p = fake_ydl.instances[0].params
    assert p["format"] == "bv*+ba/b" and p["format_sort"] == ["res:1080", "vcodec:h264", "ext:mp4:m4a"]
    assert p["merge_output_format"] == "mp4" and p["noplaylist"] is True
    assert p["outtmpl"] == {"default": "original.%(ext)s"} and p["paths"]["home"] == str(
        (tmp_path / "v1").resolve()
    )
    assert p["sleep_interval"] >= 1 and p["max_sleep_interval"] >= p["sleep_interval"]
    assert p["retries"] == ytdlp.NETWORK_RETRIES and p["max_filesize"] == ytdlp.MAX_FILESIZE
    assert "proxy" not in p and "cookiefile" not in p
    assert Path(p["ffmpeg_location"]).name == "ffmpeg"


def test_ytdlp_download_without_file(fake_ydl, tmp_path):
    fake_ydl.info = {"id": "1"}
    with pytest.raises(PermanentError, match="without a video file"):
        ytdlp.download("https://www.youtube.com/watch?v=1", tmp_path)
    fake_ydl.info = {"_type": "playlist", "entries": []}
    with pytest.raises(PermanentError, match="playlist"):
        ytdlp.download("https://www.youtube.com/watch?v=1", tmp_path)


def _http_error(status: int) -> YtHTTPError:
    return YtHTTPError(YtResponse(io.BytesIO(b""), "https://www.tiktok.com/x", {}, status=status))


def _wrapped(cause: BaseException, msg: str = "ERROR: Unable to download webpage") -> DownloadError:
    try:
        raise cause
    except BaseException:
        import sys

        return DownloadError(msg, sys.exc_info())


@pytest.mark.parametrize(
    ("error", "exc_type", "code"),
    [
        (DownloadError("ERROR: [TikTok] 123: This account is private"), PermanentError, "private"),
        (
            DownloadError("ERROR: [youtube] abc: Private video. Sign in if you've been granted access"),
            PermanentError,
            "private",
        ),
        (
            DownloadError(
                "ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader"
            ),
            PermanentError,
            "removed",
        ),
        (
            DownloadError("ERROR: [youtube] abc: Sign in to confirm your age."),
            PermanentError,
            "login_required",
        ),
        (
            DownloadError("ERROR: [TikTok] 1: Log in for access. Use --cookies"),
            PermanentError,
            "login_required",
        ),
        (
            DownloadError("ERROR: [youtube] abc: Sign in to confirm you\u2019re not a bot."),
            RateLimitedError,
            "bot_check",
        ),
        (
            DownloadError("ERROR: Unsupported URL: https://www.tiktok.com/foo"),
            PermanentError,
            "unsupported_url",
        ),
        (UnsupportedError("https://www.youtube.com/feed"), PermanentError, "unsupported_url"),
        (GeoRestrictedError("This video is not available in your country"), PermanentError, "geo_restricted"),
        (
            DownloadError("ERROR: [youtube] abc: Requested format is not available."),
            PermanentError,
            "format_unavailable",
        ),
        (
            _wrapped(_http_error(429), "ERROR: HTTP Error 429: Too Many Requests"),
            RateLimitedError,
            "rate_limited",
        ),
        (_wrapped(_http_error(503)), RetryableError, "server_error"),
        (
            _wrapped(_http_error(404), "ERROR: Unable to download webpage: HTTP Error 404: Not Found"),
            PermanentError,
            "removed",
        ),
        (_wrapped(_http_error(403), "ERROR: HTTP Error 403: Forbidden"), RetryableError, "blocked"),
        (_wrapped(TransportError("Connection reset by peer")), RetryableError, "network"),
        (ExtractorError("Read timed out.", expected=True), RetryableError, "network"),
        (
            DownloadError("ERROR: [TikTok] 7412942912345: Unable to extract universal data"),
            RetryableError,
            "unknown",
        ),
        (DownloadError("ERROR: ffprobe and ffmpeg not found. Please install"), PermanentError, "ffmpeg"),
    ],
)
def test_ytdlp_error_mapping(error, exc_type, code):
    mapped = ytdlp.classify_error(error)
    assert type(mapped) is exc_type
    assert mapped.code == code
    if exc_type is PermanentError and code != "ffmpeg":
        assert "Google Drive" in mapped.message


def test_ytdlp_errors_are_mapped_through_run_and_scrub_proxy_credentials(fake_ydl, caplog):
    proxy = "http://alice:s3cr3t@res.proxy.example:8000"
    fake_ydl.error = _wrapped(
        TransportError(f"Unable to connect to proxy {proxy}"), f"ERROR: proxy {proxy} failed"
    )
    with pytest.raises(RetryableError) as exc:
        ytdlp.list_profile("https://www.tiktok.com/@magiclab", proxy=proxy)
    assert "s3cr3t" not in str(exc.value) and "s3cr3t" not in caplog.text
    assert exc.value.__cause__ is None and exc.value.__suppress_context__

    fake_ydl.error = KeyError("extractor bug")
    with pytest.raises(RetryableError, match="unexpectedly"):
        ytdlp.download("https://www.tiktok.com/@a/video/1", Path("/tmp/does-not-matter-ytdlp"))


def test_scrub_userinfo_without_known_proxy():
    assert (
        ytdlp.scrub("via socks5://bob:pw@1.2.3.4:1080 and https://x.test/a")
        == "via socks5://***@1.2.3.4:1080 and https://x.test/a"
    )


def test_ytdlp_options_for_source(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "ytdlp_proxy", "http://server-proxy:3128")
    monkeypatch.setattr(settings, "ytdlp_cookies_dir", tmp_path)
    assert ytdlp.options_for_source(7, {}) == ("http://server-proxy:3128", None)
    (tmp_path / "source-7.txt").write_text("# Netscape HTTP Cookie File\n")
    assert ytdlp.options_for_source(7, {"proxy": "socks5://own:1080"}) == (
        "socks5://own:1080",
        tmp_path / "source-7.txt",
    )
    monkeypatch.setattr(settings, "ytdlp_proxy", "")
    monkeypatch.setattr(settings, "ytdlp_cookies_dir", None)
    assert ytdlp.options_for_source(7, None) == (None, None)


def test_real_youtubedl_accepts_our_params(tmp_path):
    """Builds a real YoutubeDL (no network) to validate option names and extractor filters."""
    from yt_dlp import YoutubeDL

    with YoutubeDL(ytdlp._base_params(None, None)) as ydl:
        names = {ie.IE_NAME.lower() for ie in ydl._ies.values()}
    assert {"tiktok", "tiktok:user", "youtube", "youtube:tab"} <= names
    assert "generic" not in names and not any(n.startswith("instagram") for n in names)


def test_ytdlp_module_is_isolated_from_oauth():
    source = Path(ytdlp.__file__).read_text()
    for forbidden in ("repeater.accounts", "repeater.crypto", "access_token", "load_tokens", "fresh_context"):
        assert forbidden not in source


# =========================================================================== poll_source


def test_first_poll_records_baseline_without_backfill(db, workspace, make_account, make_source):
    acc = make_account("instagram")
    src = make_source(SourceKind.PLATFORM_API, Platform.INSTAGRAM, account=acc)
    fake = FakeLister(Platform.INSTAGRAM, [remote(f"m{i}", T0 - timedelta(hours=i)) for i in range(3)])
    with override("lister", "instagram", fake):
        out = importer.poll_source(db, src.id, now=T0)

    assert out.status == "ok" and out.listed == 3 and out.created == 0
    db.refresh(src)
    assert src.config["baseline_at"] == T0.isoformat()
    assert src.config["seen_ids"] == ["m0", "m1", "m2"]
    assert src.last_polled_at == T0 and src.last_success_at == T0
    assert src.consecutive_failures == 0 and src.last_error is None
    assert videos(db) == [] and jobs_of(db, kinds.IMPORT_VIDEO) == []
    assert fake.calls[0]["since"] is None and fake.calls[0]["limit"] == importer.POLL_LIST_LIMIT
    assert fake.calls[0]["account"].tokens.access_token == "access-token"


def test_first_poll_backfills_newest_items_oldest_first(db, workspace, make_account, make_source):
    acc = make_account("instagram")
    src = make_source(SourceKind.PLATFORM_API, Platform.INSTAGRAM, account=acc, config={"backfill_count": 2})
    items = [remote(f"m{i}", T0 - timedelta(hours=i)) for i in (2, 0, 1)]  # unordered listing
    with override("lister", "instagram", FakeLister(Platform.INSTAGRAM, items)):
        out = importer.poll_source(db, src.id, now=T0)

    assert out.created == 2
    vids = videos(db)
    assert [v.external_id for v in vids] == ["m1", "m0"]  # oldest first
    v = vids[1]
    assert v.status == VideoStatus.IMPORTING and v.source_platform == Platform.INSTAGRAM
    assert v.source_id == src.id and v.source_account_id == acc.id and v.workspace_id == workspace.id
    assert v.permalink == "https://example.test/m0" and v.title == "title m0" and v.caption == "caption m0"
    assert v.source_published_at == T0
    job = jobs_of(db, kinds.IMPORT_VIDEO)[1]
    assert job.payload == {"video_id": v.id, "download_url": "https://cdn.example.test/m0.mp4"}
    assert job.dedupe_key == f"import_video:{v.id}" and job.max_attempts == 4


def test_later_polls_import_only_new_items(db, workspace, make_account, make_source):
    acc = make_account("facebook")
    src = make_source(SourceKind.PLATFORM_API, Platform.FACEBOOK, account=acc)
    fake = FakeLister(Platform.FACEBOOK, [remote("old1", T0 - timedelta(days=1))])
    with override("lister", "facebook", fake):
        importer.poll_source(db, src.id, now=T0)
        fake.items = [
            remote("new2", T0 + timedelta(hours=2), extra={"media_type": "VIDEO"}),
            remote("new1", T0 + timedelta(hours=1)),
            remote("old1", T0 - timedelta(days=1)),
            remote("older-never-seen", T0 - timedelta(days=3)),
            remote("img", T0 + timedelta(hours=1), is_video=False),
        ]
        out = importer.poll_source(db, src.id, now=T0 + timedelta(hours=3))
        assert fake.calls[1]["since"] == T0
        # A third poll with the same listing creates nothing new.
        again = importer.poll_source(db, src.id, now=T0 + timedelta(hours=4))

    assert out.created == 2 and again.created == 0
    assert [v.external_id for v in videos(db)] == ["new1", "new2"]
    newest_job = jobs_of(db, kinds.IMPORT_VIDEO)[-1]
    assert newest_job.payload["remote_extra"] == {"media_type": "VIDEO"}
    db.refresh(src)
    assert src.config["seen_ids"][:4] == ["new2", "new1", "old1", "older-never-seen"]
    assert "img" not in src.config["seen_ids"]


def test_poll_relies_on_unique_index_for_known_videos(db, workspace, make_account, make_source):
    acc = make_account("instagram")
    src = make_source(
        SourceKind.PLATFORM_API, Platform.INSTAGRAM, account=acc, config={"baseline_at": T0.isoformat()}
    )
    db.add(
        Video(
            workspace_id=workspace.id,
            source_platform=Platform.INSTAGRAM,
            external_id="dup",
            status=VideoStatus.READY,
        )
    )
    db.commit()
    with override(
        "lister",
        "instagram",
        FakeLister(
            Platform.INSTAGRAM,
            [remote("dup", T0 + timedelta(hours=1)), remote("fresh", T0 + timedelta(hours=1))],
        ),
    ):
        out = importer.poll_source(db, src.id, now=T0 + timedelta(hours=2))
    assert out.created == 1
    assert sorted(v.external_id for v in videos(db)) == ["dup", "fresh"]
    assert len(jobs_of(db, kinds.IMPORT_VIDEO)) == 1


def test_poll_without_timestamps_uses_seen_ids(db, workspace, make_source, fake_ydl, monkeypatch):
    calls: list[dict] = []

    def fake_list(url, *, limit, proxy, cookies_file):
        calls.append({"url": url, "limit": limit, "proxy": proxy, "cookies": cookies_file})
        return [
            remote(i, None, download_url=None, permalink=f"https://www.tiktok.com/@magiclab/video/{i}")
            for i in ids
        ]

    monkeypatch.setattr(importer.ytdlp, "list_profile", fake_list)
    monkeypatch.setattr(get_settings(), "ytdlp_proxy", "http://server-proxy:3128")
    src = make_source(
        SourceKind.YTDLP,
        Platform.TIKTOK,
        config={"profile_url": "https://www.tiktok.com/@magiclab", "backfill_count": 1},
        unofficial_ack_at=T0,
    )
    ids = ["c", "b", "a"]
    assert importer.poll_source(db, src.id, now=T0).created == 1  # backfill: newest only
    ids = ["e", "d", "c", "b", "a"]
    assert importer.poll_source(db, src.id, now=T0 + timedelta(hours=1)).created == 2
    assert [v.external_id for v in videos(db)] == ["c", "d", "e"]
    assert calls[0] == {
        "url": "https://www.tiktok.com/@magiclab",
        "limit": importer.UNOFFICIAL_LIST_LIMIT,
        "proxy": "http://server-proxy:3128",
        "cookies": None,
    }
    assert all(v.source_platform == Platform.TIKTOK and v.source_published_at is None for v in videos(db))


def test_ytdlp_source_matches_own_account_to_avoid_reposting(
    db, workspace, make_account, make_source, monkeypatch
):
    own = make_account("tiktok", extra={"open_id": "o", "username": "MagicLab"})
    make_account("tiktok", extra={"username": "someoneelse"})
    monkeypatch.setattr(importer.ytdlp, "list_profile", lambda url, **kw: [remote("v1", None)])
    src = make_source(
        SourceKind.YTDLP,
        Platform.TIKTOK,
        config={"profile_url": "https://www.tiktok.com/@magiclab?lang=en", "backfill_count": 1},
        unofficial_ack_at=T0,
    )
    importer.poll_source(db, src.id, now=T0)
    assert videos(db)[0].source_account_id == own.id


def test_ytdlp_source_with_linked_account_uses_official_listing(
    db, workspace, make_account, make_source, monkeypatch
):
    acc = make_account("youtube", extra={"channel_id": "UC1", "uploads_playlist_id": "UU1"})
    src = make_source(
        SourceKind.YTDLP, Platform.YOUTUBE, account=acc, config={"backfill_count": 1}, unofficial_ack_at=T0
    )
    monkeypatch.setattr(
        importer.ytdlp, "list_profile", lambda *a, **k: pytest.fail("must not scrape the listing")
    )
    fake = FakeLister(Platform.YOUTUBE, [remote("yt1", T0 - timedelta(hours=1), download_url=None)])
    with override("lister", "youtube", fake):
        out = importer.poll_source(db, src.id, now=T0)
    assert out.created == 1 and fake.calls
    assert videos(db)[0].source_account_id == acc.id
    assert jobs_of(db, kinds.IMPORT_VIDEO)[0].payload == {"video_id": videos(db)[0].id}


def test_unofficial_source_needs_acknowledgement(db, workspace, make_source, monkeypatch):
    monkeypatch.setattr(importer.ytdlp, "list_profile", lambda *a, **k: pytest.fail("must not run"))
    src = make_source(
        SourceKind.YTDLP, Platform.TIKTOK, config={"profile_url": "https://www.tiktok.com/@magiclab"}
    )
    assert importer.poll_source(db, src.id, now=T0).status == "blocked"
    assert importer.poll_source(db, src.id, now=T0).status == "blocked"
    db.refresh(src)
    assert "confirm" in src.last_error and src.consecutive_failures == 0 and src.enabled
    assert len(activities(db, ActivityLevel.WARNING)) == 1  # not repeated on every poll


def test_disabled_and_manual_sources_are_skipped(db, workspace, make_source):
    disabled = make_source(SourceKind.DRIVE, Platform.GDRIVE, enabled=False)
    man = make_source(SourceKind.MANUAL, None)
    for src in (disabled, man):
        assert importer.poll_source(db, src.id, now=T0).status == "skipped"
        db.refresh(src)
        assert src.last_polled_at is None
    assert importer.poll_source(db, 999_999).status == "skipped"


def test_repeated_failures_pause_the_source_and_notify(
    db, workspace, make_account, make_source, notifications
):
    acc = make_account("instagram")
    src = make_source(SourceKind.PLATFORM_API, Platform.INSTAGRAM, account=acc, name="Magic IG")
    fake = FakeLister(Platform.INSTAGRAM)
    fake.list_error = RetryableError("Instagram is having trouble (HTTP 503)")
    with override("lister", "instagram", fake):
        for n in (1, 2):
            assert importer.poll_source(db, src.id, now=T0).status == "failed"
            db.refresh(src)
            assert src.consecutive_failures == n and src.enabled
        assert notifications == []
        importer.poll_source(db, src.id, now=T0)
    db.refresh(src)
    assert src.enabled is False and src.consecutive_failures == 3
    assert src.last_error == "Instagram is having trouble (HTTP 503)"
    assert len(activities(db, ActivityLevel.WARNING)) == 1
    errors = activities(db, ActivityLevel.ERROR)
    assert len(errors) == 1 and "Magic IG" in errors[0].message and errors[0].data == {"source_id": src.id}
    assert len(notifications) == 1 and notifications[0][1] == {"workspace_id": workspace.id, "level": "error"}
    # Paused sources are no longer polled.
    assert importer.poll_source(db, src.id).status == "skipped"


def test_success_resets_failure_counter_and_notify_errors_are_swallowed(
    db, workspace, make_account, make_source, monkeypatch
):
    def broken_notify(*a, **k):
        raise NotImplementedError

    monkeypatch.setattr(importer.notify_mod, "notify", broken_notify)
    acc = make_account("instagram")
    src = make_source(
        SourceKind.PLATFORM_API, Platform.INSTAGRAM, account=acc, consecutive_failures=2, last_error="x"
    )
    fake = FakeLister(Platform.INSTAGRAM, [])
    with override("lister", "instagram", fake):
        assert importer.poll_source(db, src.id, now=T0).status == "ok"
        db.refresh(src)
        assert src.consecutive_failures == 0 and src.last_error is None
        src.consecutive_failures = 2
        db.commit()
        fake.list_error = PermanentError("gone")
        assert importer.poll_source(db, src.id, now=T0).status == "failed"
    db.refresh(src)
    assert src.enabled is False


def test_auth_error_marks_account_for_reconnect(db, workspace, make_account, make_source):
    acc = make_account("facebook")
    src = make_source(SourceKind.PLATFORM_API, Platform.FACEBOOK, account=acc)
    fake = FakeLister(Platform.FACEBOOK)
    fake.list_error = AuthError("Facebook session expired.")
    with override("lister", "facebook", fake):
        out = importer.poll_source(db, src.id, now=T0)
    assert out.status == "failed" and "Reconnect" in out.message
    db.refresh(acc)
    assert acc.status == AccountStatus.NEEDS_RECONNECT
    assert "access-token" not in out.message


@pytest.mark.parametrize(
    ("kind", "platform", "config", "fragment"),
    [
        (SourceKind.PLATFORM_API, Platform.TIKTOK, {}, "official download"),
        (SourceKind.PLATFORM_API, Platform.INSTAGRAM, {}, "No connected account"),
        (SourceKind.YTDLP, Platform.TIKTOK, {}, "No profile link"),
        (
            SourceKind.YTDLP,
            Platform.INSTAGRAM,
            {"profile_url": "https://instagram.com/x"},
            "TikTok and YouTube",
        ),
    ],
)
def test_misconfigured_sources_record_failures(db, workspace, make_source, kind, platform, config, fragment):
    src = make_source(kind, platform, config=config, unofficial_ack_at=T0)
    out = importer.poll_source(db, src.id, now=T0)
    assert out.status == "failed" and fragment in out.message
    db.refresh(src)
    assert src.consecutive_failures == 1 and fragment in src.last_error


def test_disconnected_account_and_unexpected_errors(db, workspace, make_account, make_source):
    acc = make_account("instagram")
    acc.status = AccountStatus.DISABLED
    db.commit()
    src = make_source(SourceKind.PLATFORM_API, Platform.INSTAGRAM, account=acc)
    assert "reconnected" in importer.poll_source(db, src.id, now=T0).message
    acc.status = AccountStatus.ACTIVE
    db.commit()
    fake = FakeLister(Platform.INSTAGRAM)
    fake.list_error = KeyError("https://graph.example/?access_token=leak")
    with override("lister", "instagram", fake):
        msg = importer.poll_source(db, src.id, now=T0).message
    assert msg == "Unexpected error while checking the source (KeyError)."


def test_drive_source_poll_uses_drive_lister(db, workspace, make_account, make_source):
    acc = make_account("gdrive", extra={"email": "a@magic-lap.com"})
    src = make_source(
        SourceKind.DRIVE, Platform.GDRIVE, account=acc, config={"folder_id": FOLDER_ID, "backfill_count": 5}
    )
    with respx.mock:
        respx.get(FILES_URL).mock(return_value=httpx.Response(200, json={"files": [_drive_file(1, T0)]}))
        out = importer.poll_source(db, src.id, now=T0 + timedelta(minutes=5))
    assert out.created == 1
    v = videos(db)[0]
    assert v.source_platform == Platform.GDRIVE and v.title == "clip 1" and v.caption == "clip 1"
    payload = jobs_of(db, kinds.IMPORT_VIDEO)[0].payload
    assert payload["download_url"].startswith(FILES_URL) and payload["remote_extra"]["ext"] == "mov"


def test_merge_seen_caps_and_dedupes():
    merged = importer._merge_seen(["n2", "n1", "a"], ["a"] + [f"x{i}" for i in range(600)])
    assert merged[:4] == ["n2", "n1", "a", "x0"] and len(merged) == importer.SEEN_IDS_CAP


def test_due_sources_and_enqueue_due_polls(db, workspace, make_source):
    now = utcnow()
    never = make_source(SourceKind.DRIVE, Platform.GDRIVE)
    due = make_source(
        SourceKind.DRIVE,
        Platform.GDRIVE,
        poll_interval_minutes=10,
        last_polled_at=now - timedelta(minutes=11),
    )
    make_source(
        SourceKind.DRIVE, Platform.GDRIVE, poll_interval_minutes=10, last_polled_at=now - timedelta(minutes=5)
    )
    make_source(
        SourceKind.YTDLP,
        Platform.TIKTOK,
        poll_interval_minutes=15,
        last_polled_at=now - timedelta(minutes=20),
    )
    slow = make_source(
        SourceKind.YTDLP,
        Platform.TIKTOK,
        poll_interval_minutes=15,
        last_polled_at=now - timedelta(minutes=31),
    )
    make_source(SourceKind.MANUAL, None)
    make_source(SourceKind.DRIVE, Platform.GDRIVE, enabled=False)
    assert [s.id for s in importer.due_sources(db, now)] == [never.id, due.id, slow.id]
    assert importer.enqueue_due_polls(db, now) == 3
    assert importer.enqueue_due_polls(db, now) == 0  # dedupe while queued
    db.commit()
    assert {j.payload["source_id"] for j in jobs_of(db, kinds.POLL_SOURCE)} == {never.id, due.id, slow.id}


# =========================================================================== import_video


def _importing_video(db, workspace, source, **kw) -> Video:
    v = Video(
        workspace_id=workspace.id,
        source_id=source.id,
        source_platform=kw.pop("platform", source.platform),
        source_account_id=source.connected_account_id,
        external_id=kw.pop("external_id", "ext-1"),
        permalink=kw.pop("permalink", None),
        title="Launch",
        status=kw.pop("status", VideoStatus.IMPORTING),
        **kw,
    )
    db.add(v)
    db.commit()
    return v


def test_import_video_with_official_download(db, workspace, make_account, make_source, sample_video):
    acc = make_account("instagram")
    src = make_source(SourceKind.PLATFORM_API, Platform.INSTAGRAM, account=acc)
    v = _importing_video(db, workspace, src)
    kinds.enqueue_import_video(db, v.id)
    job = db.execute(select(Job)).scalar_one()
    job.payload = {
        "video_id": v.id,
        "download_url": "https://cdn.example.test/x.mp4?sig=1",
        "remote_extra": {"k": 1},
    }
    db.commit()
    fake = FakeLister(Platform.INSTAGRAM, sample=sample_video)
    with override("lister", "instagram", fake):
        job, retry = run_next_job(db, kinds.IMPORT_VIDEO)

    assert retry is None and job.status == JobStatus.DONE
    db.refresh(v)
    assert v.status == VideoStatus.PROCESSING and v.error is None
    assert v.original_path == f"ws{workspace.id}/v{v.id}/original.mp4"
    assert storage.abs_path(v.original_path).read_bytes() == sample_video.read_bytes()
    assert v.size_bytes == sample_video.stat().st_size
    rv = fake.downloads[0]
    assert (
        rv.external_id == "ext-1"
        and rv.download_url == "https://cdn.example.test/x.mp4?sig=1"
        and rv.extra == {"k": 1}
    )
    assert [j.payload for j in jobs_of(db, kinds.PROCESS_VIDEO)] == [{"video_id": v.id}]


def test_import_video_drive_keeps_extension(db, workspace, make_account, make_source, sample_video):
    acc = make_account("gdrive")
    src = make_source(SourceKind.DRIVE, Platform.GDRIVE, account=acc, config={"folder_id": FOLDER_ID})
    v = _importing_video(db, workspace, src, external_id="file1xxxxxxxxxx")
    with respx.mock:
        respx.get(f"{FILES_URL}/file1xxxxxxxxxx").mock(
            return_value=httpx.Response(200, content=sample_video.read_bytes())
        )
        importer.import_video(db, v.id, {"video_id": v.id, "remote_extra": {"ext": "mov"}})
    db.refresh(v)
    assert v.status == VideoStatus.PROCESSING and v.original_path.endswith("/original.mov")


def test_import_video_with_ytdlp(db, workspace, make_source, sample_video, monkeypatch, tmp_path):
    calls = []

    def fake_download(url, dest_dir, *, proxy, cookies_file, basename):
        calls.append((url, proxy, cookies_file, basename))
        out = dest_dir / f"{basename}.webm"
        shutil.copyfile(sample_video, out)
        return out

    monkeypatch.setattr(importer.ytdlp, "download", fake_download)
    monkeypatch.setattr(get_settings(), "ytdlp_cookies_dir", tmp_path)
    src = make_source(
        SourceKind.YTDLP,
        Platform.TIKTOK,
        config={"profile_url": "https://www.tiktok.com/@magiclab", "proxy": "socks5://p:1"},
        unofficial_ack_at=T0,
    )
    (tmp_path / f"source-{src.id}.txt").write_text("cookies")
    v = _importing_video(db, workspace, src, external_id="74001", permalink=None)
    stale = storage.ensure_video_dir(workspace.id, v.id) / "original.mp4"
    stale.write_bytes(b"partial from a failed attempt")
    importer.import_video(db, v.id, {"video_id": v.id})

    assert calls == [
        (
            "https://www.tiktok.com/@magiclab/video/74001",
            "socks5://p:1",
            tmp_path / f"source-{src.id}.txt",
            "original",
        )
    ]
    assert not stale.exists()
    db.refresh(v)
    assert v.status == VideoStatus.PROCESSING and v.original_path.endswith("original.webm")


def test_import_video_youtube_url_from_external_id(db, workspace, make_source, monkeypatch, sample_video):
    seen = []
    monkeypatch.setattr(
        importer.ytdlp,
        "download",
        lambda url, d, **kw: (
            seen.append(url) or shutil.copyfile(sample_video, d / "original.mp4") or d / "original.mp4"
        ),
    )
    src = make_source(
        SourceKind.YTDLP,
        Platform.YOUTUBE,
        config={"profile_url": "https://www.youtube.com/@m"},
        unofficial_ack_at=T0,
    )
    v = _importing_video(db, workspace, src, external_id="dQw4w9WgXcQ")
    importer.import_video(db, v.id)
    assert seen == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (PermanentError("The media was deleted on Instagram."), "deleted on Instagram"),
        (NotSupportedError("No official download."), "Google Drive"),
        (AuthError("Token revoked."), "Reconnect"),
    ],
)
def test_import_video_permanent_failures(db, workspace, make_account, make_source, error, fragment):
    acc = make_account("instagram")
    src = make_source(SourceKind.PLATFORM_API, Platform.INSTAGRAM, account=acc)
    v = _importing_video(db, workspace, src)
    fake = FakeLister(Platform.INSTAGRAM)
    fake.download_error = error
    with override("lister", "instagram", fake):
        importer.import_video(db, v.id, {"video_id": v.id})
    db.refresh(v)
    assert v.status == VideoStatus.FAILED and fragment in v.error
    err = activities(db, ActivityLevel.ERROR)[-1]
    assert err.data["video_id"] == v.id and "could not be imported" in err.message
    assert jobs_of(db, kinds.PROCESS_VIDEO) == []


def test_import_video_retries_then_fails(db, workspace, make_account, make_source):
    acc = make_account("instagram")
    src = make_source(SourceKind.PLATFORM_API, Platform.INSTAGRAM, account=acc)
    v = _importing_video(db, workspace, src)
    kinds.enqueue_import_video(db, v.id)
    db.commit()
    fake = FakeLister(Platform.INSTAGRAM)
    fake.download_error = RetryableError("CDN timeout")
    with override("lister", "instagram", fake):
        for attempt in (1, 2, 3):
            job, retry = run_next_job(db, kinds.IMPORT_VIDEO)
            assert retry is not None and job.status == JobStatus.QUEUED
            assert retry.seconds == queue.default_backoff(attempt).total_seconds()
            assert job.payload["retries"] == attempt and job.attempts == 0
            db.refresh(v)
            assert v.status == VideoStatus.IMPORTING and v.error.startswith(
                f"Attempt {attempt} of 4 failed: CDN timeout"
            )
        job, retry = run_next_job(db, kinds.IMPORT_VIDEO)
    assert retry is None and job.status == JobStatus.DONE
    db.refresh(v)
    assert v.status == VideoStatus.FAILED and "gave up after 4 attempts" in v.error
    assert len(fake.downloads) == 4


def test_import_video_rate_limit_waits_at_least_retry_after(db, workspace, make_account, make_source):
    acc = make_account("instagram")
    src = make_source(SourceKind.PLATFORM_API, Platform.INSTAGRAM, account=acc)
    v = _importing_video(db, workspace, src)
    kinds.enqueue_import_video(db, v.id)
    db.commit()
    fake = FakeLister(Platform.INSTAGRAM)
    fake.download_error = RateLimitedError("slow down", retry_after=7200)
    with override("lister", "instagram", fake):
        _, retry = run_next_job(db, kinds.IMPORT_VIDEO)
    assert retry.seconds == 7200


def test_import_video_unexpected_error_is_retried(db, workspace, make_account, make_source):
    acc = make_account("instagram")
    src = make_source(SourceKind.PLATFORM_API, Platform.INSTAGRAM, account=acc)
    v = _importing_video(db, workspace, src)
    fake = FakeLister(Platform.INSTAGRAM)
    fake.download_error = ZeroDivisionError("bug")
    with override("lister", "instagram", fake), pytest.raises(RetryLater):
        importer.import_video(db, v.id, {"video_id": v.id})
    db.refresh(v)
    assert v.status == VideoStatus.IMPORTING and "Unexpected error (ZeroDivisionError)" in v.error


def test_import_video_guards(db, workspace, make_source, monkeypatch):
    monkeypatch.setattr(importer.ytdlp, "download", lambda *a, **k: pytest.fail("must not download"))
    src = make_source(SourceKind.YTDLP, Platform.TIKTOK, config={"profile_url": "https://www.tiktok.com/@m"})
    v = _importing_video(db, workspace, src)
    importer.import_video(db, v.id)  # acknowledgement withdrawn
    db.refresh(v)
    assert v.status == VideoStatus.FAILED and "turned off" in v.error

    ready = _importing_video(db, workspace, src, external_id="other", status=VideoStatus.READY)
    importer.import_video(db, ready.id)  # not importing: no-op
    importer.import_video(db, 424242)  # missing: no-op
    db.refresh(ready)
    assert ready.status == VideoStatus.READY


# =========================================================================== process_video


def _processing_video(db, workspace, src_file: Path, **kw) -> Video:
    v = Video(workspace_id=workspace.id, status=VideoStatus.PROCESSING, title=kw.pop("title", "Clip"), **kw)
    db.add(v)
    db.flush()
    dest = storage.ensure_video_dir(workspace.id, v.id) / f"original{src_file.suffix}"
    shutil.copyfile(src_file, dest)
    v.original_path = storage.rel_path(dest)
    db.commit()
    return v


def test_process_video_end_to_end(db, workspace, sample_video, fast_encode):
    v = _processing_video(db, workspace, sample_video)
    kinds.enqueue_process_video(db, v.id)
    db.commit()
    job, retry = run_next_job(db, kinds.PROCESS_VIDEO)

    assert retry is None and job.status == JobStatus.DONE
    db.refresh(v)
    assert v.status == VideoStatus.READY and v.error is None
    assert (v.width, v.height, v.fps) == (1080, 1920, 30)
    assert 3.9 <= v.duration_s <= 4.2
    assert v.encoded_path == f"ws{workspace.id}/v{v.id}/encoded.mp4" and v.thumbnail_path.endswith(
        "thumb.jpg"
    )
    encoded = storage.abs_path(v.encoded_path)
    assert v.encoded_size_bytes == encoded.stat().st_size and probe(encoded).audio_codec == "aac"
    assert storage.abs_path(v.thumbnail_path).is_file()
    assert v.sha256 == sha256_file(sample_video) and len(v.phash.split()) == 8
    assert v.size_bytes == sample_video.stat().st_size
    info = activities(db, ActivityLevel.INFO)[-1]
    assert "is ready (0:04)" in info.message and info.data["video_id"] == v.id
    assert [j.payload for j in jobs_of(db, kinds.PLAN_WORKSPACE)] == [{"workspace_id": workspace.id}]


def test_process_landscape_video_notes_letterboxing(db, workspace, sample_video_landscape, fast_encode):
    v = _processing_video(db, workspace, sample_video_landscape)
    importer.process_video(db, v.id)
    db.refresh(v)
    assert v.status == VideoStatus.READY and (v.width, v.height) == (1080, 1920)
    msg = activities(db, ActivityLevel.INFO)[-1]
    assert "landscape" in msg.message and msg.data["source_width"] == 1280


def _ready_video(db, workspace, sample_video, **kw) -> Video:
    v = Video(
        workspace_id=workspace.id,
        status=kw.pop("status", VideoStatus.READY),
        title=kw.pop("title", "Original upload"),
        duration_s=kw.pop("duration_s", 4.0),
        sha256=kw.pop("sha256", None),
        phash=kw.pop("phash", None),
        **kw,
    )
    db.add(v)
    db.commit()
    return v


def test_process_marks_exact_duplicate(db, workspace, sample_video, monkeypatch):
    monkeypatch.setattr(
        importer, "encode_for_platforms", lambda *a, **k: pytest.fail("duplicates are not encoded")
    )
    first = _ready_video(db, workspace, sample_video, sha256=sha256_file(sample_video))
    v = _processing_video(db, workspace, sample_video, title="From TikTok")
    importer.process_video(db, v.id)
    db.refresh(v)
    assert v.status == VideoStatus.DUPLICATE and v.duplicate_of_id == first.id
    assert v.encoded_path is None and v.sha256 == first.sha256
    msg = activities(db, ActivityLevel.INFO)[-1]
    assert "same as 'Original upload'" in msg.message and msg.data["duplicate_of_id"] == first.id
    assert jobs_of(db, kinds.PLAN_WORKSPACE) == []


def test_process_marks_perceptual_duplicate(db, workspace, sample_video, tmp_path, monkeypatch):
    monkeypatch.setattr(
        importer, "encode_for_platforms", lambda *a, **k: pytest.fail("duplicates are not encoded")
    )
    reencoded = tmp_path / "copy.mp4"
    import subprocess

    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(sample_video),
            "-vf",
            "scale=720:1280",
            "-c:v",
            "libx264",
            "-crf",
            "30",
            "-preset",
            "ultrafast",
            str(reencoded),
        ],
        check=True,
    )
    hashes = " ".join(phash_frames(reencoded, 4.0))
    _ready_video(db, workspace, sample_video, phash=hashes, duration_s=5.5)  # duration too far off
    archived = _ready_video(
        db, workspace, sample_video, status=VideoStatus.ARCHIVED, phash=hashes, duration_s=4.4
    )
    v = _processing_video(db, workspace, sample_video)
    importer.process_video(db, v.id)
    db.refresh(v)
    assert v.status == VideoStatus.DUPLICATE and v.duplicate_of_id == archived.id


def test_duplicates_only_against_ready_or_archived_in_same_workspace(
    db, workspace, sample_video, fast_encode
):
    from repeater.models import Workspace

    other_ws = Workspace(name="Other")
    db.add(other_ws)
    db.commit()
    sha = sha256_file(sample_video)
    _ready_video(db, workspace, sample_video, sha256=sha, status=VideoStatus.FAILED)
    _ready_video(db, workspace, sample_video, sha256=sha, status=VideoStatus.DUPLICATE)
    db.add(Video(workspace_id=other_ws.id, status=VideoStatus.READY, sha256=sha, duration_s=4.0))
    db.commit()
    v = _processing_video(db, workspace, sample_video)
    importer.process_video(db, v.id)
    db.refresh(v)
    assert v.status == VideoStatus.READY and v.duplicate_of_id is None


def test_process_rejects_non_video_and_missing_file(db, workspace, tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_text("not really")
    v = _processing_video(db, workspace, junk)
    importer.process_video(db, v.id)
    db.refresh(v)
    assert v.status == VideoStatus.FAILED and v.error.startswith("The file is not a video")

    gone = _processing_video(db, workspace, junk)
    storage.abs_path(gone.original_path).unlink()
    importer.process_video(db, gone.id)
    db.refresh(gone)
    assert gone.status == VideoStatus.FAILED and "missing" in gone.error


@pytest.mark.parametrize(("duration", "fragment"), [(0.4, "too short"), (1200.0, "too long (20:00)")])
def test_process_rejects_durations_no_platform_accepts(
    db, workspace, sample_video, monkeypatch, duration, fragment
):
    monkeypatch.setattr(
        importer, "probe", lambda p: ProbeResult(duration, 1080, 1920, 30, "h264", "aac", True, 10)
    )
    v = _processing_video(db, workspace, sample_video)
    importer.process_video(db, v.id)
    db.refresh(v)
    assert v.status == VideoStatus.FAILED and fragment in v.error


def test_process_encode_error_fails_video(db, workspace, sample_video, monkeypatch):
    def broken(*a, **k):
        raise encode.EncodeError("encoding failed (ffmpeg exit code 1)", stderr_tail="Invalid data")

    monkeypatch.setattr(importer, "encode_for_platforms", broken)
    v = _processing_video(db, workspace, sample_video)
    importer.process_video(db, v.id)
    db.refresh(v)
    assert v.status == VideoStatus.FAILED and "Encoding failed" in v.error and "Invalid data" in v.error
    assert v.sha256 is not None  # fingerprints were committed before encoding


def test_process_timeouts_retry_using_error_text_when_no_job_row(db, workspace, sample_video, monkeypatch):
    def slow(*a, **k):
        raise encode.EncodeTimeout("encoding timed out after 900 s", stderr_tail="")

    monkeypatch.setattr(importer, "encode_for_platforms", slow)
    v = _processing_video(db, workspace, sample_video)
    for attempt in (1, 2):
        with pytest.raises(RetryLater):
            importer.process_video(db, v.id, {"video_id": v.id})
        db.refresh(v)
        assert v.status == VideoStatus.PROCESSING and v.error.startswith(f"Attempt {attempt} of 3 failed")
    importer.process_video(db, v.id, {"video_id": v.id})
    db.refresh(v)
    assert v.status == VideoStatus.FAILED and "gave up after 3 attempts" in v.error


def test_process_keeps_user_archive_made_during_encoding(
    db, workspace, sample_video, fast_encode, monkeypatch
):
    real = importer.encode_for_platforms

    def encode_while_user_archives(*a, **k):
        from repeater.db import get_sessionmaker

        with get_sessionmaker()() as other:
            other.get(Video, v.id).status = VideoStatus.ARCHIVED
            other.commit()
        return real(*a, **k)

    monkeypatch.setattr(importer, "encode_for_platforms", encode_while_user_archives)
    v = _processing_video(db, workspace, sample_video)
    importer.process_video(db, v.id)
    db.refresh(v)
    assert v.status == VideoStatus.ARCHIVED and v.encoded_path is not None


def test_process_video_noop_for_other_statuses(db, workspace, sample_video):
    v = _processing_video(db, workspace, sample_video)
    v.status = VideoStatus.READY
    db.commit()
    importer.process_video(db, v.id)
    importer.process_video(db, 999_999)
    db.refresh(v)
    assert v.status == VideoStatus.READY and v.sha256 is None


def test_retry_failed_video(db, workspace, sample_video, make_source):
    v = _processing_video(db, workspace, sample_video)
    v.status, v.error = VideoStatus.FAILED, "boom"
    db.commit()
    importer.retry_failed_video(db, v, actor="user")
    assert v.status == VideoStatus.PROCESSING and v.error is None
    assert len(jobs_of(db, kinds.PROCESS_VIDEO)) == 1

    src = make_source(SourceKind.DRIVE, Platform.GDRIVE)
    w = _importing_video(db, workspace, src, status=VideoStatus.FAILED)
    importer.retry_failed_video(db, w)
    assert w.status == VideoStatus.IMPORTING and len(jobs_of(db, kinds.IMPORT_VIDEO)) == 1
    with pytest.raises(ValueError, match="Only failed"):
        importer.retry_failed_video(db, w)


def test_job_handlers_are_registered():
    assert HANDLERS[kinds.POLL_SOURCE] is source_jobs.handle_poll_source
    assert HANDLERS[kinds.IMPORT_VIDEO] is source_jobs.handle_import_video
    assert HANDLERS[kinds.PROCESS_VIDEO] is source_jobs.handle_process_video


def test_poll_job_through_queue(db, workspace, make_account, make_source):
    acc = make_account("instagram")
    src = make_source(SourceKind.PLATFORM_API, Platform.INSTAGRAM, account=acc)
    kinds.enqueue_poll_source(db, src.id)
    db.commit()
    with override("lister", "instagram", FakeLister(Platform.INSTAGRAM, [])):
        job, retry = run_next_job(db, kinds.POLL_SOURCE)
    assert retry is None and job.status == JobStatus.DONE
    db.refresh(src)
    assert src.last_success_at is not None


# =========================================================================== manual uploads


def _upload(src: Path, name: str = "upload.part") -> Path:
    dest = storage.uploads_dir() / name
    shutil.copyfile(src, dest)
    return dest


def test_manual_upload_creates_video_and_queues_processing(db, workspace, sample_video):
    up = _upload(sample_video)
    v = manual.create_video_from_upload(
        db,
        workspace_id=workspace.id,
        upload_path=up,
        original_filename="C:\\phone\\Magic Trick.MOV",
        caption="  wow  ",
        actor="admin@magic-lap.com",
    )
    db.commit()
    assert not up.exists()
    assert v.status == VideoStatus.PROCESSING and v.source_platform is None and v.external_id is None
    assert v.title == "Magic Trick" and v.caption == "wow"
    assert v.original_path == f"ws{workspace.id}/v{v.id}/original.mov"
    assert storage.abs_path(v.original_path).read_bytes() == sample_video.read_bytes()
    assert (v.width, v.height) == (1080, 1920) and v.size_bytes == sample_video.stat().st_size
    src = db.get(Source, v.source_id)
    assert src.kind == SourceKind.MANUAL and src.platform is None
    assert [j.payload for j in jobs_of(db, kinds.PROCESS_VIDEO)] == [{"video_id": v.id}]
    log = activities(db)[-1]
    assert log.actor == "admin@magic-lap.com" and log.data == {"video_id": v.id}

    second = manual.create_video_from_upload(
        db,
        workspace_id=workspace.id,
        upload_path=_upload(sample_video, "b.part"),
        original_filename="noext",
        title="Mine",
    )
    db.commit()
    assert (
        second.source_id == v.source_id
        and second.title == "Mine"
        and second.original_path.endswith("original.mp4")
    )
    assert db.execute(select(func.count()).select_from(Source)).scalar() == 1


def test_manual_upload_then_processing(db, workspace, sample_video_landscape, fast_encode):
    v = manual.create_video_from_upload(
        db,
        workspace_id=workspace.id,
        upload_path=_upload(sample_video_landscape),
        original_filename="wide.mp4",
    )
    db.commit()
    run_next_job(db, kinds.PROCESS_VIDEO)
    db.refresh(v)
    assert v.status == VideoStatus.READY and storage.abs_path(v.encoded_path).is_file()


def test_manual_upload_rejects_non_video(db, workspace, tmp_path):
    up = storage.uploads_dir() / "evil.part"
    up.write_text("#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:10.0,\nfile:///etc/passwd\n")
    with pytest.raises(ValueError, match="^'notes.mp4' is not a video"):
        manual.create_video_from_upload(
            db, workspace_id=workspace.id, upload_path=up, original_filename="notes.mp4"
        )
    assert not up.exists()
    assert db.execute(select(func.count()).select_from(Video)).scalar() == 0


def test_manual_upload_refuses_files_outside_uploads_dir(db, workspace, sample_video, tmp_path):
    outside = tmp_path / "x.mp4"
    shutil.copyfile(sample_video, outside)
    with pytest.raises(ValueError, match="upload the file again"):
        manual.create_video_from_upload(
            db, workspace_id=workspace.id, upload_path=outside, original_filename="x.mp4"
        )
    assert outside.exists()
    empty = storage.uploads_dir() / "empty.part"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        manual.create_video_from_upload(
            db, workspace_id=workspace.id, upload_path=empty, original_filename="x.mp4"
        )


def test_backfill_is_capped_and_widens_first_listing(db, workspace, make_account, make_source):
    acc = make_account("instagram")
    src = make_source(
        SourceKind.PLATFORM_API, Platform.INSTAGRAM, account=acc, config={"backfill_count": 5000}
    )
    fake = FakeLister(Platform.INSTAGRAM, [])
    with override("lister", "instagram", fake):
        importer.poll_source(db, src.id, now=T0)
        importer.poll_source(db, src.id, now=T0 + timedelta(minutes=15))
    assert [c["limit"] for c in fake.calls] == [importer.MAX_BACKFILL, importer.POLL_LIST_LIMIT]


def test_newest_keeps_listing_order_without_timestamps():
    items = [remote("b", None), remote("a", T0)]
    assert [r.external_id for r in importer._newest(items, 1)] == ["b"]
    assert [r.external_id for r in importer._oldest_first(items)] == ["a", "b"]
    assert importer._newest(items, 0) == []


def test_import_video_unclassified_platform_error_is_retried(db, workspace, make_account, make_source):
    from repeater.platforms.base import PlatformError

    acc = make_account("facebook")
    src = make_source(SourceKind.PLATFORM_API, Platform.FACEBOOK, account=acc)
    v = _importing_video(db, workspace, src)
    fake = FakeLister(Platform.FACEBOOK)
    fake.download_error = PlatformError("Graph API hiccup")
    with override("lister", "facebook", fake), pytest.raises(RetryLater):
        importer.import_video(db, v.id, {"video_id": v.id})
    db.refresh(v)
    assert v.status == VideoStatus.IMPORTING and "Graph API hiccup" in v.error


def test_real_youtubedl_prefers_1080p_h264_mp4(tmp_path):
    """The download options are valid for a real YoutubeDL and rank formats as intended (no network)."""
    from yt_dlp import YoutubeDL

    def fmt(fid, height, vcodec, ext, pref=None):
        f = {"format_id": fid, "url": f"https://v.test/{fid}", "height": height, "width": height * 9 // 16}
        f.update({"vcodec": vcodec, "acodec": "aac", "ext": ext})
        if pref is not None:
            f["preference"] = pref
        return f

    info = {
        "id": "x",
        "formats": [
            fmt("2160-vp9", 2160, "vp9", "webm"),
            fmt("1080-vp9", 1080, "vp9", "webm"),
            fmt("1080-h264", 1080, "avc1.640028", "mp4"),
            fmt("1080-h264-watermarked", 1080, "avc1.640028", "mp4", pref=-2),
            fmt("720-h264", 720, "avc1.4d401f", "mp4"),
        ],
    }
    with YoutubeDL(ytdlp.download_params(tmp_path, proxy=None)) as ydl:
        ydl.sort_formats(info)
    assert info["formats"][-1]["format_id"] == "1080-h264"  # best last
    assert info["formats"][0]["format_id"] == "1080-h264-watermarked"
