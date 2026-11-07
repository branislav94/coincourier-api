from __future__ import annotations

import inspect
import hashlib
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = PROJECT_DIR.parent
MIGRATION_DIR = REPOSITORY_DIR / "maintenance" / "migrations"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config
import fetcher
import gpt_processor
from duplicate_detection.event_matching import EventFacts, extract_event_facts, extract_relationship_organizations
from duplicate_detection.identities import (
    canonicalize_url,
    content_fingerprint,
    normalize_title,
)
from duplicate_detection.policy import AssessmentType, assess_relationship
from duplicate_detection.shadow import analyze_duplicates_in_shadow
from repositories.duplicate_assessments import DuplicateAssessmentRepository


def article(article_id: int, **overrides):
    value = {
        "id": article_id,
        "news_id": f"provider-{article_id}",
        "event_id": None,
        "news_url": f"https://news.example/article-{article_id}",
        "title": "Crypto market report",
        "full_text": "Short source text.",
        "publish_date": "2026-08-31 12:00:00",
        "source_name": "Example News",
        "tickers": "",
        "topics": "",
    }
    value.update(overrides)
    return value


class IdentityTests(unittest.TestCase):
    def test_same_provider_id_is_exact_duplicate(self):
        result = assess_relationship(
            article(1, news_id="shared"),
            article(2, news_id="shared"),
        )
        self.assertEqual(result.assessment_type, AssessmentType.EXACT_DUPLICATE)
        self.assertTrue(result.same_provider_article_id)

    def test_same_canonical_url_is_exact_duplicate(self):
        result = assess_relationship(
            article(1, news_url="HTTPS://NEWS.EXAMPLE:443/story/"),
            article(2, news_url="https://news.example/story"),
        )
        self.assertEqual(result.assessment_type, AssessmentType.EXACT_DUPLICATE)
        self.assertTrue(result.same_canonical_url)

    def test_tracking_only_url_variants_normalize_together(self):
        left = canonicalize_url("https://News.Example/story/?id=7&utm_source=x#top")
        right = canonicalize_url("https://news.example/story?id=7&fbclid=abc")
        self.assertEqual(left, right)

    def test_meaningful_query_parameters_remain_distinct(self):
        left = canonicalize_url("https://news.example/story?edition=us")
        right = canonicalize_url("https://news.example/story?edition=eu")
        self.assertNotEqual(left, right)

    def test_same_content_fingerprint_is_exact_duplicate(self):
        source = "This is the same complete source report with factual detail. " * 3
        result = assess_relationship(
            article(1, full_text=f"<p>{source}</p>"),
            article(2, full_text=f"  {source}  "),
        )
        self.assertIsNotNone(content_fingerprint(source))
        self.assertEqual(result.assessment_type, AssessmentType.EXACT_DUPLICATE)
        self.assertTrue(result.same_content_hash)

    def test_title_normalization_preserves_entities_dates_and_numbers(self):
        normalized = normalize_title("  Coinbase -- BTC: +12% on Sept. 1, 2026  ")
        self.assertIn("coinbase", normalized)
        self.assertIn("btc", normalized)
        self.assertIn("12", normalized)
        self.assertIn("2026", normalized)


class RelationshipOrganizationTests(unittest.TestCase):
    def test_relationship_organizations_recognize_title_and_source_body(self):
        cases = (
            {"title": "Mercado Bitcoin Hack Continues After Containment Claim", "full_text": "Brief source summary."},
            {"title": "Exchange incident", "full_text": "Attackers continued draining Mercado Bitcoin wallets"},
            {"title": "Exchange incident", "text": "Attackers continued draining Mercado Bitcoin wallets", "full_text": "Other content."},
            {"title": "  mErCaDo   bItCoIn wallets  ", "full_text": "Brief source summary."},
            {"title": "Exchange incident", "full_text": "<p>Attackers reached Mercado <b>Bitcoin</b> wallets.</p>"},
        )
        for fields in cases:
            with self.subTest(fields=fields):
                row = article(1, **fields)
                original = dict(row)
                before = extract_event_facts(row)
                self.assertEqual(extract_relationship_organizations(row), ("org:mercado-bitcoin",))
                self.assertEqual(extract_event_facts(row), before)
                self.assertEqual(row, original)

    def test_relationship_context_preserves_reporting_actor_and_core_facts(self):
        row = article(1, title="Mercado Bitcoin says phased reopening", full_text="Brief source summary.")
        expected = EventFacts(
            entities=("actor:mercado bitcoin", "asset:BTC", "source:example news"),
            dates=(), numbers=(), actions=(),
        )
        self.assertEqual(extract_event_facts(row), expected)
        self.assertEqual(extract_relationship_organizations(row), ("org:mercado-bitcoin",))
        self.assertEqual(extract_event_facts(row), expected)
        self.assertNotIn("org:mercado-bitcoin", expected.entities)

    def test_relationship_organizations_ignore_partial_and_non_source_mentions(self):
        cases = (
            {"title": "Mercado Bitcoins incident"},
            {"title": "NotMercado Bitcoin incident"},
            {"source_name": "Mercado Bitcoin"},
            {"news_url": "https://news.example/Mercado-Bitcoin"},
            {"generated_title": "Mercado Bitcoin incident", "generated_text": "Mercado Bitcoin wallets"},
            {"enriched_title": "Mercado Bitcoin incident", "enriched_text": "Mercado Bitcoin wallets"},
            {"title": "Mercado", "full_text": "Bitcoin wallets are discussed."},
            {"text": "Original unrelated source evidence.", "full_text": "Mercado Bitcoin wallets"},
        )
        for fields in cases:
            with self.subTest(fields=fields):
                row = article(1, title="Exchange incident", full_text="Brief source summary.")
                row.update(fields)
                original = dict(row)
                before = extract_event_facts(row)
                self.assertEqual(extract_relationship_organizations(row), ())
                self.assertEqual(extract_event_facts(row), before)
                self.assertEqual(row, original)

    def test_shared_relationship_context_refines_only_broad_overlap(self):
        current = article(1,
            title="Mercado Bitcoin hack continued hours after exchange said attack was contained, report finds",
            full_text="Attackers drained $391000 through 2 wallets.")
        prior = article(2,
            title="Mercado Bitcoin says phased reopening to begin Monday, will cover losses",
            full_text="Mercado Bitcoin halted withdrawals on Monday.")
        before = (extract_event_facts(current), extract_event_facts(prior))
        with patch("duplicate_detection.policy.extract_relationship_organizations", return_value=()):
            baseline = assess_relationship(current, prior)
        self.assertEqual(baseline.assessment_type, AssessmentType.BROAD_TOPIC_OVERLAP)
        result = assess_relationship(current, prior)
        self.assertEqual(result.assessment_type, AssessmentType.RELATED_EVENT)
        self.assertEqual(result.reason_codes, ("shared_relationship_organization", "different_action_or_status"))
        self.assertEqual(set(result.shared_entities), set(baseline.shared_entities) | {"org:mercado-bitcoin"})
        self.assertEqual(result.new_fact_codes, baseline.new_fact_codes)
        self.assertEqual(result.evidence_uncertainty, baseline.evidence_uncertainty)
        self.assertEqual(result.event_identity_source, baseline.event_identity_source)
        self.assertEqual(result.event_identity_source, "none")
        self.assertEqual(result.shared_dates, baseline.shared_dates)
        self.assertEqual(result.shared_numbers, baseline.shared_numbers)
        self.assertEqual(result.shared_actions, baseline.shared_actions)
        self.assertEqual(result.confirmation_anchors, baseline.confirmation_anchors)
        self.assertEqual(result.contradiction_flags, baseline.contradiction_flags)
        self.assertEqual((extract_event_facts(current), extract_event_facts(prior)), before)
        for facts in before:
            self.assertNotIn("org:mercado-bitcoin", facts.entities)

    def test_one_sided_relationship_organization_does_not_refine(self):
        mercado = article(1, title="Mercado Bitcoin hack continues", full_text="Short source text.")
        unrelated = article(2, title="Bitcoin custody policy discussion", full_text="Brief source summary.")
        for current, prior in ((mercado, unrelated), (unrelated, mercado)):
            with self.subTest(current_id=current["id"]):
                with patch("duplicate_detection.policy.extract_relationship_organizations", return_value=()):
                    baseline = assess_relationship(current, prior)
                self.assertEqual(baseline.assessment_type, AssessmentType.BROAD_TOPIC_OVERLAP)
                self.assertEqual(assess_relationship(current, prior), baseline)

    def test_non_broad_assessments_do_not_consume_relationship_context(self):
        body = "Neutral background context about markets and the network for readers. " * 3
        cases = (
            (AssessmentType.MATERIAL_UPDATE,
             article(1, event_id="synthetic-context-event", title="Mercado Bitcoin says Bitcoin rises", full_text="Brief summary."),
             article(2, event_id="synthetic-context-event", title="Mercado Bitcoin price rises", full_text="Short context.")),
            (AssessmentType.SAME_EVENT_DUPLICATE,
             article(1, event_id="synthetic-context-event", title="Coinbase acquires Mercado Bitcoin", full_text="Brief summary."),
             article(2, event_id="synthetic-context-event", title="Coinbase acquires Mercado Bitcoin", full_text="Short context.")),
            (AssessmentType.EXACT_DUPLICATE,
             article(1, title="Mercado Bitcoin price rises", full_text=body),
             article(2, title="Mercado Bitcoin price rises", full_text=body)),
            (AssessmentType.RELATED_EVENT,
             article(1, title="Mercado Bitcoin price rises", full_text="Brief summary."),
             article(2, title="Mercado Bitcoin price falls", full_text="Short context.")),
        )
        for expected_type, current, prior in cases:
            with self.subTest(classification=expected_type):
                with patch("duplicate_detection.policy.extract_relationship_organizations", return_value=()):
                    baseline = assess_relationship(current, prior)
                with patch("duplicate_detection.policy.extract_relationship_organizations", wraps=extract_relationship_organizations) as context:
                    result = assess_relationship(current, prior)
                self.assertEqual(result.assessment_type, expected_type)
                self.assertEqual(result, baseline)
                context.assert_not_called()


class EventPolicyTests(unittest.TestCase):
    def test_same_non_null_event_id_is_same_event_duplicate(self):
        result = assess_relationship(
            article(1, event_id="event-7"),
            article(2, event_id="event-7"),
        )
        self.assertTrue(result.same_event_id)
        self.assertEqual(result.assessment_type, AssessmentType.SAME_EVENT_DUPLICATE)

    def test_prior_only_actions_are_uncertain_same_event_evidence(self):
        cases = (
            ("Brief source summary.", "Bitcoin rises.", (), ("price_rise",)),
            ("Bitcoin rises.", "Bitcoin rises. A report followed.", ("price_rise",), ("price_rise", "report")),
        )
        for current_body, prior_body, current_actions, prior_actions in cases:
            with self.subTest(current_actions=current_actions, prior_actions=prior_actions):
                current = article(1, event_id="synthetic-action-event", title="Bitcoin market discussion", full_text=current_body)
                prior = article(2, event_id="synthetic-action-event", title="Bitcoin market discussion", full_text=prior_body)
                self.assertEqual(extract_event_facts(current).actions, current_actions)
                self.assertEqual(extract_event_facts(prior).actions, prior_actions)
                result = assess_relationship(current, prior)
                self.assertEqual(result.assessment_type, AssessmentType.SAME_EVENT_DUPLICATE)
                self.assertEqual(result.new_fact_codes, ())
                self.assertIn("prior_only_action_evidence", result.evidence_uncertainty)

    def test_current_action_additions_remain_material_updates(self):
        cases = (
            ("Bitcoin rises.", "Brief source summary.", ("price_rise",), ()),
            ("Bitcoin rises.", "Bitcoin falls.", ("price_rise",), ("price_fall",)),
            ("Bitcoin rises. A report followed.", "Bitcoin rises.", ("price_rise", "report"), ("price_rise",)),
            ("Bitcoin rises. A report followed.", "Bitcoin falls. A report followed.", ("price_rise", "report"), ("price_fall", "report")),
        )
        for current_body, prior_body, current_actions, prior_actions in cases:
            with self.subTest(current_actions=current_actions, prior_actions=prior_actions):
                current = article(1, event_id="synthetic-action-event", title="Bitcoin market discussion", full_text=current_body)
                prior = article(2, event_id="synthetic-action-event", title="Bitcoin market discussion", full_text=prior_body)
                self.assertEqual(extract_event_facts(current).actions, current_actions)
                self.assertEqual(extract_event_facts(prior).actions, prior_actions)
                result = assess_relationship(current, prior)
                self.assertEqual(result.assessment_type, AssessmentType.MATERIAL_UPDATE)
                self.assertEqual(result.new_fact_codes, ("new_action_or_status",))
                self.assertNotIn("prior_only_action_evidence", result.evidence_uncertainty)

    def test_equal_action_sets_preserve_same_event_classification(self):
        for body, actions in (("Brief source summary.", ()), ("Bitcoin rises.", ("price_rise",))):
            with self.subTest(actions=actions):
                current = article(1, event_id="synthetic-action-event", title="Bitcoin market discussion", full_text=body)
                prior = article(2, event_id="synthetic-action-event", title="Bitcoin market discussion", full_text=body)
                self.assertEqual(extract_event_facts(current).actions, actions)
                self.assertEqual(extract_event_facts(prior).actions, actions)
                result = assess_relationship(current, prior)
                self.assertEqual(result.assessment_type, AssessmentType.SAME_EVENT_DUPLICATE)
                self.assertEqual(result.new_fact_codes, ())
                self.assertNotIn("prior_only_action_evidence", result.evidence_uncertainty)

    def test_prior_only_action_evidence_preserves_existing_uncertainty(self):
        current = article(1, event_id="synthetic-action-event", title="Bitcoin market discussion", full_text="Bitcoin may move.")
        prior = article(2, event_id="synthetic-action-event", title="Bitcoin market discussion", full_text="Bitcoin rises.")
        self.assertEqual(extract_event_facts(current).actions, ())
        self.assertEqual(extract_event_facts(prior).actions, ("price_rise",))
        self.assertIn("uncertain_outcome_or_status", extract_event_facts(current).uncertainty)
        result = assess_relationship(current, prior)
        self.assertEqual(result.assessment_type, AssessmentType.SAME_EVENT_DUPLICATE)
        self.assertEqual(result.new_fact_codes, ())
        self.assertEqual(result.evidence_uncertainty, ("prior_only_action_evidence", "uncertain_outcome_or_status"))

    def test_related_event_action_reason_remains_symmetric(self):
        sparse = article(1, title="Coinbase market discussion", full_text="Brief source summary.")
        active = article(2, title="Coinbase market discussion", full_text="Coinbase launches.")
        self.assertEqual(extract_event_facts(sparse).actions, ())
        self.assertEqual(extract_event_facts(active).actions, ("launch",))
        for current, prior in ((sparse, active), (active, sparse)):
            with self.subTest(current_id=current["id"]):
                result = assess_relationship(current, prior)
                self.assertEqual(result.assessment_type, AssessmentType.RELATED_EVENT)
                self.assertFalse(result.same_event_id)
                self.assertEqual(result.reason_codes, ("shared_key_entity", "different_action_or_status"))

    def test_null_event_ids_do_not_compare_equal(self):
        result = assess_relationship(article(1), article(2))
        self.assertFalse(result.same_event_id)

    def test_different_event_ids_can_still_match_same_event(self):
        first = article(
            1,
            event_id="event-a",
            title="SEC approves Bitcoin ETF on January 10, 2026",
            tickers="BTC",
        )
        second = article(
            2,
            event_id="event-b",
            title="Bitcoin ETF approved by SEC on January 10 2026",
            tickers="BTC",
        )
        result = assess_relationship(first, second)
        self.assertFalse(result.same_event_id)
        self.assertEqual(result.assessment_type, AssessmentType.SAME_EVENT_DUPLICATE)

    def test_high_title_overlap_same_entities_and_date_is_same_event(self):
        result = assess_relationship(
            article(
                1,
                title="SEC approves Bitcoin ETF on January 10, 2026",
                tickers="BTC",
            ),
            article(
                2,
                title="Bitcoin ETF approved by SEC on January 10 2026",
                tickers="BTC",
            ),
        )
        self.assertGreaterEqual(result.title_token_jaccard, 0.60)
        self.assertEqual(result.assessment_type, AssessmentType.SAME_EVENT_DUPLICATE)

    def test_high_title_overlap_with_different_key_date_is_related(self):
        result = assess_relationship(
            article(1, title="SEC reviews Bitcoin ETF on January 10 2026", tickers="BTC"),
            article(2, title="SEC reviews Bitcoin ETF on January 12 2026", tickers="BTC"),
        )
        self.assertEqual(result.assessment_type, AssessmentType.RELATED_EVENT)
        self.assertIn("different_key_date", result.reason_codes)

    def test_same_event_with_new_percentage_is_material_update(self):
        result = assess_relationship(
            article(
                1,
                event_id="event-7",
                title="Bitcoin ETF inflows rise 20%",
                tickers="BTC",
            ),
            article(
                2,
                event_id="event-7",
                title="Bitcoin ETF inflows rise",
                tickers="BTC",
            ),
        )
        self.assertEqual(result.assessment_type, AssessmentType.MATERIAL_UPDATE)
        self.assertIn("new_numeric_value", result.reason_codes)

    def test_generic_bitcoin_overlap_is_broad_topic_overlap(self):
        result = assess_relationship(
            article(1, title="Bitcoin price rises after a busy trading day"),
            article(2, title="Bitcoin miners discuss regional energy policy"),
        )
        self.assertEqual(result.assessment_type, AssessmentType.BROAD_TOPIC_OVERLAP)

    def test_same_company_with_different_action_is_related_event(self):
        result = assess_relationship(
            article(1, title="Coinbase launches custody service"),
            article(2, title="Coinbase acquires trading platform"),
        )
        self.assertEqual(result.assessment_type, AssessmentType.RELATED_EVENT)
        self.assertIn("different_action_or_status", result.reason_codes)

    def test_structured_tickers_take_precedence_and_source_is_labeled(self):
        facts = extract_event_facts(
            article(1, title="Market update", tickers="BTC, ETH", source_name="Wire Desk")
        )
        self.assertIn("asset:BTC", facts.entities)
        self.assertIn("asset:ETH", facts.entities)
        self.assertIn("source:wire desk", facts.entities)

    def test_dates_percentages_money_and_quantities_are_separate_signals(self):
        facts = extract_event_facts(
            article(
                1,
                title="Bitcoin rose 12% on September 1, 2026",
                full_text="Funds reported $1.2 billion and 5,000 BTC in holdings.",
            )
        )
        self.assertIn("2026-09-01", facts.dates)
        self.assertIn("12%", facts.numbers)
        self.assertIn("$1.2 billion", facts.numbers)
        self.assertIn("5000 btc", facts.numbers)


class RecordingRepository:
    def __init__(self, candidates):
        self.candidates = candidates
        self.load_calls = []
        self.saved = []

    def load_candidates(self, article_id, *, lookback_hours):
        self.load_calls.append((article_id, lookback_hours))
        return list(self.candidates)

    def save_assessments(self, article_id, assessments):
        self.saved.append((article_id, list(assessments)))
        return len(self.saved[-1][1])


class ShadowAnalysisTests(unittest.TestCase):
    def setUp(self):
        log_patch = patch("duplicate_detection.shadow.logging.info")
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def test_assessments_are_independent_of_candidate_order(self):
        current = article(10, title="Coinbase launches custody service")
        candidates = [
            article(3, title="Bitcoin miners discuss energy policy"),
            article(2, title="Coinbase acquires trading platform"),
        ]
        first = analyze_duplicates_in_shadow(
            current,
            repository=RecordingRepository(candidates),
        )
        second = analyze_duplicates_in_shadow(
            current,
            repository=RecordingRepository(list(reversed(candidates))),
        )
        self.assertEqual(first, second)
        self.assertEqual([candidate_id for candidate_id, _ in first], [2, 3])

    def test_article_is_never_compared_with_itself(self):
        current = article(10)
        repository = RecordingRepository([current, article(11)])
        results = analyze_duplicates_in_shadow(current, repository=repository)
        self.assertEqual([candidate_id for candidate_id, _ in results], [11])
        self.assertEqual([candidate_id for candidate_id, _ in repository.saved[0][1]], [11])

    def test_candidate_query_is_bounded_to_recent_relevant_rows(self):
        cursor = Mock()
        cursor.fetchall.return_value = []
        connection = Mock()
        connection.cursor.return_value = cursor
        repository = DuplicateAssessmentRepository(connect=lambda: connection)
        self.assertEqual(repository.load_candidates(7, lookback_hours=72), [])
        sql, params = cursor.execute.call_args.args
        normalized = " ".join(sql.lower().split())
        self.assertIn("timestampadd(hour, -%s, utc_timestamp())", normalized)
        self.assertIn("chosen_for_publish = 1 or processed = 1", normalized)
        self.assertIn("limit %s", normalized)
        self.assertEqual(params, (7, 72, 200))

    def test_duplicate_pair_storage_is_idempotent(self):
        cursor = Mock()
        connection = Mock()
        connection.cursor.return_value = cursor
        repository = DuplicateAssessmentRepository(connect=lambda: connection)
        assessment = assess_relationship(
            article(1, news_id="same"),
            article(2, news_id="same"),
        )
        repository.save_assessments(1, [(2, assessment)])
        repository.save_assessments(1, [(2, assessment)])
        first_sql, first_rows = cursor.executemany.call_args_list[0].args
        second_sql, second_rows = cursor.executemany.call_args_list[1].args
        self.assertIn("ON DUPLICATE KEY UPDATE", first_sql)
        self.assertEqual(first_rows, second_rows)
        self.assertEqual(connection.commit.call_count, 2)

    def _assert_processing_continues(self, candidate, expected_type, **raw_overrides):
        repository = RecordingRepository([candidate])
        raw = article(10, **raw_overrides)
        final_doc = {
            "title": "Processed title",
            "full_text": "<p>Processed body.</p>",
            "seo_focus": "focus",
            "seo_slug": "processed-title",
            "seo_meta": "Processed article metadata.",
        }

        def run_shadow(selected):
            return analyze_duplicates_in_shadow(selected, repository=repository)

        with (
            patch.object(gpt_processor, "DUPLICATE_SHADOW_ENABLED", True),
            patch.object(gpt_processor, "analyze_duplicates_in_shadow", side_effect=run_shadow),
            patch.object(gpt_processor, "enrich_with_search", return_value={}) as enrich,
            patch.object(gpt_processor, "_maybe_video_url", return_value=""),
            patch.object(gpt_processor, "classify_and_rewrite", return_value=({}, "test")),
            patch.object(gpt_processor, "repair_if_needed", return_value=final_doc),
            patch.object(gpt_processor, "build_news_schema_jsonld", return_value=None),
            patch.object(gpt_processor, "validate_rewritten_article"),
            patch.object(gpt_processor, "store_rich_news") as store,
            patch.object(gpt_processor, "mark_processed") as complete,
            patch.object(gpt_processor.logging, "info"),
            patch.object(gpt_processor.logging, "exception"),
        ):
            self.assertTrue(gpt_processor.process_one(raw))

        enrich.assert_called_once_with(raw)
        store.assert_called_once_with(final_doc, raw)
        complete.assert_called_once_with(raw["news_url"])
        self.assertEqual(repository.saved[0][1][0][1].assessment_type, expected_type)

    def test_shadow_exact_duplicate_still_continues_processing(self):
        self._assert_processing_continues(
            article(2, news_id="provider-10"),
            AssessmentType.EXACT_DUPLICATE,
        )

    def test_shadow_same_event_duplicate_still_continues_processing(self):
        self._assert_processing_continues(
            article(2, event_id="event-7"),
            AssessmentType.SAME_EVENT_DUPLICATE,
            event_id="event-7",
        )

    def test_shadow_analysis_exception_still_continues_processing(self):
        raw = article(10)
        final_doc = {
            "title": "Processed title",
            "full_text": "<p>Processed body.</p>",
            "seo_focus": "focus",
            "seo_slug": "processed-title",
            "seo_meta": "Processed article metadata.",
        }
        with (
            patch.object(gpt_processor, "DUPLICATE_SHADOW_ENABLED", True),
            patch.object(
                gpt_processor,
                "analyze_duplicates_in_shadow",
                side_effect=RuntimeError("database unavailable"),
            ),
            patch.object(gpt_processor, "enrich_with_search", return_value={}) as enrich,
            patch.object(gpt_processor, "_maybe_video_url", return_value=""),
            patch.object(gpt_processor, "classify_and_rewrite", return_value=({}, "test")),
            patch.object(gpt_processor, "repair_if_needed", return_value=final_doc),
            patch.object(gpt_processor, "build_news_schema_jsonld", return_value=None),
            patch.object(gpt_processor, "validate_rewritten_article"),
            patch.object(gpt_processor, "store_rich_news"),
            patch.object(gpt_processor, "mark_processed"),
            patch.object(gpt_processor.logging, "info"),
            patch.object(gpt_processor.logging, "exception"),
        ):
            self.assertTrue(gpt_processor.process_one(raw))
        enrich.assert_called_once_with(raw)

    def test_disabled_flag_performs_no_duplicate_queries_or_writes(self):
        with (
            patch.object(gpt_processor, "DUPLICATE_SHADOW_ENABLED", False),
            patch.object(gpt_processor, "analyze_duplicates_in_shadow") as analyze,
        ):
            gpt_processor._run_duplicate_shadow_fail_open(article(1))
        analyze.assert_not_called()


class FetchCoverageAndMigrationTests(unittest.TestCase):
    def test_every_active_fetch_path_requests_eventid(self):
        calls = []

        def fake_fetch(url, params):
            calls.append((url, dict(params)))
            return {"data": []}

        with (
            patch.object(fetcher, "_fetch", side_effect=fake_fetch),
            patch.object(fetcher, "ALLOW_VIDEO", False),
        ):
            self.assertEqual(fetcher._pull_batch(), [])
        self.assertEqual(len(calls), 3)
        for _url, params in calls:
            self.assertEqual(params["extra-fields"], "id,eventid,rankscore")
        category = calls[1][1]
        self.assertEqual(
            {key: value for key, value in category.items() if key != "extra-fields"},
            {"section": "general", "items": fetcher.ITEMS_PER_PULL, "page": 1},
        )

    def test_upsert_preserves_existing_event_id_when_provider_omits_it(self):
        cursor = Mock()
        connection = Mock()
        connection.cursor.return_value = cursor
        item = {
            "news_url": "https://news.example/story",
            "_canonical_url": "https://news.example/story",
            "title": "Story",
            "text": "Source text",
            "source_name": "Wire",
            "date": None,
            "_title_hash": "hash",
        }
        with patch.object(fetcher, "get_db_connection", return_value=connection):
            fetcher._insert_or_update([item], "batch")
        sql = cursor.execute.call_args.args[0]
        params = cursor.execute.call_args.args[1]
        self.assertIn("event_id = COALESCE(VALUES(event_id), event_id)", sql)
        self.assertIsNone(params[14])

    def test_legacy_pull_identity_helpers_remain_unchanged(self):
        self.assertEqual(
            fetcher._clean_url("https://News.Example/story/?ref=feed"),
            "https://News.Example/story",
        )
        expected = hashlib.sha256("title 12".encode("utf-8")).hexdigest()
        self.assertEqual(fetcher._hash_title("  Title 12  "), expected)

    def test_phase5_migration_is_additive_and_pairwise_idempotent(self):
        preflight = (MIGRATION_DIR / "006_phase5_duplicate_preflight.sql").read_text(
            encoding="utf-8"
        )
        migration = (MIGRATION_DIR / "007_phase5_duplicate_shadow.sql").read_text(
            encoding="utf-8"
        )
        lowered = migration.lower()
        self.assertIn("duplicate_assessments", migration)
        self.assertIn("uq_duplicate_assessment_pair_policy", migration)
        self.assertIn("article_id, candidate_article_id, policy_version", migration)
        self.assertIn("event_id", preflight)
        self.assertNotIn("drop ", lowered)
        self.assertNotIn("vector", lowered)

    def test_source_defaults_keep_shadow_disabled_and_lookback_bounded(self):
        source = inspect.getsource(config)
        self.assertIn('_env_bool("DUPLICATE_SHADOW_ENABLED", False)', source)
        self.assertIn('os.getenv("DUPLICATE_LOOKBACK_HOURS", "72")', source)
        self.assertIn('os.getenv("DUPLICATE_POLICY_VERSION", "v1")', source)
        env_example = (REPOSITORY_DIR / ".env.example").read_text(encoding="utf-8")
        self.assertIn("DUPLICATE_SHADOW_ENABLED=false", env_example)
        self.assertIn("DUPLICATE_LOOKBACK_HOURS=72", env_example)
        self.assertIn("DUPLICATE_POLICY_VERSION=v1", env_example)

    def test_image_search_v1_default_remains_unchanged(self):
        self.assertIn('os.getenv("IMAGE_SEARCH_ENGINE", "v1")', inspect.getsource(config))


if __name__ == "__main__":
    unittest.main()
