"""Private HTTP target for Cloud Tasks.

Deploy this service with unauthenticated access disabled. Cloud Run IAM must
grant roles/run.invoker only to the dedicated Cloud Tasks OIDC service account.
"""
from __future__ import annotations

import os

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
