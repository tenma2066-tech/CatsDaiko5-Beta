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

    def begin_dispatch(self, execution_id: str) -> dict[str, str] | None:
        """Single-use launch gate; ambiguous launches are never auto-replayed."""
        reference = self._outbox.document(execution_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def begin(tx):
            snapshot = reference.get(transaction=tx)
            if not snapshot.exists:
                raise WorkflowStateError("outbox item not found")
            data = snapshot.to_dict()
            if data.get("state") != "pending":
                return None
            tx.update(reference, {
                "state": "launching",
                "launch_started": self._clock(),
                "updated": self._clock(),
            })
            return {
                "execution_id": data["execution_id"],
                "order_id": data["order_id"],
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
            tx.update(outbox_ref, {
                "state": "dispatched",
                "cloud_run_operation": cloud_run_operation,
                "updated": self._clock(),
            })
            tx.update(execution_ref, {
                "cloud_run_operation": cloud_run_operation,
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
            if outbox.to_dict().get("state") != "launching":
                return
            tx.update(outbox_ref, {
                "state": "outcome_unknown",
                "error_code": (safe_error_code or "launch_unknown")[:80],
                "updated": self._clock(),
            })
            tx.update(execution_ref, {
                "state": "review",
                "outcome_unknown": True,
                "updated": self._clock(),
            })

        mark(transaction)

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
            if (od.get("status") != "paid"
                    or od.get("active_execution_id") != execution_id
                    or od.get("credentials_submitted") is not True
                    or ex.get("state") not in {"queued", "starting"}
                    or ex.get("order_id") != order_id):
                return False
            if slot.exists and slot.to_dict().get("state") != "released":
                return False
            if transfer_lock.exists and transfer_lock.to_dict().get("state") != "released":
                return False
            now = self._clock()
            tx.set(slot_ref, {
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
                "slot_id": slot_id, "transfer_lock_id": transfer_digest,
                "started": now, "updated": now,
            })
            tx.update(order_ref, {"job_id": execution_id, "updated": now})
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
                "updated": now,
            })
            slot_id = ex.get("slot_id")
            lock_id = ex.get("transfer_lock_id")
            if slot_id:
                slot_ref = self._slots.document(slot_id)
                slot = slot_ref.get(transaction=tx)
                if slot.exists and slot.to_dict().get("owner") == owner:
                    tx.update(slot_ref, {"state": "released", "released": now, "updated": now})
            if lock_id:
                lock_ref = self._locks.document(lock_id)
                lock = lock_ref.get(transaction=tx)
                if lock.exists and lock.to_dict().get("owner") == owner:
                    tx.update(lock_ref, {"state": "released", "released": now, "updated": now})

        finish(transaction)

    def save_otp_session(
        self, session_id: str, encrypted_state: str, expires_at: float,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", session_id or ""):
            raise ValueError("invalid OTP session id")
        if not isinstance(encrypted_state, str) or not encrypted_state or len(encrypted_state) > 20000:
            raise ValueError("invalid encrypted OTP session")
        if expires_at <= self._clock() or expires_at > self._clock() + 900:
            raise ValueError("OTP session expiry must be within 15 minutes")
        self._otp.document(session_id).create({
            "encrypted_state": encrypted_state,
            "state": "pending",
            "expires_at": expires_at,
            "created": self._clock(),
        })

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
