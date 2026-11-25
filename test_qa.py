"""Q&A チャットボット（qa_bot / /api/qa）のテスト。実APIは呼ばず、SDKをモックする。"""
from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

import qa_bot


def _fake_anthropic(capture=None, text="PayPayのみです。", raise_exc=None):
    """anthropic モジュールの差し替え。create の引数を capture に記録する。"""
    mod = types.ModuleType("anthropic")

    class Block:
        type = "text"

        def __init__(self, t):
            self.text = t

    class Resp:
        def __init__(self, t):
            self.content = [Block(t)]

    class Msgs:
        def create(self, **kw):
            if capture is not None:
                capture.update(kw)
            if raise_exc:
                raise raise_exc
            return Resp(text)

    class Client:
        def __init__(self, *a, **k):
            self.messages = Msgs()

    mod.Anthropic = Client
    return mod


class CleanQuestion(unittest.TestCase):
    def test_strips_control_and_limits_length(self):
        self.assertEqual(qa_bot.clean_question("a\x00b\x07c"), "abc")
        self.assertEqual(len(qa_bot.clean_question("x" * 500)), qa_bot.MAX_Q_CHARS)
        self.assertEqual(qa_bot.clean_question("  "), "")
        self.assertEqual(qa_bot.clean_question("1行目\n2行目"), "1行目\n2行目")


class Enabled(unittest.TestCase):
    def test_reflects_env(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}, clear=False):
            self.assertTrue(qa_bot.enabled())
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(qa_bot.enabled())


class Disabled(unittest.TestCase):
    def test_fallback_when_no_key(self):
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            r = qa_bot.answer("puni", "支払いは？", [])
        self.assertFalse(r["enabled"])
        self.assertTrue(r["ok"])
        self.assertIn("よくある質問", r["answer"])

    def test_empty_question(self):
        r = qa_bot.answer("puni", "   ", [])
        self.assertFalse(r["ok"])


class SystemPrompt(unittest.TestCase):
    def test_includes_faq_orders_and_injection_rule(self):
        sp = qa_bot._system_prompt("puni", [{"state_ja": "完了", "amount": 60,
                                             "date": "11/26", "summary": "ぷにぷに代行／コインガチャ"}])
        self.assertIn("支払い方法", sp)        # FAQ
        self.assertIn("コインガチャ", sp)       # 注文要約
        self.assertIn("従わない", sp)           # プロンプト注入対策
        self.assertIn("案内のみ", sp)

    def test_no_orders_text(self):
        sp = qa_bot._system_prompt("nyanko", [])
        self.assertIn("注文はありません", sp)


class EnabledPath(unittest.TestCase):
    def test_calls_haiku_with_grounded_system(self):
        cap = {}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}, clear=False), \
             mock.patch.dict(sys.modules, {"anthropic": _fake_anthropic(cap, text="PayPayのみ。")}):
            r = qa_bot.answer("tsum", "支払いは？", [])
        self.assertTrue(r["enabled"])
        self.assertEqual(r["answer"], "PayPayのみ。")
        self.assertEqual(cap["model"], "claude-haiku-5-5")       # Haiku 5.5
        self.assertIn("サービスの仕組み", cap["system"])            # 仕組みの説明が根拠に入る
        self.assertIn("選べるメニュー", cap["system"])             # 部門のメニュー一覧も入る
        self.assertEqual(cap["messages"][0]["role"], "user")
        self.assertEqual(cap["messages"][0]["content"], "支払いは？")

    def test_sdk_exception_falls_back(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}, clear=False), \
             mock.patch.dict(sys.modules, {"anthropic": _fake_anthropic(raise_exc=RuntimeError("boom"))}):
            r = qa_bot.answer("puni", "支払いは？", [])
        self.assertTrue(r["ok"])          # 例外は投げない
        self.assertTrue(r["enabled"])
        self.assertIn("お問い合わせ", r["answer"])


class ApiEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("PUNI_DATA_DIR", "/tmp/puni_t")
        import app
        cls.app = app
        cls.client = app.app.test_client()

    def test_empty_question_400(self):
        self.assertEqual(self.client.post("/api/qa", json={"q": "", "shop": "puni"}).status_code, 400)

    def test_disabled_returns_fallback(self):
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            r = self.client.post("/api/qa", json={"q": "支払いは？", "shop": "puni"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j["enabled"])
        self.assertIn("よくある質問", j["answer"])

    def test_bad_shop_defaults_without_error(self):
        r = self.client.post("/api/qa", json={"q": "test", "shop": "xxx"})
        self.assertEqual(r.status_code, 200)

    def test_qa_orders_exclude_other_owner_and_secrets(self):
        app = self.app
        with mock.patch.object(app, "_owner", return_value="owner-Q"):
            with app._orders_lock:
                app._orders["QORD1"] = {"owner": "owner-Q", "state": "done", "amount": 60,
                                        "created": 0, "summary": "ぷにぷに代行／コインガチャ",
                                        "job": {"mode": "puni"}}
                app._orders["QORD2"] = {"owner": "someone-else", "state": "done", "amount": 99,
                                        "created": 0, "summary": "他人の注文",
                                        "job": {"mode": "puni"}}
            try:
                orders = app._qa_orders_for("puni")
            finally:
                with app._orders_lock:
                    app._orders.pop("QORD1", None)
                    app._orders.pop("QORD2", None)
        summaries = [o["summary"] for o in orders]
        self.assertIn("ぷにぷに代行／コインガチャ", summaries)
        self.assertNotIn("他人の注文", summaries)   # 他人の注文は渡さない
        # 要約にログイン情報が無いこと（_puni_summary/_tsum_summary が除外している前提）
        for o in orders:
            self.assertNotIn("password", str(o).lower())


if __name__ == "__main__":
    unittest.main()
