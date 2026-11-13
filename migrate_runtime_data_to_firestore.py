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
    encrypted = github_runtime_store.read_blob(name)
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


def payment_digest_coverage(orders, attempts):
    """Count order payment digests that cannot be proven present in the old ledger."""
    ledger_digests = {
        str(row.get("digest") or "")
        for row in attempts
        if re.fullmatch(r"[0-9a-f]{64}", str(row.get("digest") or ""))
    }
    missing = set()
    invalid = 0
    for row in orders:
        digest = row.get("payment_digest")
        if digest in (None, ""):
            continue
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            invalid += 1
        elif digest not in ledger_digests:
            missing.add(digest)
    return {"missing": len(missing), "invalid": invalid}


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
            if (len(order_id) != 10 or not encrypted or len(access_hash) != 64):
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

        valid_attempts = []
        for row in attempts:
            digest = str(_row_value(row, "digest", ""))
            order_id = str(_row_value(row, "order_id", ""))
            if len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest):
                valid_attempts.append({
                    "digest": digest,
                    "order_id": order_id,
                    "state": str(_row_value(row, "state", "review")),
                    "created": float(_row_value(row, "created", 0) or 0),
                })

        digest_coverage = payment_digest_coverage(orders, attempts)
        print("Dry-run summary" if not args.apply else "Import summary")
        print(f"Shop order rows found: {len(orders)}")
        print(f"Valid order rows: {len(valid_orders)}")
        print(f"Invalid order rows skipped: {len(orders) - len(valid_orders)}")
        print(f"PayPay ledger rows found: {len(attempts)}")
        print(f"Valid payment attempts: {len(valid_attempts)}")
        print(f"Invalid payment rows skipped: {len(attempts) - len(valid_attempts)}")
        print(f"Order payment digests missing from ledger: {digest_coverage['missing']}")
        print(f"Invalid order payment digests: {digest_coverage['invalid']}")
        if args.apply and (digest_coverage["missing"] or digest_coverage["invalid"]):
            raise RuntimeError(
                "payment ledger does not cover every order payment digest; "
                "resolve the mismatch before importing"
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
