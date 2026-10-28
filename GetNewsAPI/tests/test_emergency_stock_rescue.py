"""Offline regression fixtures for the separately bounded safe-stock rescue."""
from __future__ import annotations

import hashlib
import unittest
from unittest.mock import Mock, patch

from image_search.downloader import ImageDownloadError, MAX_IMAGE_BYTES
from image_search.emergency import (
    CC_BY_CHANGE_NOTICE, MAX_REQUIRED_ATTRIBUTION_CHARS, _public_http_url,
    reject_rescue_candidate, rescue_images,
)
from image_search.models import DownloadedImage, ImageCandidate, RescueValidationResult
from image_search.provider import ImageSearchProvider, ProviderUnavailable
from image_search.selection import search_images


ARTICLE = {"id": 42, "title": "Bitcoin markets", "seo_focus": "bitcoin cryptocurrency"}


def candidate(provider="pexels", asset_id="one", *, score=.65, threshold=None, canonical=None):
    thresholds = {"pexels": .72, "pixabay": .78, "openverse": .70, "wikimedia": .70}
    if provider == "pexels":
        image_url = f"https://images.pexels.com/photos/{asset_id}/hero.jpg"
        source_page = f"https://www.pexels.com/photo/{asset_id}/"
        license_name, license_url = "pexels-license", "https://www.pexels.com/license/"
    elif provider == "pixabay":
        image_url = f"https://cdn.pixabay.com/{asset_id}.jpg"
        source_page = f"https://pixabay.com/photos/{asset_id}/"
        license_name, license_url = "pixabay-content-license", "https://pixabay.com/service/license-summary/"
    else:
        image_url = f"https://images.example/{asset_id}.jpg"
        source_page = f"https://source.example/{asset_id}"
        license_name, license_url = "cc-by", "https://creativecommons.org/licenses/by/4.0/"
    return ImageCandidate(
        provider=provider, asset_id=asset_id, image_url=image_url, source_page_url=source_page,
        canonical_source=canonical or f"{provider}:{asset_id}", creator_name="Fixture Creator",
        license_name=license_name, license_version="4.0", license_url=license_url,
        attribution_text=f"Fixture Creator. Source: {source_page}. License: {license_name} ({license_url}).",
        width=1600, height=900, query="bitcoin cryptocurrency", final_score=score,
        provider_threshold=threshold if threshold is not None else thresholds[provider],
        metadata={"attribution_complete": True, "metadata_text": "bitcoin cryptocurrency markets"},
    )


def image(seed=b"fixture-image", *, dhash=None, width=1600, height=900, mime="image/jpeg"):
    sha = hashlib.sha256(seed).hexdigest()
    return DownloadedImage(seed, mime, width, height, sha, dhash or sha[:16])


class StaticProvider(ImageSearchProvider):
    def __init__(self, name, items=(), *, failure=None):
        self.provider_name, self.enabled, self.candidate_threshold = name, True, 0.0
        self.items, self.failure, self.calls = list(items), failure, 0

    def search(self, query):
        self.calls += 1
        if self.failure:
            raise self.failure
        return self.items if self.calls == 1 else []

    def normalize_candidate(self, raw, query, provider_rank):
        raise NotImplementedError


class StaticDownloader:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.calls = []

    def download(self, item):
        self.calls.append((item.provider, item.usable_url))
        outcome = self.outcomes.get(item.usable_url, image(item.asset_key.encode()))
        if isinstance(outcome, Exception):
            raise outcome
        item.content_sha256, item.perceptual_hash = outcome.content_sha256, outcome.perceptual_hash
        return outcome


class EmergencyStockRescueTests(unittest.TestCase):
    def setUp(self):
        scorer = patch("image_search.selection.score_candidate", side_effect=lambda item, _article: item)
        scorer.start()
        self.addCleanup(scorer.stop)

    def search(self, providers, *, downloader=None, local=(), wp=(), validator=None, budget=50):
        return search_images(
            ARTICLE, providers=providers, downloader=downloader or StaticDownloader(),
            local_usage=list(local), recent_wp_usage=list(wp),
            post_download_validator=validator, global_candidate_limit=budget,
        )

    def test_observed_dev_provider_failure_rescues_highest_safe_pexels_without_searching_again(self):
        low, high = candidate(asset_id="low", score=.60), candidate(asset_id="high", score=.69)
        pixabay = [candidate("pixabay", str(index), score=.90 - index*.01) for index in range(2)]
        downloader = StaticDownloader({
            item.usable_url: ImageDownloadError("http_429", provider_unavailable=True) for item in pixabay
        })
        providers = [
            StaticProvider("pexels", [low, high]), StaticProvider("pixabay", pixabay),
            StaticProvider("openverse", failure=ProviderUnavailable("openverse", "edge_access_denied", status_code=403)),
            StaticProvider("wikimedia"),
        ]
        strict = self.search(providers, downloader=downloader)
        self.assertIsNone(strict.candidate)
        self.assertFalse(strict.all_available_providers_exhausted)
        self.assertFalse(strict.generation_allowed)
        before = [provider.calls for provider in providers]
        callback = Mock(return_value=True)
        rescued = rescue_images(strict, post_download_validator=callback)
        self.assertIs(rescued.candidate, high)
        self.assertEqual([provider.calls for provider in providers], before)
        self.assertEqual(downloader.calls, [(item.provider, item.usable_url) for item in (*pixabay, high)])
        self.assertEqual(rescued.emergency_summary["emergency_download_attempts"], 1)
        self.assertTrue(high.metadata["emergency_stock_rescue"])
        self.assertEqual(high.metadata["original_rejection"], "score_below_threshold")
        self.assertEqual(high.final_score, .69)
        self.assertEqual(high.provider_threshold, .72)
        callback.assert_called_once()

    def test_fresh_below_threshold_precedes_recent_above_threshold(self):
        fresh, recent = candidate(asset_id="fresh", score=.3), candidate(asset_id="recent", score=.99)
        strict = self.search([StaticProvider("pexels", [recent, fresh])], local=[{"asset_key": recent.asset_key}])
        result = rescue_images(strict)
        self.assertIs(result.candidate, fresh)
        self.assertEqual(fresh.metadata["emergency_rescue_tier"], 1)

    def test_recent_above_threshold_precedes_recent_below_threshold(self):
        above, below = candidate(asset_id="above", score=.80), candidate(asset_id="below", score=.69)
        strict = self.search([StaticProvider("pexels", [below, above])], local=[{"asset_key": item.asset_key} for item in (above, below)])
        result = rescue_images(strict)
        self.assertIs(result.candidate, above)
        self.assertEqual(above.metadata["emergency_rescue_tier"], 2)

    def test_recent_below_threshold_precedes_content_history_last_tier(self):
        recent = candidate(asset_id="recent-below", score=.69)
        content = candidate(asset_id="hash-reused", score=.99)
        downloaded = image(b"reused bytes")
        loader = StaticDownloader({content.usable_url: downloaded})
        strict = self.search([StaticProvider("pexels", [recent, content])], downloader=loader, local=[
            {"asset_key": recent.asset_key}, {"content_sha256": downloaded.content_sha256},
        ])
        result = rescue_images(strict)
        self.assertIs(result.candidate, recent)
        self.assertEqual(recent.metadata["emergency_rescue_tier"], 3)
        entry = next(entry for entry in strict.rescue_context.entries if entry.candidate is content)
        self.assertIn("local_history_sha_reuse", entry.soft_reasons)

    def test_sha_and_dhash_last_tier_reuses_validated_strict_bytes_without_new_download(self):
        for reason in ("local_history_sha_reuse", "local_history_dhash_reuse"):
            with self.subTest(reason=reason):
                item = candidate(score=.95)
                downloaded = image(dhash="0123456789abcdef")
                history = {"content_sha256": downloaded.content_sha256} if reason.endswith("sha_reuse") else {"perceptual_hash": "0123456789abcdee"}
                loader = StaticDownloader({item.usable_url: downloaded})
                strict = self.search([StaticProvider("pexels", [item])], downloader=loader, local=[history])
                self.assertIsNone(strict.candidate)
                callback = Mock(return_value=True)
                result = rescue_images(strict, post_download_validator=callback)
                self.assertIs(result.candidate, item)
                self.assertEqual(len(loader.calls), 1)
                self.assertEqual(result.emergency_summary["emergency_download_attempts"], 0)
                self.assertEqual(result.emergency_summary["emergency_attempts"], 1)
                self.assertEqual(item.metadata["emergency_rescue_tier"], 4)
                callback.assert_called_once_with(item, downloaded)

    def test_score_ranks_before_provider_then_provider_precedence_breaks_ties(self):
        for pixabay_score, expected in ((.70, "pixabay"), (.60, "pexels")):
            with self.subTest(score=pixabay_score):
                first = candidate("pexels", "p", score=.60)
                second = candidate("pixabay", "x", score=pixabay_score)
                strict = self.search([StaticProvider("pexels", [first]), StaticProvider("pixabay", [second])])
                self.assertEqual(rescue_images(strict).candidate.provider, expected)

    def test_deterministic_candidate_ties_ignore_input_order(self):
        for reverse in (False, True):
            items = [candidate(asset_id="a"), candidate(asset_id="b")]
            strict = self.search([StaticProvider("pexels", list(reversed(items)) if reverse else items)])
            self.assertEqual(rescue_images(strict).candidate.asset_id, "a")

    def test_below_threshold_candidate_is_fully_hard_validated_before_retention(self):
        cases = [
            ("license_name", "unknown"), ("image_url", "ftp://images.pexels.com/no.jpg"),
            ("image_url", "https://untrusted.example/no.jpg"), ("source_page_url", "http://127.0.0.1/page"),
            ("mime_type", "image/svg+xml"), ("width", 1199), ("height", 674),
            ("height", 2000), ("attribution_text", ""),
            ("attribution_text", "a" * (MAX_REQUIRED_ATTRIBUTION_CHARS + 1)),
        ]
        for field, value in cases:
            with self.subTest(field=field, value_length=len(value) if isinstance(value, str) else value):
                item = candidate(score=.2)
                setattr(item, field, value)
                if field == "image_url":
                    item.download_url = value
                strict = self.search([StaticProvider("pexels", [item])])
                self.assertEqual(strict.rescue_context.entries, [])
                self.assertIsNone(rescue_images(strict).candidate)
                self.assertEqual(strict.rescue_context.download_attempts, 0)

    def test_oversized_metadata_and_cc_by_notice_limit_remain_hard(self):
        oversized = candidate(score=.2)
        oversized.metadata["size"] = MAX_IMAGE_BYTES + 1
        by = candidate("openverse", score=.2)
        by.attribution_text = "a" * (MAX_REQUIRED_ATTRIBUTION_CHARS - len(CC_BY_CHANGE_NOTICE) + 1)
        for item in (oversized, by):
            strict = self.search([StaticProvider(item.provider, [item])])
            self.assertEqual(strict.rescue_context.entries, [])

    def test_content_history_evidence_propagates_from_hard_excluded_group_member(self):
        excluded = candidate(asset_id="overlong", score=.99, canonical="shared-asset")
        excluded.attribution_text = "a" * (MAX_REQUIRED_ATTRIBUTION_CHARS + 1)
        alternate = candidate(asset_id="alternate", score=.65, canonical="shared-asset")
        downloaded = image(b"known reused source")
        strict = self.search([StaticProvider("pexels", [excluded, alternate])],
                             downloader=StaticDownloader({excluded.usable_url: downloaded}),
                             local=[{"content_sha256": downloaded.content_sha256}])
        self.assertEqual(len(strict.rescue_context.entries), 1)
        entry = strict.rescue_context.entries[0]
        self.assertIs(entry.candidate, alternate)
        self.assertIn("local_history_sha_reuse", entry.soft_reasons)

    def test_incomplete_by_attribution_and_mismatched_license_url_remain_hard(self):
        for field, value in (("metadata", {"attribution_complete": False}), ("creator_name", "unknown"), ("license_url", "https://example.org/license")):
            item = candidate("openverse", score=.2)
            setattr(item, field, value)
            strict = self.search([StaticProvider("openverse", [item])])
            self.assertEqual(strict.rescue_context.entries, [])

    def test_untrusted_wikimedia_original_cannot_enter_rescue(self):
        item = candidate("wikimedia", score=.2)
        strict = self.search([StaticProvider("wikimedia", [item])])
        self.assertEqual(strict.rescue_context.entries, [])

    def test_public_url_guard_rejects_local_and_legacy_ip_forms_without_dns(self):
        for host in ("localhost", "localhost.", "example.local", "127.0.0.1", "127.1", "0177.0.0.1", "0x7f.0.0.1", "１２７.０.０.１", "169.254.169.254", "10.0.0.1", "[::1]"):
            with self.subTest(host=host):
                self.assertFalse(_public_http_url(f"https://{host}/image.jpg"))
        self.assertTrue(_public_http_url("https://images.example/image.jpg"))

    def test_failed_exact_url_is_not_retried_across_providers_but_other_url_can_be_rescued(self):
        failed = candidate("openverse", "failed", score=.95, canonical="shared")
        same_url = candidate("openverse", "same-url", score=.6, canonical="shared")
        same_url.image_url = same_url.download_url = failed.usable_url
        alternate = candidate("openverse", "alternate", score=.5, canonical="shared")
        loader = StaticDownloader({failed.usable_url: ImageDownloadError("http_429", provider_unavailable=True)})
        strict = self.search([StaticProvider("openverse", [failed, same_url, alternate])], downloader=loader)
        result = rescue_images(strict)
        self.assertIs(result.candidate, alternate)
        self.assertEqual([url for _provider, url in loader.calls], [failed.usable_url, alternate.usable_url])

    def test_strict_openverse_failure_bans_same_wikimedia_url_only_in_rescue(self):
        from tests.test_wikimedia_provider import candidate as wikimedia_candidate
        indexed = candidate("openverse", "strict-indexed", score=.95, canonical="wikimedia:123")
        direct = wikimedia_candidate()
        direct.final_score = .6
        indexed.image_url = indexed.download_url = direct.usable_url
        alternate = candidate("openverse", "different-delivery", score=.5, canonical="wikimedia:123")
        loader = StaticDownloader({indexed.usable_url: ImageDownloadError("http_429", provider_unavailable=True)})
        strict = self.search([
            StaticProvider("openverse", [indexed, alternate]), StaticProvider("wikimedia", [direct]),
        ], downloader=loader)
        result = rescue_images(strict)
        self.assertIs(result.candidate, alternate)
        self.assertEqual(loader.calls, [(indexed.provider, indexed.usable_url), (alternate.provider, alternate.usable_url)])
        direct_entry = next(entry for entry in strict.rescue_context.entries if entry.candidate is direct)
        self.assertEqual(direct_entry.hard_reason, "download_failed")

    def test_newly_discovered_sha_reuse_is_deferred_behind_fresh_below_candidate(self):
        reused, fresh = candidate(asset_id="newly-reused", score=.69), candidate(asset_id="fresh", score=.68)
        reused_image = image(b"recent actual content")
        loader = StaticDownloader({reused.usable_url: reused_image})
        strict = self.search([StaticProvider("pexels", [reused, fresh])], downloader=loader, local=[{"content_sha256": reused_image.content_sha256}])
        validated = []
        result = rescue_images(strict, post_download_validator=lambda item, _image: validated.append(item.asset_id) or True)
        self.assertIs(result.candidate, fresh)
        self.assertEqual(validated, ["fresh"])
        self.assertEqual(result.emergency_summary["emergency_download_attempts"], 2)
        entry = next(entry for entry in strict.rescue_context.entries if entry.candidate is reused)
        self.assertIn("local_history_sha_reuse", entry.soft_reasons)
        self.assertIs(entry.downloaded, reused_image)

    def test_newly_discovered_content_reuse_can_eventually_use_cached_tier_four(self):
        for history_field in ("content_sha256", "perceptual_hash"):
            with self.subTest(history_field=history_field):
                item, downloaded = candidate(), image(b"one reused image")
                loader = StaticDownloader({item.usable_url: downloaded})
                strict = self.search([StaticProvider("pexels", [item])], downloader=loader, local=[{history_field: getattr(downloaded, history_field)}])
                validator = Mock(return_value=True)
                result = rescue_images(strict, post_download_validator=validator)
                self.assertIs(result.candidate, item)
                self.assertEqual(item.metadata["emergency_rescue_tier"], 4)
                self.assertEqual(len(loader.calls), 1)
                self.assertEqual(result.emergency_summary["emergency_download_attempts"], 1)
                validator.assert_called_once_with(item, downloaded)

    def test_budget_ends_before_deferred_content_can_displace_untried_fresh_candidate(self):
        reused, fresh = candidate(asset_id="used", score=.69), candidate(asset_id="fresh", score=.68)
        reused_image = image(b"recent content")
        loader = StaticDownloader({reused.usable_url: reused_image})
        strict = self.search([StaticProvider("pexels", [reused, fresh])], downloader=loader, local=[{"content_sha256": reused_image.content_sha256}])
        validator = Mock(return_value=True)
        result = rescue_images(strict, post_download_validator=validator, limit=1)
        self.assertIsNone(result.candidate)
        self.assertEqual(len(loader.calls), 1)
        self.assertTrue(result.emergency_summary["emergency_completed"])
        validator.assert_not_called()

    def test_sole_newly_discovered_content_reuse_can_finish_at_limit_one(self):
        item, downloaded = candidate(), image(b"only recently used candidate")
        loader = StaticDownloader({item.usable_url: downloaded})
        strict = self.search([StaticProvider("pexels", [item])], downloader=loader, local=[{"content_sha256": downloaded.content_sha256}])
        result = rescue_images(strict, limit=1)
        self.assertIs(result.candidate, item)
        self.assertEqual(result.emergency_summary["emergency_attempts"], 1)
        self.assertEqual(result.emergency_summary["emergency_download_attempts"], 1)
        self.assertIn("local_history_sha_reuse", result.emergency_summary["emergency_original_rejection_reason"])
        self.assertEqual(item.metadata["emergency_rescue_tier"], 4)

    def test_rescue_download_and_conversion_failures_continue_with_separate_budget(self):
        items = [candidate(asset_id=str(index), score=.69-index*.01) for index in range(4)]
        loader = StaticDownloader({items[0].usable_url: ImageDownloadError("http_404"), items[1].usable_url: ImageDownloadError("timeout", provider_unavailable=True)})
        strict = self.search([StaticProvider("pexels", items)], downloader=loader)
        validator = Mock(side_effect=[False, True])
        result = rescue_images(strict, post_download_validator=validator, limit=4)
        self.assertIs(result.candidate, items[3])
        self.assertEqual(result.emergency_summary["emergency_download_attempts"], 4)
        self.assertEqual(result.emergency_summary["emergency_attempts"], 4)

    def test_rescue_budget_is_cumulative_and_does_not_search_again(self):
        items = [candidate(asset_id=str(index), score=.69-index*.01) for index in range(3)]
        loader = StaticDownloader({item.usable_url: ImageDownloadError("http_403") for item in items})
        provider = StaticProvider("pexels", items)
        strict = self.search([provider], downloader=loader)
        calls = provider.calls
        result = rescue_images(strict, limit=2)
        self.assertIsNone(result.candidate)
        self.assertTrue(result.emergency_summary["emergency_completed"])
        again = rescue_images(strict, limit=2)
        self.assertEqual(again.emergency_summary["emergency_download_attempts"], 2)
        self.assertEqual(provider.calls, calls)

    def test_attribution_failure_is_hard_and_next_candidate_is_tried(self):
        first, second = candidate(asset_id="first", score=.69), candidate(asset_id="second", score=.68)
        strict = self.search([StaticProvider("pexels", [first, second])])
        seen = []

        def validate(item, _downloaded):
            seen.append(item.metadata["emergency_stock_rescue"])
            return RescueValidationResult(False, "attribution_persistence_failed") if item is first else True

        result = rescue_images(strict, post_download_validator=validate)
        self.assertIs(result.candidate, second)
        self.assertEqual(seen, [True, True])
        self.assertEqual(strict.rescue_context.failed_transports[(first.provider, first.usable_url)], "attribution_persistence_failed")

    def test_typed_strict_metadata_failure_does_not_poison_other_group_content(self):
        first, second = candidate(asset_id="first", score=.99), candidate(asset_id="second", score=.95)
        shared = image(b"same image, different legal metadata")
        loader = StaticDownloader({item.usable_url: shared for item in (first, second)})
        validator = Mock(side_effect=[RescueValidationResult(False, "attribution_persistence_failed"), True])
        result = self.search([StaticProvider("pexels", [first, second])], downloader=loader, validator=validator)
        self.assertIs(result.candidate, second)
        self.assertEqual(validator.call_count, 2)
        self.assertEqual(result.rejection_counts["attribution_persistence_failed"], 1)

    def test_failed_strict_conversion_url_never_returns_via_rescue(self):
        strict_item = candidate(asset_id="strict", score=.99)
        same = candidate(asset_id="same", score=.5)
        same.image_url = same.download_url = strict_item.usable_url
        other = candidate(asset_id="other", score=.4)
        loader = StaticDownloader()
        strict = self.search([StaticProvider("pexels", [strict_item, same, other])], downloader=loader, validator=lambda *_args: False)
        result = rescue_images(strict)
        self.assertIs(result.candidate, other)
        self.assertEqual(loader.calls.count((strict_item.provider, strict_item.usable_url)), 1)

    def test_rescue_can_resume_after_explicit_attribution_failure(self):
        first, second = candidate(asset_id="first", score=.69), candidate(asset_id="second", score=.68)
        strict = self.search([StaticProvider("pexels", [first, second])])
        first_result = rescue_images(strict)
        self.assertIs(first_result.candidate, first)
        reject_rescue_candidate(first_result, first, "attribution_persistence_failed")
        second_result = rescue_images(first_result)
        self.assertIs(second_result.candidate, second)
        self.assertEqual(second_result.emergency_summary["emergency_attempts"], 2)

    def test_rejected_selection_followed_by_exhaustion_has_no_stale_selected_summary(self):
        first, second = candidate(asset_id="first", score=.69), candidate(asset_id="second", score=.68)
        strict = self.search([StaticProvider("pexels", [first, second])])
        selected = rescue_images(strict)
        reject_rescue_candidate(selected, first, "attribution_persistence_failed")
        result = rescue_images(selected, post_download_validator=lambda *_args: False)
        self.assertIsNone(result.candidate)
        self.assertFalse(result.emergency_summary["emergency_selected"])
        self.assertEqual(result.emergency_summary["emergency_selected_provider"], "")
        self.assertEqual(result.emergency_summary["emergency_original_rejection_reason"], "")

    def test_unknown_validation_error_is_sanitized_and_hard(self):
        item = candidate()
        strict = self.search([StaticProvider("pexels", [item])])
        with self.assertLogs("image_search.emergency", level="INFO") as logs:
            result = rescue_images(strict, post_download_validator=lambda *_args: RescueValidationResult(False, "private-token-value"))
        self.assertIsNone(result.candidate)
        self.assertNotIn("private-token-value", " ".join(logs.output))
        self.assertEqual(next(iter(strict.rescue_context.failed_transports.values())), "validation_failed")

    def test_cached_or_downloaded_unsafe_dimensions_never_pass_conversion(self):
        item = candidate()
        loader = StaticDownloader({item.usable_url: image(width=100)})
        strict = self.search([StaticProvider("pexels", [item])], downloader=loader)
        validator = Mock(return_value=True)
        self.assertIsNone(rescue_images(strict, post_download_validator=validator).candidate)
        validator.assert_not_called()

    def test_successful_strict_selection_is_unchanged_and_rescue_not_invoked(self):
        selected, lower = candidate(score=.99), candidate(asset_id="lower")
        loader = StaticDownloader()
        strict = self.search([StaticProvider("pexels", [selected, lower])], downloader=loader)
        self.assertIs(strict.candidate, selected)
        result = rescue_images(strict)
        self.assertIs(result, strict)
        self.assertEqual(loader.calls, [(selected.provider, selected.usable_url)])
        self.assertNotIn("emergency_stock_rescue", selected.metadata)

    def test_strict_budget_zero_rescues_fresh_above_threshold_as_explicit_tier_zero(self):
        item = candidate(score=.99)
        loader = StaticDownloader()
        strict = self.search([StaticProvider("pexels", [item])], downloader=loader, budget=0)
        self.assertIsNone(strict.candidate)
        self.assertTrue(strict.search_budget_exhausted)
        self.assertEqual(strict.rescue_context.strict_attempted_transports, set())
        self.assertEqual(loader.calls, [])
        result = rescue_images(strict)
        self.assertIs(result.candidate, item)
        self.assertEqual(item.metadata["emergency_rescue_tier"], 0)
        self.assertEqual(item.metadata["emergency_original_rejection_reason"], "strict_unattempted")
        self.assertEqual(item.metadata["original_rejection"], "strict_unattempted")
        self.assertEqual(result.emergency_summary["emergency_candidates_available"], 1)
        self.assertEqual(result.emergency_summary["emergency_download_attempts"], 1)
        self.assertEqual(strict.rescue_context.strict_attempted_transports, set())

    def test_strict_budget_consumed_before_later_fresh_above_threshold_candidate(self):
        first, later = candidate(asset_id="first", score=.99), candidate(asset_id="later", score=.95)
        loader = StaticDownloader({first.usable_url: ImageDownloadError("http_429", provider_unavailable=True)})
        strict = self.search([StaticProvider("pexels", [first, later])], downloader=loader, budget=1)
        self.assertEqual(strict.rescue_context.strict_attempted_transports, {(first.provider, first.usable_url)})
        self.assertTrue(strict.search_budget_exhausted)
        result = rescue_images(strict)
        self.assertIs(result.candidate, later)
        self.assertEqual(later.metadata["emergency_rescue_tier"], 0)
        self.assertEqual(loader.calls, [(first.provider, first.usable_url), (later.provider, later.usable_url)])

    def test_tier_zero_score_then_configured_provider_precedence(self):
        for pixabay_score, order, expected in (
            (.99, ("pexels", "pixabay"), "pixabay"),
            (.90, ("pexels", "pixabay"), "pexels"),
            (.90, ("pixabay", "pexels"), "pixabay"),
        ):
            with self.subTest(score=pixabay_score, order=order):
                items = {"pexels": candidate("pexels", "p", score=.90), "pixabay": candidate("pixabay", "x", score=pixabay_score)}
                strict = self.search([StaticProvider(name, [items[name]]) for name in order], budget=0)
                rescued = rescue_images(strict)
                self.assertEqual(rescued.candidate.provider, expected)
                self.assertEqual(rescued.candidate.metadata["emergency_rescue_tier"], 0)

    def test_tier_zero_ties_are_deterministic_under_reversed_collection_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                items = [candidate(asset_id="a", score=.99), candidate(asset_id="b", score=.99)]
                strict = self.search([StaticProvider("pexels", list(reversed(items)) if reverse else items)], budget=0)
                self.assertEqual(rescue_images(strict).candidate.asset_id, "a")

    def test_tiers_zero_through_four_are_validated_in_order(self):
        zero = candidate(asset_id="zero", score=.80)
        one = candidate(asset_id="one", score=.69)
        two = candidate(asset_id="two", score=.95)
        three = candidate(asset_id="three", score=.68)
        four = candidate(asset_id="four", score=.99)
        content = image(b"recent content for final tier")
        loader = StaticDownloader({four.usable_url: content})
        strict = self.search([StaticProvider("pexels", [four, three, two, one, zero])], downloader=loader, budget=0,
                             local=[{"asset_key": two.asset_key}, {"asset_key": three.asset_key}, {"content_sha256": content.content_sha256}])
        validated = []

        def validate(item, _downloaded):
            validated.append((item.asset_id, item.metadata["emergency_rescue_tier"]))
            if item is zero:
                self.assertEqual(item.metadata["original_rejection"], "strict_unattempted")
            return item is four

        result = rescue_images(strict, post_download_validator=validate)
        self.assertIs(result.candidate, four)
        self.assertEqual(validated, [("zero", 0), ("one", 1), ("two", 2), ("three", 3), ("four", 4)])
        self.assertEqual(loader.calls.count((four.provider, four.usable_url)), 1)

    def test_explicitly_attempted_strict_candidate_cannot_return_as_tier_zero(self):
        item = candidate(score=.99)
        loader = StaticDownloader()
        strict = self.search([StaticProvider("pexels", [item])], downloader=loader)
        self.assertIs(strict.candidate, item)
        self.assertEqual(strict.rescue_context.strict_attempted_transports, {(item.provider, item.usable_url)})
        # Even if a caller loses the selected result without recording a soft or
        # hard rejection, explicit attempt evidence prevents reconsideration.
        strict.candidate, strict.downloaded = None, None
        result = rescue_images(strict)
        self.assertIsNone(result.candidate)
        self.assertEqual(loader.calls, [(item.provider, item.usable_url)])

    def test_failed_strict_transport_cannot_return_as_tier_zero(self):
        item = candidate(score=.99)
        loader = StaticDownloader({item.usable_url: ImageDownloadError("http_404")})
        strict = self.search([StaticProvider("pexels", [item])], downloader=loader)
        self.assertIn((item.provider, item.usable_url), strict.rescue_context.strict_attempted_transports)
        self.assertIsNone(rescue_images(strict).candidate)
        self.assertEqual(loader.calls, [(item.provider, item.usable_url)])

    def test_failed_url_cross_provider_is_blocked_but_different_url_same_identity_is_tier_zero(self):
        from tests.test_wikimedia_provider import candidate as wikimedia_candidate
        indexed = candidate("openverse", "strict-indexed", score=.99, canonical="wikimedia:123")
        direct = wikimedia_candidate()
        direct.final_score = .99
        indexed.image_url = indexed.download_url = direct.usable_url
        alternate = candidate("openverse", "alternate", score=.95, canonical="wikimedia:123")
        loader = StaticDownloader({indexed.usable_url: ImageDownloadError("http_429", provider_unavailable=True)})
        strict = self.search([StaticProvider("openverse", [indexed, alternate]), StaticProvider("wikimedia", [direct])], downloader=loader, budget=1)
        self.assertNotIn((direct.provider, direct.usable_url), strict.rescue_context.strict_attempted_transports)
        self.assertNotIn((alternate.provider, alternate.usable_url), strict.rescue_context.strict_attempted_transports)
        rescued = rescue_images(strict)
        self.assertIs(rescued.candidate, alternate)
        self.assertEqual(alternate.metadata["emergency_rescue_tier"], 0)
        self.assertEqual(loader.calls, [(indexed.provider, indexed.usable_url), (alternate.provider, alternate.usable_url)])

    def test_hard_invalid_fresh_above_threshold_candidate_never_becomes_tier_zero(self):
        for field, value in (
            ("license_name", "unknown"), ("source_page_url", "http://127.0.0.1/"),
            ("mime_type", "image/svg+xml"), ("width", 100), ("height", 2000),
            ("attribution_text", ""), ("attribution_text", "a"*(MAX_REQUIRED_ATTRIBUTION_CHARS+1)),
        ):
            with self.subTest(field=field, value_length=len(value) if isinstance(value, str) else value):
                item = candidate(score=.99)
                setattr(item, field, value)
                strict = self.search([StaticProvider("pexels", [item])], budget=0)
                self.assertEqual(strict.rescue_context.entries, [])
                self.assertEqual(strict.rescue_context.strict_attempted_transports, set())
                self.assertIsNone(rescue_images(strict).candidate)

    def test_disabled_rescue_finishes_without_downloading(self):
        strict = self.search([StaticProvider("pexels", [candidate()])])
        result = rescue_images(strict, enabled=False)
        self.assertFalse(result.emergency_summary["emergency_stock_rescue_attempted"])
        self.assertTrue(result.emergency_summary["emergency_completed"])
        self.assertEqual(result.emergency_summary["emergency_download_attempts"], 0)


if __name__ == "__main__":
    unittest.main()
