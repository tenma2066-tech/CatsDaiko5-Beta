"""Unit tests for safe Cloud Tasks and Cloud Run Job boundaries."""
from __future__ import annotations

import json
import unittest
from google.api_core.exceptions import AlreadyExists, NotFound

from shop_cloud_run_dispatcher import (
    CloudTasksOutboxPublisher,
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
        self.outbox_order_id = "0123456789"

    def get_outbox_item(self, execution_id):
        return {"order_id": self.outbox_order_id, "execution_id": execution_id}

    def begin_dispatch(self, execution_id):
        if self.state != "task_enqueued":
            return None
        self.state = "launching"
        self.launch_claims += 1
        return {"order_id": "0123456789", "execution_id": execution_id, "slot_id": "slot-0"}

    def mark_dispatch_started(self, execution_id, operation):
        self.started = operation
        self.state = "dispatched"

    def mark_dispatch_unknown(self, execution_id, code):
        self.unknown = code
        self.state = "outcome_unknown"


class FakeLauncher:
    def __init__(self, operation=None, error=None):
        self.operation = operation or {
            "operation_name": "operations/abc", "execution_name": None,
        }
        self.error = error
        self.calls = []

    def launch(self, order_id, execution_id, slot_id):
        self.calls.append((order_id, execution_id, slot_id))
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
            build_task_payload("invalid-order", "exec_12345678")

    def test_dispatch_launches_once_and_duplicate_delivery_is_noop(self):
        repository = FakeRepository()
        launcher = FakeLauncher()
        payload = {"order_id": "0123456789", "execution_id": "exec_12345678"}
        first = handle_dispatch_payload(payload, repository, launcher)
        second = handle_dispatch_payload(payload, repository, launcher)
        self.assertEqual(first["state"], "started")
        self.assertEqual(second["state"], "duplicate_or_already_claimed")
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(launcher.calls[0][2], "slot-0")
        self.assertEqual(repository.started, {
            "operation_name": "operations/abc", "execution_name": None,
        })

    def test_dispatch_rejects_payload_order_mismatch_before_reserving_slot(self):
        repository = FakeRepository()
        launcher = FakeLauncher()
        repository.outbox_order_id = "9999999999"
        payload = {"order_id": "0123456789", "execution_id": "exec_12345678"}
        with self.assertRaisesRegex(ValueError, "binding mismatch"):
            handle_dispatch_payload(payload, repository, launcher)
        self.assertEqual(repository.launch_claims, 0)
        self.assertEqual(launcher.calls, [])

    def test_ambiguous_job_start_is_not_automatically_retried(self):
        repository = FakeRepository()
        launcher = FakeLauncher(error=TimeoutError("request timeout"))
        payload = {"order_id": "0123456789", "execution_id": "exec_12345678"}
        result = handle_dispatch_payload(payload, repository, launcher)
        self.assertEqual(result["state"], "outcome_unknown")
        self.assertEqual(repository.state, "outcome_unknown")
        self.assertEqual(repository.launch_claims, 1)

    def test_cloud_run_job_receives_order_execution_and_reserved_slot_args(self):
        class Operation:
            name = "operations/123"
            metadata = type("Metadata", (), {"name": "job-run-xyz"})()

        class Client:
            def run_job(self, request):
                self.request = request
                return Operation()

        client = Client()
        launcher = GoogleCloudRunJobLauncher(
            "projects/demo/locations/asia-northeast1/jobs/catsdaiko-worker",
            client=client,
        )
        operation = launcher.launch("0123456789", "exec_12345678", "slot-0")
        args = client.request["overrides"]["container_overrides"][0]["args"]
        self.assertEqual(operation, {
            "operation_name": "operations/123", "execution_name": "job-run-xyz",
        })
        self.assertEqual(args, [
            "--order-id", "0123456789",
            "--execution-id", "exec_12345678",
            "--slot-id", "slot-0",
        ])

    def test_cloud_tasks_publisher_uses_non_secret_deterministic_task(self):
        class Repo:
            def __init__(self):
                self.item = {
                    "task_id": "exec_12345678",
                    "execution_id": "exec_12345678",
                    "order_id": "0123456789",
                    "state": "pending",
                }
                self.marked = False

            def get_outbox_item(self, execution_id):
                return dict(self.item)

            def mark_task_enqueued(self, execution_id):
                self.item["state"] = "task_enqueued"
                self.marked = True

        class Client:
            def queue_path(self, project, location, queue):
                return f"projects/{project}/locations/{location}/queues/{queue}"

            def task_path(self, project, location, queue, task):
                return f"projects/{project}/locations/{location}/queues/{queue}/tasks/{task}"

            def create_task(self, request):
                self.request = request
                return request["task"]

            def get_task(self, request):
                return {"name": request["name"]}

        repo = Repo()
        client = Client()
        publisher = CloudTasksOutboxPublisher(
            client, repo, project="demo", location="asia-northeast1",
            queue="orders", dispatcher_url="https://dispatcher.example",
            oidc_service_account="tasks@example.iam.gserviceaccount.com",
        )
        self.assertTrue(publisher.publish("exec_12345678"))
        task = client.request["task"]
        body = json.loads(task.http_request.body)
        self.assertEqual(body, {
            "order_id": "0123456789",
            "execution_id": "exec_12345678",
        })
        self.assertEqual(task.http_request.url,
                         "https://dispatcher.example/dispatch")
        self.assertEqual(task.http_request.oidc_token.audience,
                         "https://dispatcher.example")
        self.assertTrue(repo.marked)
        self.assertFalse(publisher.publish("exec_12345678"))

    def test_cloud_tasks_already_exists_checks_task_before_acknowledging(self):
        class Repo:
            def __init__(self):
                self.item = {
                    "task_id": "exec_12345678", "execution_id": "exec_12345678",
                    "order_id": "0123456789", "state": "pending",
                    "task_generation": 0,
                }
                self.marked = False
                self.generations = []

            def get_outbox_item(self, execution_id):
                return dict(self.item)

            def advance_outbox_task_generation(self, execution_id, expected):
                self.generations.append(expected)
                self.item["task_generation"] = expected + 1
                return expected + 1

            def mark_task_enqueued(self, execution_id):
                self.item["state"] = "task_enqueued"
                self.marked = True

        class Client:
            def __init__(self):
                self.names = []
                self.created = 0

            def queue_path(self, project, location, queue):
                return f"projects/{project}/locations/{location}/queues/{queue}"

            def task_path(self, project, location, queue, task):
                return f"projects/{project}/locations/{location}/queues/{queue}/tasks/{task}"

            def create_task(self, request):
                self.names.append(request["task"].name)
                self.created += 1
                if self.created == 1:
                    raise AlreadyExists("dedupe reservation")
                return request["task"]

            def get_task(self, request):
                raise NotFound("task was already deleted")

        repo = Repo()
        client = Client()
        publisher = CloudTasksOutboxPublisher(
            client, repo, project="demo", location="asia-northeast1",
            queue="orders", dispatcher_url="https://dispatcher.example",
            oidc_service_account="tasks@example.iam.gserviceaccount.com",
        )
        self.assertTrue(publisher.publish("exec_12345678"))
        self.assertEqual(repo.generations, [0])
        self.assertTrue(repo.marked)
        self.assertEqual(len(client.names), 2)
        self.assertIn("exec-exec_12345678-g0", client.names[0])
        self.assertIn("exec-exec_12345678-g1", client.names[1])

    def test_cloud_tasks_already_exists_with_live_task_is_acknowledged(self):
        class Repo:
            def __init__(self):
                self.item = {
                    "task_id": "exec_12345678", "execution_id": "exec_12345678",
                    "order_id": "0123456789", "state": "pending",
                    "task_generation": 0,
                }
                self.marked = False

            def get_outbox_item(self, execution_id):
                return dict(self.item)

            def advance_outbox_task_generation(self, execution_id, expected):
                raise AssertionError("generation must not advance for a live task")

            def mark_task_enqueued(self, execution_id):
                self.item["state"] = "task_enqueued"
                self.marked = True

        class Client:
            def queue_path(self, project, location, queue):
                return f"projects/{project}/locations/{location}/queues/{queue}"

            def task_path(self, project, location, queue, task):
                return f"projects/{project}/locations/{queue}/tasks/{task}"

            def create_task(self, request):
                raise AlreadyExists("task exists")

            def get_task(self, request):
                return {"name": request["name"]}

        repo = Repo()
        publisher = CloudTasksOutboxPublisher(
            Client(), repo, project="demo", location="asia-northeast1",
            queue="orders", dispatcher_url="https://dispatcher.example",
            oidc_service_account="tasks@example.iam.gserviceaccount.com",
        )
        self.assertTrue(publisher.publish("exec_12345678"))
        self.assertTrue(repo.marked)

    def test_task_enqueued_outbox_republishes_when_cloud_task_was_deleted(self):
        class Repo:
            def __init__(self):
                self.item = {
                    "task_id": "exec_12345678", "execution_id": "exec_12345678",
                    "order_id": "0123456789", "state": "task_enqueued",
                    "task_generation": 0,
                }
                self.advanced = []
                self.marked = False

            def get_outbox_item(self, execution_id):
                return dict(self.item)

            def advance_outbox_task_generation(self, execution_id, expected):
                self.advanced.append(expected)
                self.item["task_generation"] = expected + 1
                return expected + 1

            def mark_task_enqueued(self, execution_id):
                self.marked = True

        class RotatingClient:
            def __init__(self):
                self.names = []

            def queue_path(self, project, location, queue):
                return f"projects/{project}/locations/{location}/queues/{queue}"

            def task_path(self, project, location, queue, task):
                return f"projects/{project}/locations/{location}/queues/{queue}/tasks/{task}"

            def get_task(self, request):
                raise NotFound("task generation is absent")

            def create_task(self, request):
                self.names.append(request["task"].name)
                if request["task"].name.endswith("-g0"):
                    raise AlreadyExists("deleted task name is still reserved")
                return request["task"]

        repo = Repo()
        client = RotatingClient()
        publisher = CloudTasksOutboxPublisher(
            client, repo, project="demo", location="asia-northeast1",
            queue="orders", dispatcher_url="https://dispatcher.example",
            oidc_service_account="tasks@example.iam.gserviceaccount.com",
        )
        self.assertTrue(publisher.publish("exec_12345678"))
        self.assertEqual(repo.advanced, [0])
        self.assertTrue(repo.marked)
        self.assertEqual(len(client.names), 2)
        self.assertTrue(client.names[1].endswith("-g1"))

    def test_task_enqueued_outbox_with_live_task_is_not_recreated(self):
        class Repo:
            def __init__(self):
                self.item = {
                    "task_id": "exec_12345678", "execution_id": "exec_12345678",
                    "order_id": "0123456789", "state": "task_enqueued",
                    "task_generation": 0,
                }

            def get_outbox_item(self, execution_id):
                return dict(self.item)

            def advance_outbox_task_generation(self, execution_id, expected):
                raise AssertionError("generation should not advance")

            def mark_task_enqueued(self, execution_id):
                raise AssertionError("existing task should not be republished")

        class Client:
            def queue_path(self, project, location, queue):
                return f"projects/{project}/locations/{location}/queues/{queue}"

            def task_path(self, project, location, queue, task):
                return f"projects/{project}/locations/{queue}/tasks/{task}"

            def get_task(self, request):
                return {"name": request["name"]}

            def create_task(self, request):
                raise AssertionError("existing task should not be created again")

        publisher = CloudTasksOutboxPublisher(
            Client(), Repo(), project="demo", location="asia-northeast1",
            queue="orders", dispatcher_url="https://dispatcher.example",
            oidc_service_account="tasks@example.iam.gserviceaccount.com",
        )
        self.assertFalse(publisher.publish("exec_12345678"))

    def test_task_enqueued_retry_exhaustion_stops_republishing(self):
        class Repo:
            def __init__(self):
                self.item = {
                    "task_id": "exec_12345678", "execution_id": "exec_12345678",
                    "order_id": "0123456789", "state": "task_enqueued",
                    "task_generation": 3,
                }

            def get_outbox_item(self, execution_id):
                return dict(self.item)

            def advance_outbox_task_generation(self, execution_id, expected):
                return None

            def mark_task_enqueued(self, execution_id):
                raise AssertionError("exhausted outbox must not be marked enqueued")

        class Client:
            def queue_path(self, project, location, queue):
                return f"projects/{project}/locations/{location}/queues/{queue}"

            def task_path(self, project, location, queue, task):
                return f"projects/{project}/locations/{location}/queues/{queue}/tasks/{task}"

            def get_task(self, request):
                raise NotFound("deleted task name still reserved")

            def create_task(self, request):
                raise AlreadyExists("dedupe reservation")

        publisher = CloudTasksOutboxPublisher(
            Client(), Repo(), project="demo", location="asia-northeast1",
            queue="orders", dispatcher_url="https://dispatcher.example",
            oidc_service_account="tasks@example.iam.gserviceaccount.com",
        )
        self.assertFalse(publisher.publish("exec_12345678"))

    def test_dispatch_rejects_secret_or_extra_payload_fields(self):
        repository = FakeRepository()
        launcher = FakeLauncher()
        with self.assertRaises(ValueError):
            handle_dispatch_payload({
                "order_id": "0123456789",
                "execution_id": "exec_12345678",
                "transfer_code": "must-not-be-sent",
            }, repository, launcher)
        self.assertEqual(launcher.calls, [])

    def test_dispatch_defers_without_consuming_task_when_no_global_slot(self):
        class NoSlotRepository(FakeRepository):
            def begin_dispatch(self, execution_id):
                self.launch_claims += 1
                return {"state": "no_slot"}

        repository = NoSlotRepository()
        launcher = FakeLauncher()
        result = handle_dispatch_payload(
            {"order_id": "0123456789", "execution_id": "exec_12345678"},
            repository, launcher,
        )
        self.assertEqual(result["state"], "no_slot")
        self.assertEqual(launcher.calls, [])


if __name__ == "__main__":
    unittest.main()
