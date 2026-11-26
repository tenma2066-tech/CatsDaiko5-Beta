"""v1.26（お店全体のバグ確認）で直した点の再発防止テスト。

- 支払い済みの充当：一部を提供済みの注文は別の内容に充てない／安くなるときは先に聞く／期間チケットは充てない
- 結果の扱い：実行中は見せない／コードの出ない注文の控えを残す／結果不明の注文を一覧に出す
- 壊れた入力（単独のサロゲート・深すぎる入れ子）で止まらない
- クーポンの二重使用／無料クーポンの同時注文
- 実行枠：新規作成どうしは並べて動かす／取り残された実行が握っている枠は使わない
- そのほか（保留の自動確認の順番・新キャラ・図鑑の更新・2段階認証・言葉づかいの誤検知）
PayPay・ゲームへの通信はモック。"""
import fcntl
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

import admin_2fa
import app as A
import engine
import paypay_gateway as G
import review_ai
import review_filter
import site_config

GOOD = "注文から5分で完了しました。案内が分かりやすくて安心でした。"
PUNI = {"mode": "email", "login": "a@example.com", "password": "pw", "menus": ["puni_hitodama", "puni_farm"],
        "qty": {"puni_hitodama": "100", "puni_farm": "300"}, "stages": {"puni_farm": "1001001"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(A, "PAYMENTS_ENABLED", True), patch.object(site_config, "REVIEWS_ENABLED", True),
            patch.object(A, "ADMIN_PASSWORD", "pass-1234"),
            patch.dict(A._orders, {}, clear=True), patch.dict(A._jobs, {}, clear=True), patch.dict(A._fail, {}, clear=True),
            patch.object(A, "_slots", [None] * 2), patch.object(A, "_long_slots", [None] * 1),
            patch.object(A, "_pending_jobs", type(A._pending_jobs)()),
            patch.object(G, "history_upsert"), patch.object(G, "mark_used"),
            patch.object(G, "_LEDGER_FILE", Path(self.tmp.name) / "l.sqlite3"),
            patch.object(A, "_persist_orders"), patch.object(A, "_rate_ok", return_value=True),
            patch.object(review_ai, "judge", return_value=(None, "")),
            patch.dict(os.environ, {"PAYPAY_DATA_DIR": self.tmp.name, "PAYPAY_ENCRYPTION_KEY": Fernet.generate_key().decode()}),
        ]
        for p in self.patches:
            p.start()
        self.c = A.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def order(self, body=None, c=None):
        return (c or self.c).post("/run", json=body or {"items": ["XP"]})

    def owner(self, c=None):
        with (c or self.c).session_transaction() as s:
            return s["job_owner"]

    def failed_paid(self, body, **more):
        """支払い済みで、実行に失敗して「もう一度実行できる」状態に戻った注文を1件作る。"""
        oid = self.order(body).get_json()["order"]
        A._orders[oid].update(state="paid", retry=True, **more)
        return oid

    def started(self, job_id="J1", body=None, **job_fields):
        oid = self.order(body).get_json()["order"]
        A._orders[oid].update(state="started", job_id=job_id)
        A._jobs[job_id] = dict({"status": "running", "error": None, "result": None, "finished": None, "created": time.time(),
                                "owner": self.owner(), "step": 1, "log": [], "warn": [], "report": [], "dry": False, "clone": False,
                                "mode": "download", "count": 1}, **job_fields)
        return oid

    def mine(self, c=None):
        return (c or self.c).get("/api/orders/mine").get_json()["orders"]

    def admin(self, action, c=None, **data):
        c = c or self.c
        c.get("/admin")
        with c.session_transaction() as s:
            token = s["admin_csrf"]
        return c.post("/admin", data=dict(data, csrf=token, action=action))


class Credit(Base):
    """支払い済みで実行できなかった注文の支払いを、次の注文に充てるときの決まり。"""

    def test_same_amount_is_used_without_asking(self):
        old = self.failed_paid({"items": ["XP"]})
        r = self.order({"items": ["NP"]}).get_json()
        self.assertEqual((r["order"], r.get("prepaid")), (old, True))

    def test_cheaper_order_asks_first(self):
        old = self.failed_paid({"items": ["XP", "NP", "猫缶"]})                    # 75円
        r = self.order({"items": ["XP"]})                                          # 25円
        self.assertEqual((r.status_code, r.get_json()["code"]), (409, "credit_confirm"))
        self.assertEqual((r.get_json()["paid_amount"], r.get_json()["amount"]), (75, 25))
        self.assertEqual(len(A._orders), 1)                                        # 聞いただけ。注文は増えない・前の注文もそのまま
        self.assertEqual(len(A._orders[old]["breakdown"][0]) and A._orders[old]["breakdown"][0]["count"], 3)
        no = self.c.post("/run", json={"items": ["XP"], "use_credit": False}).get_json()
        self.assertNotEqual(no["order"], old)                                      # 充てない＝新しく支払う。前の注文は残る
        self.assertEqual((A._orders[no["order"]]["state"], A._orders[old]["state"]), ("unpaid", "paid"))
        yes = self.c.post("/run", json={"items": ["XP"], "use_credit": True}).get_json()
        self.assertEqual((yes["order"], yes["prepaid"]), (old, True))

    def test_dearer_order_is_never_covered(self):
        old = self.failed_paid({"items": ["XP"]})
        r = self.order({"items": ["XP", "NP"]}).get_json()
        self.assertNotEqual(r["order"], old)
        self.assertNotIn("prepaid", r)

    def test_other_department_is_not_touched(self):
        old = self.failed_paid({"items": ["XP", "NP"]})                            # にゃんこ 50円
        r = self.order({"puni": dict(PUNI, menus=["puni_hitodama"], qty={"puni_hitodama": "100"}, stages={})}).get_json()   # ぷにぷに 50円
        self.assertNotEqual(r["order"], old)
        self.assertEqual(A._orders[old]["state"], "paid")

    def test_partly_delivered_order_only_covers_the_same_content(self):
        old = self.failed_paid({"puni": PUNI}, delivered=True, checkpoint={"finished": ["puni_hitodama"], "menu": "puni_farm", "done": 120, "elapsed": 0})
        other = self.order({"puni": dict(PUNI, menus=["puni_farm"], qty={"puni_farm": "300"})})   # 済んだメニューを外した別の内容（安い）
        self.assertEqual(other.status_code, 200)
        self.assertNotEqual(other.get_json()["order"], old)                        # 充てない（聞きもしない）＝1回の支払いで2回受け取れない
        self.assertNotIn("prepaid", other.get_json())
        again = self.order({"puni": dict(PUNI, login="b@example.com")}).get_json() # ログイン情報の直し＝同じ内容
        self.assertEqual((again["order"], again["prepaid"]), (old, True))
        self.assertEqual(A._orders[old]["arguments"][0]["resume"]["done"], 120)    # 済んだ分は繰り返さず、続きから
        self.assertEqual(A._orders[old]["arguments"][0]["login"], "b@example.com")

    def test_period_ticket_order_is_not_a_credit(self):
        old = self.failed_paid({"items": ["XP"]}, coupon_kind="period", free_coupon=True, coupon="PS-AAAA")
        r = self.order({"items": ["XP"]}).get_json()
        self.assertNotEqual(r["order"], old)
        self.assertNotIn("prepaid", r)

    def test_free_coupon_order_needs_the_code_again(self):
        old = self.failed_paid({"items": ["XP"]}, free_coupon=True, coupon="FR-AAAA2222", coupon_used=True)
        A._orders[old]["amount"] = 0
        plain = self.order({"items": ["XP"]}).get_json()
        self.assertNotEqual(plain["order"], old)                                   # コードを入れなければ、ふつうの注文（お金のかかる注文に無料は充てない）
        back = self.c.post("/run", json={"items": ["XP", "NP"], "coupon": "FR-AAAA2222"}).get_json()
        self.assertEqual((back["order"], back["prepaid"]), (old, True))            # 同じコードを入れ直せば、金額に関係なく充てる

    def test_link_is_not_moved_off_a_partly_delivered_order(self):
        old = self.failed_paid({"puni": PUNI}, delivered=True, link_code="abcDEF123456")
        new = self.order({"items": ["XP"]}).get_json()["order"]
        A._orders[new]["link_code"] = "abcDEF123456"
        with patch.object(G, "adopt_link") as adopt, self.assertRaises(G.PayPayError) as err:
            A._adopt_existing_link(new, A._orders[new])
        self.assertEqual(err.exception.code, "duplicate")
        adopt.assert_not_called()
        self.assertEqual(A._orders[old]["state"], "paid")


class Results(Base):
    def test_running_job_never_shows_a_result(self):
        self.started(result={"transfer": "HALFWAY1", "confirm": "1111"})
        st = self.c.get("/status/J1").get_json()
        self.assertEqual((st["status"], st["result"]), ("running", None))
        self.assertNotIn("HALFWAY1", self.c.get("/status/J1").get_data(as_text=True))

    def test_empty_frame_is_not_a_result(self):
        self.assertFalse(A._has_result(None))
        self.assertFalse(A._has_result({"transfer": None, "confirm": None}))       # 複製：発行の前に作る空の枠
        self.assertTrue(A._has_result({"transfer": "ABC", "confirm": None}))
        self.assertTrue(A._has_result([{"email": "a@example.com"}]))

    def test_failure_before_issue_can_be_rerun(self):
        oid = self.started(result={"transfer": None, "confirm": None}, clone=True)
        A._jobs["J1"].update(status="error", error="失敗", finished=time.time())
        A._reconcile_orders()
        self.assertEqual((A._orders[oid]["state"], A._orders[oid]["retry"]), ("paid", True))   # 空の枠だけ＝何も渡していない → やり直せる

    def test_outcome_is_kept_and_waits_for_the_review(self):
        oid = self.started(body={"puni": PUNI}, mode="puni", puni={"lines": ["■ ひとだま補充：100", "■ 自動周回：300周"], "done": 2, "menus": ["ひとだま補充"]})
        A._jobs["J1"].update(status="done", finished=time.time())
        A._reconcile_orders()
        o = A._orders[oid]
        self.assertEqual(o["outcome"]["lines"][1], "■ 自動周回：300周")
        row = self.mine()[0]
        self.assertEqual((row["state"], row["locked"], row["outcome"]), ("done", True, None))  # 評価を書くまでは控えも渡さない
        A._jobs.clear()                                                             # 実行の記録が消えたあと（30分後・再起動後）
        self.assertTrue(self.c.post("/api/reviews", json={"stars": 5, "text": GOOD, "order": oid}).get_json()["saved"])
        row = self.mine()[0]
        self.assertEqual((row["locked"], row["outcome"]["lines"][0]), (False, "■ ひとだま補充：100"))

    def test_done_without_anything_to_hand_over_is_not_locked(self):
        oid = self.started()
        A._jobs["J1"].update(status="done", finished=time.time())                   # 結果も控えも無い完了（修復不要 など）
        A._reconcile_orders()
        self.assertFalse(A._review_locked(A._orders[oid]))
        self.assertFalse(self.c.get("/status/J1").get_json()["result_locked"])

    def test_partial_delivery_is_remembered_on_failure(self):
        oid = self.started(body={"puni": PUNI}, mode="puni", puni={"lines": ["■ ひとだま補充：100"], "done": 1, "menus": []})
        A._jobs["J1"].update(status="error", error="通信エラー", finished=time.time())
        A._reconcile_orders()
        o = A._orders[oid]
        self.assertEqual((o["state"], o["retry"], o["delivered"]), ("paid", True, True))
        oid2 = self.started("J2")                                                   # にゃんこの失敗は、何も渡していない
        A._jobs["J2"].update(status="error", error="失敗", finished=time.time())
        A._reconcile_orders()
        self.assertNotIn("delivered", A._orders[oid2])

    def test_unknown_outcome_is_listed_for_the_customer_and_admin(self):
        oid = self.started(outcome_unknown=True)
        A._jobs["J1"].update(status="error", error="発行の途中で切れました", finished=time.time())
        A._reconcile_orders()
        self.assertTrue(A._orders[oid]["diag"].startswith("結果不明"))
        row = self.mine()[0]
        self.assertEqual((row["order"], row["state"], row["unknown"], row["locked"]), (oid, "done", True, False))
        self.admin("unlock", passcode="pass-1234")
        page = self.c.get("/admin").get_data(as_text=True)
        self.assertIn("結果不明", page)
        self.assertIn(oid[:8], page)

    def test_claim_moves_the_job_too(self):
        oid = self.started()
        A._jobs["J1"].update(status="done", finished=time.time(), result={"transfer": "NEWCODE1", "confirm": "4321"})
        A._reconcile_orders()
        other = A.app.test_client()
        self.assertEqual(other.post(f"/api/orders/{oid}/claim").status_code, 200)
        self.assertEqual(A._jobs["J1"]["owner"], self.owner(other))
        self.assertEqual(other.get("/status/J1").status_code, 200)                  # 呼び戻した先で「見つかりません」にならない

    def test_review_lands_on_one_order_only(self):
        first = self.started("J1")
        A._jobs["J1"].update(status="done", finished=time.time(), result={"transfer": "CODE0001", "confirm": "1"})
        A._reconcile_orders()
        second = self.started("J2")
        A._jobs["J2"].update(status="done", finished=time.time(), result={"transfer": "CODE0002", "confirm": "2"})
        A._reconcile_orders()

        def slow_judge(text, stars):   # AI の確認を待つあいだに、同じ送信がもう1つ先に通った
            A._orders[first]["reviewed"] = True
            return None, ""
        with patch.object(review_ai, "judge", side_effect=slow_judge):
            r = self.c.post("/api/reviews", json={"stars": 5, "text": GOOD, "order": first}).get_json()
        self.assertEqual(r, {"ok": True, "saved": False})
        self.assertFalse(A._orders[second].get("reviewed"))                         # 1つの感想で、別の注文まで開かない


class BrokenInput(Base):
    def test_scrub(self):
        self.assertEqual(A._scrub_json({"a\ud800": ["x\udfffy", 1, None, True]}), {"a": ["xy", 1, None, True]})
        deep = cur = []
        for _ in range(40):
            cur.append([])
            cur = cur[0]
        self.assertIsNone(A._scrub_json({"k": deep})["k"][0][0][0][0][0][0][0][0][0][0][0])

    def test_lone_surrogate_does_not_break_the_order_or_the_save(self):
        r = self.c.post("/run", data='{"items": ["XP"], "transfer": "ab\\ud800cd", "coupon": "\\udc00"}', content_type="application/json")
        self.assertLess(r.status_code, 500)
        for o in A._orders.values():
            repr(o).encode("utf-8")                                                 # 保存できる形（UTF-8 にできる）で入っている
        self.assertLess(self.c.post("/api/reviews", data='{"stars": 5, "text": "' + "\\ud83d" * 30 + '"}',
                                    content_type="application/json").status_code, 500)
        self.assertLess(self.c.post("/api/box", data='{"kind": "opinion", "text": "こんにちは\\ud800。ご意見です、よろしくお願いします。"}',
                                    content_type="application/json").status_code, 500)

    def test_very_deep_json_is_refused_politely(self):
        body = "[" * 100000 + "]" * 100000
        for url in ("/run", "/api/reviews", "/api/box", "/api/qa"):
            self.assertLess(self.c.post(url, data=body, content_type="application/json").status_code, 500, url)
        nested = '{"items": ["XP"], "cat_settings": ' + '{"a": ' * 60 + "1" + "}" * 60 + "}"
        self.assertLess(self.c.post("/run", data=nested, content_type="application/json").status_code, 500)

    def test_orders_are_saved_even_with_an_odd_character(self):
        with patch.object(G, "_ROOT", Path(self.tmp.name)), patch.object(G, "_ORDERS_FILE", Path(self.tmp.name) / "orders.enc"), \
             patch.object(G.github_runtime_store, "enabled", return_value=False):
            G.save_orders({"x": {"summary": "ab\ud800cd", "created": 1}})         # 例外で保存をやめない（全員の注文が保存できなくなるのを防ぐ）
            self.assertTrue((Path(self.tmp.name) / "orders.enc").stat().st_size > 0)


class Coupons(Base):
    def test_consume_only_by_the_order_that_holds_it(self):
        code = G.coupon_issue("src-order", 30)
        self.assertTrue(G.coupon_reserve(code, "order-A"))
        self.assertFalse(G.coupon_consume(code, "order-B"))                         # ほかの注文が押さえている
        self.assertTrue(G.coupon_consume(code, "order-A"))
        self.assertFalse(G.coupon_consume(code, "order-A"))                         # もう使用済み
        self.assertFalse(G.coupon_consume("NK-NOPE0000", "order-A"))

    def test_free_order_does_not_start_when_the_coupon_went_elsewhere(self):
        oid = self.order().get_json()["order"]
        o = A._orders[oid]
        o.update(state="paid", free_coupon=True, coupon="FR-AAAA2222", coupon_kind="free", amount=0)
        with patch.object(G, "coupon_consume", return_value=False), patch.object(A, "_start_job") as start, A.app.test_request_context():
            body, status = A._start_paid_order(o)
        self.assertEqual((status, body.get_json()["code"], o["state"]), (409, "coupon_invalid", "dead"))
        start.assert_not_called()

    def test_dead_order_gives_its_coupon_back(self):
        code = G.coupon_issue("src-order", 30)
        G.coupon_reserve(code, "order-A")
        A._release_unused_coupon("order-A", {"coupon": code})
        self.assertTrue(G.coupon_check(code)[0])
        G.coupon_reserve(code, "order-A")
        A._release_unused_coupon("order-A", {"coupon": code, "coupon_used": True})  # 使ったあとのものは戻さない
        self.assertFalse(G.coupon_check(code)[0])

    def test_period_ticket_order_holds_its_turn_from_the_start(self):
        code, _ = G.coupon_issue_kind("period", 7, "src-order")
        seen = []
        with patch.object(A, "_start_paid_order", side_effect=lambda o: seen.append(o["state"]) or A.jsonify(job="J")):
            self.c.post("/run", json={"items": ["XP"], "coupon": code})
        self.assertEqual(seen, ["paid"])


class Slots(Base):
    def test_account_creation_orders_do_not_share_one_lock(self):
        self.assertEqual(A._lock_subject({"mode": "tsum"}, ({"login": ""},)), "")
        self.assertEqual(A._lock_subject({"mode": "puni"}, ({"mode": "new"},)), "")
        self.assertEqual(A._lock_subject({"mode": "puni"}, ({"login": "a@example.com"},)), "puni:a@example.com")
        self.assertEqual(A._lock_subject({"mode": "download"}, ("abc123", "1234")), "abc123")

    def test_slot_still_held_by_a_leftover_worker_is_skipped(self):
        with patch.dict(os.environ, {"BCSFE_PARALLEL_ROOT": self.tmp.name}):
            os.makedirs(os.path.join(self.tmp.name, "locks"))
            self.assertFalse(A._slot_held_elsewhere(0))
            with open(os.path.join(self.tmp.name, "locks", "slot-0"), "a") as held:
                fcntl.flock(held, fcntl.LOCK_EX)                                    # 取り残された前の実行が握っている
                self.assertTrue(A._slot_held_elsewhere(0))
                with A._jobs_lock:
                    self.assertEqual(A._free_slot_locked(False), 1)                 # 握られていない枠を選ぶ
            self.assertFalse(A._slot_held_elsewhere(0))
            with A._jobs_lock:
                self.assertEqual(A._free_slot_locked(False), 0)

    def test_worker_notices_a_dead_parent(self):
        with patch.dict(A._worker_parent, {"pid": None}):
            self.assertFalse(A._parent_gone())                                      # 実行用のプロセスの外では、いつも False
        with patch.dict(A._worker_parent, {"pid": os.getppid()}):
            self.assertFalse(A._parent_gone())
        with patch.dict(A._worker_parent, {"pid": os.getppid() + 1}):
            self.assertTrue(A._parent_gone())


class HoldWatcher(Base):
    def test_every_held_order_gets_its_turn(self):
        now = time.time()
        for i in range(8):
            A._orders[f"H{i}"] = {"owner": "o", "state": "hold", "link_code": f"code{i}", "created": now - 1000 + i, "amount": 100}
        seen = []

        class Stop(BaseException):
            pass
        ticks = iter([None, None, Stop()])

        def sleep(_):
            step = next(ticks)
            if step:
                raise step
        with patch.object(A.time, "sleep", side_effect=sleep), patch.object(A, "_recheck_and_start", side_effect=lambda k, who: seen.append(k)), \
             patch.object(A, "_draining", return_value=False), self.assertRaises(Stop):
            A._hold_watcher()
        self.assertEqual(seen[:5], ["H0", "H1", "H2", "H3", "H4"])
        self.assertEqual(set(seen[5:8]), {"H5", "H6", "H7"})                        # 2回目は、まだ見ていないものから（古い5件だけを見続けない）


class Cats(Base):
    def setUp(self):
        super().setUp()
        self.saved = dict(A._names)
        A._set_names({0: ["ネコ"], 881: ["比古清十郎"]}, "test", "15.7.2")

    def tearDown(self):
        A._names.clear()
        A._names.update(self.saved)
        super().tearDown()

    def test_new_cats_need_a_transfer(self):
        new = min(engine.SUPPLEMENT_CATS) + A.DB_OFFSET
        r = self.c.post("/run", json={"items": ["XP"], "chars": str(new)})
        self.assertEqual((r.status_code, r.get_json()["code"]), (400, "new_cat_needs_transfer"))
        self.assertEqual(self.c.post("/run", json={"items": ["XP"], "chars": "1"}).status_code, 200)

    def test_refresh_keeps_the_list_everyone_is_using(self):
        started = []
        with patch.object(A.threading, "Thread", side_effect=lambda **kw: started.append(kw) or type("T", (), {"start": lambda s: None})()):
            r = self.c.post("/api/cats/refresh")
        self.assertEqual(r.get_json()["state"], "ready")
        self.assertTrue(A._names["items"])                                          # 作り直しているあいだも、検索は空にならない
        self.assertEqual(started[0]["kwargs"], {"keep": True})
        A._names["refreshing"] = False


class TwoStep(Base):
    def test_full_width_digits_are_read(self):
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        code = admin_2fa.code_at(secret, 59 // 30)
        wide = code.translate(str.maketrans("0123456789", "０１２３４５６７８９"))
        self.assertEqual(admin_2fa._match(secret, wide, now=59), 59 // 30)
        self.assertEqual(admin_2fa._match(secret, " ".join(code), now=59), 59 // 30)
        for bad in ("", "１２３", "²³¹²³¹", "٣٣٣٣٣٣", "abcdef", None):                 # 例外にせず、ただ合わない
            self.assertIsNone(admin_2fa._match(secret, bad, now=59))

    def test_setup_cannot_be_restarted_once_on(self):
        admin_2fa.cancel()
        admin_2fa._last_counter["value"] = 0
        act = self.admin
        act("unlock", passcode="pass-1234")
        act("tfa_begin")
        secret = admin_2fa._pending["secret"]
        act("tfa_confirm", code=admin_2fa.code_at(secret, int(time.time() // 30)))
        self.assertTrue(admin_2fa.enabled())
        act("tfa_begin")                                                            # 有効なあいだは、新しい鍵を作らせない（盗んだログインで鍵を差し替えられない）
        self.assertFalse(admin_2fa._pending.get("secret"))
        self.assertEqual(admin_2fa._load(), secret)
        for _ in range(5):
            act("tfa_disable", code="000000")
        act("tfa_disable", code=admin_2fa.code_at(secret, int(time.time() // 30) + 1))
        self.assertTrue(admin_2fa.enabled())                                        # 総当たりは止める


class Words(Base):
    def test_ordinary_words_are_not_taken_for_abuse(self):
        for text in ("プレイヤーとして助かりました。プレミアムガチャも回せて満足です。", "ボスを瞬殺できるようになりました。本当にありがとうございます。",
                     "メキシコ旅行の前に頼みました。時差ボケでも分かる案内でした。", "注文が殺到しているのに早かったです。雑魚敵で試しました。"):
            self.assertTrue(review_filter.check(text)[0], text)
        for text in ("クソみたいなサービスでした。二度と使いたくないです。", "店主は死ねばいいと思います。本当に最悪の対応でした。"):
            self.assertFalse(review_filter.check(text)[0], text)

    def test_box_allows_links_but_not_abuse(self):
        ok = self.c.post("/api/box", json={"kind": "opinion", "text": "この画面（https://example.com/page）のように、進み具合を出してほしいです。"})
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.get_json().get("ok"))
        self.assertEqual(self.c.post("/api/box", json={"kind": "opinion", "text": "クソみたいなお店ですね。早く閉めたほうがいいと思います。"}).status_code, 400)
        self.assertEqual(self.c.post("/api/box", json={"kind": "opinion", "text": "　" * 30}).status_code, 400)   # 全角の空白だけ


class Served(Base):
    def test_static_path_with_a_trailing_slash(self):
        self.assertLess(self.c.get("/static/shop.js/").status_code, 500)
        self.assertEqual(self.c.get("/static/shop.js").status_code, 200)

    def test_pages_still_render(self):
        for url in ("/", "/nyanko", "/tsum", "/puni", "/ticket", "/support", "/reviews", "/box"):
            self.assertEqual(self.c.get(url).status_code, 200, url)


if __name__ == "__main__":
    unittest.main()
