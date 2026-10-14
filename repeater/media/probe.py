"""ffprobe wrapper: the technical facts the importer, encoder and engine need about a file.

Inputs are untrusted (uploads, downloads), so every ffmpeg/ffprobe call made by the media
modules restricts the input to local files (`-protocol_whitelist file`) and only container
formats from ALLOWED_CONTAINERS are accepted. This keeps playlist-style inputs (HLS, concat)
from making ffmpeg read other files or URLs.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from repeater.config import get_settings

log = logging.getLogger("repeater.media")

PROBE_TIMEOUT_S = 60
# Input options shared by every ffmpeg/ffprobe invocation on untrusted files.
SAFE_INPUT_ARGS: tuple[str, ...] = ("-protocol_whitelist", "file")
# ffprobe `format_name` entries (comma-separated in its output) that we accept as videos.
ALLOWED_CONTAINERS = frozenset(
    {"mov", "mp4", "m4a", "3gp", "3g2", "mj2", "matroska", "webm", "avi", "mpegts", "flv", "mpeg", "asf"}
)
NOT_A_VIDEO = "not a video: the file could not be read as a video (unsupported format or damaged file)"


class MediaToolError(RuntimeError):
    """ffmpeg/ffprobe is missing, timed out or failed for reasons unrelated to the input."""


# Colour metadata names (ffprobe / libavutil spelling) that are passed on to ffmpeg filters. Values
# read from an untrusted file are only ever used when they are in these sets.
KNOWN_TRANSFERS = frozenset(
    {
        "bt709",
        "smpte170m",
        "smpte240m",
        "bt470m",
        "bt470bg",
        "linear",
        "iec61966-2-1",
        "bt2020-10",
        "bt2020-12",
        "smpte2084",
        "arib-std-b67",
    }
)
KNOWN_PRIMARIES = frozenset(
    {"bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "bt2020", "smpte431", "smpte432"}
)
KNOWN_MATRICES = frozenset({"bt709", "fcc", "bt470bg", "smpte170m", "smpte240m", "bt2020nc", "bt2020c"})
PQ = "smpte2084"
HLG = "arib-std-b67"


@dataclass(frozen=True)
class ProbeResult:
    duration_s: float
    width: int  # display width: coded width times the pixel aspect ratio, then `rotation` applied
    height: int  # display height
    fps: float
    video_codec: str
    audio_codec: str | None
    has_audio: bool
    size_bytes: int
    rotation: int = 0  # clockwise degrees applied at display time: 0, 90, 180 or 270
    format_name: str = ""
    pix_fmt: str = ""
    bit_depth: int = 8
    color_space: str = ""  # matrix, e.g. bt709 / bt2020nc ("" when untagged or unknown)
    color_transfer: str = ""  # e.g. bt709 / smpte2084 (PQ) / arib-std-b67 (HLG)
    color_primaries: str = ""
    color_range: str = ""  # tv | pc | ""
    peak_nits: float | None = None  # HDR10 MaxCLL or mastering peak, when the stream carries it

    @property
    def is_vertical(self) -> bool:
        return self.height > self.width

    @property
    def is_landscape(self) -> bool:
        return self.width > self.height

    @property
    def is_hdr(self) -> bool:
        """PQ (HDR10, Dolby Vision 8.1) or HLG (iPhone / Android HDR video)."""
        return self.color_transfer in (PQ, HLG)

    @property
    def is_wide_gamut_sdr(self) -> bool:
        """BT.2020 / DCI-P3 tagged material that is not HDR (needs a gamut conversion only)."""
        if self.is_hdr:
            return False
        return (
            self.color_primaries in ("bt2020", "smpte431", "smpte432")
            or self.color_space in ("bt2020nc", "bt2020c")
            or self.color_transfer in ("bt2020-10", "bt2020-12")
        )


def probe(path: Path | str) -> ProbeResult:
    """Inspect `path` with ffprobe. Raises ValueError("not a video ...") for files that are not
    readable videos, FileNotFoundError if the file is missing and MediaToolError if ffprobe
    itself cannot run."""
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"no such file: {p.name}")
    data = _run_ffprobe(p)
    return parse_probe_json(data, size_fallback=p.stat().st_size)


def _run_ffprobe(p: Path) -> dict[str, Any]:
    cmd = [
        get_settings().ffprobe_bin,
        "-v",
        "error",
        *SAFE_INPUT_ARGS,
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(p),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT_S, check=False)
    except FileNotFoundError as exc:
        raise MediaToolError(f"ffprobe not found ({get_settings().ffprobe_bin}); install ffmpeg") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaToolError(f"ffprobe timed out after {PROBE_TIMEOUT_S} s") from exc
    if proc.returncode != 0:
        log.info("ffprobe rejected %s: %s", p.name, _tail(proc.stderr))
        raise ValueError(NOT_A_VIDEO)
    try:
        data = json.loads(proc.stdout or b"{}")
    except json.JSONDecodeError as exc:
        raise ValueError(NOT_A_VIDEO) from exc
    return data if isinstance(data, dict) else {}


def parse_probe_json(data: dict[str, Any], *, size_fallback: int = 0) -> ProbeResult:
    """Build a ProbeResult from `ffprobe -show_format -show_streams` JSON."""
    fmt = data.get("format") or {}
    streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
    format_name = str(fmt.get("format_name") or "")
    if not ALLOWED_CONTAINERS.intersection(format_name.split(",")):
        raise ValueError(f"not a video: unsupported file format '{format_name or 'unknown'}'")
    video = _video_stream(streams)
    if video is None:
        raise ValueError("not a video: the file has no video track")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = _positive_float(fmt.get("duration")) or _positive_float(video.get("duration"))
    if not duration:
        raise ValueError("not a video: the file has no playable duration")
    coded_w, coded_h = _int(video.get("width")), _int(video.get("height"))
    if coded_w <= 0 or coded_h <= 0:
        raise ValueError("not a video: the video track has no picture size")
    rotation = _rotation(video)
    # Anamorphic material (camcorders, HDV): the picture is shown `sar` times wider than coded.
    coded_w = max(1, round(coded_w * _sample_aspect_ratio(video)))
    width, height = (coded_h, coded_w) if rotation in (90, 270) else (coded_w, coded_h)
    return ProbeResult(
        duration_s=duration,
        width=width,
        height=height,
        fps=_fps(video),
        video_codec=str(video.get("codec_name") or "unknown"),
        audio_codec=str(audio.get("codec_name") or "unknown") if audio else None,
        has_audio=audio is not None,
        size_bytes=_int(fmt.get("size")) or size_fallback,
        rotation=rotation,
        format_name=format_name,
        pix_fmt=str(video.get("pix_fmt") or ""),
        bit_depth=_bit_depth(video),
        color_space=_known(video.get("color_space"), KNOWN_MATRICES),
        color_transfer=_known(video.get("color_transfer"), KNOWN_TRANSFERS),
        color_primaries=_known(video.get("color_primaries"), KNOWN_PRIMARIES),
        color_range=_known(video.get("color_range"), frozenset({"tv", "pc"})),
        peak_nits=_peak_nits(video),
    )


def _video_stream(streams: list[dict[str, Any]]) -> dict[str, Any] | None:
    for s in streams:
        if s.get("codec_type") != "video":
            continue
        if (s.get("disposition") or {}).get("attached_pic"):
            continue  # cover art, not the picture track
        return s
    return None


def _known(value: Any, allowed: frozenset[str]) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else ""


def _sample_aspect_ratio(stream: dict[str, Any]) -> float:
    """Pixel aspect ratio, 1.0 when absent, unknown ("0:1") or implausible."""
    raw = str(stream.get("sample_aspect_ratio") or "")
    num, _, den = raw.partition(":")
    try:
        value = int(num) / int(den)
    except (ValueError, ZeroDivisionError):
        return 1.0
    return value if 0.25 <= value <= 4.0 else 1.0


def _bit_depth(stream: dict[str, Any]) -> int:
    depth = _int(stream.get("bits_per_raw_sample"))
    if depth <= 0:
        m = re.search(r"p(\d{2})(?:le|be)?$", str(stream.get("pix_fmt") or ""))
        depth = int(m.group(1)) if m else 8
    return depth


def _peak_nits(stream: dict[str, Any]) -> float | None:
    """HDR10 peak luminance: MaxCLL if present, else the mastering display's maximum."""
    max_cll = mastering = None
    for side in stream.get("side_data_list") or []:
        if not isinstance(side, dict):
            continue
        kind = str(side.get("side_data_type") or "").lower()
        if "content light level" in kind:
            max_cll = _positive_float(side.get("max_content"))
        elif "mastering display" in kind:
            mastering = _fraction_value(side.get("max_luminance"))
    return max_cll or mastering


def _fraction_value(value: Any) -> float | None:
    if isinstance(value, str) and "/" in value:
        return _positive_float(_fraction(value))
    return _positive_float(value)


def _rotation(stream: dict[str, Any]) -> int:
    """Clockwise display rotation. The display matrix reports counter-clockwise degrees
    (phones usually write -90); the legacy `rotate` tag is clockwise."""
    for side in stream.get("side_data_list") or []:
        if isinstance(side, dict) and "rotation" in side:
            try:
                return _normalize_degrees(-float(side["rotation"]))
            except (TypeError, ValueError):
                break
    tag = (stream.get("tags") or {}).get("rotate")
    if tag is not None:
        try:
            return _normalize_degrees(float(tag))
        except (TypeError, ValueError):
            return 0
    return 0


def _normalize_degrees(deg: float) -> int:
    return (int(round(deg / 90.0)) * 90) % 360


def _fps(stream: dict[str, Any]) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = _fraction(stream.get(key))
        if value > 0:
            return round(value, 3)
    return 0.0


def _fraction(value: Any) -> float:
    if not value or not isinstance(value, str):
        return 0.0
    try:
        num, _, den = value.partition("/")
        if den and float(den) == 0:
            return 0.0
        return float(Fraction(int(num), int(den or 1)))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _positive_float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _tail(stderr: bytes | str | None, limit: int = 800) -> str:
    if not stderr:
        return ""
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    return text.strip()[-limit:]
