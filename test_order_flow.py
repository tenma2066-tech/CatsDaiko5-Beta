"""決済してから実行する流れのテスト（PayPay通信はモック）。"""
import unittest
from unittest.mock import patch, AsyncMock

import app as A
import paypay_gateway as G


class OrderFlow(unittest.TestCase):
    def setUp(self):
        self.p = patch.object(A, "PAYMENTS_ENABLED", True)
        self.p.start()
        self.o = patch.dict(A._orders, {}, clear=True)  # テストごとに注文を空にする
        self.o.start()
        self.j = patch.dict(A._jobs, {}, clear=True)
        self.jobs = self.j.start()
        self.h = patch.object(G, "history_upsert")  # 履歴の書き込みは別のテストで確認
        self.h.start()
        self.c = A.app.test_client()

    def tearDown(self):
        self.h.stop()
        self.j.stop()
        self.o.stop()
        self.p.stop()

    def order(self, c=None):
        r = (c or self.c).post("/run", json={"items": ["XP"], "dry": True})
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_run_returns_order_not_job(self):
        j = self.order()
        self.assertEqual(j["amount"], 25)
        self.assertNotIn("job", j)

    def test_prices(self):
        f = A.compute_amount
        self.assertEqual(f(["XP"], [], [], {}, [], False, False)[0], 25)
        self.assertEqual(f(["XP"], ["全キャラレベルMAX"], [], {"XP": 5, "NP": 1}, [], False, False)[0], 75)  # XP/NP/サブ=3件
        self.assertEqual(f([], [], [], {}, [1, 2, 3], False, False)[0], 150)
        self.assertEqual(f([], [], list(A.CRASH_ITEMS), {}, [], False, False)[0], 100)
        self.assertEqual(f([], [], list(A.CRASH_ITEMS) + ["施設全強化"], {}, [7], False, False)[0], 175)
        # キャラ: 1番=レベル+形態+本能(3件), 2番=設定なし(1件), 3番=+値のみ(1件) → 5件=250円
        cs = {0: {"base_level": 50, "form": 3, "talents_all": True}, 2: {"plus_level": 10}}
        self.assertEqual(f([], [], [], {}, [1, 2, 3], False, False, cs)[0], 250)
        self.assertEqual(f([], [], [], {}, [], True, False)[0], 100)
        self.assertEqual(f([], [], [], {}, [], False, True)[0], 500)

    def test_order_amount_is_server_side(self):
        r = self.c.post("/run", json={"chars": "1-3", "amount": 1}).get_json()
        self.assertEqual(r["amount"], 150)
        r = self.c.post("/run", json={"chars": "1", "cat_settings": {"1": {"base_level": 30, "form": 2}}}).get_json()
        self.assertEqual(r["amount"], 100)
        r = self.c.post("/run", json={"transfer": "abc", "confirm": "1234", "integrity_only": True}).get_json()
        self.assertEqual(r["amount"], 500)
        r = self.c.post("/run", json={"transfer": "abc", "confirm": "1234", "clone": True}).get_json()
        self.assertEqual(r["amount"], 100)

    def test_unpaid_failure_does_not_start(self):
        j = self.order()
        with patch.object(G, "receive_payment", AsyncMock(side_effect=G.PayPayError("amount_mismatch", "金額が違います"))), \
             patch.object(A, "_start_job") as start:
            r = self.c.post(f"/api/orders/{j['order']}/pay", json={"link": "x"})
        self.assertEqual(r.status_code, 402)
        start.assert_not_called()
        self.assertFalse(r.get_json()["uncertain"])

    LINK = "https://pay.paypay.ne.jp/abcDEF123456"

    def test_hold_then_received_starts_job_without_new_link(self):
        j = self.order()
        with patch.object(G, "receive_payment", AsyncMock(side_effect=G.PayPayError("temporary_hold", "保留"))):
            r = self.c.post(f"/api/orders/{j['order']}/pay", json={"link": self.LINK})
        self.assertEqual(r.status_code, 402)
        self.assertTrue(r.get_json()["hold"])
        # まだ受け取られていない → 保留のまま
        with patch.object(G, "confirm_received", AsyncMock(side_effect=G.PayPayError("still_pending", "未"))):
            r = self.c.post(f"/api/orders/{j['order']}/pay", json={})
        self.assertEqual((r.status_code, r.get_json()["hold"], r.get_json()["code"]), (402, True, "still_pending"))
        # 解除待ちの確認を何度しても「失敗が続いた」でロックされない
        with patch.object(G, "confirm_received", AsyncMock(side_effect=G.PayPayError("still_pending", "未"))):
            for _ in range(8):
                r = self.c.post(f"/api/orders/{j['order']}/pay", json={})
        self.assertEqual(r.get_json()["code"], "still_pending")
        # 受け取り済み → 開始（新しいリンクもreceive_paymentも使わない）
        with patch.object(G, "confirm_received", AsyncMock(return_value="d")) as cr, \
             patch.object(G, "receive_payment", AsyncMock()) as rp, \
             patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="J3")):
            r = self.c.post(f"/api/orders/{j['order']}/pay", json={"link": "https://pay.paypay.ne.jp/otherLINK9999"})
        self.assertEqual(r.get_json()["job"], "J3")
        cr.assert_awaited_once()
        rp.assert_not_awaited()

    def test_mine_lists_only_own_resumable_orders(self):
        j = self.order()
        mine = self.c.get("/api/orders/mine").get_json()["orders"]
        self.assertEqual([o["order"] for o in mine], [j["order"]])
        self.assertEqual(mine[0]["state"], "unpaid")
        self.assertTrue(mine[0]["breakdown"])
        self.assertEqual(A.app.test_client().get("/api/orders/mine").get_json()["orders"], [])

    def test_claim_by_order_number_moves_to_new_browser(self):
        j = self.order()
        other = A.app.test_client()
        self.assertEqual(other.post("/api/orders/wrong-number/claim").status_code, 404)
        r = other.post(f"/api/orders/{j['order']}/claim")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["amount"], 25)
        with patch.object(G, "receive_payment", AsyncMock(return_value="d")), \
             patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="JX")):
            self.assertEqual(self.c.post(f"/api/orders/{j['order']}/pay", json={"link": "x"}).status_code, 404)  # 元のブラウザは使えない
            self.assertEqual(other.post(f"/api/orders/{j['order']}/pay", json={"link": "x"}).get_json()["job"], "JX")
        # 開始済みは再開対象外
        self.assertEqual(other.post(f"/api/orders/{j['order']}/claim").status_code, 404)

    def test_admin_can_rescue_dead_order(self):
        j = self.order()
        with patch.object(G, "receive_payment", AsyncMock(side_effect=G.PayPayError("accept_uncertain", "不明"))):
            self.c.post(f"/api/orders/{j['order']}/pay", json={"link": self.LINK})
        with patch.object(G, "confirm_received", AsyncMock(side_effect=G.PayPayError("hold_failed", "x"))):
            self.c.post(f"/api/orders/{j['order']}/pay", json={})
        mine = self.c.get("/api/orders/mine").get_json()["orders"]
        self.assertEqual(mine[0]["state"], "dead")  # 利用者にも見える（注文番号を伝えられる）
        admin = A.app.test_client()
        with patch.object(A, "ADMIN_PASSWORD", "adm"), patch.object(G, "status", return_value={}), \
             patch.object(G, "link_diag", return_value="review / S0000/CANCELED"), patch.object(G, "mark_paid_by_admin") as mp:
            admin.get("/admin")
            with admin.session_transaction() as s:
                csrf = s["admin_csrf"]
            self.assertEqual(admin.post("/admin", data={"action": "order_paid", "csrf": csrf, "order": j["order"]}).status_code, 403)
            admin.post("/admin", data={"action": "unlock", "csrf": csrf, "passcode": "adm"})
            with admin.session_transaction() as s:
                csrf = s["admin_csrf"]
            page = admin.get("/admin").get_data(as_text=True)
            self.assertIn(j["order"], page)
            self.assertIn("hold_failed", page)
            def real_like_start(*a):  # 本物と同じく、開始したジョブを登録してから返す
                A._jobs["JR"] = {"finished": None, "created": __import__("time").time(), "status": "running", "error": None, "result": None}
                return A.jsonify(job="JR")
            with patch.object(A, "_start_job", side_effect=real_like_start) as st:
                page = admin.post("/admin", data={"action": "order_paid", "csrf": csrf, "order": j["order"]}).get_data(as_text=True)
            mp.assert_called_once()
            st.assert_called_once()  # 管理者が支払い済みにした時点で実行が始まる
            self.assertIn("実行を開始しました", page)
        self.assertEqual((A._orders[j["order"]]["state"], A._orders[j["order"]]["job_id"]), ("started", "JR"))
        # 利用者の画面からは、その実行が見える（自動で状況画面に切り替わる）
        self.jobs["JR"] = {"finished": None, "created": __import__("time").time(), "status": "running", "error": None, "result": None}
        mine = self.c.get("/api/orders/mine").get_json()["orders"]
        self.assertEqual([(o["state"], o.get("job")) for o in mine], [("started", "JR")])

    def test_admin_paid_when_busy_leaves_start_button(self):
        j = self.order()
        A._orders[j["order"]].update(state="dead", link_code="abcDEF123456")
        admin = A.app.test_client()
        with patch.object(A, "ADMIN_PASSWORD", "adm"), patch.object(G, "status", return_value={}), patch.object(G, "mark_paid_by_admin"), \
             patch.object(G, "link_diag", return_value=""):
            admin.get("/admin")
            with admin.session_transaction() as s:
                csrf = s["admin_csrf"]
            admin.post("/admin", data={"action": "unlock", "csrf": csrf, "passcode": "adm"})
            with admin.session_transaction() as s:
                csrf = s["admin_csrf"]
            with patch.object(A, "_start_job", side_effect=lambda *a: (A.jsonify(error="busy"), 429)):
                page = admin.post("/admin", data={"action": "order_paid", "csrf": csrf, "order": j["order"]}).get_data(as_text=True)
        self.assertIn("待機枠がいっぱい", page)
        self.assertEqual(A._orders[j["order"]]["state"], "paid")
        self.assertEqual(self.c.get("/api/orders/mine").get_json()["orders"][0]["state"], "paid")

    def test_same_link_in_new_order_takes_over_stuck_order(self):
        old = self.order()
        with patch.object(G, "receive_payment", AsyncMock(side_effect=G.PayPayError("temporary_hold", "保留"))):
            self.c.post(f"/api/orders/{old['order']}/pay", json={"link": self.LINK})
        new = self.order()
        with patch.object(G, "receive_payment", AsyncMock(side_effect=G.PayPayError("duplicate", "dup"))), \
             patch.object(G, "adopt_link") as adopt, patch.object(G, "confirm_received", AsyncMock(return_value="d")), \
             patch.object(G, "mark_used") as used, \
             patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="JN")):
            r = self.c.post(f"/api/orders/{new['order']}/pay", json={"link": self.LINK})
            self.assertEqual(r.get_json().get("job"), "JN", r.get_json())
            adopt.assert_called_once()
            used.assert_called_once()
            # 古い注文は無効になり、二重には実行できない
            self.assertEqual(self.c.post(f"/api/orders/{old['order']}/pay", json={}).status_code, 409)

    def test_link_of_started_order_cannot_be_reused(self):
        first = self.order()
        with patch.object(G, "receive_payment", AsyncMock(return_value="d")), patch.object(G, "mark_used"), \
             patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="J1")):
            self.c.post(f"/api/orders/{first['order']}/pay", json={"link": self.LINK})
        self.jobs["J1"] = {"finished": None, "created": __import__("time").time(), "status": "running", "error": None, "result": None}
        second = self.order()
        with patch.object(G, "receive_payment", AsyncMock(side_effect=G.PayPayError("duplicate", "dup"))), \
             patch.object(G, "adopt_link") as adopt, patch.object(A, "_start_job") as start:
            r = self.c.post(f"/api/orders/{second['order']}/pay", json={"link": self.LINK})
        self.assertEqual((r.status_code, r.get_json()["code"]), (402, "duplicate"))
        adopt.assert_not_called(); start.assert_not_called()

    def _paid_and_started(self, job_id="JF"):
        j = self.order()
        with patch.object(G, "receive_payment", AsyncMock(return_value="d")), patch.object(G, "mark_used"), \
             patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job=job_id)):
            self.c.post(f"/api/orders/{j['order']}/pay", json={"link": self.LINK})
        return j

    def test_failed_job_returns_to_paid_and_can_rerun_without_paying(self):
        j = self._paid_and_started()
        self.jobs["JF"] = {"finished": None, "created": __import__("time").time(), "status": "error", "error": "引継ぎコードが違います", "result": None}
        A._reconcile_orders()
        o = A._orders[j["order"]]
        self.assertEqual((o["state"], o["retry"]), ("paid", True))
        self.assertIsNotNone(o["arguments"])  # 同じ内容で再実行できる
        mine = self.c.get("/api/orders/mine").get_json()["orders"]
        self.assertTrue(mine[0]["retry"])
        # 内容を直して「実行」→ 新しい支払いは不要で、同じ注文が使われる
        r = self.c.post("/run", json={"items": ["NP"], "transfer": "newcode12", "confirm": "1234"}).get_json()
        self.assertEqual((r["order"], r.get("prepaid"), r["state"]), (j["order"], True, "paid"))
        self.assertEqual(A._orders[j["order"]]["arguments"][0], "newcode12")
        with patch.object(G, "receive_payment", AsyncMock()) as rp, patch.object(G, "mark_used"), \
             patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="JG")):
            self.assertEqual(self.c.post(f"/api/orders/{j['order']}/pay", json={}).get_json()["job"], "JG")
        rp.assert_not_awaited()
        # 高い内容には充てない（新しい注文になる）
        self.jobs["JG"] = {"finished": None, "created": __import__("time").time(), "status": "error", "error": "x", "result": None}
        A._reconcile_orders()
        r = self.c.post("/run", json={"chars": "1-5"}).get_json()
        self.assertNotEqual(r["order"], j["order"])

    def test_interrupted_job_after_restart_returns_to_paid(self):
        j = self._paid_and_started("gone")  # サーバー再起動でジョブが消えた
        A._reconcile_orders()
        self.assertEqual(A._orders[j["order"]]["state"], "paid")

    def test_done_job_completes_order_and_clears_inputs(self):
        j = self._paid_and_started("JD")
        self.jobs["JD"] = {"finished": None, "created": __import__("time").time(), "status": "running", "error": None, "result": None}
        A._reconcile_orders()
        self.assertEqual(A._orders[j["order"]]["state"], "started")
        mine = self.c.get("/api/orders/mine").get_json()["orders"]
        self.assertEqual(mine[0]["job"], "JD")  # 状況画面に戻れる
        self.jobs["JD"] = {"finished": None, "created": __import__("time").time(), "status": "done", "error": None, "result": {"transfer": "x"}}
        A._reconcile_orders()
        o = A._orders[j["order"]]
        self.assertEqual((o["state"], o["arguments"]), ("done", None))
        r = self.c.post("/run", json={"items": ["XP"]}).get_json()
        self.assertNotIn("prepaid", r)  # 完了した注文は使い回せない

    def test_unknown_outcome_is_not_rerun_automatically(self):
        j = self._paid_and_started("JU")
        self.jobs["JU"] = {"finished": None, "created": __import__("time").time(), "status": "error", "error": "発行結果が不明", "result": None, "outcome_unknown": True}
        A._reconcile_orders()
        self.assertEqual(A._orders[j["order"]]["state"], "done")

    def test_typed_quantities_count_once_per_base_item(self):
        f = A.compute_amount
        self.assertEqual(f([], [], [], {"マタタビ#1": 5, "マタタビ#2": 5, "マタタビ#3": 5}, [], False, False)[0], 25)
        self.assertEqual(f(["マタタビ"], [], [], {"マタタビ#1": 5, "XP": 1}, [], False, False)[0], 50)

    def test_done_order_cannot_be_rerun_for_free(self):
        j = self._paid_and_started("JZ")
        self.jobs["JZ"] = {"finished": None, "created": __import__("time").time(), "status": "done", "error": None, "result": {"transfer": "t"}}
        A._reconcile_orders()
        with patch.object(A, "_start_job") as start:
            r = self.c.post(f"/api/orders/{j['order']}/pay", json={})
        self.assertEqual((r.status_code, r.get_json()["code"]), (409, "done"))
        start.assert_not_called()

    def test_double_press_starts_only_once(self):
        import threading
        j = self.order()
        A._orders[j["order"]]["state"] = "paid"
        calls = []
        def slow(*a):
            calls.append(1); __import__("time").sleep(0.3); return A.jsonify(job="JJ")
        owner = A._orders[j["order"]]["owner"]
        def hit():
            c2 = A.app.test_client()
            with c2.session_transaction() as s:
                s["job_owner"] = owner
            c2.post(f"/api/orders/{j['order']}/pay", json={})
        with patch.object(A, "_start_job", side_effect=slow):
            ts = [threading.Thread(target=hit) for _ in range(3)]
            [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(len(calls), 1)

    def test_failed_start_returns_to_paid(self):
        j = self.order()
        A._orders[j["order"]]["state"] = "paid"
        with patch.object(A, "_start_job", side_effect=RuntimeError("boom")):
            r = self.c.post(f"/api/orders/{j['order']}/pay", json={})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(A._orders[j["order"]]["state"], "paid")

    def test_recheck_validation_error_goes_to_admin_not_stranded(self):
        j = self.order()
        A._orders[j["order"]].update(state="hold", link_code="abcDEF123456")
        with patch.object(G, "confirm_received", AsyncMock(side_effect=G.PayPayError("expired", "期限切れ"))):
            self.c.post(f"/api/orders/{j['order']}/pay", json={})
        self.assertEqual(A._orders[j["order"]]["state"], "dead")

    def test_recheck_our_side_outage_keeps_hold(self):
        j = self.order()
        A._orders[j["order"]].update(state="hold", link_code="abcDEF123456")
        for code in ("session_rejected", "otp_required", "login_required"):
            with patch.object(G, "confirm_received", AsyncMock(side_effect=G.PayPayError(code, "x"))):
                r = self.c.post(f"/api/orders/{j['order']}/pay", json={})
            self.assertEqual(A._orders[j["order"]]["state"], "hold", code)
            self.assertTrue(r.get_json()["hold"])

    def test_starting_state_after_restart_becomes_paid(self):
        o = A._order_from_json({"state": "starting", "arguments": None, "transfer_key": None})
        self.assertEqual((o["state"], o["retry"]), ("paid", True))

    def test_rerun_does_not_rewrite_previous_job_record(self):
        job = {"status": "error", "log": ["old"], "owner": "o"}
        with A.app.app_context(), patch.object(A, "_launch_job"), patch.object(A, "_slots", [None]), patch.dict(A._jobs, {}, clear=True):
            A._start_job(job, ("", "", ["XP"], [], [], {}, [], {}, False, False, False), None)
        self.assertEqual((job["status"], job["log"]), ("error", ["old"]))

    def test_drain_makes_pay_wait_and_needs_admin_token(self):
        j = self.order()
        with patch.object(A, "ADMIN_PASSWORD", "adm"), patch.object(A, "_drain_until", 0.0):
            self.assertEqual(self.c.post("/api/drain", data={"token": "wrong"}).status_code, 403)
            self.assertTrue(self.c.post("/api/drain", data={"token": "adm"}).get_json()["draining"])
            self.assertTrue(self.c.get("/api/state").get_json()["draining"])
            with patch.object(G, "receive_payment", AsyncMock()) as rp:
                r = self.c.post(f"/api/orders/{j['order']}/pay", json={"link": self.LINK})
            self.assertEqual((r.status_code, r.get_json()["code"]), (503, "updating"))
            rp.assert_not_awaited()
            self.assertEqual(A._orders[j["order"]]["state"], "unpaid")  # 注文はそのまま
            self.assertEqual(self.c.post("/run", json={"items": ["XP"]}).status_code, 200)  # 注文の作成はできる
            self.assertFalse(self.c.post("/api/drain", data={"token": "adm", "off": "1"}).get_json()["draining"])

    def test_blocked_link_on_recheck_keeps_hold(self):
        j = self.order()
        A._orders[j["order"]].update(state="hold", link_code="abcDEF123456")
        with patch.object(G, "confirm_received", AsyncMock(side_effect=G.PayPayError("temporary_hold", "保留"))):
            for _ in range(7):
                r = self.c.post(f"/api/orders/{j['order']}/pay", json={})
        self.assertEqual((A._orders[j["order"]]["state"], r.get_json()["hold"]), ("hold", True))

    def test_codes_still_shown_after_server_forgets_job(self):
        j = self._paid_and_started("JK")
        self.jobs["JK"] = {"finished": __import__("time").time(), "created": __import__("time").time(), "status": "done", "error": None,
                           "result": {"transfer": "KEEPcode1", "confirm": "1111"}, "clone": False, "dry": False}
        A._reconcile_orders()
        del self.jobs["JK"]  # 30分たってサーバーの表示から消えた（または再起動した）
        mine = self.c.get("/api/orders/mine").get_json()["orders"]
        self.assertEqual(mine[0]["state"], "done")
        self.assertEqual(mine[0]["result"]["transfer"], "KEEPcode1")
        self.assertIsNone(mine[0]["job"])
        self.assertEqual(A.app.test_client().get("/api/orders/mine").get_json()["orders"], [])  # 他人には見えない
        self.assertIsNone(A._orders[j["order"]]["arguments"])  # 入力した引継ぎコードは消えている

    def test_dry_run_result_not_kept(self):
        j = self._paid_and_started("JY")
        self.jobs["JY"] = {"finished": None, "created": __import__("time").time(), "status": "done", "error": None, "result": {"transfer": "x"}, "dry": True}
        A._reconcile_orders(); del self.jobs["JY"]
        self.assertEqual(self.c.get("/api/orders/mine").get_json()["orders"], [])

    def test_hold_failed_kills_order(self):
        j = self.order()
        with patch.object(G, "receive_payment", AsyncMock(side_effect=G.PayPayError("accept_uncertain", "不明"))):
            self.c.post(f"/api/orders/{j['order']}/pay", json={"link": self.LINK})
        with patch.object(G, "confirm_received", AsyncMock(side_effect=G.PayPayError("hold_failed", "x"))):
            self.c.post(f"/api/orders/{j['order']}/pay", json={})
        r = self.c.post(f"/api/orders/{j['order']}/pay", json={})
        self.assertEqual(r.status_code, 409)

    def test_other_session_cannot_use_order(self):
        j = self.order()
        r = A.app.test_client().post(f"/api/orders/{j['order']}/pay", json={"link": "x"})
        self.assertEqual(r.status_code, 404)

    def test_paid_starts_job_once(self):
        j = self.order()
        with patch.object(G, "receive_payment", AsyncMock(return_value="d")) as rp, \
             patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="J1")):
            r = self.c.post(f"/api/orders/{j['order']}/pay", json={"link": "x"})
            self.assertEqual(r.get_json()["job"], "J1")
            r2 = self.c.post(f"/api/orders/{j['order']}/pay", json={"link": "x"})
        self.assertEqual(r2.status_code, 409)
        rp.assert_awaited_once()

    def test_paid_but_busy_retries_without_repay(self):
        j = self.order()
        busy = lambda *a: (A.jsonify(error="busy"), 429)
        with patch.object(G, "receive_payment", AsyncMock(return_value="d")) as rp:
            with patch.object(A, "_start_job", side_effect=busy):
                r = self.c.post(f"/api/orders/{j['order']}/pay", json={"link": "x"})
            self.assertEqual((r.status_code, r.get_json()["paid"]), (429, True))
            with patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="J2")):
                r = self.c.post(f"/api/orders/{j['order']}/pay", json={})
            self.assertEqual(r.get_json()["job"], "J2")
        rp.assert_awaited_once()



class HistoryTests(unittest.TestCase):
    def test_history_rows_with_user_and_sender(self):
        import os, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"RUNTIME_GITHUB_REPO": ""}), \
             patch.object(G, "_LEDGER_FILE", Path(d) / "l.sqlite3"):
            with G._ledger() as db:
                db.execute("INSERT INTO links(digest,order_id,state,created,sender_id,sender_name) VALUES('dg','o1','paid',1,'pp-sender-1','たろう')")
            G.history_upsert([{"order_id": "o1", "created": 100.0, "user_id": "aaaa1111", "amount": 75, "state": "done", "summary": "最大: XP", "diag": "done"},
                              {"order_id": "o2", "created": 200.0, "user_id": "bbbb2222", "amount": 25, "state": "unpaid", "summary": "最大: NP", "diag": ""}])
            G.history_upsert([{"order_id": "o2", "created": 200.0, "user_id": "bbbb2222", "amount": 25, "state": "hold", "summary": "最大: NP", "diag": "temporary_hold"}])
            rows = G.history_list()
            self.assertEqual([r["order_id"] for r in rows], ["o2", "o1"])
            self.assertEqual(rows[0]["state"], "hold")
            self.assertEqual((rows[1]["sender_name"], rows[1]["pay_state"]), ("たろう", "paid"))
            self.assertEqual([r["order_id"] for r in G.history_list(user_id="aaaa1111")], ["o1"])
            self.assertEqual([r["order_id"] for r in G.history_list(user_id="pp-sender-1")], ["o1"])

    def test_admin_page_shows_history_and_no_secrets(self):
        import os, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"RUNTIME_GITHUB_REPO": ""}), \
             patch.object(G, "_LEDGER_FILE", Path(d) / "l.sqlite3"), patch.object(G, "save_orders"), \
             patch.object(A, "PAYMENTS_ENABLED", True), patch.dict(A._orders, {}, clear=True), \
             patch.object(A, "ADMIN_PASSWORD", "adm"), patch.object(G, "status", return_value={}):
            c = A.app.test_client()
            j = c.post("/run", json={"items": ["XP"], "transfer": "SECRETcode9", "confirm": "4321"}).get_json()
            admin = A.app.test_client()
            admin.get("/admin")
            with admin.session_transaction() as s:
                csrf = s["admin_csrf"]
            admin.post("/admin", data={"action": "unlock", "csrf": csrf, "passcode": "adm"})
            page = admin.get("/admin").get_data(as_text=True)
            uid = A._orders[j["order"]]["uid"]
            self.assertIn("取引履歴", page)
            self.assertIn(uid, page)
            self.assertIn("最大: XP", page)
            self.assertNotIn("SECRETcode9", page)
            self.assertNotIn("4321", page)
            self.assertIn(uid, admin.get("/admin?user=" + uid).get_data(as_text=True))
            self.assertNotIn(j["order"], admin.get("/admin?user=nobody00").get_data(as_text=True).split("取引履歴")[1])


class OrderPersistence(unittest.TestCase):
    def test_roundtrip_survives_restart_and_resumes_hold(self):
        import os, tempfile
        from pathlib import Path
        from cryptography.fernet import Fernet
        with tempfile.TemporaryDirectory() as d, \
             patch.dict(os.environ, {"PAYPAY_ENCRYPTION_KEY": Fernet.generate_key().decode(), "RUNTIME_GITHUB_REPO": ""}), \
             patch.object(G, "_ROOT", Path(d)), patch.object(G, "_ORDERS_FILE", Path(d) / "orders.enc"), \
             patch.object(A, "PAYMENTS_ENABLED", True), patch.dict(A._orders, {}, clear=True):
            c = A.app.test_client()
            j = c.post("/run", json={"transfer": "abcd1234", "confirm": "1234", "qty": {"XP": "5"}, "chars": "1", "cat_settings": {"1": {"form": 2}}}).get_json()
            link = "https://pay.paypay.ne.jp/abcDEF123456"
            with patch.object(G, "receive_payment", AsyncMock(side_effect=G.PayPayError("temporary_hold", "保留"))):
                self.assertTrue(c.post(f"/api/orders/{j['order']}/pay", json={"link": link}).get_json()["hold"])
            self.assertNotIn(b"abcd1234", (Path(d) / "orders.enc").read_bytes())  # 暗号化されている
            # 再起動をシミュレート
            A._orders.clear()
            A._load_orders()
            self.assertEqual(A._orders[j["order"]]["state"], "hold")
            self.assertEqual(A._orders[j["order"]]["arguments"][7], {0: {"form": 2}})
            with patch.object(G, "confirm_received", AsyncMock(return_value="d")), \
                 patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="J9")):
                r = c.post(f"/api/orders/{j['order']}/pay", json={})
            self.assertEqual(r.get_json()["job"], "J9")

    def test_paying_at_crash_becomes_hold(self):
        o = {"state": "paying", "link_code": "abcdef", "arguments": None, "transfer_key": None}
        self.assertEqual(A._order_from_json(o)["state"], "hold")
        o["link_code"] = None
        self.assertEqual(A._order_from_json(o)["state"], "unpaid")


if __name__ == "__main__":
    unittest.main()
