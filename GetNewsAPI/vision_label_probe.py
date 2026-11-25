"""Evaluate a local image with the rescue crop and Vision policy."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from PIL import Image

from config import (
    GOOGLE_CLOUD_VISION_API_KEY,
    IMAGE_MIN_HEIGHT,
    IMAGE_MIN_WIDTH,
    STOCK_IMAGE_VISION_MODE,
    STOCK_IMAGE_VISION_MONTHLY_UNIT_LIMIT,
    WRITABLE_STATE_DIR,
)
from image_search.downloader import ImageDownloadError, MAX_IMAGE_BYTES, inspect_image_bytes
from image_search.preparation import cover_bytes_to_1536x1024
from image_search.vision import VisionCheck, make_preview
from image_search.vision_policy import build_vision_conflict_rule, classify_vision_topic


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # An unknown argument could contain an accidentally pasted API key.
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid probe arguments. Use --help and supply credentials through runtime configuration.\n")


def main(argv: list[str] | None = None) -> int:
    parser = _SafeArgumentParser(description="Check a local photo with the CoinCourier Vision rescue policy.")
    parser.add_argument("--image", type=Path, required=True, help="Local JPEG, PNG or WebP file.")
    parser.add_argument("--title", required=True, help="Article headline for local topic selection.")
    parser.add_argument("--lead", default="", help="Optional article lead for local topic selection.")
    parser.add_argument("--live", action="store_true", help="Allow a submission when Vision mode is enabled.")
    args = parser.parse_args(argv)
    if not args.title.strip():
        parser.error("headline is required")

    topic = classify_vision_topic({"title": args.title, "lead": args.lead})
    mode = STOCK_IMAGE_VISION_MODE
    if mode not in {"off", "shadow", "enforce"}:
        mode = "invalid"
    record = {
        "mode": mode,
        "topic": topic,
        "decision": "skipped",
        "status": "skipped",
        "units_reserved": 0,
        "labels": [],
    }

    if args.live and mode not in {"shadow", "enforce"}:
        reason = "mode_off" if mode == "off" else "invalid_mode"
        print(json.dumps({**record, "reason": reason}, sort_keys=True))
        return 2

    try:
        with args.image.open("rb") as input_file:
            raw_bytes = input_file.read(MAX_IMAGE_BYTES + 1)
        inspect_image_bytes(raw_bytes, min_width=IMAGE_MIN_WIDTH, min_height=IMAGE_MIN_HEIGHT)
    except ImageDownloadError as exc:
        print(json.dumps({**record, "reason": exc.reason}, sort_keys=True))
        return 2
    except (OSError, ValueError, Image.DecompressionBombError):
        print(json.dumps({**record, "reason": "invalid_local_image"}, sort_keys=True))
        return 2

    prepared = cover_bytes_to_1536x1024(raw_bytes, min_width=IMAGE_MIN_WIDTH, min_height=IMAGE_MIN_HEIGHT)
    if prepared is None:
        print(json.dumps({**record, "reason": "crop_failed"}, sort_keys=True))
        return 2

    crop, crop_info = prepared
    record.update(crop_width=crop_info["output_width"], crop_height=crop_info["output_height"])
    if not args.live:
        preview = make_preview(crop)
        if preview is None:
            print(json.dumps({**record, "reason": "preview_failed"}, sort_keys=True))
            return 2
        print(json.dumps({**record, "reason": "offline", "preview_bytes": len(preview)}, sort_keys=True))
        return 0

    if topic not in {"hardware_wallet", "financial_record"}:
        print(json.dumps({**record, "reason": topic or "unrelated_topic"}, sort_keys=True))
        return 2

    verdict = VisionCheck(
        mode=mode,
        api_key=GOOGLE_CLOUD_VISION_API_KEY,
        monthly_limit=STOCK_IMAGE_VISION_MONTHLY_UNIT_LIMIT,
        state_dir=WRITABLE_STATE_DIR,
        conflict_rule=build_vision_conflict_rule(topic),
    ).check(crop)
    record.update(
        decision=verdict.decision,
        reason=verdict.reason,
        status=verdict.status,
        units_reserved=verdict.units_reserved,
        labels=verdict.labels,
        latency_ms=verdict.latency_ms,
        checked_at_utc=verdict.checked_at_utc,
    )
    print(json.dumps(record, sort_keys=True))
    return 0 if verdict.labels else 1


if __name__ == "__main__":
    raise SystemExit(main())
