"""Compatibility alias for the extracted WordPress publisher implementation."""

from __future__ import annotations

import sys

from publishing.wordpress import publisher as _publisher


if __name__ == "__main__":
    _publisher.publish_news_to_wp()
else:
    # Preserve legacy helper and patch surfaces while keeping one implementation.
    sys.modules[__name__] = _publisher
