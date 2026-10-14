"""ffmpeg filter graphs shared by the platform encode and by the fingerprinting.

Both turn whatever a creator uploaded into the same *display-normalised* picture: pixel aspect
ratio applied (anamorphic camcorder footage), fitted into a canvas with black bars, and converted
to SDR BT.709 when the source is HDR or wide gamut. The encode makes a 1080x1920 canvas of it; the
fingerprint makes a small 9:16 canvas, so a letterboxed re-post of a video hashes like the video.

HDR to SDR (PQ / HDR10 and HLG, for example iPhone "HDR Video" and Android HDR10 clips):
    1. to linear light with 203 cd/m2 = 1.0 (BT.2408 reference white; HLG 75% and PQ 58% are 203),
    2. BT.2020 to BT.709 primaries,
    3. a soft highlight roll-off (mobius, linear up to 80% of SDR white) for the part above white,
    4. BT.709 transfer, matrix and limited range.
Everything at or below reference white keeps its brightness, so colours are neither washed out nor
dark. Needs ffmpeg built with libzimg (zscale) and the tonemap filter: Debian/Ubuntu ffmpeg 6.1 and
7.1 have both (see hdr_tools_available()).
"""

from __future__ import annotations

import subprocess
from functools import lru_cache

from repeater.config import get_settings
from repeater.media.probe import HLG, MediaToolError, ProbeResult

REFERENCE_WHITE_NITS = 203.0
HLG_PEAK_NITS = 1000.0
DEFAULT_PQ_PEAK_NITS = 1000.0
MAX_PQ_PEAK_NITS = 10000.0
TONEMAP_KNEE = 0.8
# Apply pixel aspect ratio, reducing both dimensions when display width exceeds the cap.
# Use the rounded output width for height too, so rounding cannot stretch the picture.
SAR_SCALE = "scale=w=max(2\\,trunc(min(iw*sar\\,4096)/2)*2):h=max(2\\,trunc(ih*ow/(iw*sar)/2)*2),setsar=1"
_FILTER_PROBE_TIMEOUT_S = 20


class HdrToolsMissing(MediaToolError):
    """The server's ffmpeg cannot convert HDR or wide-gamut video (no zscale/tonemap filter)."""


@lru_cache(maxsize=4)
def _filter_names(ffmpeg_bin: str) -> frozenset[str]:
    try:
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-filters"],
            capture_output=True,
            timeout=_FILTER_PROBE_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return frozenset()
    names: set[str] = set()
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 3 and len(parts[0]) == 3:  # " TSC zscale  V->V  description"
            names.add(parts[1])
    return frozenset(names)


def hdr_tools_available(ffmpeg_bin: str | None = None) -> bool:
    """True when ffmpeg has the zscale and tonemap filters. Checked once per binary."""
    names = _filter_names(ffmpeg_bin or get_settings().ffmpeg_bin)
    return {"zscale", "tonemap"} <= names


def colour_filter(info: ProbeResult | None) -> str:
    """Filters that convert `info`'s picture to 8-bit SDR BT.709 limited range ("" if it already
    is one). Raises HdrToolsMissing when ffmpeg cannot do the conversion."""
    if info is None:
        return ""
    if _is_bt601_sdr(info):
        # SD Y'CbCr uses the BT.601 matrix: tagging those pixels BT.709 changes their colours.
        # swscale performs the matrix/range conversion without requiring the HDR tools.
        return (
            f"scale=in_color_matrix={info.color_space}:out_color_matrix=bt709:"
            f"in_range={info.color_range or 'tv'}:out_range=tv:flags=accurate_rnd+full_chroma_int,"
            "setparams=colorspace=bt709:range=tv"
        )
    if not (info.is_hdr or info.is_wide_gamut_sdr):
        return ""
    if not hdr_tools_available():
        raise HdrToolsMissing(
            "this server's ffmpeg has no zscale/tonemap filter, so HDR and wide-gamut video cannot be "
            "converted to SDR (install an ffmpeg built with libzimg)"
        )
    src = (
        f"tin={_transfer(info)}:pin={info.color_primaries or 'bt2020'}:"
        f"min={info.color_space or 'bt2020nc'}:rin={info.color_range or 'tv'}"
    )
    sdr = "t=bt709:m=bt709:r=tv:dither=ordered"
    if not info.is_hdr:
        return f"zscale={src}:p=bt709:{sdr},format=yuv420p"
    peak = HLG_PEAK_NITS if info.color_transfer == HLG else _pq_peak_nits(info)
    return (
        f"zscale={src}:t=linear:npl={REFERENCE_WHITE_NITS:g},format=gbrpf32le,zscale=p=bt709,"
        f"tonemap=tonemap=mobius:param={TONEMAP_KNEE}:peak={peak / REFERENCE_WHITE_NITS:.2f},"
        f"zscale={sdr},format=yuv420p"
    )


def _is_bt601_sdr(info: ProbeResult | None) -> bool:
    return (
        info is not None
        and not (info.is_hdr or info.is_wide_gamut_sdr)
        and info.color_space in ("smpte170m", "bt470bg")
    )


def _transfer(info: ProbeResult) -> str:
    if info.color_transfer:
        return info.color_transfer
    return "bt2020-10" if info.color_primaries == "bt2020" else "bt709"


def _pq_peak_nits(info: ProbeResult) -> float:
    peak = info.peak_nits or DEFAULT_PQ_PEAK_NITS
    return min(max(peak, REFERENCE_WHITE_NITS), MAX_PQ_PEAK_NITS)


def picture_filter(info: ProbeResult | None, width: int, height: int) -> str:
    """Decoded frame -> `width` x `height` SDR canvas with black bars (no fps / pixel format).
    Rotation needs no filter: ffmpeg applies the display matrix when it decodes."""
    parts = [
        SAR_SCALE,
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
    ]
    colour = colour_filter(info)
    if colour:
        if _is_bt601_sdr(info):
            # swscale may normalise full range while resizing. Convert first, while the probed
            # range still describes the pixels, otherwise the later conversion expands twice.
            parts.insert(0, colour)
        else:
            parts.append(colour)
    parts.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")
    parts.append("setsar=1")
    return ",".join(parts)
