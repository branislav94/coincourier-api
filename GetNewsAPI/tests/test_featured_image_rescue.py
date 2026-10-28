"""Offline tests of stock rescue through the publication boundary."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from image_search import emergency, selection
from image_search.downloader import ImageDownloadError
from image_search.models import DownloadedImage, ImageCandidate
from image_search.provider import ImageSearchProvider, ProviderUnavailable
from publishing import PublicationArticle, PublicationContext, PublicationImage
from publishing.wordpress import media, publisher


ARTICLE = {
    "id": 101, "title": "Bitcoin market liquidity rises", "seo_focus": "Bitcoin market liquidity",
    "full_text": "Markets liquidity rises.", "category": "Markets", "hashtags": "bitcoin,markets",
    "seo_meta": "Bitcoin market liquidity improves.", "news_url": "https://news.example.org/bitcoin",
}


def stock(provider: str, asset_id: str, score: float, *, variant: str = "") -> ImageCandidate:
    if provider == "pexels":
        source = f"https://www.pexels.com/photo/bitcoin-{asset_id}/"
        url = f"https://images.pexels.com/photos/{asset_id}/bitcoin{variant}.jpg"
        license_name, license_url, threshold = "pexels-license", "https://www.pexels.com/license/", 0.72
    else:
        source = f"https://pixabay.com/photos/bitcoin-{asset_id}/"
        url = f"https://cdn.pixabay.com/photo/2026/01/01/00/00/bitcoin-{asset_id}{variant}.jpg"
        license_name, license_url, threshold = "pixabay-content-license", "https://pixabay.com/service/license-summary/", 0.78
    return ImageCandidate(
        provider=provider, asset_id=asset_id, canonical_source=f"{provider}:{asset_id}",
        image_url=url, source_page_url=source, creator_name="Fixture Creator",
        license_name=license_name, license_url=license_url,
        attribution_text=f"Photo by Fixture Creator. Source: {source}. License: {license_url}",
        width=1600, height=900, mime_type="image/jpeg", provider_threshold=threshold,
        metadata={"fixture_score": score, "metadata_text": "bitcoin market liquidity", "attribution_complete": True},
    )


class FixtureProvider(ImageSearchProvider):
    def __init__(self, name: str, items: list[ImageCandidate] | None = None, *, failure: bool = False) -> None:
        self.provider_name, self.enabled, self.candidate_threshold = name, True, 0.0
        self.items, self.failure, self.calls = items or [], failure, []

    def search(self, query: str) -> list[ImageCandidate]:
        self.calls.append(query)
        if self.failure:
            raise ProviderUnavailable("openverse", "edge_access_denied", status_code=403,
                                      content_type="text/html", server_cloudflare=True, cf_ray_present=True)
        if len(self.calls) > 1:
            return []
        for item in self.items:
            item.query = query
        return self.items

    def normalize_candidate(self, raw: dict, query: str, provider_rank: int) -> ImageCandidate | None:
        raise NotImplementedError


class FixtureDownloader:
    def __init__(self, failures: dict[tuple[str, str], ImageDownloadError] | None = None) -> None:
        self.failures, self.calls = failures or {}, []

    def download(self, candidate: ImageCandidate) -> DownloadedImage:
        transport = candidate.provider, candidate.usable_url
        self.calls.append(transport)
        if transport in self.failures:
            raise self.failures[transport]
        content = candidate.asset_key.encode()
        digest = hashlib.sha256(content).hexdigest()
        image = DownloadedImage(content, "image/jpeg", 1600, 900, digest, digest[:16])
        candidate.content_sha256, candidate.perceptual_hash = image.content_sha256, image.perceptual_hash
        return image


class FeaturedStockRescueTests(unittest.TestCase):
    @contextmanager
    def route(self, providers, *, downloader=None, local_usage=None, metadata_outcomes=None, engine="v2"):
        loader = downloader or FixtureDownloader()
        results = []

        def score(candidate, _article):
            candidate.final_score = candidate.metadata["fixture_score"]
            return candidate

        def strict(article, **kwargs):
            result = selection.search_images(article, providers=providers, downloader=loader,
                                             local_usage=local_usage or [], recent_wp_usage=[], **kwargs)
            results.append(result)
            return result

        with ExitStack() as stack:
            for name, value in {
                "IMAGE_SEARCH_ENGINE": engine, "IMAGE_SOURCE_MODE": "hybrid", "IMAGE_SOURCE_PRIORITY": "stock_first",
                "USE_SOURCE_IMAGES": False, "PRIMARY_IMAGE_PROVIDER": "grok", "IMAGE_FALLBACK_PROVIDER": "openai",
                "OPENAI_IMAGE_FALLBACK": True, "IMAGE_GENERATION_ONLY_AFTER_SEARCH_EXHAUSTED": True,
                "IMAGE_GENERATION_ON_PROVIDER_ERROR": False, "IMAGE_EMERGENCY_STOCK_RESCUE_ENABLED": True,
                "IMAGE_EMERGENCY_STOCK_RESCUE_LIMIT": 10,
            }.items():
                stack.enter_context(patch.object(publisher, name, value))
            stack.enter_context(patch.object(selection, "score_candidate", side_effect=score))
            stack.enter_context(patch.object(publisher, "search_images", side_effect=strict))
            cover = stack.enter_context(patch.object(publisher, "_cover_bytes_to_1536x1024", return_value=(b"hero", {"width": 1536, "height": 1024})))
            upload = stack.enter_context(patch.object(publisher, "upload_media", side_effect=iter(range(701, 751))))
            metadata = stack.enter_context(patch.object(publisher, "set_media_details", return_value=True, side_effect=metadata_outcomes))
            generate = stack.enter_context(patch.object(publisher, "_generated_image_content", return_value=None))
            rescue = stack.enter_context(patch.object(publisher, "rescue_images", wraps=emergency.rescue_images))
            yield SimpleNamespace(loader=loader, results=results, cover=cover, upload=upload,
                                  metadata=metadata, generate=generate, rescue=rescue)

    def upload(self):
        return publisher.upload_image(None, ARTICLE["title"], ARTICLE["hashtags"], article=ARTICLE)

    def test_exact_dev_failures_rescue_safe_pexels_and_publish_featured_media(self) -> None:
        low, best = stock("pexels", "101", 0.60), stock("pexels", "102", 0.71)
        pixabay = [stock("pixabay", "201", 0.90), stock("pixabay", "202", 0.91)]
        providers = [FixtureProvider("pexels", [low, best]), FixtureProvider("pixabay", pixabay),
                     FixtureProvider("openverse", failure=True), FixtureProvider("wikimedia")]
        loader = FixtureDownloader({(item.provider, item.usable_url): ImageDownloadError("http_429", provider_unavailable=True) for item in pixabay})
        with self.route(providers, downloader=loader) as route, self.assertLogs(level="INFO") as logs:
            media_id, metadata = self.upload()
        self.assertEqual(media_id, 701)
        self.assertEqual(metadata["asset_key"], best.asset_key)
        self.assertTrue(metadata["emergency_stock_rescue"])
        self.assertFalse(metadata["strict_stock_selected"])
        self.assertEqual(metadata["emergency_original_rejection_reason"], "score_below_threshold")
        self.assertFalse(route.results[0].generation_allowed)
        self.assertFalse(route.results[0].all_available_providers_exhausted)
        self.assertIn("openverse", route.results[0].provider_failures)
        self.assertIn("pixabay", route.results[0].provider_failures)
        self.assertEqual(route.loader.calls[-1], ("pexels", best.usable_url))
        for item in pixabay:
            self.assertEqual(route.loader.calls.count((item.provider, item.usable_url)), 1)
        self.assertEqual(metadata["emergency_download_attempts"], 1)
        route.generate.assert_not_called()
        self.assertIn("reason=emergency_stock_selected", " ".join(logs.output))

        adapter = publisher.WordPressPublisher()
        post_response = Mock(status_code=201)
        post_response.json.return_value = {"id": 801, "link": "https://news.example.org/published"}
        with (
            patch.object(adapter, "_validate_yoast"), patch.object(adapter, "reconcile", return_value=None),
            patch.object(adapter, "_write_yoast"), patch.object(publisher, "ensure_category", return_value=1),
            patch.object(publisher, "ensure_term", return_value=2), patch.object(publisher.session, "post", return_value=post_response) as post,
            patch.object(publisher, "record_image_usage") as usage, patch("builtins.print"),
        ):
            result = adapter.publish(PublicationArticle.from_mapping(ARTICLE), PublicationImage(media_id, metadata),
                                     PublicationContext(datetime(2026, 10, 28, tzinfo=timezone.utc), creation_is_fresh=True))
        self.assertTrue(result.success)
        self.assertEqual(post.call_args.kwargs["json"]["featured_media"], 701)
        usage.assert_called_once_with(metadata, 801, ARTICLE["title"])
        self.assertEqual(usage.call_args.args[0]["license_name"], "pexels-license")

    def test_strict_success_does_not_attempt_rescue_or_upload_twice(self) -> None:
        item = stock("pexels", "111", 0.90)
        with self.route([FixtureProvider("pexels", [item])]) as route:
            media_id, metadata = self.upload()
        self.assertEqual(media_id, 701)
        self.assertTrue(metadata["strict_stock_selected"])
        self.assertFalse(metadata["emergency_selected"])
        self.assertEqual(route.loader.calls, [(item.provider, item.usable_url)])
        route.upload.assert_called_once()
        route.metadata.assert_called_once()
        route.rescue.assert_not_called()
        route.generate.assert_not_called()

    def test_strict_attribution_failure_tries_next_strict_candidate(self) -> None:
        first, second = stock("pexels", "121", 0.95), stock("pexels", "122", 0.90)
        with self.route([FixtureProvider("pexels", [first, second])],
                        metadata_outcomes=[media.MediaMetadataError("metadata_update_failed"), True]) as route:
            media_id, metadata = self.upload()
        self.assertEqual(media_id, 702)
        self.assertEqual(metadata["asset_key"], second.asset_key)
        self.assertTrue(metadata["strict_stock_selected"])
        self.assertEqual(len(route.loader.calls), 2)
        self.assertEqual(route.metadata.call_count, 2)
        route.rescue.assert_not_called()
        route.generate.assert_not_called()

    def test_rescue_attribution_failure_tries_same_identity_alternate_transport(self) -> None:
        first, second = stock("pexels", "131", 0.71), stock("pexels", "131", 0.70, variant="-alternate")
        with self.route([FixtureProvider("pexels", [first, second])],
                        metadata_outcomes=[media.MediaMetadataError("metadata_update_failed"), True]) as route:
            media_id, metadata = self.upload()
        self.assertEqual(media_id, 702)
        self.assertEqual(metadata["image_url"], second.usable_url)
        self.assertTrue(metadata["emergency_stock_rescue"])
        self.assertEqual(route.loader.calls, [(first.provider, first.usable_url), (second.provider, second.usable_url)])
        self.assertEqual(metadata["emergency_download_attempts"], 2)
        self.assertEqual(route.metadata.call_count, 2)
        route.generate.assert_not_called()

    def test_fresh_below_threshold_precedes_recent_above_threshold_rescue(self) -> None:
        fresh, recent = stock("pexels", "141", 0.69), stock("pexels", "142", 0.98)
        with self.route([FixtureProvider("pexels", [recent, fresh])], local_usage=[{"asset_key": recent.asset_key}]) as route:
            media_id, metadata = self.upload()
        self.assertEqual(media_id, 701)
        self.assertEqual(metadata["asset_key"], fresh.asset_key)
        self.assertEqual(route.loader.calls, [(fresh.provider, fresh.usable_url)])
        self.assertEqual(metadata["emergency_original_rejection_reason"], "score_below_threshold")
        route.generate.assert_not_called()

    def test_hard_unsafe_low_score_candidates_never_download_or_upload(self) -> None:
        for kind in ("license", "private_url", "wrong_cdn", "mime", "dimensions", "orientation", "attribution"):
            with self.subTest(kind=kind):
                item = stock("pexels", "151", 0.60)
                if kind == "license":
                    item.license_name = "unknown"
                elif kind == "private_url":
                    item.download_url = "http://127.0.0.1/private.jpg"
                elif kind == "wrong_cdn":
                    item.download_url = "https://images.pexels.com.evil.example/private.jpg"
                elif kind == "mime":
                    item.mime_type = "image/svg+xml"
                elif kind == "dimensions":
                    item.width = 1199
                elif kind == "orientation":
                    item.height = 1700
                else:
                    item.attribution_text = "x" * 17000
                with self.route([FixtureProvider("pexels", [item]), FixtureProvider("openverse", failure=True)]) as route:
                    media_id, metadata = self.upload()
                self.assertIsNone(media_id)
                self.assertEqual(route.loader.calls, [])
                route.upload.assert_not_called()
                self.assertFalse(metadata["emergency_selected"])

    def test_v1_rollback_stays_on_existing_image_route(self) -> None:
        selected = (b"v1", "pexels.jpg", {"provider": "pexels", "credit_text": "Complete existing Pexels credit"})
        with self.route([], engine="v1") as route, patch.object(publisher, "_stock_image_content_v1", return_value=selected) as v1:
            media_id, metadata = self.upload()
        self.assertEqual(media_id, 701)
        self.assertEqual(metadata["provider"], "pexels")
        v1.assert_called_once()
        route.rescue.assert_not_called()
        route.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
