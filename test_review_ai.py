"""実績の入力の検査：でたらめな並び・危険な文字（機械的）と、AI（Haiku・エフォート低）の確認。"""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app as A
import paypay_gateway as G
import review_ai
import review_filter

GOOD = "注文から5分で完了しました。案内が分かりやすくて安心でした。"


class Mechanical(unittest.TestCase):
    def test_junk_is_refused(self):
        for text in ("あかさたなはまやらわあかさたなはまやらわ", "あいうえおかきくけこさしすせそたちつてと", "　" * 30,
                     "よかったよかったよかったよかったよかった", "ああああああああああいいいいいいいいいい",
                     "!!!!!!!!!!??????????!!!!!!!!!!", "asdfghjkl asdfghjkl qwerty", "１２３４５６７８９０１２３４５６７８９０", "😀" * 25):
            self.assertEqual(review_filter.check(text)[1], "gibberish", text)

    def test_dangerous_characters_are_refused(self):
        for text in ("<script>alert(1)</script> とても良かったです。また使います。", "とても‮良かったです。また使いたいと思います。",
                     "とても良かった​です。また使いたいと思います。本当に。", '<img src=x onerror=alert(1)> 早くて助かりました。ありがとう。',
                     "javascript:alert(1) とても早くて助かりました。ありがとう。", "良かったです &#x3c;script&#x3e; また使いたいと思います。"):
            self.assertEqual(review_filter.check(text)[1], "bad_chars", text)

    def test_real_reviews_pass(self):
        for text in (GOOD, "めっちゃ早かった！！ありがとうございます😊また頼みます〜", "遅かったし一回失敗した。でもやり直しで出来たので星3です。",
                     "最高でした最高でした、本当にありがとうございました。", "ABCのランク上げをお願いして123体そろいました。満足です。",
                     "3 < 5 分で終わったので驚きました。次もお願いしたいです。", "ありがとうございました！！！！！また利用します！！！！！",
                     "This was fast and easy. I got my code in five minutes."):
            self.assertEqual(review_filter.check(text), (True, "", ""), text)


def fake_sdk(reply=None, error=None, stop="end_turn", seen=None):
    """anthropic の代わり（通信しない）。送った内容を seen に残す。"""
    class Messages:
        def create(self, **kw):
            if seen is not None:
                seen.update(kw)
            if error:
                raise error
            return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=reply)])

    class Client:
        def __init__(self, **kw):
            if seen is not None:
                seen["client"] = kw
            self.messages = Messages()
    return types.SimpleNamespace(Anthropic=Client)


class Judge(unittest.TestCase):
    def run_judge(self, text=GOOD, stars=5, **kw):
        seen = {}
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "REVIEW_AI": "1"}), patch.dict(sys.modules, {"anthropic": fake_sdk(seen=seen, **kw)}):
            return review_ai.judge(text, stars), seen

    def test_request_shape(self):
        (verdict, _), seen = self.run_judge(reply='{"verdict": "ok"}')
        self.assertEqual(verdict, "ok")
        self.assertEqual(seen["model"], "claude-haiku-5-5")
        self.assertEqual(seen["output_config"]["effort"], "low")
        self.assertEqual(seen["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(seen["output_config"]["format"]["schema"]["properties"]["verdict"]["enum"], ["ok", "gibberish", "off_topic", "unsafe"])
        self.assertEqual(seen["client"], {"timeout": review_ai.TIMEOUT, "max_retries": 0})
        self.assertEqual(seen["messages"], [{"role": "user", "content": f"星：5（5が最高）\n<review>\n{GOOD}\n</review>"}])
        self.assertNotIn("thinking", seen)
        self.assertNotIn("temperature", seen)

    def test_verdicts(self):
        for v in ("gibberish", "off_topic", "unsafe"):
            (verdict, message), _ = self.run_judge(reply='{"verdict": "%s"}' % v)
            self.assertEqual((verdict, message), (v, review_ai.MESSAGES[v]))

    def test_unknown_when_anything_goes_wrong(self):
        self.assertEqual(self.run_judge(error=TimeoutError())[0], (None, ""))
        self.assertEqual(self.run_judge(reply="not json")[0], (None, ""))
        self.assertEqual(self.run_judge(reply='{"verdict": "banana"}')[0], (None, ""))
        self.assertEqual(self.run_judge(reply='{"verdict": "unsafe"}', stop="max_tokens")[0], (None, ""))
        self.assertEqual(self.run_judge(reply='{"verdict": "unsafe"}', stop="refusal")[0], (None, ""))

    def test_off_without_key_or_when_switched_off(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertEqual(review_ai.judge(GOOD, 5), (None, ""))
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "REVIEW_AI": "0"}):
            self.assertEqual(review_ai.judge(GOOD, 5), (None, ""))


class Endpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(A, "PAYMENTS_ENABLED", True), patch.dict(A._orders, {}, clear=True), patch.dict(A._jobs, {}, clear=True),
            patch.object(G, "history_upsert"), patch.object(G, "_LEDGER_FILE", Path(self.tmp.name) / "l.sqlite3"),
            patch.object(A, "_persist_orders"), patch.object(A, "_rate_ok", return_value=True), patch.object(A, "_reconcile_orders"),
        ]
        for p in self.patches:
            p.start()
        self.c = A.app.test_client()
        self.oid = self.c.post("/run", json={"items": ["XP"]}).get_json()["order"]
        A._orders[self.oid].update(state="done", job_id="J1", gate=True, result={"transfer": "abc", "confirm": "1234"})

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def post(self, text=GOOD, stars=5):
        return self.c.post("/api/reviews", json={"stars": stars, "text": text, "order": self.oid})

    def test_ai_rejection_keeps_the_result_locked(self):
        with patch.object(review_ai, "judge", return_value=("off_topic", review_ai.MESSAGES["off_topic"])) as judge:
            r = self.post("今日はいい天気ですね。散歩に行きたい気分になりました。")
            self.assertEqual((r.status_code, r.get_json()["code"]), (400, "off_topic"))
            self.assertEqual(judge.call_args.args, ("今日はいい天気ですね。散歩に行きたい気分になりました。", 5))
        self.assertEqual(G.review_list(), [])
        self.assertNotIn("reviewed", A._orders[self.oid])
        self.assertTrue(A._review_locked(A._orders[self.oid]))                  # 断ったときは、結果を渡さない（免除もしない）

    def test_ok_and_unknown_both_save(self):
        with patch.object(review_ai, "judge", return_value=("ok", "")):
            self.assertTrue(self.post().get_json()["saved"])
        self.assertFalse(A._review_locked(A._orders[self.oid]))
        A._orders[self.oid].pop("reviewed")
        G.review_set_hidden(G.review_list()[0]["id"], True)
        oid2 = self.c.post("/run", json={"items": ["XP"]}).get_json()["order"]
        A._orders[oid2].update(state="done", job_id="J2", gate=True, result={"transfer": "x", "confirm": "1"})
        with patch.object(review_ai, "judge", return_value=(None, "")):         # AI に聞けなかった → 止めない
            self.assertTrue(self.c.post("/api/reviews", json={"stars": 4, "text": GOOD + "また使います。", "order": oid2}).get_json()["saved"])

    def test_ai_is_not_called_for_what_the_cheap_checks_catch(self):
        with patch.object(review_ai, "judge") as judge:
            self.assertEqual(self.post("あかさたなはまやらわあかさたなはまやらわ").get_json()["code"], "gibberish")
            self.assertEqual(self.post("<script>alert(1)</script> とても良かったです。また使います。").get_json()["code"], "bad_chars")
            self.assertEqual(self.post("短い").get_json()["code"], "too_short")
            self.assertEqual(self.post("クソみたいなサービスでした。二度と使いたくないです。").get_json()["code"], "bad_words")
            judge.assert_not_called()

    def test_ai_is_not_called_without_an_order_to_review(self):
        other = A.app.test_client()
        with patch.object(review_ai, "judge") as judge:
            self.assertEqual(other.post("/api/reviews", json={"stars": 5, "text": GOOD, "order": self.oid}).get_json(), {"ok": True, "saved": False})
            judge.assert_not_called()

    def test_ai_calls_are_rate_limited(self):
        with patch.object(A, "_rate_ok", side_effect=lambda key, *a: not key.startswith("reviewai:")), patch.object(review_ai, "judge") as judge:
            self.assertEqual(self.post().status_code, 429)
            judge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
