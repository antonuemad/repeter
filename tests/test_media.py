"""Tests for media processing: probe, platform encode, thumbnails, fingerprints, retention."""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image

from repeater.config import get_settings
from repeater.media import encode, fingerprint, retention, storage
from repeater.media.encode import (
    EncodeError,
    EncodeTimeout,
    build_encode_command,
    encode_for_platforms,
    make_thumbnail,
)
from repeater.media.probe import MediaToolError, ProbeResult, parse_probe_json, probe
from repeater.models import Platform, ScheduledPost, Video, VideoStatus
from repeater.timeutil import utcnow

# --------------------------------------------------------------------------- helpers


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def ffprobe_streams(path: Path) -> list[dict]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        check=True,
        capture_output=True,
    ).stdout
    return json.loads(out)["streams"]


def top_level_atoms(path: Path) -> list[str]:
    atoms: list[str] = []
    with open(path, "rb") as fh:
        while True:
            header = fh.read(8)
            if len(header) < 8:
                break
            size, kind = struct.unpack(">I4s", header)
            atoms.append(kind.decode("latin-1"))
            if size == 1:
                size = struct.unpack(">Q", fh.read(8))[0]
                fh.seek(size - 16, 1)
            elif size == 0:
                break
            else:
                fh.seek(size - 8, 1)
    return atoms


def make_clip(
    path: Path, *, size: str = "360x640", seconds: int = 2, audio: bool = True, extra: tuple = ()
) -> Path:
    args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}", "-c:a", "aac", "-shortest"]
    ffmpeg(*args, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", *extra, str(path))
    return path


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("clips")


@pytest.fixture(scope="module")
def rotated_clip(clips: Path) -> Path:
    """A landscape-coded 640x360 clip without audio whose display matrix turns it portrait."""
    plain = make_clip(clips / "plain_landscape.mp4", size="640x360", audio=False)
    out = clips / "rotated.mp4"
    ffmpeg("-display_rotation", "90", "-i", str(plain), "-c", "copy", str(out))
    return out


@pytest.fixture(scope="module")
def encoded_sample(sample_video: Path, clips: Path) -> Path:
    """The vertical sample encoded once with the real (slow preset) platform profile."""
    out = clips / "encoded.mp4"
    encode_for_platforms(sample_video, out)
    return out


@pytest.fixture
def clean_media() -> Path:
    root = storage.media_root()
    for child in root.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    return root


# --------------------------------------------------------------------------- probe


def test_probe_vertical_sample(sample_video):
    info = probe(sample_video)
    assert (info.width, info.height) == (1080, 1920)
    assert info.fps == 30
    assert info.video_codec == "h264" and info.audio_codec == "aac" and info.has_audio
    assert 3.9 <= info.duration_s <= 4.2
    assert info.rotation == 0 and info.size_bytes == sample_video.stat().st_size
    assert info.is_vertical and not info.is_landscape
    assert "mp4" in info.format_name


def test_probe_landscape_sample(sample_video_landscape):
    info = probe(sample_video_landscape)
    assert (info.width, info.height, info.fps) == (1280, 720, 25)
    assert info.is_landscape


def test_probe_reports_display_size_of_rotated_video(rotated_clip):
    info = probe(rotated_clip)
    assert (info.width, info.height) == (360, 640)
    assert info.rotation == 270  # display matrix +90 (counter-clockwise) == 270 clockwise
    assert info.has_audio is False and info.audio_codec is None


def test_probe_phone_style_rotation(clips):
    plain = clips / "plain_landscape.mp4"
    if not plain.exists():
        make_clip(plain, size="640x360", audio=False)
    out = clips / "rotated_phone.mp4"
    ffmpeg("-display_rotation", "-90", "-i", str(plain), "-c", "copy", str(out))
    info = probe(out)
    assert (info.width, info.height, info.rotation) == (360, 640, 90)


def _probe_json(**video_overrides) -> dict:
    video = {
        "codec_type": "video",
        "codec_name": "hevc",
        "width": 1920,
        "height": 1080,
        "avg_frame_rate": "0/0",
    }
    video.update(video_overrides)
    return {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "12.5", "size": "1000"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "mjpeg",
                "width": 4000,
                "height": 4000,
                "disposition": {"attached_pic": 1},
            },
            video,
        ],
    }


def test_parse_probe_json_legacy_rotate_tag_and_fps_fallback():
    info = parse_probe_json(_probe_json(tags={"rotate": "90"}, r_frame_rate="30000/1001"))
    assert (info.width, info.height, info.rotation) == (1080, 1920, 90)
    assert info.video_codec == "hevc"  # cover art stream skipped
    assert info.fps == pytest.approx(29.97, abs=0.01)
    assert info.has_audio is False and info.size_bytes == 1000


def test_parse_probe_json_upside_down_keeps_size():
    info = parse_probe_json(
        _probe_json(side_data_list=[{"side_data_type": "Display Matrix", "rotation": 180}])
    )
    assert (info.width, info.height, info.rotation) == (1920, 1080, 180)


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (
            {"format": {"format_name": "image2", "duration": "1"}, "streams": [{"codec_type": "video"}]},
            "unsupported",
        ),
        ({"format": {"format_name": "hls", "duration": "5"}, "streams": []}, "unsupported"),
        (
            {"format": {"format_name": "mov,mp4", "duration": "5"}, "streams": [{"codec_type": "audio"}]},
            "no video",
        ),
        (
            {
                "format": {"format_name": "mov,mp4"},
                "streams": [{"codec_type": "video", "width": 2, "height": 2}],
            },
            "no playable duration",
        ),
    ],
)
def test_parse_probe_json_rejects_non_videos(data, fragment):
    with pytest.raises(ValueError, match="not a video") as exc:
        parse_probe_json(data)
    assert fragment in str(exc.value)


def test_probe_rejects_text_audio_and_image_files(tmp_path):
    text = tmp_path / "notes.mp4"
    text.write_text("definitely not a video")
    audio = tmp_path / "voice.m4a"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", str(audio))
    image = tmp_path / "still.png"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=64x64:duration=1", "-frames:v", "1", str(image))
    for path in (text, audio, image):
        with pytest.raises(ValueError, match="^not a video"):
            probe(path)


def test_probe_missing_file_and_missing_binary(tmp_path, sample_video, monkeypatch):
    with pytest.raises(FileNotFoundError):
        probe(tmp_path / "nope.mp4")
    monkeypatch.setattr(get_settings(), "ffprobe_bin", str(tmp_path / "no-ffprobe"))
    with pytest.raises(MediaToolError, match="ffprobe not found"):
        probe(sample_video)


# --------------------------------------------------------------------------- encode


def test_build_encode_command_matches_plan_profile(tmp_path):
    src, out = tmp_path / "in.mov", tmp_path / "out.part"
    cmd = build_encode_command(src, out, has_audio=True, ffmpeg_bin="ffmpeg")
    joined = " ".join(cmd)
    assert (
        "-vf scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,"
        "fps=30,format=yuv420p" in joined
    )
    assert (
        "-c:v libx264 -profile:v high -level 4.0 -preset slow -crf 20 -maxrate 12M -bufsize 24M "
        "-g 60 -keyint_min 60 -sc_threshold 0 -bf 2" in joined
    )
    assert "-c:a aac -b:a 192k -ar 48000 -ac 2" in joined
    assert "-movflags +faststart -f mp4" in joined and cmd[-1] == str(out)
    assert "-protocol_whitelist file -i " + str(src) in joined
    assert "-map 0:V:0 -map 0:a:0" in joined and "anullsrc" not in joined

    silent = " ".join(build_encode_command(src, out, has_audio=False, ffmpeg_bin="ffmpeg"))
    assert "-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000" in silent
    assert "-map 0:V:0 -map 1:a:0 -shortest" in silent


def test_encode_vertical_with_real_profile(encoded_sample, sample_video):
    info = probe(encoded_sample)
    assert (info.width, info.height, info.fps) == (1080, 1920, 30)
    assert info.video_codec == "h264" and info.audio_codec == "aac"
    assert abs(info.duration_s - probe(sample_video).duration_s) < 0.2
    v, a = (
        next(s for s in ffprobe_streams(encoded_sample) if s["codec_type"] == t) for t in ("video", "audio")
    )
    assert v["profile"] == "High" and v["level"] == 40 and v["pix_fmt"] == "yuv420p"
    assert a["sample_rate"] == "48000" and a["channels"] == 2 and a["profile"] == "LC"
    atoms = top_level_atoms(encoded_sample)
    assert atoms.index("moov") < atoms.index("mdat"), atoms  # +faststart
    assert not list(encoded_sample.parent.glob(".*.part"))


def test_encode_landscape_is_padded_to_vertical_30fps(sample_video_landscape, tmp_path):
    out = encode_for_platforms(sample_video_landscape, tmp_path / "land.mp4")
    assert (out.width, out.height, out.fps) == (1080, 1920, 30)
    assert abs(out.duration_s - 3.0) < 0.2


def test_encode_adds_silent_audio_and_applies_rotation(rotated_clip, tmp_path):
    dst = tmp_path / "nested" / "silent.mp4"
    out = encode_for_platforms(rotated_clip, dst)
    assert dst.is_file()
    assert out.has_audio and out.audio_codec == "aac"
    assert (out.width, out.height, out.rotation) == (1080, 1920, 0)
    assert abs(out.duration_s - 2.0) < 0.2
    audio = next(s for s in ffprobe_streams(dst) if s["codec_type"] == "audio")
    assert audio["channels"] == 2 and audio["sample_rate"] == "48000"
    # Rotated to 360x640 the picture fills 1080x1920 exactly; unrotated it would be letterboxed
    # and the top rows would be black padding.
    frame = tmp_path / "frame.png"
    ffmpeg("-i", str(dst), "-frames:v", "1", str(frame))
    with Image.open(frame) as img:
        assert img.size == (1080, 1920)
        assert max(img.convert("L").getpixel((x, 4)) for x in range(0, 1080, 60)) > 40


def test_encode_failure_reports_stderr_and_leaves_nothing(tmp_path, sample_video):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"\x00" * 4096)
    fake = ProbeResult(4.0, 1080, 1920, 30.0, "h264", "aac", True, 4096)
    dst = tmp_path / "out.mp4"
    with pytest.raises(EncodeError) as exc:
        encode_for_platforms(bad, dst, source_probe=fake)
    assert not isinstance(exc.value, EncodeTimeout)
    assert exc.value.stderr_tail and str(tmp_path) not in exc.value.stderr_tail
    assert "broken.mp4" in str(exc.value)
    assert not dst.exists() and not list(tmp_path.glob(".*.part"))


def test_encode_timeout_kills_ffmpeg_and_cleans_up(tmp_path, sample_video):
    dst = tmp_path / "slow.mp4"
    with pytest.raises(EncodeTimeout, match="timed out"):
        encode_for_platforms(sample_video, dst, timeout_s=0.05)
    assert not dst.exists() and not list(tmp_path.glob(".*.part"))


def test_encode_missing_ffmpeg_binary(tmp_path, sample_video, monkeypatch):
    monkeypatch.setattr(get_settings(), "ffmpeg_bin", str(tmp_path / "no-ffmpeg"))
    with pytest.raises(MediaToolError, match="ffmpeg not found"):
        encode_for_platforms(sample_video, tmp_path / "x.mp4")


def test_make_thumbnail_jpeg_540x960(encoded_sample, sample_video_landscape, tmp_path):
    thumb = make_thumbnail(encoded_sample, tmp_path / "thumb.jpg", at_s=1.0)
    with Image.open(thumb) as img:
        assert img.format == "JPEG" and img.size == (540, 960)
    # Past the end falls back to the first frame; landscape sources are letterboxed.
    late = make_thumbnail(sample_video_landscape, tmp_path / "late.jpg", at_s=99)
    with Image.open(late) as img:
        assert img.size == (540, 960)
        assert img.convert("L").getpixel((270, 5)) < 30  # black pad band at the top
    assert not list(tmp_path.glob(".*.part"))


def test_make_thumbnail_error(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"nope")
    with pytest.raises(EncodeError):
        make_thumbnail(bad, tmp_path / "t.jpg", at_s=0)
    assert not (tmp_path / "t.jpg").exists()


def test_encode_constants_match_plan():
    assert encode.TARGET_WIDTH == 1080 and encode.TARGET_HEIGHT == 1920 and encode.TARGET_FPS == 30


# --------------------------------------------------------------------------- fingerprints


def test_sha256_file(sample_video):
    assert fingerprint.sha256_file(sample_video) == hashlib.sha256(sample_video.read_bytes()).hexdigest()


def test_sample_times_are_slice_centres():
    assert fingerprint.sample_times(8.0) == [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5]
    assert fingerprint.sample_times(0) == [] and fingerprint.sample_times(5, 0) == []


def test_reencoded_copy_is_duplicate(sample_video, tmp_path):
    copy = tmp_path / "copy.mp4"
    ffmpeg(
        "-i",
        str(sample_video),
        "-vf",
        "scale=720:1280",
        "-c:v",
        "libx264",
        "-crf",
        "32",
        "-preset",
        "ultrafast",
        str(copy),
    )
    a_info, b_info = probe(sample_video), probe(copy)
    a = fingerprint.phash_frames(sample_video, a_info.duration_s)
    b = fingerprint.phash_frames(copy, b_info.duration_s)
    assert len(a) == len(b) == fingerprint.PHASH_FRAMES
    assert all(len(h) == 16 and int(h, 16) >= 0 for h in a)
    assert fingerprint.is_duplicate(a, a_info.duration_s, b, b_info.duration_s)


def test_platform_encode_of_same_video_is_duplicate(sample_video, encoded_sample):
    a = fingerprint.phash_frames(sample_video, probe(sample_video).duration_s)
    b = fingerprint.phash_frames(encoded_sample, probe(encoded_sample).duration_s)
    assert fingerprint.is_duplicate(a, 4.0, b, 4.02)


@pytest.mark.parametrize("pattern", ["mandelbrot", "smptehdbars", "rgbtestsrc", "life"])
def test_different_sources_are_not_duplicates(sample_video, tmp_path, pattern):
    other = tmp_path / f"{pattern}.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"{pattern}=size=360x640:rate=30",
        "-t",
        "4",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        str(other),
    )
    a = fingerprint.phash_frames(sample_video, 4.0)
    b = fingerprint.phash_frames(other, probe(other).duration_s)
    assert not fingerprint.is_duplicate(a, 4.0, b, probe(other).duration_s)


def test_landscape_sample_is_not_duplicate_even_within_duration_tolerance(
    sample_video, sample_video_landscape
):
    a = fingerprint.phash_frames(sample_video, 4.0)
    b = fingerprint.phash_frames(sample_video_landscape, 3.0)
    assert not fingerprint.is_duplicate(a, 4.0, b, 3.0)  # durations differ by exactly 1.0 s


def test_is_duplicate_rules():
    same = ["0" * 16] * 8
    close = ["0" * 15 + "f"] * 8  # 4 bits apart
    far = ["f" * 16] * 8
    assert fingerprint.is_duplicate(same, 10.0, close, 10.9)
    assert not fingerprint.is_duplicate(same, 10.0, close, 11.1)  # duration tolerance
    assert not fingerprint.is_duplicate(same, None, close, 10.0)
    assert not fingerprint.is_duplicate(same, 10.0, far, 10.0)
    assert fingerprint.is_duplicate(same, 10.0, close[:6] + far[:2], 10.0)  # 6 of 8 match
    assert not fingerprint.is_duplicate(same, 10.0, close[:5] + far[:3], 10.0)  # only 5
    assert fingerprint.is_duplicate(same[:4], 10.0, close[:3] + far[:1], 10.0)  # 3 of 4 (scaled)
    assert not fingerprint.is_duplicate(same[:4], 10.0, close[:2] + far[:2], 10.0)
    assert not fingerprint.is_duplicate([], 10.0, [], 10.0)
    assert not fingerprint.is_duplicate(same, 10.0, ["zz"] * 8, 10.0)  # malformed hashes
    assert fingerprint.hamming("ff", "0f") == 4


def test_phash_frames_of_unreadable_file_is_empty(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"x" * 100)
    assert fingerprint.phash_frames(bad, 4.0) == []


# --------------------------------------------------------------------------- retention


def _video_with_files(db, workspace, *, status=VideoStatus.READY, encoded=True, age_days=40) -> Video:
    v = Video(workspace_id=workspace.id, status=status, title="clip")
    db.add(v)
    db.flush()
    d = storage.ensure_video_dir(workspace.id, v.id)
    (d / "original.mp4").write_bytes(b"original")
    v.original_path = storage.rel_path(d / "original.mp4")
    if encoded:
        (d / "encoded.mp4").write_bytes(b"encoded")
        (d / "thumb.jpg").write_bytes(b"thumb")
        v.encoded_path = storage.rel_path(d / "encoded.mp4")
        v.thumbnail_path = storage.rel_path(d / "thumb.jpg")
    v.created_at = utcnow() - timedelta(days=age_days)
    db.commit()
    return v


def test_purge_expired_originals(db, workspace, make_account, clean_media):
    old_ready = _video_with_files(db, workspace)
    recent = _video_with_files(db, workspace, age_days=3)
    old_but_recently_published = _video_with_files(db, workspace)
    never_encoded = _video_with_files(db, workspace, status=VideoStatus.FAILED, encoded=False)
    encoded_missing_on_disk = _video_with_files(db, workspace)
    storage.abs_path(encoded_missing_on_disk.encoded_path).unlink()
    old_duplicate = _video_with_files(db, workspace, status=VideoStatus.DUPLICATE, encoded=False)
    archived = _video_with_files(db, workspace, status=VideoStatus.ARCHIVED, age_days=90)
    acc = make_account("tiktok")
    for video, published_days_ago in ((old_but_recently_published, 2), (old_ready, 35)):
        db.add(
            ScheduledPost(
                workspace_id=workspace.id,
                video_id=video.id,
                connected_account_id=acc.id,
                platform=Platform.TIKTOK,
                idempotency_key=f"{video.id}:{acc.id}:0",
                scheduled_at=utcnow(),
                published_at=utcnow() - timedelta(days=published_days_ago),
            )
        )
    db.commit()
    originals = {v.id: storage.abs_path(v.original_path) for v in db.query(Video)}

    removed = retention.purge_expired_originals(db, older_than_days=30)
    db.commit()

    assert removed == 3
    for v in (old_ready, old_duplicate, archived):
        db.refresh(v)
        assert v.original_path is None and not originals[v.id].exists()
    for v in (old_ready, archived):
        assert storage.abs_path(v.encoded_path).is_file() and storage.abs_path(v.thumbnail_path).is_file()
    for v in (recent, old_but_recently_published, never_encoded, encoded_missing_on_disk):
        db.refresh(v)
        assert v.original_path is not None and originals[v.id].is_file()
    # Running again finds nothing to do.
    assert retention.purge_expired_originals(db, older_than_days=30) == 0


def test_purge_clears_path_when_file_already_gone(db, workspace, clean_media):
    v = _video_with_files(db, workspace)
    storage.abs_path(v.original_path).unlink()
    assert retention.purge_expired_originals(db, older_than_days=30) == 0
    db.refresh(v)
    assert v.original_path is None


def test_purge_skips_paths_outside_media_root(db, workspace, clean_media):
    v = _video_with_files(db, workspace)
    v.original_path = "../../etc/passwd"
    db.commit()
    assert retention.purge_expired_originals(db, older_than_days=30) == 0
    db.refresh(v)
    assert v.original_path == "../../etc/passwd"
