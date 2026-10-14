"""The single platform-wide encoding profile (PLAN.md section 5.4) and thumbnails.

One MP4 per video is accepted by TikTok, YouTube Shorts, Instagram Reels and Facebook Reels:
1080x1920 (scale to fit + pad), 30 fps, yuv420p, H.264 High@4.0 with a closed 2-second GOP,
AAC-LC 192 kb/s 48 kHz stereo, moov atom first (+faststart).
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

from repeater.config import get_settings
from repeater.media.probe import SAFE_INPUT_ARGS, MediaToolError, ProbeResult, probe

TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
TARGET_FPS = 30
VIDEO_FILTER = (
    "scale=1080:1920:force_original_aspect_ratio=decrease,"
    "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p"
)
VIDEO_CODEC_ARGS: tuple[str, ...] = (
    "-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
    "-preset", "slow", "-crf", "20", "-maxrate", "12M", "-bufsize", "24M",
    "-g", "60", "-keyint_min", "60", "-sc_threshold", "0", "-bf", "2",
)  # fmt: skip
AUDIO_CODEC_ARGS: tuple[str, ...] = ("-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2")
# Some platforms reject files without an audio track, so silent stereo is added when missing.
SILENT_AUDIO_SOURCE = "anullsrc=channel_layout=stereo:sample_rate=48000"

THUMB_WIDTH = 540
THUMB_HEIGHT = 960
THUMB_FILTER = (
    f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=decrease,"
    f"pad={THUMB_WIDTH}:{THUMB_HEIGHT}:(ow-iw)/2:(oh-ih)/2"
)
STDERR_TAIL_CHARS = 1500


class EncodeError(MediaToolError):
    """ffmpeg exited with an error. `stderr_tail` holds the end of its error output."""

    def __init__(self, message: str, *, stderr_tail: str = "") -> None:
        super().__init__(f"{message}: {stderr_tail}" if stderr_tail else message)
        self.stderr_tail = stderr_tail


class EncodeTimeout(EncodeError):
    """ffmpeg did not finish within the allowed time (it was killed)."""


def build_encode_command(
    src: Path, out: Path, *, has_audio: bool, ffmpeg_bin: str | None = None
) -> list[str]:
    """The exact ffmpeg argv for the platform profile; `out` is written as MP4 whatever its name."""
    cmd = [ffmpeg_bin or get_settings().ffmpeg_bin, "-hide_banner", "-nostdin", "-v", "error", "-y"]
    cmd += [*SAFE_INPUT_ARGS, "-i", str(src)]
    if has_audio:
        maps = ["-map", "0:V:0", "-map", "0:a:0"]
    else:
        cmd += ["-f", "lavfi", "-i", SILENT_AUDIO_SOURCE]
        maps = ["-map", "0:V:0", "-map", "1:a:0", "-shortest"]
    cmd += maps
    cmd += ["-vf", VIDEO_FILTER, *VIDEO_CODEC_ARGS, *AUDIO_CODEC_ARGS]
    cmd += ["-movflags", "+faststart", "-f", "mp4", str(out)]
    return cmd


def encode_for_platforms(
    src: Path | str, dst: Path | str, *, timeout_s: float = 900, source_probe: ProbeResult | None = None
) -> ProbeResult:
    """Encode `src` into `dst` with the platform profile and return the probe of the result.
    The output appears atomically (temp file + rename). Raises EncodeTimeout or EncodeError."""
    src_p, dst_p = Path(src).resolve(), Path(dst)
    info = source_probe or probe(src_p)
    dst_p.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_path(dst_p)
    try:
        _run_ffmpeg(
            build_encode_command(src_p, tmp, has_audio=info.has_audio),
            timeout_s=timeout_s,
            what="encoding",
            redact=(src_p, tmp),
        )
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise EncodeError("encoding produced no output")
        os.replace(tmp, dst_p)
    finally:
        tmp.unlink(missing_ok=True)
    return probe(dst_p)


def make_thumbnail(src: Path | str, dst: Path | str, at_s: float, *, timeout_s: float = 120) -> Path:
    """Write a 540x960 JPEG of the frame at `at_s` seconds (first frame if `at_s` is past the end)."""
    src_p, dst_p = Path(src).resolve(), Path(dst)
    dst_p.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_path(dst_p)
    try:
        for position in (max(0.0, float(at_s)), 0.0):
            _run_ffmpeg(
                _thumbnail_command(src_p, tmp, position),
                timeout_s=timeout_s,
                what="thumbnail",
                redact=(src_p, tmp),
            )
            if tmp.is_file() and tmp.stat().st_size > 0:
                os.replace(tmp, dst_p)
                return dst_p
        raise EncodeError("thumbnail: no frame could be extracted")
    finally:
        tmp.unlink(missing_ok=True)


def _thumbnail_command(src: Path, out: Path, at_s: float) -> list[str]:
    return [
        get_settings().ffmpeg_bin, "-hide_banner", "-nostdin", "-v", "error", "-y",
        "-ss", f"{at_s:.3f}", *SAFE_INPUT_ARGS, "-i", str(src),
        "-frames:v", "1", "-vf", THUMB_FILTER, "-q:v", "3",
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
        raise EncodeError(
            f"{what} failed (ffmpeg exit code {proc.returncode})", stderr_tail=_tail(proc.stderr, redact)
        )


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
