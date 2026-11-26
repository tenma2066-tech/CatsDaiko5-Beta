/* 購入のときの「人間であることの確認」（Cloudflare Turnstile）。全画面共通。
   画面に <div id="humanBox" data-sitekey="…"> があれば、見えるようになったときにチェックを出す。
     Human.get(待つミリ秒) → 確認の印（まだなら、出るまで少し待つ。出なければ空）
     Human.reset()         → 印は1回しか使えないので、送ったあとに取り直す */
(function () {
  "use strict";
  var wid = null, tok = "", waiters = [];
  function box() { return document.getElementById("humanBox"); }
  function done(t) { tok = t || ""; if (tok) { waiters.splice(0).forEach(function (f) { f(tok); }); } }
  function render() {
    var b = box();
    if (!b || wid !== null || !window.turnstile || b.offsetParent === null) return;
    try {
      wid = window.turnstile.render(b, { sitekey: b.getAttribute("data-sitekey"), language: "ja", callback: done,
        "expired-callback": function () { tok = ""; }, "error-callback": function () { tok = ""; } });
    } catch (e) { wid = null; }
  }
  var timer = setInterval(function () { if (wid !== null) { clearInterval(timer); return; } render(); }, 400);
  window.Human = {
    get: function (ms) {
      render();
      if (tok || !box()) return Promise.resolve(tok);
      return new Promise(function (resolve) {
        var f = function (t) { clearTimeout(to); resolve(t); };
        var to = setTimeout(function () { var i = waiters.indexOf(f); if (i >= 0) waiters.splice(i, 1); resolve(""); }, ms || 8000);
        waiters.push(f);
      });
    },
    reset: function () { tok = ""; if (wid !== null && window.turnstile) { try { window.turnstile.reset(wid); } catch (e) {} } }
  };
})();
