"""Tests for media processing: probe, platform encode, thumbnails, fingerprints, retention."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import struct
import subprocess
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from repeater.config import get_settings
from repeater.media import encode, filters, fingerprint, retention, storage
from repeater.media.encode import (
    DiskFullError,
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


_CONTAINER_BOXES = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta", b"dinf"}


def mp4_boxes(path: Path) -> list[str]:
    """Slash-joined paths of every box inside moov (e.g. "moov/trak/edts/elst")."""
    data = path.read_bytes()
    found: list[str] = []

    def walk(start: int, end: int, trail: str) -> None:
        pos = start
        while pos + 8 <= end:
            size, kind = struct.unpack(">I4s", data[pos : pos + 8])
            header = 8
            if size == 1:
                size, header = struct.unpack(">Q", data[pos + 8 : pos + 16])[0], 16
            elif size == 0:
                size = end - pos
            name = f"{trail}/{kind.decode('latin-1')}" if trail else kind.decode("latin-1")
            found.append(name)
            if kind in _CONTAINER_BOXES:
                walk(pos + header, pos + size, name)
            pos += size

    walk(0, len(data), "")
    return found


def decode_rgb(
    path: Path, vf: str = "null", at_s: float = 0.5, size: tuple[int, int] = (1080, 1920)
) -> np.ndarray:
    """One frame as an RGB array, read the way an SDR BT.709 player reads it."""
    w, h = size
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(at_s), "-i", str(path), "-frames:v", "1",
         "-vf", f"{vf},scale={w}:{h}:in_color_matrix=bt709:out_color_matrix=bt709,format=rgb24",
         "-f", "rawvideo", "-"],
        check=True, capture_output=True,
    ).stdout  # fmt: skip
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3).astype(float)


def mean_luma(rgb: np.ndarray) -> float:
    return float((0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]).mean())


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


def test_build_encode_command_matches_plan_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "encode_preset", "veryfast")
    src, out = tmp_path / "in.mov", tmp_path / "out.part"
    cmd = build_encode_command(src, out, has_audio=True, ffmpeg_bin="ffmpeg")
    joined = " ".join(cmd)
    assert (
        "-vf scale=w=trunc(min(iw*sar\\,4096)/2)*2:h=ih,setsar=1,"
        "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,"
        "fps=30,format=yuv420p" in joined
    )
    assert (
        "-c:v libx264 -profile:v high -level 4.0 -preset veryfast -crf 20 -maxrate 12000k -bufsize 24000k "
        "-g 60 -keyint_min 60 -sc_threshold 0 -bf 0" in joined
    )
    assert "-color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range tv" in joined
    assert "-c:a aac -b:a 128k -ar 48000 -ac 2" in joined
    assert "-use_editlist 0 -avoid_negative_ts make_zero -map_metadata -1" in joined
    assert "-movflags +faststart -f mp4" in joined and cmd[-1] == str(out)
    assert "-protocol_whitelist file -i " + str(src) in joined
    assert "-map 0:V:0 -map 0:a:0" in joined and "anullsrc" not in joined
    assert "zscale" not in joined  # SDR input: no colour conversion

    silent = " ".join(build_encode_command(src, out, has_audio=False, ffmpeg_bin="ffmpeg"))
    assert "-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000" in silent
    assert "-map 0:V:0 -map 1:a:0 -shortest" in silent


def test_encode_preset_comes_from_settings(monkeypatch, caplog):
    monkeypatch.setattr(get_settings(), "encode_preset", "ultrafast")
    assert encode.encode_preset() == "ultrafast"
    assert "-preset ultrafast" in " ".join(build_encode_command(Path("a"), Path("b"), has_audio=True))
    monkeypatch.setattr(get_settings(), "encode_preset", "bogus; rm -rf")
    assert encode.encode_preset() == "medium" and "not an x264 preset" in caplog.text


def test_encode_timeout_follows_settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "encode_timeout_base_s", 300)
    monkeypatch.setattr(s, "encode_timeout_per_media_s", 20.0)
    monkeypatch.setattr(s, "encode_timeout_max_s", 1800)
    assert encode.encode_timeout(10) == 500
    assert encode.encode_timeout(60) == 1500
    assert encode.encode_timeout(900) == 1800  # capped
    assert encode.encode_timeout(600) > encode.encode_timeout(60) > encode.encode_timeout(3)
    monkeypatch.setattr(s, "encode_timeout_max_s", 400)
    assert encode.encode_timeout(60) == 400


def test_encode_for_platforms_passes_a_duration_based_timeout(sample_video, tmp_path, monkeypatch):
    seen: list[float] = []
    monkeypatch.setattr(encode, "_run_ffmpeg", lambda cmd, *, timeout_s, **kw: seen.append(timeout_s))
    with pytest.raises(EncodeError, match="no output"):
        encode_for_platforms(sample_video, tmp_path / "o.mp4")
    assert seen == [encode.encode_timeout(probe(sample_video).duration_s)]
    seen.clear()
    with pytest.raises(EncodeError):
        encode_for_platforms(sample_video, tmp_path / "o.mp4", timeout_s=7)
    assert seen == [7.0]  # an explicit timeout still wins


def test_encode_vertical_with_real_profile(encoded_sample, sample_video):
    info = probe(encoded_sample)
    assert (info.width, info.height) == (1080, 1920) and abs(info.fps - 30) < 0.5
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


def test_encode_output_has_no_edit_lists_and_128k_audio(encoded_sample, sample_video):
    """Instagram's Reel spec: no edit lists, moov first, AAC 128 kb/s (import-media-9)."""
    boxes = mp4_boxes(encoded_sample)
    assert "moov/trak/mdia" in boxes and not any("edts" in b or "elst" in b for b in boxes), boxes
    streams = {s["codec_type"]: s for s in ffprobe_streams(encoded_sample)}
    assert 110_000 <= int(streams["audio"]["bit_rate"]) <= 140_000
    assert streams["video"]["start_time"] == "0.000000" and streams["audio"]["start_time"] == "0.000000"
    assert (streams["video"]["color_space"], streams["video"]["color_transfer"]) == ("bt709", "bt709")
    assert (streams["video"]["color_primaries"], streams["video"]["color_range"]) == ("bt709", "tv")
    # The source's own metadata (device, location, creation time) is not carried over.
    tags = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(encoded_sample)],
        check=True,
        capture_output=True,
    ).stdout
    assert set(json.loads(tags).get("format", {}).get("tags", {})) <= {
        "major_brand",
        "minor_version",
        "compatible_brands",
        "encoder",
    }


def test_max_video_kbps_keeps_long_videos_under_the_instagram_cap():
    cap = encode.size_cap_bytes()
    assert cap == 300 * 1024**2  # Instagram, the smallest limit
    assert encode.max_video_kbps(30) == encode.MAX_VIDEO_KBPS  # short videos keep the full ceiling
    for seconds in (200, 420, 900):
        kbps = encode.max_video_kbps(seconds)
        assert kbps < encode.MAX_VIDEO_KBPS
        assert (kbps + encode.AUDIO_KBPS) * 1000 / 8 * seconds <= cap * encode.SIZE_MARGIN * 1.001
    assert encode.max_video_kbps(900) >= 2000  # still a decent picture at the longest allowed length
    cmd = " ".join(build_encode_command(Path("a"), Path("b"), has_audio=True, max_kbps=2500))
    assert "-maxrate 2500k -bufsize 5000k" in cmd


def test_oversized_encode_is_redone_at_a_lower_bitrate(sample_video, tmp_path, monkeypatch):
    """The file must fit the smallest platform limit: if rate control overshoots, encode again."""
    monkeypatch.setattr(get_settings(), "encode_preset", "ultrafast")
    ref = tmp_path / "ref.mp4"
    encode_for_platforms(sample_video, ref)
    cap = int(ref.stat().st_size * 0.6)
    monkeypatch.setattr(encode, "size_cap_bytes", lambda: cap)
    seen_kbps: list[int] = []
    real = encode.build_encode_command

    def spy(*a, **kw):
        seen_kbps.append(kw["max_kbps"])
        return real(*a, **kw)

    monkeypatch.setattr(encode, "build_encode_command", spy)
    out = tmp_path / "capped.mp4"
    encode_for_platforms(sample_video, out)
    assert out.stat().st_size <= cap
    assert len(seen_kbps) >= 2 and seen_kbps[1] < seen_kbps[0]
    # A file that cannot be made small enough is an error, not a silent over-limit upload.
    monkeypatch.setattr(encode, "size_cap_bytes", lambda: 2000)
    with pytest.raises(EncodeError, match="limit of the smallest platform"):
        encode_for_platforms(sample_video, tmp_path / "never.mp4")
    assert not (tmp_path / "never.mp4").exists() and not list(tmp_path.glob(".*.part"))


def test_ffmpeg_disk_full_is_its_own_retryable_error(tmp_path):
    full = ["sh", "-c", "echo 'av_interleaved_write_frame(): No space left on device' >&2; exit 1"]
    with pytest.raises(DiskFullError) as exc:
        encode._run_ffmpeg(full, timeout_s=5, what="encoding")
    assert not isinstance(exc.value, EncodeError)  # the importer must not fail the video for it
    assert isinstance(exc.value, MediaToolError)
    with pytest.raises(EncodeError):
        encode._run_ffmpeg(["sh", "-c", "echo 'Invalid data' >&2; exit 1"], timeout_s=5, what="encoding")


# --------------------------------------------------------------------------- colour (HDR) and geometry


def _hdr_clip(path: Path, *, kind: str, base: Path, rotate: bool = False) -> Path:
    """An iPhone/Android-style HEVC Main10 clip tagged BT.2020 HLG or PQ (BT.2408: SDR white is
    203 cd/m2), made from the SDR clip `base` with zscale. `rotate` adds the phone's display matrix."""
    transfer = {"hlg": "arib-std-b67", "pq": "smpte2084"}[kind]
    vf = (
        f"zscale=tin=bt709:pin=bt709:min=bt709:rin=tv:t={transfer}:p=bt2020:m=bt2020nc:r=tv:npl=203,"
        "format=yuv420p10le"
    )
    plain = path.with_name(f"{path.stem}.plain.mkv") if rotate else path
    ffmpeg(
        "-i", str(base), "-vf", vf, "-c:v", "libx265", "-preset", "ultrafast", "-crf", "8",
        "-x265-params", f"colorprim=bt2020:transfer={transfer}:colormatrix=bt2020nc:range=limited:log-level=error",
        "-tag:v", "hvc1", "-an", str(plain),
    )  # fmt: skip
    if rotate:
        ffmpeg("-display_rotation", "-90", "-i", str(plain), "-c", "copy", str(path))
    return path


@pytest.fixture(scope="module")
def sdr_reference(clips: Path) -> Path:
    """SDR BT.709 pictures with saturated colours, portrait, the way a phone records them."""
    out = clips / "sdr_reference.mp4"
    ffmpeg(
        "-f", "lavfi", "-i", "mandelbrot=size=360x640:rate=30,trim=duration=1,setpts=PTS-STARTPTS,format=yuv420p",
        "-c:v", "libx264", "-crf", "10", "-preset", "ultrafast",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", str(out),
    )  # fmt: skip
    return out


@pytest.mark.skipif(not filters.hdr_tools_available(), reason="ffmpeg without zscale/tonemap")
@pytest.mark.parametrize("kind", ["hlg", "pq"])
def test_hdr_sources_are_tone_mapped_to_tagged_sdr(kind, clips, sdr_reference, tmp_path, monkeypatch):
    """import-media-3: HDR was truncated to 8 bit and kept its BT.2020/HLG tags, so every SDR
    player showed it dull and dark. Now the output is BT.709 SDR and looks like the SDR original."""
    monkeypatch.setattr(get_settings(), "encode_preset", "ultrafast")
    hdr = _hdr_clip(clips / f"{kind}.mkv", kind=kind, base=sdr_reference)
    info = probe(hdr)
    assert info.is_hdr and info.bit_depth == 10 and info.color_primaries == "bt2020"
    out = tmp_path / f"{kind}.mp4"
    encode_for_platforms(hdr, out)

    v = next(s for s in ffprobe_streams(out) if s["codec_type"] == "video")
    assert v["pix_fmt"] == "yuv420p" and v["codec_name"] == "h264"
    assert (v["color_space"], v["color_transfer"], v["color_primaries"]) == ("bt709", "bt709", "bt709")

    reference = decode_rgb(sdr_reference, encode.video_filter(None))
    converted = decode_rgb(out)
    naive = tmp_path / "naive.mp4"  # what the old filter graph produced: no conversion at all
    ffmpeg(
        "-i",
        str(hdr),
        "-vf",
        encode.video_filter(None),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        str(naive),
    )
    old = decode_rgb(naive)
    assert np.abs(reference - converted).mean() < 8
    assert abs(mean_luma(converted) / mean_luma(reference) - 1) < 0.05  # not washed out, not dark
    assert np.abs(reference - old).mean() > 20 and mean_luma(old) < 0.95 * mean_luma(reference)


@pytest.mark.skipif(not filters.hdr_tools_available(), reason="ffmpeg without zscale/tonemap")
def test_rotated_hlg_phone_clip_keeps_orientation_and_colours(clips, sdr_reference, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "encode_preset", "ultrafast")
    landscape_sdr = clips / "sdr_landscape.mp4"
    ffmpeg("-i", str(sdr_reference), "-vf", "transpose=2", "-c:v", "libx264", "-crf", "10", "-preset", "ultrafast",
           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", str(landscape_sdr))  # fmt: skip
    hdr = _hdr_clip(clips / "hlg_rot.mov", kind="hlg", base=landscape_sdr, rotate=True)
    assert probe(hdr).rotation == 90 and (probe(hdr).width, probe(hdr).height) == (360, 640)
    out = tmp_path / "rot.mp4"
    info = encode_for_platforms(hdr, out)
    assert (info.width, info.height, info.rotation) == (1080, 1920, 0)
    converted = decode_rgb(out)
    reference = decode_rgb(sdr_reference, encode.video_filter(None))
    assert (
        np.abs(reference - converted).mean() < 12
        and abs(mean_luma(converted) / mean_luma(reference) - 1) < 0.08
    )


def test_hdr_filter_chain_is_built_from_the_probe():
    base = {
        "duration_s": 5.0, "width": 1080, "height": 1920, "fps": 30.0, "video_codec": "hevc",
        "audio_codec": None, "has_audio": False, "size_bytes": 1, "pix_fmt": "yuv420p10le", "bit_depth": 10,
        "color_space": "bt2020nc", "color_primaries": "bt2020", "color_range": "tv",
    }  # fmt: skip
    hlg = filters.colour_filter(ProbeResult(**base, color_transfer="arib-std-b67"))
    assert "tin=arib-std-b67:pin=bt2020:min=bt2020nc:rin=tv:t=linear:npl=203" in hlg
    assert "tonemap=tonemap=mobius:param=0.8:peak=4.93" in hlg  # 1000 cd/m2 / 203
    assert hlg.endswith("t=bt709:m=bt709:r=tv:dither=ordered,format=yuv420p")
    pq = filters.colour_filter(ProbeResult(**base, color_transfer="smpte2084", peak_nits=4000.0))
    assert "tin=smpte2084" in pq and "peak=19.70" in pq  # HDR10 MaxCLL / mastering peak is honoured
    pq_default = filters.colour_filter(ProbeResult(**base, color_transfer="smpte2084"))
    assert "peak=4.93" in pq_default
    wide = filters.colour_filter(ProbeResult(**base, color_transfer="bt2020-10"))
    assert "tonemap" not in wide and "tin=bt2020-10" in wide and "p=bt709:t=bt709" in wide
    sdr = ProbeResult(
        **{**base, "color_space": "bt709", "color_primaries": "bt709", "bit_depth": 8, "pix_fmt": "yuv420p"},
        color_transfer="bt709",
    )
    assert filters.colour_filter(sdr) == "" and filters.colour_filter(None) == ""


def test_hdr_without_zscale_is_a_tool_error_not_a_wrong_picture(monkeypatch):
    monkeypatch.setattr(filters, "hdr_tools_available", lambda *a, **k: False)
    hlg = ProbeResult(5.0, 1080, 1920, 30.0, "hevc", None, False, 1, color_transfer="arib-std-b67")
    with pytest.raises(filters.HdrToolsMissing, match="zscale") as exc:
        encode.video_filter(hlg)
    assert isinstance(exc.value, MediaToolError)  # the importer retries it, then reports it
    assert "zscale" not in encode.video_filter(ProbeResult(5.0, 1080, 1920, 30.0, "h264", None, False, 1))


def test_filter_listing_is_parsed_from_ffmpeg(monkeypatch):
    filters._filter_names.cache_clear()
    names = filters._filter_names("ffmpeg")
    assert {"scale", "pad", "fps", "format"} <= names
    assert filters.hdr_tools_available("ffmpeg") == ({"zscale", "tonemap"} <= names)
    assert filters.hdr_tools_available("/nonexistent/ffmpeg") is False


def _anamorphic(path: Path, *, size: str, sar: str) -> Path:
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration=2,setsar={sar.replace(':', '/')}",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path))  # fmt: skip
    return path


def test_probe_applies_the_pixel_aspect_ratio(clips):
    """import-media-12: a 1440x1080 clip with 4:3 pixels is shown 1920x1080, not 1440x1080."""
    hdv = _anamorphic(clips / "hdv.mp4", size="1440x1080", sar="4:3")
    info = probe(hdv)
    assert (info.width, info.height) == (1920, 1080) and info.is_landscape
    square = _anamorphic(clips / "square_pixels.mp4", size="1440x1080", sar="1:1")
    assert (probe(square).width, probe(square).height) == (1440, 1080)
    tall = _anamorphic(clips / "narrow.mp4", size="1080x1080", sar="4:3")
    assert (probe(tall).width, probe(tall).height) == (1440, 1080)


def test_anamorphic_source_is_encoded_with_square_pixels(clips, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "encode_preset", "ultrafast")
    hdv = _anamorphic(clips / "hdv2.mp4", size="1440x1080", sar="4:3")
    out = tmp_path / "hdv.enc.mp4"
    info = encode_for_platforms(hdv, out)
    v = next(s for s in ffprobe_streams(out) if s["codec_type"] == "video")
    assert (info.width, info.height) == (1080, 1920)
    assert v["sample_aspect_ratio"] == "1:1" and v["display_aspect_ratio"] == "9:16"
    # 1920x1080 on display: fitted to 1080 wide gives a 608 px band, not the 810 px of the coded size.
    frame = decode_rgb(out)
    content_rows = [y for y in range(0, 1920, 8) if frame[y].max() > 40]
    assert 590 <= (content_rows[-1] - content_rows[0]) <= 625


def test_parse_probe_json_reads_colour_tags_and_hdr_peak():
    info = parse_probe_json(
        _probe_json(
            pix_fmt="yuv420p10le",
            color_space="bt2020nc",
            color_transfer="smpte2084",
            color_primaries="bt2020",
            color_range="tv",
            side_data_list=[
                {"side_data_type": "Mastering display metadata", "max_luminance": "10000000/10000"},
                {"side_data_type": "Content light level metadata", "max_content": 1200, "max_average": 300},
            ],
        )
    )
    assert info.is_hdr and info.bit_depth == 10 and info.peak_nits == 1200.0
    assert (info.color_space, info.color_transfer, info.color_primaries, info.color_range) == (
        "bt2020nc", "smpte2084", "bt2020", "tv",
    )  # fmt: skip
    only_mastering = parse_probe_json(
        _probe_json(
            color_transfer="arib-std-b67",
            side_data_list=[{"side_data_type": "Mastering display metadata", "max_luminance": "4000/1"}],
        )
    )
    assert only_mastering.peak_nits == 4000.0 and only_mastering.is_hdr
    plain = parse_probe_json(_probe_json(pix_fmt="yuv420p", color_transfer="bt709", color_primaries="bt709"))
    assert (
        not plain.is_hdr and not plain.is_wide_gamut_sdr and plain.bit_depth == 8 and plain.peak_nits is None
    )
    wide = parse_probe_json(_probe_json(color_primaries="smpte432", color_transfer="iec61966-2-1"))
    assert wide.is_wide_gamut_sdr and not wide.is_hdr
    # Values from an untrusted file only reach ffmpeg's filter graph when they are known names.
    odd = parse_probe_json(
        _probe_json(color_transfer="smpte2084,evil=1", color_space="x:y", color_range="pc")
    )
    assert odd.color_transfer == "" and odd.color_space == "" and odd.color_range == "pc"
    # Absurd pixel aspect ratios are ignored rather than producing a gigantic picture.
    assert parse_probe_json(_probe_json(sample_aspect_ratio="1000:1")).width == 1920
    assert parse_probe_json(_probe_json(sample_aspect_ratio="0:1")).width == 1920


def test_encode_landscape_is_padded_to_vertical_30fps(sample_video_landscape, tmp_path):
    out = encode_for_platforms(sample_video_landscape, tmp_path / "land.mp4")
    assert (out.width, out.height) == (1080, 1920) and abs(out.fps - 30) < 0.5
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


def test_thumbnail_seek_past_the_video_stream_falls_back_to_the_first_frame(tmp_path):
    """A short video track with longer audio: the seek finds no frame. ffmpeg 6.x exits 0 without
    output, 7.x exits non-zero ("Non full-range YUV is non-standard"); both give the first frame."""
    clip = tmp_path / "short_video_long_audio.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip),
        ],
        check=True,
    )  # fmt: skip
    thumb = make_thumbnail(clip, tmp_path / "t.jpg", at_s=3.0)
    with Image.open(thumb) as img:
        assert img.format == "JPEG" and img.size == (540, 960)


def test_thumbnail_retries_at_zero_when_the_seeked_attempt_exits_non_zero(tmp_path, monkeypatch):
    calls: list[float] = []

    def fake_run(cmd, *, timeout_s, what, redact=()):
        position = float(cmd[cmd.index("-ss") + 1])
        calls.append(position)
        if position > 0:
            raise EncodeError("thumbnail failed (ffmpeg exit code 234)")
        Path(cmd[-1]).write_bytes(b"\xff\xd8jpeg")

    monkeypatch.setattr(encode, "_run_ffmpeg", fake_run)
    out = make_thumbnail(tmp_path / "in.mp4", tmp_path / "t.jpg", at_s=2.5)
    assert out.read_bytes().startswith(b"\xff\xd8") and calls == [2.5, 0.0]
    # A real failure at the start is still an error, and a timeout is not retried.
    monkeypatch.setattr(encode, "_run_ffmpeg", lambda *a, **k: (_ for _ in ()).throw(EncodeError("bad")))
    with pytest.raises(EncodeError):
        make_thumbnail(tmp_path / "in.mp4", tmp_path / "u.jpg", at_s=1.0)
    attempts: list[int] = []

    def slow(*a, **k):
        attempts.append(1)
        raise EncodeTimeout("thumbnail timed out")

    monkeypatch.setattr(encode, "_run_ffmpeg", slow)
    with pytest.raises(EncodeTimeout):
        make_thumbnail(tmp_path / "in.mp4", tmp_path / "v.jpg", at_s=1.0)
    assert attempts == [1]


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


# --------------------------------------------------------------------------- display-normalised and audio fingerprints


def _picture_audio_clip(path: Path, *, size: str, notes: list[int], seconds: int = 6, rate: int = 30) -> Path:
    """Moving fractal pictures with a melody (one note sequence per audio identity)."""
    expr = "+".join(
        f"0.3*sin(2*PI*t*{220 * 2 ** (n / 12):.3f})*between(mod(t*3\\,{len(notes)})\\,{i}\\,{i + 1})"
        for i, n in enumerate(notes)
    )
    ffmpeg(
        "-f", "lavfi", "-i", f"mandelbrot=size={size}:rate={rate},trim=duration={seconds},setpts=PTS-STARTPTS",
        "-f", "lavfi", "-i", f"aevalsrc='{expr}':d={seconds}:s=44100",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    )  # fmt: skip
    return path


MELODY_A = [0, 4, 7, 12, 7, 4, 0, -5]
MELODY_B = [2, 5, 9, 14, 9, 5, 2, -3]


def _fingerprint(path: Path) -> tuple[list[str], float, str | None]:
    info = probe(path)
    fp = fingerprint.fingerprint_video(path, info)
    return fp.frames, info.duration_s, fp.audio


@pytest.mark.parametrize("size", ["640x360", "640x800", "640x640", "360x640"])
def test_letterboxed_repost_fingerprints_like_the_original(size, clips, tmp_path, monkeypatch):
    """import-media-2: Repeater's own 1080x1920 letterboxed post, re-encoded by the platform at
    720p, came back as a new video because the original was hashed un-padded."""
    monkeypatch.setattr(get_settings(), "encode_preset", "ultrafast")
    original = _picture_audio_clip(clips / f"orig_{size}.mp4", size=size, notes=MELODY_A)
    posted = tmp_path / "posted.mp4"
    encode_for_platforms(original, posted)
    platform_copy = tmp_path / "platform_copy.mp4"
    ffmpeg("-i", str(posted), "-vf", "scale=720:1280", "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
           "-c:a", "aac", "-b:a", "128k", str(platform_copy))  # fmt: skip
    a_frames, a_dur, a_audio = _fingerprint(original)
    for other in (posted, platform_copy):
        b_frames, b_dur, b_audio = _fingerprint(other)
        distances = [fingerprint.hamming(x, y) for x, y in zip(a_frames, b_frames, strict=True)]
        assert max(distances) <= fingerprint.MAX_FRAME_DISTANCE, distances
        assert a_audio and b_audio and fingerprint.audio_match(a_audio, b_audio) is True
        assert fingerprint.is_duplicate(a_frames, a_dur, b_frames, b_dur, a_audio, b_audio)


def test_same_pictures_with_different_sound_are_different_videos(clips, tmp_path):
    """import-media-8: a series over the same b-roll with a new voice-over is not a duplicate."""
    one = _picture_audio_clip(clips / "ep1.mp4", size="360x640", notes=MELODY_A)
    two = _picture_audio_clip(clips / "ep2.mp4", size="360x640", notes=MELODY_B)
    a_frames, a_dur, a_audio = _fingerprint(one)
    b_frames, b_dur, b_audio = _fingerprint(two)
    assert all(
        fingerprint.hamming(x, y) <= 2 for x, y in zip(a_frames, b_frames, strict=True)
    )  # same pictures
    assert a_audio and b_audio and fingerprint.audio_match(a_audio, b_audio) is False
    assert not fingerprint.is_duplicate(a_frames, a_dur, b_frames, b_dur, a_audio, b_audio)
    # ...while the same pictures with the same sound, re-encoded the way a platform does, still are.
    copy = clips / "ep1_copy.mp4"
    ffmpeg("-i", str(one), "-vf", "scale=270:480", "-c:v", "libx264", "-crf", "30", "-preset", "ultrafast",
           "-c:a", "aac", "-b:a", "64k", "-ar", "44100", "-ac", "1", str(copy))  # fmt: skip
    c_frames, c_dur, c_audio = _fingerprint(copy)
    assert fingerprint.is_duplicate(a_frames, a_dur, c_frames, c_dur, a_audio, c_audio)


def test_audio_that_cannot_be_compared_does_not_veto_a_picture_match(clips, tmp_path):
    melody = _picture_audio_clip(clips / "with_sound.mp4", size="360x640", notes=MELODY_A)
    silent = tmp_path / "muted.mp4"  # a platform muted the track (copyright), Repeater adds silence
    ffmpeg("-i", str(melody), "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
           "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-shortest", str(silent))  # fmt: skip
    mute = tmp_path / "no_track.mp4"
    ffmpeg("-i", str(melody), "-an", "-c:v", "copy", str(mute))
    a_frames, a_dur, a_audio = _fingerprint(melody)
    assert a_audio is not None
    for other in (silent, mute):
        b_frames, b_dur, b_audio = _fingerprint(other)
        assert b_audio is None  # silence and a missing track are "unknown", not "different"
        assert fingerprint.is_duplicate(a_frames, a_dur, b_frames, b_dur, a_audio, b_audio)
    short = tmp_path / "short.mp4"
    ffmpeg("-i", str(melody), "-t", "1.5", "-c", "copy", str(short))
    assert fingerprint.audio_fingerprint(short) is None  # too short for Chromaprint


def _token(items: np.ndarray) -> str:
    raw = items.astype("<u4").tobytes()
    return fingerprint.AUDIO_PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_audio_match_rules():
    rng = np.random.default_rng(7)
    a = rng.integers(0, 2**32, size=200, dtype=np.uint64).astype("<u4")
    noisy = a ^ (
        rng.integers(0, 2**32, size=200, dtype=np.uint64).astype("<u4")
        & rng.integers(0, 2**32, size=200, dtype=np.uint64).astype("<u4")
        & rng.integers(0, 2**32, size=200, dtype=np.uint64).astype("<u4")
    )
    other = rng.integers(0, 2**32, size=200, dtype=np.uint64).astype("<u4")
    assert fingerprint.audio_match(_token(a), _token(a)) is True
    assert fingerprint.audio_match(_token(a), _token(noisy)) is True  # ~12% of the bits flipped
    assert fingerprint.audio_match(_token(a), _token(other)) is False  # ~50%
    assert fingerprint.audio_match(_token(a), _token(np.concatenate([other[:3], a]))) is True  # shifted by 3
    assert fingerprint.audio_match(_token(a), _token(a[:40])) is True  # shorter copy (overlap is enough)
    assert fingerprint.audio_match(_token(a), None) is None and fingerprint.audio_match(None, None) is None
    assert fingerprint.audio_match(_token(a), "a:not-base64!!") is None
    assert fingerprint.audio_match(_token(a), _token(a[:5])) is None  # too short to judge


def test_pack_and_unpack_round_trip():
    frames = ["00" * 8, "ff" * 8]
    text = fingerprint.pack(frames, "a:abc")
    assert text == "0000000000000000 ffffffffffffffff a:abc"
    assert fingerprint.unpack(text) == (frames, "a:abc")
    assert fingerprint.unpack("0000000000000000") == (["0000000000000000"], None)  # no audio part
    assert fingerprint.unpack(None) == ([], None) and fingerprint.pack([], None) is None


def test_fingerprint_reports_missing_samples_at_the_end(clips, tmp_path):
    full = make_clip(clips / "fp_full.mp4", size="360x640", seconds=8, extra=("-movflags", "+faststart"))
    cut = tmp_path / "cut.mp4"
    cut.write_bytes(full.read_bytes()[: int(full.stat().st_size * 0.4)])
    info = probe(cut)  # the moov atom is at the front: the duration is still the full one
    assert info.duration_s > 7
    fp = fingerprint.fingerprint_video(cut, info)
    assert 0 < len(fp.frames) < fingerprint.PHASH_FRAMES and fp.tail_missing >= 3
    assert fingerprint.fingerprint_video(full, probe(full)).tail_missing == 0


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


def test_purge_removes_originals_of_old_failed_videos(db, workspace, clean_media):
    """import-media-14: originals of FAILED videos (up to 4 GB) were never purged."""
    old_failed = _video_with_files(db, workspace, status=VideoStatus.FAILED, encoded=False)
    recent_failed = _video_with_files(db, workspace, status=VideoStatus.FAILED, encoded=False)
    old_processing = _video_with_files(db, workspace, status=VideoStatus.PROCESSING, encoded=False)
    for v, days in ((old_failed, 8), (recent_failed, 3), (old_processing, 40)):
        v.updated_at = utcnow() - timedelta(days=days)
    db.commit()
    paths = {v.id: storage.abs_path(v.original_path) for v in (old_failed, recent_failed, old_processing)}

    assert retention.purge_expired_originals(db, older_than_days=30) == 0  # failed ones are opt-in there
    assert retention.purge_failed_originals(db) == 1
    db.commit()
    db.refresh(old_failed)
    assert old_failed.original_path is None and not paths[old_failed.id].exists()
    for v in (recent_failed, old_processing):
        db.refresh(v)
        assert v.original_path is not None and paths[v.id].is_file()
    assert retention.purge_failed_originals(db) == 0
    # The window is configurable and counted from the last change; one pass can do both kinds.
    assert retention.purge_expired_originals(db, older_than_days=30, failed_older_than_days=1) == 1
    db.refresh(recent_failed)
    assert recent_failed.original_path is None and paths[old_processing.id].is_file()


def test_sweep_partial_files_removes_only_abandoned_ones(db, workspace, clean_media):
    import os

    video_dir = storage.ensure_video_dir(workspace.id, 1)
    old, fresh = utcnow().timestamp() - 3 * 86400, utcnow().timestamp() - 600
    stale_encode = video_dir / ".encoded.mp4.0123456789ab.part"
    stale_download = video_dir / "original.mp4.part"
    running = video_dir / ".encoded.mp4.ba9876543210.part"
    keep = video_dir / "encoded.mp4"
    upload = storage.uploads_dir() / "abc.part"
    for path, mtime in (
        (stale_encode, old),
        (stale_download, old),
        (running, fresh),
        (keep, old),
        (upload, old),
    ):
        path.write_bytes(b"x")
        os.utime(path, (mtime, mtime))
    assert retention.sweep_partial_files() == 3
    assert not stale_encode.exists() and not stale_download.exists() and not upload.exists()
    assert running.exists() and keep.exists()
    assert retention.sweep_partial_files() == 0
