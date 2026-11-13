"""Private HTTP target for Cloud Tasks.

Deploy this service with unauthenticated access disabled. Cloud Run IAM must
grant roles/run.invoker only to the dedicated Cloud Tasks OIDC service account.
"""
from __future__ import annotations

import os
import time

from flask import Flask, jsonify, request

from google.cloud import firestore

from shop_cloud_run_dispatcher import (
    CloudTasksOutboxPublisher,
    GoogleCloudRunJobLauncher,
    handle_dispatch_payload,
)
from shop_firestore_workflow import FirestoreWorkflowRepository


def create_app():
    application = Flask(__name__)

    @application.get("/healthz")
    def healthz():
        return jsonify(ok=True)

    @application.post("/sweep")
    def sweep():
        project = os.environ.get("CLOUD_TASKS_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
        location = os.environ.get("CLOUD_TASKS_LOCATION", "asia-northeast1")
        queue = os.environ.get("CLOUD_TASKS_QUEUE")
        dispatcher_url = os.environ.get("CLOUD_RUN_DISPATCHER_URL")
        service_account = os.environ.get("CLOUD_TASKS_SERVICE_ACCOUNT")
        if not all((project, location, queue, dispatcher_url, service_account)):
            return jsonify(error="outbox publisher is not configured"), 503
        try:
            client = firestore.Client(project=project) if project else firestore.Client()
            repository = FirestoreWorkflowRepository(client)
            from google.cloud import tasks_v2
            publisher = CloudTasksOutboxPublisher(
                tasks_v2.CloudTasksClient(), repository,
                project=project, location=location, queue=queue,
                dispatcher_url=dispatcher_url,
                oidc_service_account=service_account,
                audience=os.environ.get("CLOUD_RUN_DISPATCHER_AUDIENCE"),
            )
            result = publisher.publish_pending(limit=100)
            return jsonify(result), 200
        except Exception:
            application.logger.exception("Cloud Tasks outbox sweep failed")
            return jsonify(error="outbox sweep temporarily unavailable"), 503

    @application.post("/reconcile")
    def reconcile():
        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("FIRESTORE_PROJECT_ID")
        job_resource = os.environ.get("CLOUD_RUN_JOB_RESOURCE", "").strip()
        if not job_resource:
            return jsonify(error="worker Job resource is not configured"), 503
        try:
            client = firestore.Client(project=project) if project else firestore.Client()
            repository = FirestoreWorkflowRepository(client)
            from google.cloud import run_v2
            executions_client = run_v2.ExecutionsClient()
            checked = reconciled = unknown_launches = 0
            stale_launch_seconds = max(
                60, int(os.environ.get("CLOUD_RUN_STALE_LAUNCH_SECONDS", "600"))
            )
            now = time.time()
            for item in repository.list_active_executions(limit=100):
                owner = item.get("owner")
                order_id = item.get("order_id")
                execution_id = item.get("execution_id")
                execution_name = item.get("cloud_run_execution_name") or owner
                if not order_id or not execution_id:
                    continue
                if not execution_name:
                    # A dispatcher can die after reserving a slot but before
                    # persisting the Cloud Run execution name. After a long
                    # grace period, surface it as unknown for manual review.
                    # Keep the reserved slot: absence of a name is not proof
                    # that the Job API did not accept the launch.
                    started = item.get("updated")
                    if (item.get("state") == "starting" and isinstance(started, (int, float))
                            and now - started >= stale_launch_seconds):
                        repository.mark_dispatch_unknown(
                            execution_id, "dispatcher_launch_confirmation_timeout"
                        )
                        unknown_launches += 1
                    continue
                if str(execution_name).startswith("worker-"):
                    continue
                resource_name = (
                    str(execution_name) if "/" in str(execution_name)
                    else job_resource + "/executions/" + str(execution_name)
                )
                try:
                    execution = executions_client.get_execution(request={"name": resource_name})
                except Exception as exc:
                    # Not found or transient API errors are not proof of completion.
                    if type(exc).__name__ in {"NotFound", "PermissionDenied"}:
                        continue
                    raise
                checked += 1
                if getattr(execution, "completion_time", None):
                    if repository.reconcile_terminal_execution(
                        order_id, execution_id, owner=owner,
                        cloud_run_execution_name=str(execution_name),
                    ):
                        reconciled += 1
            return jsonify(checked=checked, reconciled=reconciled, unknown_launches=unknown_launches), 200
        except Exception:
            application.logger.exception("Cloud Run execution reconciliation failed")
            return jsonify(error="reconciliation temporarily unavailable"), 503

    @application.post("/dispatch")
    def dispatch():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="invalid payload"), 400
        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("FIRESTORE_PROJECT_ID")
        job_resource = os.environ.get("CLOUD_RUN_JOB_RESOURCE", "").strip()
        if not job_resource:
            return jsonify(error="dispatcher is not configured"), 503
        try:
            client = firestore.Client(project=project) if project else firestore.Client()
            repository = FirestoreWorkflowRepository(client)
            launcher = GoogleCloudRunJobLauncher(job_resource)
            result = handle_dispatch_payload(payload, repository, launcher)
        except ValueError:
            return jsonify(error="invalid payload"), 400
        except Exception:
            # Do not log request bodies or exception strings; the caller retries.
            application.logger.exception("Cloud Run dispatch infrastructure failure")
            return jsonify(error="dispatch temporarily unavailable"), 503
        if result.get("state") == "no_slot":
            response = jsonify(result)
            response.headers["Retry-After"] = "15"
            return response, 503
        return jsonify(result), 200

    return application


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
