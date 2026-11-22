"""ブラウザを使わない部分のテスト。python test_logic.py で実行（Xには接続しない）。"""
import io
import os
import sys
import tempfile
import time
import unittest

tmp = tempfile.mkdtemp()
os.environ["XBOT_DATA_DIR"] = tmp
os.environ["XBOT_ADMIN_PASSWORD"] = "test-pass"
os.environ["XBOT_COOKIE_SECURE"] = "0"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xbot import logic, store  # noqa: E402


def R(**kw):
    base = dict(id=1, name="r", kind="keyword", keywords="", match="any", reply="x", enabled=1, priority=100)
    base.update(kw)
    return base


class Logic(unittest.TestCase):
    def test_keyword_any_all_and_width(self):
        r = R(keywords="料金, ねだん")
        self.assertTrue(logic.rule_matches(r, "料金はいくらですか", False))
        self.assertTrue(logic.rule_matches(r, "ネダン？".replace("ネダン", "ねだん"), False))
        self.assertFalse(logic.rule_matches(r, "こんにちは", False))
        self.assertTrue(logic.rule_matches(R(keywords="ＰＡＹＰＡＹ"), "paypayで払えますか", False))   # 全角・大文字を無視
        a = R(keywords="料金、代行", match="all")
        self.assertFalse(logic.rule_matches(a, "料金は？", False))
        self.assertTrue(logic.rule_matches(a, "代行の料金は？", False))
        self.assertFalse(logic.rule_matches(R(keywords=" , "), "なんでも", False))
        self.assertFalse(logic.rule_matches(R(keywords="料金", enabled=0), "料金", False))

    def test_pick_rule_priority_and_kinds(self):
        rules = [R(id=1, kind="always", priority=200, reply="always"), R(id=2, keywords="料金", priority=10, reply="price"),
                 R(id=3, kind="first_time", priority=50, reply="hello")]
        self.assertEqual(logic.pick_rule(rules, "料金は？", True)["reply"], "price")
        self.assertEqual(logic.pick_rule(rules, "こんにちは", True)["reply"], "hello")
        self.assertEqual(logic.pick_rule(rules, "こんにちは", False)["reply"], "always")
        self.assertIsNone(logic.pick_rule([R(keywords="料金")], "やあ", False))

    def test_limits(self):
        now = logic.parse_jst("2026-11-20T12:00")
        self.assertTrue(logic.can_post([now - 3600] * 4, now, 5))
        self.assertFalse(logic.can_post([now - 3600] * 5, now, 5))
        self.assertTrue(logic.can_post([now - 13 * 3600] * 5, now, 5))       # 前日の分は数えない（日本時間で日付が変わる）
        self.assertFalse(logic.can_reply([now - 100] * 10, now, 10))
        self.assertTrue(logic.can_reply([now - 4000] * 10, now, 10))
        self.assertTrue(logic.in_cooldown(now - 3600, now, 24))
        self.assertFalse(logic.in_cooldown(None, now, 24))
        self.assertFalse(logic.in_cooldown(now - 25 * 3600, now, 24))

    def test_time_and_misc(self):
        self.assertEqual(logic.fmt_jst(logic.parse_jst("2026-11-20T21:30")), "11/20 21:30")
        self.assertTrue(logic.is_own_preview("あなた: ありがとう"))
        self.assertTrue(logic.is_own_preview("You: thanks"))
        self.assertFalse(logic.is_own_preview("料金は？"))
        self.assertEqual(logic.post_length("abc"), 3)
        self.assertEqual(logic.post_length("あい"), 4)
        self.assertNotEqual(logic.msg_hash("/messages/1", "a"), logic.msg_hash("/messages/2", "a"))
        self.assertEqual(logic.msg_hash("/m/1", "ＡＢＣ"), logic.msg_hash("/m/1", "abc"))


class GotoRetry(unittest.TestCase):
    def test_aborted_navigation_is_retried(self):
        from xbot.browser import Browser
        import xbot.browser as xb
        calls = []

        class P:
            url = "about:blank"
            def goto(self, url, wait_until=None):
                calls.append(url)
                if len(calls) < 3:
                    raise RuntimeError("Page.goto: net::ERR_ABORTED at " + url)
                self.url = url
        b = Browser.__new__(Browser); b.page = P()
        old = xb.time.sleep; xb.time.sleep = lambda x: None
        try:
            b.goto("https://x.com/i/chat")
            self.assertEqual(len(calls), 3)
            calls.clear()
            class Q(P):
                def goto(self, url, wait_until=None):
                    calls.append(url); raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")
            b.page = Q()
            with self.assertRaises(RuntimeError):
                b.goto("https://x.com/i/chat")
            self.assertEqual(len(calls), 1)          # ほかのエラーはやり直さない
        finally:
            xb.time.sleep = old


class Repeat(unittest.TestCase):
    def test_next_time(self):
        t = logic.parse_jst("2026-11-20T10:00")          # 金曜
        self.assertEqual(logic.fmt_jst(logic.next_time({"type": "daily", "time": "09:00"}, t)), "11/21 09:00")
        self.assertEqual(logic.fmt_jst(logic.next_time({"type": "daily", "time": "21:30"}, t)), "11/20 21:30")
        self.assertEqual(logic.fmt_jst(logic.next_time({"type": "weekly", "time": "09:00", "days": [0, 2]}, t)), "11/23 09:00")  # 月
        self.assertEqual(logic.fmt_jst(logic.next_time({"type": "hours", "hours": 6, "anchor": t}, t + 7 * 3600)), "11/20 22:00")
        self.assertIsNone(logic.next_time({"type": "weekly", "time": "09:00", "days": []}, t))
        self.assertEqual(logic.describe_repeat({"type": "weekly", "time": "09:00", "days": [5, 0]}), "毎週 月・土 09:00")
        self.assertEqual(logic.render_text("{日付}({曜日}) {時刻}", t), "11/20(金) 10:00")


class Cookies(unittest.TestCase):
    def test_parse(self):
        from xbot import cookies
        tok = "a" * 40
        ck = cookies.parse("", tok, "b" * 64)
        self.assertEqual({c["name"] for c in ck}, {"auth_token", "ct0"})
        self.assertTrue(all(c["domain"] == ".x.com" and c["secure"] for c in ck))
        js = '[{"name":"auth_token","value":"%s","domain":".x.com","httpOnly":true},{"name":"ct0","value":"%s","domain":".x.com"},' \
             '{"name":"twid","value":"u%%3D1","domain":".x.com"},{"name":"other","value":"1","domain":".google.com"}]' % (tok, "c" * 64)
        names = {c["name"] for c in cookies.parse(js, "", "")}
        self.assertEqual(names, {"auth_token", "ct0", "twid"})
        for bad in (("", "", ""), ("not json", "", ""), ("", "short!", "")):
            with self.assertRaises(ValueError):
                cookies.parse(*bad)

    def test_web_route(self):
        from unittest import mock
        from xbot import web
        c = web.app.test_client()
        c.post("/login", data={"password": "test-pass"}); c.get("/")
        with c.session_transaction() as ss:
            csrf = ss["csrf"]
        with mock.patch.object(web.xlogin, "call", return_value={"state": "running", "step": 2, "label": "x"}) as call:
            r = c.post("/xcookie", data={"csrf": csrf, "auth_token": "a" * 40, "ct0": "b" * 64})
            self.assertEqual(r.headers["Location"], "/xcookie/status")
            self.assertEqual(call.call_args.args[0], "cookies")
            page = c.get("/xcookie/status").get_data(as_text=True)
            self.assertIn('http-equiv="refresh"', page)       # 進行中は自動で更新
        with mock.patch.object(web.xlogin, "call", return_value={"state": "done", "step": 4, "label": "ok"}):
            page = c.get("/xcookie/status").get_data(as_text=True)
            self.assertIn("完了しました", page)
            self.assertNotIn('http-equiv="refresh"', page)
        page = c.post("/xcookie", data={"csrf": csrf, "auth_token": "x"}).get_data(as_text=True)
        self.assertIn("形が正しくありません", page)


class ChatPin(unittest.TestCase):
    def test_pin_route(self):
        from unittest import mock
        from xbot import web
        c = web.app.test_client()
        c.post("/login", data={"password": "test-pass"}); c.get("/")
        with c.session_transaction() as ss:
            csrf = ss["csrf"]
        with mock.patch.object(web.xlogin, "call", return_value={"state": "running", "step": 2, "label": "x"}) as call:
            self.assertIn("4桁", c.post("/chatpin", data={"csrf": csrf, "pin": "12a"}).get_data(as_text=True))
            call.assert_not_called()
            r = c.post("/chatpin", data={"csrf": csrf, "pin": "1234"})
            self.assertEqual(r.headers["Location"], "/chatpin/status")
            self.assertEqual(call.call_args.args, ("pin", "1234"))
            self.assertIn("チャットの暗証番号", c.get("/chatpin/status").get_data(as_text=True))


class Web(unittest.TestCase):
    def setUp(self):
        from xbot import web
        self.web = web
        self.c = web.app.test_client()

    def login(self):
        self.c.post("/login", data={"password": "test-pass"})
        return self._csrf()

    def _csrf(self):
        self.c.get("/")
        with self.c.session_transaction() as s:
            return s["csrf"]

    def test_requires_login_and_csrf(self):
        self.assertEqual(self.c.get("/").status_code, 302)
        self.assertEqual(self.c.get("/auth").status_code, 401)
        self.c.post("/login", data={"password": "wrong"})
        self.assertEqual(self.c.get("/").status_code, 302)
        csrf = self.login()
        self.assertEqual(self.c.get("/auth").status_code, 200)
        self.assertEqual(self.c.post("/action/pause", data={"csrf": "bad"}).status_code, 400)   # CSRFなしは拒否
        self.c.post("/action/pause", data={"csrf": csrf})
        self.assertTrue(store.get("paused"))
        self.c.post("/action/resume", data={"csrf": csrf})
        self.assertFalse(store.get("paused"))
        r = self.c.post("/action/check_dm", data={"csrf": csrf, "back": "dm"})
        self.assertEqual((store.get("request"), r.headers["Location"]), ("check_dm", "/?tab=dm"))
        store.put("request", "")

    def test_post_with_image_and_validation(self):
        csrf = self.login()
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 50
        r = self.c.post("/post/new", data={"csrf": csrf, "text": "テスト投稿", "at": "2030-01-01T09:00",
                                           "images": [(io.BytesIO(png), "a.png")]}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        with store.db() as d:
            row = d.execute("SELECT * FROM posts ORDER BY id DESC").fetchone()
        self.assertEqual((row["text"], row["state"]), ("テスト投稿", "scheduled"))
        self.assertEqual(logic.fmt_jst(row["at"]), "01/01 09:00")
        self.assertIn(".png", row["images"])
        page = self.c.get("/?tab=posts").get_data(as_text=True)
        self.assertIn("テスト投稿", page)
        self.assertIn("下書きモード中", page)
        long_ok = "あ" * 3000                     # Plus の長文ポスト：280字を超えても予約できる
        self.c.post("/post/new", data={"csrf": csrf, "text": long_ok, "at": "2030-01-02T09:00"})
        with store.db() as d:
            self.assertEqual(d.execute("SELECT text FROM posts ORDER BY id DESC").fetchone()["text"], long_ok)
        too_long = "あ" * (logic.MAX_POST_LEN // 2 + 1)
        self.c.post("/post/new", data={"csrf": csrf, "text": too_long})
        self.assertIn("長すぎます", self.c.get("/?tab=posts").get_data(as_text=True))
        self.c.post("/post/new", data={"csrf": csrf, "text": "x", "images": [(io.BytesIO(b"x"), "a.heic")]}, content_type="multipart/form-data")
        self.assertIn("HEIC", self.c.get("/?tab=posts").get_data(as_text=True))
        self.c.post(f"/post/{row['id']}/cancel", data={"csrf": csrf})
        with store.db() as d:
            self.assertEqual(d.execute("SELECT state FROM posts WHERE id=?", (row["id"],)).fetchone()["state"], "canceled")

    def test_rules_crud(self):
        csrf = self.login()
        self.c.post("/rule/new", data={"csrf": csrf, "name": "料金", "kind": "keyword", "keywords": "料金,値段", "reply": "料金表です", "priority": "10"})
        self.c.post("/rule/new", data={"csrf": csrf, "kind": "keyword", "keywords": "", "reply": "x"})   # キーワードなしは不可
        with store.db() as d:
            rows = d.execute("SELECT * FROM rules").fetchall()
        self.assertEqual(len(rows), 1)
        rid = rows[0]["id"]
        self.c.post(f"/rule/{rid}/edit", data={"csrf": csrf, "name": "料金2", "kind": "always", "reply": "いつも", "priority": "5"})
        self.c.post(f"/rule/{rid}/toggle", data={"csrf": csrf})
        with store.db() as d:
            r = d.execute("SELECT * FROM rules WHERE id=?", (rid,)).fetchone()
        self.assertEqual((r["name"], r["kind"], r["enabled"], r["priority"]), ("料金2", "always", 0, 5))
        self.assertIn("料金2", self.c.get("/?tab=dm").get_data(as_text=True))

    def test_settings_and_login_screen(self):
        csrf = self.login()
        self.c.post("/settings", data={"csrf": csrf, "max_posts_day": "3", "max_replies_hour": "8", "dm_interval_min": "10",
                                       "reply_cooldown_hours": "12", "dm_scan_limit": "5"})
        self.assertEqual((store.get("max_posts_day"), store.get("max_replies_hour")), (3, 8))
        self.c.post("/settings", data={"csrf": csrf, "max_posts_day": "999", "max_replies_hour": "8", "dm_interval_min": "10",
                                       "reply_cooldown_hours": "12", "dm_scan_limit": "5"})
        self.assertEqual(store.get("max_posts_day"), 3)
        self.assertEqual(self.c.get("/login-screen").status_code, 302)    # 準備していないときは開けない
        self.c.post("/action/login_start", data={"csrf": csrf})
        self.assertGreater(store.get("login_until"), time.time())
        page = self.c.get("/login-screen").get_data(as_text=True)
        self.assertIn("/novnc/vnc.html", page)
        self.c.post("/action/login_stop", data={"csrf": csrf})
        self.assertEqual(store.get("login_until"), 0)
        self.assertTrue(store.get("dry_run"))        # 最初は下書きモード
        self.assertFalse(store.get("dm_enabled"))    # DM返信は最初はオフ


class Worker(unittest.TestCase):
    """ブラウザの代わりにダミーを使って、上限・重複防止・下書きモードを確かめる。"""

    def setUp(self):
        from xbot import worker
        self.w = worker
        with store.db() as d:
            d.execute("DELETE FROM posts"); d.execute("DELETE FROM rules"); d.execute("DELETE FROM replied"); d.execute("DELETE FROM handoffs")
        store.update_status(dm_fail_streak=0)

    def fake_browser(self, convs, incoming, requests=None):
        test = self
        test.sent, test.posted, test.accepted = [], [], []
        reqs = dict(requests or {})

        class FB:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def post(self, text, imgs, dry): test.posted.append((text, dry)); return None
            def dm_open_inbox(self, requests=False, other=False): self.cur = None; self.other = other
            def dm_request_keys(self): return [k for k in reqs if (k.startswith("o") == self.other)]
            def dm_open_request(self, key): self.cur = key; return reqs[key]
            def dm_accept(self): test.accepted.append(self.cur); reqs.pop(self.cur, None)
            def dm_chat_keys(self, limit): return [c["conv"] for c in convs][:limit]
            def dm_open_chat(self, key): self.cur = key; return incoming.get(key)
            def dm_reply(self, text, dry): test.sent.append((self.cur, text, dry))
            def dm_peer(self): return "テスト太郎（@taro_test）"
        return FB

    def test_post_respects_dry_and_limit(self):
        with store.db() as d:
            for i in range(3):
                d.execute("INSERT INTO posts(text, at, created) VALUES(?,?,?)", (f"p{i}", time.time() - 10, time.time()))
        self.w.Browser = self.fake_browser([], {})
        s = dict(store.get_all(), dry_run=False, max_posts_day=2)
        for _ in range(3):
            row = self.w.due_post()
            if row:
                self.w.run_post(s, row)
        with store.db() as d:
            states = [r["state"] for r in d.execute("SELECT state FROM posts ORDER BY id")]
        self.assertEqual(states, ["posted", "posted", "scheduled"])     # 3件目は上限で明日に回る
        s = dict(s, dry_run=True, max_posts_day=10)
        self.w.run_post(s, self.w.due_post())
        self.assertEqual(self.posted[-1], ("p2", True))

    def test_repeat_post_advances_and_keeps_record(self):
        import json as _j
        rep = {"type": "daily", "time": "09:00"}
        with store.db() as d:
            d.execute("INSERT INTO posts(text, at, created, repeat) VALUES(?,?,?,?)", ("おはよう {曜日}", time.time() - 5, time.time(), _j.dumps(rep)))
        self.w.Browser = self.fake_browser([], {})
        s = dict(store.get_all(), dry_run=False, max_posts_day=10)
        self.w.run_post(s, self.w.due_post())
        with store.db() as d:
            rows = [dict(r) for r in d.execute("SELECT * FROM posts ORDER BY id")]
        self.assertEqual(rows[0]["state"], "scheduled")                  # 定期の元は次の時刻へ
        self.assertGreater(rows[0]["at"], time.time())
        self.assertEqual(logic.fmt_jst(rows[0]["at"])[-5:], "09:00")
        self.assertEqual((rows[1]["state"], rows[1]["parent"]), ("posted", rows[0]["id"]))   # 実行の記録
        self.assertNotIn("{曜日}", self.posted[-1][0])
        self.assertIsNone(self.w.due_post())                             # すぐにはもう一度投稿しない

    def test_web_repeat_form(self):
        from xbot import web
        c = web.app.test_client()
        c.post("/login", data={"password": "test-pass"}); c.get("/")
        with c.session_transaction() as ss:
            csrf = ss["csrf"]
        c.post("/post/new", data={"csrf": csrf, "text": "週報", "at": "2030-01-07T20:00", "repeat": "weekly", "days": ["0", "4"]})
        c.post("/post/new", data={"csrf": csrf, "text": "x", "at": "2030-01-07T20:00", "repeat": "weekly"})   # 曜日なしは不可
        with store.db() as d:
            rows = [dict(r) for r in d.execute("SELECT * FROM posts WHERE text IN ('週報','x')")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(_json_days(rows[0]), [0, 4])
        self.assertIn("毎週 月・金 20:00", c.get("/?tab=posts").get_data(as_text=True))

    def test_dm_reply_once_and_skip_own(self):
        with store.db() as d:
            d.execute("INSERT INTO rules(name, kind, keywords, reply, created) VALUES('料金','keyword','料金','料金表です',?)", (time.time(),))
        convs = [{"conv": "1:9"}, {"conv": "2:9"}, {"conv": "3:9"}]
        inc = {"1:9": ("料金は？", False, True), "2:9": ("どうも", True, False), "3:9": ("こんにちは", False, True)}
        fb = self.fake_browser(convs, inc)
        self.w.DMBrowser = fb
        self.w.random.uniform = lambda a, b: 0
        self.w.time.sleep = lambda x: None
        s = dict(store.get_all(), dry_run=False, dm_accept_requests=False)
        self.w.run_dms(s)
        self.assertEqual(self.sent, [("1:9", "料金表です", False)])
        with store.db() as d:
            self.assertEqual(d.execute("SELECT who FROM replied WHERE rule_id IS NOT NULL").fetchone()["who"], "テスト太郎（@taro_test）")
        from xbot import web
        c = web.app.test_client(); c.post("/login", data={"password": "test-pass"})
        self.assertIn("テスト太郎（@taro_test）", c.get("/?tab=dm").get_data(as_text=True))     # DM履歴に相手が出る
        self.w.run_dms(s)                     # 同じメッセージには二度返さない
        self.assertEqual(len(self.sent), 1)

    def test_request_failure_does_not_stop_chats(self):
        with store.db() as d:
            d.execute("INSERT INTO rules(name, kind, keywords, reply, created) VALUES('料金','keyword','料金','料金表です',?)", (time.time(),))
        fb = self.fake_browser([{"conv": "1:9"}], {"1:9": ("料金は？", False, True)}, requests={"5:9": ("x", False, True)})
        class Broken(fb):
            def dm_open_request(self, key): raise TimeoutError("boom")
            def shot(self, name): return None
        self.w.DMBrowser = Broken
        self.w.random.uniform = lambda a, b: 0
        self.w.time.sleep = lambda x: None
        self.w.run_dms(dict(store.get_all(), dry_run=False, dm_accept_requests=True))
        self.assertEqual(self.sent, [("1:9", "料金表です", False)])          # リクエストで失敗しても、会話には返信する

    def test_hidden_other_requests_are_accepted_too(self):
        with store.db() as d:
            d.execute("INSERT INTO rules(name, kind, reply, created) VALUES('はじめて','first_time','ようこそ',?)", (time.time(),))
        fb = self.fake_browser([], {}, requests={"5:9": ("a", False, True), "o6:9": ("b", False, True), "o7:9": ("c", False, True)})
        self.w.DMBrowser = fb
        self.w.random.uniform = lambda a, b: 0
        self.w.time.sleep = lambda x: None
        self.w.run_dms(dict(store.get_all(), dry_run=False, dm_accept_requests=True))
        self.assertEqual(sorted(self.accepted), ["5:9", "o6:9", "o7:9"])     # 「その他」の分も許可する
        self.assertEqual(len(self.sent), 3)

    def test_draft_does_not_block_real_reply_and_rule_change_rechecks(self):
        with store.db() as d:
            d.execute("INSERT INTO rules(name, kind, keywords, reply, created) VALUES('料金','keyword','料金','料金表です',?)", (time.time(),))
        convs = [{"conv": "1:9"}, {"conv": "2:9"}]
        inc = {"1:9": ("料金は？", False, True), "2:9": ("予約したい", False, True)}
        self.w.DMBrowser = self.fake_browser(convs, inc)
        self.w.random.uniform = lambda a, b: 0
        self.w.time.sleep = lambda x: None
        self.w.run_dms(dict(store.get_all(), dry_run=True, dm_accept_requests=False))     # 下書き
        self.assertEqual(self.sent, [("1:9", "料金表です", True)])
        self.w.run_dms(dict(store.get_all(), dry_run=False, dm_accept_requests=False))    # 本番にしたら返信する
        self.assertEqual(self.sent[-1], ("1:9", "料金表です", False))
        n = len(self.sent)
        with store.db() as d:
            d.execute("INSERT INTO rules(name, kind, keywords, reply, created) VALUES('予約','keyword','予約','予約はこちら',?)", (time.time(),))
        store.put("rules_changed", time.time() + 1)
        self.w.run_dms(dict(store.get_all(), dry_run=False, dm_accept_requests=False))    # ルールを足したら、前は当てはまらなかった分も見る
        self.assertEqual(self.sent[n:], [("2:9", "予約はこちら", False)])
        store.put("rules_changed", 0)

    def test_requests_accepted_and_answered(self):
        with store.db() as d:
            d.execute("INSERT INTO rules(name, kind, reply, created) VALUES('はじめて','first_time','ようこそ',?)", (time.time(),))
        fb = self.fake_browser([], {}, requests={"5:9": ("はじめまして", False, True), "6:9": ("こんにちは", False, True)})
        self.w.DMBrowser = fb
        self.w.random.uniform = lambda a, b: 0
        self.w.time.sleep = lambda x: None
        self.w.run_dms(dict(store.get_all(), dry_run=True, dm_accept_requests=True))   # 下書きモードでは許可しない
        self.assertEqual(self.accepted, [])
        fb = self.fake_browser([], {}, requests={"5:9": ("はじめまして", False, True), "6:9": ("こんにちは", False, True)})
        self.w.DMBrowser = fb
        self.w.run_dms(dict(store.get_all(), dry_run=False, dm_accept_requests=True))
        self.assertEqual(sorted(self.accepted), ["5:9", "6:9"])
        self.assertEqual(sorted(x[0] for x in self.sent), ["5:9", "6:9"])

    # ---------------- 高性能化（claude/xbot-next）
    def _quiet(self):
        self.w.random.uniform = lambda a, b: 0
        self.w.time.sleep = lambda x: None

    def test_logic_reply_name_and_backoff(self):
        self.assertEqual(logic.render_reply("{name}さん、ありがとう", "たろう"), "たろうさん、ありがとう")
        self.assertEqual(logic.render_reply("{name}さん、ありがとう", None), "、ありがとう")
        self.assertEqual(logic.render_reply("こんにちは", None), "こんにちは")
        self.assertEqual(logic.dm_next_delay(5, 0), 300)
        self.assertEqual(logic.dm_next_delay(5, 1), 600)
        self.assertEqual(logic.dm_next_delay(5, 3), 2400)
        self.assertEqual(logic.dm_next_delay(5, 20), logic.DM_BACKOFF_MAX)       # 最大3時間
        h = {"id": 1, "enabled": 1, "kind": "handoff", "keywords": "返金, 詐欺", "match": "any", "priority": 1}
        self.assertTrue(logic.rule_matches(h, "返金してください", False))
        self.assertFalse(logic.rule_matches(h, "料金は？", False))

    def test_handoff_is_not_answered_and_listed(self):
        with store.db() as d:
            d.execute("INSERT INTO rules(name, kind, keywords, reply, priority, created) VALUES('トラブル','handoff','返金','',1,?)", (time.time(),))
            d.execute("INSERT INTO rules(name, kind, reply, priority, created) VALUES('受け皿','always','{name}さん、確認します',999,?)", (time.time(),))
        convs = [{"conv": "1:9"}, {"conv": "2:9"}]
        inc = {"1:9": ("返金してほしい", False, False), "2:9": ("質問です", False, False)}
        self.w.DMBrowser = self.fake_browser(convs, inc)
        self._quiet()
        s = dict(store.get_all(), dry_run=False, dm_accept_requests=False)
        c = self.w.run_dms(s)
        self.assertEqual(self.sent, [("2:9", "テスト太郎（@taro_test）さん、確認します", False)])   # 要対応には返さない・受け皿は返す
        self.assertEqual(c["handoff"], 1)
        with store.db() as d:
            rows = [dict(r) for r in d.execute("SELECT * FROM handoffs")]
        self.assertEqual((len(rows), rows[0]["snippet"], rows[0]["done"]), (1, "返金してほしい", 0))
        self.w.run_dms(s)                                  # 二度数えない・二度返さない
        with store.db() as d:
            self.assertEqual(d.execute("SELECT COUNT(*) c FROM handoffs").fetchone()["c"], 1)
        self.assertEqual(len(self.sent), 1)
        from xbot import web
        cl = web.app.test_client(); cl.post("/login", data={"password": "test-pass"})
        page = cl.get("/?tab=dm").get_data(as_text=True)
        self.assertIn("要対応（1件）", page)
        self.assertIn("返金してほしい", page)
        with cl.session_transaction() as ss:
            csrf = ss["csrf"]
        cl.post(f"/handoff/{rows[0]['id']}/done", data={"csrf": csrf})
        self.assertNotIn("要対応（", cl.get("/?tab=dm").get_data(as_text=True))

    def test_handoff_rule_form_allows_empty_reply(self):
        from xbot import web
        cl = web.app.test_client(); cl.post("/login", data={"password": "test-pass"}); cl.get("/")
        with cl.session_transaction() as ss:
            csrf = ss["csrf"]
        cl.post("/rule/new", data={"csrf": csrf, "name": "トラブル", "kind": "handoff", "keywords": "返金", "reply": "", "priority": "1"})
        cl.post("/rule/new", data={"csrf": csrf, "name": "空", "kind": "keyword", "keywords": "x", "reply": "", "priority": "1"})   # ふつうのルールは返信文が必要
        cl.post("/rule/new", data={"csrf": csrf, "name": "kwなし", "kind": "handoff", "keywords": "", "reply": "", "priority": "1"})
        with store.db() as d:
            self.assertEqual([r["name"] for r in d.execute("SELECT name FROM rules")], ["トラブル"])

    def test_dm_yields_to_due_post(self):
        with store.db() as d:
            d.execute("INSERT INTO rules(name, kind, reply, created) VALUES('全部','always','どうも',?)", (time.time(),))
        convs = [{"conv": f"{i}:9"} for i in range(5)]
        inc = {c["conv"]: ("こんにちは", False, False) for c in convs}
        self.w.DMBrowser = self.fake_browser(convs, inc)
        self._quiet()
        calls = {"n": 0}

        def due():
            calls["n"] += 1
            return calls["n"] >= 3                       # 2件返したところで予約投稿の時刻が来た
        c = self.w.run_dms(dict(store.get_all(), dry_run=False, dm_accept_requests=False), yield_to_post=due)
        self.assertTrue(c["yielded"])
        self.assertEqual(len(self.sent), 2)
        c = self.w.run_dms(dict(store.get_all(), dry_run=False, dm_accept_requests=False))   # 次の回で続きから（返した分は飛ばす）
        self.assertEqual(len(self.sent), 5)

    def test_open_failures_stop_run_and_backoff(self):
        with store.db() as d:
            d.execute("INSERT INTO rules(name, kind, reply, created) VALUES('全部','always','どうも',?)", (time.time(),))
        convs = [{"conv": f"{i}:9"} for i in range(6)]
        FB = self.fake_browser(convs, {})
        tried = []

        class Broken(FB):
            def dm_open_chat(self, key):
                tried.append(key)
                raise TimeoutError("x")
        self.w.DMBrowser = Broken
        self._quiet()
        s = dict(store.get_all(), dry_run=False, dm_accept_requests=False, dm_enabled=True, dm_interval_min=5)
        t0 = time.time()
        nxt = self.w._auto_dm(s)
        self.assertEqual(len(tried), self.w.OPEN_FAIL_STOP)          # 6件すべてでタイムアウトを待たない
        self.assertEqual(store.get("status")["dm_fail_streak"], 1)
        nxt = self.w._auto_dm(dict(s, status=store.get("status")))
        self.assertEqual(store.get("status")["dm_fail_streak"], 2)
        self.assertGreaterEqual(nxt - t0, 1200)                       # 2回続いたら 5分×4 = 20分あける
        self.w.DMBrowser = self.fake_browser(convs, {})
        self.w._auto_dm(dict(s, status=store.get("status")))
        self.assertEqual(store.get("status")["dm_fail_streak"], 0)    # うまくいったら元に戻る

    def test_chats_use_a_browser_that_never_opened_requests(self):
        """実験の結果：リクエスト画面を開いたブラウザでは会話の一覧が0件になる。会話は別のブラウザで見る。"""
        with store.db() as d:
            d.execute("INSERT INTO rules(name, kind, reply, created) VALUES('全部','always','どうも',?)", (time.time(),))
        convs = [{"conv": "1:9"}, {"conv": "2:9"}]
        FB = self.fake_browser(convs, {"1:9": ("やあ", False, False), "2:9": ("どうも", False, False)},
                               requests={"5:9": ("はじめまして", False, True)})

        class LikeX(FB):
            def __init__(self, *a, **k):
                self.saw_requests = False
            def dm_open_inbox(self, requests=False, other=False):
                self.saw_requests = self.saw_requests or requests
                super().dm_open_inbox(requests, other)
            def dm_chat_keys(self, limit):
                return [] if self.saw_requests else super().dm_chat_keys(limit)
        self.w.DMBrowser = LikeX
        self._quiet()
        c = self.w.run_dms(dict(store.get_all(), dry_run=False, dm_accept_requests=True))
        self.assertEqual(c["chats"], 2)
        self.assertEqual(sorted(x[0] for x in self.sent), ["1:9", "2:9", "5:9"])

    def test_dm_run_time_limit(self):
        with store.db() as d:
            d.execute("INSERT INTO rules(name, kind, reply, created) VALUES('全部','always','どうも',?)", (time.time(),))
        convs = [{"conv": f"{i}:9"} for i in range(4)]
        self.w.DMBrowser = self.fake_browser(convs, {c["conv"]: ("やあ", False, False) for c in convs})
        self._quiet()
        old = self.w.DM_RUN_MAX_SEC
        self.w.DM_RUN_MAX_SEC = -1
        try:
            c = self.w.run_dms(dict(store.get_all(), dry_run=False, dm_accept_requests=False))
        finally:
            self.w.DM_RUN_MAX_SEC = old
        self.assertTrue(c["timeout"])
        self.assertEqual(self.sent, [])


def _json_days(row):
    import json as _j
    return _j.loads(row["repeat"])["days"]


if __name__ == "__main__":
    unittest.main(verbosity=1)
