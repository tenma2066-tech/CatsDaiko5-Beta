"""Order creation and fulfillment-claim repository contracts."""
from __future__ import annotations

import time
from typing import Any, Callable, ContextManager, Protocol


class OrderRepository(Protocol):
    def create_order(self, values: dict[str, Any]) -> None: ...
    def claim_execution(self, order_id: str, access_hash: str) -> bool: ...
    def save_credentials(self, order_id: str, access_hash: str, encrypted: bytes) -> bool: ...
    def bind_job(self, order_id: str, job_id: str) -> bool: ...
    def release_execution(self, order_id: str) -> None: ...


class SQLiteOrderRepository:
    """SQLite adapter retaining the existing conditional-update semantics."""

    def __init__(self, database: Callable[[], ContextManager[Any]], clock=time.time):
        self._database = database
        self._clock = clock

    def create_order(self, values: dict[str, Any]) -> None:
        with self._database() as db:
            db.execute(
                """INSERT INTO orders
                   (id,access_hash,sku,title,amount,unit_price_yen,quantity,credential_enc,
                    operation_enc,service_type,job_id,credentials_submitted,status,created,updated)
                   VALUES(?,?,?,?,?,?,?,?,?,?,NULL,0,'awaiting_payment',?,?)""",
                (
                    values["id"], values["access_hash"], values["sku"], values["title"],
                    values["amount"], values["unit_price_yen"], values["quantity"],
                    values["credential_enc"], values.get("operation_enc"),
                    values["service_type"], values["now"], values["now"],
                ),
            )

    def claim_execution(self, order_id: str, access_hash: str) -> bool:
        with self._database() as db:
            changed = db.execute(
                """UPDATE orders SET job_id='starting',updated=?
                   WHERE id=? AND access_hash=? AND status='paid'
                     AND credentials_submitted=1 AND job_id IS NULL""",
                (self._clock(), order_id, access_hash),
            )
            return changed.rowcount == 1

    def save_credentials(self, order_id: str, access_hash: str, encrypted: bytes) -> bool:
        with self._database() as db:
            changed = db.execute(
                """UPDATE orders SET credential_enc=?,credentials_submitted=1,updated=?
                   WHERE id=? AND access_hash=? AND status='paid'
                     AND credentials_submitted=0 AND job_id IS NULL""",
                (encrypted, self._clock(), order_id, access_hash),
            )
            return changed.rowcount == 1

    def bind_job(self, order_id: str, job_id: str) -> bool:
        with self._database() as db:
            changed = db.execute(
                """UPDATE orders SET job_id=?,updated=?
                   WHERE id=? AND status='paid' AND credentials_submitted=1
                     AND job_id='starting'""",
                (job_id, self._clock(), order_id),
            )
            return changed.rowcount == 1

    def release_execution(self, order_id: str) -> None:
        with self._database() as db:
            db.execute(
                "UPDATE orders SET job_id=NULL,updated=? WHERE id=? AND job_id='starting'",
                (self._clock(), order_id),
            )
