"""画面に出る金額と、サーバーが決める金額が一致するかのブラウザテスト（docs/AUDIT_PLAN.md C2）。

料金の計算は、サーバー（compute_amount）と画面（JavaScript の renderCart）の2か所にある。
片方だけ直すと、画面の金額と実際の請求がずれる。いろいろな組み合わせを画面で選び、
画面の合計と、同じ内容でサーバーに注文したときの金額を比べる。
Playwright と Chromium がない環境では飛ばす。
"""
import os
import random
import shutil
import threading
import unittest
from unittest.mock import patch

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

CHROME = os.environ.get("XBOT_CHROMIUM") or next(
    (p for p in ("/opt/pw-browsers/chromium-1243/chrome-linux64/chrome", shutil.which("chromium"), shutil.which("chromium-browser")) if p and os.path.exists(p)), None)

# 画面で選んで、画面の合計と /run の金額を返す（ブラウザの中で動かす）
PICK_JS = r"""
async (o) => {
  const rnd = (() => { let x = o.seed; return () => (x = (x * 1103515245 + 12345) % 2147483648) / 2147483648; })();
  const pickSome = (name, p, max) => {
    const all = [...document.querySelectorAll('input[name=' + name + ']')];
    all.forEach(e => { e.checked = false; });
    let n = 0;
    for (const e of all) { if (n >= max) break; if (!e.disabled && rnd() < p) { e.checked = true; e.dispatchEvent(new Event('change', {bubbles: true})); n++; } }
  };
  ['#integrity', '#clone', '#restart'].forEach(q => { const e = document.querySelector(q); if (e) e.checked = false; });
  if (o.mode === 'integrity') document.querySelector('#integrity').checked = true;
  if (o.mode === 'clone') document.querySelector('#clone').checked = true;
  if (o.mode === 'restart') { const r = document.querySelector('#restart'); if (r && !r.disabled) r.checked = true; }
  pickSome('subs', 0.25, 6);
  pickSome('events', 0.05, 8);          // 前提のあるマップは、前提を選ぶまで押せない（画面のとおり）
  pickSome('events', 0.05, 8);
  pickSome('others', 0.3, 5);
  document.querySelectorAll('input.q').forEach(e => { e.value = rnd() < 0.08 ? String(1 + Math.floor(rnd() * 99)) : ''; });
  const nums = [];
  if (rnd() < 0.6) { for (let i = 0; i < 1 + Math.floor(rnd() * 6); i++) nums.push(1 + Math.floor(rnd() * 60)); }
  document.querySelector('#chars').value = [...new Set(nums)].join(',');
  Object.keys(catSettings).forEach(k => { delete catSettings[k]; });
  [...new Set(nums)].forEach(n => { const s = {};
    if (rnd() < 0.4) s.base_level = 30; if (rnd() < 0.3) s.plus_level = 10; if (rnd() < 0.3) s.form = 2; if (rnd() < 0.3) s.talents_all = true;
    catSettings[n] = s; });
  const h = document.querySelector('#ptH'), m = document.querySelector('#ptM');
  if (h) { h.value = rnd() < 0.3 ? String(1 + Math.floor(rnd() * 5)) : ''; m.value = h.value ? String(10 * Math.floor(rnd() * 6)) : '0'; }
  if (typeof syncModes === 'function') syncModes();
  refresh();
  renderCart(parseNums(document.querySelector('#chars').value), qtyMap());
  const tot = document.querySelector('.ctotal b');
  const shown = tot ? parseInt(tot.firstChild.textContent.replace(/[^0-9]/g, ''), 10) : null;
  const body = runBody('', '');
  const r = await fetch('/run', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  return {shown, status: r.status, amount: j.amount, error: j.error || null, code: j.code || null,
          picked: {subs: body.subs.length, events: body.events.length, others: body.others.length, qty: Object.keys(body.qty).length,
                   chars: body.chars, pt: body.playtime_add_min, mode: o.mode}};
}
"""


@unittest.skipIf(sync_playwright is None, "Playwright がない")
class PriceParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as A
        from werkzeug.serving import make_server
        cls.A = A
        cls.tmp = __import__("tempfile").TemporaryDirectory()
        cls.patches = [patch.object(A, "PAYMENTS_ENABLED", True), patch.object(A, "_persist_orders"),
                       patch.object(A, "MAX_UNPAID_PER_OWNER", 10 ** 6)]
        for p in cls.patches:
            p.start()
        cls.srv = make_server("127.0.0.1", 0, A.app, threaded=True)
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        except Exception as exc:  # noqa: BLE001
            cls.tearDownClass()
            raise unittest.SkipTest(f"Chromium を起動できない: {type(exc).__name__}")

    @classmethod
    def tearDownClass(cls):
        for name in ("browser", "pw"):
            obj = getattr(cls, name, None)
            if obj is not None:
                try:
                    obj.close() if name == "browser" else obj.stop()
                except Exception:  # noqa: BLE001
                    pass
        cls.srv.shutdown()
        for p in reversed(cls.patches):
            p.stop()
        cls.tmp.cleanup()

    def test_screen_total_equals_server_amount(self):
        ctx = self.browser.new_context(viewport={"width": 390, "height": 844})
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        pg.goto(self.url)
        pg.wait_for_timeout(800)
        checked, skipped = 0, []
        modes = ["normal"] * 6 + ["restart", "integrity", "clone"]
        rng = random.Random(20261123)
        for i in range(int(os.environ.get("PARITY_CASES", "60"))):
            mode = rng.choice(modes)
            res = pg.evaluate(PICK_JS, {"seed": 1000 + i, "mode": mode})
            if res["status"] != 200:
                # 画面でも止める組み合わせ（真レジェンドだけ・前提のないイベステ など）は、金額の比較の対象外
                skipped.append((i, res["code"]))
                continue
            self.assertEqual(res["shown"], res["amount"], f"case {i}: 画面 {res['shown']}円 ≠ サーバー {res['amount']}円 {res['picked']}")
            checked += 1
        self.assertGreaterEqual(checked, 30, f"比べられた組み合わせが少ない（飛ばした: {skipped[:10]}）")


if __name__ == "__main__":
    unittest.main()
