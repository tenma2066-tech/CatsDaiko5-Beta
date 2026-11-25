/* 実績（星5段階＋感想）の入力欄。全画面共通。

     ReviewForm(置く場所の要素, { min:20, max:400, target:()=>({order, job}), pastUrl:'…', onDone:(saved)=>{} })

   送信できたら onDone を呼ぶ。保存先は POST /api/reviews（完了した注文1件につき1つ）。
   通信に続けて失敗したときは、お客さんを待たせないよう onDone(false) で先へ進める。 */
(function () {
  "use strict";
  var CSS = ".rvf{display:block}.rvf-stars{display:flex;justify-content:center;gap:4px;margin:6px 0 2px}" +
    ".rvf-stars button{width:48px;height:48px;border:0;background:none;font-size:34px;line-height:1;cursor:pointer;color:#c9ced6;padding:0}" +
    ".rvf-stars button[aria-checked=true]{color:#f5a623}" +
    ".rvf-stars button:focus-visible{outline:3px solid #2f7df6;outline-offset:2px;border-radius:8px}" +
    ".rvf-word{text-align:center;font-size:13px;font-weight:700;min-height:1.4em;margin:0 0 8px}" +
    ".rvf textarea{display:block;width:100%;min-height:110px;padding:11px 12px;border:2px solid var(--line,#17181c);border-radius:12px;" +
    "background:var(--panel,#fff);color:inherit;font:inherit;font-size:16px;line-height:1.6;resize:vertical;box-sizing:border-box}" +
    ".rvf-count{display:flex;justify-content:space-between;gap:8px;font-size:12.5px;color:var(--muted,#5b6472);margin:4px 2px 0}" +
    ".rvf-count b.ok{color:var(--ok,#12a05c)}" +
    ".rvf-err{color:var(--bad,#d93636);font-size:13.5px;font-weight:700;min-height:1.2em;margin:6px 0}" +
    ".rvf-send{display:block;width:100%;min-height:50px;margin:4px 0 0;border:2px solid var(--line,#17181c);border-radius:14px;" +
    "background:var(--accent,#ffc42e);color:#17181c;font:inherit;font-weight:800;font-size:16px;cursor:pointer}" +
    ".rvf-send:disabled{opacity:.5;cursor:not-allowed}" +
    ".rvf-past{display:block;text-align:center;margin:10px 0 0;font-size:13px}";
  var WORDS = ["", "残念だった", "いまひとつ", "ふつう", "よかった", "とてもよかった！"];
  var styled = false;
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  window.ReviewForm = function (root, cfg) {
    if (!styled) { styled = true; var st = el("style"); st.textContent = CSS; document.head.append(st); }
    var min = cfg.min || 20, max = cfg.max || 400, stars = 0, fails = 0, busy = false;
    root.textContent = ""; root.classList.add("rvf");

    var row = el("div", "rvf-stars"); row.setAttribute("role", "radiogroup"); row.setAttribute("aria-label", "評価（星1〜5）");
    var btns = [1, 2, 3, 4, 5].map(function (n) {
      var b = el("button", null, "★"); b.type = "button"; b.setAttribute("role", "radio");
      b.setAttribute("aria-label", "星" + n + "（" + WORDS[n] + "）"); b.setAttribute("aria-checked", "false");
      b.addEventListener("click", function () { setStars(n); });
      b.addEventListener("keydown", function (e) {
        if (e.key === "ArrowRight" || e.key === "ArrowUp") { e.preventDefault(); setStars(Math.min(5, (stars || 0) + 1), true); }
        if (e.key === "ArrowLeft" || e.key === "ArrowDown") { e.preventDefault(); setStars(Math.max(1, (stars || 2) - 1), true); }
      });
      row.append(b); return b;
    });
    var word = el("p", "rvf-word", "星をタップして評価してください");
    var area = el("textarea"); area.maxLength = max; area.setAttribute("aria-label", "感想（" + min + "文字以上）");
    area.placeholder = "例：注文から5分で完了しました。画面の案内が分かりやすくて、はじめてでも迷いませんでした。";
    var count = el("div", "rvf-count"), left = el("b"), limit = el("span", null, "最大 " + max + " 文字");
    count.append(left, limit);
    var err = el("p", "rvf-err"); err.setAttribute("role", "alert");
    var send = el("button", "rvf-send", "実績を送信する"); send.type = "button";
    root.append(row, word, area, count, err, send);
    if (cfg.pastUrl) {
      var past = el("a", "rvf-past", "これまでの実績を見る →"); past.href = cfg.pastUrl; past.target = "_blank"; past.rel = "noopener";
      root.append(past);
    }

    function setStars(n, focus) {
      stars = n;
      btns.forEach(function (b, i) { b.setAttribute("aria-checked", i < n ? "true" : "false"); b.tabIndex = i === n - 1 ? 0 : -1; });
      word.textContent = "★".repeat(n) + "　" + WORDS[n];
      if (focus) btns[n - 1].focus();
      refresh();
    }
    function length() { return area.value.replace(/[ \t　]{2,}/g, " ").trim().length; }
    function refresh() {
      var n = length();
      left.className = n >= min ? "ok" : "";
      left.textContent = n >= min ? "✓ " + n + " 文字" : "あと " + (min - n) + " 文字（" + min + "文字以上）";
      send.disabled = busy || !stars || n < min;
      err.textContent = "";
    }
    area.addEventListener("input", refresh);
    refresh();

    send.addEventListener("click", async function () {
      if (busy) return;
      busy = true; refresh(); send.textContent = "送信しています…";
      var target = (cfg.target && cfg.target()) || {};
      try {
        var r = await fetch("/api/reviews", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ stars: stars, text: area.value, order: target.order || "", job: target.job || "" }) });
        var j = {}; try { j = await r.json(); } catch (e) {}
        if (r.ok) { cfg.onDone(!!j.saved); return; }
        if (r.status === 404) { cfg.onDone(false); return; }        /* 実績の機能が止められている */
        busy = false; refresh(); send.textContent = "実績を送信する";
        err.textContent = j.error || "送信できませんでした。もう一度お試しください。";
        if (r.status >= 500 && ++fails >= 2) cfg.onDone(false);      /* サーバーの不調が続くときは、先へ進める */
      } catch (e) {
        busy = false; refresh(); send.textContent = "実績を送信する";
        err.textContent = "通信できませんでした。電波の良いところでもう一度お試しください。";
        if (++fails >= 2) cfg.onDone(false);
      }
    });
    return { focus: function () { btns[0].focus(); } };
  };
})();
