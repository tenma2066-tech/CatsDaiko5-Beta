"""Unit tests for Cloud Run worker input validation and secret handling."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from shop_cloud_run_worker import _build_arguments, _redact
from shop_firestore_workflow import transfer_lock_id


class CloudRunWorkerTests(unittest.TestCase):
    def setUp(self):
        self.fake_app = SimpleNamespace(
            ITEMS=["XP", "猫缶"],
            SUBS=["日本編 第1章クリア"],
            OTHERS=["施設全強化"],
            HIGH_RISK_ITEMS=["猫缶"],
            DB_OFFSET=1,
            engine=SimpleNamespace(QUANTITY_ITEM_LIMITS={"XP": 100, "猫缶": 100}),
            parse_cat_numbers=lambda value: ([int(value)] if value else [], None),
        )

    def test_saved_operation_is_validated_and_safe_mode_filters_risky_items(self):
        operation = {
            "safe": True, "dry": False, "clone": False, "integrity_only": False,
            "items": ["XP", "猫缶", "unknown"],
            "qty": {"XP": "4", "猫缶": "3"},
            "chars": "2",
            "cat_settings": {"2": {"base_level": 10}},
            "subs": ["日本編 第1章クリア"],
            "others": ["施設全強化"],
        }
        credentials = {"transfer_code": "transfer-123", "pin": "1234"}
        arguments, metadata = _build_arguments(self.fake_app, operation, credentials)
        self.assertEqual(arguments[2], ["XP"])
        self.assertEqual(arguments[5], {"XP": 4})
        self.assertEqual(arguments[6], [2])
        self.assertEqual(arguments[7], {1: {"base_level": 10}})
        self.assertEqual(metadata["skipped"], ["猫缶"])

    def test_integrity_mode_requires_existing_account_and_clone_discards_edits(self):
        with self.assertRaises(ValueError):
            _build_arguments(
                self.fake_app,
                {"integrity_only": True},
                {"transfer_code": "", "pin": ""},
            )
        arguments, metadata = _build_arguments(
            self.fake_app,
            {"clone": True, "dry": True, "items": ["XP"]},
            {"transfer_code": "transfer-123", "pin": "1234"},
        )
        self.assertEqual(arguments[2:5], ([], [], []))
        self.assertTrue(metadata["clone"])
        self.assertTrue(metadata["dry"])

    def test_execution_owner_requires_cloud_run_identity(self):
        from shop_cloud_run_worker import _execution_owner

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "identity is missing"):
                _execution_owner()
        expected = "projects/demo/locations/asia-northeast1/jobs/test/executions/run-1"
        with patch.dict(
            os.environ, {"CLOUD_RUN_EXECUTION": expected}, clear=True
        ):
            self.assertEqual(_execution_owner(), expected)

    def test_transfer_lock_is_hmac_and_redaction_removes_secrets(self):
        pepper = b"p" * 32
        first = transfer_lock_id("same-account-code", pepper)
        second = transfer_lock_id("same-account-code", pepper)
        other = transfer_lock_id("different-account-code", pepper)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(len(first), 64)
        self.assertEqual(
            _redact("transfer same-account-code and pin 1234",
                    ("same-account-code", "1234")),
            "transfer [REDACTED] and pin [REDACTED]",
        )


if __name__ == "__main__":
    unittest.main()
