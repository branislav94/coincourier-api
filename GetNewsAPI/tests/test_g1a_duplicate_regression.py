"""Meaningful offline decision contracts; no historical accuracy claim."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .duplicate_regression_support import (
    MemoryAssessmentRepository, canonical_json, evaluate_corpus, forbid_external_io,
    input_sha256, load_corpus, render_report,
)


class CorpusIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.corpus = load_corpus()

    def invalid_fixture(self, change):
        value = deepcopy(self.corpus)
        change(value)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid-corpus.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_corpus(path)

    def test_populations_and_missing_history_are_explicit(self):
        populations = {case["population"] for case in self.corpus["cases"]}
        self.assertEqual(populations, {"synthetic_contract", "reconstructed_baseline", "operator_FINAL_minimal_analogue"})
        self.assertEqual(self.corpus["historical_readiness"]["independent_historical_raw_pairs"], 0)
        self.assertTrue(all(case["historical_accuracy_eligible"] is False for case in self.corpus["cases"]))

    def test_missing_production_inputs_and_measured_exception_stay_quarantined(self):
        quarantined = {item["case_id"]: item for item in self.corpus["quarantine"]}
        observation = quarantined["Q01_observed_production_34778_34388"]
        self.assertEqual(observation["observed_classification"], "related_event")
        self.assertIsNone(observation["semantic_reference"])
        self.assertIsNone(observation["inputs"])
        exception = quarantined["Q02_alpenglow_measured_finality_6344"]
        self.assertEqual(exception["semantic_reference"], "material_update")
        self.assertIsNone(exception["inputs"])
        family = next(item for item in self.corpus["final_family_label_registry"] if item["family"] == "alpenglow_mainnet_activation")
        self.assertNotIn(6344, family["wp_ids"])
        self.assertIn(6344, family["excluded_material_exception_wp_ids"])

    def test_frozen_input_hashes_preserve_values_and_types(self):
        for case in self.corpus["cases"]:
            for side in ("prior", "current"):
                self.assertEqual(case["input_sha256"][side], input_sha256(case["inputs"][side]))
        self.assertNotEqual(input_sha256({"id": 1}), input_sha256({"id": "1"}))
        self.assertNotEqual(input_sha256({"event_id": None}), input_sha256({}))

    def test_changed_unreviewed_or_falsely_historical_inputs_are_rejected(self):
        self.invalid_fixture(lambda value: value["cases"][0]["inputs"]["current"].update(title="Changed after review"))
        self.invalid_fixture(lambda value: value["cases"][0]["semantic_reference"].update(review_status="pending"))
        self.invalid_fixture(lambda value: value["cases"][0].update(historical_accuracy_eligible=True))
        self.invalid_fixture(lambda value: value["quarantine"][0].update(inputs={"current": {}}))
        self.invalid_fixture(lambda value: value["cases"][0]["inputs"]["current"].update(id=True))

    def test_unrelated_mapping_and_unknown_granularity_are_explicit(self):
        self.assertEqual(self.corpus["native_relation_mapping"]["unrelated"], "broad_topic_overlap")
        case = next(item for item in self.corpus["cases"] if item["case_id"] == "F06_tianjin_distinct_from_uk_qian")
        self.assertEqual(case["semantic_reference"]["relation"], "insufficient_evidence")
        self.assertFalse(case["semantic_reference"]["duplicate_truth"])
        self.assertNotIn("insufficient_evidence", self.corpus["native_relation_mapping"])


class OfflineDecisionCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_corpus()
        cls.report = evaluate_corpus(cls.corpus)

    def row(self, scenario, case="S02_same_event_paraphrase"):
        return next(item for item in self.report["scenarios"] if item["case_id"] == case and item["scenario_id"] == scenario)

    def test_report_is_byte_deterministic_without_mutating_inputs(self):
        before = canonical_json(self.corpus)
        repeated = render_report(evaluate_corpus(self.corpus))
        first = render_report(self.report)
        self.assertEqual(first, repeated)
        self.assertEqual(hashlib.sha256(first.encode()).hexdigest(), hashlib.sha256(repeated.encode()).hexdigest())
        self.assertEqual(before, canonical_json(self.corpus))

    def test_no_quality_accuracy_is_fabricated_from_contract_cases(self):
        self.assertEqual(self.report["historical_accuracy"]["eligible_pairs"], 0)
        self.assertEqual(self.report["historical_accuracy"]["status"], "unavailable")
        self.assertNotIn("accuracy", self.report)
        self.assertNotIn("precision", self.report)
        self.assertEqual(len(self.report["quarantine"]), 2)

    def test_reviewed_confirmation_contracts_remain_satisfied(self):
        expected_by_case = {
            case["case_id"]: case["expected_contract_confirmation"]
            for case in self.corpus["cases"]
        }
        self.assertCountEqual(
            [result["case_id"] for result in self.report["cases"]],
            list(expected_by_case),
        )
        for result in self.report["cases"]:
            case_id = result["case_id"]
            with self.subTest(case_id=case_id):
                expected = expected_by_case[case_id]
                self.assertEqual(result["expected_contract_confirmation"], expected)
                self.assertEqual(result["confirmed_duplicate"], expected)
                self.assertFalse(result["contract_confirmation_mismatch"])

    def test_gap_reporting_preserves_reviewed_references(self):
        references = {case["case_id"]: case["semantic_reference"]["relation"] for case in self.corpus["cases"]}
        for result in self.report["cases"]:
            self.assertEqual(result["semantic_reference"], references[result["case_id"]])
            expected = result["native_reference"]
            self.assertEqual(result["semantic_classification_alignment_gap"], None if expected is None else result["classification"] != expected)
            self.assertEqual(result["contract_confirmation_mismatch"], result["confirmed_duplicate"] != result["expected_contract_confirmation"])
        unknown = next(item for item in self.report["cases"] if item["case_id"] == "F06_tianjin_distinct_from_uk_qian")
        self.assertIsNone(unknown["semantic_classification_alignment_gap"])
        self.assertFalse(unknown["confirmed_duplicate"])

    def test_semantic_alignment_is_separate_from_reviewed_contract_confirmation(self):
        expected_true = {"S01_exact_source_copy", "S02_same_event_paraphrase", "R01_existing_fomc_reconstruction"}
        for result in self.report["cases"]:
            self.assertEqual(result["expected_contract_confirmation"], result["case_id"] in expected_true)
        sparse = next(item for item in self.report["cases"] if item["case_id"] == "S07_same_event_missing_identity")
        self.assertTrue(sparse["duplicate_truth"])
        self.assertFalse(sparse["expected_contract_confirmation"])
        self.assertTrue(sparse["semantic_confirmation_alignment_gap"])
        self.assertFalse(sparse["contract_confirmation_mismatch"])

    def test_shadow_and_off_never_reject_or_request_disposition(self):
        for row in self.report["scenarios"]:
            if row["mode"] in {"shadow", "off"}:
                self.assertFalse(row["reject"], row["scenario_id"])
                self.assertEqual(row["disposition"]["intent"], "none")
                self.assertEqual(row["fake_publication_lock_acquisitions"], 0)
        off = self.row("off_raw")
        self.assertEqual(off["repository_source_reloads"], 0)
        self.assertIsNone(off["classification"])
        self.assertIsNone(off["confirmed_duplicate"])

    def test_rejection_requires_confirmation_available_audit_and_canonical(self):
        rejected = [row for row in self.report["scenarios"] if row["reject"]]
        self.assertTrue(rejected)
        for row in rejected:
            self.assertEqual(row["mode"], "enforce")
            self.assertTrue(row["available"])
            self.assertTrue(row["confirmed_duplicate"])
            self.assertTrue(row["audit_persisted"])
            self.assertIsNotNone(row["canonical_raw_id"])
            self.assertIsNotNone(row["canonical_rich_id"])

    def test_material_update_and_distinct_event_controls_are_allowed(self):
        for case in ("S03_material_new_assets", "S04_distinct_regulatory_action", "S05_broad_topic_analysis", "S06_unrelated_topics", "F06_tianjin_distinct_from_uk_qian"):
            for scenario in ("enforce_raw_published", "enforce_pre_publish_published"):
                row = self.row(scenario, case)
                self.assertFalse(row["reject"], case)
                self.assertEqual(row["disposition"]["intent"], "none")

    def test_mutable_identity_and_uncertainty_do_not_become_content_proof(self):
        mutable = next(item for item in self.report["cases"] if item["case_id"] == "S08_mutable_identity_without_content_proof")
        self.assertEqual(mutable["semantic_reference"], "same_event_duplicate")
        self.assertFalse(mutable["confirmed_duplicate"])
        uncertain = next(item for item in self.report["cases"] if item["case_id"] == "S09_identical_uncertain_source")
        self.assertEqual(uncertain["semantic_reference"], "exact_duplicate")
        if uncertain["uncertainty"]:
            self.assertFalse(uncertain["confirmed_duplicate"])

    def test_ineligible_candidates_are_not_scored_as_confirmed_negatives(self):
        for scenario in ("enforce_raw_selected_only", "enforce_raw_terminal_duplicate", "enforce_pre_publish_terminal_duplicate", "enforce_pre_publish_unattempted_claim"):
            row = self.row(scenario)
            self.assertFalse(row["assessment_evidence_observed"])
            self.assertFalse(row["eligible_observed"])
            self.assertIsNone(row["confirmed_duplicate"])
            self.assertFalse(row["reject"])

    def test_stage_distinguishes_processed_primary_and_external_publication(self):
        self.assertTrue(self.row("enforce_raw_processed_unpublished")["reject"])
        self.assertFalse(self.row("enforce_pre_publish_processed_unpublished")["reject"])
        self.assertFalse(self.row("enforce_pre_publish_processed_unpublished")["assessment_evidence_observed"])

    def test_unresolved_prior_publication_defers_without_terminal_rejection(self):
        row = self.row("enforce_pre_publish_unresolved")
        self.assertFalse(row["reject"])
        self.assertFalse(row["available"])
        self.assertTrue(row["confirmed_duplicate"])
        self.assertEqual(row["article_decision"], "defer_manual_review")
        self.assertEqual(row["disposition"]["intent"], "request_manual_reconciliation")
        shadow = self.row("shadow_pre_publish_unresolved")
        self.assertTrue(shadow["available"])
        self.assertFalse(shadow["reject"])

    def test_audit_failure_is_not_duplicate_rejection(self):
        for scenario in ("enforce_raw_audit_failure", "enforce_pre_publish_audit_failure", "enforce_raw_incomplete_audit"):
            row = self.row(scenario)
            self.assertFalse(row["available"])
            self.assertFalse(row["reject"])
            self.assertFalse(row["audit_persisted"])
            self.assertEqual(row["counters"]["exact_blocks"] + row["counters"]["same_event_blocks"], 0)
        self.assertEqual(self.row("enforce_raw_audit_failure")["article_decision"], "continue_unavailable")
        self.assertEqual(self.row("enforce_pre_publish_audit_failure")["article_decision"], "defer_assessment_unavailable")

    def test_disposition_intents_do_not_claim_database_or_ownership_execution(self):
        self.assertEqual(self.report["external_io_attempts"], 0)
        self.assertEqual(self.report["database_state_changes"], 0)
        for row in self.report["scenarios"]:
            self.assertFalse(row["disposition"]["executed"])
            self.assertFalse(row["disposition"]["ownership_verified"])
            self.assertEqual(row["disposition"]["database_state_changes"], 0)

    def test_actual_engine_reloads_source_and_chooses_published_canonical(self):
        from duplicate_detection.enforcement import assess_duplicate_enforcement
        import config

        case = next(item for item in self.corpus["cases"] if item["case_id"] == "S02_same_event_paraphrase")
        current = deepcopy(case["inputs"]["current"])
        published = {**deepcopy(case["inputs"]["prior"]), **self.corpus["candidate_contexts"]["published"]}
        unpublished = {**deepcopy(published), **self.corpus["candidate_contexts"]["processed_unpublished"], "id": 9001, "canonical_rich_id": 9002}
        with forbid_external_io(), patch.multiple(config, DUPLICATE_LOOKBACK_HOURS=72, DUPLICATE_POLICY_VERSION="v1"):
            for candidates in ([unpublished, published], [published, unpublished]):
                repository = MemoryAssessmentRepository(current, candidates)
                outcome = assess_duplicate_enforcement({"id": current["id"], "title": "Changed rewritten caller text"},
                    stage="raw", mode="enforce", repository=repository)
                self.assertTrue(outcome.reject)
                self.assertEqual(outcome.canonical_raw_id, published["id"])
                self.assertEqual(repository.loads[0], ("source", current["id"]))


if __name__ == "__main__":
    unittest.main()
