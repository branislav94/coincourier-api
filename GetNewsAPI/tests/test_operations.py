from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import Mock, patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = PROJECT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import fetcher
from operations import jobs
from operations.jobs import JobRequest
from operations.locking import advisory_job_lock, job_lock_name
from operations.models import DatabaseTarget, JobStatus, ScheduleClass, WorkResult
from runtime.config_validation import format_config_issues, validate_runtime_config
import tasks


def base_environment() -> dict[str, str]:
    return {
        "APP_ENV": "test",
        "DB_USER": "app_user",
        "DB_PASSWORD": "app-secret",
        "DB_HOST": "127.0.0.1",
        "DB_PORT": "3306",
        "DB_NAME": "coincourier_job_test",
        "DB_SSL_ENABLED": "false",
        "ENABLE_APSCHEDULER": "false",
        "JOB_LOCK_TIMEOUT_SECONDS": "1",
        "VECTOR_ENABLED": "false",
        "EMBEDDING_ENABLED": "false",
        "SEMANTIC_SHADOW_ENABLED": "false",
    }


def process_environment() -> dict[str, str]:
    environment = base_environment()
    environment.update(
        {
            "GOOGLE_API_KEY": "google-secret",
            "PRIMARY_LLM_PROVIDER": "grok",
            "LLM_FALLBACK_PROVIDER": "openai",
            "GROK_API_KEY": "grok-secret",
            "OPENAI_API_KEY": "openai-secret",
        }
    )
    return environment


def publish_environment() -> dict[str, str]:
    environment = base_environment()
    environment.update(
        {
            "WP_API_URL": "https://wordpress.example.test",
            "WP_USERNAME": "publisher",
            "WP_APP_PASSWORD": "wordpress-secret",
            "WP_DB_USER": "wp_user",
            "WP_DB_PASSWORD": "wp-db-secret",
            "WP_DB_HOST": "127.0.0.1",
            "WP_DB_PORT": "3306",
            "WP_DB_NAME": "wordpress_test",
            "WP_DB_SSL_ENABLED": "false",
            "PRIMARY_IMAGE_PROVIDER": "grok",
            "IMAGE_FALLBACK_PROVIDER": "openai",
            "OPENAI_IMAGE_FALLBACK": "true",
            "GROK_API_KEY": "grok-secret",
            "OPENAI_API_KEY": "openai-secret",
        }
    )
    return environment


def pipeline_environment() -> dict[str, str]:
    environment = process_environment()
    environment.update(publish_environment())
    return environment


def embedding_environment(*, worker: bool = False) -> dict[str, str]:
    environment = base_environment()
    environment.update(
        {
            "VECTOR_ENABLED": "true",
            "EMBEDDING_ENABLED": "true" if worker else "false",
            "VECTOR_DB_USER": "vector_user",
            "VECTOR_DB_PASSWORD": "vector-secret",
            "VECTOR_DB_HOST": "127.0.0.1",
            "VECTOR_DB_PORT": "3306",
            "VECTOR_DB_NAME": "coincourier_vectors_job_test",
            "VECTOR_DB_SSL_ENABLED": "false",
            "EMBEDDING_PROVIDER": "openai",
            "EMBEDDING_MODEL": "text-embedding-3-small",
            "EMBEDDING_DIMENSIONS": "1536",
            "EMBEDDING_CHUNKER_VERSION": "chunk-v1",
        }
    )
    if worker:
        environment["OPENAI_API_KEY"] = "openai-secret"
    return environment


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None

    def execute(self, sql, params=()):
        self.connection.statements.append((" ".join(sql.split()), params))
        if "SELECT DATABASE()" in sql:
            self.row = (self.connection.database,)
        elif "GET_LOCK" in sql:
            lock_result = (
                None if self.connection.acquire is None else int(self.connection.acquire)
            )
            self.row = (lock_result,)
        elif "RELEASE_LOCK" in sql:
            self.row = (1,)

    def fetchone(self):
        return self.row

    def close(self):
        pass


class FakeConnection:
    def __init__(self, database: str, *, acquire: bool | None = True):
        self.database = database
        self.acquire = acquire
        self.autocommit = False
        self.closed = False
        self.statements: list[tuple[str, tuple]] = []

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class CatalogTests(unittest.TestCase):
    def test_catalog_is_explicit_and_classifies_every_supported_job(self):
        specs = {spec.name: spec for spec in jobs.JOB_CATALOG}
        self.assertEqual(
            set(specs),
            {
                "fetch_once",
                "pipeline_once",
                "process",
                "publish",
                "embedding_ingest",
                "embedding_worker",
                "embedding_backfill",
            },
        )
        self.assertEqual(specs["fetch_once"].schedule_class, ScheduleClass.RECURRING)
        self.assertEqual(specs["pipeline_once"].schedule_class, ScheduleClass.RECURRING)
        self.assertEqual(specs["embedding_backfill"].schedule_class, ScheduleClass.MANUAL)
        self.assertEqual(
            specs["embedding_worker"].schedule_class,
            ScheduleClass.ROLLOUT_DECISION,
        )
        self.assertTrue(specs["embedding_worker"].parallel_safe)
        self.assertIsNone(specs["embedding_worker"].conflict_group)

    def test_pipeline_commands_share_conflict_group(self):
        specs = {spec.name: spec for spec in jobs.JOB_CATALOG}
        groups = {specs[name].conflict_group for name in ("pipeline_once", "process", "publish")}
        self.assertEqual(groups, {"pipeline-workflow"})

    def test_ingest_and_backfill_share_registration_conflict_group(self):
        specs = {spec.name: spec for spec in jobs.JOB_CATALOG}
        self.assertEqual(
            specs["embedding_ingest"].conflict_group,
            specs["embedding_backfill"].conflict_group,
        )

    def test_embedding_jobs_expose_existing_bounds_and_backfill_is_manual(self):
        specs = {spec.name: spec for spec in jobs.JOB_CATALOG}
        self.assertIn("maximum=1000", specs["embedding_ingest"].bounds)
        self.assertIn("maximum=100", specs["embedding_worker"].bounds)
        self.assertIn("EMBEDDING_MAX_CHUNKS_PER_JOB", specs["embedding_worker"].bounds)
        self.assertIn("EMBEDDING_BACKFILL_PAGE_SIZE", specs["embedding_backfill"].bounds)
        self.assertEqual(
            specs["embedding_backfill"].schedule_class,
            ScheduleClass.MANUAL,
        )

    def test_catalog_is_read_only_and_imports_no_job_implementation(self):
        output = io.StringIO()
        with (
            patch("mysql.connector.connect") as connect,
            patch.object(socket, "create_connection") as create_connection,
            redirect_stdout(output),
        ):
            self.assertEqual(tasks.run_job_catalog(), 0)
        connect.assert_not_called()
        create_connection.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["operation"], "job_catalog")


class ConfigurationTests(unittest.TestCase):
    def test_fetch_validates_only_fetch_requirements(self):
        environment = base_environment()
        environment.update(
            {
                "CRYPTO_NEWS_TOKEN": "crypto-secret",
                "OPENAI_API_KEY": "openai-secret",
            }
        )
        self.assertEqual(validate_runtime_config(environment, profile="fetch"), ())
        self.assertNotIn("WP_API_URL", environment)
        self.assertNotIn("GOOGLE_API_KEY", environment)

    def test_process_does_not_require_wordpress_configuration(self):
        environment = process_environment()
        self.assertEqual(validate_runtime_config(environment, profile="process"), ())
        self.assertNotIn("WP_API_URL", environment)

    def test_pipeline_requires_process_and_publish_configuration_not_fetch_token(self):
        environment = pipeline_environment()
        self.assertEqual(validate_runtime_config(environment, profile="pipeline"), ())
        self.assertNotIn("CRYPTO_NEWS_TOKEN", environment)

    def test_development_scheduler_pipeline_does_not_require_fetch_token(self):
        environment = pipeline_environment()
        environment["ENABLE_APSCHEDULER"] = "true"
        self.assertEqual(validate_runtime_config(environment, profile="pipeline"), ())
        self.assertNotIn("CRYPTO_NEWS_TOKEN", environment)

    def test_production_rejects_in_process_scheduler(self):
        environment = base_environment()
        environment.update({"APP_ENV": "production", "ENABLE_APSCHEDULER": "true"})
        rendered = format_config_issues(validate_runtime_config(environment, profile="web"))
        self.assertIn("ENABLE_APSCHEDULER: must be false in production", rendered)

    def test_disabled_embedding_worker_calls_nothing_and_returns_zero(self):
        executor = Mock()
        connect = Mock()
        with patch.dict(jobs.EXECUTORS, {"embedding_worker": executor}):
            result = jobs.run_job(
                "embedding_worker",
                environment=base_environment(),
                connection_factory=connect,
            )
        self.assertEqual(result.status, JobStatus.DISABLED)
        self.assertEqual(result.exit_code, 0)
        executor.assert_not_called()
        connect.assert_not_called()

    def test_enabled_embedding_worker_without_key_fails_before_work(self):
        environment = embedding_environment(worker=True)
        environment.pop("OPENAI_API_KEY")
        executor = Mock()
        with patch.dict(jobs.EXECUTORS, {"embedding_worker": executor}):
            result = jobs.run_job("embedding_worker", environment=environment)
        self.assertEqual(result.status, JobStatus.CONFIGURATION_ERROR)
        self.assertEqual(result.exit_code, 1)
        executor.assert_not_called()

    def test_embedding_worker_does_not_validate_unused_job_lock_timeout(self):
        environment = embedding_environment(worker=True)
        environment["JOB_LOCK_TIMEOUT_SECONDS"] = "61"
        self.assertEqual(
            validate_runtime_config(environment, profile="embedding_worker"),
            (),
        )

    def test_configuration_output_never_contains_credentials(self):
        environment = process_environment()
        secret = environment["DB_PASSWORD"]
        environment.pop("DB_HOST")
        result = jobs.run_job("process", environment=environment)
        rendered = jobs.render_job_result(result)
        self.assertEqual(result.status, JobStatus.CONFIGURATION_ERROR)
        self.assertNotIn(secret, rendered)

    def test_job_execution_never_changes_feature_flags(self):
        environment = process_environment()
        before = dict(environment)
        connection = FakeConnection(environment["DB_NAME"])
        with patch.dict(
            jobs.EXECUTORS,
            {"process": lambda _request: WorkResult(JobStatus.NO_WORK, "process")},
        ):
            jobs.run_job(
                "process",
                environment=environment,
                connection_factory=lambda _target: connection,
            )
        self.assertEqual(environment, before)


class LockingTests(unittest.TestCase):
    def test_unsafe_job_uses_expected_advisory_lock_without_transaction(self):
        environment = process_environment()
        connection = FakeConnection(environment["DB_NAME"])
        executor = Mock(return_value=WorkResult(JobStatus.NO_WORK, "process"))
        with patch.dict(jobs.EXECUTORS, {"process": executor}):
            result = jobs.run_job(
                "process",
                environment=environment,
                connection_factory=lambda _target: connection,
            )
        self.assertEqual(result.status, JobStatus.NO_WORK)
        self.assertTrue(connection.autocommit)
        self.assertTrue(connection.closed)
        sql = [statement for statement, _params in connection.statements]
        self.assertTrue(any("GET_LOCK" in statement for statement in sql))
        self.assertTrue(any("RELEASE_LOCK" in statement for statement in sql))
        executor.assert_called_once()

    def test_busy_conflict_group_skips_with_zero_exit(self):
        environment = process_environment()
        connection = FakeConnection(environment["DB_NAME"], acquire=False)
        executor = Mock()
        with patch.dict(jobs.EXECUTORS, {"process": executor}):
            result = jobs.run_job(
                "process",
                environment=environment,
                connection_factory=lambda _target: connection,
            )
        self.assertEqual(result.status, JobStatus.SKIPPED_ALREADY_RUNNING)
        self.assertEqual(result.exit_code, 0)
        executor.assert_not_called()
        self.assertFalse(any("RELEASE_LOCK" in sql for sql, _params in connection.statements))

    def test_database_lock_error_is_not_reported_as_contention(self):
        environment = process_environment()
        connection = FakeConnection(environment["DB_NAME"], acquire=None)
        executor = Mock()
        with patch.dict(jobs.EXECUTORS, {"process": executor}):
            result = jobs.run_job(
                "process",
                environment=environment,
                connection_factory=lambda _target: connection,
            )
        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.reason_type, "JobLockError")
        self.assertTrue(connection.closed)
        executor.assert_not_called()

    def test_pipeline_overlap_is_prevented_with_durable_processing_off_and_on(self):
        for durable_enabled in ("false", "true"):
            with self.subTest(durable_enabled=durable_enabled):
                environment = pipeline_environment()
                environment["PROCESS_DURABLE_CLAIMS_ENABLED"] = durable_enabled
                connection = FakeConnection(environment["DB_NAME"], acquire=False)
                executor = Mock()
                with patch.dict(jobs.EXECUTORS, {"pipeline": executor}):
                    result = jobs.run_job(
                        "pipeline_once",
                        environment=environment,
                        connection_factory=lambda _target: connection,
                    )
                self.assertEqual(result.status, JobStatus.SKIPPED_ALREADY_RUNNING)
                self.assertEqual(result.exit_code, 0)
                executor.assert_not_called()

    def test_lock_releases_after_executor_exception(self):
        environment = process_environment()
        connection = FakeConnection(environment["DB_NAME"])
        with patch.dict(
            jobs.EXECUTORS,
            {"process": Mock(side_effect=RuntimeError("private detail"))},
        ):
            result = jobs.run_job(
                "process",
                environment=environment,
                connection_factory=lambda _target: connection,
            )
        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(result.reason_type, "RuntimeError")
        self.assertTrue(any("RELEASE_LOCK" in sql for sql, _params in connection.statements))
        self.assertTrue(connection.closed)

    def test_conflicting_jobs_share_name_and_other_groups_do_not(self):
        pipeline = job_lock_name("pipeline-workflow", DatabaseTarget.APP, "coincourier")
        publish = job_lock_name("pipeline-workflow", DatabaseTarget.APP, "coincourier")
        registration = job_lock_name(
            "embedding-registration",
            DatabaseTarget.VECTOR,
            "coincourier_vectors",
        )
        self.assertEqual(pipeline, publish)
        self.assertNotEqual(pipeline, registration)
        self.assertTrue(pipeline.startswith("getnewsapi:job:v1:"))
        self.assertNotIn("getnewsapi:migrate", pipeline)

    def test_lock_timeout_is_bounded_before_connect(self):
        environment = process_environment()
        environment["JOB_LOCK_TIMEOUT_SECONDS"] = "61"
        connect = Mock()
        result = jobs.run_job(
            "process",
            environment=environment,
            connection_factory=connect,
        )
        self.assertEqual(result.status, JobStatus.CONFIGURATION_ERROR)
        connect.assert_not_called()

    def test_lock_context_releases_on_controlled_success(self):
        environment = process_environment()
        connection = FakeConnection(environment["DB_NAME"])
        with advisory_job_lock(
            "pipeline-workflow",
            DatabaseTarget.APP,
            environment=environment,
            timeout_seconds=1,
            connection_factory=lambda _target: connection,
        ) as lock:
            self.assertTrue(lock.acquired)
        self.assertTrue(any("RELEASE_LOCK" in sql for sql, _params in connection.statements))


class PipelineTests(unittest.TestCase):
    def test_process_then_publish_order(self):
        events: list[str] = []
        with (
            patch.object(
                jobs,
                "_execute_process",
                side_effect=lambda _request: events.append("process")
                or WorkResult(JobStatus.SUCCESS, "process", {"attempted": 1}),
            ),
            patch.object(
                jobs,
                "_execute_publish",
                side_effect=lambda _request: events.append("publish")
                or WorkResult(JobStatus.SUCCESS, "publish", {"attempted": 1}),
            ),
        ):
            result = jobs._execute_pipeline(JobRequest())
        self.assertEqual(events, ["process", "publish"])
        self.assertEqual(result.status, JobStatus.SUCCESS)

    def test_no_processing_work_still_attempts_publish(self):
        publish = Mock(return_value=WorkResult(JobStatus.SUCCESS, "publish", {"attempted": 1}))
        with (
            patch.object(
                jobs,
                "_execute_process",
                return_value=WorkResult(JobStatus.NO_WORK, "process", {"attempted": 0}),
            ),
            patch.object(jobs, "_execute_publish", publish),
        ):
            result = jobs._execute_pipeline(JobRequest())
        publish.assert_called_once()
        self.assertEqual(result.status, JobStatus.SUCCESS)

    def test_process_exception_still_attempts_publish_and_returns_failure(self):
        publish = Mock(return_value=WorkResult(JobStatus.NO_WORK, "publish"))
        with (
            patch.object(jobs, "_execute_process", side_effect=RuntimeError("private")),
            patch.object(jobs, "_execute_publish", publish),
        ):
            result = jobs._execute_pipeline(JobRequest())
        publish.assert_called_once()
        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(result.stage, "process")

    def test_publish_exception_after_process_is_nonzero_failure(self):
        with (
            patch.object(
                jobs,
                "_execute_process",
                return_value=WorkResult(JobStatus.SUCCESS, "process", {"attempted": 1}),
            ),
            patch.object(jobs, "_execute_publish", side_effect=RuntimeError("private")),
        ):
            result = jobs._execute_pipeline(JobRequest())
        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(result.stage, "publish")

    def test_item_failures_do_not_prevent_publish(self):
        publish = Mock(return_value=WorkResult(JobStatus.SUCCESS, "publish"))
        with (
            patch.object(
                jobs,
                "_execute_process",
                return_value=WorkResult(JobStatus.FAILED, "process", {"failed": 1}),
            ),
            patch.object(jobs, "_execute_publish", publish),
        ):
            result = jobs._execute_pipeline(JobRequest())
        publish.assert_called_once()
        self.assertEqual(result.status, JobStatus.FAILED)


class EmbeddingConcurrencyTests(unittest.TestCase):
    def test_parallel_embedding_worker_uses_durable_claims_without_job_lock(self):
        environment = embedding_environment(worker=True)
        executor = Mock(return_value=WorkResult(JobStatus.NO_WORK, "embedding_worker"))
        connect = Mock()
        with patch.dict(jobs.EXECUTORS, {"embedding_worker": executor}):
            result = jobs.run_job(
                "embedding_worker",
                environment=environment,
                connection_factory=connect,
            )
        self.assertEqual(result.status, JobStatus.NO_WORK)
        executor.assert_called_once()
        connect.assert_not_called()

    def test_ingest_and_backfill_use_vector_lock_target(self):
        environment = embedding_environment()
        for name in ("embedding_ingest", "embedding_backfill"):
            with self.subTest(name=name):
                connection = FakeConnection(environment["VECTOR_DB_NAME"])
                seen_targets: list[DatabaseTarget] = []
                with patch.dict(
                    jobs.EXECUTORS,
                    {jobs.job_by_name(name).executor: lambda _request: WorkResult(JobStatus.NO_WORK, name)},
                ):
                    result = jobs.run_job(
                        name,
                        environment=environment,
                        connection_factory=lambda target: seen_targets.append(target) or connection,
                    )
                self.assertEqual(result.status, JobStatus.NO_WORK)
                self.assertEqual(seen_targets, [DatabaseTarget.VECTOR])


class FetchResultTests(unittest.TestCase):
    def test_all_provider_request_failures_are_not_reported_as_no_work(self):
        with patch.object(fetcher, "_fetch", return_value={fetcher._FETCH_ERROR_KEY: True}):
            with self.assertRaisesRegex(RuntimeError, "all CryptoNews requests failed"):
                fetcher._pull_batch()

    def test_fetch_lock_contention_returns_explicit_skip(self):
        connection = Mock()
        connection.is_connected.return_value = True
        cursor = Mock()
        cursor.fetchone.return_value = (123,)
        connection.cursor.return_value = cursor
        with (
            patch.object(fetcher, "get_db_connection", return_value=connection),
            patch.object(fetcher, "_get_lock", return_value=False),
        ):
            result = fetcher.run_fetch_cycle()
        self.assertEqual(result["status"], "skipped_already_running")

    def test_fetch_exception_returns_failure_and_releases_lock(self):
        connection = Mock()
        connection.is_connected.return_value = True
        with (
            patch.object(fetcher, "get_db_connection", return_value=connection),
            patch.object(fetcher, "_get_lock", return_value=True),
            patch.object(fetcher, "_pull_batch", side_effect=RuntimeError("private")),
            patch.object(fetcher, "_release_lock", return_value=True) as release,
            patch.object(fetcher.logging, "exception"),
        ):
            result = fetcher.run_fetch_cycle()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason_type"], "RuntimeError")
        release.assert_called_once_with(connection, "news_fetcher_lock")


class TaskCommandTests(unittest.TestCase):
    def test_explicit_one_shot_commands_dispatch(self):
        for command, runner_name in (
            ("fetch_once", "run_fetch_once"),
            ("pipeline_once", "run_pipeline_once"),
        ):
            with self.subTest(command=command), patch.object(
                sys, "argv", ["tasks.py", command]
            ), patch.object(tasks, runner_name, return_value=0) as runner:
                self.assertEqual(tasks.main(), 0)
                runner.assert_called_once_with()

    def test_actual_job_failure_exit_is_preserved(self):
        with (
            patch.object(sys, "argv", ["tasks.py", "pipeline_once"]),
            patch.object(tasks, "run_pipeline_once", return_value=1),
        ):
            self.assertEqual(tasks.main(), 1)

    def test_invalid_backfill_source_fails_before_operational_work(self):
        with patch.object(tasks, "_run_operational_job") as run:
            with self.assertRaisesRegex(ValueError, "source must be source or generated"):
                tasks.run_embedding_backfill("invalid")
        run.assert_not_called()

    def test_unsupported_command_exits_nonzero(self):
        with (
            patch.object(sys, "argv", ["tasks.py", "not_a_job"]),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(tasks.main(), 2)

    def test_help_and_catalog_do_not_connect(self):
        for command in ("help", "job_catalog"):
            with self.subTest(command=command), patch.object(
                sys, "argv", ["tasks.py", command]
            ), patch("mysql.connector.connect") as connect, patch.object(
                socket, "create_connection"
            ) as create_connection, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(tasks.main(), 0)
                connect.assert_not_called()
                create_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
