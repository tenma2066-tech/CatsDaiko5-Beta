"""Firestore transactional adapters for storefront orders and payments.

These adapters are selected only when SHOP_STORAGE_BACKEND=firestore. The
legacy SQLite/GitHub snapshot backend remains the default; Firestore selection
fails closed rather than silently falling back to legacy storage.
"""
from __future__ import annotations

import time
import re
from typing import Any

from google.cloud import firestore
from google.api_core.exceptions import AlreadyExists

from shop_payment_repository import PaymentRepositoryError


class FirestoreOrderRepository:
    def __init__(self, client, clock=time.time):
        self._client = client
        self._clock = clock
        self._orders = client.collection("orders")

    def import_legacy_order(self, values: dict[str, Any]) -> bool:
        """Create-only import preserving encrypted blobs and a safe inactive state."""
        order_id = values["id"]
        if not re.fullmatch(r"[A-Z0-9]{10}", order_id or ""):
            raise ValueError("invalid legacy order id")
        status = values.get("status", "review")
        if status in {"fulfilling", "payment_processing"}:
            status = "review"
        if status not in {
            "awaiting_payment", "paid", "review", "completed", "cancelled",
        }:
            status = "review"
        if values.get("job_id") and status in {"paid", "completed"}:
            status = "review"
        document = self._orders.document(order_id)
        try:
            document.create({
                "id": order_id,
                "access_hash": values["access_hash"],
                "sku": values.get("sku", ""),
                "title": values.get("title", ""),
                "amount": int(values.get("amount", 0)),
                "unit_price_yen": int(values.get("unit_price_yen", values.get("amount", 0))),
                "quantity": int(values.get("quantity", 1)),
                "credential_enc": values["credential_enc"],
                "operation_enc": values.get("operation_enc"),
                "service_type": values.get("service_type") or "other-service",
                "job_id": None,
                "active_execution_id": None,
                "credentials_submitted": bool(values.get("credentials_submitted", False)),
                "status": status,
                "payment_digest": values.get("payment_digest"),
                "payment_note": (values.get("payment_note") or "legacy_import")[:300],
                "created": float(values.get("created") or self._clock()),
                "updated": self._clock(),
                "legacy_imported": True,
            })
            return True
        except AlreadyExists:
            existing = document.get()
            if not existing.exists or not existing.to_dict().get("legacy_imported"):
                raise RuntimeError(
                    "existing Firestore order conflicts with create-only legacy import"
                )
            return False

    def create_order(self, values: dict[str, Any]) -> None:
        document = self._orders.document(values["id"])
        data = {
            "id": values["id"],
            "access_hash": values["access_hash"],
            "sku": values["sku"],
            "title": values["title"],
            "amount": values["amount"],
            "unit_price_yen": values["unit_price_yen"],
            "quantity": values["quantity"],
            "credential_enc": values["credential_enc"],
            "operation_enc": values.get("operation_enc"),
            "service_type": values["service_type"],
            "job_id": None,
            "credentials_submitted": False,
            "status": "awaiting_payment",
            "payment_digest": None,
            "payment_note": "",
            "created": values["now"],
            "updated": values["now"],
        }
        document.create(data)

    def get_order(self, order_id: str):
        snapshot = self._orders.document(order_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    def list_orders(self, limit: int = 100):
        query = self._orders.order_by("created", direction=firestore.Query.DESCENDING)
        return [snapshot.to_dict() for snapshot in query.limit(limit).stream()]

    def claim_execution(self, order_id: str, access_hash: str) -> bool:
        reference = self._orders.document(order_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def claim(tx):
            snapshot = reference.get(transaction=tx)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict()
            if (data.get("access_hash") != access_hash
                    or data.get("status") != "paid"
                    or not data.get("credentials_submitted")
                    or data.get("job_id") is not None):
                return False
            tx.update(reference, {"job_id": "starting", "updated": self._clock()})
            return True

        return claim(transaction)

    def save_credentials(self, order_id: str, access_hash: str, encrypted: bytes) -> bool:
        reference = self._orders.document(order_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def save(tx):
            snapshot = reference.get(transaction=tx)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict()
            if (data.get("access_hash") != access_hash
                    or data.get("status") != "paid"
                    or data.get("credentials_submitted")
                    or data.get("job_id") is not None):
                return False
            tx.update(reference, {
                "credential_enc": encrypted,
                "credentials_submitted": True,
                "updated": self._clock(),
            })
            return True

        return save(transaction)

    def bind_job(self, order_id: str, job_id: str) -> bool:
        reference = self._orders.document(order_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def bind(tx):
            snapshot = reference.get(transaction=tx)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict()
            if (data.get("status") != "paid"
                    or not data.get("credentials_submitted")
                    or data.get("job_id") != "starting"):
                return False
            tx.update(reference, {"job_id": job_id, "updated": self._clock()})
            return True

        return bind(transaction)

    def release_execution(self, order_id: str) -> None:
        reference = self._orders.document(order_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def release(tx):
            snapshot = reference.get(transaction=tx)
            if snapshot.exists and snapshot.to_dict().get("job_id") == "starting":
                tx.update(reference, {"job_id": None, "updated": self._clock()})

        release(transaction)

    def set_status(self, order_id: str, status: str, *, payment_digest=None, note=None) -> None:
        transitions = {
            "awaiting_payment": {"review", "cancelled"},
            "payment_processing": {"review"},
            "paid": {"fulfilling", "cancelled"},
            "review": {"paid", "cancelled"},
            "fulfilling": {"completed", "review"},
            "completed": set(),
            "cancelled": set(),
        }
        reference = self._orders.document(order_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def update(tx):
            snapshot = reference.get(transaction=tx)
            if not snapshot.exists:
                raise ValueError("注文が見つかりません。")
            data = snapshot.to_dict()
            current = data.get("status")
            if status not in transitions.get(current, set()):
                raise ValueError("この注文状態からは変更できません。")
            if status == "fulfilling" and not data.get("credentials_submitted"):
                raise ValueError("購入者の引継ぎ情報が未入力です。")
            tx.update(reference, {
                "status": status,
                "payment_digest": payment_digest if payment_digest is not None else data.get("payment_digest"),
                "payment_note": (note or "")[:300],
                "updated": self._clock(),
            })

        update(transaction)


class FirestorePaymentRepository:
    def __init__(self, client, clock=time.time):
        self._client = client
        self._clock = clock
        self._orders = client.collection("orders")
        self._attempts = client.collection("payment_attempts")

    def import_legacy_attempt(
        self, payment_digest: str, order_id: str, state: str, created: float,
    ) -> bool:
        if not re.fullmatch(r"[0-9a-f]{64}", payment_digest or ""):
            raise ValueError("invalid legacy payment digest")
        state_map = {
            "attempted": "outcome_unknown",
            "pending": "pending",
            "paid": "paid",
            "review": "review",
        }
        imported_state = state_map.get(state, "review")
        reference = self._attempts.document(payment_digest)
        try:
            reference.create({
                "order_id": order_id,
                "state": imported_state,
                "created": float(created or self._clock()),
                "updated": self._clock(),
                "legacy_imported": True,
            })
            return True
        except AlreadyExists:
            existing = reference.get()
            existing_data = existing.to_dict() if existing.exists else {}
            if (not existing_data.get("legacy_imported")
                    or existing_data.get("order_id") != order_id):
                raise RuntimeError(
                    "existing Firestore payment attempt conflicts with create-only legacy import"
                )
            return False

    def claim_payment_attempt(self, order_id: str, payment_digest: str) -> bool:
        """Atomically reserve one payment link digest for one awaiting order."""
        if not re.fullmatch(r"[0-9a-f]{64}", payment_digest or ""):
            raise ValueError("payment digest must be a SHA-256 hex digest")
        order_ref = self._orders.document(order_id)
        attempt_ref = self._attempts.document(payment_digest)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def claim(tx):
            order = order_ref.get(transaction=tx)
            attempt = attempt_ref.get(transaction=tx)
            if (not order.exists or order.to_dict().get("status") != "awaiting_payment"
                    or attempt.exists):
                return False
            now = self._clock()
            tx.create(attempt_ref, {
                "order_id": order_id, "state": "attempted",
                "created": now, "updated": now,
            })
            tx.update(order_ref, {
                "status": "payment_processing", "payment_digest": payment_digest,
                "payment_note": "", "updated": now,
            })
            return True

        return claim(transaction)

    def transition_payment_attempt(
        self, order_id: str, payment_digest: str, state: str, note: str = "",
    ) -> None:
        allowed = {
            "attempted": {"pending", "paid", "review", "outcome_unknown", "rejected"},
            "pending": {"paid", "review", "outcome_unknown"},
            "outcome_unknown": {"paid", "review"},
            "review": {"paid"},
            "paid": set(),
        }
        if state not in allowed:
            raise ValueError("invalid payment attempt state")
        if not re.fullmatch(r"[0-9a-f]{64}", payment_digest or ""):
            raise ValueError("payment digest must be a SHA-256 hex digest")
        order_ref = self._orders.document(order_id)
        attempt_ref = self._attempts.document(payment_digest)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def transition(tx):
            order = order_ref.get(transaction=tx)
            attempt = attempt_ref.get(transaction=tx)
            if not order.exists or not attempt.exists:
                raise PaymentRepositoryError("決済試行が見つかりません。")
            attempt_data = attempt.to_dict()
            if attempt_data.get("order_id") != order_id:
                raise PaymentRepositoryError("決済リンクは別の注文で使用されています。")
            current = attempt_data.get("state")
            if state not in allowed.get(current, set()):
                raise PaymentRepositoryError("決済状態を安全に更新できません。")
            order_data = order.to_dict()
            if state == "paid" and order_data.get("status") not in {"payment_processing", "review"}:
                raise PaymentRepositoryError("注文状態が決済完了と一致しません。")
            now = self._clock()
            tx.update(attempt_ref, {
                "state": state, "note": (note or "")[:120],
                "updated": now,
            })
            order_state = (
                "paid" if state == "paid"
                else "awaiting_payment" if state == "rejected"
                else "review" if state in {"review", "outcome_unknown"}
                else "payment_processing"
            )
            tx.update(order_ref, {
                "status": order_state,
                "payment_digest": payment_digest,
                "payment_note": (note or "")[:300],
                "updated": now,
            })

        transition(transaction)

    def claim_payment(self, order_id: str) -> bool:
        reference = self._orders.document(order_id)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def claim(tx):
            snapshot = reference.get(transaction=tx)
            if not snapshot.exists or snapshot.to_dict().get("status") != "awaiting_payment":
                return False
            tx.update(reference, {"status": "payment_processing", "updated": self._clock()})
            return True

        return claim(transaction)

    def release_payment(self, order_id: str, *, review=False, note="") -> bool:
        if not review:
            raise PaymentRepositoryError(
                "Firestore決済はリンク単位の状態遷移が必要です。自動的に再受付へ戻しません。"
            )
        reference = self._orders.document(order_id)
        target = "review"
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def release(tx):
            snapshot = reference.get(transaction=tx)
            if not snapshot.exists or snapshot.to_dict().get("status") != "payment_processing":
                return False
            tx.update(reference, {
                "status": target, "payment_note": (note or "")[:300],
                "updated": self._clock(),
            })
            return True

        return release(transaction)

    def complete_payment(self, order_id: str, payment_digest: str) -> None:
        order_ref = self._orders.document(order_id)
        attempt_ref = self._attempts.document(payment_digest)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def complete(tx):
            order_snapshot = order_ref.get(transaction=tx)
            attempt_snapshot = attempt_ref.get(transaction=tx)
            if not order_snapshot.exists or order_snapshot.to_dict().get("status") != "payment_processing":
                raise PaymentRepositoryError(
                    "決済後の注文状態を確定できません。管理者による確認が必要です。"
                )
            if attempt_snapshot.exists:
                existing = attempt_snapshot.to_dict()
                if (existing.get("order_id") != order_id
                        or existing.get("state") not in {"attempted", "pending"}):
                    raise PaymentRepositoryError(
                        "決済リンクは既に使用済み、または確認が必要です。"
                    )
                tx.update(attempt_ref, {"state": "paid", "updated": self._clock()})
            else:
                tx.create(attempt_ref, {
                    "order_id": order_id, "state": "paid",
                    "created": self._clock(), "updated": self._clock(),
                })
            tx.update(order_ref, {
                "status": "paid", "payment_digest": payment_digest,
                "payment_note": "", "updated": self._clock(),
            })

        complete(transaction)
