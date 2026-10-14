"""The single platform-wide encoding profile (PLAN.md section 5.4) and thumbnails.

One MP4 per video is accepted by TikTok, YouTube Shorts, Instagram Reels and Facebook Reels:
1080x1920 (scale to fit + pad), 30 fps, SDR BT.709 yuv420p tagged as such (HDR and wide-gamut
sources are tone-mapped, see repeater.media.filters), H.264 High@4.0 with a closed 2-second GOP,
AAC-LC 128 kb/s 48 kHz stereo, moov atom first (+faststart) and no edit lists (Instagram's Reel
spec: no B-frames, so there is no start delay to hide). The x264 preset comes from settings.encode_preset; the video bitrate is capped so that the
file stays under the smallest platform size limit (Instagram's 300 MB), and the file is encoded
again at a lower bitrate if it still comes out larger.
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from pathlib import Path

from repeater.config import get_settings
from repeater.media.filters import picture_filter
from repeater.media.probe import SAFE_INPUT_ARGS, MediaToolError, ProbeResult, probe
from repeater.platforms.base import LIMITS

log = logging.getLogger("repeater.media")

TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
TARGET_FPS = 30
AUDIO_KBPS = 128
AUDIO_CODEC_ARGS: tuple[str, ...] = ("-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ar", "48000", "-ac", "2")
# Some platforms reject files without an audio track, so silent stereo is added when missing.
SILENT_AUDIO_SOURCE = "anullsrc=channel_layout=stereo:sample_rate=48000"

# Fastest first.
X264_PRESET_ORDER = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
)
X264_PRESETS = frozenset(X264_PRESET_ORDER)
DEFAULT_PRESET = "medium"
CRF = 20
MAX_VIDEO_KBPS = 12000
MIN_VIDEO_KBPS = 300
# Aim this far below the smallest platform size limit (rate control is not exact).
SIZE_MARGIN = 0.9
MAX_SIZE_ATTEMPTS = 3
# Container flags: no edit lists (Instagram's Reel spec; ffmpeg writes them for B-frame delay and AAC
# priming), timestamps from 0, and nothing from the source's metadata (GPS, device) is kept.
# Without B-frames (-bf 0, about 2% larger files) video has no start delay, so audio and video both
# start at 0 without an edit list to hide the offset.
MP4_ARGS: tuple[str, ...] = (
    "-use_editlist", "0", "-avoid_negative_ts", "make_zero",
    "-map_metadata", "-1", "-map_chapters", "-1",
    "-movflags", "+faststart", "-f", "mp4",
)  # fmt: skip
COLOUR_TAG_ARGS: tuple[str, ...] = (
    "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
)  # fmt: skip

COLOUR_TAGS_FILTER = "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"

THUMB_WIDTH = 540
THUMB_HEIGHT = 960
THUMB_FILTER = (
    f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=decrease,"
    f"pad={THUMB_WIDTH}:{THUMB_HEIGHT}:(ow-iw)/2:(oh-ih)/2"
)
STDERR_TAIL_CHARS = 1500
_DISK_FULL_MARKERS = ("no space left on device", "disk quota exceeded")


class EncodeError(MediaToolError):
    """ffmpeg exited with an error. `stderr_tail` holds the end of its error output."""

    def __init__(self, message: str, *, stderr_tail: str = "") -> None:
        super().__init__(f"{message}: {stderr_tail}" if stderr_tail else message)
        self.stderr_tail = stderr_tail


class EncodeTimeout(EncodeError):
    """ffmpeg did not finish within the allowed time (it was killed)."""


class DiskFullError(MediaToolError):
    """The disk filled up while ffmpeg was writing. Not the file's fault, so (unlike EncodeError)
    it is worth retrying later."""


def encode_preset() -> str:
    """settings.encode_preset when it is a real x264 preset, else "medium"."""
    preset = (get_settings().encode_preset or "").strip().lower()
    if preset in X264_PRESETS:
        return preset
    log.warning("encode_preset %r is not an x264 preset; using %s", preset, DEFAULT_PRESET)
    return DEFAULT_PRESET


def faster_preset(configured: str, ceiling: str) -> str:
    """The faster of two x264 presets: `configured`, unless it is slower than `ceiling`."""
    order = X264_PRESET_ORDER
    return configured if order.index(configured) <= order.index(ceiling) else ceiling


def encode_timeout(duration_s: float) -> float:
    """min(encode_timeout_max_s, encode_timeout_base_s + encode_timeout_per_media_s * duration)."""
    s = get_settings()
    return float(
        min(s.encode_timeout_max_s, s.encode_timeout_base_s + s.encode_timeout_per_media_s * duration_s)
    )


def size_cap_bytes() -> int:
    """The smallest file size any publishing platform accepts (Instagram: 300 MB)."""
    return min(lim.max_size_bytes for lim in LIMITS.values())


def max_video_kbps(duration_s: float, cap_bytes: int | None = None) -> int:
    """Video bitrate ceiling that keeps a `duration_s` file under SIZE_MARGIN of the size cap."""
    cap = cap_bytes or size_cap_bytes()
    budget = SIZE_MARGIN * cap * 8 / 1000 / max(duration_s, 1.0) - AUDIO_KBPS
    return int(max(MIN_VIDEO_KBPS, min(MAX_VIDEO_KBPS, budget)))


def video_codec_args(preset: str | None = None, max_kbps: int = MAX_VIDEO_KBPS) -> list[str]:
    return [
        "-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
        "-preset", preset or encode_preset(), "-crf", str(CRF),
        "-maxrate", f"{max_kbps}k", "-bufsize", f"{2 * max_kbps}k",
        "-g", "60", "-keyint_min", "60", "-sc_threshold", "0", "-bf", "0",
    ]  # fmt: skip


def video_filter(info: ProbeResult | None = None) -> str:
    """The -vf graph: display-normalised 1080x1920 canvas, 30 fps, 8-bit 4:2:0, tagged BT.709 limited
    range. The tags are set on the frames as well as on the encoder (COLOUR_TAG_ARGS): ffmpeg 7.x
    takes the stream's colour description from the frames and ignores the encoder options."""
    return f"{picture_filter(info, TARGET_WIDTH, TARGET_HEIGHT)},fps={TARGET_FPS},format=yuv420p,{COLOUR_TAGS_FILTER}"


def build_encode_command(
    src: Path,
    out: Path,
    *,
    has_audio: bool,
    ffmpeg_bin: str | None = None,
    info: ProbeResult | None = None,
    max_kbps: int = MAX_VIDEO_KBPS,
    preset: str | None = None,
) -> list[str]:
    """The exact ffmpeg argv for the platform profile; `out` is written as MP4 whatever its name.
    `info` (the source's probe) selects the HDR/wide-gamut conversion; without it the source is
    assumed to be SDR BT.709."""
    cmd = [ffmpeg_bin or get_settings().ffmpeg_bin, "-hide_banner", "-nostdin", "-v", "error", "-y"]
    cmd += [*SAFE_INPUT_ARGS, "-i", str(src)]
    if has_audio:
        maps = ["-map", "0:V:0", "-map", "0:a:0"]
    else:
        cmd += ["-f", "lavfi", "-i", SILENT_AUDIO_SOURCE]
        maps = ["-map", "0:V:0", "-map", "1:a:0", "-shortest"]
    cmd += maps
    cmd += [
        "-vf",
        video_filter(info),
        *video_codec_args(preset, max_kbps),
        *COLOUR_TAG_ARGS,
        *AUDIO_CODEC_ARGS,
    ]
    cmd += [*MP4_ARGS, str(out)]
    return cmd


def encode_for_platforms(
    src: Path | str,
    dst: Path | str,
    *,
    timeout_s: float | None = None,
    source_probe: ProbeResult | None = None,
    preset: str | None = None,
) -> ProbeResult:
    """Encode `src` into `dst` with the platform profile and return the probe of the result.
    The output appears atomically (temp file + rename). `timeout_s` applies to each ffmpeg run and
    defaults to encode_timeout(duration); `preset` overrides settings.encode_preset (a retry after
    a timeout uses a faster one). Raises EncodeTimeout, EncodeError, DiskFullError or
    MediaToolError (ffmpeg missing, or no HDR tools for an HDR source)."""
    src_p, dst_p = Path(src).resolve(), Path(dst)
    info = source_probe or probe(src_p)
    timeout = float(timeout_s) if timeout_s is not None else encode_timeout(info.duration_s)
    dst_p.parent.mkdir(parents=True, exist_ok=True)
    cap = size_cap_bytes()
    kbps = max_video_kbps(info.duration_s, cap)
    tmp = _temp_path(dst_p)
    try:
        for attempt in range(1, MAX_SIZE_ATTEMPTS + 1):
            _run_ffmpeg(
                build_encode_command(
                    src_p, tmp, has_audio=info.has_audio, info=info, max_kbps=kbps, preset=preset
                ),
                timeout_s=timeout,
                what="encoding",
                redact=(src_p, tmp),
            )
            if not tmp.is_file() or tmp.stat().st_size == 0:
                raise EncodeError("encoding produced no output")
            size = tmp.stat().st_size
            if size <= cap:
                break
            if attempt == MAX_SIZE_ATTEMPTS or kbps <= MIN_VIDEO_KBPS:
                raise EncodeError(
                    f"the encoded file is {size / 1024**2:.0f} MB, over the {cap / 1024**2:.0f} MB "
                    "limit of the smallest platform"
                )
            kbps = max(MIN_VIDEO_KBPS, int(kbps * SIZE_MARGIN * cap / size))
            log.info("encode of %s is %d MB, retrying at %d kb/s", dst_p.name, size // 1024**2, kbps)
        os.replace(tmp, dst_p)
    finally:
        tmp.unlink(missing_ok=True)
    return probe(dst_p)


def make_thumbnail(src: Path | str, dst: Path | str, at_s: float, *, timeout_s: float = 120) -> Path:
    """Write a 540x960 JPEG of the frame at `at_s` seconds. When that seek finds no frame (past the
    end of the video stream, e.g. audio that outlasts a short video track) the first frame is used."""
    src_p, dst_p = Path(src).resolve(), Path(dst)
    dst_p.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_path(dst_p)
    first = max(0.0, float(at_s))
    positions = (first, 0.0) if first > 0 else (0.0,)
    try:
        for position in positions:
            try:
                _run_ffmpeg(
                    _thumbnail_command(src_p, tmp, position),
                    timeout_s=timeout_s,
                    what="thumbnail",
                    redact=(src_p, tmp),
                )
            except EncodeTimeout:
                raise
            except EncodeError:
                # Depending on the ffmpeg version, a seek past the last frame exits non-zero (7.x)
                # or with no output (6.x). Only a failure at the very start is a real failure.
                if position == positions[-1]:
                    raise
                continue
            if tmp.is_file() and tmp.stat().st_size > 0:
                os.replace(tmp, dst_p)
                return dst_p
        raise EncodeError("thumbnail: no frame could be extracted")
    finally:
        tmp.unlink(missing_ok=True)


def _thumbnail_command(src: Path, out: Path, at_s: float) -> list[str]:
    # yuvj420p (full-range YUV) is what mjpeg expects; without it ffmpeg 7.x refuses to open the
    # encoder when the seek yields no frame ("Non full-range YUV is non-standard").
    return [
        get_settings().ffmpeg_bin, "-hide_banner", "-nostdin", "-v", "error", "-y",
        "-ss", f"{at_s:.3f}", *SAFE_INPUT_ARGS, "-i", str(src),
        "-frames:v", "1", "-vf", THUMB_FILTER, "-pix_fmt", "yuvj420p", "-q:v", "3",
        "-f", "image2", "-c:v", "mjpeg", "-update", "1", str(out),
    ]  # fmt: skip


def _run_ffmpeg(cmd: list[str], *, timeout_s: float, what: str, redact: tuple[Path, ...] = ()) -> None:
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MediaToolError(f"ffmpeg not found ({cmd[0]}); install ffmpeg") from exc
    except subprocess.TimeoutExpired as exc:
        raise EncodeTimeout(
            f"{what} timed out after {int(timeout_s)} s", stderr_tail=_tail(exc.stderr, redact)
        ) from exc
    if proc.returncode != 0:
        if _disk_full(proc.stderr):
            raise DiskFullError(f"{what} failed: the disk is full")
        raise EncodeError(
            f"{what} failed (ffmpeg exit code {proc.returncode})", stderr_tail=_tail(proc.stderr, redact)
        )


def _disk_full(stderr: bytes | str | None) -> bool:
    if not stderr:
        return False
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    return any(marker in text.lower() for marker in _DISK_FULL_MARKERS)


def _temp_path(dst: Path) -> Path:
    return dst.with_name(f".{dst.name}.{uuid.uuid4().hex[:12]}.part")


def _tail(stderr: bytes | str | None, redact: tuple[Path, ...] = ()) -> str:
    """Last lines of ffmpeg's error output, with server paths reduced to file names."""
    if not stderr:
        return ""
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    for p in redact:
        text = text.replace(str(p), p.name)
    return text.strip()[-STDERR_TAIL_CHARS:]
