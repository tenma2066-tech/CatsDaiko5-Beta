"""Read-only inspection of ambiguous Cloud Run Job launches.

This command never releases worker slots, clears transfer locks, or retries jobs.
It only correlates a Firestore execution ID with the non-secret Job arguments
stored in Cloud Run Execution resources.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any


def _field(value: Any, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _execution_args(execution: Any) -> list[str]:
    template = _field(execution, "template")
    containers = _field(template, "containers", []) or []
    if not containers:
        return []
    args = _field(containers[0], "args", []) or []
    return [str(item) for item in args]


def _option_value(args: list[str], option: str) -> str | None:
    for index, item in enumerate(args):
        if item == option and index + 1 < len(args):
            return args[index + 1]
        if item.startswith(option + "="):
            return item[len(option) + 1:]
    return None


def execution_matches(
    execution: Any, *, order_id: str, execution_id: str, slot_id: str,
) -> bool:
    """Match all three opaque identifiers; never inspect credentials or secrets."""
    args = _execution_args(execution)
    return (
        _option_value(args, "--order-id") == order_id
        and _option_value(args, "--execution-id") == execution_id
        and _option_value(args, "--slot-id") == slot_id
    )


def _execution_summary(execution: Any) -> dict[str, Any]:
    return {
        "name": str(_field(execution, "name", "") or ""),
        "create_time": str(_field(execution, "create_time", "") or ""),
        "start_time": str(_field(execution, "start_time", "") or ""),
        "completion_time": str(_field(execution, "completion_time", "") or ""),
        "running_count": int(_field(execution, "running_count", 0) or 0),
        "succeeded_count": int(_field(execution, "succeeded_count", 0) or 0),
        "failed_count": int(_field(execution, "failed_count", 0) or 0),
        "cancelled_count": int(_field(execution, "cancelled_count", 0) or 0),
    }


def inspect_unknown_launch(
    workflow, executions_client, *, job_resource: str, execution_id: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", execution_id or ""):
        raise ValueError("invalid execution id")
    if not re.fullmatch(
        r"projects/[^/]+/locations/[^/]+/jobs/[^/]+", job_resource or ""
    ):
        raise ValueError("invalid Cloud Run Job resource")

    stored = workflow.get_execution(execution_id)
    outbox = workflow.get_outbox_item(execution_id)
    if not stored or not outbox:
        raise ValueError("Firestore execution or outbox record was not found")
    order_id = stored.get("order_id")
    slot_id = stored.get("slot_id") or outbox.get("slot_id")
    if (stored.get("state") != "review"
            or stored.get("outcome_unknown") is not True
            or outbox.get("state") != "outcome_unknown"
            or outbox.get("order_id") != order_id
            or not order_id or not slot_id):
        raise ValueError("Firestore records are not a consistent unknown launch")

    pager = executions_client.list_executions(
        request={"parent": job_resource, "page_size": 100}
    )
    matches = [
        _execution_summary(item)
        for item in pager
        if execution_matches(
            item, order_id=order_id, execution_id=execution_id, slot_id=slot_id
        )
    ]
    if len(matches) == 1:
        status = "matching_execution_found"
    elif len(matches) > 1:
        status = "multiple_matching_executions_found"
    else:
        status = "no_matching_execution_found"
    return {
        "status": status,
        "execution_id": execution_id,
        "order_id": order_id,
        "slot_id": slot_id,
        "firestore_execution_state": stored.get("state"),
        "outbox_state": outbox.get("state"),
        "matches": matches,
        "safe_to_release_slot": False,
        "note": (
            "Read-only inspection only. A missing match is not proof that the "
            "Job API did not accept the launch. Do not release the slot or "
            "transfer lock based only on this result."
        ),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only inspection for an ambiguous Cloud Run Job launch"
    )
    parser.add_argument("--execution-id", required=True)
    parser.add_argument(
        "--job-resource",
        default=os.environ.get("CLOUD_RUN_JOB_RESOURCE", ""),
    )
    args = parser.parse_args(argv)
    if not args.job_resource:
        parser.error("--job-resource or CLOUD_RUN_JOB_RESOURCE is required")

    from google.cloud import firestore, run_v2
    from shop_firestore_workflow import FirestoreWorkflowRepository

    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("FIRESTORE_PROJECT_ID")
    client = firestore.Client(project=project) if project else firestore.Client()
    workflow = FirestoreWorkflowRepository(client)
    result = inspect_unknown_launch(
        workflow, run_v2.ExecutionsClient(),
        job_resource=args.job_resource, execution_id=args.execution_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "matching_execution_found":
        return 0
    if result["status"] == "no_matching_execution_found":
        return 2
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
