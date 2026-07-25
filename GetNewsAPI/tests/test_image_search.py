from __future__ import annotations

import hashlib
import io
import json
import logging
import sys
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PIL import Image

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import publish_to_wp
import stock_images
from image_search.cache import CachedHttpClient
from image_search.downloader import ImageDownloader, inspect_image_bytes, perceptual_hash_distance
from image_search.license_policy import ImageLicensePolicy
from image_search.models import DownloadedImage, ImageCandidate
from image_search.provider import ImageSearchProvider, ProviderUnavailable
from image_search.providers.openverse import OpenverseProvider
from image_search.providers.pexels import PexelsProvider
from image_search.providers.pixabay import PixabayProvider
from image_search.queries import build_image_queries
from image_search.reuse import (
    candidate_recent_local_usage,
    candidate_recent_wp_usage,
    downloaded_recent_local_usage,
    load_image_usage,
    prune_image_usage,
    record_image_usage,
)
from image_search.scoring import rank_candidates, score_candidate
from image_search.selection import search_images


ARTICLE = {
    "seo_focus": "Bitcoin ETF liquidity",
    "title": "Bitcoin ETF trading volume rises",
    "hashtags": "bitcoin, markets",
    "category": "Markets",
    "tickers": "BTC",
}


def candidate(
    provider: str = "openverse",
    asset_id: str = "asset-1",
    *,
    threshold: float = 0.70,
    canonical_source: str | None = None,
    image_url: str | None = None,
) -> ImageCandidate:
    source_page = f"https://source.example/{provider}/{asset_id}"
    license_url = "https://creativecommons.org/licenses/by/4.0/"
    return ImageCandidate(
        provider=provider,
        asset_id=asset_id,
        image_url=image_url or f"https://images.example/{provider}/{asset_id}.jpg",
        source_page_url=source_page,
        canonical_source=canonical_source or f"source:{provider}:{asset_id}",
        creator_name="Creator",
        creator_url="https://source.example/creator",
        license_name="cc-by",
        license_version="4.0",
        license_url=license_url,
        attribution_text=(
            f"Bitcoin image by Creator. Source: {source_page}. "
            f"License: CC BY 4.0 ({license_url})"
        ),
        width=1600,
        height=900,
        query="bitcoin etf liquidity",
        provider_threshold=threshold,
        metadata={
            "metadata_text": "bitcoin etf liquidity cryptocurrency blockchain trading markets",
            "views": 50000,
            "downloads": 10000,
            "likes": 1000,
            "attribution_complete": True,
            "attribution_authoritative": False,
        },
    )


def downloaded(seed: bytes = b"image", perceptual_hash: str | None = None) -> DownloadedImage:
    sha256 = hashlib.sha256(seed).hexdigest()
    return DownloadedImage(
        content=seed,
        mime_type="image/jpeg",
        width=1600,
        height=900,
        content_sha256=sha256,
        perceptual_hash=perceptual_hash or sha256[:16],
    )


def encoded_image_bytes(size: tuple[int, int] = (1600, 900)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color=(40, 100, 180)).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


class StaticProvider(ImageSearchProvider):
    def __init__(self, name: str, candidates: list[ImageCandidate], *, fail: bool = False) -> None:
        self.provider_name = name
        self.enabled = True
        self.candidate_threshold = 0.0
        self._candidates = candidates
        self._fail = fail
        self._calls = 0

    def search(self, query: str) -> list[ImageCandidate]:
        self._calls += 1
        if self._fail:
            raise ProviderUnavailable(self.provider_name, "timeout", retry_exhausted=True)
        if self._calls > 1:
            return []
        for item in self._candidates:
            item.query = query
        return self._candidates

    def normalize_candidate(self, raw: dict, query: str, provider_rank: int) -> ImageCandidate | None:
        raise NotImplementedError


class DelayedStaticProvider(StaticProvider):
    def __init__(self, name: str, candidates: list[ImageCandidate], delay: float) -> None:
        super().__init__(name, candidates)
        self.delay = delay

    def search(self, query: str) -> list[ImageCandidate]:
        if self._calls == 0 and self.delay:
            time.sleep(self.delay)
        return super().search(query)


class StaticDownloader:
    def __init__(self, images: dict[str, DownloadedImage] | None = None) -> None:
        self.images = images or {}
        self.calls: list[str] = []

    def download(self, item: ImageCandidate) -> DownloadedImage:
        self.calls.append(item.asset_key)
        result = self.images.get(item.asset_key) or downloaded(item.asset_key.encode("utf-8"))
        item.content_sha256 = result.content_sha256
        item.perceptual_hash = result.perceptual_hash
        return result


class FakeJsonResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return self._payload


class FakeJsonSession:
    def __init__(self, responses: list[FakeJsonResponse]) -> None:
        self.responses = responses
        self.calls = 0

    def request(self, _method: str, _url: str, **_kwargs) -> FakeJsonResponse:
        response = self.responses[self.calls]
        self.calls += 1
        return response


class FakeDownloadResponse:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content


class QueueDownloadSession:
    def __init__(self, responses: list[FakeDownloadResponse | Exception]) -> None:
        self.responses = responses
        self.calls = 0

    def get(self, _url: str, **_kwargs) -> FakeDownloadResponse:
        response = self.responses[self.calls]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response


class RaisingJsonSession:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def request(self, _method: str, _url: str, **_kwargs) -> FakeJsonResponse:
        raise self.error


class AdapterTests(unittest.TestCase):
    def test_pexels_adapter_normalization_and_stable_key(self) -> None:
        raw = {
            "id": 123,
            "width": 2400,
            "height": 1350,
            "url": "https://www.pexels.com/photo/123/",
            "photographer": "Ada",
            "photographer_url": "https://www.pexels.com/@ada",
            "alt": "Bitcoin market trading",
            "src": {"large2x": "https://images.pexels.com/123.jpg"},
            "liked": True,
        }
        item = PexelsProvider(enabled=False).normalize_candidate(raw, "bitcoin market", 0)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.asset_key, "pexels:123")
        self.assertEqual(item.creator_name, "Ada")
        self.assertEqual(item.license_name, "pexels-license")
        self.assertIn(raw["url"], item.attribution_text)

    def test_pixabay_adapter_normalization_and_stable_key(self) -> None:
        raw = {
            "id": 456,
            "imageWidth": 1920,
            "imageHeight": 1080,
            "pageURL": "https://pixabay.com/photos/456/",
            "largeImageURL": "https://cdn.pixabay.com/456.jpg",
            "user": "Grace",
            "user_id": 42,
            "tags": "bitcoin, market",
            "views": 20000,
            "downloads": 5000,
            "likes": 400,
        }
        item = PixabayProvider(enabled=False).normalize_candidate(raw, "bitcoin market", 0)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.asset_key, "pixabay:456")
        self.assertEqual(item.creator_name, "Grace")
        self.assertEqual(item.license_name, "pixabay-content-license")

    def test_openverse_adapter_normalization(self) -> None:
        raw = {
            "id": "ov-7",
            "foreign_identifier": "foreign-7",
            "source": "flickr",
            "provider": "flickr",
            "url": "https://live.staticflickr.com/7.jpg",
            "thumbnail": "https://api.openverse.org/thumb/7",
            "foreign_landing_url": "https://flickr.com/photos/7",
            "creator": "Lin",
            "creator_url": "https://flickr.com/people/lin",
            "license": "by",
            "license_version": "4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "attribution": "Work by Lin, CC BY 4.0",
            "width": 2048,
            "height": 1152,
            "title": "Bitcoin network",
            "tags": [{"name": "bitcoin"}],
        }
        item = OpenverseProvider(enabled=False).normalize_candidate(raw, "bitcoin", 2)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.asset_key, "openverse:ov-7")
        self.assertEqual(item.canonical_source, "flickr:foreign-7")
        self.assertEqual(item.license_name, "cc-by")
        self.assertEqual(item.provider_rank, 2)
        self.assertIn(raw["foreign_landing_url"], item.attribution_text)
        self.assertIn(raw["license_url"], item.attribution_text)

    def test_openverse_cc_by_unknown_creator_without_authoritative_attribution_is_rejected(self) -> None:
        raw = {
            "id": "ov-unknown",
            "foreign_identifier": "foreign-unknown",
            "source": "flickr",
            "url": "https://live.staticflickr.com/unknown.jpg",
            "foreign_landing_url": "https://flickr.com/photos/unknown",
            "license": "by",
            "license_version": "4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "width": 1600,
            "height": 900,
            "title": "Unknown work",
        }
        item = OpenverseProvider(enabled=False).normalize_candidate(raw, "bitcoin", 0)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.creator_name, "")
        self.assertEqual(item.attribution_text, "")
        self.assertFalse(ImageLicensePolicy(("cc-by",)).validate(item)[0])

    def test_openverse_complete_authoritative_attribution_can_supply_missing_creator(self) -> None:
        source_page = "https://flickr.com/photos/authoritative"
        license_url = "https://creativecommons.org/licenses/by/4.0/"
        attribution = (
            f'<a href="{source_page}">Work by Archive Contributor</a>. '
            f'Licensed <a href="{license_url}">CC BY 4.0</a>.'
        )
        raw = {
            "id": "ov-authoritative",
            "foreign_identifier": "foreign-authoritative",
            "source": "flickr",
            "url": "https://live.staticflickr.com/authoritative.jpg",
            "foreign_landing_url": source_page,
            "license": "by",
            "license_version": "4.0",
            "license_url": license_url,
            "attribution": attribution,
            "width": 1600,
            "height": 900,
            "title": "Authoritative work",
        }
        item = OpenverseProvider(enabled=False).normalize_candidate(raw, "bitcoin", 0)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.attribution_text, attribution)
        self.assertTrue(item.metadata["attribution_authoritative"])
        self.assertTrue(ImageLicensePolicy(("cc-by",)).validate(item)[0])


class ParityAndPolicyTests(unittest.TestCase):
    def test_v1_and_v2_queries_are_equivalent(self) -> None:
        self.assertEqual(build_image_queries(ARTICLE), stock_images.build_stock_queries(ARTICLE))

    def test_v1_and_v2_scoring_threshold_parity(self) -> None:
        item = candidate("pexels", "99", threshold=0.72)
        item.license_name = "pexels-license"
        item.license_url = "https://www.pexels.com/license/"
        score_candidate(item, ARTICLE)
        old_score, _parts = stock_images._score_candidate(
            "pexels",
            item.query,
            ARTICLE,
            item.metadata["metadata_text"],
            item.width,
            item.height,
            item.metadata,
        )
        self.assertEqual(item.final_score, old_score)
        self.assertEqual(item.final_score >= item.provider_threshold, old_score >= 0.72)

    def test_pixabay_scoring_threshold_parity(self) -> None:
        item = candidate("pixabay", "88", threshold=0.78)
        item.license_name = "pixabay-content-license"
        item.license_url = "https://pixabay.com/service/license-summary/"
        score_candidate(item, ARTICLE)
        old_score, _parts = stock_images._score_candidate(
            "pixabay",
            item.query,
            ARTICLE,
            item.metadata["metadata_text"],
            item.width,
            item.height,
            item.metadata,
        )
        self.assertEqual(item.final_score, old_score)
        self.assertEqual(item.final_score >= item.provider_threshold, old_score >= 0.78)

    def test_shared_http_client_uses_bounded_retry(self) -> None:
        session = FakeJsonSession([
            FakeJsonResponse(503, {}),
            FakeJsonResponse(200, {"results": []}),
        ])
        client = CachedHttpClient(
            session=session,
            cache_hours=0,
            max_retries=1,
            sleeper=lambda _seconds: None,
        )
        self.assertEqual(
            client.get_json("openverse", "bitcoin", "https://api.example/search"),
            {"results": []},
        )
        self.assertEqual(session.calls, 2)

    def test_pixabay_request_failure_never_exposes_api_key(self) -> None:
        secret = "pixabay-super-secret-key"
        unsafe_error = requests.ConnectionError(
            f"connection failed for https://pixabay.com/api/?key={secret}&q=bitcoin"
        )
        client = CachedHttpClient(
            session=RaisingJsonSession(unsafe_error),
            cache_hours=0,
            max_retries=0,
            sleeper=lambda _seconds: None,
        )
        with self.assertRaises(ProviderUnavailable) as raised:
            client.get_json(
                "pixabay",
                "bitcoin",
                "https://pixabay.com/api/",
                params={"key": secret, "q": "bitcoin"},
            )
        self.assertNotIn(secret, str(raised.exception))

        provider = PixabayProvider(client=client, api_key=secret, enabled=True)
        with self.assertLogs("image_search.selection", level=logging.WARNING) as captured:
            result = search_images(
                ARTICLE,
                providers=[provider],
                downloader=StaticDownloader(),
                local_usage=[],
                recent_wp_usage=[],
            )
        failure_text = json.dumps(result.provider_failures, sort_keys=True)
        log_text = "\n".join(captured.output)
        self.assertNotIn(secret, failure_text)
        self.assertNotIn(secret, log_text)
        self.assertIn("connection_failure", failure_text)

    def test_license_allowlist_accepts_only_audited_licenses(self) -> None:
        policy = ImageLicensePolicy(("cc0", "pdm", "cc-by"))
        for license_name in ("cc0", "pdm", "cc-by"):
            item = candidate()
            item.license_name = license_name
            self.assertTrue(policy.validate(item)[0], license_name)
        for license_name in ("cc-by-nc", "cc-by-nd", "cc-by-nc-sa", "cc-by-nc-nd", "unknown", ""):
            item = candidate()
            item.license_name = license_name
            self.assertFalse(policy.validate(item)[0], license_name)

    def test_provider_threshold_rejects_below_threshold_without_stopping_other_candidate(self) -> None:
        rejected = candidate("openverse", "high-threshold", threshold=1.01)
        accepted = candidate("openverse", "accepted", threshold=0.70)
        result = search_images(
            ARTICLE,
            providers=[StaticProvider("openverse", [rejected, accepted])],
            downloader=StaticDownloader(),
            local_usage=[],
            recent_wp_usage=[],
        )
        self.assertEqual(result.candidate.asset_id if result.candidate else None, "accepted")
        self.assertEqual(result.rejection_counts.get("threshold"), 1)

    def test_global_ranking_normalizes_against_provider_threshold(self) -> None:
        pexels = candidate("pexels", "p", threshold=0.72)
        pixabay = candidate("pixabay", "x", threshold=0.78)
        pexels.final_score = 0.86
        pixabay.final_score = 0.88
        ranked = rank_candidates([pixabay, pexels])
        self.assertEqual(ranked[0].asset_key, "pexels:p")


class SelectionAndReuseTests(unittest.TestCase):
    @staticmethod
    def _ranked_duplicate_pair() -> tuple[ImageCandidate, ImageCandidate]:
        lower = candidate(
            "pixabay",
            "lower",
            threshold=0.70,
            canonical_source="flickr:shared-ranked",
            image_url="https://lower.example/image.jpg",
        )
        lower.license_name = "pixabay-content-license"
        lower.license_url = "https://pixabay.com/service/license-summary/"
        lower.final_score = 0.82
        higher = candidate(
            "openverse",
            "higher",
            threshold=0.70,
            canonical_source="flickr:shared-ranked",
            image_url="https://higher.example/image.jpg",
        )
        higher.final_score = 0.96
        return lower, higher

    def test_ranked_identity_dedup_keeps_highest_when_lower_finishes_first(self) -> None:
        lower, higher = self._ranked_duplicate_pair()
        loader = StaticDownloader()
        with patch("image_search.selection.score_candidate", side_effect=lambda item, _article: item):
            result = search_images(
                ARTICLE,
                providers=[
                    DelayedStaticProvider("pixabay", [lower], 0.0),
                    DelayedStaticProvider("openverse", [higher], 0.03),
                ],
                downloader=loader,
                local_usage=[],
                recent_wp_usage=[],
            )
        self.assertEqual(result.candidate.asset_key if result.candidate else None, "openverse:higher")
        self.assertEqual(loader.calls, ["openverse:higher"])
        self.assertEqual(result.rejection_counts.get("duplicate"), 1)

    def test_ranked_identity_dedup_is_independent_of_provider_completion_order(self) -> None:
        winners: list[str | None] = []
        for low_delay, high_delay in ((0.0, 0.03), (0.03, 0.0)):
            lower, higher = self._ranked_duplicate_pair()
            with patch("image_search.selection.score_candidate", side_effect=lambda item, _article: item):
                result = search_images(
                    ARTICLE,
                    providers=[
                        DelayedStaticProvider("pixabay", [lower], low_delay),
                        DelayedStaticProvider("openverse", [higher], high_delay),
                    ],
                    downloader=StaticDownloader(),
                    local_usage=[],
                    recent_wp_usage=[],
                )
            winners.append(result.candidate.asset_key if result.candidate else None)
        self.assertEqual(winners, ["openverse:higher", "openverse:higher"])

    def test_transitive_identity_bridge_rejects_entire_duplicate_group(self) -> None:
        winners: list[str | None] = []
        for delays in ((0.03, 0.0, 0.01), (0.0, 0.02, 0.03)):
            first = candidate("openverse", "component-winner", canonical_source="shared-source")
            first.final_score = 0.96
            bridge = candidate("pixabay", "component-bridge", canonical_source="shared-source")
            bridge.license_name = "pixabay-content-license"
            bridge.license_url = "https://pixabay.com/service/license-summary/"
            bridge.url_hash = "shared-url"
            bridge.final_score = 0.90
            third = candidate("pexels", "component-third", canonical_source="third-source")
            third.license_name = "pexels-license"
            third.license_url = "https://www.pexels.com/license/"
            third.url_hash = "shared-url"
            third.final_score = 0.85
            loader = StaticDownloader()
            with patch("image_search.selection.score_candidate", side_effect=lambda item, _article: item):
                result = search_images(
                    ARTICLE,
                    providers=[
                        DelayedStaticProvider("openverse", [first], delays[0]),
                        DelayedStaticProvider("pixabay", [bridge], delays[1]),
                        DelayedStaticProvider("pexels", [third], delays[2]),
                    ],
                    downloader=loader,
                    local_usage=[],
                    recent_wp_usage=[],
                )
            winners.append(result.candidate.asset_key if result.candidate else None)
            self.assertEqual(loader.calls, ["openverse:component-winner"])
            self.assertEqual(result.rejection_counts.get("duplicate"), 2)
        self.assertEqual(
            winners,
            ["openverse:component-winner", "openverse:component-winner"],
        )

    def test_provider_failure_isolated_from_successful_provider(self) -> None:
        good = candidate("openverse", "good")
        result = search_images(
            ARTICLE,
            providers=[StaticProvider("pexels", [], fail=True), StaticProvider("openverse", [good])],
            downloader=StaticDownloader(),
            local_usage=[],
            recent_wp_usage=[],
        )
        self.assertEqual(result.candidate.asset_key if result.candidate else None, "openverse:good")
        self.assertIn("pexels", result.provider_failures)

    def test_http_404_rejects_candidate_and_tries_next_without_provider_failure(self) -> None:
        missing = candidate("openverse", "missing")
        missing.final_score = 0.96
        available = candidate("openverse", "available")
        available.final_score = 0.90
        session = QueueDownloadSession([
            FakeDownloadResponse(404),
            FakeDownloadResponse(200, encoded_image_bytes()),
        ])
        loader = ImageDownloader(session=session, max_retries=0, sleeper=lambda _seconds: None)
        with patch("image_search.selection.score_candidate", side_effect=lambda item, _article: item):
            result = search_images(
                ARTICLE,
                providers=[StaticProvider("openverse", [missing, available])],
                downloader=loader,
                local_usage=[],
                recent_wp_usage=[],
            )
        self.assertEqual(result.candidate.asset_key if result.candidate else None, "openverse:available")
        self.assertEqual(session.calls, 2)
        self.assertEqual(result.provider_failures, {})
        self.assertEqual(result.rejection_counts.get("http_404"), 1)

    def test_timeout_is_provider_unavailable_and_not_confirmed_exhaustion(self) -> None:
        item = candidate("openverse", "timeout")
        session = QueueDownloadSession([requests.Timeout("unsafe authenticated image URL")])
        loader = ImageDownloader(session=session, max_retries=0, sleeper=lambda _seconds: None)
        result = search_images(
            ARTICLE,
            providers=[StaticProvider("openverse", [item])],
            downloader=loader,
            local_usage=[],
            recent_wp_usage=[],
        )
        self.assertIsNone(result.candidate)
        self.assertIn("openverse", result.provider_failures)
        self.assertIn("timeout", result.provider_failures["openverse"])
        self.assertNotIn("authenticated image URL", result.provider_failures["openverse"])
        self.assertFalse(result.all_available_providers_exhausted)

    def test_openverse_unknown_dimensions_are_validated_after_download(self) -> None:
        raw = {
            "id": "ov-no-dimensions",
            "foreign_identifier": "foreign-no-dimensions",
            "source": "flickr",
            "url": "https://live.staticflickr.com/no-dimensions.jpg",
            "foreign_landing_url": "https://flickr.com/photos/no-dimensions",
            "creator": "Known Creator",
            "license": "by",
            "license_version": "4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "title": "Bitcoin market",
            "tags": [{"name": "bitcoin"}, {"name": "market"}],
        }
        item = OpenverseProvider(enabled=False, threshold=0.30).normalize_candidate(raw, "bitcoin", 0)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertIsNone(item.width)
        self.assertIsNone(item.height)
        loader = ImageDownloader(
            session=QueueDownloadSession([FakeDownloadResponse(200, encoded_image_bytes())]),
            max_retries=0,
            sleeper=lambda _seconds: None,
        )
        result = search_images(
            ARTICLE,
            providers=[StaticProvider("openverse", [item])],
            downloader=loader,
            local_usage=[],
            recent_wp_usage=[],
        )
        self.assertEqual(result.candidate.asset_key if result.candidate else None, "openverse:ov-no-dimensions")
        self.assertEqual(result.downloaded.width if result.downloaded else None, 1600)
        self.assertEqual(result.downloaded.height if result.downloaded else None, 900)

    def test_same_canonical_image_under_different_urls_is_deduplicated(self) -> None:
        first = candidate("pexels", "101", canonical_source="flickr:shared", image_url="https://one/image.jpg")
        first.license_name = "pexels-license"
        first.license_url = "https://www.pexels.com/license/"
        second = candidate("openverse", "202", canonical_source="flickr:shared", image_url="https://two/image.jpg")
        loader = StaticDownloader()
        result = search_images(
            ARTICLE,
            providers=[StaticProvider("pexels", [first]), StaticProvider("openverse", [second])],
            downloader=loader,
            local_usage=[],
            recent_wp_usage=[],
        )
        self.assertIsNotNone(result.candidate)
        self.assertEqual(len(loader.calls), 1)
        self.assertEqual(result.rejection_counts.get("duplicate"), 1)

    def test_sha256_duplicate_detection(self) -> None:
        image = downloaded(b"same bytes")
        entry = {"content_sha256": image.content_sha256}
        self.assertIs(downloaded_recent_local_usage(image, [entry]), entry)

    def test_perceptual_hash_duplicate_detection_across_encodings(self) -> None:
        base = Image.new("RGB", (32, 18), color=(40, 100, 180))
        png_buffer = io.BytesIO()
        jpeg_buffer = io.BytesIO()
        base.save(png_buffer, format="PNG")
        base.save(jpeg_buffer, format="JPEG", quality=90)
        png = inspect_image_bytes(png_buffer.getvalue(), min_width=1, min_height=1)
        jpeg = inspect_image_bytes(jpeg_buffer.getvalue(), min_width=1, min_height=1)
        self.assertNotEqual(png.content_sha256, jpeg.content_sha256)
        self.assertLessEqual(perceptual_hash_distance(png.perceptual_hash, jpeg.perceptual_hash), 5)
        entry = {"perceptual_hash": png.perceptual_hash}
        self.assertIs(downloaded_recent_local_usage(jpeg, [entry]), entry)

    def test_twenty_day_window_and_old_usage_fields_remain_compatible(self) -> None:
        now = datetime.now(timezone.utc)
        fresh = {"asset_key": "pexels:123", "used_at": (now - timedelta(days=19)).isoformat()}
        expired = {"asset_key": "pexels:456", "used_at": (now - timedelta(days=21)).isoformat()}
        self.assertEqual(prune_image_usage([fresh, expired]), [fresh])
        item = candidate("pexels", "123")
        self.assertIs(candidate_recent_local_usage(item, [fresh]), fresh)

    def test_old_json_and_wordpress_history_are_read(self) -> None:
        used_at = datetime.now(timezone.utc).isoformat()
        old_entry = {
            "provider": "pexels",
            "asset_key": "pexels:321",
            "provider_asset_id": "321",
            "credit_url": "https://www.pexels.com/photo/321/",
            "image_url_hash": "old-hash",
            "used_at": used_at,
        }
        path = Mock(spec=Path)
        path.exists.return_value = True
        path.read_text.return_value = json.dumps([old_entry])
        with patch("image_search.reuse._usage_path", return_value=path):
            self.assertEqual(load_image_usage(), [old_entry])
        item = candidate("pexels", "321")
        item.source_page_url = old_entry["credit_url"]
        self.assertIs(candidate_recent_local_usage(item, [old_entry]), old_entry)
        row = {"post_excerpt": f"Photo source {item.source_page_url}", "post_content": "", "guid": ""}
        self.assertIs(candidate_recent_wp_usage(item, [row]), row)

    def test_wordpress_history_ignores_unrelated_numeric_asset_id(self) -> None:
        item = candidate("pexels", "321")
        row = {
            "post_excerpt": "Camera resolution 321 pixels from an unrelated upload",
            "post_content": "Article number 321",
            "guid": "https://uploads.example/unrelated.jpg",
        }
        self.assertIsNone(candidate_recent_wp_usage(item, [row]))

    def test_usage_record_is_additive_and_v1_compatible(self) -> None:
        item = candidate("openverse", "recorded")
        image = downloaded(b"recorded")
        meta = {
            "provider": item.provider,
            "asset_id": item.asset_id,
            "asset_key": item.asset_key,
            "image_url": item.usable_url,
            "canonical_source": item.canonical_source,
            "source_page_url": item.source_page_url,
            "creator_name": item.creator_name,
            "creator_url": item.creator_url,
            "license_name": item.license_name,
            "license_version": "4.0",
            "license_url": item.license_url,
            "attribution_text": item.attribution_text,
            "content_sha256": image.content_sha256,
            "perceptual_hash": image.perceptual_hash,
            "query": item.query,
            "score": 0.9,
        }
        with (
            patch("image_search.reuse.load_image_usage", return_value=[]),
            patch("image_search.reuse._write_image_usage") as write_usage,
        ):
            record_image_usage(meta, 44, "Article")
        entry = write_usage.call_args.args[0][0]
        for field in (
            "provider", "asset_key", "asset_id", "canonical_source", "source_page_url",
            "creator_name", "creator_url", "license_name", "license_version", "license_url",
            "attribution_text", "content_sha256", "perceptual_hash", "query", "score",
            "post_id", "title", "used_at", "provider_asset_id", "credit_url", "credit_name",
        ):
            self.assertIn(field, entry)

    def test_v2_usage_record_round_trips_through_v1_reuse_check(self) -> None:
        image_url = "https://images.pexels.com/photos/987/hero.jpg"
        source_page = "https://www.pexels.com/photo/987/"
        written: list[dict] = []
        meta = {
            "provider": "pexels",
            "asset_id": "987",
            "asset_key": "pexels:987",
            "image_url": image_url,
            "canonical_source": "pexels:987",
            "source_page_url": source_page,
            "creator_name": "Ada",
            "creator_url": "https://www.pexels.com/@ada",
            "license_name": "pexels-license",
            "license_url": "https://www.pexels.com/license/",
            "attribution_text": f"Photo by Ada on Pexels: {source_page}",
            "query": "bitcoin market",
            "score": 0.91,
        }
        with (
            patch("image_search.reuse.load_image_usage", return_value=[]),
            patch("image_search.reuse._write_image_usage", side_effect=lambda entries: written.extend(entries)),
        ):
            record_image_usage(meta, 55, "Round trip")
        serialized = json.dumps(written)
        v1_path = Mock(spec=Path)
        v1_path.exists.return_value = True
        v1_path.read_text.return_value = serialized
        with patch("stock_images._usage_path", return_value=v1_path):
            v1_entries = stock_images.load_stock_image_usage()
        v1_candidate = stock_images.StockImageCandidate(
            provider="pexels",
            image_url=image_url,
            query="bitcoin market",
            score=0.91,
            threshold=0.72,
            credit_name="Ada",
            credit_url=source_page,
            provider_asset_id="987",
        )
        recent = stock_images._candidate_recent_local_usage(v1_candidate, v1_entries)
        self.assertIsNotNone(recent)
        self.assertEqual(recent[0]["post_id"] if recent else None, 55)


class PublisherRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.routing = patch.multiple(
            publish_to_wp,
            IMAGE_SEARCH_ENGINE="v2",
            IMAGE_SOURCE_MODE="hybrid",
            IMAGE_SOURCE_PRIORITY="stock_first",
            USE_SOURCE_IMAGES=False,
            PRIMARY_IMAGE_PROVIDER="grok",
            IMAGE_FALLBACK_PROVIDER="openai",
            OPENAI_IMAGE_FALLBACK=True,
            IMAGE_GENERATION_ONLY_AFTER_SEARCH_EXHAUSTED=True,
            IMAGE_GENERATION_ON_PROVIDER_ERROR=False,
        )
        self.routing.start()
        self.session_post_patcher = patch.object(publish_to_wp.session, "post")
        self.session_post = self.session_post_patcher.start()
        self.session_post.return_value = Mock()

    def tearDown(self) -> None:
        self.session_post_patcher.stop()
        self.routing.stop()

    @staticmethod
    def _selected(provider: str = "openverse") -> tuple[bytes, str, dict]:
        return b"licensed", "licensed.jpg", {"provider": provider, "credit_text": "Credit"}

    def test_generated_fallback_not_reached_when_search_succeeds(self) -> None:
        response = Mock()
        response.json.return_value = {"id": 77}
        with (
            patch.object(publish_to_wp, "_search_stock_image_content", return_value=(self._selected(), False)),
            patch.object(publish_to_wp, "_generated_image_content") as generated,
            patch.object(publish_to_wp, "_post_with_retries", return_value=response),
        ):
            media_id, meta = publish_to_wp.upload_image(None, "Title", "bitcoin", article=ARTICLE)
        self.assertEqual(media_id, 77)
        self.assertEqual(meta["provider"], "openverse")
        generated.assert_not_called()

    def test_generated_fallback_reached_only_after_valid_exhaustion(self) -> None:
        response = Mock()
        response.json.return_value = {"id": 78}
        with (
            patch.object(publish_to_wp, "_search_stock_image_content", return_value=(None, True)),
            patch.object(publish_to_wp, "_generated_image_content", return_value=self._selected("grok")) as generated,
            patch.object(publish_to_wp, "_post_with_retries", return_value=response),
        ):
            media_id, _meta = publish_to_wp.upload_image(None, "Title", "bitcoin", article=ARTICLE)
        self.assertEqual(media_id, 78)
        generated.assert_called_once_with("grok", "Title", "bitcoin", fallback_used=False)

        with (
            patch.object(publish_to_wp, "_search_stock_image_content", return_value=(None, False)),
            patch.object(publish_to_wp, "_generated_image_content") as unavailable_generated,
        ):
            media_id, _meta = publish_to_wp.upload_image(None, "Title", "bitcoin", article=ARTICLE)
        self.assertIsNone(media_id)
        unavailable_generated.assert_not_called()

    def test_http_404_candidate_exhaustion_allows_generated_fallback(self) -> None:
        item = candidate("openverse", "only-missing")
        search_result = search_images(
            ARTICLE,
            providers=[StaticProvider("openverse", [item])],
            downloader=ImageDownloader(
                session=QueueDownloadSession([FakeDownloadResponse(404)]),
                max_retries=0,
                sleeper=lambda _seconds: None,
            ),
            local_usage=[],
            recent_wp_usage=[],
        )
        self.assertTrue(search_result.all_available_providers_exhausted)
        response = Mock()
        response.json.return_value = {"id": 79}
        with (
            patch.object(
                publish_to_wp,
                "_search_stock_image_content",
                return_value=(None, search_result.all_available_providers_exhausted),
            ),
            patch.object(
                publish_to_wp,
                "_generated_image_content",
                return_value=self._selected("grok"),
            ) as generated,
            patch.object(publish_to_wp, "_post_with_retries", return_value=response),
        ):
            media_id, _meta = publish_to_wp.upload_image(None, "Title", "bitcoin", article=ARTICLE)
        self.assertEqual(media_id, 79)
        generated.assert_called_once_with("grok", "Title", "bitcoin", fallback_used=False)

    def test_provider_timeout_prevents_generation_when_override_is_false(self) -> None:
        item = candidate("openverse", "timed-out")
        search_result = search_images(
            ARTICLE,
            providers=[StaticProvider("openverse", [item])],
            downloader=ImageDownloader(
                session=QueueDownloadSession([requests.Timeout("authenticated URL")]),
                max_retries=0,
                sleeper=lambda _seconds: None,
            ),
            local_usage=[],
            recent_wp_usage=[],
        )
        self.assertFalse(search_result.all_available_providers_exhausted)
        with (
            patch.object(
                publish_to_wp,
                "_search_stock_image_content",
                return_value=(None, search_result.all_available_providers_exhausted),
            ),
            patch.object(publish_to_wp, "_generated_image_content") as generated,
        ):
            media_id, _meta = publish_to_wp.upload_image(None, "Title", "bitcoin", article=ARTICLE)
        self.assertIsNone(media_id)
        generated.assert_not_called()

    def test_final_conversion_failure_tries_second_candidate_without_generation(self) -> None:
        first = candidate("openverse", "conversion-fails")
        first.final_score = 0.96
        second = candidate("openverse", "conversion-succeeds")
        second.final_score = 0.90
        provider = StaticProvider("openverse", [first, second])
        loader = StaticDownloader()
        response = Mock()
        response.json.return_value = {"id": 80}
        converted = (
            b"prepared-hero",
            {"width": 1600, "height": 900, "output_width": 1536, "output_height": 1024},
        )
        with (
            patch("image_search.selection.build_provider_registry", return_value=[provider]),
            patch("image_search.selection.load_image_usage", return_value=[]),
            patch("image_search.selection.load_recent_wp_image_usage", return_value=[]),
            patch("image_search.selection.ImageDownloader", return_value=loader),
            patch("image_search.selection.score_candidate", side_effect=lambda item, _article: item),
            patch.object(
                publish_to_wp,
                "_cover_bytes_to_1536x1024",
                side_effect=[None, converted],
            ) as converter,
            patch.object(publish_to_wp, "_generated_image_content") as generated,
            patch.object(publish_to_wp, "_post_with_retries", return_value=response),
        ):
            media_id, meta = publish_to_wp.upload_image(None, "Title", "bitcoin", article=ARTICLE)
        self.assertEqual(media_id, 80)
        self.assertEqual(meta["asset_id"], "conversion-succeeds")
        self.assertEqual(converter.call_count, 2)
        generated.assert_not_called()

    def test_wordpress_media_metadata_payload_includes_complete_attribution(self) -> None:
        raw = {
            "id": "ov-media",
            "foreign_identifier": "foreign-media",
            "source": "flickr",
            "url": "https://live.staticflickr.com/media.jpg",
            "foreign_landing_url": "https://flickr.com/photos/media",
            "creator": "Media Creator",
            "license": "by",
            "license_version": "4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "title": "Media work",
            "width": 1600,
            "height": 900,
        }
        item = OpenverseProvider(enabled=False).normalize_candidate(raw, "bitcoin", 0)
        self.assertIsNotNone(item)
        assert item is not None
        response = Mock()
        with patch.object(publish_to_wp.session, "post", return_value=response) as post:
            publish_to_wp.set_media_details(
                81,
                "Bitcoin market",
                caption=item.attribution_text,
                description=item.attribution_text,
            )
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["caption"], item.attribution_text)
        self.assertEqual(payload["description"], item.attribution_text)
        self.assertIn(raw["foreign_landing_url"], payload["description"])
        self.assertIn(raw["license_url"], payload["description"])
        response.raise_for_status.assert_called_once_with()

    def test_openverse_attribution_flows_through_upload_image_to_wordpress_metadata(self) -> None:
        raw = {
            "id": "ov-integration",
            "foreign_identifier": "foreign-integration",
            "source": "flickr",
            "url": "https://live.staticflickr.com/integration.jpg",
            "foreign_landing_url": "https://flickr.com/photos/integration",
            "creator": "Integration Creator",
            "creator_url": "https://flickr.com/people/integration",
            "license": "by",
            "license_version": "4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "title": "Bitcoin liquidity work",
            "tags": [{"name": "bitcoin"}, {"name": "liquidity"}],
        }
        item = OpenverseProvider(enabled=False, threshold=0.30).normalize_candidate(
            raw,
            "bitcoin liquidity",
            0,
        )
        self.assertIsNotNone(item)
        assert item is not None
        provider = StaticProvider("openverse", [item])
        loader = ImageDownloader(
            session=QueueDownloadSession([FakeDownloadResponse(200, encoded_image_bytes())]),
            max_retries=0,
            sleeper=lambda _seconds: None,
        )
        media_response = Mock()
        media_response.json.return_value = {"id": 82}
        with (
            patch("image_search.selection.build_provider_registry", return_value=[provider]),
            patch("image_search.selection.load_image_usage", return_value=[]),
            patch("image_search.selection.load_recent_wp_image_usage", return_value=[]),
            patch("image_search.selection.ImageDownloader", return_value=loader),
            patch.object(publish_to_wp, "_generated_image_content") as generated,
            patch.object(publish_to_wp, "_post_with_retries", return_value=media_response),
        ):
            media_id, meta = publish_to_wp.upload_image(
                None,
                "Article title",
                "bitcoin,liquidity",
                article={**ARTICLE, "seo_focus": "Bitcoin liquidity"},
            )
        self.assertEqual(media_id, 82)
        self.assertEqual(meta["provider"], "openverse")
        generated.assert_not_called()
        metadata_call = self.session_post.call_args
        self.assertTrue(metadata_call.args[0].endswith("/wp-json/wp/v2/media/82"))
        payload = metadata_call.kwargs["json"]
        attribution = payload["description"]
        self.assertEqual(payload["caption"], attribution)
        for expected in (
            raw["title"],
            raw["creator"],
            raw["foreign_landing_url"],
            "CC BY 4.0",
            raw["license_url"],
        ):
            self.assertIn(expected, attribution)

    def test_v1_rollback_calls_original_selector(self) -> None:
        selected = self._selected("pexels")
        with (
            patch.object(publish_to_wp, "IMAGE_SEARCH_ENGINE", "v1"),
            patch.object(publish_to_wp, "_stock_image_content_v1", return_value=selected) as v1,
            patch.object(publish_to_wp, "_stock_image_content_v2") as v2,
        ):
            result, exhausted = publish_to_wp._search_stock_image_content(ARTICLE)
        self.assertIs(result, selected)
        self.assertTrue(exhausted)
        v1.assert_called_once_with(ARTICLE)
        v2.assert_not_called()


if __name__ == "__main__":
    unittest.main()
