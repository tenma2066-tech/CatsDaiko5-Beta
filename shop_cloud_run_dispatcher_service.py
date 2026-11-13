"""Private HTTP target for Cloud Tasks.

Deploy this service with unauthenticated access disabled. Cloud Run IAM must
grant roles/run.invoker only to the dedicated Cloud Tasks OIDC service account.
"""
from __future__ import annotations

import os

from flask import Flask, jsonify, request

from google.cloud import firestore

from shop_cloud_run_dispatcher import (
    GoogleCloudRunJobLauncher,
    handle_dispatch_payload,
)
from shop_firestore_workflow import FirestoreWorkflowRepository


def create_app():
    application = Flask(__name__)

    @application.get("/healthz")
    def healthz():
        return jsonify(ok=True)

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
