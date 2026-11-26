"""管理画面の運用機能（v1.27）：売上の集計とグラフ用データ・注文の検索と詳細・受付の一時停止・お知らせ・セール・
お客さんの受付停止・実行枠の数・管理操作の記録・メニュー別の成功率・Q&A の追加回答・実行中の注文の操作。
PayPay・ゲームへの通信はしない（台帳は一時フォルダー）。"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

import admin_ops
import app as A
import paypay_gateway as G
import qa_bot
import site_config

TSUM = {"mode": "line", "login": "a@example.com", "password": "pw", "menus": ["tsum_coin_max"], "qty": {}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(A, "PAYMENTS_ENABLED", True), patch.object(A, "ADMIN_PASSWORD", "pass-1234"),
            patch.object(site_config, "REVIEWS_ENABLED", True),
            patch.dict(A._orders, {}, clear=True), patch.dict(A._jobs, {}, clear=True), patch.dict(A._fail, {}, clear=True),
            patch.object(A, "_slots", [None] * 2), patch.object(A, "_long_slots", [None] * 1),
            patch.object(A, "_pending_jobs", type(A._pending_jobs)()),
            patch.object(G, "_LEDGER_FILE", Path(self.tmp.name) / "l.sqlite3"),
            patch.object(A, "_persist_orders"), patch.object(A, "_rate_ok", return_value=True),
            patch.object(A, "_active_transfers", set()), patch.object(A, "_launch_job"),     # 実行用のプロセスは起動しない
            patch.dict(os.environ, {"PAYPAY_DATA_DIR": self.tmp.name, "PAYPAY_ENCRYPTION_KEY": Fernet.generate_key().decode()}),
        ]
        for p in self.patches:
            p.start()
        self.c = A.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def admin(self, action, c=None, **data):
        c = c or self.c
        c.get("/admin")
        with c.session_transaction() as s:
            token = s["admin_csrf"]
        return c.post("/admin", data=dict(data, csrf=token, action=action))

    def login(self, c=None):
        self.admin("unlock", c, passcode="pass-1234")

    def paid(self, oid, at, amount, shop="tsum", base=None, who="u1", lines=None, summary=None):
        """入金済みの注文を台帳に1件（見本）。"""
        G.history_upsert([{"order_id": oid, "created": at - 10, "user_id": who, "amount": amount, "state": "done",
                           "summary": summary or f"{shop}の見本", "diag": "done", "shop": shop, "base": base or amount,
                           "lines": json.dumps(lines or [{"label": "メニューA", "count": 1, "unit": base or amount}])}])
        with G._ledger() as db:
            db.execute("INSERT INTO links(digest,order_id,state,created,amount,sender_id) VALUES(?,?,?,?,?,?)",
                       ("d" + oid, oid, "paid", at, amount, "s-" + who))


class Sales(Base):
    def test_report_counts_only_received_money(self):
        now = time.time()
        self.paid("o1", now - 3600, 1000, "tsum", base=1200, who="a")
        self.paid("o2", now - 7200, 500, "nyanko", who="b")
        self.paid("o3", now - 40 * 86400, 300, "puni", who="a")                  # 前の期間（リピーターの判定に使う）
        G.history_upsert([{"order_id": "o4", "created": now, "user_id": "c", "amount": 9999, "state": "unpaid",
                           "summary": "", "diag": ""}])                         # 入金されていない注文は数えない
        admin_ops.save_note("o1", "半分返金", "400", "一部失敗")
        r = admin_ops.report(now - 30 * 86400, now + 60)
        t = r["totals"]
        self.assertEqual((t["gross"], t["count"], t["refund"], t["net"], t["discount"]), (1500, 2, 400, 1100, 200))
        self.assertEqual((t["customers"], t["new_customers"], t["repeat_customers"]), (2, 1, 1))
        self.assertEqual(r["unit"], "day")
        self.assertEqual(sum(b["total"] for b in r["buckets"]), 1500)
        self.assertEqual(r["buckets"][-1]["cumulative"], 1100)
        self.assertEqual({s["key"]: s["yen"] for s in r["by_series"]}, {"nyanko": 500, "tsum": 1000, "puni": 0, "other": 0})
        self.assertEqual(sum(h["yen"] for h in r["hours"]), 1500)
        self.assertEqual(r["prev"]["gross"], 300)
        self.assertEqual(r["all_time"]["count"], 3)
        only = admin_ops.report(now - 30 * 86400, now + 60, series="tsum")
        self.assertEqual((only["totals"]["gross"], only["totals"]["refund"]), (1000, 400))

    def test_menu_shares_split_the_amount(self):
        now = time.time()
        self.paid("o1", now - 60, 900, base=1000, lines=[{"label": "A", "count": 1, "unit": 750}, {"label": "B", "count": 1, "unit": 250},
                                                         {"label": "クーポン", "count": 1, "unit": -100, "coupon": True}])
        menus = {m["label"]: m["yen"] for m in admin_ops.report(now - 86400, now)["menus"]}
        self.assertEqual(menus, {"A": 675, "B": 225})

    def test_units_and_bucket_limit(self):
        now = time.time()
        self.assertEqual(admin_ops.pick_unit(now - 86400, now, None), "hour")
        self.assertEqual(admin_ops.pick_unit(now - 365 * 86400, now, None), "week")
        self.assertEqual(admin_ops.pick_unit(now - 3 * 365 * 86400, now, "hour"), "week")   # 区切りが多すぎる → 粗くする
        r = admin_ops.report(now - 2 * 86400, now, "hour")
        self.assertLessEqual(len(r["buckets"]), 50)

    def test_old_rows_without_department(self):
        labels = [("ticket", "1回なんでも無料チケット"), ("support", "支援BOX")]
        self.assertEqual(admin_ops.shop_from_summary("【無料クーポン】ツムツム代行／コイン", labels), "tsum")
        self.assertEqual(admin_ops.shop_from_summary("ぷにぷに代行／周回", labels), "puni")
        self.assertEqual(admin_ops.shop_from_summary("支援BOX（500円）", labels), "support")
        self.assertEqual(admin_ops.shop_from_summary("最大: XP／新規アカウント", labels), "nyanko")

    def test_api_is_admin_only_and_csv_is_safe(self):
        for url in ("/admin/api/sales", "/admin/api/sales.csv", "/admin/api/orders", "/admin/api/order/x", "/admin/api/health", "/admin/api/log"):
            self.assertEqual(self.c.get(url).status_code, 403, url)
        now = time.time()
        self.paid("o1", now - 60, 1000, summary="=HYPERLINK(\"x\")")
        self.login()
        d = self.c.get("/admin/api/sales?from=all").get_json()
        self.assertEqual(d["totals"]["gross"], 1000)
        r = self.c.get("/admin/api/sales.csv")
        self.assertEqual(r.status_code, 200)
        text = r.get_data(as_text=True)
        self.assertTrue(text.startswith("﻿入金日時"))
        self.assertIn("'=HYPERLINK", text)                                      # 表計算ソフトで式として動かない
        self.assertEqual(r.headers["Cache-Control"], "no-store")


class Orders(Base):
    def test_history_keeps_department_and_timeline(self):
        oid = self.c.post("/run", json={"tsum": TSUM}).get_json()["order"]
        A._orders[oid]["state"] = "hold"
        G.history_upsert(A._history_rows())
        rec = admin_ops.order_record(oid)
        self.assertEqual((rec["history"]["shop"], rec["history"]["base"]), ("tsum", A._orders[oid]["amount"]))
        self.assertEqual([e["state"] for e in rec["events"]], ["hold"])
        A._orders[oid]["state"] = "paid"
        G.history_upsert(A._history_rows())
        G.history_upsert(A._history_rows())                                     # 変わっていなければ増やさない
        self.assertEqual([e["state"] for e in admin_ops.order_record(oid)["events"]], ["hold", "paid"])

    def test_search_and_detail(self):
        oid = self.c.post("/run", json={"tsum": TSUM}).get_json()["order"]
        A._orders[oid].update(state="done", result={"transfer": "SECRET-CODE", "confirm": "9999"})
        self.login()
        found = self.c.get("/admin/api/orders?q=" + oid[:6]).get_json()["orders"]
        self.assertEqual(found[0]["order"], oid)
        self.assertEqual(self.c.get("/admin/api/orders?q=" + oid[:6] + "&shop=nyanko").get_json()["orders"], [])
        d = self.c.get("/admin/api/order/" + oid)
        self.assertEqual(d.status_code, 200)
        body = d.get_data(as_text=True)
        self.assertNotIn("SECRET-CODE", body)                                   # 結果のコード・ログイン情報は出さない
        self.assertNotIn('"pw"', body)
        self.assertEqual(d.get_json()["live"]["dept"], "ツムツム")
        self.assertEqual(self.c.get("/admin/api/order/nope").status_code, 404)

    def test_note_and_refund(self):
        oid = self.c.post("/run", json={"tsum": TSUM}).get_json()["order"]
        self.login()
        r = self.admin("order_note", order=oid, note="お詫び済み", refund="1,000", refund_note="失敗")
        self.assertIn("#o:" + oid, r.headers["Location"])                       # 詳細を開き直す
        n = admin_ops.note_of(oid)
        self.assertEqual((n["note"], n["refund"], n["refund_note"]), ("お詫び済み", 1000, "失敗"))
        self.admin("order_note", order=oid, note="x", refund="abc")
        self.assertEqual(admin_ops.note_of(oid)["refund"], 1000)                # 数字でなければ変えない


class Gates(Base):
    def test_pause_department_and_menu(self):
        self.login(A.app.test_client())
        admin_ops.set_paused(["tsum"], [], "", [k for k, _ in A._pausable_menus()])
        r = self.c.post("/run", json={"tsum": TSUM})
        self.assertEqual((r.status_code, r.get_json()["code"]), (503, "paused"))
        self.assertIn("一時停止", self.c.get("/tsum").get_data(as_text=True))
        self.assertNotIn("一時停止しています", self.c.get("/puni").get_data(as_text=True))
        admin_ops.set_paused([], ["tsum_coin_max"], "夕方に再開します", [k for k, _ in A._pausable_menus()])
        r = self.c.post("/run", json={"tsum": TSUM})
        self.assertEqual(r.get_json()["code"], "paused_menu")
        self.assertIn("夕方に再開します", r.get_json()["error"])
        other = dict(TSUM, menus=["tsum_coin"], qty={"tsum_coin": "1000"})
        self.assertEqual(self.c.post("/run", json={"tsum": other}).status_code, 200)
        self.login()
        self.assertEqual(self.c.post("/run", json={"tsum": TSUM}).status_code, 200)   # 管理者は止めない

    def test_pause_nyanko_kind_and_products(self):
        admin_ops.set_paused(["ticket"], ["nyanko:new"], "", [k for k, _ in A._pausable_menus()])
        self.assertEqual(self.c.post("/run", json={"items": ["XP"]}).get_json()["code"], "paused_menu")
        self.assertEqual(self.c.post("/api/ticket/order", json={"plan": "once"}).get_json()["code"], "paused")

    def test_block_customer(self):
        self.c.get("/")
        with self.c.session_transaction() as s:
            uid = A._uid(s.setdefault("job_owner", "owner-x"))
        admin_ops.add_block("uid", uid, "", "テスト")
        r = self.c.post("/run", json={"items": ["XP"]})
        self.assertEqual((r.status_code, r.get_json()["code"]), (403, "blocked"))
        self.assertEqual(self.c.post("/api/box", json={"kind": "opinion", "text": "テストの投稿です。よろしくお願いします。"}).status_code, 403)
        self.assertTrue(admin_ops.remove_block("uid", uid))
        self.assertEqual(self.c.post("/run", json={"items": ["XP"]}).status_code, 200)
        admin_ops.add_block("ip", "127.0.0.1", "1")
        self.assertEqual(self.c.post("/run", json={"items": ["XP"]}).status_code, 403)
        with self.assertRaises(ValueError):
            admin_ops.add_block("ip", "not-an-ip", "")
        with self.assertRaises(ValueError):
            admin_ops.add_block("uid", "ZZZ", "")

    def test_block_from_order(self):
        oid = self.c.post("/run", json={"items": ["XP"]}).get_json()["order"]
        self.login(A.app.test_client())
        admin = A.app.test_client()
        self.login(admin)
        self.admin("block_order", admin, order=oid, with_ip="1")
        kinds = {b["kind"] for b in admin_ops.blocks()}
        self.assertEqual(kinds, {"uid", "ip"})
        self.assertEqual(self.c.post("/run", json={"items": ["XP"]}).status_code, 403)

    def test_sale_discounts_and_shows(self):
        admin_ops.set_sale("20", "24", ["tsum"], "週末セール")
        r = self.c.post("/run", json={"tsum": TSUM}).get_json()
        o = A._orders[r["order"]]
        self.assertEqual(r["amount"], o["base_amount"] * 80 // 100)
        self.assertTrue(any(b.get("sale") and "週末セール（20%引き）" in b["label"] for b in r["breakdown"]))
        self.assertIn("週末セール", self.c.get("/tsum").get_data(as_text=True))
        nyan = self.c.post("/run", json={"items": ["XP"]}).get_json()
        self.assertEqual(nyan["amount"], A._orders[nyan["order"]]["base_amount"])     # 対象外の部門は割り引かない
        with self.assertRaises(ValueError):
            admin_ops.set_sale("80", "24", ["tsum"])
        with self.assertRaises(ValueError):
            admin_ops.set_sale("10", "", ["tsum"])                                    # 終わりの無いセールは作れない
        admin_ops.set_sale("0", "", [])
        self.assertIsNone(admin_ops.sale())

    def test_notice_is_escaped_and_expires(self):
        admin_ops.set_notice("<script>alert(1)</script>混雑中", "1", "warn")
        page = self.c.get("/").get_data(as_text=True)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;混雑中", page)
        self.assertNotIn("<script>alert(1)", page)
        self.assertIn("混雑中", self.c.get("/nyanko").get_data(as_text=True))
        self.assertNotIn("混雑中", self.c.get("/admin").get_data(as_text=True))
        d = admin_ops.settings()
        d["notice"]["until"] = time.time() - 1
        admin_ops._save(d)
        self.assertNotIn("混雑中", self.c.get("/").get_data(as_text=True))


class Ops(Base):
    def test_audit_log(self):
        self.admin("unlock", passcode="wrong-pass")
        self.login()
        self.admin("notice_save", text="テスト", hours="")
        log = admin_ops.action_log()
        self.assertEqual([x["action"] for x in log][:3], ["notice_save", "login", "login_failed"])
        self.assertNotIn("wrong-pass", json.dumps(log, ensure_ascii=False))
        self.assertNotIn("pass-1234", json.dumps(log, ensure_ascii=False))
        items = self.c.get("/admin/api/log").get_json()["items"]
        self.assertEqual([i["label"] for i in items][:3], ["お知らせを変更", "ログイン", "ログイン失敗"])
        self.assertTrue(items[2]["failed"])

    def test_slots_resize(self):
        self.login()
        self.admin("slots_save", normal="4", long="2")
        self.assertEqual((len(A._slots), len(A._long_slots)), (4, 2))
        A._slots[3] = "busy-job"
        self.admin("slots_save", normal="1", long="0")
        self.assertEqual((len(A._slots), len(A._long_slots)), (4, 0))           # 動いている枠は止めない
        A._slots[3] = None
        with A._jobs_lock:
            A._resize_slots_locked()                                            # 空いたら減る
        self.assertEqual(len(A._slots), 1)
        self.admin("slots_save", normal="99", long="")
        self.assertEqual(admin_ops.slots()["normal"], 1)                        # 範囲外は保存しない
        A._slot_target.update(normal=None, long=None)

    def test_job_stats_from_reconcile(self):
        oid = self.c.post("/run", json={"tsum": TSUM}).get_json()["order"]
        A._orders[oid].update(state="started", job_id="J1")
        A._jobs["J1"] = {"status": "error", "error": "ログインできませんでした。", "result": None, "finished": time.time(), "created": time.time(),
                         "owner": "x", "mode": "tsum", "log": []}
        A._reconcile_orders()
        oid2 = self.c.post("/run", json={"items": ["XP"]}).get_json()["order"]
        A._orders[oid2].update(state="started", job_id="J2")
        A._jobs["J2"] = {"status": "error", "error": "順番待ちを取り消しました。", "result": None, "finished": time.time(), "created": time.time(),
                         "owner": "x", "mode": "new", "log": []}
        A._reconcile_orders()                                                   # お客さんの取り消しは失敗に数えない
        A._orders[oid].update(state="started", job_id="J3")
        A._jobs["J3"] = {"status": "done", "error": None, "result": {"transfer": "a"}, "finished": time.time(), "created": time.time(),
                         "owner": "x", "mode": "tsum", "log": []}
        A._reconcile_orders()
        s = {e["menu"]: e for e in admin_ops.job_summary()}
        self.assertEqual(list(s), ["コインMAX"])
        self.assertEqual((s["コインMAX"]["week"], s["コインMAX"]["week_rate"]), ([2, 1], 50))
        self.assertEqual(s["コインMAX"]["errors"][0][0], "ログインできませんでした。")

    def test_queue_front_and_cancel(self):
        ids = []
        for i in range(2):
            oid = self.c.post("/run", json={"items": ["XP"]}).get_json()["order"]
            A._orders[oid].update(state="started", job_id=f"Q{i}")
            A._jobs[f"Q{i}"] = {"status": "queued", "created": time.time(), "owner": "x", "finished": None, "log": []}
            A._pending_jobs.append((f"Q{i}", (), None))
            ids.append(oid)
        A._slots[:] = ["busy1", "busy2"]
        self.login()
        self.admin("job_front", order=ids[1])
        self.assertEqual([e[0] for e in A._pending_jobs], ["Q1", "Q0"])
        self.admin("job_cancel", order=ids[0])
        self.assertEqual([e[0] for e in A._pending_jobs], ["Q1"])
        self.assertEqual(A._jobs["Q0"]["status"], "error")
        A._reconcile_orders()
        self.assertEqual((A._orders[ids[0]]["state"], A._orders[ids[0]]["retry"]), ("paid", True))   # お支払いは残る

    def test_stop_running_puni(self):
        oid = self.c.post("/run", json={"items": ["XP"]}).get_json()["order"]
        A._orders[oid].update(state="started", job_id="P1")
        A._jobs["P1"] = {"status": "running", "mode": "puni", "created": time.time(), "owner": "x", "finished": None, "log": []}
        sent = []
        pipe = type("Pipe", (), {"send": lambda self, m: sent.append(m)})()
        with patch.dict(A._job_pipes, {"P1": pipe}):
            self.login()
            self.admin("job_stop", order=oid)
        self.assertEqual(sent, [{"stop": True}])

    def test_faq_used_by_chat(self):
        self.login()
        self.admin("faq_add", shop="tsum", q="夜中でも注文できますか", a="はい、24時間受け付けています。")
        self.assertEqual(len(admin_ops.faq_list("tsum")), 1)
        self.assertEqual(admin_ops.faq_list("puni"), [])
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            r = self.c.post("/api/qa", json={"shop": "tsum", "q": "夜中でも注文できる？"}).get_json()
            self.assertEqual(r["answer"], "はい、24時間受け付けています。")
            r = self.c.post("/api/qa", json={"shop": "tsum", "q": "支払い方法は？"}).get_json()
            self.assertNotEqual(r["answer"], "はい、24時間受け付けています。")
        self.assertIn("夜中でも注文できますか", qa_bot._system_prompt("tsum", [], None, admin_ops.faq_list("tsum")))
        fid = admin_ops.faq_list()[0]["id"]
        self.admin("faq_delete", id=str(fid))
        self.assertEqual(admin_ops.faq_list(), [])

    def test_health_and_pages(self):
        self.login()
        h = self.c.get("/admin/api/health").get_json()
        names = {i["name"] for i in h["items"]}
        self.assertTrue({"PayPay の接続", "注文の保存", "ディスクの空き"} <= names)
        page = self.c.get("/admin").get_data(as_text=True)
        for word in ('data-sec="sales"', 'data-sec="ops"', "受付の一時停止", "メニュー別の成功率", "注文を探す", "/static/admin.js"):
            self.assertIn(word, page)
        self.assertEqual(self.c.get("/static/admin.js").status_code, 200)


if __name__ == "__main__":
    unittest.main()
