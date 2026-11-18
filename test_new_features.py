"""管理者無料・プレイ時間を増やす・初期垢作成・クーポンのテスト（PayPay通信はモック）。"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import app as A
import paypay_gateway as G


class NewFeatures(unittest.TestCase):
    LINK = "https://pay.paypay.ne.jp/abcDEF123456"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(A, "PAYMENTS_ENABLED", True),
            patch.dict(A._orders, {}, clear=True),
            patch.dict(A._jobs, {}, clear=True),
            patch.object(G, "history_upsert"),
            patch.object(G, "_LEDGER_FILE", Path(self.tmp.name) / "l.sqlite3"),
            patch.object(A, "_RESTART_FILE", os.path.join(self.tmp.name, "rp.json")),
            patch.object(A, "_persist_orders"),
        ]
        for p in self.patches:
            p.start()
        self.jobs = A._jobs
        self.c = A.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def run_(self, body, c=None):
        return (c or self.c).post("/run", json=body)

    # ---------------- プレイ時間を増やす
    def test_playtime_price_and_validation(self):
        r = self.run_({"playtime_add_min": 90}).get_json()
        self.assertEqual(r["amount"], 90)  # 9 × 10円
        for bad in (50, 65, "abc", -60, A.PT_MAX_MINUTES + 10):
            self.assertEqual(self.run_({"playtime_add_min": bad}).status_code, 400, bad)
        self.assertEqual(self.run_({"playtime_add_min": 60, "others": ["プレイ時間カンスト"]}).status_code, 400)
        # ほかのメニューと一緒
        self.assertEqual(self.run_({"playtime_add_min": 120, "items": ["XP"]}).get_json()["amount"], 120 + 25)

    def test_playtime_added_to_existing_and_capped(self):
        save = SimpleNamespace(officer_pass=SimpleNamespace(play_time=30 * 60 * 30))  # 30分
        job = {"log": []}
        with patch.object(A, "_log"), patch.object(A.engine, "apply_subs_and_others", return_value=[]):
            A._apply_edits(job, save, [], [], [], {A.PT_KEY: 60}, [])
        self.assertEqual(save.officer_pass.play_time, 90 * 60 * 30)
        save.officer_pass.play_time = 2147483000
        with patch.object(A, "_log"), patch.object(A.engine, "apply_subs_and_others", return_value=[]):
            A._apply_edits(job, save, [], [], [], {A.PT_KEY: 600}, [])
        self.assertEqual(save.officer_pass.play_time, 2147483647)

    # ---------------- 初期垢作成
    def test_restart_default_is_official_and_admin_can_disable(self):
        r = self.run_({"restart_pack": True})          # 未設定なら公式と同じ中身で選べる
        self.assertEqual((r.status_code, r.get_json()["amount"]), (200, 50))
        self.assertEqual(A.restart_pack()["cats"], A.RESTART_OFFICIAL["cats"])
        A._save_restart(A._parse_restart_form("XP=1", "", False, ""))   # 管理者が「準備中」にした
        self.assertEqual(self.run_({"restart_pack": True}).status_code, 400)

    def test_restart_price_and_transfer_must_be_empty(self):
        A._save_restart(A._parse_restart_form("XP=1000\n", "", True, "テスト"))
        r = self.run_({"restart_pack": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["amount"], 50)
        self.assertEqual(self.run_({"restart_pack": True, "items": ["NP"]}).get_json()["amount"], 75)
        self.assertEqual(self.run_({"restart_pack": True, "transfer": "abc", "confirm": "1234"}).status_code, 400)

    def test_restart_form_rejects_unknown_items(self):
        with self.assertRaises(ValueError):
            A._parse_restart_form("存在しない項目=5", "", True, "")

    def test_restart_items_applied(self):
        A._save_restart(A._parse_restart_form("XP=1000", "", True, ""))
        save = SimpleNamespace(officer_pass=SimpleNamespace(play_time=0))
        with patch.object(A, "_log"), patch.object(A.engine, "apply_subs_and_others", return_value=[]), \
             patch.object(A.engine, "apply_item_amounts", return_value=[]) as ap:
            A._apply_edits({"log": []}, save, [], [], [A.RESTART_KEY], {}, [])
        self.assertEqual(ap.call_args_list[0].kwargs["quantity_map"], {"XP": 1000})

    def test_restart_official_preset_and_levels(self):
        d = A._parse_restart_form("レアチケ=1\nにゃんチケ=10", "1-9,17,25,26", True, "", ["日本編 第1章クリア", "存在しない"][:1], "20", "20")
        self.assertEqual((d["cat_level"], d["facility_level"], d["subs"]), (20, 20, ["日本編 第1章クリア"]))
        with self.assertRaises(ValueError):
            A._parse_restart_form("", "", True, "", ["存在しない"])
        with self.assertRaises(ValueError):
            A._parse_restart_form("", "", True, "", [], "20")           # レベルだけでキャラ番号なし
        with self.assertRaises(ValueError):
            A._parse_restart_form("", "1", True, "", [], "", "21")
        A._save_restart(dict(A.RESTART_OFFICIAL, enabled=True))
        rp = A.restart_pack()
        self.assertTrue(rp["enabled"])
        self.assertEqual(rp["cats"], [1, 2, 3, 4, 5, 6, 7, 8, 9, 17, 25, 26])
        save = SimpleNamespace(officer_pass=SimpleNamespace(play_time=0))
        with patch.object(A, "_log"), patch.object(A.engine, "apply_subs_and_others", return_value=[]) as subs, \
             patch.object(A.engine, "apply_item_amounts", return_value=[]), \
             patch.object(A.engine, "apply_cat_customizations", return_value={}) as cust, \
             patch.object(A.engine, "set_facility_levels", return_value=True) as fac:
            A._apply_edits({"log": [], "warn": []}, save, [], [], [A.RESTART_KEY], {}, [])
        self.assertEqual(subs.call_args_list[0].args[1], ["日本編 第1章クリア", "日本編 第2章クリア", "日本編 第3章クリア"])
        self.assertEqual(cust.call_args.args[1][0], {"base_level": 20})
        self.assertEqual(len(cust.call_args.args[1]), 12)
        fac.assert_called_once_with(save, 20)

    # ---------------- 管理者は無料
    def _admin_client(self):
        c = A.app.test_client()
        with c.session_transaction() as s:
            s["admin"] = True
        return c

    def test_admin_runs_for_free(self):
        c = self._admin_client()
        with patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="JA")) as st:
            r = self.run_({"items": ["XP"], "subs": ["全キャラレベルMAX"]}, c)
        self.assertEqual(r.get_json(), {"job": "JA"})
        st.assert_called_once()
        o = next(iter(A._orders.values()))
        self.assertEqual((o["amount"], o["admin_free"]), (0, True))
        self.assertIn("無料", c.get("/").get_data(as_text=True))
        # 一般の利用者は今まで通り有料
        self.assertEqual(self.run_({"items": ["XP"]}).get_json()["amount"], 25)

    def test_admin_free_order_gets_no_coupon(self):
        oid = "F1"
        A._orders[oid] = {"owner": "x", "state": "started", "amount": 0, "admin_free": True, "job_id": "J1", "created": time.time()}
        self.jobs["J1"] = {"finished": None, "created": time.time(), "status": "done", "error": None, "result": {"transfer": "t"}}
        A._reconcile_orders()
        self.assertFalse(A._orders[oid].get("coupon_issued"))

    # ---------------- クーポン
    def test_coupon_lifecycle(self):
        code = G.coupon_issue("O1", 30)
        self.assertEqual(G.coupon_issue("O1", 30), code)  # 同じ注文で二重発行しない
        self.assertTrue(code.startswith("NK-"))
        # 200円未満では使えない
        r = self.run_({"items": ["XP"], "coupon": code})
        self.assertEqual((r.status_code, r.get_json()["code"]), (400, "coupon_min"))
        # 200円以上 → 10%引き（切り捨て）
        body = {"chars": "1-5", "items": ["XP"], "coupon": code.lower()}   # 250+25=275 → 247
        r = self.run_(body).get_json()
        self.assertEqual(r["amount"], 247)
        self.assertEqual(G.coupon_check(code)[0], False)                      # 使用中（ほかの注文では使えない）
        r2 = self.run_(body, A.app.test_client())
        self.assertEqual((r2.status_code, r2.get_json()["code"]), (400, "coupon_invalid"))
        # 支払いが確認できたら使用済みに
        with patch.object(G, "receive_payment", AsyncMock(return_value="d")), patch.object(G, "mark_used"), \
             patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="JC")):
            self.c.post(f"/api/orders/{r['order']}/pay", json={"link": self.LINK})
        self.assertEqual(G.coupon_list()[0]["state"], "used")
        self.assertIn("すでに使われています", G.coupon_check(code)[1])

    def test_coupon_released_when_unpaid_order_expires(self):
        code = G.coupon_issue("O2", 30)
        r = self.run_({"chars": "1-4", "coupon": code}).get_json()
        self.assertEqual(r["amount"], 180)
        A._orders[r["order"]]["created"] -= 10 ** 6
        A._gc_orders()
        self.assertTrue(G.coupon_check(code)[0])

    def test_coupon_invalid_and_expired(self):
        r = self.run_({"chars": "1-5", "coupon": "NK-XXXX-YYYY"})
        self.assertEqual(r.get_json()["code"], "coupon_invalid")
        code = G.coupon_issue("O3", 0)
        time.sleep(0.01)
        self.assertIn("期限", G.coupon_check(code)[1])

    def test_coupon_issued_on_done_paid_200_or_more(self):
        for oid, amount, expect in (("P1", 200, True), ("P2", 199, False)):
            A._orders[oid] = {"owner": "x", "state": "started", "amount": amount, "job_id": "J" + oid, "created": time.time()}
            self.jobs["J" + oid] = {"finished": None, "created": time.time(), "status": "done", "error": None, "result": {"transfer": "t"}}
        A._reconcile_orders()
        self.assertTrue(A._orders["P1"].get("coupon_issued"))
        self.assertFalse(A._orders["P2"].get("coupon_issued"))
        pub = A._public_order("P1", A._orders["P1"])
        self.assertEqual(pub["coupon_issued"], A._orders["P1"]["coupon_issued"])


if __name__ == "__main__":
    unittest.main()
