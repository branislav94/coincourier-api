"""Explicit migration inventory and immutable checksum contract."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .models import MigrationSpec, MigrationTarget, ScriptKind


class ManifestError(RuntimeError):
    pass


def repository_root() -> Path:
    """Resolve resources in both the checkout and the immutable /app image."""

    package_parent = Path(__file__).resolve().parents[1]
    if (package_parent / "maintenance").is_dir():
        return package_parent
    return package_parent.parent


MIGRATIONS: tuple[MigrationSpec, ...] = (
    MigrationSpec(
        id="app-001",
        name="Phase 2 identity preflight",
        target=MigrationTarget.APP,
        relative_path="maintenance/migrations/001_phase2_identity_preflight.sql",
        kind=ScriptKind.PREFLIGHT,
        order=10,
        checksum="b95bd495c69260d9fe5e0a5a103fe4e3a665da0d12a566de02a4b8c5e550c236",
        safe_to_rerun=True,
        mutates_data=False,
        contains_ddl=False,
        include_in_apply=False,
    ),
    MigrationSpec(
        id="app-002",
        name="Phase 2 durable state and backfill",
        target=MigrationTarget.APP,
        relative_path="maintenance/migrations/002_phase2_durable_state.sql",
        kind=ScriptKind.FORWARD_BACKFILL,
        order=20,
        checksum="d3522cafe1ddc8d36e3fee23fdacd4388007cf8b7147c912ccc883bb7976c6bc",
        safe_to_rerun=True,
        mutates_data=True,
        contains_ddl=True,
        include_in_apply=True,
        dependencies=("app-001",),
        preflight_ids=("app-001",),
        verification_key="app_phase2_state",
    ),
    MigrationSpec(
        id="app-003",
        name="Phase 2 uniqueness preflight",
        target=MigrationTarget.APP,
        relative_path="maintenance/migrations/003_phase2_uniqueness_preflight.sql",
        kind=ScriptKind.PREFLIGHT,
        order=30,
        checksum="7a9f0fb27927ced8cbdbdc460efdfaae28b41871c25ed2537b7bb4735d71fbe9",
        safe_to_rerun=True,
        mutates_data=False,
        contains_ddl=False,
        include_in_apply=False,
        dependencies=("app-002",),
    ),
    MigrationSpec(
        id="app-004",
        name="Phase 2 durable state indexes",
        target=MigrationTarget.APP,
        relative_path="maintenance/migrations/004_phase2_indexes.sql",
        kind=ScriptKind.FORWARD,
        order=40,
        checksum="7c039ae8af51cef22ca1f64f96c07d0d01a2af4cf443bd7e9a625df3ad2727f9",
        safe_to_rerun=True,
        mutates_data=False,
        contains_ddl=True,
        include_in_apply=True,
        dependencies=("app-002", "app-003"),
        preflight_ids=("app-003",),
        verification_key="app_phase2_indexes",
    ),
    MigrationSpec(
        id="app-005",
        name="Phase 2 claimed-state recovery utility",
        target=MigrationTarget.APP,
        relative_path="maintenance/migrations/005_phase2_rollback_state.sql",
        kind=ScriptKind.RECOVERY,
        order=50,
        checksum="d3f97ec347c751e7e96e120f6cb703c4c5be6aa309bab7ab5d53cddacc7c5887",
        safe_to_rerun=True,
        mutates_data=True,
        contains_ddl=False,
        include_in_apply=False,
        dependencies=("app-002",),
    ),
    MigrationSpec(
        id="app-006",
        name="Phase 5 duplicate table ownership preflight",
        target=MigrationTarget.APP,
        relative_path="maintenance/migrations/006_phase5_duplicate_preflight.sql",
        kind=ScriptKind.PREFLIGHT,
        order=60,
        checksum="d483752355e92161264d61d0df71d12496eeeda5cfe9404a492f5b0d037cf4f4",
        safe_to_rerun=False,
        mutates_data=False,
        contains_ddl=False,
        include_in_apply=False,
        dependencies=("app-004",),
    ),
    MigrationSpec(
        id="app-007",
        name="Phase 5 duplicate shadow schema",
        target=MigrationTarget.APP,
        relative_path="maintenance/migrations/007_phase5_duplicate_shadow.sql",
        kind=ScriptKind.FORWARD,
        order=70,
        checksum="2c0cbb21a4fa76e8474c1b75df920fcfa4fb76c7f68520a1df7d2a333756bad4",
        safe_to_rerun=True,
        mutates_data=False,
        contains_ddl=True,
        include_in_apply=True,
        dependencies=("app-004", "app-006"),
        preflight_ids=("app-006",),
        verification_key="app_phase5_duplicate",
    ),
    MigrationSpec(
        id="app-fresh-dry-run",
        name="Fresh-start inspection utility",
        target=MigrationTarget.APP,
        relative_path="maintenance/sql/fresh_start_dry_run.sql",
        kind=ScriptKind.FRESH_START_READ_ONLY,
        order=900,
        checksum="e1e74428aa2f2149924c4ca244f36ca5bad5ffcb18d02fa602a9d5af3ae0da4e",
        safe_to_rerun=True,
        mutates_data=False,
        contains_ddl=False,
        include_in_apply=False,
    ),
    MigrationSpec(
        id="app-fresh-apply",
        name="Fresh-start cleanup utility",
        target=MigrationTarget.APP,
        relative_path="maintenance/sql/fresh_start_apply.sql",
        kind=ScriptKind.FRESH_START_MUTATION,
        order=910,
        checksum="ad9bad76b630e527e0a506331d90112eedb9c3c302ac873c6d55aa49dec08682",
        safe_to_rerun=False,
        mutates_data=True,
        contains_ddl=False,
        include_in_apply=False,
    ),
    MigrationSpec(
        id="app-test-baseline",
        name="Disposable pre-Phase-2 integration fixture",
        target=MigrationTarget.APP,
        relative_path="maintenance/testing/mariadb_phase2_baseline.sql",
        kind=ScriptKind.TEST_FIXTURE,
        order=920,
        checksum="64b3a456087a83c364f06f8cb8f5003165af077f70bd201f4a9b7897979677a4",
        safe_to_rerun=True,
        mutates_data=True,
        contains_ddl=True,
        include_in_apply=False,
    ),
    MigrationSpec(
        id="vector-001",
        name="Vector documents, chunks, and jobs schema",
        target=MigrationTarget.VECTOR,
        relative_path="maintenance/vector_migrations/001_vector_schema.sql",
        kind=ScriptKind.FORWARD,
        order=10,
        checksum="8cc09d51728c822c4e5b00f6a510c6ed35f3263af39364d6362f1e4fcbc23ecf",
        safe_to_rerun=True,
        mutates_data=False,
        contains_ddl=True,
        include_in_apply=True,
        verification_key="vector_core",
    ),
    MigrationSpec(
        id="vector-002",
        name="Native cosine vector index",
        target=MigrationTarget.VECTOR,
        relative_path="maintenance/vector_migrations/002_vector_indexes.sql",
        kind=ScriptKind.FORWARD,
        order=20,
        checksum="61bfbc25ca6aa1913ad93591c075683afad29e26e7fc06931de08f1d6990dbda",
        safe_to_rerun=True,
        mutates_data=False,
        contains_ddl=True,
        include_in_apply=True,
        dependencies=("vector-001",),
        verification_key="vector_index",
    ),
    MigrationSpec(
        id="vector-003",
        name="Semantic shadow assessment schema",
        target=MigrationTarget.VECTOR,
        relative_path="maintenance/vector_migrations/003_semantic_shadow_assessments.sql",
        kind=ScriptKind.FORWARD,
        order=30,
        checksum="ff76f9e7e466bad92b85996bf6f4b4381a41c40ac8c75ec430873adc1509b3ab",
        safe_to_rerun=True,
        mutates_data=False,
        contains_ddl=True,
        include_in_apply=True,
        dependencies=("vector-002",),
        verification_key="vector_semantic",
    ),
)


def migration_by_id(migration_id: str) -> MigrationSpec:
    for migration in MIGRATIONS:
        if migration.id == migration_id:
            return migration
    raise ManifestError(f"unknown migration dependency: {migration_id}")


def target_inventory(target: MigrationTarget) -> tuple[MigrationSpec, ...]:
    return tuple(sorted((m for m in MIGRATIONS if m.target == target), key=lambda m: m.order))


def forward_migrations(target: MigrationTarget) -> tuple[MigrationSpec, ...]:
    return tuple(m for m in target_inventory(target) if m.include_in_apply)


def execution_sequence(target: MigrationTarget) -> tuple[MigrationSpec, ...]:
    """Return preflight/forward order without recovery or fresh-start utilities."""

    included: dict[str, MigrationSpec] = {}
    for migration in forward_migrations(target):
        for preflight_id in migration.preflight_ids:
            included[preflight_id] = migration_by_id(preflight_id)
        included[migration.id] = migration
    return tuple(sorted(included.values(), key=lambda m: m.order))


def compute_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_manifest(root: Path | None = None, *, require_test_resources: bool = True) -> None:
    root = root or repository_root()
    ids: set[str] = set()
    for migration in MIGRATIONS:
        if migration.id in ids:
            raise ManifestError(f"duplicate migration id: {migration.id}")
        ids.add(migration.id)
        path = migration.path(root)
        if migration.kind == ScriptKind.TEST_FIXTURE and not require_test_resources:
            continue
        if not path.is_file():
            raise ManifestError(f"migration resource missing: {migration.relative_path}")
        actual = compute_checksum(path)
        if actual != migration.checksum:
            raise ManifestError(
                f"migration checksum drift: {migration.id} expected "
                f"{migration.checksum_prefix}, found {actual[:12]}"
            )
        for dependency in migration.dependencies + migration.preflight_ids:
            dependency_spec = migration_by_id(dependency)
            if dependency_spec.target != migration.target:
                raise ManifestError(f"cross-target dependency rejected: {migration.id}")
            if dependency_spec.order >= migration.order:
                raise ManifestError(f"non-forward dependency rejected: {migration.id}")
