"""Typed contracts for bounded one-shot operational jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping


class JobStatus(str, Enum):
    SUCCESS = "SUCCESS"
    NO_WORK = "NO_WORK"
    SKIPPED_ALREADY_RUNNING = "SKIPPED_ALREADY_RUNNING"
    DISABLED = "DISABLED"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    FAILED = "FAILED"


class ScheduleClass(str, Enum):
    RECURRING = "recurring"
    MANUAL = "manual"
    ROLLOUT_DECISION = "rollout_decision"


class DatabaseTarget(str, Enum):
    APP = "app"
    VECTOR = "vector"


@dataclass(frozen=True)
class JobSpec:
    name: str
    description: str
    command: str
    executor: str
    schedule_class: ScheduleClass
    config_profile: str
    conflict_group: str | None
    lock_target: DatabaseTarget | None
    lock_strategy: str
    parallel_safe: bool
    required_features: tuple[str, ...] = ()
    bounds: tuple[str, ...] = ()
    cadence: str | None = None


@dataclass(frozen=True)
class WorkResult:
    status: JobStatus
    stage: str
    counts: Mapping[str, int] = field(default_factory=dict)
    reason_type: str | None = None
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class JobResult:
    job_name: str
    status: JobStatus
    stage: str
    started_at: datetime
    finished_at: datetime
    counts: Mapping[str, int] = field(default_factory=dict)
    reason_type: str | None = None
    details: tuple[str, ...] = ()
    lock_name: str | None = None

    @property
    def duration_ms(self) -> int:
        return max(0, int((self.finished_at - self.started_at).total_seconds() * 1000))

    @property
    def exit_code(self) -> int:
        if self.status in {JobStatus.CONFIGURATION_ERROR, JobStatus.FAILED}:
            return 1
        return 0
