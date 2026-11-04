"""Synthetic detector contracts, not independently labelled editorial accuracy."""

from __future__ import annotations

import logging
import unittest
from unittest.mock import Mock, patch

from GetNewsAPI import public_copy_gate as gate


class PublicCopyDetectorTests(unittest.TestCase):
    def test_explicit_workflow_references_are_candidates_for_all_five_phrases(self):
        examples = {
            "generation_time": "The generation time of this draft is recorded in its prompt.",
            "reporting_cutoff": "This draft uses a reporting cutoff of 08:00 UTC.",
            "supplied_evidence": "The supplied evidence in this prompt does not identify a buyer.",
            "available_evidence": "The available evidence provided to this model omits a later filing.",
            "reviewed_coverage": "The reviewed coverage supplied to the writer omits the new filing.",
        }
        for rule, sentence in examples.items():
            with self.subTest(rule=rule):
                result = gate.assess_public_copy(f"<p>{sentence}</p>")
                self.assertEqual(result.status, "evaluated")
                self.assertEqual(result.findings, (
                    gate.PublicCopyFinding(rule, "workflow_reference_candidate"),
                ))

    def test_ordinary_reporting_and_technical_uses_remain_ambiguous(self):
        sentences = (
            "The software reduced key generation time.",
            "The company set its quarterly reporting cutoff at September 30.",
            "The regulator supplied evidence to the court.",
            "The available evidence links the transfers to one address.",
            "The committee reviewed coverage of rural broadband deployments.",
        )
        result = gate.assess_public_copy("".join(f"<p>{sentence}</p>" for sentence in sentences))
        self.assertEqual({finding.rule for finding in result.findings}, {rule for rule, _ in gate.PHRASES})
        self.assertTrue(all(finding.kind == "ambiguous_candidate" for finding in result.findings))

    def test_quotation_presentation_abstains_even_with_workflow_markers(self):
        examples = (
            '<p>The witness said, "The supplied evidence in this prompt is incomplete."</p>',
            '<p>The witness said, “The supplied evidence in this prompt is incomplete.”</p>',
            "<p>The witness said, 'The company's supplied evidence in this prompt is incomplete.'</p>",
            '<p>The witness said, <q>The supplied evidence in this prompt is incomplete.</q></p>',
            '<blockquote><p>The supplied evidence in this prompt is incomplete.</p></blockquote>',
        )
        for html in examples:
            with self.subTest(html=html):
                self.assertEqual(gate.assess_public_copy(html).findings, (
                    gate.PublicCopyFinding("supplied_evidence", "quoted_candidate"),
                ))

    def test_quote_state_does_not_leak_out_of_a_blockquote(self):
        result = gate.assess_public_copy(
            '<blockquote><p>The supplied evidence in this prompt is incomplete.</p></blockquote>'
            '<p>The supplied evidence in this prompt is incomplete.</p>'
        )
        self.assertEqual([finding.kind for finding in result.findings], [
            "quoted_candidate", "workflow_reference_candidate",
        ])

    def test_nested_same_name_quotes_keep_outer_quote_protection(self):
        result = gate.assess_public_copy(
            '<p><q><q>A witness spoke.</q> The supplied evidence in this prompt is incomplete.</q></p>'
        )
        self.assertEqual(result.findings[0].kind, "quoted_candidate")

    def test_unmatched_opening_quote_is_ambiguous_not_workflow_confirmation(self):
        result = gate.assess_public_copy('<p>“The supplied evidence in this prompt is incomplete.</p>')
        self.assertEqual(result.findings[0].kind, "ambiguous_candidate")

    def test_case_entities_whitespace_and_inline_markup_preserve_visible_phrase(self):
        for html in (
            '<p>GENERATION&nbsp;<strong>TIME</strong> for this draft.</p>',
            '<p>generation\n\t time for this draft.</p>',
            '<p>generation<br>time for this draft.</p>',
            '<p>generation <a href="https://example.invalid">time</a> for this draft.</p>',
        ):
            with self.subTest(html=html):
                self.assertEqual(gate.assess_public_copy(html).findings, (
                    gate.PublicCopyFinding("generation_time", "workflow_reference_candidate"),
                ))

    def test_entities_are_decoded_once(self):
        self.assertEqual(gate.assess_public_copy('<p>generation&amp;nbsp;time</p>').findings, ())

    def test_phrases_are_not_manufactured_across_blocks_or_code(self):
        for html in (
            '<p>generation</p><p>time</p>',
            '<div>generation</div><div>time</div>',
            '<p>generation<code>ordinary visible example</code> time</p>',
        ):
            with self.subTest(html=html):
                self.assertEqual(gate.assess_public_copy(html).findings, ())

    def test_hidden_markup_attributes_urls_and_code_are_outside_body_text_scope(self):
        html = (
            '<!-- generation time -->'
            '<script type="application/ld+json">{"description":"generation time"}</script>'
            '<style>.generation time {}</style><noscript>generation time</noscript>'
            '<template><p>generation time</p></template>'
            '<pre>generation time</pre><code>generation time</code>'
            '<p title="generation time"><a href="https://example.invalid/generation time">Source</a></p>'
        )
        self.assertEqual(gate.assess_public_copy(html).findings, ())

    def test_nested_same_name_code_remains_excluded(self):
        self.assertEqual(gate.assess_public_copy(
            '<code><code>Example.</code> The supplied evidence in this prompt is incomplete.</code>'
        ).findings, ())

    def test_word_boundaries_do_not_match_longer_words(self):
        self.assertEqual(gate.assess_public_copy(
            '<p>Regeneration time, generation times, unavailable evidence and available evidences.</p>'
        ).findings, ())

    def test_context_does_not_cross_clause_or_block_boundaries(self):
        for html in (
            '<p>This draft is complete. The available evidence supports the claim.</p>',
            '<p>This draft is complete; the available evidence supports the claim.</p>',
            '<p>This draft is complete.</p><p>The available evidence supports the claim.</p>',
        ):
            with self.subTest(html=html):
                self.assertEqual(gate.assess_public_copy(html).findings[0].kind, "ambiguous_candidate")

    def test_unsupported_and_oversized_input_are_not_editorial_failures(self):
        for value in (None, 42, object()):
            self.assertEqual(gate.assess_public_copy(value).status, "not_evaluated_invalid_input")
        self.assertEqual(gate.assess_public_copy("x" * (gate.MAX_HTML_CHARACTERS + 1)).status,
                         "not_evaluated_oversize")

    def test_parser_failure_is_reported_without_private_exception_text(self):
        with patch.object(gate._VisibleBlocks, "feed", side_effect=ValueError("PRIVATE_BODY_CANARY")):
            result = gate.assess_public_copy('<p>Available evidence.</p>')
        self.assertEqual(result, gate.PublicCopyAssessment("not_evaluated_parse_error"))
        self.assertNotIn("PRIVATE_BODY_CANARY", repr(result))

    def test_many_matches_have_explicitly_bounded_findings(self):
        result = gate.assess_public_copy('<p>' + 'available evidence ' * (gate.MAX_FINDINGS + 1) + '</p>')
        self.assertEqual(len(result.findings), gate.MAX_FINDINGS)
        self.assertTrue(result.truncated)


class PublicCopyObservationTests(unittest.TestCase):
    def test_off_or_unsupported_mode_never_evaluates_or_logs(self):
        for mode in ("off", "enforce", "unexpected"):
            with self.subTest(mode=mode), patch.object(gate, "assess_public_copy") as evaluate:
                logger = Mock()
                self.assertIsNone(gate.observe_public_copy("private", mode=mode, stage="pre_storage", logger=logger))
                evaluate.assert_not_called()
                logger.info.assert_not_called()
                logger.warning.assert_not_called()

    def test_shadow_returns_metadata_only_and_does_not_log_copy_or_untrusted_identifiers(self):
        html = '<p>The supplied evidence in this prompt omits PRIVATE_BODY_CANARY.</p>'
        with self.assertLogs(gate.__name__, level=logging.INFO) as logs:
            result = gate.observe_public_copy(
                html, mode="shadow", stage="PRIVATE_STAGE_CANARY",
                raw_article_id="PRIVATE_ID_CANARY", rich_article_id=True,
            )
        output = "\n".join(logs.output)
        self.assertIn("stage=unknown", output)
        self.assertIn("raw_article_id=None rich_article_id=None", output)
        self.assertIn("decision=continue", output)
        for canary in ("PRIVATE_BODY_CANARY", "PRIVATE_STAGE_CANARY", "PRIVATE_ID_CANARY", html):
            self.assertNotIn(canary, output)
            self.assertNotIn(canary, repr(result))

    def test_detector_and_logger_failures_are_fail_open_without_exception_payload(self):
        logger = Mock()
        with patch.object(gate, "assess_public_copy", side_effect=ValueError("PRIVATE_ERROR_CANARY")):
            self.assertIsNone(gate.observe_public_copy("private", mode="shadow", stage="pre_storage", logger=logger))
        self.assertNotIn("PRIVATE_ERROR_CANARY", repr(logger.mock_calls))
        logger.info.side_effect = ValueError("PRIVATE_ERROR_CANARY")
        logger.warning.side_effect = ValueError("PRIVATE_ERROR_CANARY")
        self.assertIsNone(gate.observe_public_copy("<p>Available evidence.</p>", mode="shadow", stage="pre_create", logger=logger))


if __name__ == "__main__":
    unittest.main()
