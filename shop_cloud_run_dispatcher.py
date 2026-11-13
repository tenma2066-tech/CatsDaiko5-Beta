"""Cloud Tasks publisher and authenticated Cloud Run Job launch adapter.

Only order_id/execution_id cross the queue boundary. No customer credentials,
payment links, OTP, PayPay cookies or encrypted blobs are placed in task bodies.
"""
from __future__ import annotations

import json
import re
from typing import Any

from google.api_core.exceptions import AlreadyExists, NotFound


def build_task_payload(order_id: str, execution_id: str) -> bytes:
    if not re.fullmatch(r"[A-Z0-9]{10}", order_id or ""):
        raise ValueError("invalid order id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", execution_id or ""):
        raise ValueError("invalid execution id")
    return json.dumps(
        {"order_id": order_id, "execution_id": execution_id},
        separators=(",", ":"),
    ).encode("utf-8")


class CloudTasksOutboxPublisher:
    def __init__(
        self, client, repository, *, project: str, location: str, queue: str,
        dispatcher_url: str, oidc_service_account: str, audience: str | None = None,
    ):
        self._client = client
        self._repository = repository
        self._parent = client.queue_path(project, location, queue)
        self._dispatcher_url = dispatcher_url.rstrip("/") + "/dispatch"
        self._service_account = oidc_service_account
        self._audience = audience or dispatcher_url.rstrip("/")
        self._project, self._location, self._queue = project, location, queue

    def publish_pending(self, limit: int = 100) -> dict[str, int]:
        published = failed = 0
        for item in self._repository.list_pending_outbox(limit):
            try:
                if self.publish(item["execution_id"]):
                    published += 1
            except Exception:
                # Leave the outbox record pending; a later sweep can retry the
                # deterministic Cloud Tasks create operation safely.
                failed += 1
        return {"published": published, "failed": failed}

    def publish(self, execution_id: str) -> bool:
        from google.cloud import tasks_v2

        for _ in range(3):
            item = self._repository.get_outbox_item(execution_id)
            if not item:
                raise ValueError("outbox item not found")
            if item.get("state") == "task_enqueued":
                return False
            if item.get("state") != "pending":
                return False
            task_id = item["task_id"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", task_id):
                raise ValueError("invalid task id")
            generation = int(item.get("task_generation") or 0)
            task_name = self._client.task_path(
                self._project, self._location, self._queue,
                f"exec-{task_id}-g{generation}",
            )
            task = tasks_v2.Task(
                name=task_name,
                http_request=tasks_v2.HttpRequest(
                    http_method=tasks_v2.HttpMethod.POST,
                    url=self._dispatcher_url,
                    headers={"Content-Type": "application/json"},
                    body=build_task_payload(item["order_id"], item["execution_id"]),
                    oidc_token=tasks_v2.OidcToken(
                        service_account_email=self._service_account,
                        audience=self._audience,
                    ),
                ),
            )
            try:
                self._client.create_task(request={"parent": self._parent, "task": task})
            except AlreadyExists:
                # A deleted/exhausted task name remains reserved for up to 24h
                # (or longer for legacy queues). Confirm it still exists before
                # treating AlreadyExists as a successful enqueue.
                try:
                    self._client.get_task(request={"name": task_name})
                except NotFound:
                    next_generation = self._repository.advance_outbox_task_generation(
                        execution_id, generation
                    )
                    if next_generation is None:
                        return False
                    continue
            self._repository.mark_task_enqueued(execution_id)
            return True
        raise RuntimeError("Cloud Tasks task-name collision could not be resolved")


class GoogleCloudRunJobLauncher:
    def __init__(self, job_resource: str, client=None):
        self._job_resource = job_resource
        self._client = client

    def launch(self, order_id: str, execution_id: str, slot_id: str) -> str:
        if not self._job_resource:
            raise ValueError("Cloud Run Job resource is not configured")
        build_task_payload(order_id, execution_id)
        if not re.fullmatch(r"slot-[0-3]", slot_id or ""):
            raise ValueError("invalid reserved slot id")
        if self._client is None:
            from google.cloud import run_v2
            self._client = run_v2.JobsClient()
        operation = self._client.run_job(request={
            "name": self._job_resource,
            "overrides": {
                "container_overrides": [{
                    "args": [
                        "--order-id", order_id,
                        "--execution-id", execution_id,
                        "--slot-id", slot_id,
                    ],
                }],
            },
        })
        metadata = getattr(operation, "metadata", None)
        return {
            "operation_name": operation.name,
            "execution_name": getattr(metadata, "name", None),
        }


def handle_dispatch_payload(payload: dict[str, Any], repository, job_launcher) -> dict[str, str]:
    """Run once from the private OIDC-authenticated dispatcher HTTP handler."""
    if not isinstance(payload, dict) or set(payload) != {"order_id", "execution_id"}:
        raise ValueError("invalid dispatcher payload")
    order_id, execution_id = payload["order_id"], payload["execution_id"]
    build_task_payload(order_id, execution_id)
    claim = repository.begin_dispatch(execution_id)
    if claim is None:
        return {"state": "duplicate_or_already_claimed"}
    if claim.get("state") == "no_slot":
        return {"state": "no_slot"}
    try:
        operation_info = job_launcher.launch(
            claim["order_id"], claim["execution_id"], claim["slot_id"]
        )
        repository.mark_dispatch_started(execution_id, operation_info)
        return {"state": "started"}
    except Exception as exc:
        # The API may have accepted the Job before a network timeout. Never retry
        # a potentially non-idempotent launch automatically.
        repository.mark_dispatch_unknown(
            execution_id, type(exc).__name__.lower()
        )
        return {"state": "outcome_unknown"}
