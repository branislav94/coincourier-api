from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = PROJECT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import tasks
from deployment.migrations import (
    MIGRATIONS,
    compute_checksum,
    execution_sequence,
    forward_migrations,
    migration_by_id,
    validate_manifest,
)
from deployment.models import (
    ArtifactState,
    FeatureStatus,
    MigrationObservation,
    MigrationStatus,
    MigrationTarget,
    OperationResult,
    TargetInfo,
)
from deployment.preflight import MigrationSafetyError, guard_target, migration_lock
from deployment.service import MigrationService, render_readiness
from deployment.verification import (
    APP_DUPLICATE_COLUMNS,
    APP_DUPLICATE_INDEXES,
    APP_PHASE2_INDEXES,
    APP_PHASE2_RAW_COLUMNS,
    APP_PHASE2_RICH_COLUMNS,
    SEMANTIC_COLUMNS,
    SEMANTIC_INDEXES,
    VECTOR_COLUMNS,
    VECTOR_INDEXES,
    verify_artifact,
    verify_target_schema,
)


def environment(*, production: bool = False, target: MigrationTarget = MigrationTarget.APP):
    values = {
        "APP_ENV": "production" if production else "test",
        "MIGRATION_TEST_MODE": "true",
        "MIGRATION_LOCK_TIMEOUT_SECONDS": "2",
        "DB_USER": "app_user",
        "DB_PASSWORD": "app-password-secret",
        "DB_HOST": "127.0.0.1",
        "DB_NAME": "coincourier_api_test",
        "VECTOR_DB_USER": "vector_user",
        "VECTOR_DB_PASSWORD": "vector-password-secret",
        "VECTOR_DB_HOST": "127.0.0.1",
        "VECTOR_DB_NAME": "coincourier_vectors_test",
        "OPENAI_API_KEY": "provider-secret",
    }
    return values


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, sql, params=(), **kwargs):
        self.connection.statements.append((" ".join(sql.split()), params, kwargs))
        self.rows = list(self.connection.responder(sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def close(self):
        pass


class _Connection:
    def __init__(self, responder=None):
        self.responder = responder or (lambda _sql, _params: [])
        self.statements = []
        self.closed = False
        self.autocommit = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


def guard_connection(database, version, tables, *, vector_distance=0.0):
    def respond(sql, _params):
        normalized = " ".join(sql.split()).upper()
        if "SELECT DATABASE(), VERSION()" in normalized:
            return [(database, version)]
        if "INFORMATION_SCHEMA.TABLES" in normalized:
            return [(table,) for table in tables]
        if "VEC_DISTANCE_COSINE" in normalized:
            return [(vector_distance,)]
        return []

    return _Connection(respond)


def observations(target, status):
    return [
        MigrationObservation(
            migration,
            status,
            ArtifactState.COMPLETE if status == MigrationStatus.APPLIED else ArtifactState.NONE,
            "synthetic",
        )
        for migration in forward_migrations(target)
    ]


@contextmanager
def accepted_lock(*_args, **_kwargs):
    yield


class ManifestTests(unittest.TestCase):
    def test_application_manifest_order_is_explicit(self):
        self.assertEqual(
            [item.id for item in execution_sequence(MigrationTarget.APP)],
            ["app-001", "app-002", "app-003", "app-004", "app-006", "app-007"],
        )

    def test_rollback_utility_is_excluded_from_forward_apply(self):
        self.assertFalse(migration_by_id("app-005").include_in_apply)
        self.assertNotIn("app-005", [item.id for item in execution_sequence(MigrationTarget.APP)])

    def test_fresh_start_scripts_are_excluded_from_existing_database_chain(self):
        ids = [item.id for item in execution_sequence(MigrationTarget.APP)]
        self.assertNotIn("app-fresh-dry-run", ids)
        self.assertNotIn("app-fresh-apply", ids)

    def test_vector_manifest_order_is_explicit(self):
        self.assertEqual(
            [item.id for item in execution_sequence(MigrationTarget.VECTOR)],
            ["vector-001", "vector-002", "vector-003"],
        )

    def test_checksums_are_deterministic_and_paths_exist(self):
        validate_manifest(REPOSITORY_DIR)
        for migration in MIGRATIONS:
            path = migration.path(REPOSITORY_DIR)
            self.assertTrue(path.is_file())
            self.assertEqual(compute_checksum(path), migration.checksum)

    def test_excluded_utility_checksum_drift_blocks_before_database_access(self):
        for utility_id in ("app-fresh-dry-run", "app-fresh-apply"):
            for target in (MigrationTarget.APP, MigrationTarget.VECTOR):
                with self.subTest(utility=utility_id, target=target), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    for migration in MIGRATIONS:
                        destination = migration.path(root)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(migration.path(REPOSITORY_DIR).read_bytes())
                    utility = migration_by_id(utility_id).path(root)
                    utility.write_bytes(utility.read_bytes().replace(b"\n", b"\r\n"))
                    connect = Mock()
                    service = MigrationService(
                        connection_factory=connect,
                        environment=environment(target=target),
                        root=root,
                    )
                    result = service.plan(target)
                    self.assertFalse(result.success)
                    self.assertEqual(result.outcome, "PLAN_BLOCKED")
                    self.assertIn("checksum drift", "\n".join(result.lines))
                    self.assertIn(utility_id, "\n".join(result.lines))
                    connect.assert_not_called()

    def test_every_maintenance_sql_file_is_classified(self):
        actual = {
            path.relative_to(REPOSITORY_DIR).as_posix()
            for path in (REPOSITORY_DIR / "maintenance").rglob("*.sql")
        }
        manifested = {migration.relative_path for migration in MIGRATIONS}
        self.assertEqual(actual, manifested)


class TargetGuardTests(unittest.TestCase):
    def test_app_target_accepts_expected_application_database(self):
        connection = guard_connection(
            "coincourier_api_test", "10.4.32-MariaDB", {"cryptonewsapi", "rich_crpytonews"}
        )
        info = guard_target(connection, MigrationTarget.APP, "coincourier_api_test")
        self.assertEqual(info.connected_database, "coincourier_api_test")

    def test_app_target_rejects_vector_database(self):
        connection = guard_connection(
            "coincourier_vectors_test", "11.8.2-MariaDB", {"vector_documents"}
        )
        with self.assertRaises(MigrationSafetyError):
            guard_target(connection, MigrationTarget.APP, "coincourier_vectors_test")

    def test_vector_target_accepts_expected_database_and_capability(self):
        connection = guard_connection("coincourier_vectors_test", "11.8.2-MariaDB", set())
        info = guard_target(connection, MigrationTarget.VECTOR, "coincourier_vectors_test")
        self.assertEqual(info.target, MigrationTarget.VECTOR)

    def test_vector_target_rejects_application_database(self):
        connection = guard_connection(
            "coincourier_vectors_test", "11.8.2-MariaDB", {"cryptonewsapi"}
        )
        with self.assertRaises(MigrationSafetyError):
            guard_target(connection, MigrationTarget.VECTOR, "coincourier_vectors_test")

    def test_vector_rejects_unsupported_server_version(self):
        connection = guard_connection("coincourier_vectors_test", "11.7.2-MariaDB", set())
        with self.assertRaises(MigrationSafetyError):
            guard_target(connection, MigrationTarget.VECTOR, "coincourier_vectors_test")

    def test_database_name_mismatch_is_rejected_without_secrets(self):
        connection = guard_connection(
            "other_test", "10.4.32-MariaDB", {"cryptonewsapi", "rich_crpytonews"}
        )
        with self.assertRaises(MigrationSafetyError) as raised:
            guard_target(connection, MigrationTarget.APP, "coincourier_api_test")
        self.assertNotIn("password", str(raised.exception).lower())
        self.assertNotIn("app-password-secret", str(raised.exception))


class _ContractInspector:
    def __init__(self, *, include_semantic=True, missing_app_index=None):
        self.include_semantic = include_semantic
        self.missing_app_index = missing_app_index

    @staticmethod
    def _columns(expected):
        result = {}
        for name, item in expected.items():
            default = None if item.default is ... else item.default
            extra = item.extra_contains or ""
            generation = " ".join(item.generation_contains)
            column_type = item.column_type.replace("tinyint(1)", "tinyint")
            result[name] = (column_type, item.nullable, default, extra, generation)
        return result

    def tables(self):
        tables = {
            "cryptonewsapi", "rich_crpytonews", "duplicate_assessments",
            "vector_documents", "vector_chunks", "embedding_jobs",
        }
        if self.include_semantic:
            tables.add("semantic_shadow_assessments")
        return tables

    def columns(self, table):
        maps = {
            "cryptonewsapi": APP_PHASE2_RAW_COLUMNS,
            "rich_crpytonews": APP_PHASE2_RICH_COLUMNS,
            "duplicate_assessments": APP_DUPLICATE_COLUMNS,
            **VECTOR_COLUMNS,
            "semantic_shadow_assessments": SEMANTIC_COLUMNS,
        }
        return self._columns(maps.get(table, {}))

    def indexes(self, table):
        result = {}
        for (expected_table, name), contract in {**APP_PHASE2_INDEXES, **VECTOR_INDEXES}.items():
            if expected_table == table and name != self.missing_app_index:
                result[name] = contract
        if table == "duplicate_assessments":
            result.update(APP_DUPLICATE_INDEXES)
        if table == "semantic_shadow_assessments":
            result.update(SEMANTIC_INDEXES)
        if table == "vector_chunks":
            result["idx_vector_chunks_embedding_cosine"] = (False, "VECTOR", ("embedding",))
        return result

    def checks(self, table):
        names = {
            "duplicate_assessments": ["chk_duplicate_assessment_distinct_articles"],
            "vector_documents": ["chk_vector_documents_source_type", "chk_vector_documents_provenance"],
            "vector_chunks": ["chk_vector_chunks_dimensions"],
            "embedding_jobs": ["chk_embedding_jobs_status"],
            "semantic_shadow_assessments": [
                "chk_semantic_shadow_source_id", "chk_semantic_shadow_status",
                "chk_semantic_shadow_bounds", "chk_semantic_shadow_evidence_json",
                "chk_semantic_shadow_result_shape", "chk_semantic_shadow_error_shape",
            ],
        }
        token_text = (
            "article_id candidate_article_id source_article coincourier_generated rich_article_id "
            "source_type embedding_dimensions 1536 pending completed failed query_source_article_id 0 "
            "retrieved error not_ready lookback_hours requested_top_k candidate_count json_valid "
            "json_length best_native_distance error_type safe_error"
        )
        return {name: token_text for name in names.get(table, [])}

    def foreign_keys(self, table):
        return {
            "vector_chunks": {
                "fk_vector_chunks_document": ("document_id", "vector_documents", "id", "CASCADE")
            },
            "embedding_jobs": {
                "fk_embedding_jobs_document": ("document_id", "vector_documents", "id", "CASCADE")
            },
            "semantic_shadow_assessments": {
                "fk_semantic_shadow_query_document": (
                    "query_document_id", "vector_documents", "id", "CASCADE"
                )
            },
        }.get(table, {})


class VerificationTests(unittest.TestCase):
    def test_application_verification_checks_complete_contract(self):
        self.assertEqual(verify_target_schema(_ContractInspector(), MigrationTarget.APP), [])

    def test_missing_application_index_fails_verification(self):
        issues = verify_target_schema(
            _ContractInspector(missing_app_index="uq_rich_crpytonews_wp_post_id"),
            MigrationTarget.APP,
        )
        self.assertTrue(any("uq_rich_crpytonews_wp_post_id" in issue for issue in issues))

    def test_vector_verification_covers_migration_003(self):
        self.assertEqual(verify_target_schema(_ContractInspector(), MigrationTarget.VECTOR), [])

    def test_missing_semantic_schema_fails_vector_verification(self):
        issues = verify_target_schema(
            _ContractInspector(include_semantic=False), MigrationTarget.VECTOR
        )
        self.assertTrue(any("semantic_shadow_assessments" in issue for issue in issues))


class StateClassificationTests(unittest.TestCase):
    def setUp(self):
        self.service = MigrationService(environment=environment())
        self.connection = _Connection()

    def test_schema_present_without_ledger_is_untracked(self):
        with (
            patch("deployment.service.read_ledger", return_value={}),
            patch("deployment.service.artifact_state", return_value=ArtifactState.COMPLETE),
        ):
            actual, _ = self.service._inspect(
                self.connection, MigrationTarget.APP, "coincourier_api_test"
            )
        self.assertTrue(all(item.status == MigrationStatus.UNTRACKED_APPLIED for item in actual))

    def test_partial_schema_without_ledger_is_ambiguous(self):
        with (
            patch("deployment.service.read_ledger", return_value={}),
            patch("deployment.service.artifact_state", return_value=ArtifactState.PARTIAL),
        ):
            actual, _ = self.service._inspect(
                self.connection, MigrationTarget.APP, "coincourier_api_test"
            )
        self.assertTrue(all(item.status == MigrationStatus.AMBIGUOUS for item in actual))

    def test_checksum_mismatch_is_drift(self):
        migration = forward_migrations(MigrationTarget.APP)[0]
        from deployment.models import LedgerRecord

        with (
            patch(
                "deployment.service.read_ledger",
                return_value={migration.id: LedgerRecord(migration.id, "0" * 64, "old")},
            ),
            patch("deployment.service.artifact_state", return_value=ArtifactState.COMPLETE),
        ):
            actual, _ = self.service._inspect(
                self.connection, MigrationTarget.APP, "coincourier_api_test"
            )
        self.assertEqual(actual[0].status, MigrationStatus.DRIFT)

    def test_ledger_success_with_missing_schema_is_drift(self):
        migration = forward_migrations(MigrationTarget.APP)[0]
        from deployment.models import LedgerRecord

        with (
            patch(
                "deployment.service.read_ledger",
                return_value={
                    migration.id: LedgerRecord(migration.id, migration.checksum, "phase7c1-v1")
                },
            ),
            patch("deployment.service.artifact_state", return_value=ArtifactState.NONE),
        ):
            actual, _ = self.service._inspect(
                self.connection, MigrationTarget.APP, "coincourier_api_test"
            )
        self.assertEqual(actual[0].status, MigrationStatus.DRIFT)


class ServiceCommandTests(unittest.TestCase):
    def _service(self, *, production=False):
        connection = _Connection()
        service = MigrationService(
            connection_factory=lambda _target: connection,
            environment=environment(production=production),
            root=REPOSITORY_DIR,
        )
        return service, connection

    def _service_patches(self, target, state):
        return (
            patch("deployment.service.guard_target", return_value=TargetInfo(
                target, environment()["DB_NAME" if target == MigrationTarget.APP else "VECTOR_DB_NAME"],
                environment()["DB_NAME" if target == MigrationTarget.APP else "VECTOR_DB_NAME"],
                "11.8.2-MariaDB" if target == MigrationTarget.VECTOR else "10.4.32-MariaDB",
            )),
            patch.object(MigrationService, "_inspect", return_value=(observations(target, state), [])),
        )

    def test_plan_is_read_only_and_reports_pending(self):
        service, connection = self._service()
        guard_patch, inspect_patch = self._service_patches(MigrationTarget.APP, MigrationStatus.PENDING)
        with guard_patch, inspect_patch:
            result = service.plan(MigrationTarget.APP)
        self.assertTrue(result.success)
        self.assertTrue(any("pending=3" in line for line in result.lines))
        self.assertEqual(connection.statements, [])

    def test_failed_preflight_blocks_apply_before_sql(self):
        service, _ = self._service()
        guard_patch, inspect_patch = self._service_patches(MigrationTarget.APP, MigrationStatus.PENDING)
        failed = Mock(passed=False, result_set_counts=(1,), duration_seconds=0.01)
        with (
            guard_patch, inspect_patch,
            patch("deployment.service.migration_lock", accepted_lock),
            patch("deployment.service.run_preflight", return_value=failed),
            patch("deployment.service.execute_script") as execute,
        ):
            result = service.apply(MigrationTarget.APP, allow_disposable=True)
        self.assertEqual(result.outcome, "PREFLIGHT_FAILED")
        execute.assert_not_called()

    def test_production_backup_and_restore_attestations_are_both_required(self):
        service, connection = self._service(production=True)
        missing_backup = service.apply(MigrationTarget.APP, restore_tested=True)
        missing_restore = service.apply(MigrationTarget.APP, backup_confirmed=True)
        self.assertEqual(missing_backup.outcome, "ACKNOWLEDGEMENT_REQUIRED")
        self.assertEqual(missing_restore.outcome, "ACKNOWLEDGEMENT_REQUIRED")
        output = "\n".join(missing_backup.lines + missing_restore.lines)
        self.assertIn("operator attestation", output)
        self.assertIn("did not verify", output)
        self.assertEqual(connection.statements, [])

    def test_nonproduction_requires_explicit_disposable_mode(self):
        service, connection = self._service()
        result = service.apply(MigrationTarget.APP)
        self.assertEqual(result.outcome, "ACKNOWLEDGEMENT_REQUIRED")
        self.assertEqual(connection.statements, [])

    def test_production_attestations_permit_progression(self):
        service, _ = self._service(production=True)
        guard_patch, inspect_patch = self._service_patches(
            MigrationTarget.APP, MigrationStatus.APPLIED
        )
        with (
            guard_patch,
            inspect_patch,
            patch("deployment.service.migration_lock", accepted_lock),
            patch(
                "deployment.service.run_preflight",
                return_value=Mock(passed=True, result_set_counts=(0,), duration_seconds=0.01),
            ),
            patch("deployment.service.verify_target_schema", return_value=[]),
        ):
            result = service.apply(
                MigrationTarget.APP,
                backup_confirmed=True,
                restore_tested=True,
            )
        self.assertEqual(result.outcome, "NO_PENDING_MIGRATIONS")
        self.assertTrue(
            any("safety_gate=operator_backup_restore_attested" in line for line in result.lines)
        )

    def test_vector_admin_config_does_not_require_vector_feature_flag_and_keeps_tls(self):
        values = environment()
        values["VECTOR_ENABLED"] = "false"
        values.update(
            {
                "VECTOR_DB_SSL_ENABLED": "true",
                "VECTOR_DB_SSL_VERIFY_CERT": "true",
                "VECTOR_DB_SSL_VERIFY_IDENTITY": "true",
                "VECTOR_DB_SSL_CA": "/run/secrets/vector-ca.pem",
            }
        )
        service = MigrationService(environment=values, root=REPOSITORY_DIR)
        config = service._config(MigrationTarget.VECTOR)
        self.assertTrue(config["ssl_verify_cert"])
        self.assertTrue(config["ssl_verify_identity"])
        self.assertEqual(config["ssl_ca"], "/run/secrets/vector-ca.pem")

    def test_apply_runs_only_forward_migrations_in_order_and_records_after_verify(self):
        service, _ = self._service()
        guard_patch, inspect_patch = self._service_patches(MigrationTarget.APP, MigrationStatus.PENDING)
        events = []
        passed = Mock(passed=True, result_set_counts=(0,), duration_seconds=0.01)
        with (
            guard_patch, inspect_patch,
            patch("deployment.service.migration_lock", accepted_lock),
            patch("deployment.service.run_preflight", return_value=passed),
            patch("deployment.service.create_ledger", side_effect=lambda _c: events.append("ledger")),
            patch("deployment.service.execute_script", side_effect=lambda _c, m, _s: events.append(f"sql:{m.id}")),
            patch("deployment.service.verify_artifact", side_effect=lambda _i, key: events.append(f"verify:{key}") or []),
            patch("deployment.service.record_migration", side_effect=lambda _c, m: events.append(f"record:{m.id}")),
            patch("deployment.service.verify_target_schema", return_value=[]),
        ):
            result = service.apply(MigrationTarget.APP, allow_disposable=True)
        self.assertEqual(result.outcome, "APPLIED_AND_VERIFIED")
        sql_ids = [event.removeprefix("sql:") for event in events if event.startswith("sql:")]
        self.assertEqual(sql_ids, ["app-002", "app-004", "app-007"])
        self.assertNotIn("sql:app-005", events)
        self.assertNotIn("sql:app-fresh-apply", events)
        for migration_id in sql_ids:
            self.assertLess(events.index(f"sql:{migration_id}"), events.index(f"record:{migration_id}"))

    def test_apply_failure_stops_later_migrations_and_reports_non_atomicity(self):
        service, _ = self._service()
        guard_patch, inspect_patch = self._service_patches(MigrationTarget.APP, MigrationStatus.PENDING)
        executed = []

        def execute(_connection, migration, _sql):
            executed.append(migration.id)
            if migration.id == "app-004":
                raise RuntimeError("synthetic failure")

        passed = Mock(passed=True, result_set_counts=(0,), duration_seconds=0.01)
        with (
            guard_patch, inspect_patch,
            patch("deployment.service.migration_lock", accepted_lock),
            patch("deployment.service.run_preflight", return_value=passed),
            patch("deployment.service.create_ledger"),
            patch("deployment.service.execute_script", side_effect=execute),
            patch("deployment.service.verify_artifact", return_value=[]),
            patch("deployment.service.record_migration"),
        ):
            result = service.apply(MigrationTarget.APP, allow_disposable=True)
        self.assertEqual(executed, ["app-002", "app-004"])
        self.assertEqual(result.outcome, "APPLY_FAILED_PARTIAL_POSSIBLE")
        self.assertTrue(any("not transactionally atomic" in line for line in result.lines))

    def test_second_apply_is_verified_noop(self):
        service, _ = self._service()
        guard_patch, inspect_patch = self._service_patches(MigrationTarget.APP, MigrationStatus.APPLIED)
        with (
            guard_patch, inspect_patch,
            patch("deployment.service.migration_lock", accepted_lock),
            patch("deployment.service.execute_script") as execute,
            patch(
                "deployment.service.run_preflight",
                return_value=Mock(passed=True, result_set_counts=(0,), duration_seconds=0.01),
            ) as preflight,
            patch("deployment.service.verify_target_schema", return_value=[]),
        ):
            result = service.apply(MigrationTarget.APP, allow_disposable=True)
        self.assertEqual(result.outcome, "NO_PENDING_MIGRATIONS")
        self.assertTrue(any("safety_gate=explicit_disposable_test_mode" in line for line in result.lines))
        self.assertEqual(preflight.call_count, 2)
        execute.assert_not_called()

    def test_apply_succeeded_verify_failed_is_distinct_and_not_recorded(self):
        service, _ = self._service()
        guard_patch, inspect_patch = self._service_patches(MigrationTarget.VECTOR, MigrationStatus.PENDING)
        with (
            guard_patch, inspect_patch,
            patch("deployment.service.migration_lock", accepted_lock),
            patch("deployment.service.create_ledger"),
            patch("deployment.service.execute_script"),
            patch("deployment.service.verify_artifact", return_value=["missing artifact"]),
            patch("deployment.service.record_migration") as record,
        ):
            result = service.apply(MigrationTarget.VECTOR, allow_disposable=True)
        self.assertEqual(result.outcome, "APPLY_SUCCEEDED_VERIFY_FAILED")
        record.assert_not_called()


class LockTests(unittest.TestCase):
    def test_lock_timeout_fails_explicitly(self):
        connection = _Connection(lambda sql, _params: [(0,)] if "GET_LOCK" in sql else [])
        with self.assertRaises(MigrationSafetyError):
            with migration_lock(connection, MigrationTarget.APP, "coincourier_api_test", 1):
                pass

    def test_lock_is_released_after_controlled_failure(self):
        def respond(sql, _params):
            if "GET_LOCK" in sql:
                return [(1,)]
            if "RELEASE_LOCK" in sql:
                return [(1,)]
            return []

        connection = _Connection(respond)
        with self.assertRaises(RuntimeError):
            with migration_lock(connection, MigrationTarget.APP, "coincourier_api_test", 1):
                raise RuntimeError("controlled")
        self.assertTrue(any("RELEASE_LOCK" in sql for sql, _params, _kwargs in connection.statements))


class ReadinessAndTaskTests(unittest.TestCase):
    def test_readiness_requires_schema_and_embedding_configuration(self):
        service = MigrationService(environment=environment(), root=REPOSITORY_DIR)
        app = {item.id: MigrationStatus.APPLIED for item in forward_migrations(MigrationTarget.APP)}
        vector = {item.id: MigrationStatus.APPLIED for item in forward_migrations(MigrationTarget.VECTOR)}
        with patch.object(service, "_read_statuses", side_effect=[(app, None), (vector, None)]):
            statuses = service.feature_readiness()
        self.assertTrue(all(status.ready for status in statuses))

    def test_readiness_does_not_change_feature_flags_or_call_providers(self):
        values = environment()
        values.update({"VECTOR_ENABLED": "false", "EMBEDDING_ENABLED": "false"})
        service = MigrationService(environment=values, root=REPOSITORY_DIR)
        with (
            patch.object(service, "_read_statuses", return_value=({}, "not verified")),
            patch("socket.create_connection") as network,
        ):
            service.feature_readiness()
        network.assert_not_called()
        self.assertEqual(values["VECTOR_ENABLED"], "false")
        self.assertEqual(values["EMBEDDING_ENABLED"], "false")

    def test_task_commands_render_safe_results_and_exit_deterministically(self):
        result = OperationResult("migration_plan", MigrationTarget.APP, True, "PLAN_READY", ["safe"])
        fake_service = Mock()
        fake_service.plan.return_value = result
        with (
            patch("deployment.service.MigrationService", return_value=fake_service),
            patch.object(sys, "argv", ["tasks.py", "migration_plan", "app"]),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(tasks.main(), 0)
        fake_service.plan.assert_called_once_with(MigrationTarget.APP)

    def test_feature_readiness_output_says_activation_none(self):
        output = render_readiness((FeatureStatus("vector", False, ("schema missing",)),))
        self.assertIn("activation=none", output)
        self.assertIn("vector=blocked", output)


if __name__ == "__main__":
    unittest.main()
