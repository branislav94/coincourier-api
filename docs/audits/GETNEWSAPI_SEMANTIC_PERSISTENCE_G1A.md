# G1A semantic-shadow persistence diagnostics

This change repairs the local persistence boundary, not an established production
root cause. A prior `DatabaseError / persistence_failed / persisted=false` log did
not identify the failing database operation or establish whether a commit had
already happened. No production database, provider or WordPress call is part of
this implementation.

`SemanticAssessmentRepository.save_assessment()` now protects connection and
cursor acquisition, attempts each cleanup independently and preserves the primary
operation/error codes when rollback or cleanup also fails. A successful commit
remains successful if cursor/connection cleanup subsequently fails; that secondary
failure emits a controlled warning with `transaction_outcome=committed`. A commit
acknowledgement error remains `commit_unknown`, even if rollback returns normally.
The repository performs no automatic retry of an ambiguous commit.

Diagnostics contain only an allowlisted operation, fixed exception class,
bounded primitive connector errno, validated five-character SQLSTATE, transaction
outcome and boolean cleanup flags. Original exception messages/args, SQL, bound
values, credentials/endpoints, source URLs/titles, evidence JSON, bodies and vectors
are not copied into diagnostics or logs. Wrapped errors use a fixed message and
suppress the original exception's displayed context.

The semantic shadow result remains advisory and fail-open. Persistence failures
retain already-validated source candidates and `decision=continue`; no semantic
distance threshold, duplicate authority, provider request, processing disposition
or publishing gate is added. Candidate source records still require the existing
independent deterministic policy before any decision.

The SQL, migration 003 and provisional identity policy are unchanged. In
particular, an older null-document observation saved after a resolved observation
can still create a provisional row; a later resolved save merges it. Assigning a
stale observation to multiple document versions needs a separate explicit policy.
The native rollback test uses that existing state to verify atomic merging rather
than silently changing its meaning.

Unit regressions cover cursor acquisition, rollback/independent close failures,
successful-commit cleanup, unknown commit acknowledgement, serialization,
diagnostic canaries and fail-open candidate preservation. New opt-in tests in
`tests/integration/test_semantic_persistence_mariadb.py` reuse the existing exact
loopback/disposable test-schema guard. They exercise actual MariaDB 11.8 status
upserts, JSON/result/bounds CHECK rejection, foreign-key rollback and CHECK failure
after a merge DELETE, followed by the unchanged successful merge. They create no
application or WordPress data and make no provider calls.

Validation results belong to the coordinating agent's actual isolated test run;
these tests were not executed by the implementation sub-agent. No production
cause, production compatibility or deployed outcome is claimed from source alone.
