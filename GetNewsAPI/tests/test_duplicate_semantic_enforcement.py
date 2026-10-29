"""Semantic matches find source rows; fresh deterministic facts make decisions."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from duplicate_detection.enforcement import assess_duplicate_enforcement
from semantic_retrieval.models import (
    SemanticCandidate, SemanticRetrievalResult, SemanticRetrievalSettings, SemanticRetrievalStatus,
)
from semantic_retrieval.shadow import run_semantic_shadow
from vector_store.models import SourceType
from vector_store.semantic_assessments import SemanticAssessmentStatus


NOW = datetime(2026, 10, 28, 12)
VERSION = "openai:text-embedding-3-small:1536:chunk-v1"


def source(article_id, *, prior=False, event_id="alpenglow-activation-20261028"):
    return {
        "id": article_id, "news_id": f"provider-item-{article_id}", "event_id": event_id,
        "news_url": f"https://source.example/{article_id}",
        "title": "Solana validators activate Alpenglow mainnet" if prior else "Solana Alpenglow mainnet activation",
        "full_text": (
            "Solana validators activate Alpenglow mainnet on October 28, 2026 with 1000 transactions. "
            "The protocol milestone is confirmed by validators."
            if prior else
            "Alpenglow mainnet activation on Solana takes place on October 28, 2026 with 1000 transactions. "
            "Validators confirm the protocol milestone."
        ),
        "source_name": "Independent source", "publish_date": NOW - timedelta(hours=1 if prior else 0),
        "processed": int(prior), "processing_status": "completed" if prior else "pending",
        **({"canonical_rich_id": 301, "canonical_wp_id": 501,
            "canonical_wp_url": "https://coincourier.example/canonical/",
            "canonical_published": 1, "canonical_publish_status": "published"} if prior else {}),
    }


def semantic_candidate(*, distance=0.001, title="Vector-only title, deliberately not evidence"):
    return SemanticCandidate(
        query_document_id=10, query_source_article_id=100,
        candidate_document_id=20, candidate_source_article_id=201,
        candidate_document_key="source_article:201", candidate_source_url="https://stale.example/vector",
        candidate_title=title, native_distance=distance, embedding_version=VERSION,
        best_query_chunk_index=0, best_candidate_chunk_index=0, matched_query_chunk_count=1,
        published_at=NOW - timedelta(hours=1), publication_delta_hours=1,
        source_type=SourceType.SOURCE_ARTICLE,
    )


class SourceRepository:
    def __init__(self, current, prior, *, direct=False):
        self.current, self.prior, self.direct = deepcopy(current), deepcopy(prior), direct
        self.source_reads, self.candidate_requests, self.saved = [], [], []

    def load_source_article(self, raw_id):
        self.source_reads.append(raw_id)
        return deepcopy(self.current) if raw_id == self.current["id"] else None

    def load_enforcement_candidates(self, article_id, *, lookback_hours, stage, source_article_ids=()):
        self.candidate_requests.append((article_id, stage, tuple(source_article_ids)))
        return [deepcopy(self.prior)] if self.direct or self.prior["id"] in source_article_ids else []

    def save_assessments(self, article_id, assessments, *, audit_context):
        self.saved.append((article_id, list(assessments), deepcopy(audit_context)))
        return len(self.saved[-1][1])


class SemanticConfirmationIntegrationTests(unittest.TestCase):
    def assess(self, current, prior, *, candidates=(), direct=False, stage="raw", supplied=None):
        repo = SourceRepository(current, prior, direct=direct)
        with patch("embeddings.provider.OpenAIEmbeddingProvider") as provider:
            result = assess_duplicate_enforcement(
                supplied or current, stage=stage, mode="enforce", repository=repo,
                semantic_candidates=candidates,
            )
        provider.assert_not_called()
        self.assertTrue(result.available)
        self.assertEqual(repo.source_reads, [100])
        return result, repo

    def test_nearest_vector_different_actual_event_remains_allowed(self):
        current, prior = source(100), source(201, prior=True, event_id="different-event")
        prior.update(title="Solana validators file a regulatory request",
                     full_text="Solana validators file a regulatory request with a separate court on October 27, 2026.")
        result, repo = self.assess(current, prior, candidates=(semantic_candidate(distance=0.000001),))
        self.assertFalse(result.reject)
        self.assertEqual(result.counters["semantic_candidates_evaluated"], 1)
        self.assertEqual(result.counters["semantic_candidates_promoted"], 0)
        self.assertEqual(repo.candidate_requests, [(100, "raw", (201,))])
        self.assertEqual(result.audit["candidate_audits"][201]["semantic_distance"], 0.000001)

    def test_fresh_source_same_event_confirms_even_distant_misleading_vector_title(self):
        current, prior = source(100), source(201, prior=True)
        result, repo = self.assess(current, prior, candidates=(semantic_candidate(distance=0.999),),
                                  supplied={"id": 100, "title": "Untrusted rewritten title", "full_text": "Ignore this rewritten body"})
        self.assertTrue(result.reject)
        self.assertTrue(result.assessment.same_event_id)
        self.assertIn("no_new_structured_facts", result.assessment.reason_codes)
        self.assertEqual(result.canonical_raw_id, 201)
        self.assertEqual(result.canonical_rich_id, 301)
        self.assertEqual(result.canonical_wp_id, 501)
        self.assertEqual(result.counters["semantic_candidates_promoted"], 1)
        self.assertEqual(repo.saved[0][0], 100)

    def test_fresh_numeric_update_remains_allowed_despite_same_event_near_vector(self):
        current, prior = source(100), source(201, prior=True)
        current["full_text"] = current["full_text"].replace("1000 transactions", "2000 transactions")
        result, _repo = self.assess(current, prior, candidates=(semantic_candidate(distance=0.000001),))
        self.assertFalse(result.reject)
        self.assertEqual(result.counters["material_updates_allowed"], 1)
        self.assertEqual(result.counters["semantic_candidates_promoted"], 0)
        self.assertIn("new_numeric_value", result.audit["candidate_audits"][201]["new_fact_codes"])

    def test_strong_structured_same_event_without_provider_id_is_confirmed_after_vector_retrieval(self):
        current, prior = source(100, event_id=None), source(201, prior=True, event_id=None)
        result, _repo = self.assess(current, prior, candidates=(semantic_candidate(distance=0.000001),))
        self.assertTrue(result.reject)
        self.assertFalse(result.audit["candidate_audits"][201]["same_event_id"])
        self.assertEqual(result.audit["candidate_audits"][201]["event_identity_source"], "structured_cross_source")
        self.assertEqual(result.counters["semantic_candidates_promoted"], 1)

    def test_missing_vectors_do_not_disable_direct_duplicate_confirmation(self):
        current, prior = source(100), source(201, prior=True)
        result, repo = self.assess(current, prior, candidates=(), direct=True)
        self.assertTrue(result.reject)
        self.assertEqual(result.counters["same_event_blocks"], 1)
        self.assertEqual(result.counters["semantic_candidates_evaluated"], 0)
        self.assertEqual(repo.candidate_requests, [(100, "raw", ())])

    def test_vector_match_without_established_canonical_cannot_block(self):
        current, prior = source(100), source(201, prior=True)
        for field in tuple(prior):
            if field.startswith("canonical_"):
                del prior[field]
        prior["processed"] = 0
        result, _repo = self.assess(current, prior, candidates=(semantic_candidate(),))
        self.assertFalse(result.reject)
        self.assertEqual(result.counters["semantic_candidates_evaluated"], 0)

    def test_valid_retrieval_survives_vector_audit_failure_then_uses_fresh_raw_policy(self):
        selected = semantic_candidate()
        retrieval = SemanticRetrievalResult(
            status=SemanticRetrievalStatus.RETRIEVED, reason=None,
            query_source_article_id=100, query_document_id=10, embedding_version=VERSION,
            requested_top_k=10, lookback_hours=72, candidates=(selected,),
        )
        settings = SemanticRetrievalSettings(True, True, VERSION)
        with (
            patch("semantic_retrieval.shadow.SemanticRetrievalService") as retriever,
            patch("semantic_retrieval.shadow.SemanticAssessmentRepository") as writer,
            patch("embeddings.provider.OpenAIEmbeddingProvider") as provider,
        ):
            retriever.return_value.retrieve_source_neighbors.return_value = retrieval
            writer.return_value.save_assessment.side_effect = RuntimeError("synthetic audit outage")
            semantic = run_semantic_shadow(100, vector_enabled=True, semantic_enabled=True,
                                           settings=settings, semantic_version="semantic-shadow-v1",
                                           vector_repository=object())
        provider.assert_not_called()
        self.assertEqual(semantic.status, SemanticAssessmentStatus.ERROR)
        self.assertFalse(semantic.persisted)
        self.assertEqual(semantic.candidates, (selected,))
        result, _repo = self.assess(source(100), source(201, prior=True), candidates=semantic.candidates)
        self.assertTrue(result.reject)
        self.assertEqual(result.counters["semantic_candidates_promoted"], 1)


if __name__ == "__main__":
    unittest.main()
