"""Decode bounded raster inputs and write inert, metadata-free JPEG derivatives."""

from __future__ import annotations

import hashlib
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from repeater.media import storage
from repeater.messages import UserError
from repeater.models import Video, VideoStatus

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})


def decode(path: Path) -> Image.Image:
    try:
        if not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
            raise ValueError("size")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("format")
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("pixels")
                image.load()
                oriented = ImageOps.exif_transpose(image).convert("RGBA")
                clean = Image.new("RGB", oriented.size, "white")
                clean.paste(oriented, mask=oriented.getchannel("A"))
                return clean
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise UserError(
            "media.invalid_image", "Use a static JPEG, PNG or WebP up to 20 MB and 24 megapixels."
        ) from None


def prepare(item: Video, original: Path, image: Image.Image | None = None) -> None:
    image = image or decode(original)
    # No cropping: a composition that fails the target ratio stays excluded with an explanation.
    image.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
    folder = storage.ensure_video_dir(item.workspace_id, item.id)
    output = folder / "image.jpg"
    image.save(output, "JPEG", quality=90)
    item.width, item.height = image.size
    item.sha256 = hashlib.sha256(image.tobytes() + str(image.size).encode()).hexdigest()
    item.encoded_path = storage.rel_path(output)
    item.encoded_size_bytes = output.stat().st_size
    image.thumbnail((640, 640), Image.Resampling.LANCZOS)
    thumb = folder / "thumb.jpg"
    image.save(thumb, "JPEG", quality=85)
    item.thumbnail_path = storage.rel_path(thumb)
    item.size_bytes = original.stat().st_size
    item.status = VideoStatus.READY
    item.error = item.error_info = None
