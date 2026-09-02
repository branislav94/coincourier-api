from __future__ import annotations

import inspect
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = PROJECT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config
from vector_store import db
from vector_store.models import SourceType, VECTOR_DIMENSIONS, VectorDocumentDraft
from vector_store.repository import serialize_embedding


def content_hash(character: str = "a") -> str:
    return character * 64


class VectorConfigurationTests(unittest.TestCase):
    def test_source_default_is_disabled_and_separate(self):
        source = inspect.getsource(config)
        self.assertIn('_env_bool("VECTOR_ENABLED", False)', source)
        self.assertIn("VECTOR_DB_CONFIG", source)
        self.assertNotEqual(config.VECTOR_DB_CONFIG, config.DB_CONFIG)
        env_example = (REPOSITORY_DIR / ".env.example").read_text(encoding="utf-8")
        self.assertIn("VECTOR_ENABLED=false", env_example)
        self.assertIn("VECTOR_DB_NAME=coincourier_vectors", env_example)

    def test_disabled_connection_never_calls_connector(self):
        with (
            patch.object(db, "VECTOR_ENABLED", False),
            patch.object(db.mysql.connector, "connect") as connect,
        ):
            with self.assertRaises(db.VectorStoreDisabledError):
                db.connect_vector_db()
        connect.assert_not_called()

    def test_database_guard_rejects_application_database_name(self):
        with self.assertRaises(db.VectorDatabaseGuardError):
            db.assert_vector_database_name("coincourier_api")
        self.assertEqual(
            db.assert_vector_database_name("coincourier_vectors_test"),
            "coincourier_vectors_test",
        )

    def test_automatic_pipeline_modules_do_not_import_vector_store(self):
        for relative_path in (
            "GetNewsAPI/app.py",
            "GetNewsAPI/scheduler.py",
            "GetNewsAPI/fetcher.py",
            "GetNewsAPI/gpt_processor.py",
            "GetNewsAPI/publishing/service.py",
        ):
            source = (REPOSITORY_DIR / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("vector_store", source, relative_path)


class VectorModelTests(unittest.TestCase):
    def test_source_and_generated_provenance_keys_are_distinct(self):
        source = VectorDocumentDraft(
            source_type=SourceType.SOURCE_ARTICLE,
            source_article_id=11,
            rich_article_id=None,
            source_url="https://source.example.test/11",
            title="Source article",
            published_at=None,
            content_hash=content_hash("a"),
            content_version="source-v1",
        )
        generated = VectorDocumentDraft(
            source_type=SourceType.COINCOURIER_GENERATED,
            source_article_id=11,
            rich_article_id=21,
            source_url="https://source.example.test/11",
            title="Generated article",
            published_at=None,
            content_hash=content_hash("b"),
            content_version="rich-v1",
        )
        self.assertEqual(source.document_key, "source_article:11")
        self.assertEqual(generated.document_key, "coincourier_generated:21")

    def test_generated_document_requires_durable_source_and_rich_ids(self):
        with self.assertRaises(ValueError):
            VectorDocumentDraft(
                source_type=SourceType.COINCOURIER_GENERATED,
                source_article_id=11,
                rich_article_id=None,
                source_url=None,
                title="Generated article",
                published_at=None,
                content_hash=content_hash(),
                content_version="rich-v1",
            )

    def test_embedding_serialization_enforces_dimension_and_finite_values(self):
        vector = [0.0] * VECTOR_DIMENSIONS
        vector[0] = 1.0
        serialized = serialize_embedding(vector)
        self.assertTrue(serialized.startswith("[1.0,0.0"))
        self.assertNotIn(" ", serialized)
        with self.assertRaises(ValueError):
            serialize_embedding(vector[:-1])
        vector[-1] = math.nan
        with self.assertRaises(ValueError):
            serialize_embedding(vector)


class VectorInfrastructureTests(unittest.TestCase):
    def test_vector_migrations_are_independent_from_application_migrations(self):
        vector_dir = REPOSITORY_DIR / "maintenance" / "vector_migrations"
        schema = (vector_dir / "001_vector_schema.sql").read_text(encoding="utf-8")
        indexes = (vector_dir / "002_vector_indexes.sql").read_text(encoding="utf-8")
        self.assertIn("VECTOR(1536)", schema)
        self.assertIn("CREATE VECTOR INDEX IF NOT EXISTS", indexes)
        self.assertNotIn("cryptonewsapi", schema.lower())
        self.assertNotIn("rich_crpytonews", schema.lower())

    def test_local_compose_is_loopback_only_and_mariadb_118(self):
        compose = (
            REPOSITORY_DIR / "maintenance" / "vector" / "docker-compose.vector.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("image: mariadb:11.8", compose)
        self.assertIn('127.0.0.1:${VECTOR_MARIADB_PORT:-13309}:3306', compose)
        self.assertIn("getnewsapi-vector-mariadb-data", compose)


if __name__ == "__main__":
    unittest.main()
