"""Encrypted SQLite/blob persistence backed by a private GitHub repository branch.

This is Git-backed file storage, not a POSIX disk. Keep the Render service to one
instance/worker, serialize writes, and fail closed on API conflicts.
"""
from __future__ import annotations

import base64
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib import error, parse, request
import json

from cryptography.fernet import Fernet, InvalidToken


class RuntimeStoreError(RuntimeError):
    """Remote persistence is unavailable; callers must not pretend data was saved."""


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_GITHUB_WRITE_LOCK = threading.RLock()


def _config():
    repo = os.environ.get("RUNTIME_GITHUB_REPO", "").strip()
    # A token used only by the separate password store must not implicitly
    # switch local development to GitHub runtime persistence.
    if not repo:
        return None
    token = os.environ.get("RUNTIME_GITHUB_TOKEN", "").strip() or os.environ.get("AUTH_GITHUB_TOKEN", "").strip()
    branch = os.environ.get("RUNTIME_GITHUB_BRANCH", "runtime-data").strip()
    prefix = os.environ.get("RUNTIME_GITHUB_PREFIX", "runtime").strip("/")
    if not token or not branch or not prefix:
        raise RuntimeStoreError("GitHubランタイム保存の設定が不足しています。")
    if "/" not in repo or len(repo.split("/")) != 2:
        raise RuntimeStoreError("RUNTIME_GITHUB_REPO は owner/repository 形式にしてください。")
    return repo, token, branch, prefix


def enabled() -> bool:
    return _config() is not None


def _lock(name: str):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(name, threading.RLock())


def _cipher():
    key = os.environ.get("PAYPAY_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeStoreError("PAYPAY_ENCRYPTION_KEY が未設定です。")
    try:
        return Fernet(key.encode("ascii"))
    except Exception:
        raise RuntimeStoreError("PAYPAY_ENCRYPTION_KEY の形式が正しくありません。") from None


def _url(path: str):
    cfg = _config()
    if cfg is None:
        raise RuntimeStoreError("GitHubランタイム保存が設定されていません。")
    repo, _, branch, _ = cfg
    return "https://api.github.com/repos/" + repo + "/contents/" + parse.quote(path, safe="/"), branch


def _request(method: str, url: str, token: str, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "catsdaiko-beta-runtime-store",
        "Content-Type": "application/json",
    })
    try:
        with request.urlopen(req, timeout=20) as response:
            raw = response.read(12_000_001)
            if len(raw) > 12_000_000:
                raise RuntimeStoreError("GitHub保存応答が大きすぎます。")
            return json.loads(raw)
    except error.HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return None
        if exc.code in (409, 422) and method == "PUT":
            raise RuntimeStoreError("GitHub保存が競合しました。データを上書きせず処理を中止しました。") from exc
        if exc.code == 403 or exc.code == 429:
            raise RuntimeStoreError("GitHub APIの利用制限または権限を確認してください。") from exc
        raise RuntimeStoreError("GitHubランタイム保存に失敗しました。") from exc
    except (OSError, ValueError) as exc:
        if isinstance(exc, RuntimeStoreError):
            raise
        raise RuntimeStoreError("GitHubランタイム保存に接続できません。") from exc


def _read_remote(name: str):
    cfg = _config()
    if cfg is None:
        return None, None
    repo, token, branch, prefix = cfg
    path = prefix + "/" + name.lstrip("/")
    url, _ = _url(path)
    # Verify that this is still a private repository before reading customer data.
    base = "https://api.github.com/repos/" + repo
    metadata = _request("GET", base, token)
    if not isinstance(metadata, dict) or metadata.get("private") is not True:
        raise RuntimeStoreError("保存先は非公開GitHubリポジトリである必要があります。")
    response = _request("GET", url + "?" + parse.urlencode({"ref": branch}), token)
    if response is None:
        return None, None
    try:
        encoded = response.get("content", "").replace("\n", "")
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > 900_000:
            raise ValueError("oversized")
        sha = response.get("sha")
        if not isinstance(sha, str) or not sha:
            raise ValueError("missing sha")
        return raw, sha
    except (ValueError, TypeError) as exc:
        raise RuntimeStoreError("GitHubの保存ファイルを読み取れません。") from exc


def read_blob(name: str, local_path=None):
    """Read a blob from GitHub, migrating a pre-existing local file on first use."""
    if _config() is None:
        if local_path and Path(local_path).exists():
            return Path(local_path).read_bytes()
        return None
    legacy = None
    with _lock("blob:" + name):
        raw, _ = _read_remote(name)
        if raw is not None:
            return raw
        if local_path and Path(local_path).is_file():
            legacy = Path(local_path).read_bytes()
    if legacy is not None:
        try:
            # Do not hold the per-blob read lock while acquiring the global
            # write lock; all writes use global-lock -> blob-lock ordering.
            write_blob(name, legacy, create_only=True)
            return legacy
        except RuntimeStoreError:
            # Another writer may have initialized the remote file between our
            # first read and create attempt. Prefer that remote copy; never
            # replace it with the older local snapshot.
            current, _ = _read_remote(name)
            if current is not None:
                return current
            raise
    return None

def write_blob(name: str, raw: bytes, *, create_only: bool = False):
    """Atomically create/update one file using the current Git blob SHA."""
    cfg = _config()
    if cfg is None:
        raise RuntimeStoreError("GitHubランタイム保存が設定されていません。")
    if not isinstance(raw, bytes) or len(raw) > 900_000:
        raise RuntimeStoreError("GitHub Contents APIで安全に扱える保存サイズ（900KB）を超えています。")
    repo, token, branch, prefix = cfg
    path = prefix + "/" + name.lstrip("/")
    url, _ = _url(path)
    with _GITHUB_WRITE_LOCK, _lock("blob:" + name):
        _, sha = _read_remote(name)
        if create_only and sha is not None:
            raise RuntimeStoreError("GitHub側に既存データがあるため、ローカルデータの移行を中止しました。")
        payload = {
            "message": "Update encrypted beta runtime data",
            "branch": branch,
            "content": base64.b64encode(raw).decode("ascii"),
        }
        if sha:
            payload["sha"] = sha
        result = _request("PUT", url, token, payload)
        if not isinstance(result, dict) or not isinstance(result.get("content"), dict):
            raise RuntimeStoreError("GitHubへの保存結果を確認できません。")
        return result


@contextmanager
def sqlite_database(name: str, local_path):
    """Open an encrypted SQLite snapshot and push only when the DB actually changes."""
    if _config() is None:
        path = Path(local_path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=20)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA busy_timeout=20000")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return

    with _lock("sqlite:" + name):
        con = sqlite3.connect(":memory:", timeout=20)
        con.row_factory = sqlite3.Row
        remote_exists = False
        initial_snapshot = None
        try:
            encrypted, _ = _read_remote(name)
            if encrypted is not None:
                remote_exists = True
                try:
                    initial_snapshot = _cipher().decrypt(encrypted)
                    con.deserialize(initial_snapshot)
                    con.row_factory = sqlite3.Row
                except (InvalidToken, sqlite3.DatabaseError, ValueError) as exc:
                    raise RuntimeStoreError("GitHub上の暗号化DBを復元できません。") from exc
            elif local_path and Path(local_path).is_file():
                # Migrate an old local SQLite file as a raw database snapshot.
                legacy = sqlite3.connect(str(local_path))
                try:
                    initial_snapshot = legacy.serialize()
                    con.deserialize(initial_snapshot)
                    con.row_factory = sqlite3.Row
                finally:
                    legacy.close()

            con.execute("PRAGMA busy_timeout=20000")
            yield con
            con.commit()
            snapshot = con.serialize()
            if not remote_exists or snapshot != initial_snapshot:
                encrypted = _cipher().encrypt(snapshot)
                # First creation/migration is create-only: a concurrent remote
                # initialization must never be overwritten by an older local DB.
                write_blob(name, encrypted, create_only=not remote_exists)
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
