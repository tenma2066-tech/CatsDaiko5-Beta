"""保留の一連の流れを、アプリ全体（注文API＋本物の決済処理）で通す。PayPayとの通信だけ偽物。

本番の記録で確認した実際の動き:
  受け取りPOST → 確定しない応答 / リンク情報 → PENDING＋isLinkBlocked / 送金者が解除 → SUCCESS（自動で入金）
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import app as A
import paypay_gateway as G
from test_paypay_hold import link_body, StatusResp

LINK = "https://pay.paypay.ne.jp/holdE2E123456"
CODE = "holdE2E123456"


class ScriptedPayPay:
    """phase に応じて応答を変える偽のPayPay。"""
    def __init__(self, amount):
        self.amount, self.phase, self.posts, self.gets = amount, "fresh", 0, 0

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, *a, **k):
        self.gets += 1
        if self.phase in ("fresh", "released_pending"):
            body = link_body("PENDING", self.amount)
        elif self.phase == "blocked":
            body = link_body("PENDING", self.amount)
            body["payload"]["pendingP2PInfo"]["isLinkBlocked"] = True
        elif self.phase == "success":
            body = link_body("SUCCESS", self.amount)
        elif self.phase == "cancelled":
            body = link_body("CANCELED", self.amount)
        elif self.phase == "down":
            return StatusResp(503)
        return StatusResp(200, body)

    def post(self, *a, **k):
        self.posts += 1
        if self.phase == "fresh":      # 最初の受け取り → 保留（確定しない応答）
            self.phase = "blocked"
            return StatusResp(200, {"header": {"resultCode": "S5000"}, "payload": None})
        if self.phase == "released_pending":  # 解除後、もう一度受け取れるパターン
            self.phase = "success"
            return StatusResp(200, {"header": {"resultCode": "S0000"}, "payload": {"orderStatus": "COMPLETED"}, "error": None})
        return StatusResp(400, {"header": {"resultCode": "X"}})


class HoldEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.started = []

        def fake_start(job, arguments, transfer_key):
            jid = "JOB%d" % (len(self.started) + 1)
            self.started.append(jid)
            A._jobs[jid] = dict(job, status="running", finished=None, created=time.time(), error=None, result=None)
            return A.jsonify(job=jid)

        self.ps = [
            patch.object(A, "PAYMENTS_ENABLED", True), patch.dict(A._orders, {}, clear=True), patch.dict(A._jobs, {}, clear=True),
            patch.dict(A._fail, {}, clear=True),
            patch.object(G, "_LEDGER_FILE", Path(self.tmp.name) / "l.sqlite3"), patch.dict(os.environ, {"RUNTIME_GITHUB_REPO": ""}),
            patch.object(G, "save_orders"),
            patch.object(G, "_fresh_account", AsyncMock(return_value={"uuid": "u", "session_cookie": "c"})),
            patch.object(A, "_start_job", side_effect=fake_start),
        ]
        for p in self.ps:
            p.start()
        self.c = A.app.test_client()
        r = self.c.post("/run", json={"items": ["XP"], "subs": ["全キャラレベルMAX"]}).get_json()
        self.oid, self.amount = r["order"], r["amount"]
        self.pp = ScriptedPayPay(self.amount)
        self.net = patch.object(G.aiohttp, "ClientSession", self.pp)
        self.net.start()

    def tearDown(self):
        self.net.stop()
        for p in reversed(self.ps):
            p.stop()
        self.tmp.cleanup()

    def pay(self, link=LINK, client=None, oid=None):
        r = (client or self.c).post(f"/api/orders/{oid or self.oid}/pay", json={"link": link} if link else {})
        return r.status_code, r.get_json()

    def state(self, oid=None):
        return A._orders[oid or self.oid]["state"]

    def go_on_hold(self):
        code, j = self.pay()
        self.assertEqual((code, j["hold"], self.state()), (402, True, "hold"), j)
        self.assertEqual(self.pp.posts, 1)

    # ---- 1) 保留になる・保留中は何度確認しても止まらない・再送金(POST)もしない
    def test_hold_then_waiting_never_kills_or_reposts(self):
        self.go_on_hold()
        for _ in range(12):  # 利用者の画面の自動確認＋ボタン連打
            code, j = self.pay(link=None)
            self.assertEqual((code, j["code"], j["hold"]), (402, "temporary_hold", True))
        for _ in range(3):   # サーバー側の自動確認
            self.assertEqual(A._recheck_and_start(self.oid, "auto"), "wait:temporary_hold")
        self.assertEqual((self.state(), self.pp.posts, self.started), ("hold", 1, []))
        self.assertEqual(self.c.get("/api/orders/mine").get_json()["orders"][0]["state"], "hold")

    # ---- 2) 解除 → 自動で入金済み(SUCCESS) → 利用者が画面を開いたまま
    def test_release_success_with_customer_watching(self):
        self.go_on_hold()
        self.pp.phase = "success"
        code, j = self.pay(link=None)
        self.assertEqual((code, j.get("job")), (200, "JOB1"), j)
        self.assertEqual((self.state(), self.pp.posts), ("started", 1))

    # ---- 3) 解除 → 利用者は画面を閉じている → サーバーが見つけて実行 → あとで開くとコードが見られる
    def test_release_success_while_customer_away(self):
        self.go_on_hold()
        self.pp.phase = "success"
        self.assertEqual(A._recheck_and_start(self.oid, "auto"), "started")
        self.assertEqual((self.state(), self.started), ("started", ["JOB1"]))
        mine = self.c.get("/api/orders/mine").get_json()["orders"]
        self.assertEqual((mine[0]["state"], mine[0]["job"]), ("started", "JOB1"))  # 開けば進み具合に切り替わる
        self.assertEqual(self.c.get("/status/JOB1").status_code, 200)             # その実行は本人のものとして見える
        A._jobs["JOB1"].update(status="done", result={"transfer": "NEWcode99", "confirm": "4321"}, finished=time.time())
        A._reconcile_orders()
        del A._jobs["JOB1"]  # 表示期限切れ・再起動
        mine = self.c.get("/api/orders/mine").get_json()["orders"]
        # 完了した。結果は、評価と感想を送るまで渡さない（locked）→ 送ると届く
        self.assertEqual((mine[0]["state"], mine[0]["locked"], mine[0]["result"]), ("done", True, None))
        with patch.object(G, "review_add", return_value=True):
            self.c.post("/api/reviews", json={"stars": 5, "text": "すぐに完了しました。案内も分かりやすくて安心でした。", "order": self.oid})
        mine = self.c.get("/api/orders/mine").get_json()["orders"]
        self.assertEqual((mine[0]["state"], mine[0]["result"]["transfer"]), ("done", "NEWcode99"))
        self.assertEqual(self.pay(link=None)[0], 409)  # 完了後に同じ注文をもう一度は実行できない

    # ---- 4) 解除後、未受取のまま（ブロック解除）→ 同じリンクでもう一度受け取る
    def test_release_pending_is_accepted_again_once(self):
        self.go_on_hold()
        self.pp.phase = "released_pending"
        self.assertEqual(A._recheck_and_start(self.oid, "auto"), "started")
        self.assertEqual((self.pp.posts, self.started), (2, ["JOB1"]))

    # ---- 5) 解除後に、利用者が同じリンクを同じ注文に貼り直す
    def test_repaste_same_link_same_order_after_release(self):
        self.go_on_hold()
        self.pp.phase = "success"
        A._orders[self.oid]["state"] = "unpaid"  # 旧バージョンで「支払い待ち」に戻された状態も含めて
        code, j = self.pay(LINK)
        self.assertEqual((code, j.get("job")), (200, "JOB1"), j)

    # ---- 6) 解除後に、利用者が新しく注文し直して同じリンクを貼る（同じ金額）
    def test_new_order_with_same_link_after_release(self):
        self.go_on_hold()
        self.pp.phase = "success"
        new = self.c.post("/run", json={"items": ["NP"], "subs": ["全キャラ最高形態"]}).get_json()
        self.assertEqual(new["amount"], self.amount)
        code, j = self.pay(LINK, oid=new["order"])
        self.assertEqual((code, j.get("job")), (200, "JOB1"), j)
        self.assertEqual(self.state(), "dead")  # 古い注文は無効（二重に実行されない）
        self.assertEqual(self.pay(link=None)[0], 409)
        self.assertEqual(A._recheck_and_start(self.oid, "auto"), "skip")
        orders = self.c.get("/api/orders/mine").get_json()["orders"]
        self.assertEqual([o["order"] for o in orders], [new["order"]])  # 古い注文は利用者の画面に出さない
        self.assertEqual(self.started, ["JOB1"])

    # ---- 7) まだ保留中に、新しい注文で同じリンクを貼る → 新しい注文が保留を引き継ぐ
    def test_new_order_with_same_link_while_still_held(self):
        self.go_on_hold()
        new = self.c.post("/run", json={"items": ["NP"], "subs": ["全キャラ最高形態"]}).get_json()
        code, j = self.pay(LINK, oid=new["order"])
        self.assertEqual((code, j["hold"], self.state(new["order"]), self.state()), (402, True, "hold", "dead"), j)
        self.pp.phase = "success"
        self.assertEqual(A._recheck_and_start(new["order"], "auto"), "started")
        self.assertEqual(self.started, ["JOB1"])

    # ---- 8) 金額の違う新しい注文には使えない（分かる説明を返す）・元の注文は保留のまま生きている
    def test_new_order_with_different_amount(self):
        self.go_on_hold()
        new = self.c.post("/run", json={"items": ["NP"]}).get_json()
        self.assertNotEqual(new["amount"], self.amount)
        code, j = self.pay(LINK, oid=new["order"])
        self.assertEqual((code, j["code"]), (402, "amount_mismatch"))
        self.assertIn(f"{self.amount:,}円", j["error"])
        self.assertEqual(self.state(), "hold")
        self.pp.phase = "success"
        self.assertEqual(A._recheck_and_start(self.oid, "auto"), "started")

    # ---- 9) 他人が同じリンクを使おうとしても、支払いは移らない…ではなく、同額なら移る仕様なので「実行は1回だけ」を保証する
    def test_link_never_runs_twice_even_across_users(self):
        self.go_on_hold()
        self.pp.phase = "success"
        self.assertEqual(self.pay(link=None)[1].get("job"), "JOB1")
        other = A.app.test_client()
        o2 = other.post("/run", json={"items": ["NP"], "subs": ["全キャラ最高形態"]}).get_json()
        code, j = self.pay(LINK, client=other, oid=o2["order"])
        self.assertEqual((code, j["code"]), (402, "duplicate"))
        self.assertEqual(self.started, ["JOB1"])

    # ---- 10) PayPay側の一時障害・こちらの接続切れでは止まらない
    def test_outage_during_hold_keeps_waiting(self):
        self.go_on_hold()
        self.pp.phase = "down"
        self.assertEqual(A._recheck_and_start(self.oid, "auto"), "wait:paypay_unavailable")
        self.assertEqual(self.pay(link=None)[1]["hold"], True)
        with patch.object(G, "_fresh_account", AsyncMock(side_effect=G.PayPayError("otp_required", "x"))):
            self.assertEqual(A._recheck_and_start(self.oid, "auto"), "wait:otp_required")
        self.assertEqual(self.state(), "hold")
        self.pp.phase = "success"
        self.assertEqual(A._recheck_and_start(self.oid, "auto"), "started")

    # ---- 11) 送金者が取り消した → 自動では進めず、管理者確認へ。管理者が再確認・支払い済みで救済できる
    def test_cancelled_goes_to_admin_and_can_be_rescued(self):
        self.go_on_hold()
        self.pp.phase = "cancelled"
        self.assertEqual(A._recheck_and_start(self.oid, "auto"), "stop:hold_failed")
        self.assertEqual(self.state(), "dead")
        self.assertEqual(self.c.get("/api/orders/mine").get_json()["orders"][0]["state"], "dead")
        self.pp.phase = "success"  # 実は入金されていた、と分かった場合
        self.assertEqual(A._recheck_and_start(self.oid, "admin"), "started")

    # ---- 12) 再起動をまたいでも保留は続く（決済確認の途中で落ちた場合も含む）
    def test_hold_survives_restart(self):
        self.go_on_hold()
        saved = {k: A._order_to_json(v) for k, v in A._orders.items()}
        saved[self.oid]["state"] = "paying"  # 確認中に落ちた
        A._orders.clear()
        A._orders.update({k: A._order_from_json(v) for k, v in saved.items()})
        self.assertEqual(self.state(), "hold")
        self.pp.phase = "success"
        self.assertEqual(A._recheck_and_start(self.oid, "auto"), "started")

    # ---- 13) 実行枠が満杯のときに入金が確認できた → 支払い済みで待ち、あとで開始できる
    def test_paid_while_busy_then_start(self):
        self.go_on_hold()
        self.pp.phase = "success"
        with patch.object(A, "_start_job", side_effect=lambda *a: (A.jsonify(error="busy"), 429)):
            self.assertEqual(A._recheck_and_start(self.oid, "auto"), "paid")
        self.assertEqual(self.state(), "paid")
        code, j = self.pay(link=None)
        self.assertEqual((code, j.get("job")), (200, "JOB1"))
        self.assertEqual(self.pp.posts, 1)


if __name__ == "__main__":
    unittest.main()
