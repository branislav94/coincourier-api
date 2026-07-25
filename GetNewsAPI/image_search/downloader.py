from __future__ import annotations

import hashlib
import io
import time
from typing import Any

import requests
from PIL import Image, UnidentifiedImageError

from config import (
    IMAGE_MIN_HEIGHT,
    IMAGE_MIN_WIDTH,
    IMAGE_PROVIDER_MAX_RETRIES,
    IMAGE_PROVIDER_TIMEOUT_SECONDS,
)

from .models import DownloadedImage, ImageCandidate


RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
SUPPORTED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class ImageDownloadError(RuntimeError):
    def __init__(self, reason: str, *, provider_unavailable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.provider_unavailable = provider_unavailable


def _difference_hash(image: Image.Image) -> str:
    resized = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(resized.tobytes())
    bits = 0
    for row in range(8):
        for column in range(8):
            bits = (bits << 1) | int(
                pixels[row * 9 + column] > pixels[row * 9 + column + 1]
            )
    return f"{bits:016x}"


def perceptual_hash_distance(left: str, right: str) -> int | None:
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except (TypeError, ValueError):
        return None


def inspect_image_bytes(
    content: bytes,
    *,
    min_width: int = IMAGE_MIN_WIDTH,
    min_height: int = IMAGE_MIN_HEIGHT,
) -> DownloadedImage:
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ImageDownloadError("invalid_size")
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            mime_type = SUPPORTED_FORMATS.get(str(image.format or "").upper())
            if not mime_type:
                raise ImageDownloadError("unsupported_format")
            width, height = image.size
            if width < min_width or height < min_height:
                raise ImageDownloadError("dimensions")
            if width < height:
                raise ImageDownloadError("orientation")
            perceptual_hash = _difference_hash(image)
    except ImageDownloadError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageDownloadError("invalid_image") from exc
    return DownloadedImage(
        content=content,
        mime_type=mime_type,
        width=width,
        height=height,
        content_sha256=hashlib.sha256(content).hexdigest(),
        perceptual_hash=perceptual_hash,
    )


class ImageDownloader:
    def __init__(
        self,
        *,
        session: Any | None = None,
        timeout: int = IMAGE_PROVIDER_TIMEOUT_SECONDS,
        max_retries: int = IMAGE_PROVIDER_MAX_RETRIES,
        sleeper=time.sleep,
        min_width: int = IMAGE_MIN_WIDTH,
        min_height: int = IMAGE_MIN_HEIGHT,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.sleeper = sleeper
        self.min_width = min_width
        self.min_height = min_height

    def download(self, candidate: ImageCandidate) -> DownloadedImage:
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(
                    candidate.usable_url,
                    timeout=self.timeout,
                    headers={"User-Agent": "CryptoCourierImageSelector/2.0"},
                )
                status = int(response.status_code)
                if 200 <= status < 300:
                    downloaded = inspect_image_bytes(
                        response.content,
                        min_width=self.min_width,
                        min_height=self.min_height,
                    )
                    candidate.content_sha256 = downloaded.content_sha256
                    candidate.perceptual_hash = downloaded.perceptual_hash
                    candidate.mime_type = downloaded.mime_type
                    return downloaded
                if status not in RETRYABLE_STATUSES:
                    # Candidate image 401/403 is treated as access/hotlink rejection;
                    # another result from the same provider may still be usable.
                    raise ImageDownloadError(f"http_{status}")
                if attempt >= self.max_retries:
                    raise ImageDownloadError(
                        f"http_{status}",
                        provider_unavailable=True,
                    )
            except ImageDownloadError:
                raise
            except requests.Timeout:
                if attempt >= self.max_retries:
                    raise ImageDownloadError(
                        "timeout",
                        provider_unavailable=True,
                    ) from None
            except requests.ConnectionError:
                if attempt >= self.max_retries:
                    raise ImageDownloadError(
                        "connection_failure",
                        provider_unavailable=True,
                    ) from None
            except Exception:
                raise ImageDownloadError("download_error") from None
            self.sleeper(min(2.0, 0.5 * (2**attempt)))
        raise ImageDownloadError("download_error")
