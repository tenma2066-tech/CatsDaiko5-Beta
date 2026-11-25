/* 質問チャット（全Shop共通）。<script src="/static/qa.js" data-shop="nyanko|tsum|puni" defer>
   で読み込むと、ボタンと会話パネルを自分で作る。会話は直近だけサーバーへ送る（対話の文脈用）。 */
(function () {
  "use strict";
  var me = document.currentScript;
  var SHOP = (me && me.getAttribute("data-shop")) || "nyanko";
  var GREET = {
    nyanko: "こんにちは！にゃんこ代行のことなら気軽に聞いてくださいね。",
    tsum: "こんにちは！ツムツム代行のことなら気軽に聞いてくださいね。",
    puni: "こんにちは！ぷにぷに代行のことなら気軽に聞いてくださいね。"
  };
  var CHIPS = {
    nyanko: ["支払い方法は？", "どのくらいで終わる？", "引継ぎコードはどうなる？", "私の注文はどうなってる？"],
    tsum: ["支払い方法は？", "画像認証って何？", "引き継ぎ番号は？", "私の注文はどうなってる？"],
    puni: ["支払い方法は？", "どのくらいかかる？", "アカウント作成って安全？", "私の注文はどうなってる？"]
  };
  var KEY = "qa-hist-" + SHOP, SEEN = "qa-seen";
  var MAX_KEEP = 20, MAX_SEND = 6;

  function store(k, v) { try { if (v === undefined) return sessionStorage.getItem(k); sessionStorage.setItem(k, v); } catch (e) { return null; } }
  function loadHist() { try { var h = JSON.parse(store(KEY) || "[]"); return Array.isArray(h) ? h : []; } catch (e) { return []; } }
  var hist = loadHist();
  function saveHist() { store(KEY, JSON.stringify(hist.slice(-MAX_KEEP))); }

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  /* ---- ボタン ---- */
  var fab = el("button", "qa-fab");
  fab.type = "button";
  fab.setAttribute("aria-label", "質問チャットを開く");
  fab.append(el("span", "qa-ico", "💬"), el("span", "qa-lbl", "質問する"));

  /* ---- パネル ---- */
  var panel = el("div", "qa-panel");
  panel.hidden = true;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-label", "質問チャット");
  var sheet = el("div", "qa-sheet");
  var head = el("div", "qa-head");
  var ttl = el("div", "qa-ttl");
  ttl.append(el("b", null, "サポート"), el("small", null, "ふだんは数秒でお返事します"));
  var reset = el("button", "qa-reset", "最初から");
  reset.type = "button";
  var x = el("button", "qa-x", "×");
  x.type = "button";
  x.setAttribute("aria-label", "閉じる");
  head.append(el("div", "qa-av", "🐾"), ttl, reset, x);
  var log = el("div", "qa-log");
  log.setAttribute("aria-live", "polite");
  var chips = el("div", "qa-chips");
  var form = el("form", "qa-form");
  var input = el("input");
  input.maxLength = 300;
  input.placeholder = "メッセージを入力…";
  input.autocomplete = "off";
  input.setAttribute("aria-label", "メッセージ");
  input.setAttribute("enterkeyhint", "send");
  var send = el("button", null, "➤");
  send.type = "submit";
  send.setAttribute("aria-label", "送信");
  form.append(input, send);
  sheet.append(head, log, chips, form);
  panel.append(sheet);

  (CHIPS[SHOP] || CHIPS.nyanko).forEach(function (t) {
    var b = el("button", null, t);
    b.type = "button";
    b.addEventListener("click", function () { ask(t); });
    chips.append(b);
  });

  function bubble(text, who) {
    var d = el("div", "qa-msg " + who, text);
    log.append(d);
    log.scrollTop = log.scrollHeight;
    return d;
  }
  function render() {
    log.textContent = "";
    bubble(GREET[SHOP] || GREET.nyanko, "bot");
    hist.forEach(function (m) { bubble(m.text, m.role === "user" ? "me" : "bot"); });
  }

  var busy = false;
  async function ask(q) {
    q = String(q || "").trim();
    if (!q || busy) return;
    busy = true;
    send.disabled = true;
    input.value = "";
    bubble(q, "me");
    var wait = el("div", "qa-msg bot");
    var dots = el("span", "qa-typing");
    dots.append(el("i"), el("i"), el("i"));
    wait.append(dots);
    log.append(wait);
    log.scrollTop = log.scrollHeight;
    var sendHist = hist.slice(-MAX_SEND).map(function (m) { return { role: m.role, text: m.text }; });
    try {
      var r = await fetch("/api/qa", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ q: q, shop: SHOP, history: sendHist }) });
      var j = {};
      try { j = await r.json(); } catch (e) {}
      if (r.ok && j.answer) {
        wait.textContent = j.answer;
        hist.push({ role: "user", text: q }, { role: "assistant", text: j.answer });
        saveHist();
      } else {
        wait.textContent = j.error || "うまく送れませんでした。少しおいてもう一度どうぞ。";
        wait.classList.add("err");
      }
    } catch (e) {
      wait.textContent = "通信できませんでした。電波の良いところでもう一度どうぞ。";
      wait.classList.add("err");
    } finally {
      busy = false;
      send.disabled = false;
      log.scrollTop = log.scrollHeight;
      if (!panel.hidden) input.focus();
    }
  }

  function open() {
    stopNudge();
    store(SEEN, "1");
    render();
    panel.hidden = false;
    document.body.style.overflow = "hidden";
    fab.classList.add("qa-hide");
    requestAnimationFrame(function () { panel.classList.add("qa-open"); });
    setTimeout(function () { input.focus(); }, 280);
  }
  function close() {
    panel.classList.remove("qa-open");
    fab.classList.remove("qa-hide");
    document.body.style.overflow = "";
    setTimeout(function () { panel.hidden = true; }, 260);
    fab.focus();
  }

  fab.addEventListener("click", open);
  x.addEventListener("click", close);
  panel.addEventListener("click", function (e) { if (e.target === panel) close(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !panel.hidden) close(); });
  reset.addEventListener("click", function () { hist = []; saveHist(); render(); input.focus(); });
  form.addEventListener("submit", function (e) { e.preventDefault(); ask(input.value); });

  /* ---- 動き：登場 → ときどき揺れて気づかせる（開くまで・最大3回）→ スクロールで縮む ---- */
  var nudges = 0, nudgeTimer = null;
  function nudge() {
    if (!panel.hidden || nudges >= 3) { stopNudge(); return; }
    nudges += 1;
    fab.classList.remove("qa-nudge");
    void fab.offsetWidth;
    fab.classList.add("qa-nudge");
  }
  function stopNudge() { if (nudgeTimer) { clearInterval(nudgeTimer); nudgeTimer = null; } }
  fab.addEventListener("animationend", function (e) {
    if (e.target === fab && e.animationName === "qa-hop") fab.classList.remove("qa-nudge");
  });

  var lastY = window.scrollY;
  window.addEventListener("scroll", function () {
    var y = window.scrollY;
    if (y > 120 && y > lastY + 4) fab.classList.add("qa-mini");
    else if (y < lastY - 4 || y < 60) fab.classList.remove("qa-mini");
    lastY = y;
  }, { passive: true });

  function mount() {
    document.body.append(fab, panel);
    setTimeout(function () { fab.classList.add("qa-in"); }, 600);
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!reduce && !store(SEEN)) {
      setTimeout(nudge, 2500);
      nudgeTimer = setInterval(nudge, 9000);
    }
  }
  if (document.body) mount(); else document.addEventListener("DOMContentLoaded", mount);
})();
