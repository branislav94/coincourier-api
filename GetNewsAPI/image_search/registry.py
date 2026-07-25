from __future__ import annotations

import logging

from config import IMAGE_SEARCH_PROVIDERS

from .cache import CachedHttpClient
from .provider import ImageSearchProvider
from .providers import OpenverseProvider, PexelsProvider, PixabayProvider


logger = logging.getLogger(__name__)


def build_provider_registry(
    provider_names: tuple[str, ...] = IMAGE_SEARCH_PROVIDERS,
    *,
    client: CachedHttpClient | None = None,
) -> list[ImageSearchProvider]:
    factories = {
        "pexels": lambda: PexelsProvider(client=client or CachedHttpClient()),
        "pixabay": lambda: PixabayProvider(client=client or CachedHttpClient()),
        "openverse": lambda: OpenverseProvider(client=client or CachedHttpClient()),
    }
    providers: list[ImageSearchProvider] = []
    seen: set[str] = set()
    for raw_name in provider_names:
        name = raw_name.strip().lower()
        if name in seen:
            continue
        seen.add(name)
        factory = factories.get(name)
        if not factory:
            logger.warning("[IMG-V2] unknown image search provider ignored provider=%s", name)
            continue
        provider = factory()
        if provider.enabled:
            providers.append(provider)
        else:
            logger.info("[IMG-V2] image search provider disabled provider=%s", name)
    return providers
