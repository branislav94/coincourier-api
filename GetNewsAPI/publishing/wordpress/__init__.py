"""WordPress publishing adapter."""

from .publisher import WordPressPublisher, publish_news_to_wp

__all__ = ["WordPressPublisher", "publish_news_to_wp"]
