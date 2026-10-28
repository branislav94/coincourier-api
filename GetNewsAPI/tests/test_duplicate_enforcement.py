from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from duplicate_detection.enforcement import DuplicateDecision, assess_duplicate_enforcement
from duplicate_detection.identities import valid_event_id
from duplicate_detection.policy import AssessmentType, assess_relationship
from repositories.duplicate_assessments import DuplicateAssessmentRepository


def source(raw_id=10, **updates):
    row = {
        "id": raw_id, "news_id": f"provider-{raw_id}", "event_id": "event-700",
        "news_url": f"https://news.example/{raw_id}", "title": "Solana Alpenglow activation confirmed",
        "full_text": "Alpenglow activates on Solana mainnet.", "publish_date": "2026-10-28 12:00:00",
        "source_name": "Independent Wire", "processed": 1,
    }
    row.update(updates)
    return row


def primary(raw_id=1, **updates):
    return source(raw_id, **{
        "canonical_rich_id": 100 + raw_id, "canonical_wp_id": 900 + raw_id,
        "canonical_wp_url": f"https://coincourier.example/{raw_id}", "canonical_published": 1,
        "canonical_publish_status": "published", **updates,
    })


class MemoryRepository:
    def __init__(self, current, candidates=()):
        self.current = current
        self.candidates = list(candidates)
        self.saved = []
        self.loads = []
        self.fail_read = False
        self.fail_save = False

    def load_source_article(self, article_id):
        if self.fail_read:
            raise RuntimeError("secret source connection text")
        return self.current

    def load_enforcement_candidates(self, article_id, **kwargs):
        self.loads.append((article_id, kwargs))
        return self.candidates

    def save_assessments(self, article_id, assessments, *, audit_context=None):
        if self.fail_save:
            raise RuntimeError("secret audit connection text")
        pairs = list(assessments)
        self.saved.append((article_id, pairs, audit_context))
        return len(pairs)


class DuplicateEnforcementTests(unittest.TestCase):
    def decide(self, current=None, candidates=None, **kwargs):
        repo = MemoryRepository(current or source(), [primary()] if candidates is None else candidates)
        result = assess_duplicate_enforcement(current or source(), mode="enforce", stage="raw", repository=repo, **kwargs)
        return result, repo

    def test_six_real_same_event_groups_and_wording_variants(self):
        groups = [
            (["Ethereum Postpones Glamsterdam Hoodi Testnet Activation", "Ethereum Foundation Postpones Glamsterdam Hoodi Testnet Fork", "Ethereum Delays Hoodi Glamsterdam Testnet Fork"], "Ethereum Foundation postponed Hoodi activation."),
            (["JPMorgan Brings Tokenized Money Market Funds to Solana", "JPMorgan Adds Tokenized Money Market Funds to Solana", "J.P. Morgan Tokenized Funds Now on Solana"], "JPMorgan launched tokenized funds on Solana."),
            (["Solana Alpenglow Mainnet Activation Confirmed", "Solana Activates Alpenglow on Mainnet"], "Solana Alpenglow mainnet activation."),
            (["Bitcoin Core Updates Security Policy for AI-Discoverable Vulnerabilities", "Bitcoin Core Revises Security Policy for AI-Enabled Bug Discovery"], "Bitcoin Core revised its security policy."),
            (["DekaBank Launches Crypto Trading for Sparkassen", "Sparkassen Crypto Trading Introduced by DekaBank"], "DekaBank launched Sparkassen crypto trading."),
            (["UK Court Applies Chinese Law in Bitcoin Seizure Ruling", "British Court Rules Chinese Law Governs Seized Bitcoin"], "UK court ruled on Chinese law and seized Bitcoin."),
        ]
        for titles, body in groups:
            first = primary(title=titles[0], full_text=body, source_name="First Outlet")
            for title in titles[1:]:
                with self.subTest(title=title):
                    result, repo = self.decide(source(title=title, full_text=body, source_name="Other Outlet"), [first])
                    self.assertTrue(result.reject)
                    self.assertEqual(result.assessment.assessment_type, AssessmentType.SAME_EVENT_DUPLICATE)
                    self.assertEqual(result.canonical_wp_id, 901)
                    self.assertEqual(len(repo.saved), 1)

    def test_material_number_date_status_actor_and_milestone_are_allowed(self):
        pairs = [
            ({"title": "Solana fund reaches $2 billion"}, {"title": "Solana fund reaches $1 billion"}),
            ({"title": "Solana launch on October 29 2026"}, {"title": "Solana launch on October 28 2026"}),
            ({"title": "Alpenglow activation completed"}, {"title": "Alpenglow activation delayed"}),
            ({"title": "Commerzbank joins DekaBank crypto trading"}, {"title": "DekaBank crypto trading"}),
            ({"title": "Solana completes Alpenglow implementation"}, {"title": "Solana Alpenglow activation announced"}),
            ({"actors": ["New Participant"]}, {"actors": []}),
            ({"title": "Jane Doe joins Solana Alpenglow"}, {"title": "Solana Alpenglow"}),
            ({"title": "Court overturns Chinese law Bitcoin ruling"}, {"title": "Court upholds Chinese law Bitcoin ruling"}),
            ({"title": "Solana exploit resolved"}, {"title": "Solana exploit reported"}),
            ({"title": "Solana case no ABC-2026-42 ruling"}, {"title": "Solana case no ABC-2026-41 ruling"}),
            ({"full_text": "Solana Alpenglow supports $200m in funds."}, {"full_text": "Solana Alpenglow supports $100m in funds."}),
            ({"full_text": "Alpenglow consensus latency is 150 milliseconds."}, {"full_text": "Alpenglow consensus latency is 400 milliseconds."}),
            ({"full_text": "Solana Alpenglow activation completed. The network is offline."}, {"full_text": "Solana Alpenglow activation completed."}),
            ({"full_text": "Solana Alpenglow activation completed. Lido becomes a validator."}, {"full_text": "Solana Alpenglow activation completed."}),
            ({"full_text": "Solana governance vote passed 42 to 15."}, {"full_text": "Solana governance vote passed 41 to 15."}),
            ({"full_text": "Solana Alpenglow deadline is Tuesday."}, {"full_text": "Solana Alpenglow deadline is Monday."}),
            ({"full_text": "Solana Alpenglow activation completed with Lido as a validator."}, {"full_text": "Solana Alpenglow activation completed."}),
        ]
        for current, prior in pairs:
            with self.subTest(current=current):
                result, _ = self.decide(source(**current), [primary(**prior)])
                self.assertFalse(result.reject)
                self.assertEqual(result.assessment, None)
                self.assertEqual(result.counters["material_updates_allowed"], 1)

    def test_same_url_or_provider_cannot_hide_material_numeric_delta(self):
        for identity in ({"news_url": "https://news.example/shared"}, {"news_id": "same-upstream-article"}):
            with self.subTest(identity=identity):
                current = source(title="Solana fund rises 20%", **identity)
                prior = primary(title="Solana fund rises 10%", **identity)
                assessment = assess_relationship(current, prior)
                self.assertEqual(assessment.assessment_type, AssessmentType.MATERIAL_UPDATE)
                self.assertIn("new_numeric_value", assessment.new_fact_codes)
                self.assertFalse(self.decide(current, [prior])[0].reject)

    def test_background_milestone_wording_is_not_a_new_completion_fact(self):
        prior = primary(full_text="Solana validators activate Alpenglow mainnet.")
        current = source(full_text="Solana validators activate Alpenglow mainnet. Validators call it a milestone.")
        result, _ = self.decide(current, [prior])
        self.assertTrue(result.reject)
        self.assertEqual(result.assessment.assessment_type, AssessmentType.SAME_EVENT_DUPLICATE)
        self.assertEqual(result.assessment.new_fact_codes, ())
        completion, _ = self.decide(source(title="Solana completes Alpenglow implementation"), [prior])
        self.assertFalse(completion.reject)
        self.assertEqual(completion.counters["material_updates_allowed"], 1)

    def test_same_url_or_provider_without_content_proof_is_not_exact_block(self):
        for identity in ({"news_url": "https://news.example/shared"}, {"news_id": "same-upstream-article"}):
            with self.subTest(identity=identity):
                self.assertFalse(self.decide(source(**identity), [primary(**identity)])[0].reject)

    def test_same_long_source_content_is_exact_block(self):
        body = "Solana Alpenglow activation has been confirmed for mainnet. " * 3
        result, _ = self.decide(source(full_text=body), [primary(full_text=body)])
        self.assertTrue(result.reject)
        self.assertEqual(result.counters["exact_blocks"], 1)

    def test_new_title_status_overrides_identical_source_body(self):
        body = "Solana Alpenglow activation was discussed by participants. " * 3
        result, _ = self.decide(source(title="Solana Alpenglow completed", full_text=body), [primary(title="Solana Alpenglow delayed", full_text=body)])
        self.assertFalse(result.reject)
        self.assertEqual(result.counters["material_updates_allowed"], 1)

    def test_invalid_or_placeholder_event_ids_never_authorize_block(self):
        for event_id in (None, "", " ", "0", 0, False, True, "null", "None", "unknown", "-1", "event\n7"):
            with self.subTest(event_id=event_id):
                self.assertFalse(valid_event_id(event_id))
                result, _ = self.decide(source(event_id=event_id), [primary(event_id=event_id)])
                self.assertFalse(result.reject)

    def test_inferred_same_event_without_equal_provider_event_is_not_blocked(self):
        current = source(event_id=None, title="SEC approves Bitcoin ETF on January 10 2026", full_text="Short summary.")
        prior = primary(event_id=None, title="Bitcoin ETF approved by SEC on January 10 2026", full_text="Short summary.")
        result, repo = self.decide(current, [prior])
        self.assertEqual(repo.saved[0][1][0][1].assessment_type, AssessmentType.SAME_EVENT_DUPLICATE)
        self.assertFalse(result.reject)

    def test_sparse_or_generic_asset_only_event_evidence_fails_open(self):
        for title in ("Market discussion", "Bitcoin market discussion"):
            with self.subTest(title=title):
                result, repo = self.decide(source(title=title, full_text="Brief source summary."), [primary(title=title, full_text="Brief source summary.")])
                self.assertFalse(result.reject)
                self.assertTrue(result.available)
                self.assertEqual(repo.saved[0][2]["candidate_audits"][1]["decision_reason"], "insufficient_structured_evidence")

    def test_specific_product_anchor_supports_valid_event_without_action(self):
        result, _ = self.decide(source(title="Solana Alpenglow mainnet", full_text="Brief summary."), [primary(title="Solana mainnet Alpenglow", full_text="Brief summary.")])
        self.assertTrue(result.reject)

    def test_related_regulatory_action_and_different_etf_dates_allowed(self):
        for now, before in (("SEC sues Coinbase", "SEC approves Coinbase"), ("Bitcoin ETF flows October 28 2026", "Bitcoin ETF flows October 27 2026")):
            with self.subTest(now=now):
                result, _ = self.decide(source(title=now, event_id=None), [primary(title=before, event_id=None)])
                self.assertFalse(result.reject)
                self.assertEqual(result.counters["related_events_allowed"], 1)

    def test_uncertain_and_negated_outcomes_fail_open(self):
        for title in ("Solana may activate Alpenglow", "Solana does not activate Alpenglow", "Solana Alpenglow reportedly activates", "Solana Alpenglow isn't online", "Solana Alpenglow isn\u2019t online", "Solana Alpenglow has yet to go online", "Solana Alpenglow activates tomorrow"):
            with self.subTest(title=title):
                result, repo = self.decide(source(title=title), [primary(title=title)])
                self.assertFalse(result.reject)
                self.assertTrue(repo.saved[0][1][0][1].evidence_uncertainty)

    def test_reversed_numeric_current_and_prior_roles_fail_open(self):
        current = source(title="JPMorgan reports Solana fund assets", full_text="Assets now total $200 million, previously $100 million.")
        prior = primary(title="JPMorgan reports Solana fund assets", full_text="Assets now total $100 million, previously $200 million.")
        result, repo = self.decide(current, [prior])
        self.assertFalse(result.reject)
        self.assertTrue(result.available)
        self.assertIn("unresolved_numeric_roles", repo.saved[0][1][0][1].evidence_uncertainty)

    def test_reversed_current_and_prior_deadline_roles_fail_open(self):
        for now, before in (("October 29 2026", "October 28 2026"), ("Tuesday", "Monday")):
            with self.subTest(now=now):
                current = source(full_text=f"Solana Alpenglow activates {now}, previously {before}.")
                prior = primary(full_text=f"Solana Alpenglow activates {before}, previously {now}.")
                result, repo = self.decide(current, [prior])
                self.assertFalse(result.reject)
                self.assertTrue(result.available)
                self.assertIn("unresolved_date_roles", repo.saved[0][1][0][1].evidence_uncertainty)

    def test_unknown_selected_appointed_or_elected_actor_fails_open(self):
        for status in ("selected", "appointed", "elected"):
            with self.subTest(status=status):
                current = source(full_text=f"Solana Alpenglow activation completed. Lido was {status} as a validator.")
                prior = primary(full_text="Solana Alpenglow activation completed.")
                result, repo = self.decide(current, [prior])
                self.assertFalse(result.reject)
                self.assertTrue(result.available)
                self.assertIn("unresolved_participant_selection", repo.saved[0][1][0][1].evidence_uncertainty)

    def test_reversed_legal_and_acquisition_participant_roles_fail_open(self):
        for now, before in (("SEC sues Coinbase over Solana trading", "Coinbase sues SEC over Solana trading"), ("Coinbase acquires Kraken", "Kraken acquires Coinbase")):
            with self.subTest(now=now):
                result, repo = self.decide(source(title=now), [primary(title=before)])
                self.assertFalse(result.reject)
                self.assertTrue(result.available)
                self.assertIn("unresolved_participant_roles", repo.saved[0][1][0][1].evidence_uncertainty)

    def test_fresh_source_reload_prevents_rewritten_evidence_authority(self):
        raw = source(event_id="different-event", title="Coinbase files court appeal")
        repo = MemoryRepository(raw, [primary()])
        result = assess_duplicate_enforcement(source(), stage="raw", mode="enforce", repository=repo)
        self.assertFalse(result.reject)

    def test_shadow_records_would_block_without_rejection(self):
        repo = MemoryRepository(source(), [primary()])
        result = assess_duplicate_enforcement(source(), stage="raw", mode="shadow", repository=repo)
        self.assertFalse(result.reject)
        self.assertEqual(result.canonical_raw_id, 1)
        self.assertTrue(repo.saved[0][2]["candidate_audits"][1]["confirmed_duplicate"])
        self.assertEqual(result.audit["decision"], "would_reject_duplicate")
        self.assertEqual(result.counters["shadow_would_reject_duplicates"], 1)
        self.assertEqual(result.counters["same_event_blocks"], 0)

    def test_fomc_shadow_and_enforce_share_classification_and_confirmation_at_both_stages(self):
        from tests.test_duplicate_cross_source import fomc_source_pair

        prior, current = fomc_source_pair()
        canonical = {**prior, "processed": 1, "canonical_rich_id": 301,
                     "canonical_wp_id": 901, "canonical_published": 1,
                     "canonical_publish_status": "published"}
        for stage in ("raw", "pre_publish"):
            with self.subTest(stage=stage), patch(
                "duplicate_detection.enforcement.assess_relationship", wraps=assess_relationship,
            ) as shared_policy:
                decisions = {}
                for mode in ("shadow", "enforce"):
                    repo = MemoryRepository(current, [canonical])
                    with self.assertLogs(level="INFO") as logs:
                        decisions[mode] = assess_duplicate_enforcement(
                            current, stage=stage, mode=mode, repository=repo,
                        )
                    evidence = decisions[mode].audit["candidate_audits"][prior["id"]]
                    self.assertEqual(evidence["event_identity_source"], "structured_cross_source")
                    self.assertTrue(evidence["confirmed_duplicate"])
                    self.assertFalse(evidence["same_event_id"])
                    self.assertEqual(evidence["contradiction_flags"], ())
                    self.assertEqual(evidence["new_fact_flags"], ())
                    self.assertGreaterEqual(len(evidence["confirmation_anchors"]), 2)
                    self.assertIn("confirmed_duplicate=true", " ".join(logs.output))
                    self.assertNotIn(current["full_text"], " ".join(logs.output))
                shadow, enforced = decisions["shadow"], decisions["enforce"]
                self.assertEqual(shared_policy.call_count, 2)
                self.assertEqual(shared_policy.call_args_list[0], shared_policy.call_args_list[1])
                self.assertEqual(shadow.assessment, enforced.assessment)
                self.assertFalse(shadow.reject)
                self.assertTrue(enforced.reject)
                self.assertEqual(shadow.audit["decision"], "would_reject_duplicate")
                self.assertEqual(enforced.audit["decision"], "reject_duplicate")
                self.assertEqual(shadow.counters["shadow_would_reject_duplicates"], 1)
                self.assertEqual(shadow.counters["same_event_blocks"], 0)
                self.assertEqual(enforced.counters["same_event_blocks"], 1)
                self.assertEqual(enforced.counters["shadow_would_reject_duplicates"], 0)
                self.assertEqual(shadow.counters["cross_source_duplicates_confirmed"], 1)

    def test_fomc_semantic_distance_only_expands_coverage_and_cannot_override_numeric_contradiction(self):
        from tests.test_duplicate_cross_source import fomc_source_pair

        prior, current = fomc_source_pair()
        canonical = {**prior, "processed": 1, "canonical_rich_id": 301,
                     "canonical_wp_id": 901, "canonical_published": 1,
                     "canonical_publish_status": "published"}
        for distance in (0.000001, 0.999):
            with self.subTest(distance=distance):
                result = assess_duplicate_enforcement(
                    current, stage="raw", mode="enforce", repository=MemoryRepository(current, [canonical]),
                    semantic_candidates=({"source_article_id": prior["id"], "native_distance": distance},),
                )
                self.assertTrue(result.reject)
                self.assertEqual(result.counters["semantic_candidates_promoted"], 1)
                self.assertEqual(result.audit["candidate_audits"][prior["id"]]["semantic_distance"], distance)
        changed = {**current, "full_text": current["full_text"] + " Bitcoin subsequently falls 15%."}
        result = assess_duplicate_enforcement(
            changed, stage="raw", mode="enforce", repository=MemoryRepository(changed, [canonical]),
            semantic_candidates=({"source_article_id": prior["id"], "native_distance": 0.000001},),
        )
        self.assertFalse(result.reject)
        self.assertEqual(result.counters["material_updates_allowed"], 1)
        self.assertEqual(result.counters["semantic_candidates_promoted"], 0)

    def test_shadow_material_update_logs_would_continue_with_fact_flags(self):
        current = source(title="Solana Alpenglow activation completed with 2000 transactions")
        repo = MemoryRepository(current, [primary(title="Solana Alpenglow activation completed with 1000 transactions")])
        with self.assertLogs(level="INFO") as logs:
            result = assess_duplicate_enforcement(current, stage="raw", mode="shadow", repository=repo)
        self.assertFalse(result.reject)
        self.assertEqual(result.audit["decision"], "would_continue")
        self.assertEqual(result.counters["shadow_would_reject_duplicates"], 0)
        self.assertEqual(result.counters["material_updates_allowed"], 1)
        self.assertIn("new_numeric_value", " ".join(logs.output))
        self.assertIn("decision=would_continue", " ".join(logs.output))

    def test_shadow_audit_failure_remains_nonblocking_and_sanitized(self):
        repo = MemoryRepository(source(), [primary()])
        repo.fail_save = True
        with self.assertLogs(level="WARNING") as logs:
            result = assess_duplicate_enforcement(source(), stage="pre_publish", mode="shadow", repository=repo)
        self.assertFalse(result.reject)
        self.assertFalse(result.available)
        self.assertEqual(result.counters["shadow_would_reject_duplicates"], 0)
        self.assertIn("decision=would_continue", " ".join(logs.output))
        self.assertNotIn("secret", " ".join(logs.output))

    def test_off_performs_no_reads_or_writes(self):
        repo = Mock()
        result = assess_duplicate_enforcement(source(), stage="raw", mode="off", repository=repo)
        self.assertEqual(result, DuplicateDecision(mode="off", article_id=10, counters=result.counters))
        self.assertEqual(repo.mock_calls, [])

    def test_read_and_audit_failure_are_unavailable_not_rejection_and_do_not_log_secrets(self):
        for failure in ("fail_read", "fail_save"):
            repo = MemoryRepository(source(), [primary()])
            setattr(repo, failure, True)
            with self.assertLogs(level="WARNING") as logs:
                result = assess_duplicate_enforcement(source(), stage="raw", mode="enforce", repository=repo)
            self.assertFalse(result.reject)
            self.assertFalse(result.available)
            self.assertEqual(result.error, "RuntimeError")
            self.assertNotIn("secret", " ".join(logs.output))

    def test_selected_unprocessed_and_terminal_duplicates_never_anchor(self):
        rows = [source(1, chosen_for_publish=1, processed=0), primary(2, processing_status="duplicate"), primary(3, canonical_publish_status="duplicate")]
        result, repo = self.decide(candidates=rows)
        self.assertFalse(result.reject)
        self.assertEqual(repo.saved[0][1], [])

    def test_unpublished_rich_primary_only_blocks_raw_not_pre_publish(self):
        row = primary(canonical_wp_id=None, canonical_published=0, canonical_publish_status="pending")
        repo = MemoryRepository(source(), [row])
        self.assertTrue(assess_duplicate_enforcement(source(), stage="raw", mode="enforce", repository=repo).reject)
        self.assertFalse(assess_duplicate_enforcement(source(), stage="pre_publish", mode="enforce", repository=repo).reject)

    def test_prior_unresolved_external_attempt_defers_without_duplicate_rejection(self):
        for status in ("claimed", "retryable", "manual_review"):
            with self.subTest(status=status):
                row = primary(processed=0, canonical_wp_id=None, canonical_published=0, canonical_publish_status=status, canonical_publish_attempt_count=1)
                repo = MemoryRepository(source(), [row])
                result = assess_duplicate_enforcement(source(), stage="pre_publish", mode="enforce", repository=repo)
                self.assertFalse(result.reject)
                self.assertFalse(result.available)
                self.assertEqual(result.error, "prior_publication_unresolved")
                self.assertEqual(result.canonical_raw_id, 1)
                self.assertEqual(result.canonical_rich_id, 101)
                self.assertEqual(repo.saved[0][2]["decision"], "defer_manual_review")

    def test_prior_unresolved_attempt_does_not_defer_material_update(self):
        row = primary(canonical_wp_id=None, canonical_published=0, canonical_publish_status="retryable", canonical_publish_attempt_count=1)
        repo = MemoryRepository(source(title="Solana Alpenglow completed"), [row])
        result = assess_duplicate_enforcement(source(), stage="pre_publish", mode="enforce", repository=repo)
        self.assertFalse(result.reject)
        self.assertTrue(result.available)
        self.assertEqual(result.counters["material_updates_allowed"], 1)

    def test_published_match_takes_precedence_over_unresolved_attempt(self):
        unresolved = primary(1, canonical_wp_id=None, canonical_published=0, canonical_publish_status="manual_review", canonical_publish_attempt_count=1)
        repo = MemoryRepository(source(), [unresolved, primary(2)])
        result = assess_duplicate_enforcement(source(), stage="pre_publish", mode="enforce", repository=repo)
        self.assertTrue(result.reject)
        self.assertTrue(result.available)
        self.assertEqual(result.canonical_raw_id, 2)

    def test_unattempted_claimed_peer_is_not_pre_publish_authority(self):
        row = primary(canonical_wp_id=None, canonical_published=0, canonical_publish_status="claimed", canonical_publish_attempt_count=0)
        repo = MemoryRepository(source(), [row])
        result = assess_duplicate_enforcement(source(), stage="pre_publish", mode="enforce", repository=repo)
        self.assertFalse(result.reject)
        self.assertTrue(result.available)

    def test_shadow_unresolved_attempt_remains_available(self):
        row = primary(canonical_wp_id=None, canonical_published=0, canonical_publish_status="manual_review", canonical_publish_attempt_count=1)
        repo = MemoryRepository(source(), [row])
        result = assess_duplicate_enforcement(source(), stage="pre_publish", mode="shadow", repository=repo)
        self.assertTrue(result.available)
        self.assertFalse(result.reject)

    def test_canonical_published_then_earliest_and_self_exclusion(self):
        rows = [primary(3), primary(2, publish_date="2026-10-27 12:00:00"), primary(1, canonical_wp_id=None, canonical_published=0, canonical_publish_status="pending"), primary(10)]
        result, _ = self.decide(candidates=list(reversed(rows)))
        self.assertEqual(result.canonical_raw_id, 2)

    def test_audit_and_log_include_stage_identity_canonical_and_fact_delta_no_body(self):
        with self.assertLogs(level="INFO") as logs:
            result, repo = self.decide()
        audit = repo.saved[0][2]
        evidence = audit["candidate_audits"][1]
        self.assertEqual(audit["stage"], "raw")
        self.assertEqual(audit["decision"], "reject_duplicate")
        self.assertEqual(evidence["event_id"], "event-700")
        self.assertEqual(evidence["canonical_wp_id"], 901)
        self.assertEqual(evidence["new_fact_codes"], ())
        self.assertEqual(result.counters["same_event_blocks"], 1)
        self.assertNotIn(source()["full_text"], " ".join(logs.output))


class EnforcementRepositoryTests(unittest.TestCase):
    def repository(self):
        cursor = Mock()
        cursor.fetchall.return_value = []
        connection = Mock()
        connection.cursor.return_value = cursor
        return DuplicateAssessmentRepository(connect=lambda: connection), connection, cursor

    def test_source_read_has_bound_raw_identity(self):
        repo, _, cursor = self.repository()
        cursor.fetchone.return_value = source()
        self.assertEqual(repo.load_source_article(10), source())
        self.assertEqual(cursor.execute.call_args.args[1], (10,))

    def test_credible_candidate_query_excludes_selected_only_and_duplicate_rows(self):
        repo, _, cursor = self.repository()
        repo.load_enforcement_candidates(10, lookback_hours=72, stage="raw", source_article_ids=(2, 2, 3))
        sql, params = cursor.execute.call_args.args
        self.assertIn("JOIN rich_crpytonews", sql)
        self.assertIn("c.processed = 1", sql)
        self.assertIn("<> 'duplicate'", sql)
        self.assertNotIn("chosen_for_publish = 1", sql)
        self.assertIn("r.raw_article_id IS NULL AND r.news_url = c.news_url", sql)
        self.assertEqual(params, (10, 72, 2, 3, 200))

    def test_pre_publish_query_requires_external_post_outcome(self):
        repo, _, cursor = self.repository()
        repo.load_enforcement_candidates(10, lookback_hours=72, stage="pre_publish")
        sql = cursor.execute.call_args.args[0]
        self.assertIn("AND (r.wp_post_id > 0 OR r.published = 1 OR r.publish_status IN ('published', 'post_created')", sql)
        self.assertIn("r.publish_attempt_count > 0", sql)
        self.assertIn("('claimed', 'retryable', 'manual_review')", sql)
        self.assertNotIn("AND (c.processed = 1", sql)

    def test_old_shadow_json_is_unchanged_and_enforcement_adds_audit_without_schema(self):
        repo, _, cursor = self.repository()
        assessment = assess_relationship(source(), primary())
        repo.save_assessments(10, [(1, assessment)])
        old = json.loads(cursor.executemany.call_args.args[1][0][12])
        self.assertEqual(old, {"codes": list(assessment.reason_codes)})
        repo.save_assessments(10, [(1, assessment)], audit_context={"stage": "raw", "mode": "enforce", "candidate_audits": {1: {"canonical_wp_id": 901}}})
        new = json.loads(cursor.executemany.call_args.args[1][0][12])
        self.assertEqual(new["enforcement"], {"stage": "raw", "mode": "enforce", "canonical_wp_id": 901})


if __name__ == "__main__":
    unittest.main()
