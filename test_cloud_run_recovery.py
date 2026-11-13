"""Tests for read-only ambiguous Cloud Run launch inspection."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from shop_cloud_run_recovery import execution_matches, inspect_unknown_launch


class FakeWorkflow:
    def __init__(self, state="review", outcome_unknown=True, outbox_state="outcome_unknown"):
        self.execution = {
            "execution_id": "exec_12345678", "order_id": "0123456789",
            "slot_id": "slot-2", "state": state,
            "outcome_unknown": outcome_unknown,
        }
        self.outbox = {
            "execution_id": "exec_12345678", "order_id": "0123456789",
            "slot_id": "slot-2", "state": outbox_state,
        }

    def get_execution(self, execution_id):
        return dict(self.execution) if execution_id == "exec_12345678" else None

    def get_outbox_item(self, execution_id):
        return dict(self.outbox) if execution_id == "exec_12345678" else None


class FakeExecutionsClient:
    def __init__(self, executions):
        self.executions = executions
        self.request = None

    def list_executions(self, request):
        self.request = request
        return iter(self.executions)


class CloudRunRecoveryTests(unittest.TestCase):
    def test_execution_match_requires_order_execution_and_slot_ids(self):
        execution = SimpleNamespace(template=SimpleNamespace(containers=[
            SimpleNamespace(args=[
                "--order-id", "0123456789",
                "--execution-id", "exec_12345678",
                "--slot-id", "slot-2",
            ])
        ]))
        self.assertTrue(execution_matches(
            execution, order_id="0123456789",
            execution_id="exec_12345678", slot_id="slot-2",
        ))
        self.assertFalse(execution_matches(
            execution, order_id="0123456789",
            execution_id="exec_12345678", slot_id="slot-1",
        ))

    def test_inspection_reports_one_match_without_mutating_state(self):
        workflow = FakeWorkflow()
        match = {
            "name": "projects/demo/locations/asia-northeast1/jobs/worker/executions/run-1",
            "template": {"containers": [{"args": [
                "--order-id", "0123456789",
                "--execution-id", "exec_12345678",
                "--slot-id", "slot-2",
            ]}]},
            "running_count": 1,
        }
        client = FakeExecutionsClient([match])
        result = inspect_unknown_launch(
            workflow, client,
            job_resource="projects/demo/locations/asia-northeast1/jobs/worker",
            execution_id="exec_12345678",
        )
        self.assertEqual(result["status"], "matching_execution_found")
        self.assertEqual(result["matches"][0]["running_count"], 1)
        self.assertFalse(result["safe_to_release_slot"])
        self.assertEqual(workflow.execution["state"], "review")
        self.assertEqual(workflow.outbox["state"], "outcome_unknown")
        self.assertEqual(client.request["page_size"], 100)

    def test_no_match_is_not_treated_as_safe_to_release(self):
        client = FakeExecutionsClient([])
        result = inspect_unknown_launch(
            FakeWorkflow(), client,
            job_resource="projects/demo/locations/asia-northeast1/jobs/worker",
            execution_id="exec_12345678",
        )
        self.assertEqual(result["status"], "no_matching_execution_found")
        self.assertFalse(result["safe_to_release_slot"])

    def test_inspection_refuses_inconsistent_firestore_state(self):
        with self.assertRaisesRegex(ValueError, "not a consistent unknown launch"):
            inspect_unknown_launch(
                FakeWorkflow(outbox_state="dispatched"), FakeExecutionsClient([]),
                job_resource="projects/demo/locations/asia-northeast1/jobs/worker",
                execution_id="exec_12345678",
            )

    def test_inspection_validates_resource_and_execution_id(self):
        with self.assertRaisesRegex(ValueError, "invalid Cloud Run Job resource"):
            inspect_unknown_launch(
                FakeWorkflow(), FakeExecutionsClient([]),
                job_resource="worker", execution_id="exec_12345678",
            )
        with self.assertRaisesRegex(ValueError, "invalid execution id"):
            inspect_unknown_launch(
                FakeWorkflow(), FakeExecutionsClient([]),
                job_resource="projects/demo/locations/asia-northeast1/jobs/worker",
                execution_id="../bad",
            )


if __name__ == "__main__":
    unittest.main()
