"""2回目の全体読み直しで直した点の再発防止テスト。
評価を受け取るまで結果を渡さない（サーバー側）・長時間レーンと続きからの再開・注文の掃除や照合の取りこぼし など。
PayPay・ゲームへの通信はモック。"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app as A
import paypay_gateway as G
import puni_service
import site_config

GOOD = "注文から5分で完了しました。案内が分かりやすくて安心でした。"


class Base(unittest.TestCase):
    LINK = "https://pay.paypay.ne.jp/abcDEF123456"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(A, "PAYMENTS_ENABLED", True), patch.object(site_config, "REVIEWS_ENABLED", True),
            patch.dict(A._orders, {}, clear=True), patch.dict(A._jobs, {}, clear=True),
            patch.object(A, "_slots", [None] * 2), patch.object(A, "_long_slots", [None] * 1),
            patch.object(A, "_pending_jobs", type(A._pending_jobs)()),
            patch.object(G, "history_upsert"), patch.object(G, "mark_used"),
            patch.object(G, "_LEDGER_FILE", Path(self.tmp.name) / "l.sqlite3"),
            patch.object(A, "_persist_orders"), patch.object(A, "_rate_ok", return_value=True),
        ]
        for p in self.patches:
            p.start()
        self.c = A.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def started(self, job_id="J1", body=None, **job_fields):
        """支払い済みで実行中の注文を1件作る（実行そのものは始めない）。"""
        oid = self.c.post("/run", json=body or {"items": ["XP"]}).get_json()["order"]
        with self.c.session_transaction() as s:
            owner = s["job_owner"]
        A._orders[oid].update(state="started", job_id=job_id)
        A._jobs[job_id] = dict({"status": "running", "error": None, "result": None, "finished": None, "created": time.time(),
                                "owner": owner, "step": 1, "log": [], "warn": [], "report": [], "dry": False, "clone": False,
                                "mode": "download", "count": 1}, **job_fields)
        return oid

    def finish(self, job_id="J1", result=None):
        A._jobs[job_id].update(status="done", finished=time.time(),
                               result=result if result is not None else {"transfer": "NEWCODE1", "confirm": "4321"})
        A._reconcile_orders()


class ResultWaitsForReview(Base):
    def mine(self, c=None):
        return (c or self.c).get("/api/orders/mine").get_json()["orders"]

    def test_server_withholds_result_until_review(self):
        oid = self.started()
        self.finish()
        st = self.c.get("/status/J1").get_json()
        self.assertEqual((st["status"], st["result"], st["result_locked"]), ("done", None, True))
        row = self.mine()[0]
        self.assertEqual((row["state"], row["locked"], row["result"]), ("done", True, None))
        self.assertNotIn("NEWCODE1", self.c.get("/api/orders/mine").get_data(as_text=True))
        self.assertNotIn("NEWCODE1", self.c.get("/status/J1").get_data(as_text=True))
        bad = self.c.post("/api/reviews", json={"stars": 5, "text": "クソみたいなサービスでした。二度と使いたくない。", "order": oid})
        self.assertEqual(bad.status_code, 400)
        self.assertIsNone(self.mine()[0]["result"])                      # 断られた感想では開かない
        self.assertTrue(self.c.post("/api/reviews", json={"stars": 5, "text": GOOD, "job": "J1"}).get_json()["saved"])
        self.assertEqual(self.c.get("/status/J1").get_json()["result"]["transfer"], "NEWCODE1")
        row = self.mine()[0]
        self.assertEqual((row["locked"], row["result"]["transfer"], row["reviewed"]), (False, "NEWCODE1", True))

    def test_locked_even_before_the_order_is_marked_done(self):
        self.started()
        A._jobs["J1"].update(status="done", finished=time.time(), result={"transfer": "EARLY", "confirm": "1"})   # 照合の前の一瞬
        st = self.c.get("/status/J1").get_json()
        self.assertEqual((st["result"], st["result_locked"]), (None, True))

    def test_summary_lines_are_withheld_too(self):
        self.started(mode="puni", puni={"lines": ["■ アカウント作成：secret@example.com"], "done": 1, "menus": []})
        self.finish(result={"puni_account": [{"email": "secret@example.com", "password": "pw"}]})
        body = self.c.get("/status/J1").get_data(as_text=True)
        self.assertNotIn("secret@example.com", body)

    def test_not_locked_when_failed_with_result_or_admin_or_switched_off(self):
        oid = self.started()                                             # 途中で止まったが結果が出た（引き継ぎ後の失敗など）
        A._jobs["J1"].update(status="error", error="x", outcome_unknown=True, finished=time.time(),
                             result={"transfer": "RESCUE1", "confirm": "1"})
        A._reconcile_orders()
        self.assertEqual(self.c.get("/status/J1").get_json()["result"]["transfer"], "RESCUE1")
        self.assertEqual((self.mine()[0]["locked"], self.mine()[0]["result"]["transfer"]), (False, "RESCUE1"))
        A._orders.pop(oid)
        oid = self.started("J2")                                         # 管理者の無料実行
        A._orders[oid]["admin_free"] = True
        self.finish("J2")
        self.assertEqual(self.c.get("/status/J2").get_json()["result"]["transfer"], "NEWCODE1")
        A._orders.pop(oid)
        self.started("J3")                                               # 実績の機能を止めているお店
        with patch.object(site_config, "REVIEWS_ENABLED", False):
            self.finish("J3")
            self.assertEqual(self.c.get("/status/J3").get_json()["result"]["transfer"], "NEWCODE1")
            self.assertFalse(self.mine()[0]["locked"])

    def test_two_save_failures_release_the_result(self):
        oid = self.started()
        self.finish()
        with patch.object(G, "review_add", side_effect=RuntimeError("db")), self.assertLogs("orders", level="ERROR"):
            r1 = self.c.post("/api/reviews", json={"stars": 5, "text": GOOD, "order": oid})
            self.assertEqual(r1.status_code, 503)
            self.assertIsNone(self.mine()[0]["result"])                  # 1回目：まだ渡さない
            r2 = self.c.post("/api/reviews", json={"stars": 5, "text": GOOD, "order": oid})
        self.assertEqual(r2.get_json(), {"ok": True, "saved": False, "waived": True})
        self.assertEqual(self.mine()[0]["result"]["transfer"], "NEWCODE1")

    def test_other_browser_cannot_unlock_or_read(self):
        oid = self.started()
        self.finish()
        other = A.app.test_client()
        self.assertEqual(other.get("/status/J1").status_code, 404)
        self.assertFalse(other.post("/api/reviews", json={"stars": 5, "text": GOOD, "order": oid}).get_json()["saved"])
        self.assertIsNone(self.mine()[0]["result"])


class OrderHousekeeping(Base):
    def test_gc_never_removes_orders_in_motion_and_ages_from_last_change(self):
        oid = self.started()
        A._orders[oid]["created"] = time.time() - 5 * 86400              # 何日も前の支払い済み注文を、いま再実行している
        A._gc_orders()
        self.assertIn(oid, A._orders)                                    # 実行中は消さない
        self.finish()
        A._gc_orders()
        self.assertIn(oid, A._orders)                                    # 完了した「今」から3日残す（作成からではない）
        A._orders[oid]["touched"] = time.time() - 4 * 86400
        A._gc_orders()
        self.assertNotIn(oid, A._orders)

    def test_reconcile_does_not_flip_an_order_that_just_started(self):
        oid = self.c.post("/run", json={"items": ["XP"]}).get_json()["order"]
        real = A._jobs_lock

        class JustStarted:
            """照合が実行の一覧を写す、その直前に実行が始まる状況を作る。"""
            def __enter__(self_inner):
                with self.c.session_transaction() as s:
                    owner = s["job_owner"]
                if A._orders[oid]["state"] != "started":
                    A._orders[oid].update(state="started", job_id="JX")
                    A._jobs["JX"] = {"status": "running", "error": None, "result": None, "finished": None,
                                     "created": time.time(), "owner": owner}
                return real.__enter__()

            def __exit__(self_inner, *a):
                return real.__exit__(*a)
        with patch.object(A, "_jobs_lock", JustStarted()):
            A._reconcile_orders()
        self.assertEqual((A._orders[oid]["state"], A._orders[oid].get("retry")), ("started", None))

    def test_rejected_link_is_not_kept_on_the_order(self):
        oid = self.c.post("/run", json={"items": ["XP"]}).get_json()["order"]
        with patch.object(G, "receive_payment", AsyncMock(side_effect=G.PayPayError("link_not_found", "見つかりません"))):
            r = self.c.post(f"/api/orders/{oid}/pay", json={"link": self.LINK})
        self.assertEqual(r.status_code, 402)
        self.assertEqual((A._orders[oid]["state"], A._orders[oid].get("link_code")), ("unpaid", None))

    def test_odd_request_bodies_do_not_crash(self):
        for path in ("/api/ticket/order", "/api/support/order", "/api/reviews", "/api/qa", "/api/jobs/x/captcha", "/api/source/order"):
            for body in ([1, 2], "text", 5, None):
                self.assertLess(self.c.post(path, json=body).status_code, 500, (path, body))
        huge = "9" * 5000
        for body in ({"chars": huge}, {"items": ["XP"], "qty": {"XP": huge}}, {"items": ["XP"], "playtime_add_min": huge},
                     {"chars": "1", "cat_settings": {huge: {}}}, {"chars": "1", "cat_settings": {"1": {"base_level": huge}}}):
            self.assertEqual(self.c.post("/run", json=body).status_code, 400, list(body))


class Coupons(Base):
    def test_coupon_applies_to_ban_guarantee_and_clone(self):
        code = G.coupon_issue("OLD", 30)
        r = self.c.post("/run", json={"transfer": "abc", "confirm": "1234", "integrity_only": True, "coupon": code}).get_json()
        self.assertEqual(r["amount"], A.PRICES["ban_guarantee"] * 9 // 10)
        free = G.coupon_issue_free(30)
        with patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="JC")):
            r = self.c.post("/run", json={"transfer": "abd", "confirm": "1234", "clone": True, "coupon": free}).get_json()
        self.assertEqual(r, {"job": "JC"})                               # 無料クーポンなら、支払いなしで開始

    def test_rejected_new_order_does_not_kill_the_old_one(self):
        code = G.coupon_issue("OLD", 30)
        first = self.c.post("/run", json={"chars": "1-5", "coupon": code}).get_json()["order"]   # 250円・クーポンを押さえる
        r = self.c.post("/run", json={"items": ["XP"], "coupon": code})                            # 25円 → 200円未満で断られる
        self.assertEqual((r.status_code, r.get_json()["code"]), (400, "coupon_min"))
        self.assertEqual(A._orders[first]["state"], "unpaid")                                      # 前の注文は生きている
        self.assertFalse(G.coupon_check(code)[0])                                                  # クーポンも前の注文が押さえたまま
        second = self.c.post("/run", json={"chars": "1-6", "coupon": code}).get_json()["order"]   # 通る注文なら、移る
        self.assertEqual((A._orders[first]["state"], A._orders[second]["coupon"]), ("dead", code))

    def test_used_coupon_reports_the_real_reason(self):
        code = G.coupon_issue("OLD", 30)
        G.coupon_consume(code, "X")
        r = self.c.post("/run", json={"items": ["XP"], "coupon": code}).get_json()
        self.assertEqual(r["code"], "coupon_invalid")                    # 「200円以上で…」ではなく「使用済み」と伝える
        self.assertIn("使われています", r["error"])

    def test_period_ticket_not_blocked_by_a_stalled_order_and_keeps_old_payment(self):
        code, _ = G.coupon_issue_kind("period", 7, "ticket:T1")
        other = A.app.test_client()
        stuck = other.post("/run", json={"items": ["XP"]}).get_json()["order"]
        A._orders[stuck].update(state="paid", retry=True, coupon=code, coupon_kind="period", free_coupon=True, amount=0)
        paid = self.c.post("/run", json={"items": ["NP"]}).get_json()["order"]
        A._orders[paid].update(state="paid", retry=True)                 # 自分には、以前の支払い済み（失敗）注文がある
        with patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="JP")):
            r = self.c.post("/run", json={"items": ["XP"], "coupon": code})
        self.assertEqual(r.get_json(), {"job": "JP"})                    # ほかの端末の止まった注文では塞がらない
        self.assertEqual((A._orders[paid]["state"], A._orders[paid].get("job_id")), ("paid", None))   # 以前の支払いは使わずに残す

    def test_once_ticket_order_can_be_retried_after_a_failed_start(self):
        free = G.coupon_issue_free(30)
        with patch.object(A, "_start_job", side_effect=lambda *a: (A.jsonify(error="full"), 429)):
            self.assertEqual(self.c.post("/run", json={"chars": "1-5", "coupon": free}).status_code, 429)
        o = next(iter(A._orders.values()))
        self.assertEqual((o["state"], o.get("retry")), ("paid", True))
        with patch.object(A, "_start_job", side_effect=lambda *a: A.jsonify(job="JR")):
            # コードが入ったまま、内容を直して送り直す（以前は「このクーポンはすでに使われています」で断っていた）
            r = self.c.post("/run", json={"chars": "1-6", "coupon": free}).get_json()
        self.assertTrue(r.get("prepaid"))                                 # 入力し直しても、その「無料」が使える


class LongLane(Base):
    """長時間の周回は専用の枠で動かし、止まっても続きから再開する。"""

    def puni_job(self, long=True, job_id=None):
        with A.app.test_request_context():
            job = {"status": "running", "step": 0, "log": [], "warn": [], "report": [], "result": None, "error": None,
                   "created": time.time(), "finished": None, "owner": "o", "mode": "puni", "count": 1, "dry": False,
                   "clone": False, "long": long}
            with patch.object(A, "_launch_job"), patch.object(A.uuid, "uuid4", return_value=type("U", (), {"hex": job_id})()):
                return A._start_job(job, ({"mode": "email", "login": job_id},), None)

    def test_classification(self):
        self.assertTrue(puni_service.is_long(["puni_unlimited"], {"puni_unlimited": 1}))
        self.assertTrue(puni_service.is_long(["puni_farm"], {"puni_farm": puni_service.LONG_FARM_LAPS}))
        self.assertFalse(puni_service.is_long(["puni_farm"], {"puni_farm": puni_service.LONG_FARM_LAPS - 1}))
        self.assertFalse(puni_service.is_long(["puni_missions"], {}))
        self.assertFalse(puni_service.is_long(["puni_farm"], {"puni_farm": "x"}))

    def test_long_jobs_do_not_take_normal_slots(self):
        self.puni_job(True, "L1")
        self.assertEqual((A._slots, A._long_slots), ([None, None], ["L1"]))
        self.puni_job(True, "L2")                                        # 長時間レーンがいっぱい → 順番待ち（ふつうの枠は使わない）
        self.assertEqual((A._jobs["L2"]["status"], A._slots), ("queued", [None, None]))
        self.puni_job(False, "S1")
        self.puni_job(False, "S2")
        self.assertEqual(A._slots, ["S1", "S2"])                         # 数分の注文は、長時間の待ちに関係なく進む
        with A._jobs_lock:
            state = A._parallel_state_locked()
        self.assertEqual((state["running_jobs"], state["queued_jobs"], state["long_running_jobs"], state["long_queued_jobs"]),
                         (2, 0, 1, 1))                                   # デプロイが待つのは、ふつうの枠だけ
        with patch.object(A, "_launch_job"):
            A._finish_job("L1", A.LONG_SLOT_BASE, None)                  # 前の周回が終わる → 待っていた長時間が始まる
        self.assertEqual((A._long_slots, A._jobs["L2"]["status"]), (["L2"], "running"))

    def test_long_queue_waits_longer_than_normal(self):
        self.puni_job(True, "L1")
        self.puni_job(True, "L2")
        A._jobs["L2"]["created"] = time.time() - A.QUEUE_TTL - 60
        A._gc_jobs()
        self.assertEqual(A._jobs["L2"]["status"], "queued")              # 15分では打ち切らない
        A._jobs["L2"]["created"] = time.time() - A.LONG_QUEUE_TTL - 60
        A._gc_jobs()
        self.assertEqual(A._jobs["L2"]["status"], "error")

    def test_progress_is_saved_and_carried_into_the_retry(self):
        body = {"puni": {"mode": "email", "login": "a@example.com", "password": "pw", "menus": ["puni_hitodama", "puni_farm"],
                         "qty": {"puni_hitodama": "100", "puni_farm": "300"}, "stages": {"puni_farm": "1001001"}}}
        oid = self.started("JL", body=body)
        o = A._orders[oid]
        self.assertTrue(o["job"]["long"])
        cp = {"finished": ["puni_hitodama"], "menu": "puni_farm", "done": 120, "elapsed": 0}
        A._jobs["JL"]["checkpoint"] = cp
        A._reconcile_orders()
        self.assertEqual(o["checkpoint"], cp)                            # 途中経過が注文に写る
        del A._jobs["JL"]                                                # サーバーの再起動で実行が消えた
        A._reconcile_orders()
        self.assertEqual((o["state"], o["retry"], o["auto_resume"]), ("paid", True, True))
        self.assertEqual(o["arguments"][0]["resume"], cp)                # 次の実行は、続きから
        row = self.c.get("/api/orders/mine").get_json()["orders"][0]
        self.assertEqual((row["long"], row["resume"]), (True, "120周まで完了"))
        back = A._order_from_json(A._order_to_json(o))                   # 保存して読み戻しても残る
        self.assertEqual(back["arguments"][0]["resume"], cp)

    def test_auto_resume_after_restart(self):
        body = {"puni": {"mode": "email", "login": "a@example.com", "password": "pw", "menus": ["puni_unlimited"],
                         "qty": {"puni_unlimited": "6"}, "stages": {"puni_unlimited": "1001001"}}}
        oid = self.started("JU", body=body)
        A._jobs["JU"]["checkpoint"] = {"finished": [], "menu": "puni_unlimited", "done": 40, "elapsed": 3600}
        A._reconcile_orders()
        del A._jobs["JU"]
        A._reconcile_orders()
        seen = {}

        def start(job, arguments, key):
            seen["resume"] = arguments[0].get("resume")
            return A.jsonify(job="JU2")
        with patch.object(A, "_start_job", side_effect=start), patch.object(A.time, "sleep"):
            A._auto_resume_orders()
        o = A._orders[oid]
        self.assertEqual((o["state"], o["job_id"], o["auto_resumed"], o.get("auto_resume")), ("started", "JU2", 1, None))
        self.assertEqual(seen["resume"]["elapsed"], 3600)                # 使った1時間は引いて、残りを回す
        o["auto_resumed"] = A.AUTO_RESUME_MAX                            # 何度も止まる注文を、いつまでも自動で始め直さない
        A._jobs.pop("JU2", None)
        o.update(state="started")
        A._reconcile_orders()
        self.assertFalse(o.get("auto_resume"))

    def test_short_orders_are_not_auto_resumed(self):
        oid = self.started("JS")
        del A._jobs["JS"]
        A._reconcile_orders()
        self.assertEqual((A._orders[oid]["state"], A._orders[oid].get("auto_resume")), ("paid", None))

    def test_stop_endpoint(self):
        self.started("JQ", mode="puni", long=True)
        sent = []
        pipe = type("P", (), {"send": lambda self_, m: sent.append(m)})()
        with patch.dict(A._job_pipes, {"JQ": pipe}):
            self.assertEqual(self.c.post("/api/jobs/JQ/stop").get_json(), {"ok": True})
            self.assertEqual(A.app.test_client().post("/api/jobs/JQ/stop").status_code, 404)   # 他人は止められない
        self.assertEqual(sent, [{"stop": True}])
        self.assertEqual(self.c.post("/api/jobs/JQ/stop").status_code, 409)                     # つながっていない
        self.started("JN")
        self.assertEqual(self.c.post("/api/jobs/JN/stop").status_code, 409)                     # にゃんこは対象外


class PuniResume(unittest.TestCase):
    """puni_service：続きからの再開・途中終了・済んだメニューを繰り返さない。"""

    def run_order(self, menus, qty, battles, resume=None, stop_after=None, fail_at=None):
        calls, points = [], []

        def battle(c_mod, client, stage, log):
            calls.append(stage)
            if fail_at is not None and len(calls) == fail_at:
                raise RuntimeError("network down")
        hitodama = []
        client = type("C", (), {"buy_hitodama": lambda self_, n: hitodama.append(n) or n})()
        with patch.object(puni_service, "core", return_value=object()), patch.object(puni_service, "_login", return_value=client), \
             patch.object(puni_service, "_stage_exists", return_value=True), patch.object(puni_service, "_battle_once", side_effect=battle):
            out = puni_service.run_order("email", "a@example.com", "pw", menus, qty, {"puni_farm": 1001001, "puni_unlimited": 1001001},
                                         resume=resume, checkpoint=points.append,
                                         should_stop=(lambda: len(calls) >= stop_after) if stop_after is not None else None)
        return out, calls, points, hitodama

    def test_farm_resumes_where_it_stopped(self):
        out, calls, points, _ = self.run_order(["puni_farm"], {"puni_farm": 10}, 10,
                                               resume={"finished": [], "menu": "puni_farm", "done": 7, "elapsed": 0})
        self.assertTrue(out["ok"])
        self.assertEqual(len(calls), 3)                                  # 残りの3周だけ
        self.assertEqual(points[-1]["finished"], ["puni_farm"])

    def test_finished_menus_are_not_repeated(self):
        out, calls, _, hitodama = self.run_order(["puni_hitodama", "puni_farm"], {"puni_hitodama": 100, "puni_farm": 5}, 5,
                                                 resume={"finished": ["puni_hitodama"], "menu": "puni_farm", "done": 2, "elapsed": 0})
        self.assertTrue(out["ok"])
        self.assertEqual((hitodama, len(calls)), ([], 3))                # ひとだまは買い直さない
        self.assertTrue(any("ひとだま補充：前回までに完了" in line for line in out["lines"]), out["lines"])

    def test_failure_midway_is_resumable_with_progress(self):
        out, calls, points, _ = self.run_order(["puni_farm"], {"puni_farm": 10}, 10, fail_at=5)
        self.assertEqual((out["ok"], out["delivered"], out["resumable"]), (False, True, True))
        self.assertEqual((points[-1]["menu"], points[-1]["done"]), ("puni_farm", 4))

    def test_customer_stop_ends_cleanly(self):
        out, calls, _, _ = self.run_order(["puni_farm", "puni_hitodama"], {"puni_farm": 100, "puni_hitodama": 100}, 100, stop_after=6)
        self.assertEqual((out["ok"], out["stopped"], len(calls)), (True, True, 6))
        self.assertIn("途中で終了", out["lines"][0])
        self.assertEqual(len(out["lines"]), 1)                           # 終了を選んだあとのメニューは実行しない

    def test_unlimited_counts_only_time_actually_run(self):
        clock = {"t": 1000.0}

        def battle(c_mod, client, stage, log):
            clock["t"] += 600                                            # 1周10分
        with patch.object(puni_service, "core", return_value=object()), patch.object(puni_service, "_login", return_value=object()), \
             patch.object(puni_service, "_stage_exists", return_value=True), patch.object(puni_service, "_battle_once", side_effect=battle), \
             patch("time.time", side_effect=lambda: clock["t"]):
            out = puni_service.run_order("email", "a@example.com", "pw", ["puni_unlimited"], {"puni_unlimited": 1},
                                         {"puni_unlimited": 1001001},
                                         resume={"finished": [], "menu": "puni_unlimited", "done": 3, "elapsed": 1800})
        self.assertTrue(out["ok"])
        self.assertIn("6 周", out["lines"][0])                           # 30分ぶんは済み → 残り30分＝3周だけ回す（3+3）


if __name__ == "__main__":
    unittest.main()
