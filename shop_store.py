"""Public checkout order storage for the isolated beta storefront."""
from __future__ import annotations
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

import shop_storage
from shop_payment_repository import PaymentRepositoryError, SQLitePaymentRepository
from shop_order_repository import SQLiteOrderRepository
from shop_firestore_repository import FirestoreOrderRepository, FirestorePaymentRepository
from shop_firestore_workflow import FirestoreWorkflowRepository

DB_PATH = Path(os.environ.get("SHOP_DB_PATH", "instance/shop.sqlite3"))
ORDER_ID_RE = re.compile(r"^[A-Z0-9]{10}$")
DEFAULT_CATALOG = [
    {"id": "character-specific", "title": "キャラ特定選択", "unit_price_yen": 50, "description": "代行するキャラ1体につき50円"},
    {"id": "other-service", "title": "その他の代行", "unit_price_yen": 25, "description": "代行1体につき25円"},
]
MAX_ORDER_QUANTITY = 100

class ShopError(Exception):
    pass

def _cipher():
    key = os.environ.get("PAYPAY_ENCRYPTION_KEY", "").strip()
    if not key:
        raise ShopError("ショップの暗号化キーが未設定です。管理者にお問い合わせください。")
    try:
        return Fernet(key.encode("ascii"))
    except Exception:
        raise ShopError("ショップの暗号化キー設定が正しくありません。") from None

@contextmanager
def _db():
    try:
        with shop_storage.database("shop.sqlite3", DB_PATH) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, access_hash TEXT NOT NULL, sku TEXT NOT NULL,
                title TEXT NOT NULL, amount INTEGER NOT NULL, credential_enc BLOB NOT NULL,
                status TEXT NOT NULL, payment_digest TEXT UNIQUE, payment_note TEXT,
                created REAL NOT NULL, updated REAL NOT NULL
            )""")
            columns = {row[1] for row in con.execute("PRAGMA table_info(orders)")}
            if "credentials_submitted" not in columns:
                con.execute("ALTER TABLE orders ADD COLUMN credentials_submitted INTEGER NOT NULL DEFAULT 0")
            if "quantity" not in columns:
                con.execute("ALTER TABLE orders ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
            if "unit_price_yen" not in columns:
                con.execute("ALTER TABLE orders ADD COLUMN unit_price_yen INTEGER NOT NULL DEFAULT 0")
                con.execute("UPDATE orders SET unit_price_yen=amount WHERE quantity=1")
            if "operation_enc" not in columns:
                con.execute("ALTER TABLE orders ADD COLUMN operation_enc BLOB")
            if "service_type" not in columns:
                con.execute("ALTER TABLE orders ADD COLUMN service_type TEXT NOT NULL DEFAULT 'other-service'")
            if "job_id" not in columns:
                con.execute("ALTER TABLE orders ADD COLUMN job_id TEXT")
            # Finish one-time schema migration before any caller starts an explicit transaction.
            con.commit()
            if not shop_storage.uses_remote_snapshot():
                os.chmod(DB_PATH, 0o600)
            yield con
    except shop_storage.ShopStorageError as exc:
        raise ShopError(str(exc)) from exc

_SHOP_BACKEND = shop_storage.selected_backend()
if _SHOP_BACKEND == "firestore":
    _firestore_client = shop_storage.firestore_client()
    _payment_repository = FirestorePaymentRepository(_firestore_client)
    _order_repository = FirestoreOrderRepository(_firestore_client)
    _workflow_repository = FirestoreWorkflowRepository(_firestore_client)
else:
    _firestore_client = None
    _payment_repository = SQLitePaymentRepository(_db)
    _order_repository = SQLiteOrderRepository(_db)
    _workflow_repository = None


def uses_firestore():
    return _SHOP_BACKEND == "firestore"


def claim_payment_attempt(order_id: str, payment_digest: str):
    if not uses_firestore():
        raise ShopError("リンク単位の決済管理はFirestore環境でのみ利用できます。")
    try:
        return _payment_repository.claim_payment_attempt(order_id, payment_digest)
    except PaymentRepositoryError as exc:
        raise ShopError(str(exc)) from exc


def transition_payment_attempt(order_id: str, payment_digest: str, state: str, note: str = ""):
    if not uses_firestore():
        raise ShopError("リンク単位の決済管理はFirestore環境でのみ利用できます。")
    try:
        return _payment_repository.transition_payment_attempt(
            order_id, payment_digest, state, note
        )
    except (PaymentRepositoryError, ValueError) as exc:
        raise ShopError(str(exc)) from exc


def enqueue_execution(order_id: str, access_token: str):
    if not uses_firestore():
        raise ShopError("Cloud Run実行キューはFirestore環境でのみ利用できます。")
    row = _row(order_id)
    access_hash = hashlib.sha256((access_token or "").encode()).hexdigest()
    if not row or not secrets.compare_digest(row["access_hash"], access_hash):
        raise ShopError("注文が見つかりません。")
    execution_id = secrets.token_hex(12)
    try:
        _workflow_repository.enqueue_execution(order_id, execution_id)
    except Exception as exc:
        raise ShopError("処理要求を安全に登録できませんでした。") from exc
    return execution_id


def publish_execution(execution_id: str):
    if not uses_firestore():
        raise ShopError("Cloud Run実行キューはFirestore環境でのみ利用できます。")
    required = {
        "project": os.environ.get("CLOUD_TASKS_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT"),
        "location": os.environ.get("CLOUD_TASKS_LOCATION", "asia-northeast1"),
        "queue": os.environ.get("CLOUD_TASKS_QUEUE"),
        "dispatcher_url": os.environ.get("CLOUD_RUN_DISPATCHER_URL"),
        "service_account": os.environ.get("CLOUD_TASKS_SERVICE_ACCOUNT"),
    }
    if not all(required.values()):
        # The transactional outbox remains pending and can be swept after config.
        return False
    try:
        from google.cloud import tasks_v2
        from shop_cloud_run_dispatcher import CloudTasksOutboxPublisher

        publisher = CloudTasksOutboxPublisher(
            tasks_v2.CloudTasksClient(), _workflow_repository,
            project=required["project"], location=required["location"],
            queue=required["queue"], dispatcher_url=required["dispatcher_url"],
            oidc_service_account=required["service_account"],
            audience=os.environ.get("CLOUD_RUN_DISPATCHER_AUDIENCE"),
        )
        return publisher.publish(execution_id)
    except Exception as exc:
        # Keep the outbox pending for deterministic recovery; never discard it.
        raise ShopError("処理要求は保存されましたが、実行キューへの登録を確認できません。") from exc


def execution_progress(order_id: str, access_token: str):
    if not uses_firestore():
        return None
    row = _row(order_id)
    expected = hashlib.sha256((access_token or "").encode()).hexdigest()
    if not row or not secrets.compare_digest(row["access_hash"], expected):
        return None
    execution_id = row.get("active_execution_id") or row.get("job_id")
    if not execution_id or execution_id in {"queued", "starting"}:
        return {"status": "queued", "job_status": "not_started"}
    execution = _workflow_repository.get_execution(execution_id)
    if not execution:
        return {"status": "queued", "job_status": "not_started"}
    state = execution.get("state")
    worker_status = execution.get("worker_status")
    if state == "completed":
        status = "done"
    elif state == "review":
        status = "error"
    elif state == "running" and worker_status in {"running", "done", "error"}:
        status = worker_status
    elif state == "queued":
        status = "queued"
    elif state == "starting":
        status = "running"
    else:
        status = "error"
    result = None
    encrypted_result = execution.get("result_enc")
    if encrypted_result:
        try:
            result = json.loads(_cipher().decrypt(encrypted_result))
        except Exception:
            result = None
    return {
        "status": status,
        "step": execution.get("step", 0),
        "total": 4,
        "queue_position": None,
        "slot": (int(execution["slot_id"].split("-")[1]) + 1) if execution.get("slot_id") else None,
        "max_concurrent_jobs": 4,
        "log": execution.get("log", []),
        "warn": execution.get("warn", []),
        "report": execution.get("report", []),
        "skipped": execution.get("skipped", []),
        "dry": execution.get("dry", False),
        "clone": execution.get("clone", False),
        "integrity_only": execution.get("integrity_only", False),
        "result": result,
        "error": execution.get("error") or ("管理者による確認が必要です。" if state == "review" else None),
        "created": execution.get("created"),
        "finished": execution.get("finished") or execution.get("worker_finished"),
        "outcome_unknown": execution.get("outcome_unknown", False),
        "job": execution_id,
    }


def cancel_queued_execution(order_id: str, access_token: str):
    if not uses_firestore():
        return False
    row = _row(order_id)
    expected = hashlib.sha256((access_token or "").encode()).hexdigest()
    if not row or not secrets.compare_digest(row["access_hash"], expected):
        return False
    execution_id = row.get("active_execution_id")
    if not execution_id:
        return False
    try:
        return _workflow_repository.cancel_queued_execution(order_id, execution_id)
    except Exception as exc:
        raise ShopError("待機中の処理を安全にキャンセルできませんでした。") from exc


def catalog():
    raw = os.environ.get("SHOP_CATALOG_JSON", "").strip()
    if raw:
        try:
            rows = json.loads(raw)
        except ValueError:
            raise ShopError("商品カタログ設定を読み取れません。") from None
    else:
        rows = DEFAULT_CATALOG
    if not isinstance(rows, list) or len(rows) > 100:
        raise ShopError("商品カタログ設定の形式が正しくありません。")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        sku, title = row.get("id"), row.get("title")
        price = row.get("unit_price_yen", row.get("price_yen"))
        if (not isinstance(sku, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", sku)
                or sku in seen or not isinstance(title, str) or not 1 <= len(title.strip()) <= 80
                or type(price) is not int or not 1 <= price <= 1000000):
            continue
        seen.add(sku)
        result.append({"id": sku, "title": title.strip(), "unit_price_yen": price,
                       "description": str(row.get("description") or "")[:300]})
    return result

def create_order(sku: str, quantity=1):
    if type(quantity) is not int or not 1 <= quantity <= MAX_ORDER_QUANTITY:
        raise ShopError("数量は1〜100体で指定してください。")
    items = {x["id"]: x for x in catalog()}
    item = items.get(sku)
    if not item:
        raise ShopError("商品が見つからないか、現在販売していません。")
    amount = item["unit_price_yen"] * quantity
    if amount > 1000000:
        raise ShopError("注文金額が上限を超えています。")
    order_id = secrets.token_hex(5).upper()
    access_token = secrets.token_urlsafe(32)
    credentials = _cipher().encrypt(json.dumps(
        {"transfer_code": "", "pin": ""}, ensure_ascii=False
    ).encode("utf-8"))
    now = time.time()
    _order_repository.create_order({
        "id": order_id, "access_hash": hashlib.sha256(access_token.encode()).hexdigest(),
        "sku": sku, "title": item["title"], "amount": amount,
        "unit_price_yen": item["unit_price_yen"], "quantity": quantity,
        "credential_enc": credentials, "operation_enc": None,
        "service_type": "other-service", "now": now,
    })
    return {"id": order_id, "access_token": access_token, "title": item["title"],
            "unit_price_yen": item["unit_price_yen"], "quantity": quantity,
            "amount": amount, "status": "awaiting_payment"}

def _selected_character_count(raw: str) -> int:
    if not isinstance(raw, str) or len(raw) > 20_000:
        raise ShopError("キャラ番号の入力形式が正しくありません。")
    value = unicodedata.normalize("NFKC", raw)
    value = re.sub(r"[~〜～ー−―‐–—]", "-", value)
    numbers = set()
    for token in (part for part in re.split(r"[\s,、，;；・]+", value) if part):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
        if not match:
            raise ShopError(f"キャラ番号「{token}」を読み取れません。")
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else first
        if first > last:
            first, last = last, first
        if first < 1 or last > 9999:
            raise ShopError("キャラ番号は1〜9999の範囲で指定してください。")
        numbers.update(range(first, last + 1))
        if len(numbers) > 2000:
            raise ShopError("キャラ番号は一度に2000体まで指定できます。")
    return len(numbers)


def create_operation_order(operation: dict, quantity: int):
    """Create an encrypted order; derive character-specific billing from the saved operation.

    The browser cannot choose the cheaper service category or override the
    quantity for character-specific orders. The only customer-entered billing
    quantity is used when no character numbers were selected.
    """
    if not isinstance(operation, dict) or len(json.dumps(operation, ensure_ascii=False)) > 100_000:
        raise ShopError("注文内容が大きすぎるか、形式が正しくありません。")
    for field in ("safe", "dry", "clone", "integrity_only"):
        if field in operation and type(operation[field]) is not bool:
            raise ShopError(f"注文オプション「{field}」の形式が正しくありません。")
    if operation.get("clone") and operation.get("integrity_only"):
        raise ShopError("保全チェックとアカウント複製は同時に選択できません。")
    character_count = _selected_character_count(operation.get("chars", ""))
    if character_count:
        service_type = "character-specific"
        quantity = character_count
        if quantity > MAX_ORDER_QUANTITY:
            raise ShopError("一度に注文できるのは100体までです。")
    else:
        service_type = "other-service"
        if type(quantity) is not int or not 1 <= quantity <= MAX_ORDER_QUANTITY:
            raise ShopError("代行体数は1〜100体で指定してください。")
    unit_price = 50 if service_type == "character-specific" else 25
    amount = unit_price * quantity
    order_id = secrets.token_hex(5).upper()
    access_token = secrets.token_urlsafe(32)
    credential_enc = _cipher().encrypt(json.dumps(
        {"transfer_code": "", "pin": ""}, ensure_ascii=False
    ).encode("utf-8"))
    operation_enc = _cipher().encrypt(json.dumps(
        operation, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8"))
    title = "キャラ特定選択" if service_type == "character-specific" else "その他の代行"
    now = time.time()
    _order_repository.create_order({
        "id": order_id, "access_hash": hashlib.sha256(access_token.encode()).hexdigest(),
        "sku": "original-" + service_type, "title": title, "amount": amount,
        "unit_price_yen": unit_price, "quantity": quantity,
        "credential_enc": credential_enc, "operation_enc": operation_enc,
        "service_type": service_type, "now": now,
    })
    return {"id": order_id, "access_token": access_token, "title": title,
            "unit_price_yen": unit_price, "quantity": quantity,
            "amount": amount, "status": "awaiting_payment"}


def operation_payload(order_id: str, access_token: str):
    row = _row(order_id)
    if not row or not secrets.compare_digest(
        row["access_hash"], hashlib.sha256((access_token or "").encode()).hexdigest()
    ):
        return None
    if not row.get("operation_enc"):
        raise ShopError("この注文には実行内容が保存されていません。管理者へお問い合わせください。")
    try:
        operation = json.loads(_cipher().decrypt(row["operation_enc"]))
    except Exception:
        raise ShopError("注文内容を安全に読み取れません。管理者へお問い合わせください。") from None
    if not isinstance(operation, dict):
        raise ShopError("注文内容の形式が正しくありません。")
    return operation


def operation_for_execution(order_id: str, access_token: str):
    row = _row(order_id)
    if not row or not secrets.compare_digest(
        row["access_hash"], hashlib.sha256((access_token or "").encode()).hexdigest()
    ):
        return None
    if row["status"] != "paid" or not row.get("credentials_submitted"):
        raise ShopError("決済確認と引継ぎ情報の入力が必要です。")
    operation = operation_payload(order_id, access_token)
    try:
        credentials = json.loads(_cipher().decrypt(row["credential_enc"]))
    except Exception:
        raise ShopError("引継ぎ情報を安全に読み取れません。管理者へお問い合わせください。") from None
    if bool(credentials.get("transfer_code")) != bool(credentials.get("pin")):
        raise ShopError("引継ぎ情報が正しくありません。")
    if (operation.get("clone") or operation.get("integrity_only")) and not credentials.get("transfer_code"):
        raise ShopError("複製・保全チェックには既存セーブの引継ぎコードと暗証番号が必要です。")
    operation["transfer"] = credentials.get("transfer_code", "")
    operation["confirm"] = credentials.get("pin", "")
    return operation


def claim_execution(order_id: str, access_token: str):
    row = _row(order_id)
    if not row or not secrets.compare_digest(
        row["access_hash"], hashlib.sha256((access_token or "").encode()).hexdigest()
    ):
        raise ShopError("注文が見つかりません。")
    if not _order_repository.claim_execution(
        order_id, hashlib.sha256((access_token or "").encode()).hexdigest()
    ):
        raise ShopError("この注文は既に処理開始済み、または状態が変わりました。")
    return True


def bind_job(order_id: str, job_id: str):
    if not _order_repository.bind_job(order_id, job_id):
        raise ShopError("処理開始状態を確定できません。管理者による確認が必要です。")


def release_execution(order_id: str):
    _order_repository.release_execution(order_id)


def claim_payment(order_id: str):
    try:
        return _payment_repository.claim_payment(order_id)
    except PaymentRepositoryError as exc:
        raise ShopError(str(exc)) from exc


def release_payment(order_id: str, *, review=False, note=""):
    try:
        return _payment_repository.release_payment(order_id, review=review, note=note)
    except PaymentRepositoryError as exc:
        raise ShopError(str(exc)) from exc


def complete_payment(order_id: str, payment_digest: str):
    try:
        return _payment_repository.complete_payment(order_id, payment_digest)
    except PaymentRepositoryError as exc:
        raise ShopError(str(exc)) from exc

def save_credentials(order_id: str, access_token: str, transfer_code: str, pin: str):
    transfer_code, pin = (transfer_code or "").strip(), (pin or "").strip()
    if bool(transfer_code) != bool(pin) or len(transfer_code) > 128 or len(pin) > 32:
        raise ShopError("引継ぎコードと暗証番号は、両方入力するか両方空欄にしてください。")
    if any(ord(ch) < 32 for ch in transfer_code + pin):
        raise ShopError("入力内容に使用できない文字が含まれています。")
    row = _row(order_id)
    if not row or not secrets.compare_digest(
        row["access_hash"], hashlib.sha256((access_token or "").encode()).hexdigest()
    ):
        raise ShopError("注文が見つかりません。")
    if row["status"] != "paid":
        raise ShopError("決済確認後に引継ぎ情報を入力できます。")
    encrypted = _cipher().encrypt(json.dumps(
        {"transfer_code": transfer_code, "pin": pin}, ensure_ascii=False
    ).encode("utf-8"))
    updated = _order_repository.save_credentials(
        order_id, hashlib.sha256((access_token or "").encode()).hexdigest(), encrypted
    )
    if not updated:
        raise ShopError("引継ぎ情報は既に送信済み、または注文状態が変わりました。注文画面を再読み込みしてください。")

def _row(order_id):
    if not ORDER_ID_RE.fullmatch(order_id or ""):
        return None
    return _order_repository.get_order(order_id)

def public_order(order_id: str, access_token: str):
    row = _row(order_id)
    if not row or not secrets.compare_digest(
        row["access_hash"], hashlib.sha256((access_token or "").encode()).hexdigest()
    ):
        return None
    return {"id": row["id"], "title": row["title"], "amount": row["amount"],
            "quantity": row.get("quantity", 1), "unit_price_yen": row.get("unit_price_yen", 0),
            "status": row["status"], "credentials_submitted": bool(row["credentials_submitted"]),
            "job_id": row.get("job_id") or row.get("active_execution_id"),
            "execution_id": row.get("active_execution_id"),
            "created": int(row["created"])}

def set_status(order_id: str, status: str, *, payment_digest=None, note=None):
    allowed = {"paid", "review", "fulfilling", "completed", "cancelled"}
    if status not in allowed:
        raise ShopError("不正な注文状態です。")
    try:
        _order_repository.set_status(
            order_id, status, payment_digest=payment_digest, note=note
        )
    except ValueError as exc:
        raise ShopError(str(exc)) from exc

def admin_orders(limit=100):
    rows = _order_repository.list_orders(limit)
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["credentials"] = json.loads(_cipher().decrypt(item.pop("credential_enc")))
        except (InvalidToken, ValueError, TypeError):
            item["credentials"] = {"error": "復号できません"}
            item.pop("credential_enc", None)
        item.pop("access_hash", None)
        result.append(item)
    return result

def get_order_for_admin(order_id):
    row = _row(order_id)
    if not row:
        return None
    try:
        credentials = json.loads(_cipher().decrypt(row["credential_enc"]))
    except Exception:
        credentials = {"error": "復号できません"}
    row.pop("credential_enc", None)
    row.pop("access_hash", None)
    row["credentials"] = credentials
    return row
