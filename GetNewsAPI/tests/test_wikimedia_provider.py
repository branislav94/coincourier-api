from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
import hashlib
import html
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote

from image_search.cache import CachedHttpClient
from image_search.commons_identity import commons_identities, commons_identities_from_text, commons_identity_conflict, normalize_commons_identity
from image_search.downloader import ImageDownloadError, MAX_IMAGE_BYTES
from image_search.license_policy import ImageLicensePolicy
from image_search.models import DownloadedImage, ImageCandidate
from image_search.provider import ImageSearchProvider, ProviderUnavailable
from image_search.providers.openverse import OpenverseProvider
from image_search.providers.wikimedia import EXTMETADATA_FIELDS, WIKIMEDIA_SEARCH_URL, WIKIMEDIA_USER_AGENT, WikimediaProvider
from image_search.registry import build_provider_registry
from image_search.reuse import candidate_recent_local_usage, candidate_recent_wp_usage, load_image_usage, record_image_usage
from image_search.selection import search_images


ARTICLE = {"seo_focus": "Bitcoin ETF liquidity", "title": "Bitcoin ETF trading volume rises", "hashtags": "bitcoin,markets", "category": "Markets", "tickers": "BTC"}
FILE_TITLE = "Bitcoin ETF liquidity.jpg"
ORIGINAL = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Bitcoin_ETF_liquidity.jpg"
FILE_PAGE = "https://commons.wikimedia.org/wiki/File:Bitcoin_ETF_liquidity.jpg"
TRACKING_QUERY = "?utm_source=commons.wikimedia.org&utm_campaign=api&utm_content=fixture"


def page(pageid: int = 123, *, license_name: str = "CC BY 4.0", license_url: str = "https://creativecommons.org/licenses/by/4.0/") -> dict:
    metadata = {
        "Artist": '<a href="https://commons.wikimedia.org/wiki/User:Ada">Ada &amp; Lin</a>',
        "Credit": "Own work", "Attribution": "Please credit Ada and Lin.",
        "AttributionRequired": "true", "LicenseShortName": license_name, "LicenseUrl": license_url,
        "UsageTerms": license_name, "Copyrighted": "True" if license_name.startswith("CC BY") else "False",
        "NonFree": "False", "ObjectName": "Bitcoin ETF liquidity", "ImageDescription": "Bitcoin ETF liquidity trading cryptocurrency markets",
    }
    return {
        "pageid": pageid, "ns": 6, "title": f"File:{FILE_TITLE}", "index": 1,
        "imageinfo": [{
            "url": ORIGINAL, "descriptionurl": FILE_PAGE,
            "descriptionshorturl": f"https://commons.wikimedia.org/?curid={pageid}",
            "width": 1600, "height": 900, "size": 12000, "mime": "image/jpeg",
            "extmetadata": {key: {"value": value} for key, value in metadata.items()},
        }],
    }


def candidate(pageid: int = 123) -> ImageCandidate:
    item = WikimediaProvider(enabled=False).normalize_candidate(page(pageid), "bitcoin etf liquidity", 0)
    assert item is not None
    return item


def live_style_jpeg() -> dict:
    """Public structural fields observed 2026-10-28; credits/descriptions are fake.

    No complete live response or real creator HTML is stored in the repository.
    """
    raw = page(92021811, license_name="CC BY 2.0", license_url="https://creativecommons.org/licenses/by/2.0")
    raw["title"] = "File:Bitcoin and cryptocurrency (38688311185).jpg"
    info = raw["imageinfo"][0]
    filename = quote(raw["title"][5:].replace(" ", "_"), safe="()")
    info.update(
        canonicaltitle=raw["title"], mime="image/jpeg", width=2000, height=1300, size=3886027,
        url=f"https://upload.wikimedia.org/wikipedia/commons/4/4f/{filename}{TRACKING_QUERY}",
        descriptionurl=f"https://commons.wikimedia.org/wiki/File:{filename}",
    )
    info["extmetadata"]["Artist"] = {"value": '<a href="https://creator.example/fixture">Fixture Creator</a>'}
    info["extmetadata"]["Attribution"] = {"value": "Fixture required notice."}
    info["extmetadata"]["Credit"] = {"value": "Fixture source credit."}
    info["extmetadata"]["ObjectName"] = {"value": "Bitcoin and cryptocurrency"}
    info["extmetadata"]["ImageDescription"] = {"value": "Bitcoin cryptocurrency markets"}
    return raw


def downloaded(seed: bytes = b"wikimedia-image", dhash: str = "0123456789abcdef") -> DownloadedImage:
    return DownloadedImage(seed, "image/jpeg", 1600, 900, hashlib.sha256(seed).hexdigest(), dhash)


class PayloadClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[tuple, dict]] = []

    def get_json(self, *args, **kwargs) -> dict:
        self.calls.append((args, kwargs))
        data = deepcopy(self.payload)
        if not kwargs["payload_validator"](data):
            raise ProviderUnavailable("wikimedia", "invalid_response")
        return data


class StaticProvider(ImageSearchProvider):
    def __init__(self, name: str, items: list[ImageCandidate], *, fail: bool = False) -> None:
        self.provider_name, self.enabled, self.candidate_threshold = name, True, 0.70
        self.items, self.fail, self.calls = items, fail, 0

    def search(self, query: str) -> list[ImageCandidate]:
        self.calls += 1
        if self.fail:
            raise ProviderUnavailable(self.provider_name, "timeout", retry_exhausted=True)
        if self.calls > 1:
            return []
        for item in self.items:
            item.query = query
        return self.items

    def normalize_candidate(self, raw: dict, query: str, provider_rank: int) -> ImageCandidate | None:
        raise NotImplementedError


class StaticDownloader:
    def __init__(self, image: DownloadedImage | None = None, *, fail_first: bool = False) -> None:
        self.image, self.fail_first, self.calls = image or downloaded(), fail_first, []

    def download(self, item: ImageCandidate) -> DownloadedImage:
        self.calls.append(item.asset_key)
        if self.fail_first and len(self.calls) == 1:
            raise ImageDownloadError("http_404")
        item.content_sha256, item.perceptual_hash = self.image.content_sha256, self.image.perceptual_hash
        return self.image


def search(items: list[ImageCandidate], *, local_usage: list[dict] | None = None, wp_usage: list[dict] | None = None, downloader: StaticDownloader | None = None):
    return search_images(ARTICLE, providers=[StaticProvider("wikimedia", items)], downloader=downloader or StaticDownloader(), local_usage=local_usage or [], recent_wp_usage=wp_usage or [])


def openverse_candidate(*, foreign_id: str = "123", landing: str = "https://commons.wikimedia.org/?curid=123", original: str = ORIGINAL) -> ImageCandidate:
    item = OpenverseProvider(enabled=False).normalize_candidate({
        "id": "openverse-uuid", "source": "wikimedia", "foreign_identifier": foreign_id,
        "foreign_landing_url": landing, "url": original, "creator": "Ada & Lin",
        "license": "by", "license_version": "4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "title": "Bitcoin ETF liquidity", "tags": [{"name": "bitcoin etf liquidity cryptocurrency markets"}], "width": 1600, "height": 900,
    }, "bitcoin etf liquidity", 0)
    assert item is not None
    return item


class WikimediaRequestTests(unittest.TestCase):
    def test_exact_bounded_request_and_descriptive_user_agent(self) -> None:
        client = PayloadClient({"query": {"pages": [page()]}})
        provider = WikimediaProvider(client=client, per_page=500)
        items = provider.search("bitcoin etf liquidity")
        args, kwargs = client.calls[0]
        self.assertEqual(args, ("wikimedia", "bitcoin etf liquidity", WIKIMEDIA_SEARCH_URL))
        self.assertEqual(kwargs["params"], {
            "action": "query", "format": "json", "formatversion": 2, "generator": "search",
            "gsrsearch": "bitcoin etf liquidity", "gsrnamespace": 6, "gsrlimit": 10,
            "prop": "imageinfo", "iiprop": "url|size|mime|canonicaltitle|extmetadata", "iilimit": 1,
            "iiextmetadatalanguage": "en", "iiextmetadatafilter": EXTMETADATA_FIELDS, "maxlag": 5,
        })
        self.assertEqual(kwargs["headers"], {"User-Agent": WIKIMEDIA_USER_AGENT})
        self.assertIn("CoinCourier", WIKIMEDIA_USER_AGENT)
        self.assertIn("GetNewsAPI", WIKIMEDIA_USER_AGENT)
        self.assertIn("https://github.com/branislav94/coincourier-api", WIKIMEDIA_USER_AGENT)
        self.assertEqual(items[0].asset_key, "wikimedia:123")
        self.assertEqual(provider.last_search_stats, {"raw": 1, "normalized": 1})

    def test_disabled_provider_makes_no_request(self) -> None:
        client = PayloadClient({})
        self.assertEqual(WikimediaProvider(client=client, enabled=False).search("bitcoin"), [])
        self.assertEqual(client.calls, [])

    def test_explicit_registry_position_and_no_default_activation(self) -> None:
        providers = build_provider_registry(("openverse", "wikimedia", "wikimedia"))
        self.assertEqual([provider.provider_name for provider in providers], ["openverse", "wikimedia"])
        from config import IMAGE_SEARCH_PROVIDERS
        self.assertNotIn("wikimedia", IMAGE_SEARCH_PROVIDERS)

    def test_legitimate_empty_pages_and_empty_generator_are_valid(self) -> None:
        for payload in ({"query": {"pages": []}}, {"batchcomplete": True}):
            with self.subTest(payload=payload):
                self.assertEqual(WikimediaProvider(client=PayloadClient(payload)).search("bitcoin"), [])

    def test_malformed_payloads_and_http_200_api_errors_fail(self) -> None:
        for payload in ({}, {"query": {}}, {"query": {"pages": {}}}, {"query": {"pages": [{}]}}, {"batchcomplete": True, "warnings": {"query": "ignored parameter"}}, {"batchcomplete": True, "continue": {"gsroffset": 10}}, {"error": {"code": "badvalue", "info": "private request details"}}, {"error": {"code": "maxlag", "info": "server busy"}}):
            with self.subTest(payload=payload):
                with self.assertRaises(ProviderUnavailable) as caught:
                    WikimediaProvider(client=PayloadClient(payload)).search("bitcoin")
                self.assertNotIn("private request details", str(caught.exception))

    def test_api_error_is_not_written_to_cache(self) -> None:
        response = Mock(status_code=200, headers={})
        response.json.return_value = {"error": {"code": "maxlag", "info": "server busy"}}
        session = Mock()
        session.request.return_value = response
        client = CachedHttpClient(session=session, max_retries=0)
        with patch.object(client, "_read_cache", return_value=None), patch.object(client, "_write_cache") as write:
            with self.assertRaises(ProviderUnavailable):
                WikimediaProvider(client=client).search("bitcoin")
        write.assert_not_called()

    def test_missing_imageinfo_is_rejected_with_visible_reason(self) -> None:
        raw = page()
        raw.pop("imageinfo")
        provider = WikimediaProvider(client=PayloadClient({"query": {"pages": [raw]}}))
        self.assertEqual(provider.search("bitcoin"), [])
        self.assertEqual(provider.last_search_rejections, {"missing_imageinfo": 1})


class WikimediaLiveCompatibilityTests(unittest.TestCase):
    def test_live_style_jpeg_tracking_url_reaches_normalization(self) -> None:
        raw = live_style_jpeg()
        provider = WikimediaProvider(client=PayloadClient({"query": {"pages": [raw]}}))
        items = provider.search("bitcoin cryptocurrency")
        self.assertEqual(provider.last_search_stats, {"raw": 1, "normalized": 1})
        self.assertEqual(provider.last_search_rejections, {})
        self.assertEqual(items[0].canonical_source, "wikimedia:92021811")
        self.assertEqual(items[0].usable_url, raw["imageinfo"][0]["url"])
        self.assertIn(raw["title"], items[0].identity_aliases)
        self.assertIn(raw["imageinfo"][0]["descriptionurl"], items[0].identity_aliases)
        self.assertIn(raw["imageinfo"][0]["descriptionshorturl"], items[0].identity_aliases)
        self.assertIn("wikimedia:file:Bitcoin and cryptocurrency (38688311185).jpg", items[0].identity_aliases)

    def test_only_observed_tracking_query_keys_are_accepted(self) -> None:
        for suffix in (
            "?download=1", "?width=1200", "?utm_medium=unobserved", "?utm_source=ok&download=1",
            "?download", "?utm_source", "?utm_source=%ZZ", "?utm_source=%FF", "#fragment",
        ):
            with self.subTest(suffix=suffix):
                raw = page()
                raw["imageinfo"][0]["url"] = ORIGINAL + suffix
                provider = WikimediaProvider(enabled=False)
                self.assertIsNone(provider.normalize_candidate(raw, "bitcoin", 0))
                self.assertEqual(provider.last_search_rejections, {"invalid_original_url": 1})
        raw = page()
        raw["imageinfo"][0]["url"] = ORIGINAL.replace("upload.wikimedia.org", "upload.wikimedia.org.evil.example") + TRACKING_QUERY
        provider = WikimediaProvider(enabled=False)
        self.assertIsNone(provider.normalize_candidate(raw, "bitcoin", 0))
        self.assertEqual(provider.last_search_rejections, {"invalid_original_url": 1})

    def test_live_style_raster_shapes_keep_strict_license_mapping(self) -> None:
        # PNG/WebP structures use observed dimensions/MIME, synthetic file names
        # and fixture licenses; they do not assert rights in the observed files.
        for mime, extension, dims, size, label, url in (
            ("image/png", "png", (1400, 825), 61927, "PDM 1.0", "https://creativecommons.org/publicdomain/mark/1.0/"),
            ("image/webp", "webp", (4827, 1644), 105024, "CC0", "https://creativecommons.org/publicdomain/zero/1.0/deed.en"),
        ):
            with self.subTest(mime=mime):
                raw = page(123, license_name=label, license_url=url)
                raw["title"] = f"File:Bitcoin fixture.{extension}"
                info = raw["imageinfo"][0]
                info.update(canonicaltitle=raw["title"], mime=mime, width=dims[0], height=dims[1], size=size,
                            url=f"https://upload.wikimedia.org/wikipedia/commons/a/ab/Bitcoin_fixture.{extension}{TRACKING_QUERY}",
                            descriptionurl=f"https://commons.wikimedia.org/wiki/File:Bitcoin_fixture.{extension}")
                item = WikimediaProvider(enabled=False).normalize_candidate(raw, "bitcoin", 0)
                self.assertIsNotNone(item)
                assert item is not None
                self.assertTrue(ImageLicensePolicy(("cc0", "pdm", "cc-by")).validate(item)[0])

    def test_page_and_canonical_title_are_preserved_as_equivalent_aliases(self) -> None:
        raw = page()
        raw["imageinfo"][0]["canonicaltitle"] = f"File:{FILE_TITLE.replace(' ', '_')}"
        item = WikimediaProvider(enabled=False).normalize_candidate(raw, "bitcoin", 0)
        assert item is not None
        self.assertIn(raw["title"], item.identity_aliases)
        self.assertIn(raw["imageinfo"][0]["canonicaltitle"], item.identity_aliases)
        self.assertEqual(item.canonical_source, "wikimedia:123")
        self.assertIn("wikimedia:file:Bitcoin ETF liquidity.jpg", item.identity_aliases)

    def test_genuinely_different_canonical_title_is_still_a_conflict(self) -> None:
        raw = page()
        raw["imageinfo"][0]["canonicaltitle"] = "File:Different image.jpg"
        provider = WikimediaProvider(enabled=False)
        self.assertIsNone(provider.normalize_candidate(raw, "bitcoin", 0))
        self.assertEqual(provider.last_search_rejections, {"commons_identity_conflict": 1})

    def test_candidate_field_failures_have_distinct_reasons(self) -> None:
        cases = []
        for key, value, reason in (("pageid", None, "missing_page_id"), ("ns", 0, "invalid_namespace"), ("title", "Bitcoin", "invalid_file_title")):
            raw = page()
            raw[key] = value
            cases.append((raw, reason))
        raw = page()
        raw["imageinfo"] = []
        cases.append((raw, "missing_imageinfo"))
        raw = page()
        raw["imageinfo"][0]["canonicaltitle"] = "Different namespace:Bitcoin"
        cases.append((raw, "invalid_canonical_title"))
        raw = page()
        raw["imageinfo"][0]["url"] = ORIGINAL.replace("liquidity", "liquidity%FF")
        cases.append((raw, "unrecognized_commons_identity"))
        raw = page()
        raw["imageinfo"][0]["descriptionshorturl"] = "https://commons.wikimedia.org/?curid=456"
        cases.append((raw, "commons_identity_conflict"))
        for raw, reason in cases:
            with self.subTest(reason=reason):
                provider = WikimediaProvider(enabled=False)
                self.assertIsNone(provider.normalize_candidate(raw, "bitcoin", 0))
                self.assertEqual(provider.last_search_rejections, {reason: 1})

    def test_provider_summary_exposes_specific_url_rejection(self) -> None:
        raw = page()
        raw["imageinfo"][0]["url"] = ORIGINAL + "?download=1"
        provider = WikimediaProvider(client=PayloadClient({"query": {"pages": [raw]}}))
        with self.assertLogs("image_search.selection", level="INFO") as captured:
            result = search_images(ARTICLE, providers=[provider], downloader=StaticDownloader(), local_usage=[], recent_wp_usage=[])
        self.assertIsNone(result.candidate)
        self.assertIn("invalid_original_url", " ".join(captured.output))
        self.assertNotIn("required_fields", " ".join(captured.output))

    def test_live_style_tracking_keeps_cross_provider_and_old_history_identity(self) -> None:
        raw = live_style_jpeg()
        direct = WikimediaProvider(enabled=False).normalize_candidate(raw, "bitcoin cryptocurrency", 0)
        assert direct is not None
        indexed = openverse_candidate(foreign_id="92021811", landing="https://commons.wikimedia.org/?curid=92021811", original=raw["imageinfo"][0]["url"])
        self.assertEqual(indexed.canonical_source, direct.canonical_source)
        old_entry = {"provider": "openverse", "canonical_source": "wikimedia:92021811", "credit_url": "http://commons.wikimedia.org/?curid=92021811"}
        self.assertIs(candidate_recent_local_usage(direct, [old_entry]), old_entry)
        old_entry = {"provider": "wikimedia", "source_page_url": raw["imageinfo"][0]["descriptionurl"]}
        self.assertIs(candidate_recent_local_usage(indexed, [old_entry]), old_entry)

    def test_live_style_long_attribution_stays_complete_and_sanitized(self) -> None:
        raw = live_style_jpeg()
        notice = "Fixture required notice. " * 100
        raw["imageinfo"][0]["extmetadata"]["Attribution"] = {"value": notice + "&lt;img onerror=bad&gt;<script>bad()</script>"}
        item = WikimediaProvider(enabled=False).normalize_candidate(raw, "bitcoin", 0)
        assert item is not None
        self.assertGreater(len(item.attribution_text), 1000)
        self.assertIn(notice.strip(), html.unescape(item.attribution_text))
        self.assertNotIn("<img", item.attribution_text)
        self.assertNotIn("bad()", item.attribution_text)

    def test_live_style_wikimedia_selection_after_empty_providers_prevents_grok(self) -> None:
        import publishing.wordpress.publisher as publisher
        article = {"seo_focus": "bitcoin cryptocurrency", "title": "Bitcoin cryptocurrency markets", "category": "Markets", "hashtags": "bitcoin,crypto"}
        provider = WikimediaProvider(client=PayloadClient({"query": {"pages": [live_style_jpeg()]}}))
        results = []

        def select(context, **kwargs):
            result = search_images(context, providers=[StaticProvider("pexels", []), StaticProvider("pixabay", []), StaticProvider("openverse", []), provider],
                                   downloader=StaticDownloader(), local_usage=[], recent_wp_usage=[], **kwargs)
            results.append(result)
            return result

        with (
            patch.object(publisher, "IMAGE_SOURCE_MODE", "hybrid"), patch.object(publisher, "IMAGE_SOURCE_PRIORITY", "stock_first"),
            patch.object(publisher, "IMAGE_SEARCH_ENGINE", "v2"), patch.object(publisher, "search_images", side_effect=select),
            patch.object(publisher, "_cover_bytes_to_1536x1024", return_value=(b"prepared", {"width": 1536, "height": 1024})),
            patch.object(publisher, "upload_media", return_value=42), patch.object(publisher, "set_media_details", return_value=True),
            patch.object(publisher, "_generated_image_content") as generate,
        ):
            media_id, meta = publisher.upload_image(None, article["title"], "bitcoin", article=article)
        self.assertEqual(media_id, 42)
        self.assertEqual(meta["provider"], "wikimedia")
        self.assertEqual(results[0].candidate.provider, "wikimedia")
        self.assertGreater(provider.last_search_stats["normalized"], 0)
        generate.assert_not_called()


class WikimediaRetryTests(unittest.TestCase):
    @staticmethod
    def client(payloads: list[dict], *, headers: list[dict] | None = None, statuses: list[int] | None = None, max_retries: int = 1):
        responses = []
        for index, payload in enumerate(payloads):
            response = Mock(status_code=(statuses[index] if statuses else 200), headers=(headers[index] if headers else {}))
            response.json.return_value = payload
            responses.append(response)
        session, sleeper = Mock(), Mock()
        session.request.side_effect = responses
        client = CachedHttpClient(session=session, max_retries=max_retries, sleeper=sleeper)
        client._read_cache = Mock(return_value=None)
        client._write_cache = Mock()
        return client, session, sleeper

    def test_maxlag_retries_then_caches_only_valid_success(self) -> None:
        success = {"query": {"pages": [page()]}}
        client, session, sleeper = self.client([
            {"error": {"code": "maxlag", "lag": 8, "info": "private upstream details"}}, success,
        ])
        items = WikimediaProvider(client=client).search("bitcoin")
        self.assertEqual(items[0].asset_key, "wikimedia:123")
        self.assertEqual(session.request.call_count, 2)
        sleeper.assert_called_once_with(8.0)
        client._write_cache.assert_called_once()
        self.assertEqual(client._write_cache.call_args.args[1], success)

    def test_ratelimited_retries_with_bounded_fallback(self) -> None:
        client, session, sleeper = self.client([
            {"error": {"code": "ratelimited", "info": "private details"}}, {"query": {"pages": []}},
        ])
        self.assertEqual(WikimediaProvider(client=client).search("bitcoin"), [])
        self.assertEqual(session.request.call_count, 2)
        sleeper.assert_called_once_with(5.0)

    def test_maxlag_delay_has_safe_minimum_and_maximum(self) -> None:
        for lag, expected in ((0, 5.0), ("invalid", 5.0), (float("nan"), 5.0), (float("inf"), 5.0), (1000, 30.0)):
            with self.subTest(lag=lag):
                client, session, sleeper = self.client([
                    {"error": {"code": "maxlag", "lag": lag}}, {"batchcomplete": True},
                ])
                self.assertEqual(WikimediaProvider(client=client).search("bitcoin"), [])
                self.assertEqual(session.request.call_count, 2)
                sleeper.assert_called_once_with(expected)

    def test_numeric_retry_after_overrides_api_fallback(self) -> None:
        for code in ("maxlag", "ratelimited"):
            with self.subTest(code=code):
                client, _, sleeper = self.client([
                    {"error": {"code": code, "lag": 8}}, {"batchcomplete": True},
                ], headers=[{"Retry-After": "12.5"}, {}])
                WikimediaProvider(client=client).search("bitcoin")
                sleeper.assert_called_once_with(12.5)

    def test_http_date_retry_after_uses_server_time(self) -> None:
        now = 1700000000.0
        retry_at = format_datetime(datetime.fromtimestamp(now + 17, timezone.utc), usegmt=True)
        client, _, sleeper = self.client([
            {"error": {"code": "maxlag", "lag": 8}}, {"batchcomplete": True},
        ], headers=[{"Retry-After": retry_at}, {}])
        with patch("image_search.cache.time.time", return_value=now):
            WikimediaProvider(client=client).search("bitcoin")
        sleeper.assert_called_once_with(17.0)

    def test_invalid_retry_after_uses_safe_api_fallback(self) -> None:
        for value in ("invalid", "-5", "nan", "inf"):
            with self.subTest(value=value):
                client, _, sleeper = self.client([
                    {"error": {"code": "maxlag", "lag": 9}}, {"batchcomplete": True},
                ], headers=[{"Retry-After": value}, {}])
                WikimediaProvider(client=client).search("bitcoin")
                sleeper.assert_called_once_with(9.0)

    def test_temporary_api_error_exhaustion_is_bounded_and_sanitized(self) -> None:
        for code in ("maxlag", "ratelimited"):
            with self.subTest(code=code):
                error = {"error": {"code": code, "info": "token=private-secret url=https://private.invalid", "lag": 7}}
                client, session, sleeper = self.client([error, error, error], max_retries=2)
                with self.assertRaises(ProviderUnavailable) as caught:
                    WikimediaProvider(client=client).search("bitcoin")
                self.assertEqual(session.request.call_count, 3)
                self.assertEqual(sleeper.call_count, 2)
                self.assertTrue(caught.exception.retry_exhausted)
                self.assertEqual(caught.exception.reason, "invalid_response")
                self.assertNotIn("private", str(caught.exception))
                client._write_cache.assert_not_called()

    def test_zero_retry_budget_makes_one_request_and_never_sleeps(self) -> None:
        client, session, sleeper = self.client([{"error": {"code": "maxlag", "lag": 10}}], max_retries=0)
        with self.assertRaises(ProviderUnavailable) as caught:
            WikimediaProvider(client=client).search("bitcoin")
        self.assertTrue(caught.exception.retry_exhausted)
        self.assertEqual(session.request.call_count, 1)
        sleeper.assert_not_called()
        client._write_cache.assert_not_called()

    def test_http_and_api_errors_share_one_retry_budget(self) -> None:
        client, session, sleeper = self.client([
            {}, {"error": {"code": "maxlag", "lag": 10}},
        ], statuses=[503, 200], max_retries=1)
        with self.assertRaises(ProviderUnavailable) as caught:
            WikimediaProvider(client=client).search("bitcoin")
        self.assertTrue(caught.exception.retry_exhausted)
        self.assertEqual(session.request.call_count, 2)
        sleeper.assert_called_once_with(0.5)
        client._write_cache.assert_not_called()

    def test_malformed_or_unknown_error_payload_is_not_retried(self) -> None:
        for payload in ({}, {"query": {}}, {"error": {"code": "badvalue", "info": "private-token"}}, {"error": "maxlag"}):
            with self.subTest(payload=payload):
                client, session, sleeper = self.client([payload], max_retries=5)
                with self.assertRaises(ProviderUnavailable) as caught:
                    WikimediaProvider(client=client).search("bitcoin")
                self.assertEqual(session.request.call_count, 1)
                self.assertFalse(caught.exception.retry_exhausted)
                self.assertEqual(str(caught.exception), "provider=wikimedia reason=invalid_response")
                sleeper.assert_not_called()
                client._write_cache.assert_not_called()


class WikimediaLicenseTests(unittest.TestCase):
    def test_valid_cc0_and_recognized_public_domain(self) -> None:
        for label, url, expected in (
            ("CC0", "https://creativecommons.org/publicdomain/zero/1.0/", "cc0"),
            ("Public domain", "https://creativecommons.org/publicdomain/mark/1.0/", "pdm"),
            ("PDM 1.0", "https://creativecommons.org/publicdomain/mark/1.0/", "pdm"),
        ):
            with self.subTest(label=label):
                item = WikimediaProvider(enabled=False).normalize_candidate(page(license_name=label, license_url=url), "bitcoin", 0)
                self.assertIsNotNone(item)
                assert item is not None
                self.assertEqual(item.license_name, expected)
                self.assertTrue(ImageLicensePolicy(("cc0", "pdm", "cc-by")).validate(item)[0])

    def test_valid_cc_by_preserves_complete_sanitized_attribution(self) -> None:
        raw = page()
        raw["imageinfo"][0]["extmetadata"]["Attribution"]["value"] += '<script>steal()</script><a href="javascript:bad()">Credit notice</a>'
        item = WikimediaProvider(enabled=False).normalize_candidate(raw, "bitcoin", 0)
        assert item is not None
        self.assertEqual(item.license_name, "cc-by")
        self.assertEqual(item.license_version, "4.0")
        for expected in ("Ada & Lin", "Own work", "Please credit Ada and Lin", "Credit notice", FILE_PAGE, "CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"):
            self.assertIn(expected, html.unescape(item.attribution_text))
        for forbidden in ("<script", "steal()", "javascript:", "<a"):
            self.assertNotIn(forbidden, item.attribution_text)
        self.assertTrue(ImageLicensePolicy(("cc0", "pdm", "cc-by")).validate(item)[0])

    def test_entity_encoded_markup_cannot_reenter_wordpress_html(self) -> None:
        raw = page()
        raw["imageinfo"][0]["extmetadata"]["Attribution"] = {"value": "Notice &lt;img src=x onerror=alert(1)&gt;"}
        item = WikimediaProvider(enabled=False).normalize_candidate(raw, "bitcoin", 0)
        assert item is not None
        self.assertNotIn("<img", item.attribution_text)
        self.assertIn("&lt;img", item.attribution_text)

    def test_unsupported_and_custom_licenses_reject(self) -> None:
        for label, slug in (("CC BY-SA 4.0", "by-sa"), ("CC BY-NC 4.0", "by-nc"), ("CC BY-ND 4.0", "by-nd"), ("CC BY-NC-SA 4.0", "by-nc-sa"), ("CC BY-NC-ND 4.0", "by-nc-nd"), ("Custom license", "by"), ("unknown", "by")):
            with self.subTest(label=label):
                self.assertIsNone(WikimediaProvider(enabled=False).normalize_candidate(page(license_name=label, license_url=f"https://creativecommons.org/licenses/{slug}/4.0/"), "bitcoin", 0))

    def test_missing_conflicting_and_uncertain_licenses_reject(self) -> None:
        for label, url in (("", ""), ("CC BY 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"), ("CC BY 4.0", "https://creativecommons.org/licenses/by/3.0/"), ("CC BY 4.0", "https://example.org/licenses/by/4.0/"), ("CC BY 4.0", "https://creativecommons.org.evil.example/licenses/by/4.0/"), ("CC BY 4.0 or CC BY-SA 4.0", "https://creativecommons.org/licenses/by/4.0/"), ("Public domain", ""), ("Public domain", "https://example.org/public-domain")):
            with self.subTest(label=label, url=url):
                self.assertIsNone(WikimediaProvider(enabled=False).normalize_candidate(page(license_name=label, license_url=url), "bitcoin", 0))

    def test_missing_license_metadata_rejects_even_when_not_copyrighted(self) -> None:
        raw = page()
        raw["imageinfo"][0]["extmetadata"] = {"Copyrighted": {"value": "False"}}
        self.assertIsNone(WikimediaProvider(enabled=False).normalize_candidate(raw, "bitcoin", 0))

    def test_nonfree_restrictions_deletion_and_conflicting_terms_reject(self) -> None:
        for field, value in (("NonFree", "true"), ("Restrictions", "personality"), ("DeletionReason", "copyright concern"), ("UsageTerms", "CC BY-SA 4.0"), ("Copyrighted", "False")):
            with self.subTest(field=field):
                raw = page()
                raw["imageinfo"][0]["extmetadata"][field] = {"value": value}
                self.assertIsNone(WikimediaProvider(enabled=False).normalize_candidate(raw, "bitcoin", 0))

    def test_cc_by_without_known_artist_rejects(self) -> None:
        for artist in ("Unknown", '<a href="https://example.org/author">Unknown</a>'):
            raw = page()
            raw["imageinfo"][0]["extmetadata"]["Artist"] = {"value": artist}
            self.assertIsNone(WikimediaProvider(enabled=False).normalize_candidate(raw, "bitcoin", 0))

    def test_unsupported_mimes_and_oversized_originals_reject(self) -> None:
        for mime in ("image/svg+xml", "image/tiff", "application/pdf", "video/webm", "audio/ogg", ""):
            with self.subTest(mime=mime):
                raw = page()
                raw["imageinfo"][0]["mime"] = mime
                self.assertIsNone(WikimediaProvider(enabled=False).normalize_candidate(raw, "bitcoin", 0))
        raw = page()
        raw["imageinfo"][0]["size"] = MAX_IMAGE_BYTES + 1
        self.assertIsNone(WikimediaProvider(enabled=False).normalize_candidate(raw, "bitcoin", 0))

    def test_undersized_and_portrait_candidates_reject_before_download(self) -> None:
        for dimensions, reason in (((1199, 675), "dimensions_rejected"), ((1200, 674), "dimensions_rejected"), ((1200, 1600), "orientation_rejected")):
            with self.subTest(dimensions=dimensions):
                item = candidate()
                item.width, item.height = dimensions
                downloader = StaticDownloader()
                result = search([item], downloader=downloader)
                self.assertIsNone(result.candidate)
                self.assertEqual(downloader.calls, [])
                self.assertEqual(result.rejection_counts[reason], 1)


class CommonsIdentityTests(unittest.TestCase):
    def test_numeric_id_requires_verified_source(self) -> None:
        self.assertEqual(commons_identities(source="wikimedia", foreign_identifier="00123"), {"wikimedia:123"})
        self.assertEqual(commons_identities(source="wikimedia_commons", foreign_identifier=123), {"wikimedia:123"})
        for source in ("flickr", "example.org", ""):
            self.assertEqual(commons_identities(source=source, foreign_identifier="123"), set())
        self.assertEqual(commons_identities(source="wikimedia", foreign_identifier=True), set())

    def test_file_url_variants_and_thumbnail_original_share_alias(self) -> None:
        urls = (
            FILE_PAGE, "http://commons.m.wikimedia.org/wiki/File:Bitcoin%20ETF%20liquidity.jpg",
            "https://commons.wikimedia.org/w/index.php?title=File%3ABitcoin_ETF_liquidity.jpg",
            ORIGINAL, "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Bitcoin_ETF_liquidity.jpg/320px-Bitcoin_ETF_liquidity.jpg",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(commons_identities(url), {f"wikimedia:file:{FILE_TITLE}"})

    def test_filename_case_is_preserved_and_unrelated_hosts_are_not_commons(self) -> None:
        self.assertNotEqual(commons_identities(FILE_PAGE), commons_identities(FILE_PAGE.replace("Bitcoin", "bitcoin")))
        for value in (FILE_PAGE.replace("commons.wikimedia.org", "commons.wikimedia.org.evil.example"), FILE_PAGE.replace("commons.wikimedia.org", "example.org"), "https://en.wikipedia.org/wiki/File:Bitcoin.jpg", "File:Bitcoin.jpg", "123", "https://user:password@commons.wikimedia.org/?curid=123"):
            self.assertEqual(commons_identities(value), set(), value)

    def test_literal_percent_titles_decode_only_at_url_boundaries(self) -> None:
        literal = "wikimedia:file:Bitcoin%20coin.jpg"
        spaced = "wikimedia:file:Bitcoin coin.jpg"
        self.assertEqual(commons_identities("File:Bitcoin%20coin.jpg", source="wikimedia"), {literal})
        self.assertEqual(commons_identities("https://commons.wikimedia.org/wiki/File:Bitcoin%2520coin.jpg"), {literal})
        self.assertEqual(commons_identities("https://commons.wikimedia.org/w/index.php?title=File%3ABitcoin%2520coin.jpg"), {literal})
        self.assertEqual(commons_identities("https://commons.wikimedia.org/wiki/File:Bitcoin%20coin.jpg"), {spaced})
        self.assertEqual(commons_identities(literal), {literal})

    def test_literal_entity_filename_is_not_decoded_as_html(self) -> None:
        literal = "wikimedia:file:A&amp;B.jpg"
        self.assertEqual(commons_identities("File:A&amp;B.jpg", source="wikimedia"), {literal})
        self.assertEqual(commons_identities("https://commons.wikimedia.org/wiki/File:A%26amp%3BB.jpg"), {literal})
        self.assertEqual(commons_identities(literal), {literal})
        self.assertNotEqual(commons_identities(literal), commons_identities("wikimedia:file:A&B.jpg"))

    def test_invalid_utf8_and_malformed_url_percent_escapes_reject(self) -> None:
        for filename in ("Bitcoin%FF.jpg", "Bitcoin%FE.jpg", "Bitcoin%ZZ.jpg", "Bitcoin%.jpg"):
            with self.subTest(filename=filename):
                self.assertEqual(commons_identities(f"https://commons.wikimedia.org/wiki/File:{filename}"), set())
                self.assertEqual(commons_identities(f"https://upload.wikimedia.org/wikipedia/commons/a/ab/{filename}"), set())
                self.assertEqual(commons_identities(f"https://commons.wikimedia.org/w/index.php?title=File:{filename}"), set())
        self.assertEqual(commons_identities("https://commons.wikimedia.org/wiki/File:Bitcoin%25ZZ.jpg"), {"wikimedia:file:Bitcoin%ZZ.jpg"})

    def test_strongest_page_identity_preserves_raw_and_title_aliases(self) -> None:
        canonical, aliases = normalize_commons_identity("openverse:uuid", FILE_PAGE, "https://commons.wikimedia.org/?curid=123")
        self.assertEqual(canonical, "wikimedia:123")
        self.assertIn("openverse:uuid", aliases)
        self.assertIn(f"wikimedia:file:{FILE_TITLE}", aliases)

    def test_wp_html_and_plain_text_extract_normalized_identities(self) -> None:
        text = f'<a href="{FILE_PAGE}">Source</a>; https://commons.wikimedia.org/?curid=123&amp;uselang=en.'
        self.assertTrue({"wikimedia:123", f"wikimedia:file:{FILE_TITLE}"} <= commons_identities_from_text(text))
        self.assertEqual(commons_identities_from_text("wikimedia:123abc wikimedia:123-other"), set())
        self.assertEqual(commons_identities_from_text("https://evil.example/proxy?url=https://commons.wikimedia.org/?curid=123"), set())
        self.assertEqual(commons_identities_from_text("https://evil.example/?id=wikimedia:123"), set())

    def test_original_must_be_an_asset_and_source_must_be_a_file_page(self) -> None:
        raw = page()
        raw["imageinfo"][0]["url"] = FILE_PAGE
        self.assertIsNone(WikimediaProvider(enabled=False).normalize_candidate(raw, "bitcoin", 0))
        raw = page()
        raw["imageinfo"][0]["descriptionurl"] = ORIGINAL
        item = WikimediaProvider(enabled=False).normalize_candidate(raw, "bitcoin", 0)
        assert item is not None
        self.assertEqual(item.source_page_url, FILE_PAGE)

    def test_conflicting_verified_page_ids_and_titles_cannot_be_merged(self) -> None:
        for values in (("wikimedia:123", "https://commons.wikimedia.org/?curid=456"), (FILE_PAGE, FILE_PAGE.replace("Bitcoin", "Ethereum"))):
            self.assertTrue(commons_identity_conflict(*values))
            self.assertEqual(normalize_commons_identity(*values), ("", []))
        raw = page()
        raw["imageinfo"][0]["descriptionshorturl"] = "https://commons.wikimedia.org/?curid=456"
        self.assertIsNone(WikimediaProvider(enabled=False).normalize_candidate(raw, "bitcoin", 0))

    def test_openverse_wikimedia_page_id_and_file_aliases_are_shared(self) -> None:
        item = openverse_candidate()
        self.assertEqual(item.asset_key, "openverse:openverse-uuid")
        self.assertEqual(item.canonical_source, "wikimedia:123")
        self.assertIn(f"wikimedia:file:{FILE_TITLE}", item.identity_aliases)

    def test_openverse_conflicting_commons_page_ids_are_rejected(self) -> None:
        item = OpenverseProvider(enabled=False).normalize_candidate({
            "id": "openverse-uuid", "source": "wikimedia", "foreign_identifier": "123",
            "foreign_landing_url": "https://commons.wikimedia.org/?curid=456", "url": ORIGINAL,
            "creator": "Ada & Lin", "license": "by", "license_version": "4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/", "title": "Bitcoin ETF liquidity",
        }, "bitcoin etf liquidity", 0)
        self.assertIsNone(item)

    def test_same_page_id_retains_provider_variants_with_the_same_download_url(self) -> None:
        first, second = openverse_candidate(), candidate()
        loader = StaticDownloader(fail_first=True)
        result = search_images(ARTICLE, providers=[StaticProvider("openverse", [first]), StaticProvider("wikimedia", [second])], downloader=loader, local_usage=[], recent_wp_usage=[])
        self.assertIs(result.candidate, second)
        self.assertEqual(loader.calls, ["openverse:openverse-uuid", "wikimedia:123"])
        self.assertFalse(result.rejection_counts.get("duplicate_identity"))

    def test_file_url_variants_deduplicate_without_foreign_id(self) -> None:
        first = openverse_candidate(foreign_id="", landing="http://commons.m.wikimedia.org/wiki/File:Bitcoin%20ETF%20liquidity.jpg", original="https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Bitcoin_ETF_liquidity.jpg/320px-Bitcoin_ETF_liquidity.jpg")
        loader = StaticDownloader(fail_first=True)
        result = search_images(ARTICLE, providers=[StaticProvider("openverse", [first]), StaticProvider("wikimedia", [candidate()])], downloader=loader, local_usage=[], recent_wp_usage=[])
        self.assertEqual(result.candidate.asset_key if result.candidate else None, "wikimedia:123")
        self.assertEqual(len(loader.calls), 2)
        self.assertFalse(result.rejection_counts.get("duplicate_identity"))

    def test_repeated_wikimedia_results_across_queries_are_deduplicated(self) -> None:
        provider = WikimediaProvider(client=PayloadClient({"query": {"pages": [page()]}}))
        loader = StaticDownloader(fail_first=True)
        result = search_images(ARTICLE, providers=[provider], downloader=loader, local_usage=[], recent_wp_usage=[])
        self.assertIsNone(result.candidate)
        self.assertEqual(len(loader.calls), 1)
        self.assertGreaterEqual(result.rejection_counts.get("duplicate_identity", 0), 1)


class WikimediaReuseAndFallbackTests(unittest.TestCase):
    def test_old_openverse_local_history_blocks_direct_and_reverse(self) -> None:
        direct, indexed = candidate(), openverse_candidate()
        old_openverse = {"provider": "openverse", "asset_key": indexed.asset_key, "canonical_source": "wikimedia:123", "credit_url": "http://commons.wikimedia.org/?curid=123"}
        self.assertIs(candidate_recent_local_usage(direct, [old_openverse]), old_openverse)
        old_direct = {"provider": "wikimedia", "asset_key": direct.asset_key, "source_page_url": "http://commons.m.wikimedia.org/wiki/File:Bitcoin%20ETF%20liquidity.jpg"}
        self.assertIs(candidate_recent_local_usage(indexed, [old_direct]), old_direct)
        result = search([direct], local_usage=[old_openverse])
        self.assertIsNone(result.candidate)
        self.assertEqual(result.rejection_counts.get("local_history_identity_reuse"), 1)

    def test_wordpress_history_url_alias_blocks_candidate(self) -> None:
        row = {"post_excerpt": "Source: http://commons.wikimedia.org/?curid=123", "post_content": "", "guid": ""}
        self.assertIs(candidate_recent_wp_usage(candidate(), [row]), row)
        result = search([candidate()], wp_usage=[row])
        self.assertIsNone(result.candidate)
        self.assertEqual(result.rejection_counts.get("wordpress_history_reuse"), 1)

    def test_wordpress_matcher_does_not_match_numeric_prefix_or_unrelated_url(self) -> None:
        item = candidate()
        for blob in (
            "Credit wikimedia:1234", "Credit wikimedia:123abc", "Credit wikimedia:123-other",
            "https://evil.example/proxy?url=https://commons.wikimedia.org/?curid=123",
            f"https://evil.example/proxy?url={FILE_PAGE}",
            "https://evil.example/?id=wikimedia:123",
        ):
            with self.subTest(blob=blob):
                self.assertIsNone(candidate_recent_wp_usage(item, [{"post_excerpt": blob, "post_content": "", "guid": ""}]))

    def test_wordpress_matcher_retains_complete_key_and_file_url_aliases(self) -> None:
        item = candidate()
        for blob in (
            "Credit wikimedia:123.", '<a href="https://commons.wikimedia.org/?curid=123">Source</a>',
            '<a href="http://commons.m.wikimedia.org/wiki/File:Bitcoin%20ETF%20liquidity.jpg">Source</a>',
            f"Original: {ORIGINAL}",
        ):
            with self.subTest(blob=blob):
                row = {"post_excerpt": "", "post_content": blob, "guid": ""}
                self.assertIs(candidate_recent_wp_usage(item, [row]), row)

    def test_sha_and_difference_hash_history_block_wikimedia(self) -> None:
        image = downloaded()
        for entry, reason in (({"content_sha256": image.content_sha256}, "local_history_sha_reuse"), ({"content_sha256": "different", "perceptual_hash": "0123456789abcdee"}, "local_history_dhash_reuse")):
            with self.subTest(reason=reason):
                result = search([candidate()], local_usage=[entry], downloader=StaticDownloader(image))
                self.assertIsNone(result.candidate)
                self.assertEqual(result.rejection_counts.get(reason), 1)

    def test_failed_download_tries_next_distinct_wikimedia_candidate(self) -> None:
        first, second = candidate(), candidate(456)
        second.identity_aliases = ["wikimedia:456"]
        second.image_url = second.download_url = ORIGINAL.replace("liquidity", "liquidity_second")
        second.source_page_url = FILE_PAGE.replace("liquidity", "liquidity_second")
        second.url_hash = hashlib.sha256(second.usable_url.encode()).hexdigest()
        loader = StaticDownloader(fail_first=True)
        result = search([first, second], downloader=loader)
        self.assertIs(result.candidate, second)
        self.assertEqual(loader.calls, ["wikimedia:123", "wikimedia:456"])

    def test_provider_error_is_nonexhausted_and_generation_remains_blocked(self) -> None:
        import publishing.wordpress.publisher as publisher
        result = search_images(ARTICLE, providers=[StaticProvider("wikimedia", [], fail=True)], downloader=StaticDownloader(), local_usage=[], recent_wp_usage=[])
        self.assertFalse(result.all_available_providers_exhausted)
        self.assertIn("wikimedia", result.provider_failures)
        with patch.object(publisher, "IMAGE_SEARCH_ENGINE", "v2"), patch.object(publisher, "IMAGE_GENERATION_ONLY_AFTER_SEARCH_EXHAUSTED", True), patch.object(publisher, "IMAGE_GENERATION_ON_PROVIDER_ERROR", False):
            self.assertFalse(publisher._generation_allowed_after_search(result.all_available_providers_exhausted))

    def test_successful_wikimedia_candidate_prevents_generated_fallback(self) -> None:
        import publishing.wordpress.publisher as publisher
        selected = (b"prepared-jpeg", "wikimedia.jpg", {"provider": "wikimedia", "credit_text": candidate().attribution_text, "license_name": "cc-by", "attribution_text": candidate().attribution_text})
        with (
            patch.object(publisher, "IMAGE_SOURCE_MODE", "hybrid"), patch.object(publisher, "IMAGE_SOURCE_PRIORITY", "stock_first"),
            patch.object(publisher, "_search_stock_image_content", return_value=(selected, False)),
            patch.object(publisher, "_generated_image_content") as generate,
            patch.object(publisher, "upload_media", return_value=42),
            patch.object(publisher, "set_media_details", return_value=True) as metadata,
        ):
            media_id, meta = publisher.upload_image(None, ARTICLE["title"], "bitcoin", article=ARTICLE)
        self.assertEqual(media_id, 42)
        self.assertEqual(meta["provider"], "wikimedia")
        generate.assert_not_called()
        self.assertIn("Changes:", str(metadata.call_args))

    def test_wikimedia_usage_recording_roundtrip_preserves_aliases_and_license(self) -> None:
        item, image = candidate(), downloaded()
        meta = {
            "provider": "wikimedia", "asset_id": item.asset_id, "asset_key": item.asset_key,
            "canonical_source": item.canonical_source, "identity_aliases": item.identity_aliases,
            "source_page_url": item.source_page_url, "image_url": item.usable_url,
            "content_sha256": image.content_sha256, "perceptual_hash": image.perceptual_hash,
            "creator_name": item.creator_name, "license_name": item.license_name,
            "license_version": item.license_version, "license_url": item.license_url,
            "attribution_text": item.attribution_text, "query": item.query, "score": 0.9,
        }
        with patch("image_search.reuse.load_image_usage", return_value=[]), patch("image_search.reuse._write_image_usage") as write:
            record_image_usage(meta, 99, ARTICLE["title"])
        entry = write.call_args.args[0][0]
        for key in ("provider", "asset_id", "asset_key", "canonical_source", "identity_aliases", "content_sha256", "perceptual_hash", "creator_name", "license_name", "license_version", "license_url", "attribution_text", "query"):
            self.assertEqual(entry[key], meta[key], key)
        path = Mock()
        path.exists.return_value, path.read_text.return_value = True, json.dumps([entry])
        with patch("image_search.reuse._usage_path", return_value=path):
            loaded = load_image_usage()
        self.assertEqual(loaded[0]["identity_aliases"], item.identity_aliases)
        self.assertIsNotNone(candidate_recent_local_usage(openverse_candidate(), loaded))


if __name__ == "__main__":
    unittest.main()
