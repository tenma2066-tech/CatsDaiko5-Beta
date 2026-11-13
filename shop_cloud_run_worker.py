"""Cloud Run Job entrypoint for one persisted storefront execution.

The Job receives identifiers only. It fetches encrypted order state from
Firestore, claims its pre-reserved global slot and HMAC account lock, then
reuses the existing BCSFE worker implementation without the web process queue.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import uuid
from typing import Any

from cryptography.fernet import Fernet

from google.cloud import firestore

from shop_firestore_repository import FirestoreOrderRepository
from shop_firestore_workflow import (
    FirestoreWorkflowRepository,
    WorkflowStateError,
    transfer_lock_id,
)


def _cipher() -> Fernet:
    value = os.environ.get("PAYPAY_ENCRYPTION_KEY", "").strip()
    if not value:
        raise RuntimeError("PAYPAY_ENCRYPTION_KEY is not configured")
    return Fernet(value.encode("ascii"))


def _pepper() -> bytes:
    value = os.environ.get("TRANSFER_LOCK_PEPPER", "").strip()
    if not value:
        raise RuntimeError("TRANSFER_LOCK_PEPPER is not configured")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise RuntimeError("TRANSFER_LOCK_PEPPER is invalid") from exc
    if len(decoded) < 32:
        raise RuntimeError("TRANSFER_LOCK_PEPPER must decode to at least 32 bytes")
    return decoded


def _decrypt_json(cipher: Fernet, value: Any, label: str) -> dict:
    if not value:
        raise RuntimeError(label + " is missing")
    try:
        decoded = json.loads(cipher.decrypt(value))
    except Exception as exc:
        raise RuntimeError(label + " could not be decrypted") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(label + " has an invalid structure")
    return decoded


def _build_arguments(app_module, operation: dict, credentials: dict):
    for name in ("safe", "dry", "clone", "integrity_only"):
        if name in operation and type(operation[name]) is not bool:
            raise ValueError("invalid option: " + name)
    transfer = str(credentials.get("transfer_code") or "").strip()
    confirm = str(credentials.get("pin") or "").strip()
    if bool(transfer) != bool(confirm):
        raise ValueError("transfer credentials are incomplete")
    clone = bool(operation.get("clone", False))
    integrity_only = bool(operation.get("integrity_only", False))
    if clone and integrity_only:
        raise ValueError("clone and integrity-only modes cannot be combined")
    if integrity_only and not (transfer and confirm):
        raise ValueError("integrity-only mode requires existing-account credentials")
    if clone and not (transfer and confirm):
        raise ValueError("clone mode requires existing-account credentials")

    data = dict(operation)
    if clone:
        data = {"clone": True, "dry": bool(operation.get("dry", False)), "safe": False}
    elif integrity_only:
        data = {"transfer": transfer, "confirm": confirm,
                "dry": bool(operation.get("dry", False))}

    items_raw = data.get("items", [])
    subs_raw = data.get("subs", [])
    others_raw = data.get("others", [])
    if not all(isinstance(value, list) for value in (items_raw, subs_raw, others_raw)):
        raise ValueError("operation item lists are invalid")
    items = [value for value in items_raw if value in app_module.ITEMS]
    subs = [value for value in subs_raw if value in app_module.SUBS]
    others = [value for value in others_raw if value in app_module.OTHERS]

    cat_nums, cat_error = app_module.parse_cat_numbers(str(data.get("chars", "")))
    if cat_error:
        raise ValueError("character selection is invalid")
    raw_settings = data.get("cat_settings") or {}
    if not isinstance(raw_settings, dict):
        raise ValueError("character settings are invalid")
    allowed = {"base_level", "plus_level", "form", "talents_all"}
    cat_settings = {}
    for raw_number, raw_setting in raw_settings.items():
        try:
            number = int(raw_number)
        except (TypeError, ValueError) as exc:
            raise ValueError("character settings number is invalid") from exc
        if number not in cat_nums or not isinstance(raw_setting, dict):
            raise ValueError("character settings do not match selected characters")
        if set(raw_setting) - allowed:
            raise ValueError("unsupported character setting")
        setting = {}
        for field, low, high in (
            ("base_level", 1, 100), ("plus_level", 0, 999), ("form", 1, 3),
        ):
            value = raw_setting.get(field)
            if value is None or str(value).strip() == "":
                continue
            if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit():
                raise ValueError("character level/form must be an integer")
            value = int(value)
            if not low <= value <= high:
                raise ValueError("character level/form is out of range")
            setting[field] = value
        if raw_setting.get("talents_all") not in (None, False, True):
            raise ValueError("talent setting is invalid")
        if raw_setting.get("talents_all") is True:
            setting["talents_all"] = True
        cat_settings[number - app_module.DB_OFFSET] = setting

    qty = {}
    raw_qty = data.get("qty") or {}
    if not isinstance(raw_qty, dict):
        raise ValueError("quantity settings are invalid")
    for key, raw_value in raw_qty.items():
        if key not in app_module.ITEMS:
            continue
        value = str(raw_value).strip()
        if not value:
            continue
        if not value.isascii() or not value.isdigit():
            raise ValueError("quantity must be a non-negative integer")
        number = int(value)
        if number > app_module.engine.QUANTITY_ITEM_LIMITS.get(key, 999):
            raise ValueError("quantity is over the allowed limit")
        qty[key] = number

    dry = bool(data.get("dry", False))
    safe = bool(data.get("safe", True))
    skipped = []
    if safe:
        skipped = [value for value in app_module.HIGH_RISK_ITEMS if value in items or value in qty]
        items = [value for value in items if value not in skipped]
        qty = {key: value for key, value in qty.items() if key not in skipped}
    if not clone and not integrity_only and not (items or subs or others or qty or cat_nums):
        raise ValueError("no executable operation remains")
    arguments = (
        transfer, confirm, items, subs, others, qty, cat_nums, cat_settings,
        dry, clone, integrity_only,
    )
    return arguments, {
        "dry": dry, "clone": clone, "integrity_only": integrity_only,
        "skipped": skipped,
        "mode": "integrity" if integrity_only else ("download" if confirm else "new"),
        "count": len(items) + len(subs) + len(others) + len(qty) + len(cat_nums),
    }


def _redact(value: Any, secrets_to_redact: tuple[str, ...]) -> str:
    text = str(value or "")
    for secret in secrets_to_redact:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text[:500]


class _ProgressPipe:
    def __init__(self, workflow, execution_id, owner, cipher, secrets_to_redact):
        self.workflow = workflow
        self.execution_id = execution_id
        self.owner = owner
        self.cipher = cipher
        self.secrets_to_redact = secrets_to_redact
        self.last_write = 0.0
        self.last_step = None
        self.last_status = None
        self.error = None

    def send(self, message):
        job = message.get("job") or {}
        now = time.monotonic()
        status = job.get("status")
        step = job.get("step")
        if (status == self.last_status and step == self.last_step
                and now - self.last_write < 3 and status == "running"):
            return
        snapshot = {
            "status": status,
            "step": step,
            "log": [_redact(line, self.secrets_to_redact) for line in (job.get("log") or [])[-60:]],
            "warn": [_redact(line, self.secrets_to_redact) for line in (job.get("warn") or [])[-100:]],
            "report": job.get("report") or [],
            "skipped": job.get("skipped") or [],
            "dry": job.get("dry"),
            "clone": job.get("clone"),
            "integrity_only": job.get("integrity_only"),
            "error": _redact(job.get("error"), self.secrets_to_redact),
            "outcome_unknown": bool(job.get("outcome_unknown") or job.get("issuing_codes")),
            "finished": job.get("finished"),
        }
        result = job.get("result")
        result_enc = None
        if result is not None:
            result_enc = self.cipher.encrypt(
                json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
        try:
            self.workflow.persist_worker_snapshot(
                self.execution_id, self.owner, snapshot, result_enc=result_enc
            )
        except Exception as exc:
            self.error = exc
            return
        self.last_write = now
        self.last_step = step
        self.last_status = status

    def close(self):
        return None


def run_worker(order_id: str, execution_id: str, slot_id: str) -> int:
    if not re.fullmatch(r"[A-Z0-9]{10}", order_id or ""):
        raise ValueError("invalid order id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", execution_id or ""):
        raise ValueError("invalid execution id")
    if not re.fullmatch(r"slot-[0-3]", slot_id or ""):
        raise ValueError("invalid slot id")

    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("FIRESTORE_PROJECT_ID")
    client = firestore.Client(project=project) if project else firestore.Client()
    orders = FirestoreOrderRepository(client)
    workflow = FirestoreWorkflowRepository(client)
    try:
        cipher = _cipher()
        pepper = _pepper()
        order = orders.get_order(order_id)
        if (not order or order.get("status") != "fulfilling"
                or order.get("job_id") != execution_id
                or order.get("credentials_submitted") is not True
                or order.get("active_execution_id") != execution_id):
            raise WorkflowStateError("order_state_mismatch")
        credentials = _decrypt_json(cipher, order.get("credential_enc"), "credentials")
        operation = _decrypt_json(cipher, order.get("operation_enc"), "operation")
        transfer = str(credentials.get("transfer_code") or "").strip()
        lock_subject = transfer if transfer else "new-order:" + order_id
        lock_digest = transfer_lock_id(lock_subject, pepper)
        import app as app_module
        arguments, metadata = _build_arguments(app_module, operation, credentials)
    except Exception as exc:
        workflow.fail_reserved_execution(
            order_id, execution_id, slot_id, "worker_preflight_" + type(exc).__name__.lower()
        )
        return 4

    owner = os.environ.get("CLOUD_RUN_EXECUTION") or ("worker-" + uuid.uuid4().hex)
    if not workflow.claim_execution(
        order_id, execution_id, owner, lock_digest, slot_id
    ):
        workflow.fail_reserved_execution(
            order_id, execution_id, slot_id, "worker_claim_rejected"
        )
        return 3

    try:
        job = {
            "status": "running", "step": 0, "log": [], "warn": [], "report": [],
            "skipped": metadata["skipped"], "dry": metadata["dry"],
            "clone": metadata["clone"], "integrity_only": metadata["integrity_only"],
            "result": None, "error": None, "created": time.time(), "finished": None,
            "owner": owner, "mode": metadata["mode"], "count": metadata["count"],
        }
        redactions = (transfer, str(credentials.get("pin") or ""), order.get("access_hash", ""))
        pipe = _ProgressPipe(workflow, execution_id, owner, cipher, redactions)
        app_module._worker_entry(
            pipe, job, arguments, int(slot_id.split("-")[1]),
            app_module._learned.get("version"), dict(app_module._names),
        )
        if pipe.error:
            raise RuntimeError("progress persistence failed")
        # Force a final snapshot even when the last log line was within throttle.
        pipe.last_write = 0
        pipe.send({"job": job})
        if pipe.error:
            raise RuntimeError("final progress persistence failed")
        success = job.get("status") == "done"
        unknown = bool(job.get("outcome_unknown") or job.get("issuing_codes"))
        workflow.finish_execution(
            order_id, execution_id, owner,
            success=success, outcome_unknown=unknown,
            safe_error_code="" if success else ("worker_outcome_unknown" if unknown else "worker_failed"),
        )
        return 0 if success else 1
    except Exception as exc:
        # Once the BCSFE worker has started, an unexpected exception is treated
        # as potentially side-effecting; preserve the account lock for review.
        try:
            workflow.finish_execution(
                order_id, execution_id, owner,
                success=False, outcome_unknown=True,
                safe_error_code=type(exc).__name__.lower(),
            )
        except Exception:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--slot-id", required=True)
    args = parser.parse_args()
    return run_worker(args.order_id, args.execution_id, args.slot_id)


if __name__ == "__main__":
    sys.exit(main())
