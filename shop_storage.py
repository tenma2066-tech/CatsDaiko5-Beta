"""Explicit storage backend selection for storefront persistence.

The legacy backend preserves the current SQLite + optional GitHub snapshot behavior.
Firestore is opt-in and must be explicitly configured; it never falls back to SQLite.
"""
from __future__ import annotations

import os
from contextlib import contextmanager


class ShopStorageError(RuntimeError):
    """The selected storage backend is unavailable; never silently fall back."""


def selected_backend() -> str:
    backend = os.environ.get("SHOP_STORAGE_BACKEND", "legacy").strip().lower()
    if backend in ("legacy", "sqlite_github"):
        return "legacy"
    if backend == "firestore":
        return "firestore"
    raise ShopStorageError("SHOP_STORAGE_BACKENDの設定値が正しくありません。")


@contextmanager
def database(name: str, local_path):
    """Open the explicitly selected database backend.

    'legacy' deliberately delegates to the existing adapter, which uses local
    SQLite when GitHub runtime persistence is not configured and encrypted
    GitHub snapshots when it is configured. This preserves Render behavior.
    """
    backend = selected_backend()
    if backend == "firestore":
        raise ShopStorageError("Firestore選択時にSQLiteデータベースは開けません。")
    if backend != "legacy":
        raise ShopStorageError("指定された注文ストレージは利用できません。")

    import github_runtime_store

    try:
        with github_runtime_store.sqlite_database(name, local_path) as connection:
            yield connection
    except github_runtime_store.RuntimeStoreError as exc:
        raise ShopStorageError(str(exc)) from exc


def uses_remote_snapshot() -> bool:
    """Return whether legacy persistence is configured to use GitHub snapshots."""
    selected_backend()
    import github_runtime_store

    try:
        return github_runtime_store.enabled()
    except github_runtime_store.RuntimeStoreError as exc:
        raise ShopStorageError(str(exc)) from exc


def firestore_client():
    """Create the explicitly selected Firestore client; never use SQLite fallback."""
    if selected_backend() != "firestore":
        raise ShopStorageError("Firestore backend is not selected.")
    from google.cloud import firestore

    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("FIRESTORE_PROJECT_ID")
    try:
        return firestore.Client(project=project) if project else firestore.Client()
    except Exception as exc:
        raise ShopStorageError("Firestoreへ接続できません。") from exc
