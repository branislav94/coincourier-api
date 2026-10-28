"""Serialize duplicate validation through the external publication boundary."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import Any, Callable, Iterator

import config
import mysql.connector
from operations.locking import JobLockError, advisory_job_lock
from operations.models import DatabaseTarget
from repositories.duplicate_assessments import DuplicateAssessmentRepository

from .enforcement import DuplicateDecision, assess_duplicate_enforcement


class PublicationDuplicateRejected(RuntimeError):
    def __init__(self, decision: DuplicateDecision) -> None:
        self.decision = decision
        super().__init__("publication_duplicate_rejected")


class PublicationDuplicateUnavailable(RuntimeError):
    def __init__(self, code: str, decision: DuplicateDecision | None = None) -> None:
        self.code = code
        self.decision = decision
        super().__init__(code)


class PublicationDuplicateGuard:
    """One application-DB mutex spans recheck, media, CREATE and ID persistence.

    The additional conflict group also protects callers bypassing the ordinary
    one-shot job lock. A global publication mutex covers identical content with
    different or missing event IDs as well as shared provider-event identities.
    No SQL transaction remains open across image or WordPress network work.
    """

    def __init__(
        self,
        raw_article_id: int | None,
        *,
        mode: str | None = None,
        repository: DuplicateAssessmentRepository | None = None,
        lock_factory: Callable[[], Any] | None = None,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.raw_article_id = raw_article_id
        self.mode = str(config.DUPLICATE_ENFORCEMENT_MODE if mode is None else mode).strip().lower()
        if self.mode not in {"off", "shadow", "enforce"}:
            raise ValueError("invalid duplicate enforcement mode")
        self.repository = repository
        self.lock_factory = lock_factory
        self.connection_factory = connection_factory
        self.decision: DuplicateDecision | None = None
        self._held = False
        self._connection = None
        self._lock_name = ""

    @property
    def enabled(self) -> bool:
        return self.mode == "enforce"

    def _lock(self):
        def connect(_target):
            self._connection = (self.connection_factory or (lambda: mysql.connector.connect(**config.DB_CONFIG)))()
            return self._connection

        return advisory_job_lock(
            "duplicate-publication", DatabaseTarget.APP,
            environment={"DB_NAME": str(config.DB_CONFIG.get("database") or "")},
            timeout_seconds=1,
            connection_factory=connect,
        )

    @contextmanager
    def hold(self) -> Iterator["PublicationDuplicateGuard"]:
        if not self.enabled:
            yield self
            return
        with ExitStack() as stack:
            try:
                lock = stack.enter_context((self.lock_factory or self._lock)())
            except (mysql.connector.Error, JobLockError):
                raise PublicationDuplicateUnavailable("duplicate_publication_lock_unavailable") from None
            if not lock.acquired:
                raise PublicationDuplicateUnavailable("duplicate_publication_lock_busy")
            self._held = True
            self._lock_name = lock.name
            try:
                yield self
            finally:
                self._held = False

    def check(self) -> None:
        if not self.enabled:
            return
        if not self._held:
            raise PublicationDuplicateUnavailable("duplicate_publication_lock_not_held")
        if self._connection is not None:
            cursor = None
            try:
                cursor = self._connection.cursor()
                cursor.execute("SELECT IS_USED_LOCK(%s), CONNECTION_ID()", (self._lock_name,))
                owner = cursor.fetchone()
                if not owner or owner[0] != owner[1]:
                    raise PublicationDuplicateUnavailable("duplicate_publication_lock_lost")
            except mysql.connector.Error:
                raise PublicationDuplicateUnavailable("duplicate_publication_lock_lost") from None
            finally:
                if cursor is not None:
                    try:
                        cursor.close()
                    except mysql.connector.Error:
                        raise PublicationDuplicateUnavailable("duplicate_publication_lock_lost") from None
        self.decision = assess_duplicate_enforcement(
            {"id": self.raw_article_id}, stage="pre_publish", mode="enforce",
            repository=self.repository,
        )
        if not self.decision.available:
            code = self.decision.error if self.decision.error == "prior_publication_unresolved" else "duplicate_assessment_unavailable"
            raise PublicationDuplicateUnavailable(code, self.decision)
        if self.decision.reject:
            raise PublicationDuplicateRejected(self.decision)
