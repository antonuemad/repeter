"""Content fingerprints for duplicate detection (PLAN.md sections 3.6 and 5.6).

Exact copies are caught by SHA-256. Re-encoded copies of the same video (for example the same
clip imported from TikTok and from Instagram) are caught by a perceptual hash (pHash) of
PHASH_FRAMES frames spread evenly over the duration, combined with a duration tolerance.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

import imagehash
from PIL import Image

from repeater.config import get_settings
from repeater.media.probe import SAFE_INPUT_ARGS, MediaToolError

# Number of frames sampled per video.
PHASH_FRAMES = 8
# Two videos are the same only if their durations differ by at most this many seconds...
MAX_DURATION_DIFF_S = 1.0
# ...and at least this many of the PHASH_FRAMES frame pairs are perceptually equal...
MIN_MATCHING_FRAMES = 6
# ...where "equal" means a Hamming distance of at most this many bits (of 64) between pHashes.
MAX_FRAME_DISTANCE = 10

# Frames are scaled to a small grey square before hashing; pHash reduces to 32x32 anyway.
_FRAME_SIZE = 128
_FRAME_TIMEOUT_S = 60
_CHUNK = 1024 * 1024


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


def phash_frames(path: Path | str, duration_s: float, n: int = PHASH_FRAMES) -> list[str]:
    """Hex pHash strings of n evenly spaced frames. Frames that cannot be decoded are skipped,
    so the list can be shorter than n; it is empty only if no frame could be read."""
    src = Path(path).resolve()
    hashes: list[str] = []
    for t in sample_times(duration_s, n):
        frame = _grey_frame(src, t)
        if frame is not None:
            hashes.append(str(imagehash.phash(frame)))
    return hashes


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def is_duplicate(
    a_hashes: Sequence[str],
    a_duration: float | None,
    b_hashes: Sequence[str],
    b_duration: float | None,
) -> bool:
    """True when both videos have (almost) the same duration and enough matching frames.
    With fewer than PHASH_FRAMES pairs the required matches scale down proportionally."""
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
    return matches >= required


def _grey_frame(src: Path, at_s: float) -> Image.Image | None:
    cmd = [
        get_settings().ffmpeg_bin, "-hide_banner", "-nostdin", "-v", "error",
        "-ss", f"{at_s:.3f}", *SAFE_INPUT_ARGS, "-i", str(src),
        "-frames:v", "1", "-vf", f"scale={_FRAME_SIZE}:{_FRAME_SIZE},format=gray",
        "-f", "rawvideo", "-",
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_FRAME_TIMEOUT_S, check=False)
    except FileNotFoundError as exc:
        raise MediaToolError(f"ffmpeg not found ({cmd[0]}); install ffmpeg") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaToolError(f"frame extraction timed out after {_FRAME_TIMEOUT_S} s") from exc
    expected = _FRAME_SIZE * _FRAME_SIZE
    if proc.returncode != 0 or len(proc.stdout) < expected:
        return None
    return Image.frombytes("L", (_FRAME_SIZE, _FRAME_SIZE), proc.stdout[:expected])
