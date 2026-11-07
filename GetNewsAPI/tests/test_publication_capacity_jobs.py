"""Offline operational status propagation for publication-capacity deferrals."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from operations import jobs
from operations.jobs import JobRequest
from operations.models import JobStatus, WorkResult


class PublicationCapacityJobTests(unittest.TestCase):
    def publish_result(self, payload):
        module = ModuleType("publish_to_wp")
        module.publish_news_to_wp = Mock(return_value=payload)
        with patch.dict(sys.modules, {"publish_to_wp": module}):
            result = jobs._execute_publish(JobRequest())
        module.publish_news_to_wp.assert_called_once_with()
        return result

    def test_capacity_unavailable_is_configuration_error_with_counters(self):
        payload = {
            "status": "capacity_unavailable", "attempted": 0,
            "succeeded": 0, "failed": 0, "capacity_unavailable": 1,
        }
        result = self.publish_result(payload)
        self.assertIs(result.status, JobStatus.CONFIGURATION_ERROR)
        self.assertEqual(result.reason_type, "PublicationCapacityUnavailable")
        self.assertEqual(result.stage, "publish")
        self.assertEqual(result.counts["capacity_unavailable"], 1)
        self.assertNotIn("status", result.counts)

    def test_full_capacity_without_success_is_no_work(self):
        result = self.publish_result({
            "status": "quota_deferred", "attempted": 0, "succeeded": 0,
            "failed": 0, "quota_deferred": 0, "quota_exhausted": 1,
        })
        self.assertIs(result.status, JobStatus.NO_WORK)
        self.assertEqual(result.reason_type, "PublicationQuotaDeferred")
        self.assertEqual(result.counts["quota_exhausted"], 1)
        self.assertEqual(result.counts["failed"], 0)

    def test_deferral_after_reconciliation_is_success_with_deferral_counters(self):
        result = self.publish_result({
            "status": "quota_deferred", "attempted": 2, "succeeded": 1,
            "failed": 0, "quota_deferred": 1, "quota_exhausted": 1,
        })
        self.assertIs(result.status, JobStatus.SUCCESS)
        self.assertEqual(result.reason_type, "PublicationQuotaDeferred")
        self.assertEqual(result.counts["succeeded"], 1)
        self.assertEqual(result.counts["quota_deferred"], 1)

    def test_existing_publication_failure_remains_failed(self):
        result = self.publish_result({
            "status": "failed", "attempted": 2, "succeeded": 1, "failed": 1,
        })
        self.assertIs(result.status, JobStatus.FAILED)
        self.assertEqual(result.reason_type, "PublicationFailures")

    def test_process_quota_deferral_is_no_work_with_reason(self):
        module = ModuleType("gpt_processor")
        module.process_news_with_gpt = Mock(return_value={
            "attempted": 0, "succeeded": 0, "failed": 0, "quota_deferred": 1,
        })
        with patch.dict(sys.modules, {"gpt_processor": module}):
            result = jobs._execute_process(JobRequest())
        module.process_news_with_gpt.assert_called_once_with()
        self.assertIs(result.status, JobStatus.NO_WORK)
        self.assertEqual(result.reason_type, "PublicationQuotaDeferred")
        self.assertEqual(result.counts["quota_deferred"], 1)

    def test_pipeline_preserves_capacity_configuration_error(self):
        with (
            patch.object(jobs, "_execute_process", return_value=WorkResult(
                JobStatus.NO_WORK, "process", {"attempted": 0, "quota_deferred": 1},
            )),
            patch.object(jobs, "_execute_publish", return_value=WorkResult(
                JobStatus.CONFIGURATION_ERROR, "publish", {"capacity_unavailable": 1},
                "PublicationCapacityUnavailable",
            )),
        ):
            result = jobs._execute_pipeline(JobRequest())
        self.assertIs(result.status, JobStatus.CONFIGURATION_ERROR)
        self.assertEqual(result.stage, "publish")
        self.assertEqual(result.reason_type, "PublicationCapacityUnavailable")
        self.assertEqual(result.counts["publish_capacity_unavailable"], 1)
        self.assertEqual(result.counts["process_quota_deferred"], 1)

    def test_pipeline_full_capacity_is_no_work_and_preserves_stage_counters(self):
        with (
            patch.object(jobs, "_execute_process", return_value=WorkResult(
                JobStatus.NO_WORK, "process", {"attempted": 0, "quota_deferred": 1},
                "PublicationQuotaDeferred",
            )),
            patch.object(jobs, "_execute_publish", return_value=WorkResult(
                JobStatus.NO_WORK, "publish", {"attempted": 0, "quota_exhausted": 1},
                "PublicationQuotaDeferred",
            )),
        ):
            result = jobs._execute_pipeline(JobRequest())
        self.assertIs(result.status, JobStatus.NO_WORK)
        self.assertEqual(result.counts["process_quota_deferred"], 1)
        self.assertEqual(result.counts["publish_quota_exhausted"], 1)


if __name__ == "__main__":
    unittest.main()
