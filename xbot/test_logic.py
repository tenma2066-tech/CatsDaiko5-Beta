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
        with mock.patch.object(web.xlogin, "call", return_value={"state": "done", "label": "ok"}) as call:
            r = c.post("/xcookie", data={"csrf": csrf, "auth_token": "a" * 40, "ct0": "b" * 64})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(call.call_args.args[0], "cookies")
        page = c.post("/xcookie", data={"csrf": csrf, "auth_token": "x"}).get_data(as_text=True)
        self.assertIn("形が正しくありません", page)


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
        too_long = "あ" * 141
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
            d.execute("DELETE FROM posts"); d.execute("DELETE FROM rules"); d.execute("DELETE FROM replied")

    def fake_browser(self, convs, incoming):
        test = self
        test.sent = []
        test.posted = []

        class FB:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def post(self, text, imgs, dry): test.posted.append((text, dry)); return None
            def list_conversations(self, limit): return convs
            def last_incoming(self, conv): return incoming.get(conv)
            def reply(self, conv, text, dry): test.sent.append((conv, text, dry))
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
        convs = [{"conv": "/messages/1", "preview": "料金は？"}, {"conv": "/messages/2", "preview": "あなた: どうも"},
                 {"conv": "/messages/3", "preview": "こんにちは"}]
        inc = {"/messages/1": ("料金は？", True), "/messages/3": ("こんにちは", True)}
        self.w.Browser = self.fake_browser(convs, inc)
        self.w.random.uniform = lambda a, b: 0
        self.w.time.sleep = lambda x: None
        s = dict(store.get_all(), dry_run=False)
        self.w.run_dms(s)
        self.assertEqual(self.sent, [("/messages/1", "料金表です", False)])
        self.w.run_dms(s)                     # 同じメッセージには二度返さない
        self.assertEqual(len(self.sent), 1)


def _json_days(row):
    import json as _j
    return _j.loads(row["repeat"])["days"]


if __name__ == "__main__":
    unittest.main(verbosity=1)
