"""Tests for the read-only runtime snapshot migration parser."""
from __future__ import annotations

import sqlite3
import unittest

from migrate_runtime_data_to_firestore import load_orders, load_payment_attempts


class RuntimeMigrationParserTests(unittest.TestCase):
    def test_reads_order_and_paypay_ledger_rows_without_mutating_snapshot(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("""
            CREATE TABLE orders (
                id TEXT, access_hash TEXT, sku TEXT, title TEXT,
                amount INTEGER, credential_enc BLOB, status TEXT,
                created REAL, updated REAL
            )
        """)
        connection.execute("""
            INSERT INTO orders VALUES (
                '0123456789', 'a' || replace(hex(zeroblob(31)), '00', 'a'),
                'sku', 'test', 25, X'0102', 'paid', 1800000000, 1800000001
            )
        """)
        connection.execute("""
            CREATE TABLE links (
                digest TEXT, order_id TEXT, state TEXT, created REAL
            )
        """)
        connection.execute("""
            INSERT INTO links VALUES (
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                '0123456789', 'attempted', 1800000000
            )
        """)
        orders = load_orders(connection)
        attempts = load_payment_attempts(connection)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["id"], "0123456789")
        self.assertEqual(orders[0]["credential_enc"], b"\x01\x02")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["state"], "attempted")
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1
        )
        connection.close()

    def test_payment_digest_coverage_blocks_missing_or_invalid_ledger_records(self):
        orders = [
            {"id": "0000000001", "payment_digest": "a" * 64},
            {"id": "0000000002", "payment_digest": "b" * 64},
            {"id": "0000000003", "payment_digest": "not-a-digest"},
            {"id": "0000000004", "payment_digest": None},
        ]
        attempts = [{"digest": "a" * 64, "order_id": "0000000001"}]
        from migrate_runtime_data_to_firestore import payment_digest_coverage

        self.assertEqual(
            payment_digest_coverage(orders, attempts),
            {
                "missing": 1, "invalid": 1, "mismatched": 0,
                "state_mismatch": 0, "paid_without_digest": 0,
                "invalid_attempts": 0, "orphaned_attempts": 0,
                "duplicate_digest_records": 0, "duplicate_order_records": 0,
            },
        )

    def test_payment_digest_coverage_accepts_complete_ledger(self):
        from migrate_runtime_data_to_firestore import payment_digest_coverage

        digest = "c" * 64
        self.assertEqual(
            payment_digest_coverage(
                [{"id": "0000000001", "status": "paid", "payment_digest": digest}],
                [{"digest": digest, "order_id": "0000000001", "state": "paid"}],
            ),
            {
                "missing": 0, "invalid": 0, "mismatched": 0,
                "state_mismatch": 0, "paid_without_digest": 0,
                "invalid_attempts": 0, "orphaned_attempts": 0,
                "duplicate_digest_records": 0, "duplicate_order_records": 0,
            },
        )

    def test_paid_order_without_digest_blocks_migration(self):
        from migrate_runtime_data_to_firestore import payment_digest_coverage

        result = payment_digest_coverage(
            [{"id": "0000000001", "status": "paid", "payment_digest": None}],
            [],
        )
        self.assertEqual(result["paid_without_digest"], 1)

    def test_digest_must_belong_to_the_same_order(self):
        from migrate_runtime_data_to_firestore import payment_digest_coverage

        digest = "d" * 64
        result = payment_digest_coverage(
            [{"id": "0000000001", "status": "paid", "payment_digest": digest}],
            [{"digest": digest, "order_id": "0000000002", "state": "paid"}],
        )
        self.assertEqual(result["mismatched"], 1)

    def test_invalid_and_orphaned_ledger_rows_are_reported(self):
        from migrate_runtime_data_to_firestore import payment_digest_coverage

        result = payment_digest_coverage(
            [{"id": "0000000001", "status": "awaiting_payment"}],
            [
                {"digest": "not-a-digest", "order_id": "0000000001", "state": "paid"},
                {"digest": "e" * 64, "order_id": "0000000002", "state": "paid"},
            ],
        )
        self.assertEqual(result["invalid_attempts"], 1)
        self.assertEqual(result["orphaned_attempts"], 1)

    def test_paid_order_requires_paid_ledger_attempt_state(self):
        from migrate_runtime_data_to_firestore import payment_digest_coverage

        digest = "f" * 64
        result = payment_digest_coverage(
            [{"id": "0000000001", "status": "paid", "payment_digest": digest}],
            [{"digest": digest, "order_id": "0000000001", "state": "attempted"}],
        )
        self.assertEqual(result["state_mismatch"], 1)

    def test_duplicate_digest_and_order_attempt_records_are_rejected(self):
        from migrate_runtime_data_to_firestore import payment_digest_coverage

        digest = "1" * 64
        result = payment_digest_coverage(
            [
                {"id": "0000000001", "status": "paid", "payment_digest": digest},
                {"id": "0000000002", "status": "review", "payment_digest": None},
            ],
            [
                {"digest": digest, "order_id": "0000000001", "state": "paid"},
                {"digest": digest, "order_id": "0000000002", "state": "review"},
            ],
        )
        self.assertEqual(result["duplicate_digest_records"], 1)

    def test_unknown_payment_attempt_state_is_invalid(self):
        from migrate_runtime_data_to_firestore import payment_digest_coverage

        result = payment_digest_coverage(
            [{"id": "0000000001", "status": "review"}],
            [{"digest": "2" * 64, "order_id": "0000000001", "state": "mystery"}],
        )
        self.assertEqual(result["invalid_attempts"], 1)

    def test_apply_requires_complete_global_paypay_ledger_snapshot(self):
        from migrate_runtime_data_to_firestore import require_ledger_snapshot_for_apply

        with self.assertRaisesRegex(RuntimeError, "ledger snapshot is missing"):
            require_ledger_snapshot_for_apply(None)
        connection = sqlite3.connect(":memory:")
        try:
            with self.assertRaisesRegex(RuntimeError, "links table is missing"):
                require_ledger_snapshot_for_apply(connection)
            connection.execute(
                "CREATE TABLE links (digest TEXT, order_id TEXT, state TEXT, created REAL)"
            )
            require_ledger_snapshot_for_apply(connection)
            connection.execute("DROP TABLE links")
            connection.execute(
                "CREATE TABLE links (digest TEXT, order_id TEXT, state TEXT)"
            )
            with self.assertRaisesRegex(RuntimeError, "schema is incomplete"):
                require_ledger_snapshot_for_apply(connection)
        finally:
            connection.close()

    def test_missing_tables_return_empty_lists(self):
        connection = sqlite3.connect(":memory:")
        self.assertEqual(load_orders(connection), [])
        self.assertEqual(load_payment_attempts(connection), [])
        connection.close()


if __name__ == "__main__":
    unittest.main()
