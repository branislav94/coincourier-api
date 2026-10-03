from __future__ import annotations

import inspect
import sys
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mysql.connector


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = PROJECT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import tasks
from embeddings.chunking import prepare_document
from embeddings.ingestion import ApplicationArticle, EmbeddingIngestionService
from embeddings.models import EmbeddingJobResult, EmbeddingSettings
from embeddings.operations import (
    run_embedding_backfill,
    run_embedding_ingest,
    run_embedding_worker,
)
from embeddings.provider import (
    EmbeddingConfigurationError,
    FakeEmbeddingProvider,
)
from repositories.embedding_articles import EmbeddingArticleRepository
from vector_store.models import SourceType
from vector_store.repository import VectorRepository


SETTINGS = EmbeddingSettings(
    enabled=True,
    provider="openai",
    model="text-embedding-3-small",
    dimensions=1536,
    chunker_version="chunk-v1",
    batch_size=16,
)


def source_article(article_id: int, *, body: str = "Source facts.") -> ApplicationArticle:
    return ApplicationArticle(
        source_type=SourceType.SOURCE_ARTICLE,
        source_article_id=article_id,
        rich_article_id=None,
        source_url=f"https://source.test/{article_id}",
        title=f"Source title {article_id}",
        body=body,
        published_at=datetime(2026, 9, 1),
    )


def generated_article(
    rich_id: int,
    *,
    source_id: int | None = 1,
    body: str = "<p>Generated facts.</p>",
) -> ApplicationArticle:
    return ApplicationArticle(
        source_type=SourceType.COINCOURIER_GENERATED,
        source_article_id=source_id,
        rich_article_id=rich_id,
        source_url=f"https://source.test/{source_id or 'missing'}",
        title=f"Generated title {rich_id}",
        body=body,
        published_at=datetime(2026, 9, 1),
    )


class MemoryVectorRepository:
    def __init__(self) -> None:
        self.documents = {}
        self.documents_by_id = {}
        self.jobs = {}
        self.checks = 0

    def check_connection(self):
        self.checks += 1

    def register_document(self, document):
        key = (document.document_key, document.content_version)
        if key in self.documents:
            return self.documents[key], False
        document_id = len(self.documents) + 1
        self.documents[key] = document_id
        self.documents_by_id[document_id] = document
        return document_id, True

    def enqueue_embedding_job_with_status(self, document_id, embedding_version):
        key = (document_id, embedding_version)
        if key in self.jobs:
            return self.jobs[key], False
        job_id = len(self.jobs) + 1
        self.jobs[key] = job_id
        return job_id, True


class MemoryArticleRepository:
    def __init__(self, articles=()) -> None:
        self.articles = list(articles)
        self.checks = 0
        self.page_calls = []

    def check_connection(self):
        self.checks += 1

    def scan_recent(self, limit):
        return sorted(
            self.articles,
            key=lambda article: article.cursor_id,
            reverse=True,
        )[:limit]

    def max_article_id(self, source_type):
        ids = [
            article.cursor_id
            for article in self.articles
            if article.source_type is source_type
        ]
        return max(ids, default=0)

    def scan_backfill_page(self, source_type, *, before_id, page_size):
        self.page_calls.append((source_type, before_id, page_size))
        eligible = [
            article
            for article in self.articles
            if article.source_type is source_type and article.cursor_id < before_id
        ]
        return sorted(eligible, key=lambda article: article.cursor_id, reverse=True)[
            :page_size
        ]

    def load_body(self, document):
        for article in self.articles:
            if document.source_type is SourceType.SOURCE_ARTICLE:
                matches = article.source_article_id == document.source_article_id
            else:
                matches = (
                    article.rich_article_id == document.rich_article_id
                    and article.source_article_id == document.source_article_id
                )
            if article.source_type is document.source_type and matches:
                return article.body
        return None


class IngestionTests(unittest.TestCase):
    def service(self, articles=()):
        vector = MemoryVectorRepository()
        application = MemoryArticleRepository(articles)
        service = EmbeddingIngestionService(
            vector_repository=vector,
            article_repository=application,
            settings=SETTINGS,
        )
        return service, vector, application

    def test_source_article_registers_exact_provenance(self):
        article = source_article(7)
        service, vector, _application = self.service([article])
        result = service.register(article)
        draft = vector.documents_by_id[result.document_id]
        self.assertEqual(draft.source_type, SourceType.SOURCE_ARTICLE)
        self.assertEqual(draft.source_article_id, 7)
        self.assertIsNone(draft.rich_article_id)

    def test_generated_article_registers_derivative_provenance(self):
        article = generated_article(9, source_id=7)
        service, vector, _application = self.service([article])
        result = service.register(article)
        draft = vector.documents_by_id[result.document_id]
        self.assertEqual(draft.source_type, SourceType.COINCOURIER_GENERATED)
        self.assertEqual(draft.source_article_id, 7)
        self.assertEqual(draft.rich_article_id, 9)

    def test_missing_generated_source_linkage_skips_without_vector_work(self):
        service, vector, _application = self.service()
        result = service.register(generated_article(9, source_id=None))
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.skip_reason, "missing_generated_source_linkage")
        self.assertEqual(vector.documents, {})
        self.assertEqual(vector.jobs, {})

    def test_unchanged_rerun_reuses_document_and_job(self):
        article = source_article(7)
        service, vector, _application = self.service([article])
        first = service.register(article)
        second = service.register(article)
        self.assertTrue(first.document_created)
        self.assertTrue(first.job_created)
        self.assertFalse(second.document_created)
        self.assertFalse(second.job_created)
        self.assertEqual(first.document_id, second.document_id)
        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(len(vector.documents), 1)
        self.assertEqual(len(vector.jobs), 1)

    def test_changed_content_creates_new_document_and_job_version(self):
        service, vector, _application = self.service()
        first = service.register(source_article(7, body="Initial facts."))
        second = service.register(source_article(7, body="Changed material facts."))
        self.assertNotEqual(first.document_id, second.document_id)
        self.assertNotEqual(first.job_id, second.job_id)
        self.assertEqual(len(vector.documents), 2)
        self.assertEqual(len(vector.jobs), 2)

    def test_registration_uses_chunk_v1_title_and_visible_body_identity(self):
        article = source_article(7, body="<p>Visible facts.</p><script>ignored()</script>")
        service, vector, _application = self.service()
        result = service.register(article)
        draft = vector.documents_by_id[result.document_id]
        expected = prepare_document(article.title, article.body)
        self.assertEqual(draft.content_hash, expected.content_hash)
        self.assertEqual(draft.content_version, expected.content_version)
        self.assertNotIn("ignored", expected.text)

    def test_vector_disabled_prevents_ingestion_connections(self):
        vector = MemoryVectorRepository()
        application = MemoryArticleRepository([source_article(1)])
        result = run_embedding_ingest(
            limit=5,
            vector_enabled=False,
            settings=SETTINGS,
            vector_repository=vector,
            article_repository=application,
        )
        self.assertEqual(result.status, "disabled")
        self.assertEqual(vector.checks, 0)
        self.assertEqual(application.checks, 0)

    def test_vector_connection_failure_propagates_without_scanning_or_provider(self):
        vector = MemoryVectorRepository()
        vector.check_connection = Mock(side_effect=RuntimeError("vector unavailable"))
        application = MemoryArticleRepository([source_article(1)])
        with patch("embeddings.operations.OpenAIEmbeddingProvider") as provider:
            with self.assertRaisesRegex(RuntimeError, "vector unavailable"):
                run_embedding_ingest(
                    limit=5,
                    vector_enabled=True,
                    settings=SETTINGS,
                    vector_repository=vector,
                    article_repository=application,
                )
        provider.assert_not_called()
        self.assertEqual(application.checks, 0)
        self.assertEqual(vector.documents, {})

    def test_application_article_repository_is_read_only(self):
        source = inspect.getsource(EmbeddingArticleRepository)
        for verb in ("INSERT ", "UPDATE ", "DELETE ", "REPLACE "):
            self.assertNotIn(verb, source.upper())

    def test_ingest_metrics_distinguish_new_and_existing_jobs(self):
        vector = MemoryVectorRepository()
        application = MemoryArticleRepository([source_article(1)])
        first = run_embedding_ingest(
            limit=5,
            vector_enabled=True,
            settings=SETTINGS,
            vector_repository=vector,
            article_repository=application,
        )
        second = run_embedding_ingest(
            limit=5,
            vector_enabled=True,
            settings=SETTINGS,
            vector_repository=vector,
            article_repository=application,
        )
        self.assertEqual((first.documents_registered, first.jobs_enqueued), (1, 1))
        self.assertEqual(second.documents_registered, 0)
        self.assertEqual(second.jobs_skipped_existing, 1)


class WorkerTests(unittest.TestCase):
    def run_with_results(self, results, *, limit=5):
        vector = MemoryVectorRepository()
        application = MemoryArticleRepository()
        provider = FakeEmbeddingProvider()
        engine = Mock()
        engine.process_next.side_effect = results
        with patch("embeddings.operations.EmbeddingJobEngine", return_value=engine):
            metrics = run_embedding_worker(
                limit=limit,
                claim_timeout_minutes=30,
                vector_enabled=True,
                settings=SETTINGS,
                vector_repository=vector,
                article_repository=application,
                provider=provider,
            )
        return metrics, engine

    def test_worker_processes_only_bounded_number_of_jobs(self):
        completed = EmbeddingJobResult(
            status="completed", job_id=1, chunk_count=2, provider_calls=1
        )
        metrics, engine = self.run_with_results([completed, completed, completed], limit=2)
        self.assertEqual(engine.process_next.call_count, 2)
        self.assertEqual(metrics.jobs_claimed, 2)
        self.assertEqual(metrics.provider_calls, 2)

    def test_default_paid_request_bound_is_thirty_five(self):
        batches_per_job = (
            SETTINGS.max_chunks_per_job + SETTINGS.batch_size - 1
        ) // SETTINGS.batch_size
        self.assertEqual(5 * batches_per_job, 35)

    def test_worker_stops_normally_on_genuine_empty_queue(self):
        metrics, engine = self.run_with_results([EmbeddingJobResult(status="idle")])
        self.assertEqual(engine.process_next.call_count, 1)
        self.assertTrue(metrics.queue_empty)
        self.assertEqual(metrics.jobs_claimed, 0)

    def test_completed_and_reconciled_results_count_correctly(self):
        metrics, _engine = self.run_with_results(
            [
                EmbeddingJobResult(
                    status="completed",
                    job_id=1,
                    chunk_count=2,
                    provider_calls=1,
                    embedding_tokens=11,
                ),
                EmbeddingJobResult(
                    status="completed",
                    job_id=2,
                    chunk_count=1,
                    reconciled=True,
                ),
                EmbeddingJobResult(status="idle"),
            ]
        )
        self.assertEqual(metrics.jobs_completed, 2)
        self.assertEqual(metrics.jobs_reconciled, 1)
        self.assertEqual(metrics.chunks_embedded, 2)
        self.assertEqual(metrics.embedding_tokens, 11)

    def test_retryable_provider_result_is_counted_and_stops_hot_retry(self):
        metrics, engine = self.run_with_results(
            [
                EmbeddingJobResult(status="retryable", job_id=1, provider_calls=1),
                EmbeddingJobResult(status="completed", job_id=2),
            ]
        )
        self.assertEqual(metrics.jobs_retryable, 1)
        self.assertEqual(metrics.provider_calls, 1)
        self.assertEqual(engine.process_next.call_count, 1)

    def test_failed_and_lost_claim_results_are_counted(self):
        metrics, _engine = self.run_with_results(
            [
                EmbeddingJobResult(status="failed", job_id=1),
                EmbeddingJobResult(status="lost_claim", job_id=2),
                EmbeddingJobResult(status="idle"),
            ]
        )
        self.assertEqual(metrics.jobs_failed, 1)
        self.assertEqual(metrics.jobs_lost_claim, 1)

    def test_unexpected_storage_error_propagates(self):
        with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
            self.run_with_results([RuntimeError("storage unavailable")])

    def test_embedding_disabled_prevents_claim_or_provider_work(self):
        vector = MemoryVectorRepository()
        application = MemoryArticleRepository()
        provider = FakeEmbeddingProvider()
        disabled = replace(SETTINGS, enabled=False)
        metrics = run_embedding_worker(
            limit=5,
            claim_timeout_minutes=30,
            vector_enabled=True,
            settings=disabled,
            vector_repository=vector,
            article_repository=application,
            provider=provider,
        )
        self.assertEqual(metrics.status, "disabled")
        self.assertEqual(vector.checks, 0)
        self.assertEqual(provider.call_count, 0)

    def test_missing_openai_key_fails_before_connection_or_claim(self):
        vector = MemoryVectorRepository()
        application = MemoryArticleRepository()
        with self.assertRaises(EmbeddingConfigurationError):
            run_embedding_worker(
                limit=5,
                claim_timeout_minutes=30,
                vector_enabled=True,
                settings=SETTINGS,
                vector_repository=vector,
                article_repository=application,
                provider=None,
                openai_api_key="",
            )
        self.assertEqual(vector.checks, 0)
        self.assertEqual(application.checks, 0)

    def test_approved_contract_misconfiguration_fails_before_connections(self):
        invalid_contracts = (
            {"provider": "other"},
            {"model": "other-model"},
            {"dimensions": 3072},
            {"chunker_version": "chunk-v2"},
        )
        for changes in invalid_contracts:
            with self.subTest(changes=changes):
                values = {
                    "enabled": True,
                    "provider": SETTINGS.provider,
                    "model": SETTINGS.model,
                    "dimensions": SETTINGS.dimensions,
                    "chunker_version": SETTINGS.chunker_version,
                    "batch_size": SETTINGS.batch_size,
                    "max_chunks_per_job": SETTINGS.max_chunks_per_job,
                }
                values.update(changes)
                vector = MemoryVectorRepository()
                application = MemoryArticleRepository()
                provider = FakeEmbeddingProvider()
                with self.assertRaises(EmbeddingConfigurationError):
                    run_embedding_worker(
                        limit=5,
                        claim_timeout_minutes=30,
                        vector_enabled=True,
                        settings=SimpleNamespace(**values),
                        vector_repository=vector,
                        article_repository=application,
                        provider=provider,
                    )
                self.assertEqual(vector.checks, 0)
                self.assertEqual(application.checks, 0)
                self.assertEqual(provider.call_count, 0)

    def test_provider_contract_mismatch_fails_before_connections(self):
        vector = MemoryVectorRepository()
        application = MemoryArticleRepository()
        provider = FakeEmbeddingProvider(model="other-model")
        with self.assertRaises(EmbeddingConfigurationError):
            run_embedding_worker(
                limit=5,
                claim_timeout_minutes=30,
                vector_enabled=True,
                settings=SETTINGS,
                vector_repository=vector,
                article_repository=application,
                provider=provider,
            )
        self.assertEqual(vector.checks, 0)
        self.assertEqual(application.checks, 0)
        self.assertEqual(provider.call_count, 0)

    def test_vector_db_configuration_fails_before_repository_or_claim(self):
        application = MemoryArticleRepository()
        provider = FakeEmbeddingProvider()
        with patch("embeddings.operations.VectorRepository") as repository:
            with self.assertRaises(EmbeddingConfigurationError):
                run_embedding_worker(
                    limit=5,
                    claim_timeout_minutes=30,
                    vector_enabled=True,
                    settings=SETTINGS,
                    vector_repository=None,
                    article_repository=application,
                    provider=provider,
                    vector_db_config={"host": "127.0.0.1"},
                )
        repository.assert_not_called()
        self.assertEqual(application.checks, 0)
        self.assertEqual(provider.call_count, 0)

    def test_application_db_preflight_failure_occurs_before_engine_or_claim(self):
        vector = MemoryVectorRepository()
        application = MemoryArticleRepository()
        application.check_connection = Mock(
            side_effect=RuntimeError("application database unavailable")
        )
        provider = FakeEmbeddingProvider()
        with patch("embeddings.operations.EmbeddingJobEngine") as engine:
            with self.assertRaisesRegex(RuntimeError, "application database unavailable"):
                run_embedding_worker(
                    limit=5,
                    claim_timeout_minutes=30,
                    vector_enabled=True,
                    settings=SETTINGS,
                    vector_repository=vector,
                    article_repository=application,
                    provider=provider,
                )
        engine.assert_not_called()
        self.assertEqual(vector.checks, 1)
        self.assertEqual(provider.call_count, 0)

    def test_vector_db_preflight_failure_occurs_before_engine_or_claim(self):
        vector = MemoryVectorRepository()
        vector.check_connection = Mock(side_effect=RuntimeError("vector unavailable"))
        application = MemoryArticleRepository()
        provider = FakeEmbeddingProvider()
        with patch("embeddings.operations.EmbeddingJobEngine") as engine:
            with self.assertRaisesRegex(RuntimeError, "vector unavailable"):
                run_embedding_worker(
                    limit=5,
                    claim_timeout_minutes=30,
                    vector_enabled=True,
                    settings=SETTINGS,
                    vector_repository=vector,
                    article_repository=application,
                    provider=provider,
                )
        engine.assert_not_called()
        self.assertEqual(application.checks, 0)
        self.assertEqual(provider.call_count, 0)


class VectorClaimSemanticsTests(unittest.TestCase):
    @staticmethod
    def deadlock():
        return mysql.connector.errors.InternalError(
            msg="deadlock",
            errno=1213,
            sqlstate="40001",
        )

    def test_transient_claim_deadlock_retries_once_instead_of_reporting_empty(self):
        repository = VectorRepository(connect=Mock())
        claimed = object()
        with patch.object(
            repository,
            "_claim_embedding_job_once",
            side_effect=[self.deadlock(), claimed],
        ) as claim_once:
            result = repository.claim_embedding_job(
                SETTINGS.embedding_version,
                timeout_minutes=30,
            )
        self.assertIs(result, claimed)
        self.assertEqual(claim_once.call_count, 2)

    def test_second_claim_deadlock_propagates_instead_of_reporting_empty(self):
        repository = VectorRepository(connect=Mock())
        with patch.object(
            repository,
            "_claim_embedding_job_once",
            side_effect=[self.deadlock(), self.deadlock()],
        ) as claim_once:
            with self.assertRaises(mysql.connector.Error):
                repository.claim_embedding_job(
                    SETTINGS.embedding_version,
                    timeout_minutes=30,
                )
        self.assertEqual(claim_once.call_count, 2)


class BackfillTests(unittest.TestCase):
    def run_backfill(self, vector, application, *, limit=2, page_size=2):
        return run_embedding_backfill(
            SourceType.SOURCE_ARTICLE,
            limit=limit,
            page_size=page_size,
            vector_enabled=True,
            settings=SETTINGS,
            vector_repository=vector,
            article_repository=application,
        )

    def test_backfill_is_newest_first_with_bounded_pages(self):
        vector = MemoryVectorRepository()
        application = MemoryArticleRepository([source_article(i) for i in range(1, 6)])
        metrics = self.run_backfill(vector, application, limit=2, page_size=1)
        registered_ids = [draft.source_article_id for draft in vector.documents_by_id.values()]
        self.assertEqual(registered_ids, [5, 4])
        self.assertEqual(metrics.documents_registered, 2)
        self.assertEqual([call[2] for call in application.page_calls], [1, 1])

    def test_backfill_restart_rescans_idempotently_and_resumes_progress(self):
        vector = MemoryVectorRepository()
        application = MemoryArticleRepository([source_article(i) for i in range(1, 5)])
        first = self.run_backfill(vector, application, limit=2, page_size=2)
        second = self.run_backfill(vector, application, limit=2, page_size=2)
        self.assertEqual(first.documents_registered, 2)
        self.assertEqual(second.documents_registered, 2)
        self.assertEqual(len(vector.documents), 4)
        self.assertGreater(second.documents_scanned, second.documents_registered)

    def test_known_newest_pages_do_not_block_arbitrarily_older_rows(self):
        vector = MemoryVectorRepository()
        application = MemoryArticleRepository([source_article(i) for i in range(1, 8)])
        service = EmbeddingIngestionService(
            vector_repository=vector,
            article_repository=application,
            settings=SETTINGS,
        )
        service.register(source_article(7))
        service.register(source_article(6))

        first = self.run_backfill(vector, application, limit=2, page_size=2)
        second = self.run_backfill(vector, application, limit=3, page_size=2)

        self.assertEqual(first.documents_scanned, 4)
        self.assertEqual(first.documents_registered, 2)
        self.assertEqual(second.documents_scanned, 7)
        self.assertEqual(second.documents_registered, 3)
        self.assertEqual(
            sorted(draft.source_article_id for draft in vector.documents_by_id.values()),
            list(range(1, 8)),
        )

    def test_completed_backfill_rerun_is_idempotent(self):
        vector = MemoryVectorRepository()
        application = MemoryArticleRepository([source_article(1), source_article(2)])
        self.run_backfill(vector, application, limit=5)
        rerun = self.run_backfill(vector, application, limit=5)
        self.assertEqual(rerun.documents_registered, 0)
        self.assertEqual(rerun.jobs_enqueued, 0)
        self.assertEqual(rerun.jobs_skipped_existing, 2)

    def test_new_article_added_during_previous_snapshot_is_not_lost(self):
        vector = MemoryVectorRepository()
        application = MemoryArticleRepository([source_article(1), source_article(2)])
        self.run_backfill(vector, application, limit=5)
        application.articles.append(source_article(3))
        result = self.run_backfill(vector, application, limit=1)
        self.assertEqual(result.documents_registered, 1)
        newest = vector.documents_by_id[max(vector.documents_by_id)]
        self.assertEqual(newest.source_article_id, 3)

    def test_changed_article_version_is_discovered_without_overwrite(self):
        vector = MemoryVectorRepository()
        application = MemoryArticleRepository([source_article(1), source_article(2)])
        self.run_backfill(vector, application, limit=5)
        application.articles[0] = source_article(1, body="Changed source facts.")
        changed = self.run_backfill(vector, application, limit=1)
        self.assertEqual(changed.documents_registered, 1)
        versions = [
            draft
            for draft in vector.documents_by_id.values()
            if draft.source_article_id == 1
        ]
        self.assertEqual(len(versions), 2)
        self.assertNotEqual(versions[0].content_version, versions[1].content_version)

    def test_generated_backfill_skips_missing_source_linkage_safely(self):
        vector = MemoryVectorRepository()
        application = MemoryArticleRepository(
            [generated_article(2, source_id=None), generated_article(1, source_id=1)]
        )
        metrics = run_embedding_backfill(
            SourceType.COINCOURIER_GENERATED,
            limit=5,
            page_size=5,
            vector_enabled=True,
            settings=SETTINGS,
            vector_repository=vector,
            article_repository=application,
        )
        self.assertEqual(metrics.documents_skipped, 1)
        self.assertEqual(metrics.documents_registered, 1)

    def test_claim_query_prioritizes_fresh_pending_work_and_rotates_retries(self):
        source = inspect.getsource(VectorRepository._claim_embedding_job_once)
        self.assertIn("WHEN 'pending' THEN 0", source)
        self.assertIn("WHEN 'claimed' THEN 1", source)
        self.assertIn("j.attempt_count", source)
        self.assertIn("j.updated_at", source)
        self.assertIn("d.published_at IS NULL ASC", source)
        self.assertIn("d.published_at DESC", source)


class TaskCommandTests(unittest.TestCase):
    def test_embedding_task_commands_dispatch_explicit_limits(self):
        cases = (
            (["tasks.py", "embedding_ingest", "7"], "run_embedding_ingest", (7,)),
            (["tasks.py", "embedding_worker", "3"], "run_embedding_worker", (3,)),
            (
                ["tasks.py", "embedding_backfill", "generated", "9"],
                "run_embedding_backfill",
                ("generated", 9),
            ),
        )
        for argv, runner_name, expected in cases:
            with self.subTest(command=argv[1]):
                with patch.object(sys, "argv", argv), patch.object(
                    tasks, runner_name
                ) as runner:
                    self.assertEqual(tasks.main(), 0)
                runner.assert_called_once_with(*expected)

    def test_scheduler_and_pipeline_modules_have_no_automatic_embedding_calls(self):
        for relative_path in (
            "GetNewsAPI/app.py",
            "GetNewsAPI/scheduler.py",
            "GetNewsAPI/fetcher.py",
            "GetNewsAPI/gpt_processor.py",
            "GetNewsAPI/publish_to_wp.py",
        ):
            source = (REPOSITORY_DIR / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("run_embedding_", source, relative_path)
            self.assertNotIn("nearest_chunks(", source, relative_path)


if __name__ == "__main__":
    unittest.main()
