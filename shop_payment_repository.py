"""Payment lifecycle repository contracts and the legacy SQLite adapter."""
from __future__ import annotations

import time
from typing import Callable, ContextManager, Protocol, Any


class PaymentRepositoryError(RuntimeError):
    """A payment state transition could not be completed safely."""


class PaymentRepository(Protocol):
    def claim_payment(self, order_id: str) -> bool: ...
    def release_payment(self, order_id: str, *, review: bool = False, note: str = "") -> bool: ...
    def complete_payment(self, order_id: str, payment_digest: str) -> None: ...


class SQLitePaymentRepository:
    """SQLite implementation preserving the current conditional state transitions."""

    def __init__(self, database: Callable[[], ContextManager[Any]], clock=time.time):
        self._database = database
        self._clock = clock

    def claim_payment(self, order_id: str) -> bool:
        with self._database() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                """UPDATE orders SET status='payment_processing',updated=?
                   WHERE id=? AND status='awaiting_payment'""",
                (self._clock(), order_id),
            )
            return changed.rowcount == 1

    def release_payment(self, order_id: str, *, review=False, note="") -> bool:
        target = "review" if review else "awaiting_payment"
        with self._database() as db:
            changed = db.execute(
                """UPDATE orders SET status=?,payment_note=?,updated=?
                   WHERE id=? AND status='payment_processing'""",
                (target, (note or "")[:300], self._clock(), order_id),
            )
            return changed.rowcount == 1

    def complete_payment(self, order_id: str, payment_digest: str) -> None:
        with self._database() as db:
            changed = db.execute(
                """UPDATE orders SET status='paid',payment_digest=?,payment_note='',
                   updated=? WHERE id=? AND status='payment_processing'""",
                (payment_digest, self._clock(), order_id),
            )
            if changed.rowcount != 1:
                raise PaymentRepositoryError(
                    "決済後の注文状態を確定できません。管理者による確認が必要です。"
                )
