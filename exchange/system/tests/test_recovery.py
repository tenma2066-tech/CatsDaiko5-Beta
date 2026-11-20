"""換金処理の障害復旧。外部 API はモック、台帳はケースごとの一時ファイル。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot
import kyash_api
import kyash_bridge
import store


class SimulatedStop(BaseException):
    pass


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name in ("ORDERS_FILE", "ACCOUNTS_FILE", "CONFIG_FILE", "USED_FILE", "HOLDS_FILE", "DETECT_FILE"):
            path = Path(tmp.name, Path(getattr(store, name)).name)
            self.enterContext(patch.object(store, name, str(path)))
        self.enterContext(patch.object(bot, "notify_admin", new_callable=AsyncMock))
        self.enterContext(patch.object(bot, "log_event", new_callable=AsyncMock))
        self.enterContext(patch.object(bot, "log_trade", new_callable=AsyncMock))
        self.enterContext(patch.object(bot, "_dm", new_callable=AsyncMock))
        self.tell = AsyncMock()

    def paypay_order(self):
        return store.get_or_create_order("p2k", 1, "user", 1000, 950, 50, 5,
                                        receipt_key="receipt", paypay_in={"order_id": "provider-id"},
                                        dest={"id": "42"})

    async def test_paypay_crash_after_order_write_reuses_order_and_sends_once(self):
        store.update_config(daily_limit_jpy=1000)
        hold = store.add_hold("receipt", user_id=1, amount=1000)
        with patch.object(store, "binding", return_value={"id": "42"}), \
             patch.object(bot.kyash, "send", return_value={"status": "sent"}) as send:
            with patch.object(store, "confirm_link", side_effect=SimulatedStop), self.assertRaises(SimulatedStop):
                await bot.settle_p2k(hold, 1000, "provider-id", self.tell, via="received")
            send.assert_not_called()
            await bot.settle_p2k(hold, 1000, "provider-id", self.tell, via="hold_history")
            await bot.settle_p2k(hold, 1000, "provider-id", self.tell, via="hold_history")
            send.assert_called_once()
            self.assertEqual([(o["id"], o["status"]) for o in store.recent_orders()], [("PK00001", "SENT")])

    def test_legacy_paypay_order_without_receipt_key(self):
        order, _ = self.paypay_order()
        data = store.load_orders()
        del data["orders"][order["id"]]["receipt_key"]
        store.save_orders(data)
        recovered, created = self.paypay_order()
        self.assertFalse(created)
        self.assertEqual(recovered["id"], order["id"])

    async def test_kyash_crash_during_receipt_requires_confirmation(self):
        interaction = MagicMock()
        interaction.user.id = 1
        interaction.followup.send = AsyncMock()
        page = {"ok": True, "uuid": "incoming-id", "amount": 1000, "sender_user_name": "user"}

        def receive(*args):
            self.assertEqual(store.recent_orders()[0]["status"], "RECEIVING")
            raise SimulatedStop

        with patch.object(bot, "_entry_checks", return_value=True), \
             patch.object(bot.aml, "check_kyash_page", return_value={"action": "allow"}), \
             patch.object(bot.kyash, "inspect_link", return_value=page), \
             patch.object(bot, "capacity", return_value={"ok": True}), \
             patch.object(bot.kyash, "claim_and_identify", side_effect=receive) as claim:
            with self.assertRaises(SimulatedStop):
                await bot.process_k2p(interaction, "test")
            await bot.recover_stuck_orders()
            await bot.process_k2p(interaction, "test")
            claim.assert_called_once()
        order = store.recent_orders()[0]
        self.assertEqual(order["hold_code"], "RECEIPT_REVIEW")
        self.assertEqual((await bot.payout(order["id"]))["status"], "review")

    async def test_gateway_timeout_blocks_payout_retry(self):
        order, _ = self.paypay_order()

        def timeout(*args, request_id):
            self.assertEqual(store.get_order(order["id"])["payout_request_id"], request_id)
            raise kyash_api.KyashError(504)

        with patch.object(kyash_api, "send_money", side_effect=timeout) as send:
            self.assertEqual((await bot.payout(order["id"], self.tell))["status"], "unknown")
            self.assertEqual((await bot.payout(order["id"], self.tell))["status"], "busy")
            send.assert_called_once()
        self.assertEqual(store.get_order(order["id"])["status"], "UNCERTAIN")

    def test_send_and_link_preserve_request_ids_on_retry(self):
        request_id = str(uuid4())
        with patch.object(kyash_api, "_request", return_value=[]) as request:
            for _ in range(2):
                kyash_api.send_money(950, 42, "user", token="test", request_id=request_id)
            first, second = [c.kwargs["json_body"] for c in request.call_args_list]
            self.assertEqual(first, second)
            self.assertEqual(first["transaction"][0]["uuidClient"], request_id)
            self.assertNotEqual(first["uuidClient"], request_id)
        with patch.object(kyash_api, "_request", return_value={}) as request:
            kyash_api.create_send_link(950, token="test", request_id=request_id)
            self.assertEqual(request.call_args.kwargs["json_body"]["uuidClient"], request_id)

    def test_link_502_is_unknown(self):
        with patch.object(kyash_api, "create_send_link", side_effect=kyash_api.KyashError(502)):
            self.assertEqual(kyash_bridge.send_link(950)["status"], "unknown")

    async def test_failed_receipt_reservation_released_after_restart(self):
        args = ("k2p", 1, "user", 1000, 950, 50, 5)
        order, _ = store.get_or_create_order(*args, receipt_key="incoming", status="RECEIVING")
        store.reserve_link("incoming", 1, order_id=order["id"])
        store.claim_order(order["id"], ("RECEIVING",), "CANCELED", receipt_rejected=True)
        await bot.recover_stuck_orders()
        self.assertIsNone(store.link_used("incoming"))
        replacement, created = store.get_or_create_order(*args, receipt_key="incoming", status="RECEIVING")
        self.assertTrue(created)
        self.assertNotEqual(order["id"], replacement["id"])

    def test_outgoing_requires_request_id_and_transaction_id(self):
        request_id = str(uuid4())
        transfer = {"amount": -950, "target": {"publicId": 42}, "txUUIDAPI": "tx"}
        with patch.object(kyash_api, "get_timeline", return_value=[{"transfer": transfer}]):
            self.assertIsNone(kyash_bridge.find_outgoing(950, "42", request_id))
            self.assertIsNone(kyash_bridge.find_outgoing(950, "42", ""))
            transfer["uuidClient"] = request_id
            self.assertEqual(kyash_bridge.find_outgoing(950, "42", request_id)["tx"], "tx")
            del transfer["txUUIDAPI"]
            self.assertIsNone(kyash_bridge.find_outgoing(950, "42", request_id))

    def test_corrupt_and_missing_orders_block_new_orders(self):
        self.paypay_order()
        path = Path(store.ORDERS_FILE)
        for contents in ("{", "{}", json.dumps({"seq": 0, "orders": []})):
            path.write_text(contents)
            with self.assertRaises(RuntimeError):
                self.paypay_order()
            self.assertEqual(path.read_text(), contents)
        path.unlink()
        with self.assertRaises(RuntimeError):
            self.paypay_order()
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
