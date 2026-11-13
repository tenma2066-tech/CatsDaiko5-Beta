"""Read-only-by-default importer for encrypted GitHub runtime SQLite snapshots.

Run from Cloud Shell with Application Default Credentials. The importer never
prints order IDs, credentials, payment links, tokens, or decrypted payloads.
Without --apply it only reports counts. Existing Firestore documents are never
overwritten, making an interrupted import safe to rerun.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
from pathlib import Path

import github_runtime_store
from google.cloud import firestore

from shop_firestore_repository import (
    FirestoreOrderRepository,
    FirestorePaymentRepository,
)


def _snapshot_database(name: str):
    # Read only the encrypted remote snapshot. Never auto-upload an unencrypted
    # local SQLite file as a side effect of migration inspection.
    encrypted = github_runtime_store.read_blob_readonly(name)
    if encrypted is None:
        return None
    try:
        raw = github_runtime_store._cipher().decrypt(encrypted)
    except Exception as exc:
        raise RuntimeError("runtime snapshot decryption failed: " + name) from exc
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.deserialize(raw)
    except Exception:
        connection.close()
        raise RuntimeError("runtime snapshot is not a valid SQLite database: " + name)
    return connection


def _row_value(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def load_orders(connection):
    if connection is None:
        return []
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "orders" not in tables:
        return []
    return [dict(row) for row in connection.execute("SELECT * FROM orders")]


def load_payment_attempts(connection):
    if connection is None:
        return []
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "links" not in tables:
        return []
    return [dict(row) for row in connection.execute(
        "SELECT digest,order_id,state,created FROM links"
    )]


def require_shop_snapshot_for_apply(shop_db):
    """Require the expected order table and payment-digest column before import."""
    if shop_db is None:
        raise RuntimeError("encrypted shop snapshot is missing; refusing Firestore import")
    tables = {
        row[0] for row in shop_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "orders" not in tables:
        raise RuntimeError("shop orders table is missing; refusing Firestore import")
    columns = {
        row[1] for row in shop_db.execute("PRAGMA table_info(orders)")
    }
    required = {
        "id", "access_hash", "sku", "title", "amount",
        "credential_enc", "status", "payment_digest", "created",
    }
    if not required.issubset(columns):
        raise RuntimeError("shop orders schema is incomplete; refusing Firestore import")


def require_ledger_snapshot_for_apply(ledger_db):
    """Require the complete legacy PayPay ledger schema before a live import."""
    if ledger_db is None:
        raise RuntimeError(
            "encrypted PayPay ledger snapshot is missing; refusing Firestore import"
        )
    tables = {
        row[0] for row in ledger_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "links" not in tables:
        raise RuntimeError(
            "PayPay ledger links table is missing; refusing Firestore import"
        )
    columns = {
        row[1] for row in ledger_db.execute("PRAGMA table_info(links)")
    }
    if not {"digest", "order_id", "state", "created"}.issubset(columns):
        raise RuntimeError(
            "PayPay ledger links schema is incomplete; refusing Firestore import"
        )


def payment_digest_coverage(orders, attempts):
    """Prove each paid order's digest maps back to that exact order in the ledger."""
    order_ids = {
        str(row.get("id") or "") for row in orders
        if re.fullmatch(r"[A-Z0-9]{10}", str(row.get("id") or ""))
    }
    ledger_by_digest = {}
    ledger_by_order = {}
    invalid_attempts = 0
    orphaned_attempts = 0
    allowed_attempt_states = {"attempted", "pending", "paid", "review"}
    for row in attempts:
        digest = row.get("digest")
        order_id = row.get("order_id")
        state = row.get("state")
        if (not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(order_id, str)
                or not re.fullmatch(r"[A-Z0-9]{10}", order_id)
                or state not in allowed_attempt_states):
            invalid_attempts += 1
            continue
        if order_id not in order_ids:
            orphaned_attempts += 1
        ledger_by_digest.setdefault(digest, []).append((order_id, state))
        ledger_by_order.setdefault(order_id, []).append(digest)

    missing = set()
    invalid = 0
    mismatched = 0
    state_mismatch = 0
    paid_without_digest = 0
    for row in orders:
        order_id = str(row.get("id") or "")
        digest = row.get("payment_digest")
        status = row.get("status")
        if digest in (None, ""):
            if status in {"paid", "fulfilling", "completed"}:
                paid_without_digest += 1
            continue
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            invalid += 1
            continue
        entries = ledger_by_digest.get(digest)
        if not entries:
            missing.add(digest)
            continue
        matching = [state for ledger_order, state in entries if ledger_order == order_id]
        if not matching:
            mismatched += 1
        elif status in {"paid", "fulfilling", "completed"} and "paid" not in matching:
            state_mismatch += 1

    duplicate_digest_records = sum(1 for entries in ledger_by_digest.values() if len(entries) > 1)
    duplicate_order_records = sum(1 for digests in ledger_by_order.values() if len(digests) > 1)
    return {
        "missing": len(missing),
        "invalid": invalid,
        "mismatched": mismatched,
        "state_mismatch": state_mismatch,
        "paid_without_digest": paid_without_digest,
        "invalid_attempts": invalid_attempts,
        "orphaned_attempts": orphaned_attempts,
        "duplicate_digest_records": duplicate_digest_records,
        "duplicate_order_records": duplicate_order_records,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="write create-only documents to Firestore")
    parser.add_argument("--shop-snapshot", default="shop.sqlite3")
    parser.add_argument("--ledger-snapshot", default="paypay-ledger.sqlite3")
    args = parser.parse_args()

    if not github_runtime_store.enabled():
        raise RuntimeError("GitHub runtime-data configuration is required")
    shop_db = _snapshot_database(args.shop_snapshot)
    ledger_db = _snapshot_database(args.ledger_snapshot)
    if shop_db is None:
        raise RuntimeError("encrypted shop runtime snapshot was not found")
    try:
        orders = load_orders(shop_db)
        attempts = load_payment_attempts(ledger_db)
        valid_orders = []
        for row in orders:
            order_id = str(_row_value(row, "id", ""))
            encrypted = _row_value(row, "credential_enc")
            access_hash = str(_row_value(row, "access_hash", ""))
            if (not re.fullmatch(r"[A-Z0-9]{10}", order_id)
                    or not isinstance(encrypted, (bytes, bytearray)) or not encrypted
                    or not re.fullmatch(r"[0-9a-f]{64}", access_hash)):
                continue
            valid_orders.append({
                "id": order_id,
                "access_hash": access_hash,
                "sku": _row_value(row, "sku", ""),
                "title": _row_value(row, "title", ""),
                "amount": _row_value(row, "amount", 0),
                "unit_price_yen": _row_value(row, "unit_price_yen", _row_value(row, "amount", 0)),
                "quantity": _row_value(row, "quantity", 1),
                "credential_enc": encrypted,
                "operation_enc": _row_value(row, "operation_enc"),
                "service_type": _row_value(row, "service_type", "other-service"),
                "job_id": _row_value(row, "job_id"),
                "credentials_submitted": _row_value(row, "credentials_submitted", 0),
                "status": _row_value(row, "status", "review"),
                "payment_digest": _row_value(row, "payment_digest"),
                "payment_note": _row_value(row, "payment_note", ""),
                "created": _row_value(row, "created", 0),
            })

        valid_order_ids = {row["id"] for row in valid_orders}
        valid_attempts = []
        for row in attempts:
            digest = str(_row_value(row, "digest", ""))
            order_id = str(_row_value(row, "order_id", ""))
            state = str(_row_value(row, "state", ""))
            if (re.fullmatch(r"[0-9a-f]{64}", digest)
                    and re.fullmatch(r"[A-Z0-9]{10}", order_id)
                    and order_id in valid_order_ids
                    and state in {"attempted", "pending", "paid", "review"}):
                valid_attempts.append({
                    "digest": digest,
                    "order_id": order_id,
                    "state": state,
                    "created": float(_row_value(row, "created", 0) or 0),
                })

        digest_coverage = payment_digest_coverage(orders, attempts)
        shop_snapshot_error = ledger_snapshot_error = None
        try:
            require_shop_snapshot_for_apply(shop_db)
        except RuntimeError as exc:
            shop_snapshot_error = str(exc)
        try:
            require_ledger_snapshot_for_apply(ledger_db)
        except RuntimeError as exc:
            ledger_snapshot_error = str(exc)
        print("Dry-run summary" if not args.apply else "Import summary")
        print("Shop order snapshot schema: " + ("OK" if not shop_snapshot_error else "FAILED"))
        print("PayPay ledger snapshot schema: " + ("OK" if not ledger_snapshot_error else "FAILED"))
        print(f"Shop order rows found: {len(orders)}")
        print(f"Valid order rows: {len(valid_orders)}")
        print(f"Invalid order rows skipped: {len(orders) - len(valid_orders)}")
        print(f"PayPay ledger rows found: {len(attempts)}")
        print(f"Valid payment attempts: {len(valid_attempts)}")
        print(f"Invalid payment rows skipped: {len(attempts) - len(valid_attempts)}")
        print(f"Order payment digests missing from ledger: {digest_coverage['missing']}")
        print(f"Invalid order payment digests: {digest_coverage['invalid']}")
        print(f"Paid orders without a payment digest: {digest_coverage['paid_without_digest']}")
        print(f"Payment digest/order mismatches: {digest_coverage['mismatched']}")
        print(f"Paid-order/payment-state mismatches: {digest_coverage['state_mismatch']}")
        print(f"Invalid payment ledger rows: {digest_coverage['invalid_attempts']}")
        print(f"Orphaned payment ledger rows: {digest_coverage['orphaned_attempts']}")
        print(f"Duplicate payment digest records: {digest_coverage['duplicate_digest_records']}")
        print(f"Orders with multiple payment attempts: {digest_coverage['duplicate_order_records']}")
        if args.apply:
            if shop_snapshot_error:
                raise RuntimeError(shop_snapshot_error)
            if ledger_snapshot_error:
                raise RuntimeError(ledger_snapshot_error)
            if len(valid_orders) != len(orders):
                raise RuntimeError(
                    "one or more order rows are invalid; refusing partial Firestore import"
                )
            if len(valid_attempts) != len(attempts):
                raise RuntimeError(
                    "one or more payment ledger rows are invalid or orphaned; "
                    "refusing partial Firestore import"
                )
            if any(digest_coverage.values()):
                raise RuntimeError(
                    "payment ledger/order integrity checks failed; "
                    "resolve every mismatch before importing"
                )
        if not args.apply:
            print("No Firestore writes performed. Re-run with --apply only after reviewing backups.")
            return 0

        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("FIRESTORE_PROJECT_ID")
        client = firestore.Client(project=project) if project else firestore.Client()
        order_repository = FirestoreOrderRepository(client)
        payment_repository = FirestorePaymentRepository(client)
        imported_orders = skipped_orders = 0
        for values in valid_orders:
            if order_repository.import_legacy_order(values):
                imported_orders += 1
            else:
                skipped_orders += 1
        imported_attempts = skipped_attempts = 0
        for values in valid_attempts:
            if payment_repository.import_legacy_attempt(
                values["digest"], values["order_id"],
                values["state"], values["created"],
            ):
                imported_attempts += 1
            else:
                skipped_attempts += 1
        print(f"Orders imported: {imported_orders}; already existed: {skipped_orders}")
        print(f"Payment attempts imported: {imported_attempts}; already existed: {skipped_attempts}")
        print("No existing Firestore documents were overwritten.")
        return 0
    finally:
        if shop_db is not None:
            shop_db.close()
        if ledger_db is not None:
            ledger_db.close()


if __name__ == "__main__":
    raise SystemExit(main())
