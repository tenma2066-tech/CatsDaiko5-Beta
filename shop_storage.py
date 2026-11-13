"""Explicit storage backend selection for storefront persistence.

The legacy backend preserves the current SQLite + optional GitHub snapshot behavior.
Firestore is intentionally unavailable until its transactional adapter is implemented.
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
        raise ShopStorageError(
            "Firestore注文ストレージはまだ実装されていません。"
            "安全のため別の保存先へ自動切替しません。"
        )
    raise ShopStorageError("SHOP_STORAGE_BACKENDの設定値が正しくありません。")


@contextmanager
def database(name: str, local_path):
    """Open the explicitly selected database backend.

    'legacy' deliberately delegates to the existing adapter, which uses local
    SQLite when GitHub runtime persistence is not configured and encrypted
    GitHub snapshots when it is configured. This preserves Render behavior.
    """
    backend = selected_backend()
    if backend != "legacy":
        # Defensive guard: adding a new backend requires an explicit adapter.
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
