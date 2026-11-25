"""Offline Cloud Vision fixtures with generated pixels and mocked responses."""
from __future__ import annotations

import base64
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack, closing, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw
import requests

from image_search import emergency, selection, vision
from image_search.models import DownloadedImage
from image_search.subject_policy import article_correlation_id
from image_search.vision_policy import build_vision_conflict_rule, classify_vision_topic
from publishing import PublicationArticle, PublicationContext, PublicationImage
from publishing.wordpress import publisher
import vision_label_probe

from . import test_featured_image_rescue as rescue_fixtures
from . import test_public_image_attribution as attribution_fixtures


API_KEY = "unit-test-vision-key"
COLD_CARD = {
    **rescue_fixtures.ARTICLE,
    "title": "Coinkite Details Coldcard Seed Flaw Missed in Reviews",
    "full_text": "The hardware wallet firmware had a flaw in the Bitcoin recovery seed generator.",
}
FINANCIAL_RECORD = {
    **rescue_fixtures.ARTICLE,
    "title": "BlackRock Bitcoin ETF records largest outflow",
    "full_text": "The fund reported a record outflow in one session.",
}
DANDELION = ("Dandelion", 0.96)
USB_DEVICE = ("USB flash drive", 0.95)
TURNTABLE = ("Turntable", 0.97)


def fixture_jpeg(color: str) -> bytes:
    # The red edges are absent only if the production hero crop is applied.
    image = Image.new("RGB", (1800, 800), "red")
    ImageDraw.Draw(image).rectangle((240, 0, 1559, 799), fill=color)
    out = io.BytesIO()
    image.save(out, "JPEG")
    return out.getvalue()


def google_response(*labels, status=200, payload=None):
    if payload is None:
        payload = {"responses": [{"labelAnnotations": [
            {"description": description, "score": score} for description, score in labels
        ]}]}
    response = Mock(status_code=status)
    response.json.return_value = payload
    return response


class FixtureDownloads:
    def __init__(self, images):
        self.images = {candidate.usable_url: fixture_jpeg(color) for candidate, color in images}
        self.calls = []

    def download(self, candidate):
        self.calls.append(candidate.usable_url)
        content = self.images[candidate.usable_url]
        digest = hashlib.sha256(content).hexdigest()
        candidate.content_sha256, candidate.perceptual_hash = digest, digest[:16]
        return DownloadedImage(content, "image/jpeg", 1800, 800, digest, digest[:16])


class VisionPublishingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_dir = Path(temporary.name)

    def publish_fixture(self, images, *, state_dir=None, article=COLD_CARD, mode="enforce", subject_mode="shadow",
                        responses=(), generate=False, priority="stock_first", engine="v2", source=None, legacy=None):
        images = list(images)
        state_dir = state_dir or self.state_dir
        groups = {}
        for candidate, _color in images:
            groups.setdefault(candidate.provider, []).append(candidate)
        providers = [rescue_fixtures.FixtureProvider(name, items) for name, items in groups.items()]
        if not providers:
            providers = [rescue_fixtures.FixtureProvider("pexels")]
        downloader = FixtureDownloads(images)
        events = []
        available_google_responses = iter(responses)

        def stock_search(article, **kwargs):
            return selection.search_images(article, providers=providers, downloader=downloader,
                                           local_usage=[], recent_wp_usage=[], **kwargs)

        def fixture_score(candidate, _article):
            candidate.final_score = candidate.metadata["fixture_score"]
            return candidate

        def google_post(*args, **kwargs):
            events.append("vision")
            response = next(available_google_responses)
            if isinstance(response, Exception):
                raise response
            return response

        def wordpress_post(url, **kwargs):
            endpoint = url.rpartition("/wp-json/wp/v2/")[2]
            events.append(endpoint)
            response = Mock(status_code=201)
            response.json.return_value = {"id": 801 if endpoint == "posts" else 701}
            return response

        generated = (fixture_jpeg("blue"), "fixture.jpg", {"provider": "grok"}) if generate else None
        adapter = publisher.WordPressPublisher()
        with ExitStack() as stack:
            for name, value in {
                "IMAGE_SEARCH_ENGINE": engine, "IMAGE_SOURCE_MODE": "hybrid", "IMAGE_SOURCE_PRIORITY": priority,
                "USE_SOURCE_IMAGES": source is not None, "PRIMARY_IMAGE_PROVIDER": "grok", "IMAGE_FALLBACK_PROVIDER": "openai",
                "OPENAI_IMAGE_FALLBACK": True, "IMAGE_GENERATION_ONLY_AFTER_SEARCH_EXHAUSTED": True,
                "IMAGE_GENERATION_ON_PROVIDER_ERROR": False, "IMAGE_EMERGENCY_STOCK_RESCUE_ENABLED": True,
                "IMAGE_EMERGENCY_STOCK_RESCUE_LIMIT": 10, "IMAGE_REQUIRE_FEATURED_IMAGE": True,
                "STOCK_IMAGE_VISION_MODE": mode, "STOCK_IMAGE_VISION_MONTHLY_UNIT_LIMIT": 100,
                "GOOGLE_CLOUD_VISION_API_KEY": API_KEY, "WRITABLE_STATE_DIR": state_dir,
            }.items():
                stack.enter_context(patch.object(publisher, name, value))
            stack.enter_context(patch.object(selection, "STOCK_IMAGE_SUBJECT_MODE", subject_mode))
            stack.enter_context(patch.object(emergency, "STOCK_IMAGE_SUBJECT_MODE", subject_mode))
            stack.enter_context(patch.object(selection, "score_candidate", side_effect=fixture_score))
            stack.enter_context(patch.object(publisher, "search_images", side_effect=stock_search))
            stack.enter_context(patch.object(publisher, "_maybe_source_image_content", return_value=source))
            stack.enter_context(patch.object(publisher, "_stock_image_content_v1", return_value=legacy))
            generation = stack.enter_context(patch.object(publisher, "_generated_image_content", return_value=generated))
            http = stack.enter_context(patch.object(vision.requests, "post", side_effect=google_post))
            wp = stack.enter_context(patch.object(publisher.session, "post", side_effect=wordpress_post))
            stack.enter_context(patch.object(adapter, "_validate_yoast"))
            stack.enter_context(patch.object(adapter, "reconcile", return_value=None))
            stack.enter_context(patch.object(adapter, "_write_yoast"))
            stack.enter_context(patch.object(publisher, "ensure_category", return_value=1))
            stack.enter_context(patch.object(publisher, "ensure_term", return_value=2))
            usage = stack.enter_context(patch.object(publisher, "record_image_usage"))
            stack.enter_context(patch("builtins.print"))
            media_id, metadata = publisher.upload_image(None, article["title"], article["hashtags"], article=article)
            image = PublicationImage(media_id, metadata) if media_id else None
            result = adapter.publish(PublicationArticle.from_mapping(article), image, PublicationContext(
                datetime(2026, 11, 25, tzinfo=timezone.utc), creation_is_fresh=True,
            ))
        self.assertTrue(result.success)
        self.assertEqual(wp.call_args.kwargs["json"].get("featured_media"), media_id)
        if media_id:
            usage.assert_called_once_with(metadata, 801, article["title"])
        else:
            usage.assert_not_called()
        return metadata, http, wp, generation, downloader, events

    def test_strict_empty_unrelated_and_disabled_paths_spend_nothing(self):
        for case, images, options, expected_provider in (
            ("strict winner", [(rescue_fixtures.stock("pexels", "1", 0.95), "green")], {}, "pexels"),
            ("empty rescue", [], {"generate": True}, "grok"),
            ("unrelated", [(rescue_fixtures.stock("pexels", "2", 0.70), "green")],
             {"article": rescue_fixtures.ARTICLE}, "pexels"),
            ("disabled", [(rescue_fixtures.stock("pexels", "3", 0.70), "green")], {"mode": "off"}, "pexels"),
            ("mixed subject", [(rescue_fixtures.stock("pexels", "4", 0.70), "green")],
             {"article": {**COLD_CARD, "title": "Museum compares Coldcard seed backups and dandelion seeds"}}, "pexels"),
        ):
            with self.subTest(case=case):
                metadata, http, _, generation, _, events = self.publish_fixture(images, **options)
                http.assert_not_called()
                self.assertEqual(list(self.state_dir.iterdir()), [])
                self.assertEqual(metadata["provider"], expected_provider)
                self.assertEqual(events, ["media", "media/701", "posts"])
                if case == "empty rescue":
                    generation.assert_called_once()
                    self.assertTrue(metadata["emergency_completed"])
                else:
                    generation.assert_not_called()

    def test_commons_public_attribution_preflight_prevents_calls_and_upload(self):
        item = attribution_fixtures.wikimedia(notice="Please credit via https://credit.example/path.")
        item.metadata["fixture_score"] = item.provider_threshold - 0.01
        with self.assertLogs(level="INFO") as logs:
            metadata, http, _, generation, downloader, events = self.publish_fixture([(item, "green")], generate=True)
        self.assertIn(item.usable_url, downloader.calls)
        self.assertTrue(any("attribution_missing" in line for line in logs.output))
        http.assert_not_called()
        generation.assert_called_once()
        self.assertEqual(metadata["provider"], "grok")
        self.assertEqual(events, ["media", "media/701", "posts"])
        self.assertEqual(list(self.state_dir.iterdir()), [])

    def test_source_v1_and_generated_only_paths_spend_nothing(self):
        for provider, options in (
            ("source", {"priority": "source_first", "source": (fixture_jpeg("blue"), "source.jpg", {"provider": "source"})}),
            ("pexels", {"engine": "v1", "legacy": (fixture_jpeg("blue"), "stock.jpg",
                                                  {"provider": "pexels", "credit_text": "Fixture creator"})}),
            ("grok", {"priority": "grok_only", "generate": True}),
        ):
            with self.subTest(provider=provider):
                metadata, http, _, _, _, _ = self.publish_fixture([], **options)
                self.assertEqual(metadata["provider"], provider)
                http.assert_not_called()
                self.assertEqual(list(self.state_dir.iterdir()), [])

    def test_shadow_uses_prepared_crop_and_preserves_publication_and_credit(self):
        def images():
            return [(rescue_fixtures.stock("pixabay", "1346727", 0.76), "green"),
                    (rescue_fixtures.stock("pexels", "3631991", 0.70), "blue")]

        baseline, _, wp_baseline, _, _, _ = self.publish_fixture(images(), mode="off")
        with self.assertLogs("image_search.vision", level="INFO") as logs:
            shadow, http, wp_shadow, generation, _, events = self.publish_fixture(
                images(), mode="shadow", responses=[google_response(DANDELION)],
            )
        self.assertEqual(baseline, shadow)
        self.assertEqual(wp_baseline.call_args_list, wp_shadow.call_args_list)
        self.assertEqual(wp_shadow.call_args_list[1].kwargs["json"]["caption"], images()[0][0].attribution_text)
        self.assertEqual(events, ["vision", "media", "media/701", "posts"])
        generation.assert_not_called()
        http.assert_called_once()
        image = http.call_args.kwargs["json"]["requests"][0]["image"]
        with Image.open(io.BytesIO(base64.b64decode(image["content"]))) as preview:
            self.assertEqual(preview.size, (768, 512))
            red, green, blue = preview.getpixel((0, 256))
            self.assertGreater(green, red + 70)
            self.assertGreater(green, blue + 70)
        self.assertEqual(len(logs.output), 1)
        for field in (f"article={article_correlation_id(COLD_CARD)}", "provider=pixabay", "asset_id=1346727",
                      "mode=shadow", "decision=would_reject", "status=submitted", "units_reserved=1", "latency_ms="):
            self.assertIn(field, logs.output[0])

    def test_enforce_blocks_an_alternate_and_identical_crop_before_the_second_upload(self):
        first = rescue_fixtures.stock("pixabay", "1346727", 0.77)
        alternate = rescue_fixtures.stock("pixabay", "1346727", 0.76, variant="-alternate")
        duplicate = rescue_fixtures.stock("pixabay", "400", 0.75)
        control = rescue_fixtures.stock("pexels", "3631991", 0.70)
        with self.assertLogs(level="INFO") as logs:
            metadata, http, wp, generation, downloader, events = self.publish_fixture(
                [(first, "green"), (alternate, "blue"), (duplicate, "green"), (control, "orange")],
                responses=[google_response(DANDELION), google_response(USB_DEVICE)],
            )
        self.assertNotIn(alternate.usable_url, downloader.calls)
        self.assertEqual(downloader.calls, [first.usable_url, duplicate.usable_url, control.usable_url])
        self.assertEqual(http.call_count, 2)
        generation.assert_not_called()
        self.assertEqual(metadata["asset_key"], control.asset_key)
        self.assertEqual(metadata["credit_text"], control.attribution_text)
        self.assertEqual(wp.call_args_list[1].kwargs["json"]["caption"], control.attribution_text)
        self.assertEqual(events, ["vision", "vision", "media", "media/701", "posts"])
        self.assertTrue(any("decision=reject" in line and "asset_id=1346727" in line for line in logs.output))

    def test_enforce_all_rejected_runs_final_generation_after_two_submissions(self):
        first = rescue_fixtures.stock("pixabay", "1346727", 0.77)
        second = rescue_fixtures.stock("pixabay", "1514032", 0.76)
        metadata, http, _, generation, _, events = self.publish_fixture(
            [(first, "green"), (second, "orange")], article={**COLD_CARD, "title": "Coldcard seed flaw postmortem"},
            responses=[google_response(DANDELION), google_response(DANDELION)], generate=True,
        )
        self.assertEqual(metadata["provider"], "grok")
        self.assertTrue(metadata["emergency_completed"])
        self.assertTrue(metadata["final_generation_rescue_allowed"])
        self.assertEqual(http.call_count, 2)
        self.assertEqual(generation.call_args.args[0], "grok")
        self.assertEqual(events, ["vision", "vision", "media", "media/701", "posts"])

    def test_vision_errors_keep_the_existing_stock_selection_and_credit(self):
        errors = (
            google_response(status=403), google_response(status=429), requests.Timeout(),
            google_response(payload={"responses": [{"error": {"code": 7}}]}),
        )
        for index, error in enumerate(errors):
            with self.subTest(error=index):
                item = rescue_fixtures.stock("pexels", str(index + 1), 0.70)
                state = self.state_dir / str(index)
                state.mkdir()
                metadata, http, wp, generation, _, events = self.publish_fixture(
                    [(item, "green")], state_dir=state, responses=[error],
                )
                http.assert_called_once()
                generation.assert_not_called()
                self.assertEqual(metadata["asset_id"], item.asset_id)
                self.assertEqual(wp.call_args_list[1].kwargs["json"]["caption"], item.attribution_text)
                self.assertEqual(events, ["vision", "media", "media/701", "posts"])

    def test_per_article_cap_preserves_selection_of_third_uncertain_candidate(self):
        images = [(rescue_fixtures.stock("pixabay", asset, score), color) for asset, score, color in (
            ("1", 0.77, "green"), ("2", 0.76, "orange"), ("3", 0.75, "blue"),
        )]
        with self.assertLogs("image_search.vision", level="INFO") as logs:
            metadata, http, _, generation, _, events = self.publish_fixture(
                images, responses=[google_response(DANDELION), google_response(DANDELION)],
            )
        self.assertEqual(metadata["asset_id"], "3")
        self.assertEqual(http.call_count, 2)
        generation.assert_not_called()
        self.assertEqual(events, ["vision", "vision", "media", "media/701", "posts"])
        self.assertIn("reason=article_limit", "\n".join(logs.output))

    def test_unexpected_optional_check_failure_preserves_shadow_selection(self):
        item = rescue_fixtures.stock("pexels", "3", 0.70)
        with (patch.object(vision.VisionCheck, "check", side_effect=RuntimeError("private details")),
              self.assertLogs("image_search.vision", level="INFO") as logs):
            metadata, http, _, generation, _, events = self.publish_fixture([(item, "green")], mode="shadow")
        http.assert_not_called()
        generation.assert_not_called()
        self.assertEqual(metadata["asset_id"], item.asset_id)
        self.assertEqual(events, ["media", "media/701", "posts"])
        self.assertIn("decision=abstain reason=unexpected_error", logs.output[0])
        self.assertNotIn("private details", logs.output[0])

    def test_financial_conflict_and_subject_policy_are_independent(self):
        def images():
            player = rescue_fixtures.stock("pixabay", "90", 0.76)
            player.subject_text = "Vinyl on a record player"
            chart = rescue_fixtures.stock("pexels", "91", 0.70)
            return [(player, "green"), (chart, "blue")]

        for subject_mode, vision_mode, responses, provider, call_count in (
            ("shadow", "off", (), "pixabay", 0),
            ("enforce", "off", (), "pexels", 0),
            ("shadow", "enforce", [google_response(TURNTABLE), google_response(("Market chart", 0.97))], "pexels", 2),
        ):
            with self.subTest(subject=subject_mode, vision=vision_mode):
                metadata, http, _, generation, _, _ = self.publish_fixture(
                    images(), article=FINANCIAL_RECORD, subject_mode=subject_mode, mode=vision_mode, responses=responses,
                )
                self.assertEqual(metadata["provider"], provider)
                self.assertEqual(http.call_count, call_count)
                generation.assert_not_called()


class VisionPolicyTests(unittest.TestCase):
    def test_headline_and_lead_distinguish_the_supported_senses(self):
        for article, topic in (
            (COLD_CARD, "hardware_wallet"),
            (FINANCIAL_RECORD, "financial_record"),
            ({"title": "Analysts discuss the flaw", "lead": "The hardware wallet firmware exposed its recovery seed."}, "hardware_wallet"),
            ({"title": "Coldcard seed backup and dandelion seeds in a new museum exhibit",
              "lead": "The exhibit compares the hardware wallet's recovery words with botanical seeds."}, "mixed_topic"),
            ({"title": "Bitcoin ETF sets a record as vinyl record sales rise", "lead": "The report covers both fund flows and turntable sales."}, "mixed_topic"),
            ({"title": "Local records office seeds its archive"}, None),
        ):
            with self.subTest(title=article["title"]):
                self.assertEqual(classify_vision_topic(article), topic)

    def test_rejects_only_specific_strong_conflicts_without_on_topic_evidence(self):
        hardware = build_vision_conflict_rule("hardware_wallet")
        financial = build_vision_conflict_rule("financial_record")
        self.assertTrue(hardware((("dandelion", 0.90),)))
        self.assertTrue(financial((("turntable", 0.97),)))
        for rule, labels in (
            (hardware, (("dandelion", 0.899),)),
            (hardware, (("dandelion", 0.97), ("usb flash drive", 0.95))),
            (hardware, (("usb flash drive", 0.95),)),
            (hardware, (("green", 0.99), ("plant", 0.99), ("seed", 0.99), ("technology", 0.99))),
            (financial, (("turntable", 0.97), ("market chart", 0.95))),
            (financial, (("record", 0.99), ("money", 0.99), ("coin", 0.99))),
        ):
            with self.subTest(labels=labels):
                self.assertFalse(rule(labels))


class VisionClientTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_dir = Path(temporary.name)
        self.image = fixture_jpeg("green")

    def check(self, *, mode="enforce", limit=100, key=API_KEY, rule=None, state_dir=None, article_id=""):
        return vision.VisionCheck(
            mode=mode, api_key=key, monthly_limit=limit, state_dir=state_dir or self.state_dir,
            conflict_rule=rule or build_vision_conflict_rule("hardware_wallet"),
            article_id=article_id,
        )

    def test_single_request_sends_a_bounded_jpeg_with_no_source_metadata(self):
        exif = Image.Exif()
        exif[270] = "private-image-description"
        raw = io.BytesIO()
        with Image.open(io.BytesIO(self.image)) as source:
            source.save(raw, "JPEG", exif=exif, comment=b"private-image-comment")
        with patch.object(vision.requests, "post", return_value=google_response(DANDELION)) as http:
            result = self.check().check(raw.getvalue())

        http.assert_called_once()
        self.assertEqual(http.call_args.args, ("https://vision.googleapis.com/v1/images:annotate",))
        options = http.call_args.kwargs
        self.assertEqual(options["headers"].get("X-Goog-Api-Key"), API_KEY)
        self.assertEqual(options["headers"].get("Content-Type"), "application/json")
        self.assertTrue(options["verify"])
        self.assertFalse(options["allow_redirects"])
        connect, read = options["timeout"]
        self.assertTrue(0 < connect <= 5 and 0 < read <= 5)
        body = options["json"]
        jpeg = body["requests"][0]["image"]["content"]
        self.assertEqual(body, {"requests": [{"image": {"content": jpeg}, "features": [
            {"type": "LABEL_DETECTION", "maxResults": 10},
        ]}]})
        preview_data = base64.b64decode(jpeg, validate=True)
        self.assertLessEqual(len(preview_data), 512 * 1024)
        self.assertNotIn(b"private-image", preview_data)
        with Image.open(io.BytesIO(preview_data)) as preview:
            self.assertEqual((preview.format, preview.mode), ("JPEG", "RGB"))
            self.assertEqual(preview.size, (768, 341))
            self.assertFalse(preview.getexif())
        self.assertTrue(result.rejected)
        self.assertEqual(result.labels, (("dandelion", 0.96),))

    def test_errors_fail_open_once_and_keep_secrets_out_of_one_decision_record(self):
        image_error = google_response(payload={"responses": [{
            "error": {"code": 7, "message": "private-google-details"},
            "labelAnnotations": [{"description": "Dandelion", "score": 0.99}],
        }]})
        malformed = google_response()
        malformed.json.side_effect = ValueError("private-google-details")
        for index, (reason, response) in enumerate((
            ("image_error", image_error),
            ("malformed_response", malformed),
            ("malformed_response", google_response(payload={"responses": [{"labelAnnotations": "private-google-details"}]})),
            ("http_403", google_response(status=403)),
            ("http_429", google_response(status=429)),
            ("http_500", google_response(status=500)),
            ("timeout", requests.Timeout("private-google-details")),
        )):
            with self.subTest(reason=reason), patch.object(vision.requests, "post", side_effect=[response]) as http:
                state = self.state_dir / f"case-{index}"
                state.mkdir()
                check = self.check(state_dir=state)
                result = check.check(self.image)
                self.assertFalse(result.rejected)
                self.assertEqual((result.reason, result.status, result.units_reserved), (reason, "submitted", 1))
                # Repeating a preview within one article must not compensate for the failure.
                self.assertFalse(check.check(self.image).rejected)
                http.assert_called_once()
                with self.assertLogs("image_search.vision", level="INFO") as logs:
                    result.log("123456789abc", "pixabay", "https://private-image.example/fixture")
                self.assertEqual(len(logs.output), 1)
                line = logs.output[0]
                self.assertIn(f"reason={reason}", line)
                for secret in (API_KEY, "private-google-details", "private-image.example", "http://", "https://"):
                    self.assertNotIn(secret, line)

    def test_invalid_labels_never_count_as_strong_evidence(self):
        response = google_response(
            ("Dandelion", "0.99"), ("Dandelion", float("nan")), ("Dandelion", float("inf")),
            ("Dandelion", 2), ("Dandelion", -1), ("Dandelion", True),
            ("https://private-google.example/secret", 0.99), ("green", 0.98),
        )
        with patch.object(vision.requests, "post", return_value=response) as http:
            result = self.check().check(self.image)
        http.assert_called_once()
        self.assertFalse(result.rejected)
        self.assertEqual(result.labels, (("green", 0.98),))

    def test_unavailable_store_and_invalid_settings_skip_new_calls(self):
        with patch.object(vision.requests, "post") as http:
            for options, reason in (
                ({"mode": "off", "key": ""}, "mode_off"), ({"key": ""}, "missing_credentials"),
                ({"key": "valid-looking-ключ"}, "invalid_credentials"),
                ({"limit": 0}, "monthly_limit_zero"), ({"limit": 2001}, "invalid_monthly_limit"),
                ({"limit": None}, "invalid_monthly_limit"),
            ):
                with self.subTest(reason=reason):
                    result = self.check(**options).check(self.image)
                    self.assertEqual((result.reason, result.units_reserved), (reason, 0))
                    self.assertFalse(result.rejected)
            self.assertEqual(list(self.state_dir.iterdir()), [])
            blocked = self.state_dir / "file"
            blocked.touch()
            result = self.check(state_dir=blocked).check(self.image)
            self.assertEqual((result.reason, result.units_reserved), ("accounting_unavailable", 0))
            self.assertFalse(result.rejected)
            http.assert_not_called()

    def test_reopened_store_preserves_spend_reuses_labels_and_reassesses_context(self):
        with patch.object(vision.requests, "post", return_value=google_response(DANDELION)) as http:
            first = self.check(limit=1).check(self.image)
            repeat = self.check(limit=1, rule=build_vision_conflict_rule("financial_record")).check(self.image)
            different = self.check(limit=1).check(fixture_jpeg("blue"))
        http.assert_called_once()
        self.assertTrue(first.rejected)
        self.assertFalse(repeat.rejected)
        self.assertEqual(first.labels, repeat.labels)
        self.assertEqual((repeat.status, repeat.units_reserved), ("cache_hit", 0))
        self.assertEqual((different.reason, different.units_reserved), ("monthly_limit", 0))

    def test_evidence_eviction_does_not_reset_monthly_spend(self):
        second_image = fixture_jpeg("blue")
        with (patch.object(vision, "_CACHE_ROWS", 1),
              patch.object(vision.requests, "post", return_value=google_response(DANDELION)) as http):
            self.assertEqual(self.check(limit=2).check(self.image).status, "submitted")
            self.assertEqual(self.check(limit=2).check(second_image).status, "submitted")
            self.assertEqual(self.check(limit=2).check(second_image).status, "cache_hit")
            evicted = self.check(limit=2).check(self.image)
        self.assertEqual((evicted.reason, evicted.units_reserved), ("monthly_limit", 0))
        self.assertEqual(http.call_count, 2)

    def test_malformed_cached_labels_fail_open_without_a_replacement_submission(self):
        with patch.object(vision.requests, "post", return_value=google_response(DANDELION)) as http:
            self.assertTrue(self.check().check(self.image).rejected)
            with closing(sqlite3.connect(self.state_dir / vision._STORE_FILENAME)) as db, db:
                db.execute("UPDATE vision_observations SET labels = ?", (json.dumps([["dandelion", "0.99"]]),))
            result = self.check().check(self.image)
        http.assert_called_once()
        self.assertFalse(result.rejected)
        self.assertEqual((result.reason, result.units_reserved), ("accounting_unavailable", 0))

    def test_article_ceiling_survives_publisher_retries_and_allows_other_articles(self):
        third_image = fixture_jpeg("orange")
        june = datetime(2026, 6, 25, tzinfo=timezone.utc)
        july = datetime(2026, 7, 25, tzinfo=timezone.utc)
        with patch.object(vision.requests, "post", return_value=google_response(DANDELION)) as http:
            with patch.object(vision, "_utc_now", return_value=june):
                self.assertEqual(self.check(article_id="first-article").check(self.image).status, "submitted")
                self.assertEqual(self.check(article_id="first-article").check(fixture_jpeg("blue")).status, "submitted")
            self.assertEqual(http.call_count, 2)
            with patch.object(vision, "_utc_now", return_value=july):
                reopened = self.check(article_id="first-article")
                blocked = reopened.check(third_image)
                self.assertEqual((blocked.reason, blocked.units_reserved), ("article_limit", 0))
                self.assertEqual(reopened.check(self.image).status, "cache_hit")
                self.assertEqual(http.call_count, 2)
                different = self.check(article_id="another-article").check(third_image)
                self.assertEqual((different.status, different.units_reserved), ("submitted", 1))
            self.assertEqual(http.call_count, 3)

    def test_parallel_workers_reserve_only_the_last_available_unit(self):
        first, second, http = self.overlapping_checks(self.image, fixture_jpeg("blue"), limit=1)
        self.assertEqual((first.status, first.units_reserved), ("submitted", 1))
        self.assertEqual((second.reason, second.units_reserved), ("monthly_limit", 0))
        http.assert_called_once()

    def test_parallel_duplicate_can_reuse_evidence_after_the_first_worker_finishes(self):
        first, second, http = self.overlapping_checks(self.image, self.image, limit=100)
        self.assertEqual((first.status, first.units_reserved), ("submitted", 1))
        self.assertEqual((second.status, second.units_reserved), ("in_flight", 0))
        http.assert_called_once()
        with patch.object(vision.requests, "post") as reopened_http:
            result = self.check().check(self.image)
        reopened_http.assert_not_called()
        self.assertEqual((result.status, result.labels), ("cache_hit", first.labels))

    def overlapping_checks(self, first_image, second_image, *, limit):
        starting_line = Barrier(2)
        entered_http = Event()
        release_http = Event()

        def blocked_http(*args, **kwargs):
            entered_http.set()
            if not release_http.wait(15):
                raise AssertionError("test never released the Vision response")
            return google_response(DANDELION)

        def check_image(image):
            starting_line.wait(timeout=5)
            return self.check(limit=limit).check(image)

        with patch.object(vision.requests, "post", side_effect=blocked_http) as http, ThreadPoolExecutor(2) as workers:
            futures = [workers.submit(check_image, image) for image in (first_image, second_image)]
            try:
                self.assertTrue(entered_http.wait(5))
                completed, pending = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
                self.assertEqual(len(pending), 1)
                competitor = completed.pop().result()
            finally:
                release_http.set()
            submitted = pending.pop().result(timeout=5)
        return submitted, competitor, http

    def test_interrupted_reservation_survives_until_the_pacific_month_rolls_over(self):
        before = datetime(2026, 7, 1, 6, 59, 59, tzinfo=timezone.utc)
        after = datetime(2026, 7, 1, 7, tzinfo=timezone.utc)
        second_image = fixture_jpeg("blue")
        with patch.object(vision, "_utc_now", return_value=before):
            with patch.object(vision.requests, "post", side_effect=KeyboardInterrupt) as interrupted:
                with self.assertRaises(KeyboardInterrupt):
                    self.check(limit=1).check(self.image)
            interrupted.assert_called_once()
            with patch.object(vision.requests, "post") as http:
                duplicate = self.check(limit=1).check(self.image)
                different = self.check(limit=1).check(second_image)
            http.assert_not_called()
            self.assertEqual(duplicate.units_reserved, 0)
            self.assertEqual((different.reason, different.units_reserved), ("monthly_limit", 0))
        with (patch.object(vision, "_utc_now", return_value=after),
              patch.object(vision.requests, "post", return_value=google_response(USB_DEVICE)) as http):
            next_month = self.check(limit=1).check(second_image)
            exhausted = self.check(limit=1).check(fixture_jpeg("orange"))
        http.assert_called_once()
        self.assertEqual((next_month.status, next_month.units_reserved), ("submitted", 1))
        self.assertEqual((exhausted.reason, exhausted.units_reserved), ("monthly_limit", 0))


class VisionProbeTests(unittest.TestCase):
    def test_explicit_live_mode_uses_persisted_vision_evidence(self):
        with TemporaryDirectory() as directory, ExitStack() as stack:
            image = Path(directory) / "generated.jpg"
            image.write_bytes(fixture_jpeg("green"))
            state_dir = Path(directory) / "state"
            args = ["--image", str(image), "--title", COLD_CARD["title"]]
            for name, value in {
                "STOCK_IMAGE_VISION_MODE": "shadow", "STOCK_IMAGE_VISION_MONTHLY_UNIT_LIMIT": 1,
                "GOOGLE_CLOUD_VISION_API_KEY": API_KEY, "WRITABLE_STATE_DIR": state_dir,
            }.items():
                stack.enter_context(patch.object(vision_label_probe, name, value))
            google = stack.enter_context(patch.object(vision.requests, "post", return_value=google_response(DANDELION)))
            wordpress = stack.enter_context(patch.object(publisher.session, "post"))
            generate = stack.enter_context(patch.object(publisher, "_generated_image_content"))
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(vision_label_probe.main(args), 0)
            self.assertEqual(json.loads(output.getvalue())["reason"], "offline")
            self.assertFalse(state_dir.exists())
            google.assert_not_called()
            with patch.object(vision_label_probe, "STOCK_IMAGE_VISION_MODE", "off"), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(vision_label_probe.main([*args, "--live"]), 2)
            self.assertEqual(json.loads(output.getvalue())["reason"], "mode_off")
            google.assert_not_called()
            self.assertFalse(state_dir.exists())
            state_dir.mkdir()
            for status, units in (("submitted", 1), ("cache_hit", 0)):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(vision_label_probe.main([*args, "--live"]), 0)
                record = json.loads(output.getvalue())
                self.assertEqual(record["status"], status)
                self.assertEqual(record["units_reserved"], units)
                self.assertEqual(record["decision"], "would_reject")
                self.assertIn("dandelion", output.getvalue())
            google.assert_called_once()
            wordpress.assert_not_called()
            generate.assert_not_called()
