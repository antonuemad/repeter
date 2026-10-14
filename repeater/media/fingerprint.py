"""Content fingerprints for duplicate detection (PLAN.md sections 3.6 and 5.6).

Exact copies are caught by SHA-256. Re-encoded copies of the same video (for example the same
clip imported from TikTok and from Instagram, or Repeater's own letterboxed re-post) are caught
by two perceptual fingerprints that must both agree:

* the video: a perceptual hash (pHash) of PHASH_FRAMES frames spread evenly over the duration,
  taken from the display-normalised picture (pixel aspect ratio applied, fitted into a 9:16
  canvas with black bars, HDR converted to SDR), so 16:9, 4:5 and 1:1 originals hash like their
  1080x1920 letterboxed copies; plus a duration tolerance;
* the audio: a Chromaprint fingerprint of the first AUDIO_WINDOW_S seconds, compared by bit error
  rate. A different voice-over or soundtrack on the same b-roll is a different video. When either
  side has no usable audio (no track, silent, too short, no Chromaprint in ffmpeg) the audio is
  not held against the match.

Both are stored in Video.phash as one text value: space-separated hex hashes, then optionally
"a:<base64url of the raw Chromaprint items>" (see pack() and unpack()).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image

from repeater.config import get_settings
from repeater.media.filters import picture_filter
from repeater.media.probe import SAFE_INPUT_ARGS, MediaToolError, ProbeResult

log = logging.getLogger("repeater.media")

# Number of frames sampled per video.
PHASH_FRAMES = 8
# Two videos are the same only if their durations differ by at most this many seconds...
MAX_DURATION_DIFF_S = 1.0
# ...and at least this many of the PHASH_FRAMES frame pairs are perceptually equal...
MIN_MATCHING_FRAMES = 6
# ...where "equal" means a Hamming distance of at most this many bits (of 64) between pHashes.
# Measured on re-encodes (CRF 20 encode, then 720p CRF 28 copy) of 16:9, 4:5, 1:1 and 9:16
# originals: 0-10 bits. Unrelated footage is 20-40 bits apart.
MAX_FRAME_DISTANCE = 12
# ...and, when both have audio, the audio fingerprints agree: bit error rate (0..1) at most this.
# The same audio through AAC 64-128 kb/s, another sample rate or another volume: 0.00-0.02.
# Different voice-over on the same music: 0.24; different speech: 0.36; different tones: 0.22.
MAX_AUDIO_BER = 0.15

# Frames are reduced to a small 9:16 grey canvas before hashing; pHash reduces to 32x32 anyway.
CANVAS_WIDTH = 144
CANVAS_HEIGHT = 256
_FRAME_TIMEOUT_S = 60
_CHUNK = 1024 * 1024

AUDIO_PREFIX = "a:"
AUDIO_WINDOW_S = 120
AUDIO_SAMPLE_RATE = 11025
AUDIO_TIMEOUT_S = 120
# Chromaprint emits about 4 items per second; shorter clips than this cannot be compared.
AUDIO_MIN_ITEMS = 12
# Compare at offsets of up to +-8 items (about 2 s) to absorb trimmed starts and encoder delay.
AUDIO_MAX_SHIFT = 8
# Below this peak level (dBFS) the track is treated as silence (Repeater adds silent audio to
# videos without sound, and platforms mute copyrighted music).
SILENCE_DB = -60.0
_MAX_VOLUME_RE = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?|-inf) dB")
_chromaprint_warned = False


@dataclass(frozen=True)
class Fingerprint:
    frames: list[str]  # hex pHash per decodable sample frame
    audio: str | None  # "a:..." token, None when audio cannot be compared
    tail_missing: int = 0  # undecodable samples at the end (a truncated file ends early)


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def sample_times(duration_s: float, n: int = PHASH_FRAMES) -> list[float]:
    """Centres of n equal slices of the video, so no sample sits on the very first or last frame."""
    if duration_s <= 0 or n <= 0:
        return []
    return [duration_s * (i + 0.5) / n for i in range(n)]


def fingerprint_video(path: Path | str, info: ProbeResult) -> Fingerprint:
    """Frame hashes and audio fingerprint of a video file whose probe is `info`."""
    frames: list[str] = []
    tail_missing = 0
    for t in sample_times(info.duration_s):
        frame = _canvas_frame(Path(path).resolve(), t, info)
        if frame is None:
            tail_missing += 1
            continue
        tail_missing = 0
        frames.append(str(imagehash.phash(frame)))
    audio = audio_fingerprint(path, info) if info.has_audio else None
    return Fingerprint(frames=frames, audio=audio, tail_missing=tail_missing)


def phash_frames(
    path: Path | str, duration_s: float, n: int = PHASH_FRAMES, *, info: ProbeResult | None = None
) -> list[str]:
    """Hex pHash strings of n evenly spaced frames. Frames that cannot be decoded are skipped,
    so the list can be shorter than n; it is empty only if no frame could be read. `info` (the
    file's probe) enables the HDR conversion; without it the picture is taken as SDR."""
    src = Path(path).resolve()
    hashes: list[str] = []
    for t in sample_times(duration_s, n):
        frame = _canvas_frame(src, t, info)
        if frame is not None:
            hashes.append(str(imagehash.phash(frame)))
    return hashes


def pack(frames: Sequence[str], audio: str | None = None) -> str | None:
    """Value for Video.phash (None when there is nothing to store)."""
    parts = [*frames, *([audio] if audio else [])]
    return " ".join(parts) or None


def unpack(text: str | None) -> tuple[list[str], str | None]:
    """Inverse of pack(): (frame hashes, audio token or None)."""
    frames: list[str] = []
    audio: str | None = None
    for token in (text or "").split():
        if token.startswith(AUDIO_PREFIX):
            audio = token
        else:
            frames.append(token)
    return frames, audio


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def is_duplicate(
    a_hashes: Sequence[str],
    a_duration: float | None,
    b_hashes: Sequence[str],
    b_duration: float | None,
    a_audio: str | None = None,
    b_audio: str | None = None,
) -> bool:
    """True when both videos have (almost) the same duration, enough matching frames and, if both
    have audio, matching audio. With fewer than PHASH_FRAMES pairs the required matches scale
    down proportionally."""
    if a_duration is None or b_duration is None:
        return False
    if abs(a_duration - b_duration) > MAX_DURATION_DIFF_S:
        return False
    pairs = list(zip(a_hashes, b_hashes, strict=False))
    if not pairs:
        return False
    required = -(-MIN_MATCHING_FRAMES * len(pairs) // PHASH_FRAMES)  # ceil
    matches = 0
    for x, y in pairs:
        try:
            if hamming(x, y) <= MAX_FRAME_DISTANCE:
                matches += 1
        except ValueError:
            continue  # malformed stored hash: count as a mismatch
    if matches < required:
        return False
    return audio_match(a_audio, b_audio) is not False


def audio_match(a: str | None, b: str | None) -> bool | None:
    """True/False when both audio fingerprints can be compared, None when they cannot."""
    items_a, items_b = _decode_audio(a), _decode_audio(b)
    if items_a is None or items_b is None:
        return None
    best = audio_distance(items_a, items_b)
    if best is None:
        return None
    return best <= MAX_AUDIO_BER


def audio_distance(a: np.ndarray, b: np.ndarray) -> float | None:
    """Lowest bit error rate between two Chromaprint item arrays over small offsets, None when
    they overlap by fewer than AUDIO_MIN_ITEMS items at every offset."""
    best: float | None = None
    for shift in range(-AUDIO_MAX_SHIFT, AUDIO_MAX_SHIFT + 1):
        x = a[shift:] if shift > 0 else a
        y = b[-shift:] if shift < 0 else b
        n = min(len(x), len(y))
        if n < AUDIO_MIN_ITEMS:
            continue
        rate = _bit_errors(x[:n], y[:n]) / (32.0 * n)
        best = rate if best is None else min(best, rate)
    return best


def audio_fingerprint(path: Path | str, info: ProbeResult | None = None) -> str | None:
    """Chromaprint of the first AUDIO_WINDOW_S seconds as an "a:..." token, or None when the file
    has no audible audio (no track, silence, under ~3 s) or ffmpeg cannot fingerprint it."""
    global _chromaprint_warned
    if info is not None and not info.has_audio:
        return None
    src = Path(path).resolve()
    cmd = [
        get_settings().ffmpeg_bin, "-hide_banner", "-nostdin", "-v", "info", "-t", str(AUDIO_WINDOW_S),
        *SAFE_INPUT_ARGS, "-i", str(src),
        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(AUDIO_SAMPLE_RATE),
        "-f", "chromaprint", "-fp_format", "raw", "pipe:1",
        "-map", "0:a:0", "-vn", "-af", "volumedetect", "-f", "null", "-",
    ]  # fmt: skip
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=AUDIO_TIMEOUT_S, check=False
        )
    except FileNotFoundError as exc:
        raise MediaToolError(f"ffmpeg not found ({cmd[0]}); install ffmpeg") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaToolError(f"audio fingerprinting timed out after {AUDIO_TIMEOUT_S} s") from exc
    stderr = proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0 or not proc.stdout:
        if "output format 'chromaprint'" in stderr.lower() and not _chromaprint_warned:
            _chromaprint_warned = True
            log.warning("this ffmpeg has no chromaprint muxer: audio is not compared for duplicates")
        return None
    if _is_silent(stderr):
        return None
    items = np.frombuffer(proc.stdout[: len(proc.stdout) // 4 * 4], dtype="<u4")
    if len(items) < AUDIO_MIN_ITEMS:
        return None
    return AUDIO_PREFIX + base64.urlsafe_b64encode(items.tobytes()).rstrip(b"=").decode("ascii")


def _is_silent(stderr: str) -> bool:
    m = _MAX_VOLUME_RE.search(stderr)
    if m is None:
        return False
    return m.group(1) == "-inf" or float(m.group(1)) <= SILENCE_DB


def _decode_audio(token: str | None) -> np.ndarray | None:
    if not token or not token.startswith(AUDIO_PREFIX):
        return None
    data = token[len(AUDIO_PREFIX) :]
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(raw) < 4 * AUDIO_MIN_ITEMS or len(raw) % 4:
        return None
    return np.frombuffer(raw, dtype="<u4")


def _bit_errors(a: np.ndarray, b: np.ndarray) -> int:
    return int(np.unpackbits((a ^ b).view(np.uint8)).sum())


def _canvas_frame(src: Path, at_s: float, info: ProbeResult | None) -> Image.Image | None:
    """The frame at `at_s`, display-normalised into a CANVAS_WIDTH x CANVAS_HEIGHT grey image."""
    vf = f"{picture_filter(info, CANVAS_WIDTH, CANVAS_HEIGHT)},format=gray"
    cmd = [
        get_settings().ffmpeg_bin, "-hide_banner", "-nostdin", "-v", "error",
        "-ss", f"{at_s:.3f}", *SAFE_INPUT_ARGS, "-i", str(src),
        "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-",
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_FRAME_TIMEOUT_S, check=False)
    except FileNotFoundError as exc:
        raise MediaToolError(f"ffmpeg not found ({cmd[0]}); install ffmpeg") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaToolError(f"frame extraction timed out after {_FRAME_TIMEOUT_S} s") from exc
    expected = CANVAS_WIDTH * CANVAS_HEIGHT
    if proc.returncode != 0 or len(proc.stdout) < expected:
        return None
    return Image.frombytes("L", (CANVAS_WIDTH, CANVAS_HEIGHT), proc.stdout[:expected])
