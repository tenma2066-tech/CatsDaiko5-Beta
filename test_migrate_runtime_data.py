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

    def test_missing_tables_return_empty_lists(self):
        connection = sqlite3.connect(":memory:")
        self.assertEqual(load_orders(connection), [])
        self.assertEqual(load_payment_attempts(connection), [])
        connection.close()


if __name__ == "__main__":
    unittest.main()
