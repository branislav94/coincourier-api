"""Typed contracts shared by migration planning, execution, and verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class MigrationTarget(str, Enum):
    APP = "app"
    VECTOR = "vector"


class ScriptKind(str, Enum):
    PREFLIGHT = "preflight"
    FORWARD = "forward"
    FORWARD_BACKFILL = "forward_backfill"
    RECOVERY = "recovery"
    FRESH_START_READ_ONLY = "fresh_start_read_only"
    FRESH_START_MUTATION = "fresh_start_mutation"
    TEST_FIXTURE = "test_fixture"


class ArtifactState(str, Enum):
    NONE = "none"
    COMPLETE = "complete"
    PARTIAL = "partial"


class MigrationStatus(str, Enum):
    PENDING = "pending"
    APPLIED = "applied"
    DRIFT = "drift"
    AMBIGUOUS = "ambiguous"
    UNTRACKED_APPLIED = "untracked_applied"


@dataclass(frozen=True)
class MigrationSpec:
    id: str
    name: str
    target: MigrationTarget
    relative_path: str
    kind: ScriptKind
    order: int
    checksum: str
    safe_to_rerun: bool
    mutates_data: bool
    contains_ddl: bool
    include_in_apply: bool
    dependencies: tuple[str, ...] = ()
    preflight_ids: tuple[str, ...] = ()
    verification_key: str | None = None

    @property
    def checksum_prefix(self) -> str:
        return self.checksum[:12]

    def path(self, repository_root: Path) -> Path:
        return repository_root / self.relative_path


@dataclass(frozen=True)
class TargetInfo:
    target: MigrationTarget
    configured_database: str
    connected_database: str
    server_version: str


@dataclass(frozen=True)
class LedgerRecord:
    migration_id: str
    checksum: str
    tool_version: str


@dataclass(frozen=True)
class MigrationObservation:
    migration: MigrationSpec
    status: MigrationStatus
    artifact_state: ArtifactState
    detail: str = ""

    @property
    def blocked(self) -> bool:
        return self.status in {
            MigrationStatus.DRIFT,
            MigrationStatus.AMBIGUOUS,
            MigrationStatus.UNTRACKED_APPLIED,
        }


@dataclass
class OperationResult:
    operation: str
    target: MigrationTarget | None
    success: bool
    outcome: str
    lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FeatureStatus:
    name: str
    ready: bool
    reasons: tuple[str, ...] = ()
