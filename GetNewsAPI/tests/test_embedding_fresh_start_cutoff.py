"""Exercise registration cutoff SQL using an isolated in-memory article database."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from embeddings.models import EmbeddingSettings
from embeddings.operations import run_embedding_backfill, run_embedding_ingest
from repositories.embedding_articles import EmbeddingArticleRepository
from vector_store.models import SourceType


CUTOFF = datetime(2026, 10, 4, 12)
SETTINGS = EmbeddingSettings(
    enabled=False,
    provider="openai",
    model="text-embedding-3-small",
    dimensions=1536,
    chunker_version="chunk-v1",
)
CUTOFF_SETTING = "repositories.embedding_articles.EMBEDDING_FRESH_START_AFTER_UTC"


class _Cursor:
    def __init__(self, database, *, dictionary=False):
        self.database = database
        self.dictionary = dictionary
        self.cursor = database.connection.cursor()

    def execute(self, sql, params=()):
        self.database.statements.append((sql, params))
        bindings = tuple(
            value.isoformat(sep=" ") if isinstance(value, datetime) else value
            for value in params
        )
        # Execute the real repository SELECT; translate only the connector placeholders.
        self.cursor.execute(sql.replace("%s", "?"), bindings)

    def fetchone(self):
        row = self.cursor.fetchone()
        if row is None:
            return None
        return dict(row) if self.dictionary else tuple(row)

    def fetchall(self):
        rows = self.cursor.fetchall()
        return [dict(row) if self.dictionary else tuple(row) for row in rows]

    def close(self):
        self.cursor.close()


class _Connection:
    def __init__(self, database):
        self.database = database

    def cursor(self, *, dictionary=False):
        return _Cursor(self.database, dictionary=dictionary)

    def close(self):
        # Repository calls have separate handles over one disposable fixture database.
        pass


class _ArticleDatabase:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.statements = []
        self.connection.executescript(
            """
            CREATE TABLE cryptonewsapi (
                id INTEGER PRIMARY KEY, news_url TEXT, title TEXT, full_text TEXT,
                publish_date TEXT, selected_at TEXT, insertDate TEXT,
                chosen_for_publish INTEGER
            );
            CREATE TABLE rich_crpytonews (
                id INTEGER PRIMARY KEY, raw_article_id INTEGER, news_url TEXT,
                title TEXT, full_text TEXT, publish_date TEXT, insertDate TEXT
            );
            """
        )
        raw_dates = {
            1: CUTOFF - timedelta(seconds=1),
            2: CUTOFF,
            3: CUTOFF + timedelta(seconds=1),
            4: None,
        }
        for article_id, raw_date in raw_dates.items():
            self.connection.execute(
                "INSERT INTO cryptonewsapi VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    article_id,
                    f"https://source.example.invalid/{article_id}",
                    f"Source title {article_id}",
                    f"Source facts {article_id}.",
                    self._text(CUTOFF + timedelta(days=30)),
                    self._text(CUTOFF + timedelta(days=10, seconds=article_id)),
                    self._text(raw_date),
                    1,
                ),
            )
        # Recent generated content cannot make an old raw source eligible.
        # Conversely, old rich publication/insertion dates do not exclude a new raw source.
        generated = (
            (11, 1, CUTOFF + timedelta(days=2)),
            (12, 2, CUTOFF - timedelta(days=2)),
            (13, 3, CUTOFF - timedelta(days=1)),
            (14, 999, CUTOFF + timedelta(days=2)),
            (15, None, CUTOFF + timedelta(days=2)),
            (16, 4, CUTOFF + timedelta(days=2)),
        )
        for rich_id, raw_id, rich_date in generated:
            self.connection.execute(
                "INSERT INTO rich_crpytonews VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    rich_id,
                    raw_id,
                    f"https://source.example.invalid/rich/{rich_id}",
                    f"Generated title {rich_id}",
                    f"Generated facts {rich_id}.",
                    self._text(rich_date),
                    self._text(rich_date),
                ),
            )
        self.connection.commit()

    @staticmethod
    def _text(value):
        return value.isoformat(sep=" ") if value is not None else None

    def connect(self):
        return _Connection(self)

    def close(self):
        self.connection.close()


class _MemoryVectorRepository:
    def __init__(self):
        self.documents = {}
        self.jobs = {}

    def check_connection(self):
        pass

    def register_document(self, document):
        identity = (document.document_key, document.content_version)
        if identity in self.documents:
            return self.documents[identity][0], False
        document_id = len(self.documents) + 1
        self.documents[identity] = (document_id, document)
        return document_id, True

    def enqueue_embedding_job_with_status(self, document_id, embedding_version):
        identity = (document_id, embedding_version)
        if identity in self.jobs:
            return self.jobs[identity], False
        job_id = len(self.jobs) + 1
        self.jobs[identity] = job_id
        return job_id, True

    def document_keys(self):
        return {document.document_key for _document_id, document in self.documents.values()}


class EmbeddingFreshStartCutoffTests(unittest.TestCase):
    def setUp(self):
        self.database = _ArticleDatabase()
        self.addCleanup(self.database.close)
        self.repository = EmbeddingArticleRepository(connect=self.database.connect)
        cutoff_patch = patch(CUTOFF_SETTING, CUTOFF)
        cutoff_patch.start()
        self.addCleanup(cutoff_patch.stop)
        connection_patch = patch(
            "mysql.connector.connect",
            side_effect=AssertionError("cutoff tests must not open a real database"),
        )
        connection_patch.start()
        self.addCleanup(connection_patch.stop)

    def test_recent_scan_includes_at_and_after_raw_cutoff_and_excludes_before(self):
        articles = self.repository.scan_recent(20)
        self.assertEqual(
            {(article.source_type, article.cursor_id) for article in articles},
            {
                (SourceType.SOURCE_ARTICLE, 2),
                (SourceType.SOURCE_ARTICLE, 3),
                (SourceType.COINCOURIER_GENERATED, 12),
                (SourceType.COINCOURIER_GENERATED, 13),
            },
        )
        sql, params = self.database.statements[-1]
        self.assertEqual(params, (CUTOFF, CUTOFF, 20))
        self.assertEqual(sql.count("c.insertDate >= %s"), 2)
        self.assertNotIn("r.insertDate >=", sql)
        self.assertNotIn(CUTOFF.isoformat(sep=" "), sql)

    def test_source_backfill_uses_inclusive_raw_cutoff(self):
        articles = self.repository.scan_backfill_page(
            SourceType.SOURCE_ARTICLE, before_id=99, page_size=20
        )
        self.assertEqual([article.cursor_id for article in articles], [3, 2])
        sql, params = self.database.statements[-1]
        self.assertIn("c.insertDate >= %s", sql)
        self.assertEqual(params, (99, CUTOFF, 20))

    def test_generated_backfill_uses_linked_raw_date_and_fails_closed(self):
        articles = self.repository.scan_backfill_page(
            SourceType.COINCOURIER_GENERATED, before_id=99, page_size=20
        )
        self.assertEqual(
            [(article.rich_article_id, article.source_article_id) for article in articles],
            [(13, 3), (12, 2)],
        )
        sql, params = self.database.statements[-1]
        self.assertIn("LEFT JOIN cryptonewsapi c ON c.id = r.raw_article_id", sql)
        self.assertIn("c.insertDate >= %s", sql)
        self.assertNotIn("r.insertDate >=", sql)
        self.assertEqual(params, (99, CUTOFF, 20))

    def test_fractional_cutoff_preserves_the_exact_inclusive_boundary(self):
        cutoff = CUTOFF + timedelta(microseconds=500000)
        for raw_id, insert_date in (
            (1, cutoff - timedelta(microseconds=1)),
            (2, cutoff),
            (3, cutoff + timedelta(microseconds=1)),
        ):
            self.database.connection.execute(
                "UPDATE cryptonewsapi SET insertDate=? WHERE id=?",
                (insert_date.isoformat(sep=" "), raw_id),
            )
        self.database.connection.commit()
        with patch(CUTOFF_SETTING, cutoff):
            self.assertEqual(len(self.repository.scan_recent(20)), 4)
            self.assertEqual(
                self.database.statements[-1][1], (cutoff, cutoff, 20)
            )
            for source_type, expected in (
                (SourceType.SOURCE_ARTICLE, [3, 2]),
                (SourceType.COINCOURIER_GENERATED, [13, 12]),
            ):
                with self.subTest(source_type=source_type):
                    articles = self.repository.scan_backfill_page(
                        source_type, before_id=99, page_size=20
                    )
                    self.assertEqual(
                        [article.cursor_id for article in articles], expected
                    )
                    self.assertEqual(
                        self.database.statements[-1][1], (99, cutoff, 20)
                    )

    def test_disabled_recent_cutoff_preserves_old_rows_unresolved_links_and_parameters(self):
        with patch(CUTOFF_SETTING, None):
            articles = self.repository.scan_recent(20)
        self.assertEqual(len(articles), 10)
        self.assertEqual(
            {
                article.cursor_id
                for article in articles
                if article.source_type is SourceType.SOURCE_ARTICLE
            },
            {1, 2, 3, 4},
        )
        self.assertEqual(
            {
                article.rich_article_id
                for article in articles
                if article.source_article_id is None
            },
            {14, 15},
        )
        sql, params = self.database.statements[-1]
        self.assertNotIn("insertDate >=", sql)
        self.assertEqual(params, (20,))

    def test_disabled_backfill_cutoff_preserves_both_query_parameters_and_eligibility(self):
        with patch(CUTOFF_SETTING, None):
            for source_type, expected in (
                (SourceType.SOURCE_ARTICLE, [4, 3, 2, 1]),
                (SourceType.COINCOURIER_GENERATED, [16, 15, 14, 13, 12, 11]),
            ):
                with self.subTest(source_type=source_type):
                    articles = self.repository.scan_backfill_page(
                        source_type, before_id=99, page_size=20
                    )
                    self.assertEqual([article.cursor_id for article in articles], expected)
                    sql, params = self.database.statements[-1]
                    self.assertNotIn("insertDate >=", sql)
                    self.assertEqual(params, (99, 20))

    def test_recent_limit_still_bounds_the_combined_source_and_generated_pool(self):
        articles = self.repository.scan_recent(1)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].source_article_id, 3)
        self.assertEqual(articles[0].source_type, SourceType.SOURCE_ARTICLE)

    def _register(self, operation, vector, *, limit=20):
        arguments = {
            "limit": limit,
            "vector_enabled": True,
            "settings": SETTINGS,
            "vector_repository": vector,
            "article_repository": self.repository,
        }
        with patch("embeddings.operations.OpenAIEmbeddingProvider") as provider:
            if operation == "ingest":
                metrics = run_embedding_ingest(**arguments)
            else:
                metrics = run_embedding_backfill(operation, page_size=1, **arguments)
        provider.assert_not_called()
        return metrics

    def test_ingest_and_both_backfills_only_register_eligible_documents_without_provider_calls(self):
        for operation, expected in (
            (
                "ingest",
                {
                    "source_article:2", "source_article:3",
                    "coincourier_generated:12", "coincourier_generated:13",
                },
            ),
            (SourceType.SOURCE_ARTICLE, {"source_article:2", "source_article:3"}),
            (
                SourceType.COINCOURIER_GENERATED,
                {"coincourier_generated:12", "coincourier_generated:13"},
            ),
        ):
            with self.subTest(operation=operation):
                vector = _MemoryVectorRepository()
                metrics = self._register(operation, vector)
                self.assertEqual(vector.document_keys(), expected)
                self.assertEqual(metrics.documents_registered, len(expected))
                self.assertEqual(metrics.jobs_enqueued, len(expected))
                self.assertEqual(len(vector.jobs), len(expected))

    def test_recent_registration_reruns_reuse_existing_documents_and_jobs(self):
        vector = _MemoryVectorRepository()
        first = self._register("ingest", vector)
        second = self._register("ingest", vector)
        self.assertEqual((first.documents_registered, first.jobs_enqueued), (4, 4))
        self.assertEqual((second.documents_registered, second.jobs_enqueued), (0, 0))
        self.assertEqual(second.jobs_skipped_existing, 4)
        self.assertEqual((len(vector.documents), len(vector.jobs)), (4, 4))

    def test_historical_only_ingest_and_both_backfills_register_nothing(self):
        self.database.connection.execute(
            "UPDATE cryptonewsapi SET insertDate=?",
            ((CUTOFF - timedelta(days=1)).isoformat(sep=" "),),
        )
        self.database.connection.execute(
            "UPDATE rich_crpytonews SET insertDate=?, publish_date=?",
            ((CUTOFF + timedelta(days=1)).isoformat(sep=" "),) * 2,
        )
        self.database.connection.commit()
        for operation in (
            "ingest", SourceType.SOURCE_ARTICLE, SourceType.COINCOURIER_GENERATED
        ):
            with self.subTest(operation=operation):
                vector = _MemoryVectorRepository()
                metrics = self._register(operation, vector)
                self.assertEqual(metrics.documents_scanned, 0)
                self.assertEqual(metrics.documents_registered, 0)
                self.assertEqual(metrics.jobs_enqueued, 0)
                self.assertEqual((len(vector.documents), len(vector.jobs)), (0, 0))
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM cryptonewsapi"
            ).fetchone()[0],
            4,
        )
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM rich_crpytonews"
            ).fetchone()[0],
            6,
        )

    def test_backfill_changed_limits_high_water_and_idempotent_restart_are_preserved(self):
        for source_type, high_water in (
            (SourceType.SOURCE_ARTICLE, 4),
            (SourceType.COINCOURIER_GENERATED, 16),
        ):
            with self.subTest(source_type=source_type):
                vector = _MemoryVectorRepository()
                first = self._register(source_type, vector, limit=1)
                second = self._register(source_type, vector, limit=1)
                third = self._register(source_type, vector, limit=1)
                self.assertEqual((first.documents_registered, first.jobs_enqueued), (1, 1))
                self.assertEqual((second.documents_registered, second.jobs_enqueued), (1, 1))
                self.assertEqual((third.documents_registered, third.jobs_enqueued), (0, 0))
                self.assertEqual(
                    [first.high_water_id, second.high_water_id, third.high_water_id],
                    [high_water] * 3,
                )
                self.assertEqual(second.jobs_skipped_existing, 1)
                self.assertEqual(third.jobs_skipped_existing, 2)
                self.assertEqual((len(vector.documents), len(vector.jobs)), (2, 2))

    def test_existing_worker_content_loading_is_not_filtered_by_registration_cutoff(self):
        for document, expected in (
            (
                SimpleNamespace(
                    source_type=SourceType.SOURCE_ARTICLE,
                    source_article_id=1,
                    rich_article_id=None,
                ),
                "Source facts 1.",
            ),
            (
                SimpleNamespace(
                    source_type=SourceType.COINCOURIER_GENERATED,
                    source_article_id=1,
                    rich_article_id=11,
                ),
                "Generated facts 11.",
            ),
        ):
            with self.subTest(source_type=document.source_type):
                self.assertEqual(self.repository.load_body(document), expected)
                self.assertNotIn("insertDate >=", self.database.statements[-1][0])


if __name__ == "__main__":
    unittest.main()
