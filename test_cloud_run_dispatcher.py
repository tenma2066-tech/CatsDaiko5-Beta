"""Unit tests for safe Cloud Tasks and Cloud Run Job boundaries."""
from __future__ import annotations

import json
import unittest

from shop_cloud_run_dispatcher import (
    GoogleCloudRunJobLauncher,
    build_task_payload,
    handle_dispatch_payload,
)


class FakeRepository:
    def __init__(self):
        self.state = "task_enqueued"
        self.launch_claims = 0
        self.unknown = None
        self.started = None

    def begin_dispatch(self, execution_id):
        if self.state != "task_enqueued":
            return None
        self.state = "launching"
        self.launch_claims += 1
        return {"order_id": "0123456789", "execution_id": execution_id}

    def mark_dispatch_started(self, execution_id, operation):
        self.started = operation
        self.state = "dispatched"

    def mark_dispatch_unknown(self, execution_id, code):
        self.unknown = code
        self.state = "outcome_unknown"


class FakeLauncher:
    def __init__(self, operation="operations/abc", error=None):
        self.operation = operation
        self.error = error
        self.calls = []

    def launch(self, order_id, execution_id):
        self.calls.append((order_id, execution_id))
        if self.error:
            raise self.error
        return self.operation


class CloudRunDispatcherTests(unittest.TestCase):
    def test_task_payload_contains_only_non_secret_identifiers(self):
        payload = json.loads(build_task_payload("0123456789", "exec_12345678"))
        self.assertEqual(payload, {
            "order_id": "0123456789",
            "execution_id": "exec_12345678",
        })
        with self.assertRaises(ValueError):
            build_task_payload("0123456789", "exec_12345678", "transfer-code")

    def test_dispatch_launches_once_and_duplicate_delivery_is_noop(self):
        repository = FakeRepository()
        launcher = FakeLauncher()
        payload = {"order_id": "0123456789", "execution_id": "exec_12345678"}
        first = handle_dispatch_payload(payload, repository, launcher)
        second = handle_dispatch_payload(payload, repository, launcher)
        self.assertEqual(first["state"], "started")
        self.assertEqual(second["state"], "duplicate_or_already_claimed")
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(repository.started, "operations/abc")

    def test_ambiguous_job_start_is_not_automatically_retried(self):
        repository = FakeRepository()
        launcher = FakeLauncher(error=TimeoutError("request timeout"))
        payload = {"order_id": "0123456789", "execution_id": "exec_12345678"}
        result = handle_dispatch_payload(payload, repository, launcher)
        self.assertEqual(result["state"], "outcome_unknown")
        self.assertEqual(repository.state, "outcome_unknown")
        self.assertEqual(repository.launch_claims, 1)

    def test_cloud_run_job_receives_only_order_and_execution_ids(self):
        class Operation:
            name = "operations/123"

        class Client:
            def run_job(self, request):
                self.request = request
                return Operation()

        client = Client()
        launcher = GoogleCloudRunJobLauncher(
            "projects/demo/locations/asia-northeast1/jobs/catsdaiko-worker",
            client=client,
        )
        operation = launcher.launch("0123456789", "exec_12345678")
        args = client.request["overrides"]["container_overrides"][0]["args"]
        self.assertEqual(operation, "operations/123")
        self.assertEqual(args, [
            "--order-id", "0123456789",
            "--execution-id", "exec_12345678",
        ])


if __name__ == "__main__":
    unittest.main()
