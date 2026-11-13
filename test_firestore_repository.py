"""Optional Firestore Emulator integration tests for transactional adapters."""
from __future__ import annotations

import os
import secrets
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from google.cloud import firestore

from shop_firestore_repository import (
    FirestoreOrderRepository,
    FirestorePaymentRepository,
)
from shop_payment_repository import PaymentRepositoryError
from shop_firestore_workflow import (
    FirestoreWorkflowRepository,
    WorkflowStateError,
    transfer_lock_id,
)


@unittest.skipUnless(
    os.environ.get("FIRESTORE_EMULATOR_HOST"),
    "FIRESTORE_EMULATOR_HOST is not configured",
)
class FirestoreRepositoryEmulatorTests(unittest.TestCase):
    def setUp(self):
        self.client = firestore.Client(
            project=os.environ.get("GOOGLE_CLOUD_PROJECT", "demo-catsdaiko-test")
        )
        self.orders = FirestoreOrderRepository(self.client)
        self.payments = FirestorePaymentRepository(self.client)
        self.order_id = secrets.token_hex(5).upper()
        self.digest = secrets.token_hex(32)
        self.access_hash = secrets.token_hex(32)
        self.orders.create_order({
            "id": self.order_id,
            "access_hash": self.access_hash,
            "sku": "emulator-test",
            "title": "emulator test",
            "amount": 25,
            "unit_price_yen": 25,
            "quantity": 1,
            "credential_enc": b"encrypted-empty-credentials",
            "operation_enc": None,
            "service_type": "other-service",
            "now": 1_800_000_000.0,
        })

    def tearDown(self):
        self.client.collection("orders").document(self.order_id).delete()
        self.client.collection("payment_attempts").document(self.digest).delete()

    def test_payment_credentials_and_execution_are_transactional(self):
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.assertFalse(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertEqual(self.orders.get_order(self.order_id)["status"], "paid")

        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer-credentials"
        ))
        self.assertFalse(self.orders.save_credentials(
            self.order_id, self.access_hash, b"second-write"
        ))

        barrier = threading.Barrier(3)

        def claim():
            barrier.wait()
            return self.orders.claim_execution(self.order_id, self.access_hash)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            barrier.wait()
            results = [future.result() for future in futures]

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)
        self.assertTrue(self.orders.bind_job(self.order_id, "emulator-job"))
        self.orders.set_status(self.order_id, "fulfilling")
        self.orders.set_status(self.order_id, "completed")
        self.assertEqual(self.orders.get_order(self.order_id)["status"], "completed")

    def test_credentials_and_outbox_are_created_atomically(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        execution_id = "exec_" + secrets.token_hex(8)
        result = workflow.submit_credentials_and_enqueue(
            self.order_id, self.access_hash, b"encrypted-transfer", execution_id
        )
        self.assertEqual(result["execution_id"], execution_id)
        order = self.orders.get_order(self.order_id)
        self.assertTrue(order["credentials_submitted"])
        self.assertEqual(order["active_execution_id"], execution_id)
        self.assertEqual(order["job_id"], "queued")
        self.assertEqual(workflow.get_execution(execution_id)["state"], "queued")
        self.assertEqual(workflow.get_outbox_item(execution_id)["state"], "pending")
        self.assertEqual(workflow.get_outbox_item(execution_id)["task_generation"], 0)
        with self.assertRaises(WorkflowStateError):
            workflow.submit_credentials_and_enqueue(
                self.order_id, self.access_hash, b"replacement", "exec_" + secrets.token_hex(8)
            )
        self.client.collection("task_outbox").document(execution_id).delete()
        self.client.collection("executions").document(execution_id).delete()

    def test_duplicate_payment_digest_is_rejected(self):
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        second_id = secrets.token_hex(5).upper()
        self.orders.create_order({
            "id": second_id,
            "access_hash": secrets.token_hex(32),
            "sku": "emulator-test",
            "title": "second emulator test",
            "amount": 25,
            "unit_price_yen": 25,
            "quantity": 1,
            "credential_enc": b"encrypted-empty-credentials",
            "operation_enc": None,
            "service_type": "other-service",
            "now": 1_800_000_001.0,
        })
        try:
            self.assertTrue(self.payments.claim_payment(second_id))
            with self.assertRaises(PaymentRepositoryError):
                self.payments.complete_payment(second_id, self.digest)
        finally:
            self.client.collection("orders").document(second_id).delete()

    def test_payment_attempt_digest_is_claimed_once_and_unknown_requires_review(self):
        digest = secrets.token_hex(32)
        self.assertTrue(self.payments.claim_payment_attempt(self.order_id, digest))
        self.assertFalse(self.payments.claim_payment_attempt(self.order_id, digest))
        self.payments.transition_payment_attempt(
            self.order_id, digest, "outcome_unknown", "accept_uncertain"
        )
        self.assertEqual(self.orders.get_order(self.order_id)["status"], "review")
        self.payments.transition_payment_attempt(
            self.order_id, digest, "paid", "admin_confirmed"
        )
        self.assertEqual(self.orders.get_order(self.order_id)["status"], "paid")
        self.client.collection("payment_attempts").document(digest).delete()

    def test_outbox_task_generation_advances_only_while_pending(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer"
        ))
        execution_id = "exec_" + secrets.token_hex(8)
        workflow.enqueue_execution(self.order_id, execution_id)
        self.assertEqual(
            workflow.advance_outbox_task_generation(execution_id, 0), 1
        )
        # Concurrent sweeps that observed generation 0 converge on generation 1.
        self.assertEqual(
            workflow.advance_outbox_task_generation(execution_id, 0), 1
        )
        self.assertEqual(
            workflow.advance_outbox_task_generation(execution_id, 1), 2
        )
        workflow.mark_task_enqueued(execution_id)
        self.assertEqual(
            [item["execution_id"] for item in workflow.list_pending_outbox()],
            [execution_id],
        )
        # task_enqueued also rotates after Cloud Tasks has deleted an exhausted task.
        self.assertEqual(
            workflow.advance_outbox_task_generation(execution_id, 2), 3
        )
        self.client.collection("task_outbox").document(execution_id).update({
            "state": "launching",
        })
        self.assertIsNone(
            workflow.advance_outbox_task_generation(execution_id, 3)
        )
        self.assertEqual(
            workflow.get_outbox_item(execution_id)["task_generation"], 3
        )
        self.client.collection("task_outbox").document(execution_id).delete()
        self.client.collection("executions").document(execution_id).delete()
        self.client.collection("orders").document(self.order_id).delete()

    def test_outbox_task_generation_exhaustion_moves_queued_order_to_review(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer"
        ))
        execution_id = "exec_" + secrets.token_hex(8)
        workflow.enqueue_execution(self.order_id, execution_id)
        self.assertEqual(workflow.advance_outbox_task_generation(execution_id, 0), 1)
        self.assertEqual(workflow.advance_outbox_task_generation(execution_id, 1), 2)
        self.assertEqual(workflow.advance_outbox_task_generation(execution_id, 2), 3)
        workflow.mark_task_enqueued(execution_id)
        self.assertIsNone(workflow.advance_outbox_task_generation(execution_id, 3))
        outbox = workflow.get_outbox_item(execution_id)
        execution = workflow.get_execution(execution_id)
        order = self.orders.get_order(self.order_id)
        self.assertEqual(outbox["state"], "review")
        self.assertEqual(outbox["error_code"], "cloud_tasks_retry_exhausted")
        self.assertEqual(execution["state"], "review")
        self.assertFalse(execution["outcome_unknown"])
        self.assertEqual(execution["error_code"], "cloud_tasks_retry_exhausted")
        self.assertEqual(order["status"], "review")
        self.assertEqual(workflow.list_pending_outbox(), [])
        self.client.collection("task_outbox").document(execution_id).delete()
        self.client.collection("executions").document(execution_id).delete()
        self.client.collection("orders").document(self.order_id).delete()

    def test_dispatch_accepts_task_delivery_before_publish_state_commit(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer"
        ))
        execution_id = "exec_" + secrets.token_hex(8)
        workflow.enqueue_execution(self.order_id, execution_id)
        # Simulate Cloud Tasks delivering immediately while outbox is still pending.
        claim = workflow.begin_dispatch(execution_id)
        self.assertEqual(claim["state"], "launch")
        self.assertEqual(workflow.get_outbox_item(execution_id)["state"], "launching")
        # Publisher's post-create update must not overwrite the dispatch transition.
        workflow.mark_task_enqueued(execution_id)
        self.assertEqual(workflow.get_outbox_item(execution_id)["state"], "launching")
        workflow.mark_dispatch_started(execution_id, {
            "operation_name": "operations/early-delivery",
            "execution_name": "early-delivery",
        })
        self.assertEqual(workflow.get_outbox_item(execution_id)["state"], "dispatched")
        self.client.collection("task_outbox").document(execution_id).delete()
        self.client.collection("executions").document(execution_id).delete()
        self.client.collection("worker_slots").document(claim["slot_id"]).delete()
        self.client.collection("orders").document(self.order_id).update({
            "status": "paid", "job_id": None, "active_execution_id": None,
        })

    def test_unknown_dispatch_after_operation_persist_but_without_execution_name(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer"
        ))
        execution_id = "exec_" + secrets.token_hex(8)
        workflow.enqueue_execution(self.order_id, execution_id)
        workflow.mark_task_enqueued(execution_id)
        workflow.begin_dispatch(execution_id)
        workflow.mark_dispatch_started(execution_id, {
            "operation_name": "operations/metadata-not-ready",
            "execution_name": None,
        })
        workflow.mark_dispatch_unknown(
            execution_id, "execution_name_missing_after_dispatch"
        )
        execution = workflow.get_execution(execution_id)
        outbox = workflow.get_outbox_item(execution_id)
        order = self.orders.get_order(self.order_id)
        self.assertEqual(execution["state"], "review")
        self.assertTrue(execution["outcome_unknown"])
        self.assertEqual(outbox["state"], "outcome_unknown")
        self.assertEqual(
            outbox["cloud_run_operation"], "operations/metadata-not-ready"
        )
        self.assertEqual(order["status"], "review")
        self.assertEqual(
            self.client.collection("worker_slots").document("slot-0").get().to_dict()["state"],
            "reserved",
        )
        self.client.collection("task_outbox").document(execution_id).delete()
        self.client.collection("executions").document(execution_id).delete()
        self.client.collection("worker_slots").document("slot-0").delete()
        self.client.collection("orders").document(self.order_id).delete()

    def test_outbox_single_launch_and_unknown_game_result_keeps_transfer_lock(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer"
        ))
        execution_id = "exec_" + secrets.token_hex(8)
        workflow.enqueue_execution(self.order_id, execution_id)
        self.assertEqual(len(workflow.list_pending_outbox()), 1)
        workflow.mark_task_enqueued(execution_id)
        # A second enqueue with the same order is rejected by the order guard.
        with self.assertRaises(WorkflowStateError):
            workflow.enqueue_execution(self.order_id, "exec_" + secrets.token_hex(8))

        # First task delivery wins the launch gate; duplicate deliveries are no-ops.
        claim = workflow.begin_dispatch(execution_id)
        self.assertEqual(claim["order_id"], self.order_id)
        self.assertIsNone(workflow.begin_dispatch(execution_id))
        workflow.mark_dispatch_started(execution_id, "operations/test-job")
        lock_digest = transfer_lock_id("transfer-code-example", b"x" * 32)
        self.assertTrue(workflow.claim_execution(
            self.order_id, execution_id, "worker-a", lock_digest, "slot-0"
        ))
        self.assertFalse(workflow.claim_execution(
            self.order_id, execution_id, "worker-b", lock_digest, "slot-1"
        ))
        workflow.finish_execution(
            self.order_id, execution_id, "worker-a",
            success=False, outcome_unknown=True, safe_error_code="game_timeout",
        )
        self.assertEqual(self.orders.get_order(self.order_id)["status"], "review")
        self.assertEqual(
            self.client.collection("transfer_locks").document(lock_digest).get().to_dict()["state"],
            "active",
        )
        self.assertEqual(
            self.client.collection("worker_slots").document("slot-0").get().to_dict()["state"],
            "released",
        )
        # Cleanup only test-owned docs.
        self.client.collection("task_outbox").document(execution_id).delete()
        self.client.collection("executions").document(execution_id).delete()
        self.client.collection("transfer_locks").document(lock_digest).delete()
        self.client.collection("worker_slots").document("slot-0").delete()

    def test_otp_session_is_encrypted_and_single_use(self):
        workflow = FirestoreWorkflowRepository(self.client)
        session_id = "otp_" + secrets.token_hex(12)
        encrypted = "fernet-ciphertext-not-plaintext-otp"
        workflow.save_otp_session(session_id, encrypted, time.time() + 300)
        stored = self.client.collection("admin_otp_sessions").document(session_id).get().to_dict()
        self.assertNotIn("plain-otp", stored["encrypted_state"])
        self.assertEqual(workflow.get_otp_session(session_id), encrypted)
        self.assertEqual(workflow.consume_otp_session(session_id), encrypted)
        self.assertIsNone(workflow.consume_otp_session(session_id))
        workflow.save_otp_session(session_id, "renewed-encrypted-state", time.time() + 300)
        self.assertEqual(workflow.get_otp_session(session_id), "renewed-encrypted-state")
        workflow.clear_otp_session(session_id)
        self.assertIsNone(workflow.get_otp_session(session_id))
        fixed_now = time.time()
        writer = FirestoreWorkflowRepository(self.client, clock=lambda: fixed_now)
        reader_after_expiry = FirestoreWorkflowRepository(
            self.client, clock=lambda: fixed_now + 2
        )
        writer.save_otp_session(session_id, "expired-state", fixed_now + 1)
        self.assertIsNone(reader_after_expiry.consume_otp_session(session_id))
        self.client.collection("admin_otp_sessions").document(session_id).delete()

    def test_otp_session_concurrent_consume_is_single_use(self):
        workflow = FirestoreWorkflowRepository(self.client)
        session_id = "otp_" + secrets.token_hex(12)
        workflow.save_otp_session(session_id, "encrypted-single-use", time.time() + 300)
        barrier = threading.Barrier(3)

        def consume():
            barrier.wait()
            return workflow.consume_otp_session(session_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(consume) for _ in range(2)]
            barrier.wait()
            results = [future.result() for future in futures]
        self.assertEqual(results.count("encrypted-single-use"), 1)
        self.assertEqual(results.count(None), 1)
        self.client.collection("admin_otp_sessions").document(session_id).delete()

    def test_execution_snapshot_splits_large_report_into_bounded_chunks(self):
        workflow = FirestoreWorkflowRepository(self.client)
        execution_id = "exec_" + secrets.token_hex(8)
        self.client.collection("executions").document(execution_id).set({
            "execution_id": execution_id, "state": "running", "owner": "worker-test",
        })
        try:
            report = ["large-report-entry-" + ("x" * 12000)] * 100
            workflow.persist_worker_snapshot(
                execution_id, "worker-test",
                {"status": "running", "step": 1, "report": report},
            )
            stored = workflow.get_execution(execution_id)
            self.assertEqual(stored["report"], report)
            self.assertGreater(stored["worker_snapshot_chunk_count"], 1)
            self.assertLessEqual(stored["worker_snapshot_chunk_count"], 32)
        finally:
            chunks = (
                self.client.collection("execution_logs").document(execution_id)
                .collection("chunks").stream()
            )
            for chunk in chunks:
                chunk.reference.delete()
            self.client.collection("execution_logs").document(execution_id).delete()
            self.client.collection("executions").document(execution_id).delete()

    def test_dispatch_defers_when_all_global_slots_are_occupied(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer"
        ))
        execution_id = "exec_" + secrets.token_hex(8)
        workflow.enqueue_execution(self.order_id, execution_id)
        workflow.mark_task_enqueued(execution_id)
        for index in range(4):
            self.client.collection("worker_slots").document(f"slot-{index}").set({
                "state": "active", "owner": "other-worker",
            })
        try:
            result = workflow.begin_dispatch(execution_id)
            self.assertEqual(result, {"state": "no_slot"})
            self.assertEqual(
                workflow.get_outbox_item(execution_id)["state"], "task_enqueued"
            )
        finally:
            for index in range(4):
                self.client.collection("worker_slots").document(f"slot-{index}").delete()
            self.client.collection("task_outbox").document(execution_id).delete()
            self.client.collection("executions").document(execution_id).delete()
            self.client.collection("orders").document(self.order_id).update({
                "status": "paid", "job_id": None, "active_execution_id": None,
            })

    def test_unknown_dispatch_launch_requires_review_and_keeps_reserved_slot(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer"
        ))
        execution_id = "exec_" + secrets.token_hex(8)
        workflow.enqueue_execution(self.order_id, execution_id)
        workflow.mark_task_enqueued(execution_id)
        claim = workflow.begin_dispatch(execution_id)
        self.assertEqual(claim["state"], "launch")
        workflow.mark_dispatch_unknown(
            execution_id, "dispatcher_launch_confirmation_timeout"
        )
        self.assertEqual(self.orders.get_order(self.order_id)["status"], "review")
        self.assertEqual(workflow.get_execution(execution_id)["state"], "review")
        self.assertEqual(
            workflow.get_outbox_item(execution_id)["state"], "outcome_unknown"
        )
        # Never release a slot just because the dispatcher lost its response.
        self.assertEqual(
            self.client.collection("worker_slots").document(claim["slot_id"]).get().to_dict()["state"],
            "reserved",
        )
        self.client.collection("task_outbox").document(execution_id).delete()
        self.client.collection("executions").document(execution_id).delete()
        self.client.collection("worker_slots").document(claim["slot_id"]).delete()
        self.client.collection("orders").document(self.order_id).update({
            "status": "awaiting_payment", "job_id": None,
            "active_execution_id": None, "credentials_submitted": False,
        })

    def test_reserved_slot_can_be_released_before_worker_claim(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer"
        ))
        execution_id = "exec_" + secrets.token_hex(8)
        workflow.enqueue_execution(self.order_id, execution_id)
        workflow.mark_task_enqueued(execution_id)
        claim = workflow.begin_dispatch(execution_id)
        self.assertEqual(claim["slot_id"], "slot-0")
        workflow.fail_reserved_execution(
            self.order_id, execution_id, "slot-0", "worker_claim_failed"
        )
        self.assertEqual(
            self.client.collection("worker_slots").document("slot-0").get().to_dict()["state"],
            "released",
        )
        self.assertEqual(self.orders.get_order(self.order_id)["status"], "review")
        self.client.collection("task_outbox").document(execution_id).delete()
        self.client.collection("executions").document(execution_id).delete()
        self.client.collection("worker_slots").document("slot-0").delete()

    def test_shop_store_uses_firestore_and_persists_order_outbox(self):
        import importlib
        from cryptography.fernet import Fernet

        import shop_store

        previous = {
            key: os.environ.get(key)
            for key in ("SHOP_STORAGE_BACKEND", "PAYPAY_ENCRYPTION_KEY",
                        "SHOP_CATALOG_JSON", "GOOGLE_CLOUD_PROJECT")
        }
        os.environ["SHOP_STORAGE_BACKEND"] = "firestore"
        os.environ["PAYPAY_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")
        os.environ["GOOGLE_CLOUD_PROJECT"] = os.environ.get(
            "GOOGLE_CLOUD_PROJECT", "demo-catsdaiko-test"
        )
        os.environ.pop("SHOP_CATALOG_JSON", None)
        shop_store = importlib.reload(shop_store)
        created = None
        try:
            operation = {
                "safe": True, "dry": False, "clone": False, "integrity_only": False,
                "items": ["XP"], "qty": {}, "chars": "", "cat_settings": {},
                "subs": [], "others": [],
            }
            created = shop_store.create_operation_order(operation, 1)
            digest = secrets.token_hex(32)
            self.assertTrue(shop_store.claim_payment_attempt(created["id"], digest))
            shop_store.transition_payment_attempt(created["id"], digest, "paid")
            shop_store.save_credentials(
                created["id"], created["access_token"], "", ""
            )
            execution_id = shop_store.enqueue_execution(
                created["id"], created["access_token"]
            )
            self.assertEqual(
                shop_store.execution_progress(
                    created["id"], created["access_token"]
                )["status"],
                "queued",
            )
            self.assertTrue(shop_store.cancel_queued_execution(
                created["id"], created["access_token"]
            ))
            self.assertEqual(
                shop_store.public_order(created["id"], created["access_token"])["status"],
                "cancelled",
            )
            self.client.collection("orders").document(created["id"]).delete()
            self.client.collection("payment_attempts").document(digest).delete()
            self.client.collection("executions").document(execution_id).delete()
            self.client.collection("task_outbox").document(execution_id).delete()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            importlib.reload(shop_store)

    def test_reconciler_releases_slot_but_keeps_lock_after_running_job_ends_unknown(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer"
        ))
        execution_id = "exec_" + secrets.token_hex(8)
        workflow.enqueue_execution(self.order_id, execution_id)
        workflow.mark_task_enqueued(execution_id)
        workflow.begin_dispatch(execution_id)
        workflow.mark_dispatch_started(execution_id, {
            "operation_name": "operations/reconcile-test",
            "execution_name": "job-run-unknown",
        })
        lock_digest = transfer_lock_id("transfer-code-example", b"z" * 32)
        self.assertTrue(workflow.claim_execution(
            self.order_id, execution_id, "job-run-unknown", lock_digest, "slot-0"
        ))
        self.assertTrue(workflow.reconcile_terminal_execution(
            self.order_id, execution_id, owner="job-run-unknown",
            cloud_run_execution_name="job-run-unknown",
        ))
        execution = workflow.get_execution(execution_id)
        self.assertEqual(execution["state"], "review")
        self.assertTrue(execution["outcome_unknown"])
        self.assertEqual(
            self.client.collection("worker_slots").document("slot-0").get().to_dict()["state"],
            "released",
        )
        self.assertEqual(
            self.client.collection("transfer_locks").document(lock_digest).get().to_dict()["state"],
            "active",
        )
        self.client.collection("task_outbox").document(execution_id).delete()
        self.client.collection("executions").document(execution_id).delete()
        self.client.collection("worker_slots").document("slot-0").delete()
        self.client.collection("transfer_locks").document(lock_digest).delete()

    def test_reconciler_releases_preclaim_reservation_without_account_lock(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        self.assertTrue(self.orders.save_credentials(
            self.order_id, self.access_hash, b"encrypted-transfer"
        ))
        execution_id = "exec_" + secrets.token_hex(8)
        workflow.enqueue_execution(self.order_id, execution_id)
        workflow.mark_task_enqueued(execution_id)
        workflow.begin_dispatch(execution_id)
        workflow.mark_dispatch_started(execution_id, {
            "operation_name": "operations/preclaim-test",
            "execution_name": "job-never-claimed",
        })
        self.assertTrue(workflow.reconcile_terminal_execution(
            self.order_id, execution_id, owner=None,
            cloud_run_execution_name="job-never-claimed",
        ))
        execution = workflow.get_execution(execution_id)
        self.assertEqual(execution["state"], "review")
        self.assertFalse(execution["outcome_unknown"])
        self.assertEqual(
            self.client.collection("worker_slots").document("slot-0").get().to_dict()["state"],
            "released",
        )
        self.client.collection("task_outbox").document(execution_id).delete()
        self.client.collection("executions").document(execution_id).delete()
        self.client.collection("worker_slots").document("slot-0").delete()

    def test_worker_snapshot_arrays_are_chunked_and_replaced_atomically(self):
        workflow = FirestoreWorkflowRepository(self.client)
        self.assertTrue(self.payments.claim_payment(self.order_id))
        self.payments.complete_payment(self.order_id, self.digest)
        execution_id = "exec_" + secrets.token_hex(8)
        workflow.submit_credentials_and_enqueue(
            self.order_id, self.access_hash, b"encrypted-transfer", execution_id
        )
        workflow.mark_task_enqueued(execution_id)
        workflow.begin_dispatch(execution_id)
        workflow.mark_dispatch_started(execution_id, {
            "operation_name": "operations/chunk-test",
            "execution_name": "job-chunk-test",
        })
        owner = "projects/demo/locations/asia-northeast1/jobs/test/executions/job-chunk-test"
        lock_digest = transfer_lock_id("chunk-test-transfer", b"x" * 32)
        self.assertTrue(workflow.claim_execution(
            self.order_id, execution_id, owner, lock_digest, "slot-0"
        ))

        self.client.collection("executions").document(execution_id).update({
            "log": ["legacy-inline"], "warn": ["legacy-warning"],
        })
        large_log = [f"entry-{index}-" + ("x" * 18000) for index in range(60)]
        workflow.persist_worker_snapshot(
            execution_id, owner,
            {"status": "running", "step": 2, "log": large_log,
             "warn": ["warning"], "report": [{"item": index} for index in range(5)],
             "skipped": ["unknown"], "dry": False, "clone": False,
             "integrity_only": False},
        )
        chunks_ref = (
            self.client.collection("execution_logs").document(execution_id)
            .collection("chunks")
        )
        first_chunks = list(chunks_ref.stream())
        self.assertGreater(len(first_chunks), 1)
        self.assertLessEqual(len(first_chunks), 32)
        restored = workflow.get_execution(execution_id)
        self.assertEqual(restored["log"], large_log)
        self.assertEqual(restored["warn"], ["warning"])
        self.assertEqual(restored["report"], [{"item": index} for index in range(5)])
        self.assertEqual(restored["skipped"], ["unknown"])
        first_revision = restored["worker_snapshot_revision"]

        workflow.persist_worker_snapshot(
            execution_id, owner,
            {"status": "running", "step": 3, "log": ["latest"],
             "warn": [], "report": [], "skipped": [],
             "dry": False, "clone": False, "integrity_only": False},
        )
        second_chunks = list(chunks_ref.stream())
        restored = workflow.get_execution(execution_id)
        self.assertEqual(len(second_chunks), 1)
        self.assertEqual(restored["worker_snapshot_revision"], first_revision + 1)
        self.assertEqual(restored["log"], ["latest"])
        self.assertEqual(restored["warn"], [])
        self.assertEqual(restored["report"], [])
        self.assertEqual(restored["skipped"], [])

        workflow.persist_worker_snapshot(
            execution_id, owner,
            {"status": "running", "step": 4, "log": [],
             "warn": [], "report": [], "skipped": [],
             "dry": False, "clone": False, "integrity_only": False},
        )
        self.assertEqual(list(chunks_ref.stream()), [])
        restored = workflow.get_execution(execution_id)
        self.assertEqual(restored["log"], [])
        self.assertEqual(restored["warn"], [])
        self.assertEqual(restored["report"], [])
        self.assertEqual(restored["skipped"], [])

        for chunk in second_chunks:
            chunk.reference.delete()
        self.client.collection("execution_logs").document(execution_id).delete()
        self.client.collection("task_outbox").document(execution_id).delete()
        self.client.collection("executions").document(execution_id).delete()
        self.client.collection("worker_slots").document("slot-0").delete()
        self.client.collection("transfer_locks").document(lock_digest).delete()
        self.client.collection("orders").document(self.order_id).delete()

    def test_otp_session_consume_is_atomic_and_single_use(self):
        workflow = FirestoreWorkflowRepository(self.client)
        session_id = "otp_" + secrets.token_hex(12)
        workflow.save_otp_session(session_id, "encrypted-otp-state", time.time() + 300)
        first = workflow.consume_otp_session(session_id)
        second = workflow.consume_otp_session(session_id)
        self.assertEqual(first, "encrypted-otp-state")
        self.assertIsNone(second)
        self.client.collection("admin_otp_sessions").document(session_id).delete()

    def test_legacy_import_is_create_only_and_marks_old_active_job_for_review(self):
        legacy_order_id = secrets.token_hex(5).upper()
        values = {
            "id": legacy_order_id,
            "access_hash": self.access_hash,
            "sku": "legacy",
            "title": "legacy order",
            "amount": 25,
            "unit_price_yen": 25,
            "quantity": 1,
            "credential_enc": b"encrypted-legacy-credentials",
            "operation_enc": b"encrypted-legacy-operation",
            "service_type": "other-service",
            "job_id": "old-in-memory-job",
            "credentials_submitted": True,
            "status": "completed",
            "payment_digest": None,
            "payment_note": "",
            "created": 1_800_000_000,
        }
        self.assertTrue(self.orders.import_legacy_order(values))
        self.assertFalse(self.orders.import_legacy_order(values))
        imported = self.orders.get_order(legacy_order_id)
        self.assertEqual(imported["status"], "review")
        self.assertIsNone(imported["job_id"])
        self.assertIsNone(imported["active_execution_id"])
        self.assertTrue(imported["legacy_imported"])
        self.client.collection("orders").document(legacy_order_id).delete()

    def test_legacy_paypay_attempt_import_maps_attempted_to_unknown(self):
        digest = secrets.token_hex(32)
        self.assertTrue(self.payments.import_legacy_attempt(
            digest, self.order_id, "attempted", 1_800_000_000
        ))
        self.assertFalse(self.payments.import_legacy_attempt(
            digest, self.order_id, "attempted", 1_800_000_000
        ))
        attempt = self.client.collection("payment_attempts").document(digest).get().to_dict()
        self.assertEqual(attempt["state"], "outcome_unknown")
        self.assertTrue(attempt["legacy_imported"])
        self.client.collection("payment_attempts").document(digest).delete()


if __name__ == "__main__":
    unittest.main()
