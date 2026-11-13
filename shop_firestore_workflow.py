"""Firestore workflow primitives for Cloud Run task/job orchestration.

This module is intentionally not wired into the live checkout path yet. It
contains transaction boundaries that can be exercised against Firestore
Emulator before enabling the Firestore backend.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from typing import Any

from google.cloud import firestore


class WorkflowStateError(RuntimeError):
    pass


def _estimate_firestore_payload_size(value: Any) -> int:
    """Conservatively estimate serialized payload bytes before a document write."""
    if value is None:
        return 1
    if isinstance(value, bytes):
        return len(value) + 16
    if isinstance(value, str):
        return len(value.encode("utf-8")) + 8
    if isinstance(value, bool):
        return 1
    if isinstance(value, (int, float)):
        return 16
    if isinstance(value, dict):
        return 16 + sum(
            _estimate_firestore_payload_size(str(key))
            + _estimate_firestore_payload_size(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return 8 + sum(_estimate_firestore_payload_size(item) for item in value)
    return _estimate_firestore_payload_size(str(value))


_WORKER_ARRAY_FIELDS = ("log", "warn", "report", "skipped")
_WORKER_CHUNK_MAX_BYTES = 128 * 1024
_WORKER_CHUNK_MAX_COUNT = 32
MAX_OUTBOX_TASK_GENERATION = 3


def _split_worker_snapshot_arrays(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Split worker-visible arrays into bounded, ordered Firestore chunk docs."""
    chunks: list[dict[str, Any]] = []
    for field in _WORKER_ARRAY_FIELDS:
        raw_values = snapshot.get(field) or []
        if not isinstance(raw_values, (list, tuple)):
            raise WorkflowStateError("execution_snapshot_array_invalid")
        values = list(raw_values)
        current: list[Any] = []
        current_size = 64
        for value in values:
            item_size = _estimate_firestore_payload_size(value) + 16
            if item_size > _WORKER_CHUNK_MAX_BYTES:
                raise WorkflowStateError("execution_snapshot_item_too_large")
            if current and current_size + item_size > _WORKER_CHUNK_MAX_BYTES:
                chunks.append({"field": field, "items": current})
                current = []
                current_size = 64
            current.append(value)
            current_size += item_size
        if current:
            chunks.append({"field": field, "items": current})
    if len(chunks) > _WORKER_CHUNK_MAX_COUNT:
        raise WorkflowStateError("execution_snapshot_chunk_limit_exceeded")
    return chunks


def transfer_lock_id(transfer_code: str, pepper: bytes) -> str:
    if not isinstance(pepper, bytes) or len(pepper) < 32:
        raise ValueError("transfer-lock pepper must be at least 32 bytes")
    normalized = (transfer_code or "").strip()
    if not normalized:
        raise ValueError("transfer code is required")
    return hmac.new(pepper, normalized.encode("utf-8"), hashlib.sha256).hexdigest()


class FirestoreWorkflowRepository:
    """Transactional outbox, execution claims, global slots, locks and OTP state."""

    def __init__(self, client, clock=time.time, max_slots: int = 4):
        if type(max_slots) is not int or not 1 <= max_slots <= 32:
            raise ValueError("max_slots must be between 1 and 32")
        self._client = client
        self._clock = clock
        self._max_slots = max_slots
        self._orders = client.collection("orders")
        self._executions = client.collection("executions")
        self._outbox = client.collection("task_outbox")
        self._slots = client.collection("worker_slots")
        self._locks = client.collection("transfer_locks")
        self._otp = client.collection("admin_otp_sessions")

    def submit_credentials_and_enqueue(
        self, order_id: str, access_hash: str, encrypted_credentials: bytes,
        execution_id: str,
    ) -> dict[str, str]:
        """Persist credentials and create the execution/outbox atomically."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", execution_id or ""):
            raise ValueError("invalid execution id")
        order_ref = self._orders.document(order_id)
        execution_ref = self._executions.document(execution_id)
        outbox_ref = self._outbox.document(execution_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def submit(tx):
            order = order_ref.get(transaction=tx)
            execution = execution_ref.get(transaction=tx)
            outbox = outbox_ref.get(transaction=tx)
            if not order.exists:
                raise WorkflowStateError("order not found")
            data = order.to_dict()
            if data.get("access_hash") != access_hash:
                raise WorkflowStateError("order access denied")
            if (data.get("status") != "paid"
                    or data.get("credentials_submitted") is True
                    or data.get("job_id") is not None
                    or data.get("active_execution_id")):
                raise WorkflowStateError("order is not eligible for credential submission")
            if execution.exists or outbox.exists:
                raise WorkflowStateError("execution id already exists")
            now = self._clock()
            tx.update(order_ref, {
                "credential_enc": encrypted_credentials,
                "credentials_submitted": True,
                "active_execution_id": execution_id,
                "job_id": "queued",
                "updated": now,
            })
            tx.create(execution_ref, {
                "execution_id": execution_id, "order_id": order_id,
                "state": "queued", "progress": 0, "owner": None,
                "slot_id": None, "transfer_lock_id": None,
                "cloud_run_operation": None, "outcome_unknown": False,
                "created": now, "updated": now,
            })
            tx.create(outbox_ref, {
                "task_id": execution_id, "execution_id": execution_id,
                "order_id": order_id, "state": "pending",
                "task_generation": 0,
                "created": now, "updated": now,
            })
            return {"execution_id": execution_id, "task_id": execution_id}

        return submit(transaction)

    def enqueue_execution(self, order_id: str, execution_id: str) -> dict[str, str]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", execution_id or ""):
            raise ValueError("invalid execution id")
        order_ref = self._orders.document(order_id)
        execution_ref = self._executions.document(execution_id)
        outbox_ref = self._outbox.document(execution_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def enqueue(tx):
            order = order_ref.get(transaction=tx)
            existing_execution = execution_ref.get(transaction=tx)
            existing_outbox = outbox_ref.get(transaction=tx)
            if not order.exists:
                raise WorkflowStateError("order not found")
            data = order.to_dict()
            if (data.get("status") != "paid"
                    or not data.get("credentials_submitted")
                    or data.get("job_id") is not None
                    or data.get("active_execution_id")):
                raise WorkflowStateError("order is not eligible for execution")
            if existing_execution.exists or existing_outbox.exists:
                raise WorkflowStateError("execution id already exists")
            now = self._clock()
            tx.create(execution_ref, {
                "execution_id": execution_id,
                "order_id": order_id,
                "state": "queued",
                "progress": 0,
                "owner": None,
                "slot_id": None,
                "transfer_lock_id": None,
                "cloud_run_operation": None,
                "outcome_unknown": False,
                "created": now,
                "updated": now,
            })
            tx.create(outbox_ref, {
                "task_id": execution_id,
                "execution_id": execution_id,
                "order_id": order_id,
                "state": "pending",
                "task_generation": 0,
                "created": now,
                "updated": now,
            })
            tx.update(order_ref, {
                "active_execution_id": execution_id,
                "job_id": "queued",
                "updated": now,
            })
            return {"execution_id": execution_id, "task_id": execution_id}

        return enqueue(transaction)

    def advance_outbox_task_generation(
        self, execution_id: str, expected_generation: int,
    ) -> int | None:
        """Atomically rotate a Cloud Tasks name after an expired dedupe reservation."""
        if type(expected_generation) is not int or expected_generation < 0:
            raise ValueError("invalid task generation")
        reference = self._outbox.document(execution_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def advance(tx):
            snapshot = reference.get(transaction=tx)
            if not snapshot.exists:
                return None
            data = snapshot.to_dict()
            if data.get("state") not in {"pending", "task_enqueued"}:
                return None
            current = int(data.get("task_generation") or 0)
            if current != expected_generation:
                return current
            if current >= MAX_OUTBOX_TASK_GENERATION:
                execution_ref = self._executions.document(execution_id)
                order_ref = self._orders.document(data.get("order_id") or "")
                execution = execution_ref.get(transaction=tx)
                order = order_ref.get(transaction=tx)
                now = self._clock()
                # Only a queued execution with no owner/slot can be safely
                # closed here. A launched/running execution is never reset.
                if execution.exists:
                    execution_data = execution.to_dict()
                    if (execution_data.get("state") == "queued"
                            and not execution_data.get("owner")
                            and not execution_data.get("slot_id")):
                        tx.update(execution_ref, {
                            "state": "review",
                            "outcome_unknown": False,
                            "error_code": "cloud_tasks_retry_exhausted",
                            "updated": now,
                        })
                if order.exists:
                    order_data = order.to_dict()
                    if order_data.get("active_execution_id") == execution_id:
                        tx.update(order_ref, {
                            "status": "review",
                            "updated": now,
                        })
                tx.update(reference, {
                    "state": "review",
                    "error_code": "cloud_tasks_retry_exhausted",
                    "updated": now,
                })
                return None
            updated = current + 1
            tx.update(reference, {
                "task_generation": updated,
                "updated": self._clock(),
            })
            return updated

        return advance(transaction)

    def get_outbox_item(self, execution_id: str) -> dict[str, Any] | None:
        snapshot = self._outbox.document(execution_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    def list_pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("outbox limit must be between 1 and 500")
        query = self._outbox.where("state", "in", ["pending", "task_enqueued"]).limit(limit)
        return [snapshot.to_dict() for snapshot in query.stream()]

    def mark_task_enqueued(self, execution_id: str) -> None:
        reference = self._outbox.document(execution_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def mark(tx):
            snapshot = reference.get(transaction=tx)
            if not snapshot.exists:
                raise WorkflowStateError("outbox item not found")
            state = snapshot.to_dict().get("state")
            if state in {"task_enqueued", "launching", "dispatched",
                         "outcome_unknown", "cancelled"}:
                return
            if state != "pending":
                raise WorkflowStateError("outbox is not pending")
            tx.update(reference, {"state": "task_enqueued", "task_enqueued": self._clock(),
                                  "updated": self._clock()})

        mark(transaction)

    def begin_dispatch(self, execution_id: str) -> dict[str, str] | None:
        """Atomically reserve one of the four global slots before launching a Job."""
        outbox_ref = self._outbox.document(execution_id)
        execution_ref = self._executions.document(execution_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def begin(tx):
            outbox = outbox_ref.get(transaction=tx)
            execution = execution_ref.get(transaction=tx)
            if not outbox.exists or not execution.exists:
                raise WorkflowStateError("execution outbox state is incomplete")
            outbox_data = outbox.to_dict()
            # Cloud Tasks may deliver immediately after create_task and
            # before the publisher can persist task_enqueued. Accept pending
            # here so that a delivered task is never acknowledged and lost.
            if outbox_data.get("state") not in {"pending", "task_enqueued"}:
                return None
            order_ref = self._orders.document(outbox_data["order_id"])
            order = order_ref.get(transaction=tx)
            if not order.exists:
                raise WorkflowStateError("order not found")
            order_data = order.to_dict()
            if (order_data.get("status") != "paid"
                    or order_data.get("job_id") != "queued"
                    or order_data.get("active_execution_id") != execution_id
                    or order_data.get("credentials_submitted") is not True):
                now = self._clock()
                tx.update(outbox_ref, {"state": "cancelled", "updated": now})
                tx.update(execution_ref, {"state": "cancelled", "finished": now, "updated": now})
                return None
            # All slot reads happen before the first write in this transaction.
            candidates = []
            for index in range(self._max_slots):
                reference = self._slots.document(f"slot-{index}")
                snapshot = reference.get(transaction=tx)
                if not snapshot.exists or snapshot.to_dict().get("state") == "released":
                    candidates.append(reference)
            if not candidates:
                return {"state": "no_slot"}
            slot_ref = candidates[0]
            slot_id = slot_ref.id
            now = self._clock()
            tx.update(outbox_ref, {
                "state": "launching", "launch_started": now, "slot_id": slot_id,
                "updated": now,
            })
            tx.update(execution_ref, {
                "state": "starting", "slot_id": slot_id, "updated": now,
            })
            tx.update(order_ref, {
                "status": "fulfilling", "job_id": execution_id,
                "updated": now,
            })
            tx.set(slot_ref, {
                "slot_id": slot_id, "state": "reserved",
                "owner": "dispatch:" + execution_id,
                "execution_id": execution_id, "claimed": now, "updated": now,
            })
            return {
                "state": "launch",
                "execution_id": outbox_data["execution_id"],
                "order_id": outbox_data["order_id"],
                "slot_id": slot_id,
            }

        return begin(transaction)

    def mark_dispatch_started(self, execution_id: str, cloud_run_operation: str) -> None:
        outbox_ref = self._outbox.document(execution_id)
        execution_ref = self._executions.document(execution_id)
        order_id = None
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def finish(tx):
            outbox = outbox_ref.get(transaction=tx)
            execution = execution_ref.get(transaction=tx)
            if not outbox.exists or not execution.exists:
                raise WorkflowStateError("execution outbox state is incomplete")
            data = outbox.to_dict()
            if data.get("state") != "launching":
                raise WorkflowStateError("outbox is not in launching state")
            operation_name = (
                cloud_run_operation.get("operation_name")
                if isinstance(cloud_run_operation, dict) else str(cloud_run_operation)
            )
            execution_name = (
                cloud_run_operation.get("execution_name")
                if isinstance(cloud_run_operation, dict) else None
            )
            tx.update(outbox_ref, {
                "state": "dispatched",
                "cloud_run_operation": operation_name,
                "cloud_run_execution_name": execution_name,
                "updated": self._clock(),
            })
            tx.update(execution_ref, {
                "cloud_run_operation": operation_name,
                "cloud_run_execution_name": execution_name,
                "state": "starting",
                "updated": self._clock(),
            })

        finish(transaction)

    def mark_dispatch_unknown(self, execution_id: str, safe_error_code: str) -> None:
        outbox_ref = self._outbox.document(execution_id)
        execution_ref = self._executions.document(execution_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def mark(tx):
            outbox = outbox_ref.get(transaction=tx)
            execution = execution_ref.get(transaction=tx)
            if not outbox.exists or not execution.exists:
                raise WorkflowStateError("execution outbox state is incomplete")
            order_ref = self._orders.document(outbox.to_dict()["order_id"])
            order = order_ref.get(transaction=tx)
            if not order.exists:
                raise WorkflowStateError("order not found")
            if outbox.to_dict().get("state") not in {"launching", "dispatched"}:
                return
            now = self._clock()
            tx.update(outbox_ref, {
                "state": "outcome_unknown",
                "error_code": (safe_error_code or "launch_unknown")[:80],
                "updated": now,
            })
            tx.update(execution_ref, {
                "state": "review",
                "outcome_unknown": True,
                "updated": now,
            })
            tx.update(order_ref, {
                "status": "review",
                "job_id": execution_id,
                "payment_note": "cloud_run_launch_unknown",
                "updated": now,
            })

        mark(transaction)

    def cancel_queued_execution(self, order_id: str, execution_id: str) -> bool:
        order_ref = self._orders.document(order_id)
        execution_ref = self._executions.document(execution_id)
        outbox_ref = self._outbox.document(execution_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def cancel(tx):
            order = order_ref.get(transaction=tx)
            execution = execution_ref.get(transaction=tx)
            outbox = outbox_ref.get(transaction=tx)
            if not order.exists or not execution.exists or not outbox.exists:
                return False
            od, ex, ob = order.to_dict(), execution.to_dict(), outbox.to_dict()
            if (od.get("status") != "paid"
                    or od.get("job_id") != "queued"
                    or od.get("active_execution_id") != execution_id
                    or ex.get("state") != "queued"
                    or ob.get("state") not in {"pending", "task_enqueued"}):
                return False
            now = self._clock()
            tx.update(order_ref, {
                "status": "cancelled", "job_id": None,
                "active_execution_id": None, "updated": now,
            })
            tx.update(execution_ref, {
                "state": "cancelled", "finished": now, "updated": now,
            })
            tx.update(outbox_ref, {"state": "cancelled", "updated": now})
            return True

        return cancel(transaction)

    def fail_reserved_execution(self, order_id: str, execution_id: str, slot_id: str, reason: str) -> None:
        """Release only a slot reserved by this execution before it claimed a worker."""
        order_ref = self._orders.document(order_id)
        execution_ref = self._executions.document(execution_id)
        outbox_ref = self._outbox.document(execution_id)
        slot_ref = self._slots.document(slot_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def fail(tx):
            order = order_ref.get(transaction=tx)
            execution = execution_ref.get(transaction=tx)
            outbox = outbox_ref.get(transaction=tx)
            slot = slot_ref.get(transaction=tx)
            if not execution.exists or not slot.exists:
                return
            ex, slot_data = execution.to_dict(), slot.to_dict()
            if (ex.get("state") != "starting"
                    or ex.get("slot_id") != slot_id
                    or slot_data.get("state") != "reserved"
                    or slot_data.get("owner") != "dispatch:" + execution_id):
                return
            now = self._clock()
            tx.update(execution_ref, {
                "state": "review", "error_code": (reason or "worker_claim_failed")[:80],
                "finished": now, "updated": now,
            })
            if order.exists:
                tx.update(order_ref, {
                    "status": "review", "job_id": execution_id,
                    "payment_note": (reason or "worker_claim_failed")[:120],
                    "updated": now,
                })
            if outbox.exists:
                tx.update(outbox_ref, {"state": "review", "updated": now})
            tx.update(slot_ref, {
                "state": "released", "released": now, "updated": now,
            })

        fail(transaction)

    def claim_execution(
        self, order_id: str, execution_id: str, owner: str,
        transfer_digest: str, slot_id: str,
    ) -> bool:
        if slot_id not in {f"slot-{i}" for i in range(self._max_slots)}:
            raise ValueError("invalid worker slot")
        order_ref = self._orders.document(order_id)
        execution_ref = self._executions.document(execution_id)
        slot_ref = self._slots.document(slot_id)
        lock_ref = self._locks.document(transfer_digest)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def claim(tx):
            order = order_ref.get(transaction=tx)
            execution = execution_ref.get(transaction=tx)
            slot = slot_ref.get(transaction=tx)
            transfer_lock = lock_ref.get(transaction=tx)
            if not order.exists or not execution.exists:
                return False
            od, ex = order.to_dict(), execution.to_dict()
            if (od.get("status") != "fulfilling"
                    or od.get("job_id") != execution_id
                    or od.get("credentials_submitted") is not True
                    or ex.get("state") != "starting"
                    or ex.get("order_id") != order_id
                    or ex.get("slot_id") != slot_id):
                return False
            if (not slot.exists
                    or slot.to_dict().get("state") != "reserved"
                    or slot.to_dict().get("owner") != "dispatch:" + execution_id):
                return False
            if transfer_lock.exists and transfer_lock.to_dict().get("state") != "released":
                return False
            now = self._clock()
            tx.update(slot_ref, {
                "slot_id": slot_id, "state": "active",
                "owner": owner, "execution_id": execution_id,
                "claimed": now, "updated": now,
            })
            tx.set(lock_ref, {
                "state": "active", "owner": owner,
                "execution_id": execution_id, "order_id": order_id,
                "claimed": now, "updated": now,
            })
            tx.update(execution_ref, {
                "state": "running", "owner": owner,
                "cloud_run_execution_name": owner,
                "slot_id": slot_id, "transfer_lock_id": transfer_digest,
                "started": now, "updated": now,
            })
            tx.update(order_ref, {"status": "fulfilling", "job_id": execution_id, "updated": now})
            return True

        return claim(transaction)

    def update_progress(
        self, execution_id: str, owner: str, progress: int,
        message: str, log_chunk_id: str | None = None,
    ) -> None:
        if type(progress) is not int or not 0 <= progress <= 100:
            raise ValueError("progress must be an integer from 0 to 100")
        execution_ref = self._executions.document(execution_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def update(tx):
            snapshot = execution_ref.get(transaction=tx)
            if not snapshot.exists or snapshot.to_dict().get("owner") != owner:
                raise WorkflowStateError("execution owner mismatch")
            tx.update(execution_ref, {
                "progress": progress,
                "message": (message or "")[:300],
                "updated": self._clock(),
            })

        update(transaction)

    def finish_execution(
        self, order_id: str, execution_id: str, owner: str,
        *, success: bool, outcome_unknown: bool = False,
        safe_error_code: str = "",
    ) -> None:
        order_ref = self._orders.document(order_id)
        execution_ref = self._executions.document(execution_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def finish(tx):
            order = order_ref.get(transaction=tx)
            execution = execution_ref.get(transaction=tx)
            if not order.exists or not execution.exists:
                raise WorkflowStateError("execution state not found")
            ex = execution.to_dict()
            if ex.get("owner") != owner or ex.get("state") != "running":
                raise WorkflowStateError("execution owner/state mismatch")
            slot_id = ex.get("slot_id")
            lock_id = ex.get("transfer_lock_id")
            slot_ref = self._slots.document(slot_id) if slot_id else None
            lock_ref = self._locks.document(lock_id) if lock_id else None
            slot = slot_ref.get(transaction=tx) if slot_ref else None
            transfer_lock = lock_ref.get(transaction=tx) if lock_ref else None
            target = "completed" if success else "review"
            now = self._clock()
            tx.update(execution_ref, {
                "state": target,
                "progress": 100 if success else ex.get("progress", 0),
                "outcome_unknown": bool(outcome_unknown),
                "error_code": (safe_error_code or "")[:80],
                "finished": now,
                "updated": now,
            })
            tx.update(order_ref, {
                "status": target,
                "job_id": None if success else execution_id,
                "active_execution_id": execution_id,
                "updated": now,
            })
            if slot_ref and slot and slot.exists and slot.to_dict().get("owner") == owner:
                tx.update(slot_ref, {"state": "released", "released": now, "updated": now})
            # Keep the account lock on an unknown game-side outcome. Only a
            # confirmed success or known-safe failure can release it.
            if (lock_ref and transfer_lock and transfer_lock.exists
                    and transfer_lock.to_dict().get("owner") == owner
                    and not outcome_unknown):
                tx.update(lock_ref, {"state": "released", "released": now, "updated": now})

        finish(transaction)

    def list_active_executions(self, limit: int = 100) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("execution limit must be between 1 and 500")
        results = []
        for state in ("starting", "running"):
            remaining = limit - len(results)
            if remaining <= 0:
                break
            query = self._executions.where("state", "==", state).limit(remaining)
            results.extend(snapshot.to_dict() for snapshot in query.stream())
        return results

    def reconcile_terminal_execution(
        self, order_id: str, execution_id: str,
        *, owner: str | None, cloud_run_execution_name: str,
    ) -> bool:
        """Reconcile a confirmed terminal Cloud Run execution without replaying it."""
        order_ref = self._orders.document(order_id)
        execution_ref = self._executions.document(execution_id)
        outbox_ref = self._outbox.document(execution_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def reconcile(tx):
            order = order_ref.get(transaction=tx)
            execution = execution_ref.get(transaction=tx)
            outbox = outbox_ref.get(transaction=tx)
            if not order.exists or not execution.exists:
                return False
            ex = execution.to_dict()
            if ex.get("cloud_run_execution_name") != cloud_run_execution_name:
                return False
            state = ex.get("state")
            if state == "starting":
                expected_slot_owner = "dispatch:" + execution_id
                outcome_unknown = False
            elif state == "running" and ex.get("owner") == owner:
                expected_slot_owner = owner
                outcome_unknown = True
            else:
                return False
            slot_id = ex.get("slot_id")
            slot_ref = self._slots.document(slot_id) if slot_id else None
            slot = slot_ref.get(transaction=tx) if slot_ref else None
            now = self._clock()
            tx.update(execution_ref, {
                "state": "review",
                "outcome_unknown": outcome_unknown,
                "error_code": (
                    "job_terminal_before_worker_claim" if not outcome_unknown
                    else "job_terminal_without_final_state"
                ),
                "finished": now,
                "updated": now,
            })
            tx.update(order_ref, {
                "status": "review",
                "job_id": execution_id,
                "payment_note": (
                    "job_terminal_before_worker_claim" if not outcome_unknown
                    else "job_terminal_without_final_state"
                ),
                "updated": now,
            })
            if outbox.exists:
                tx.update(outbox_ref, {"state": "review", "updated": now})
            if (slot_ref and slot and slot.exists
                    and slot.to_dict().get("owner") == expected_slot_owner):
                tx.update(slot_ref, {
                    "state": "released", "released": now, "updated": now,
                })
            # Running executions retain their transfer lock because game-side
            # side effects may have happened. A pre-claim failure has no lock.
            return True

        return reconcile(transaction)

    def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        snapshot = self._executions.document(execution_id).get()
        if not snapshot.exists:
            return None
        result = snapshot.to_dict()
        chunks = list(
            self._client.collection("execution_logs")
            .document(execution_id).collection("chunks")
            .order_by("index").stream()
        )
        if chunks:
            for field in _WORKER_ARRAY_FIELDS:
                result[field] = []
            for chunk in chunks:
                data = chunk.to_dict()
                field = data.get("field")
                if field in _WORKER_ARRAY_FIELDS:
                    result[field].extend(data.get("items") or [])
        else:
            # Preserve inline arrays from executions written by earlier beta
            # revisions until their first chunked snapshot is persisted.
            for field in _WORKER_ARRAY_FIELDS:
                result[field] = list(result.get(field) or [])
        return result

    def persist_worker_snapshot(
        self, execution_id: str, owner: str, snapshot: dict[str, Any],
        result_enc: bytes | None = None,
    ) -> None:
        reference = self._executions.document(execution_id)
        chunks_ref = (
            self._client.collection("execution_logs")
            .document(execution_id).collection("chunks")
        )
        worker_chunks = _split_worker_snapshot_arrays(snapshot)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def persist(tx):
            current = reference.get(transaction=tx)
            if not current.exists or current.to_dict().get("owner") != owner:
                raise WorkflowStateError("execution owner mismatch")
            current_data = current.to_dict()
            if current_data.get("state") != "running":
                raise WorkflowStateError("execution is not running")
            # Read the old bounded chunk set before any transaction writes.
            old_chunks = list(chunks_ref.stream(transaction=tx))
            revision = int(current_data.get("worker_snapshot_revision") or 0) + 1
            data = {
                "worker_status": str(snapshot.get("status") or "running")[:24],
                "step": max(0, min(int(snapshot.get("step") or 0), 4)),
                "dry": bool(snapshot.get("dry")),
                "clone": bool(snapshot.get("clone")),
                "integrity_only": bool(snapshot.get("integrity_only")),
                "error": str(snapshot.get("error") or "")[:500],
                "outcome_unknown": bool(snapshot.get("outcome_unknown")),
                "worker_snapshot_revision": revision,
                "worker_snapshot_chunk_count": len(worker_chunks),
                "updated": self._clock(),
                # Remove legacy inline arrays on the first chunked update so
                # get_execution never concatenates old data with new chunks.
                **{field: firestore.DELETE_FIELD for field in _WORKER_ARRAY_FIELDS},
            }
            if result_enc is not None:
                data["result_enc"] = result_enc
            if snapshot.get("finished") is not None:
                data["worker_finished"] = snapshot.get("finished")
            if snapshot.get("audit") is not None:
                data["audit"] = snapshot["audit"]
            # Non-array metadata remains in the execution document with a
            # 700 KiB safety margin. Worker arrays live in bounded subdocuments.
            if _estimate_firestore_payload_size(data) > 700 * 1024:
                raise WorkflowStateError("execution_snapshot_metadata_too_large")
            for old in old_chunks:
                tx.delete(old.reference)
            for index, chunk in enumerate(worker_chunks):
                chunk_data = {
                    **chunk,
                    "revision": revision,
                    "index": index,
                    "updated": self._clock(),
                }
                if _estimate_firestore_payload_size(chunk_data) > _WORKER_CHUNK_MAX_BYTES:
                    raise WorkflowStateError("execution_snapshot_chunk_too_large")
                tx.create(chunks_ref.document(f"r{revision:08d}-{index:03d}"), chunk_data)
            tx.update(reference, data)

        persist(transaction)

    def save_otp_session(
        self, session_id: str, encrypted_state: str, expires_at: float,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", session_id or ""):
            raise ValueError("invalid OTP session id")
        if not isinstance(encrypted_state, str) or not encrypted_state or len(encrypted_state) > 20000:
            raise ValueError("invalid encrypted OTP session")
        if expires_at <= self._clock() or expires_at > self._clock() + 900:
            raise ValueError("OTP session expiry must be within 15 minutes")
        reference = self._otp.document(session_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def save(tx):
            snapshot = reference.get(transaction=tx)
            data = {
                "encrypted_state": encrypted_state,
                "state": "pending",
                "expires_at": expires_at,
                "updated": self._clock(),
            }
            if snapshot.exists:
                tx.set(reference, {**data, "created": snapshot.to_dict().get("created", self._clock())})
            else:
                tx.create(reference, {**data, "created": self._clock()})

        save(transaction)

    def get_otp_session(self, session_id: str) -> str | None:
        snapshot = self._otp.document(session_id).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict()
        if data.get("state") != "pending" or data.get("expires_at", 0) <= self._clock():
            return None
        return data.get("encrypted_state")

    def clear_otp_session(self, session_id: str) -> None:
        reference = self._otp.document(session_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def clear(tx):
            snapshot = reference.get(transaction=tx)
            if snapshot.exists:
                tx.update(reference, {"state": "cleared", "cleared": self._clock()})

        clear(transaction)

    def consume_otp_session(self, session_id: str) -> str | None:
        reference = self._otp.document(session_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def consume(tx):
            snapshot = reference.get(transaction=tx)
            if not snapshot.exists:
                return None
            data = snapshot.to_dict()
            if data.get("state") != "pending" or data.get("expires_at", 0) <= self._clock():
                return None
            tx.update(reference, {"state": "consumed", "consumed": self._clock()})
            return data.get("encrypted_state")

        return consume(transaction)
