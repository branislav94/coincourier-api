"""Prepare the featured-image crop shared by the publisher and Vision probe."""
from __future__ import annotations

import io
import logging
from typing import Any

from PIL import Image


# Keep the existing crop diagnostics under the publisher's logger name.
logger = logging.getLogger("publishing.wordpress.publisher")


def cover_bytes_to_1536x1024(
    raw_bytes: bytes,
    *,
    min_width: int = 640,
    min_height: int = 360,
) -> tuple[bytes, dict[str, Any]] | None:
    """Convert downloaded source or stock bytes to a 1536x1024 JPEG without distortion."""
    try:
        img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        original_width, original_height = img.size
        if original_width < min_width or original_height < min_height:
            logger.info(
                "[IMG] rejected tiny image dims=%sx%s min=%sx%s",
                original_width,
                original_height,
                min_width,
                min_height,
            )
            return None

        ratio = original_width / max(original_height, 1)
        if ratio < 1.0:
            logger.info("[IMG] rejected portrait image dims=%sx%s", original_width, original_height)
            return None

        target_width, target_height = 1536, 1024
        scale = max(target_width / original_width, target_height / original_height)
        resized_width = max(target_width, int(round(original_width * scale)))
        resized_height = max(target_height, int(round(original_height * scale)))
        img = img.resize((resized_width, resized_height), Image.LANCZOS)

        left = (resized_width - target_width) // 2
        top = (resized_height - target_height) // 2
        img = img.crop((left, top, left + target_width, top + target_height))

        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue(), {
            "width": original_width,
            "height": original_height,
            "output_width": target_width,
            "output_height": target_height,
        }
    except Exception:
        logger.exception("[IMG] could not convert downloaded image to hero JPEG")
        return None
