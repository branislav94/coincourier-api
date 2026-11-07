"""Offline corpus adapter: real duplicate decisions, explicit fake persistence only.

This is test support, not a pipeline command or a historical replay service.
Semantic references are kept separate from the policy's observed classification.
"""

from __future__ import annotations

from collections import Counter
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCHEMA_VERSION = "g1a-duplicate-regression-v1"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "g1a_duplicate_regression.json"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
POPULATIONS = {
    "synthetic_contract", "reconstructed_baseline", "operator_FINAL_minimal_analogue",
}
RELATIONS = {
    "exact_duplicate", "same_event_duplicate", "material_update", "related_event",
    "broad_topic_overlap", "unrelated", "insufficient_evidence",
}
REVIEW_STATUSES = {"root_second_review_approved", "existing_baseline_reconstruction"}
SOURCE_PATHS = (
    "GetNewsAPI/duplicate_detection/policy.py",
    "GetNewsAPI/duplicate_detection/enforcement.py",
    "GetNewsAPI/duplicate_detection/event_matching.py",
    "GetNewsAPI/duplicate_detection/identities.py",
)


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def input_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_corpus(path: str | Path = FIXTURE_PATH) -> dict:
    """Reject changed/unreviewed inputs and unsupported historical-gold claims."""
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported G1A corpus schema")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 64:
        raise ValueError("corpus requires 1-64 bounded cases")
    identities = set()
    for case in cases:
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in identities:
            raise ValueError("case identities must be nonempty and unique")
        identities.add(case_id)
        if case.get("population") not in POPULATIONS:
            raise ValueError("unsupported corpus population")
        reference = case.get("semantic_reference", {})
        if reference.get("relation") not in RELATIONS:
            raise ValueError("unsupported semantic reference")
        if reference.get("review_status") not in REVIEW_STATUSES:
            raise ValueError("case still awaits pre-engine second review")
        if case.get("historical_accuracy_eligible") is not False:
            raise ValueError("this foundation has no independent historical raw gold")
        if type(case.get("expected_contract_confirmation")) is not bool:
            raise ValueError("pre-engine contract confirmation review is required")
        inputs = case.get("inputs", {})
        for side in ("prior", "current"):
            article = inputs.get(side)
            if not isinstance(article, dict) or type(article.get("id")) is not int or article["id"] <= 0:
                raise ValueError("each case needs distinct positive integer source identities")
            if len(str(article.get("title") or "")) > 1000 or len(str(article.get("full_text") or "")) > 16384:
                raise ValueError("case input exceeds the offline text bound")
            if case.get("input_sha256", {}).get(side) != input_sha256(article):
                raise ValueError("frozen input fingerprint mismatch")
        if inputs["current"]["id"] == inputs["prior"]["id"]:
            raise ValueError("pair cannot compare a source with itself")
    for item in payload.get("quarantine", []):
        if item.get("inputs") is not None or item.get("historical_accuracy_eligible") is not False:
            raise ValueError("quarantined observations cannot become runnable gold")
    settings = payload.get("settings", {})
    if settings.get("candidate_limit") != 200 or settings.get("daily_cap_enabled") is not False:
        raise ValueError("corpus cannot change candidate or daily-cap controls")
    if not 1 <= settings.get("duplicate_lookback_hours", 0) <= 8760:
        raise ValueError("invalid explicit duplicate lookback")
    if not isinstance(settings.get("duplicate_policy_version"), str) or not settings["duplicate_policy_version"]:
        raise ValueError("explicit policy version required")
    contexts = payload.get("candidate_contexts", {})
    scenarios = payload.get("scenarios", [])
    scenario_ids = set()
    if not 1 <= len(scenarios) <= 32:
        raise ValueError("corpus requires 1-32 bounded stage scenarios")
    for scenario in scenarios:
        scenario_id = scenario.get("scenario_id")
        if scenario_id in scenario_ids or not isinstance(scenario_id, str) or not scenario_id:
            raise ValueError("scenario identities must be unique")
        scenario_ids.add(scenario_id)
        if scenario.get("stage") not in {"raw", "pre_publish"} or scenario.get("mode") not in {"off", "shadow", "enforce"}:
            raise ValueError("unsupported explicit stage or mode")
        if scenario.get("candidate_context") not in contexts or scenario.get("audit_outcome") not in {"persist", "error", "incomplete"}:
            raise ValueError("unsupported fake-repository context")
        selected = scenario.get("cases")
        if selected != "all" and (not isinstance(selected, list) or not selected or not set(selected) <= identities):
            raise ValueError("scenario references unknown cases")
    return payload


class MemoryAssessmentRepository:
    """Explicit source reload/candidate/audit interface; no database connection."""

    def __init__(self, current: dict, candidates: list[dict], audit_outcome: str = "persist"):
        self.current = deepcopy(current)
        self.candidates = deepcopy(candidates)
        self.audit_outcome = audit_outcome
        self.loads = []
        self.saved = []

    def load_source_article(self, raw_id):
        self.loads.append(("source", raw_id))
        return deepcopy(self.current) if raw_id == self.current["id"] else None

    def load_enforcement_candidates(self, raw_id, **kwargs):
        self.loads.append(("candidates", raw_id, deepcopy(kwargs)))
        return deepcopy(self.candidates)

    def save_assessments(self, raw_id, assessments, *, audit_context):
        pairs = list(assessments)
        if self.audit_outcome == "error":
            raise RuntimeError("fixture audit unavailable")
        self.saved.append((raw_id, pairs, deepcopy(audit_context)))
        return max(0, len(pairs) - 1) if self.audit_outcome == "incomplete" else len(pairs)


@contextmanager
def forbid_external_io():
    """Any accidental provider, database or process work makes evaluation fail."""
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("forbidden_external_io")
        raise AssertionError("offline corpus attempted external I/O")

    with ExitStack() as stack:
        for target in (
            "socket.create_connection", "socket.socket.connect", "mysql.connector.connect",
            "sqlite3.connect", "requests.sessions.Session.request", "urllib.request.urlopen",
            "subprocess.Popen",
        ):
            stack.enter_context(patch(target, side_effect=forbidden))
        yield attempts
    if attempts:
        raise AssertionError("offline corpus attempted external I/O")


def _decision_row(case: dict, scenario: dict, corpus: dict) -> dict:
    from duplicate_detection.enforcement import assess_duplicate_enforcement
    from duplicate_detection.publication import (
        PublicationDuplicateGuard, PublicationDuplicateRejected, PublicationDuplicateUnavailable,
    )

    current = deepcopy(case["inputs"]["current"])
    candidate = deepcopy(case["inputs"]["prior"])
    candidate.update(deepcopy(corpus["candidate_contexts"][scenario["candidate_context"]]))
    repository = MemoryAssessmentRepository(current, [candidate], scenario["audit_outcome"])
    locks = []

    @contextmanager
    def fake_lock():
        locks.append("fake_publication_mutex")
        yield SimpleNamespace(acquired=True, name="fixture-only-publication-mutex")

    if scenario["stage"] == "pre_publish":
        guard = PublicationDuplicateGuard(current["id"], mode=scenario["mode"], repository=repository, lock_factory=fake_lock)
        try:
            with guard.hold():
                guard.check()
            decision = guard.decision
            action = "not_evaluated" if scenario["mode"] == "off" else (
                decision.audit.get("decision", "would_continue") if scenario["mode"] == "shadow" else "continue"
            )
        except PublicationDuplicateRejected as error:
            decision, action = error.decision, "reject_duplicate"
        except PublicationDuplicateUnavailable as error:
            decision = error.decision or guard.decision
            action = "defer_manual_review" if error.code == "prior_publication_unresolved" else "defer_assessment_unavailable"
    else:
        decision = assess_duplicate_enforcement(current, stage="raw", mode=scenario["mode"], repository=repository)
        action = "not_evaluated" if scenario["mode"] == "off" else (
            decision.audit.get("decision", "would_continue") if scenario["mode"] == "shadow" else
            "continue_unavailable" if not decision.available else "reject_duplicate" if decision.reject else "continue"
        )
    audits = decision.audit.get("candidate_audits", {}) if decision else {}
    pair_audit = audits.get(candidate["id"])
    disposition_intent = {
        "reject_duplicate": "request_raw_duplicate" if scenario["stage"] == "raw" else "request_rich_duplicate",
        "defer_manual_review": "request_manual_reconciliation",
        "defer_assessment_unavailable": "request_retry_defer",
    }.get(action, "none")
    return {
        "case_id": case["case_id"], "population": case["population"],
        "scenario_id": scenario["scenario_id"], "stage": scenario["stage"], "mode": scenario["mode"],
        "candidate_context": scenario["candidate_context"], "audit_outcome": scenario["audit_outcome"],
        "repository_source_reloads": sum(item[0] == "source" for item in repository.loads),
        "assessment_evidence_observed": pair_audit is not None,
        "eligible_observed": None if scenario["mode"] == "off" or (decision and not decision.available and not pair_audit) else pair_audit is not None,
        "classification": pair_audit["classification"] if pair_audit else None,
        "confirmed_duplicate": pair_audit["confirmed_duplicate"] if pair_audit else None,
        "pair_decision": pair_audit["decision"] if pair_audit else None,
        "article_decision": action, "available": decision.available if decision else None,
        "reject": decision.reject if decision else False,
        "canonical_raw_id": decision.canonical_raw_id if decision else None,
        "canonical_rich_id": decision.canonical_rich_id if decision else None,
        "audit_persisted": any(pairs for _, pairs, _ in repository.saved) and scenario["audit_outcome"] == "persist",
        "audit_attempts": len(repository.saved),
        "fake_publication_lock_acquisitions": len(locks),
        "counters": dict(decision.counters) if decision else {},
        "disposition": {"intent": disposition_intent, "executed": False,
                        "ownership_verified": False, "database_state_changes": 0,
                        "verification_scope": "actual_decision_and_guard_only"},
    }


def evaluate_corpus(corpus: dict) -> dict:
    """Return deterministic observations, gaps and exclusions without quality scores."""
    from .environment_isolation import SAFE_TEST_ENVIRONMENT

    with patch.dict(os.environ, SAFE_TEST_ENVIRONMENT), patch("dotenv.load_dotenv", return_value=False), forbid_external_io() as io_attempts:
        import config
        from duplicate_detection.enforcement import _confirmed
        from duplicate_detection.policy import assess_relationship

        settings = corpus["settings"]
        case_rows, scenario_rows = [], []
        with patch.multiple(config, DUPLICATE_LOOKBACK_HOURS=settings["duplicate_lookback_hours"],
                            DUPLICATE_POLICY_VERSION=settings["duplicate_policy_version"]):
            for case in sorted(corpus["cases"], key=lambda item: item["case_id"]):
                assessment = assess_relationship(case["inputs"]["current"], case["inputs"]["prior"],
                    lookback_hours=settings["duplicate_lookback_hours"], policy_version=settings["duplicate_policy_version"])
                relation = case["semantic_reference"]["relation"]
                expected_native = corpus["native_relation_mapping"].get(relation)
                duplicate_truth = case["semantic_reference"].get("duplicate_truth")
                if duplicate_truth is None and relation != "insufficient_evidence":
                    duplicate_truth = relation in {"exact_duplicate", "same_event_duplicate"}
                confirmed = _confirmed(assessment)
                case_rows.append({
                    "case_id": case["case_id"], "population": case["population"],
                    "semantic_reference": relation, "duplicate_truth": duplicate_truth,
                    "native_reference": expected_native, "classification": assessment.assessment_type.value,
                    "semantic_classification_alignment_gap": None if expected_native is None else assessment.assessment_type.value != expected_native,
                    "confirmed_duplicate": confirmed,
                    "expected_contract_confirmation": case["expected_contract_confirmation"],
                    "contract_confirmation_mismatch": confirmed != case["expected_contract_confirmation"],
                    "semantic_confirmation_alignment_gap": None if duplicate_truth is None else confirmed != duplicate_truth,
                    "new_fact_codes": list(assessment.new_fact_codes), "uncertainty": list(assessment.evidence_uncertainty),
                    "contradictions": list(assessment.contradiction_flags),
                    "event_identity_source": assessment.event_identity_source,
                    "confirmation_anchors": list(assessment.confirmation_anchors),
                    "historical_accuracy_eligible": False,
                    "input_sha256": deepcopy(case["input_sha256"]),
                })
                for scenario in sorted(corpus["scenarios"], key=lambda item: item["scenario_id"]):
                    if scenario["cases"] == "all" or case["case_id"] in scenario["cases"]:
                        scenario_rows.append(_decision_row(case, scenario, corpus))
    counts = Counter(case["population"] for case in corpus["cases"])
    return {
        "schema_version": SCHEMA_VERSION, "purpose": "offline_contract_foundation",
        "settings": deepcopy(corpus["settings"]),
        "input_manifest_sha256": input_sha256(corpus),
        "runtime_source_hashes": {path: hashlib.sha256((REPOSITORY_ROOT / path).read_bytes()).hexdigest() for path in SOURCE_PATHS},
        "populations": dict(sorted(counts.items())),
        "historical_accuracy": {"status": "unavailable", "eligible_pairs": 0,
                                "reason": "no immutable historical raw inputs and independent raw-pair adjudication"},
        "cases": case_rows, "scenarios": scenario_rows,
        "quarantine": deepcopy(corpus["quarantine"]),
        "external_io_attempts": len(io_attempts), "database_state_changes": 0,
        "limitations": ["semantic_contract_gaps_are_not_production_error_rates",
                        "minimal_FINAL_analogues_are_not_pipeline_raw_replays",
                        "disposition_intents_do_not_verify_claim_ownership_or_SQL_transactions"],
    }


def render_report(report: dict) -> str:
    return json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n"
