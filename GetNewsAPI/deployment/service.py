"""High-level plan, check, apply, verify, and feature-readiness operations."""

from __future__ import annotations

import os
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Mapping

import mysql.connector

from .migrations import (
    ManifestError,
    execution_sequence,
    forward_migrations,
    migration_by_id,
    repository_root,
    validate_manifest,
)
from .models import (
    ArtifactState,
    FeatureStatus,
    MigrationObservation,
    MigrationSpec,
    MigrationStatus,
    MigrationTarget,
    OperationResult,
)
from .preflight import (
    MigrationExecutionError,
    MigrationSafetyError,
    create_ledger,
    execute_script,
    guard_target,
    migration_lock,
    read_ledger,
    record_migration,
    run_preflight,
)
from .verification import SchemaInspector, artifact_state, verify_artifact, verify_target_schema


ConnectionFactory = Callable[[MigrationTarget], Any]


class MigrationService:
    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory | None = None,
        environment: Mapping[str, str] | None = None,
        root: Path | None = None,
    ):
        self._connection_factory = connection_factory
        self.environment = dict(os.environ if environment is None else environment)
        self.root = root or repository_root()

    def _config(self, target: MigrationTarget) -> dict[str, Any]:
        if target == MigrationTarget.APP:
            names = {
                "user": "DB_USER",
                "password": "DB_PASSWORD",
                "host": "DB_HOST",
                "database": "DB_NAME",
            }
            prefix = "DB"
        else:
            names = {
                "user": "VECTOR_DB_USER",
                "password": "VECTOR_DB_PASSWORD",
                "host": "VECTOR_DB_HOST",
                "database": "VECTOR_DB_NAME",
            }
            prefix = "VECTOR_DB"

        config: dict[str, Any] = {
            key: (self.environment.get(env_name) or "").strip()
            for key, env_name in names.items()
        }
        missing = [env_name for key, env_name in names.items() if not config[key]]
        if missing:
            raise MigrationSafetyError(
                f"{target.value} migration configuration is missing: " + ", ".join(missing)
            )
        try:
            config["port"] = int(self.environment.get(f"{prefix}_PORT", "3306"))
            config["connection_timeout"] = int(
                self.environment.get(f"{prefix}_CONNECT_TIMEOUT_SECONDS", "5")
            )
        except ValueError as exc:
            raise MigrationSafetyError(f"{target.value} migration DB port/timeout is invalid") from exc
        if not 1 <= config["port"] <= 65535:
            raise MigrationSafetyError(f"{target.value} migration DB port is out of range")
        if config["connection_timeout"] <= 0:
            raise MigrationSafetyError(f"{target.value} migration DB timeout must be positive")
        config.update(self._tls_options(prefix))
        return config

    def _tls_options(self, prefix: str) -> dict[str, Any]:
        def boolean(name: str, default: bool) -> bool:
            raw = self.environment.get(name)
            if raw is None or not raw.strip():
                return default
            normalized = raw.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            raise MigrationSafetyError(f"{name} must be true or false")

        enabled = boolean(f"{prefix}_SSL_ENABLED", True)
        options: dict[str, Any] = {"ssl_disabled": not enabled}
        if enabled:
            options["ssl_verify_cert"] = boolean(f"{prefix}_SSL_VERIFY_CERT", False)
            options["ssl_verify_identity"] = boolean(f"{prefix}_SSL_VERIFY_IDENTITY", False)
            ca_path = (self.environment.get(f"{prefix}_SSL_CA") or "").strip()
            if ca_path:
                options["ssl_ca"] = ca_path
        return options

    def _open(self, target: MigrationTarget) -> tuple[Any, str]:
        config = self._config(target)
        connection = (
            self._connection_factory(target)
            if self._connection_factory is not None
            else mysql.connector.connect(**config)
        )
        return connection, str(config["database"])

    def _validate_manifest(self) -> None:
        validate_manifest(
            self.root,
            require_test_resources=(self.root / "maintenance" / "testing").is_dir(),
        )

    @staticmethod
    def _close(connection: Any) -> None:
        try:
            connection.close()
        except Exception:
            pass

    def _inspect(
        self,
        connection: Any,
        target: MigrationTarget,
        database: str,
    ) -> tuple[list[MigrationObservation], list[str]]:
        inspector = SchemaInspector(connection, database)
        records = read_ledger(connection, database, target)
        expected_ids = {migration.id for migration in forward_migrations(target)}
        unknown = sorted(set(records) - expected_ids)
        issues = [f"ledger contains unknown migration ID: {migration_id}" for migration_id in unknown]
        observations: list[MigrationObservation] = []
        for migration in forward_migrations(target):
            assert migration.verification_key is not None
            state = artifact_state(inspector, migration.verification_key)
            record = records.get(migration.id)
            if record is not None:
                if record.checksum != migration.checksum:
                    status = MigrationStatus.DRIFT
                    detail = "ledger checksum differs from immutable manifest"
                elif state != ArtifactState.COMPLETE:
                    status = MigrationStatus.DRIFT
                    detail = "ledger records success but schema verification is incomplete"
                else:
                    status = MigrationStatus.APPLIED
                    detail = "ledger and schema agree"
            elif state == ArtifactState.NONE:
                status = MigrationStatus.PENDING
                detail = "no ledger record or migration artifacts"
            elif state == ArtifactState.COMPLETE:
                status = MigrationStatus.UNTRACKED_APPLIED
                detail = "schema artifacts exist without a ledger record; operator adoption is required"
            else:
                status = MigrationStatus.AMBIGUOUS
                detail = "partial or incompatible schema artifacts exist without a ledger record"
            observations.append(MigrationObservation(migration, status, state, detail))
        by_id = {item.migration.id: item for item in observations}
        for item in observations:
            if item.status != MigrationStatus.APPLIED:
                continue
            for dependency in item.migration.dependencies:
                dependency_spec = migration_by_id(dependency)
                if dependency_spec.include_in_apply and by_id[dependency].status != MigrationStatus.APPLIED:
                    issues.append(
                        f"{item.migration.id} is applied while dependency {dependency} is not applied"
                    )
        return observations, issues

    @staticmethod
    def _blocked(observations: list[MigrationObservation], issues: list[str]) -> bool:
        return bool(issues or any(observation.blocked for observation in observations))

    @staticmethod
    def _observation_lines(observations: list[MigrationObservation]) -> list[str]:
        return [
            f"migration={item.migration.id} status={item.status.value} "
            f"artifact={item.artifact_state.value} checksum={item.migration.checksum_prefix} "
            f"detail={item.detail}"
            for item in observations
        ]

    def plan(self, target: MigrationTarget) -> OperationResult:
        lines = [f"operation=migration_plan target={target.value} mode=read_only"]
        connection = None
        try:
            self._validate_manifest()
            connection, configured_database = self._open(target)
            info = guard_target(connection, target, configured_database)
            lines.append(
                f"database={info.connected_database} server={info.server_version} "
                f"configured_database={info.configured_database}"
            )
            for sequence_number, migration in enumerate(execution_sequence(target), start=1):
                lines.append(
                    f"sequence={sequence_number} migration={migration.id} "
                    f"kind={migration.kind.value} checksum={migration.checksum_prefix}"
                )
            observations, issues = self._inspect(connection, target, info.connected_database)
            lines.extend(self._observation_lines(observations))
            lines.extend(f"blocked={issue}" for issue in issues)
            pending = sum(item.status == MigrationStatus.PENDING for item in observations)
            applied = sum(item.status == MigrationStatus.APPLIED for item in observations)
            lines.append(f"summary pending={pending} applied={applied} blocked={self._blocked(observations, issues)}")
            success = not self._blocked(observations, issues)
            return OperationResult(
                "migration_plan", target, success, "PLAN_READY" if success else "PLAN_BLOCKED", lines
            )
        except Exception as exc:
            lines.append(f"blocked={self._safe_error(exc)}")
            return OperationResult("migration_plan", target, False, "PLAN_BLOCKED", lines)
        finally:
            if connection is not None:
                self._close(connection)

    def check(self, target: MigrationTarget) -> OperationResult:
        lines = [f"operation=migration_check target={target.value} mode=read_only"]
        connection = None
        try:
            self._validate_manifest()
            connection, configured_database = self._open(target)
            info = guard_target(connection, target, configured_database)
            lines.append(f"database={info.connected_database} server={info.server_version}")
            for sequence_number, migration in enumerate(execution_sequence(target), start=1):
                lines.append(
                    f"sequence={sequence_number} migration={migration.id} kind={migration.kind.value}"
                )
            observations, issues = self._inspect(connection, target, info.connected_database)
            lines.extend(self._observation_lines(observations))
            if self._blocked(observations, issues):
                lines.extend(f"blocked={issue}" for issue in issues)
                return OperationResult("migration_check", target, False, "CHECK_BLOCKED", lines)

            by_id = {item.migration.id: item for item in observations}
            completed_preflights: set[str] = set()
            for migration in forward_migrations(target):
                observation = by_id[migration.id]
                if observation.status == MigrationStatus.APPLIED:
                    for preflight_id in migration.preflight_ids:
                        preflight = migration_by_id(preflight_id)
                        if not preflight.safe_to_rerun or preflight.id in completed_preflights:
                            continue
                        result = run_preflight(
                            connection,
                            preflight,
                            preflight.path(self.root).read_text(encoding="utf-8"),
                        )
                        completed_preflights.add(preflight.id)
                        lines.append(
                            f"migration={preflight.id} status={'passed' if result.passed else 'failed'} "
                            f"result_counts={','.join(map(str, result.result_set_counts)) or 'none'} "
                            f"duration_ms={int(result.duration_seconds * 1000)}"
                        )
                        if not result.passed:
                            return OperationResult(
                                "migration_check", target, False, "PREFLIGHT_FAILED", lines
                            )
                    continue
                unmet_forward = [
                    dependency
                    for dependency in migration.dependencies
                    if migration_by_id(dependency).include_in_apply
                    and by_id[dependency].status != MigrationStatus.APPLIED
                ]
                if unmet_forward:
                    check_name = "preflight" if migration.preflight_ids else "compatibility_check"
                    lines.append(
                        f"migration={migration.id} {check_name}=deferred "
                        f"reason=pending_dependencies:{','.join(unmet_forward)}"
                    )
                    continue
                for preflight_id in migration.preflight_ids:
                    preflight = migration_by_id(preflight_id)
                    result = run_preflight(
                        connection,
                        preflight,
                        preflight.path(self.root).read_text(encoding="utf-8"),
                    )
                    lines.append(
                        f"migration={preflight.id} status={'passed' if result.passed else 'failed'} "
                        f"result_counts={','.join(map(str, result.result_set_counts)) or 'none'} "
                        f"duration_ms={int(result.duration_seconds * 1000)}"
                    )
                    if not result.passed:
                        return OperationResult("migration_check", target, False, "PREFLIGHT_FAILED", lines)
            return OperationResult("migration_check", target, True, "CHECK_PASSED", lines)
        except Exception as exc:
            lines.append(f"blocked={self._safe_error(exc)}")
            return OperationResult("migration_check", target, False, "CHECK_BLOCKED", lines)
        finally:
            if connection is not None:
                self._close(connection)

    def apply(
        self,
        target: MigrationTarget,
        *,
        backup_confirmed: bool = False,
        restore_tested: bool = False,
        allow_disposable: bool = False,
    ) -> OperationResult:
        lines = [f"operation=migration_apply target={target.value} mode=mutating"]
        gate_error = self._apply_gate(
            target,
            backup_confirmed=backup_confirmed,
            restore_tested=restore_tested,
            allow_disposable=allow_disposable,
        )
        if gate_error:
            lines.append(f"blocked={gate_error}")
            return OperationResult("migration_apply", target, False, "ACKNOWLEDGEMENT_REQUIRED", lines)

        connection = None
        scripts_completed = 0
        try:
            self._validate_manifest()
            connection, configured_database = self._open(target)
            info = guard_target(connection, target, configured_database)
            if self.environment.get("APP_ENV", "development").strip().lower() != "production":
                if not info.connected_database.endswith("_test"):
                    raise MigrationSafetyError("non-production apply requires a database name ending in _test")
            connection.autocommit = True
            lock_timeout = self._lock_timeout()
            production = (
                self.environment.get("APP_ENV", "development").strip().lower()
                == "production"
            )
            safety_gate = (
                "operator_backup_restore_attested"
                if production
                else "explicit_disposable_test_mode"
            )
            lines.append(
                f"database={info.connected_database} server={info.server_version} "
                f"safety_gate={safety_gate}"
            )
            with migration_lock(connection, target, info.connected_database, lock_timeout):
                lines.append(f"lock=acquired timeout_seconds={lock_timeout}")
                observations, issues = self._inspect(connection, target, info.connected_database)
                if self._blocked(observations, issues):
                    lines.extend(self._observation_lines(observations))
                    lines.extend(f"blocked={issue}" for issue in issues)
                    return OperationResult("migration_apply", target, False, "APPLY_BLOCKED", lines)

                by_id = {item.migration.id: item for item in observations}
                pending = [
                    migration
                    for migration in forward_migrations(target)
                    if by_id[migration.id].status == MigrationStatus.PENDING
                ]
                if not pending:
                    for migration in forward_migrations(target):
                        for preflight_id in migration.preflight_ids:
                            preflight = migration_by_id(preflight_id)
                            if not preflight.safe_to_rerun:
                                continue
                            result = run_preflight(
                                connection,
                                preflight,
                                preflight.path(self.root).read_text(encoding="utf-8"),
                            )
                            lines.append(
                                f"migration={preflight.id} status="
                                f"{'passed' if result.passed else 'failed'} "
                                f"result_counts={','.join(map(str, result.result_set_counts)) or 'none'}"
                            )
                            if not result.passed:
                                return OperationResult(
                                    "migration_apply", target, False, "PREFLIGHT_FAILED", lines
                                )
                    verify_issues = verify_target_schema(
                        SchemaInspector(connection, info.connected_database), target
                    )
                    if verify_issues:
                        lines.extend(f"verification_failed={issue}" for issue in verify_issues)
                        return OperationResult(
                            "migration_apply", target, False, "APPLY_SUCCEEDED_VERIFY_FAILED", lines
                        )
                    lines.append("status=no_pending_migrations verification=passed")
                    return OperationResult("migration_apply", target, True, "NO_PENDING_MIGRATIONS", lines)

                applied_ids = {
                    item.migration.id
                    for item in observations
                    if item.status == MigrationStatus.APPLIED
                }
                ledger_ready = False
                for migration in pending:
                    unmet = [
                        dependency
                        for dependency in migration.dependencies
                        if migration_by_id(dependency).include_in_apply and dependency not in applied_ids
                    ]
                    if unmet:
                        raise MigrationSafetyError(
                            f"{migration.id} has unmet forward dependencies: {', '.join(unmet)}"
                        )
                    for preflight_id in migration.preflight_ids:
                        preflight = migration_by_id(preflight_id)
                        result = run_preflight(
                            connection,
                            preflight,
                            preflight.path(self.root).read_text(encoding="utf-8"),
                        )
                        lines.append(
                            f"migration={preflight.id} status={'passed' if result.passed else 'failed'} "
                            f"result_counts={','.join(map(str, result.result_set_counts)) or 'none'}"
                        )
                        if not result.passed:
                            return OperationResult(
                                "migration_apply", target, False, "PREFLIGHT_FAILED", lines
                            )

                    if not ledger_ready:
                        create_ledger(connection)
                        ledger_ready = True
                    started = monotonic()
                    execute_script(
                        connection,
                        migration,
                        migration.path(self.root).read_text(encoding="utf-8"),
                    )
                    scripts_completed += 1
                    assert migration.verification_key is not None
                    verification_issues = verify_artifact(
                        SchemaInspector(connection, info.connected_database),
                        migration.verification_key,
                    )
                    duration_ms = int((monotonic() - started) * 1000)
                    if verification_issues:
                        lines.append(
                            f"migration={migration.id} status=executed verification=failed "
                            f"checksum={migration.checksum_prefix} duration_ms={duration_ms}"
                        )
                        lines.extend(f"verification_failed={issue}" for issue in verification_issues)
                        lines.append(
                            "note=no automatic rollback; MariaDB DDL may have committed partial effects"
                        )
                        return OperationResult(
                            "migration_apply", target, False, "APPLY_SUCCEEDED_VERIFY_FAILED", lines
                        )
                    record_migration(connection, migration)
                    applied_ids.add(migration.id)
                    lines.append(
                        f"migration={migration.id} status=applied_and_verified "
                        f"checksum={migration.checksum_prefix} duration_ms={duration_ms}"
                    )

                final_issues = verify_target_schema(
                    SchemaInspector(connection, info.connected_database), target
                )
                if final_issues:
                    lines.extend(f"verification_failed={issue}" for issue in final_issues)
                    return OperationResult(
                        "migration_apply", target, False, "APPLY_SUCCEEDED_VERIFY_FAILED", lines
                    )
                lines.append(f"status=applied_and_verified migrations_applied={scripts_completed}")
                return OperationResult("migration_apply", target, True, "APPLIED_AND_VERIFIED", lines)
        except Exception as exc:
            lines.append(f"failed={self._safe_error(exc)}")
            outcome = "APPLY_FAILED_PARTIAL_POSSIBLE" if scripts_completed else "APPLY_FAILED"
            lines.append("note=MariaDB DDL is not transactionally atomic; no automatic rollback was attempted")
            return OperationResult("migration_apply", target, False, outcome, lines)
        finally:
            if connection is not None:
                self._close(connection)

    def verify(self, target: MigrationTarget) -> OperationResult:
        lines = [f"operation=migration_verify target={target.value} mode=read_only"]
        connection = None
        try:
            self._validate_manifest()
            connection, configured_database = self._open(target)
            info = guard_target(connection, target, configured_database)
            observations, state_issues = self._inspect(connection, target, info.connected_database)
            schema_issues = verify_target_schema(
                SchemaInspector(connection, info.connected_database), target
            )
            lines.append(f"database={info.connected_database} server={info.server_version}")
            lines.extend(self._observation_lines(observations))
            lines.extend(f"verification_failed={issue}" for issue in state_issues + schema_issues)
            success = not self._blocked(observations, state_issues) and not schema_issues and all(
                item.status == MigrationStatus.APPLIED for item in observations
            )
            lines.append(f"verification={'passed' if success else 'failed'}")
            return OperationResult(
                "migration_verify", target, success, "VERIFIED" if success else "VERIFY_FAILED", lines
            )
        except Exception as exc:
            lines.append(f"verification_failed={self._safe_error(exc)}")
            return OperationResult("migration_verify", target, False, "VERIFY_FAILED", lines)
        finally:
            if connection is not None:
                self._close(connection)

    def feature_readiness(self) -> tuple[FeatureStatus, ...]:
        app_statuses, app_error = self._read_statuses(MigrationTarget.APP)
        vector_statuses, vector_error = self._read_statuses(MigrationTarget.VECTOR)

        def applied(statuses: dict[str, MigrationStatus], *ids: str) -> bool:
            return bool(statuses) and all(statuses.get(item) == MigrationStatus.APPLIED for item in ids)

        durable = applied(app_statuses, "app-002", "app-004")
        duplicate = applied(app_statuses, "app-002", "app-004", "app-007")
        vector = applied(vector_statuses, "vector-001", "vector-002", "vector-003")
        embedding_reasons: list[str] = []
        if not vector:
            embedding_reasons.append(vector_error or "full vector migration chain is not verified")
        if (self.environment.get("EMBEDDING_PROVIDER") or "openai").strip().lower() != "openai":
            embedding_reasons.append("EMBEDDING_PROVIDER must be openai")
        if not (self.environment.get("OPENAI_API_KEY") or "").strip():
            embedding_reasons.append("OPENAI_API_KEY is required")
        if not (self.environment.get("EMBEDDING_MODEL") or "text-embedding-3-small").strip():
            embedding_reasons.append("EMBEDDING_MODEL is required")
        if not (self.environment.get("EMBEDDING_CHUNKER_VERSION") or "chunk-v1").strip():
            embedding_reasons.append("EMBEDDING_CHUNKER_VERSION is required")
        if self._positive_int("EMBEDDING_DIMENSIONS", 1536) != 1536:
            embedding_reasons.append("EMBEDDING_DIMENSIONS must be 1536")

        semantic_reasons: list[str] = []
        if not vector:
            semantic_reasons.append(vector_error or "full vector and semantic schema is not verified")
        if not 1 <= self._positive_int("SEMANTIC_LOOKBACK_HOURS", 72) <= 8760:
            semantic_reasons.append("SEMANTIC_LOOKBACK_HOURS must be between 1 and 8760")
        if not 1 <= self._positive_int("SEMANTIC_TOP_K", 10) <= 20:
            semantic_reasons.append("SEMANTIC_TOP_K must be between 1 and 20")
        if not (self.environment.get("SEMANTIC_EVIDENCE_VERSION") or "semantic-shadow-v1").strip():
            semantic_reasons.append("SEMANTIC_EVIDENCE_VERSION is required")

        return (
            FeatureStatus(
                "durable_processing", durable, () if durable else (app_error or "app-002/app-004 are not verified",)
            ),
            FeatureStatus(
                "durable_publishing", durable, () if durable else (app_error or "app-002/app-004 are not verified",)
            ),
            FeatureStatus(
                "duplicate_shadow", duplicate, () if duplicate else (app_error or "app-007 is not verified",)
            ),
            FeatureStatus("vector", vector, () if vector else (vector_error or "vector-001..003 are not verified",)),
            FeatureStatus("embedding", not embedding_reasons, tuple(embedding_reasons)),
            FeatureStatus("semantic_shadow", not semantic_reasons, tuple(semantic_reasons)),
        )

    def _read_statuses(
        self, target: MigrationTarget
    ) -> tuple[dict[str, MigrationStatus], str | None]:
        connection = None
        try:
            self._validate_manifest()
            connection, configured_database = self._open(target)
            info = guard_target(connection, target, configured_database)
            observations, issues = self._inspect(connection, target, info.connected_database)
            if issues:
                return {}, "; ".join(issues)
            return {item.migration.id: item.status for item in observations}, None
        except Exception as exc:
            return {}, self._safe_error(exc)
        finally:
            if connection is not None:
                self._close(connection)

    def _apply_gate(
        self,
        target: MigrationTarget,
        *,
        backup_confirmed: bool,
        restore_tested: bool,
        allow_disposable: bool,
    ) -> str | None:
        production = self.environment.get("APP_ENV", "development").strip().lower() == "production"
        if production:
            if not backup_confirmed:
                return "--backup-confirmed operator attestation is required; tooling did not verify a backup"
            if not restore_tested:
                return "--restore-tested operator attestation is required; tooling did not test a restore"
            return None
        if not allow_disposable:
            return "non-production mutation requires --allow-disposable"
        if self.environment.get("MIGRATION_TEST_MODE", "false").strip().lower() not in {
            "1", "true", "yes", "on"
        }:
            return "non-production mutation requires MIGRATION_TEST_MODE=true"
        return None

    def _lock_timeout(self) -> int:
        try:
            timeout = int(self.environment.get("MIGRATION_LOCK_TIMEOUT_SECONDS", "5"))
        except ValueError as exc:
            raise MigrationSafetyError("MIGRATION_LOCK_TIMEOUT_SECONDS must be an integer") from exc
        if not 1 <= timeout <= 60:
            raise MigrationSafetyError("MIGRATION_LOCK_TIMEOUT_SECONDS must be between 1 and 60")
        return timeout

    def _positive_int(self, name: str, default: int) -> int:
        try:
            return int(self.environment.get(name, str(default)))
        except ValueError:
            return 0

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, (ManifestError, MigrationSafetyError, MigrationExecutionError)):
            return str(exc)
        if isinstance(exc, mysql.connector.Error):
            errno = getattr(exc, "errno", None)
            return f"database operation failed errno={errno if errno is not None else 'unknown'}"
        return f"{type(exc).__name__}: operation failed; inspect restricted operator logs"


def render_result(result: OperationResult) -> str:
    return "\n".join((*result.lines, f"outcome={result.outcome}"))


def render_readiness(statuses: tuple[FeatureStatus, ...]) -> str:
    lines = ["operation=feature_readiness mode=read_only activation=none"]
    for status in statuses:
        detail = "; ".join(status.reasons) if status.reasons else "all prerequisites verified"
        lines.append(f"{status.name}={'ready' if status.ready else 'blocked'} reason={detail}")
    return "\n".join(lines)
