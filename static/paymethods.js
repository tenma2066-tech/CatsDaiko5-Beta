/* 支払いパネルの「PayPay ／ Kyash」切り替え（にゃんこ・ツムツム・ぷにぷに・チケット等の共通）。
   <script src="/static/paymethods.js" data-methods='{"paypay":true,"kyash":true}'> のように読み込む。
   - 使える方法が2つなら、リンク欄の上に選ぶボタンを出す。1つならそれだけの案内にする。
   - 貼られたリンクの形（pay.paypay.ne.jp ／ kyash.me）で、自動でその方法に切り替える。
   - パネルの中の「PayPay」の文字・リンク例を、選んだ方法に合わせて書き換える（各画面のスクリプトが後から書いた文字も）。
   どちらで受け取るかはサーバーがリンクの形で決めるので、ここは案内と入力チェックだけ。 */
(function () {
  "use strict";
  var me = document.currentScript;
  var on = { paypay: true, kyash: false };
  try { var d = JSON.parse(me.getAttribute("data-methods") || "{}"); on = { paypay: !!d.paypay, kyash: !!d.kyash }; } catch (e) {}
  var LABEL = { paypay: "PayPay", kyash: "Kyash" };
  var EXAMPLE = { paypay: "https://pay.paypay.ne.jp/...", kyash: "https://kyash.me/payments/..." };
  var list = ["paypay", "kyash"].filter(function (m) { return on[m]; });
  var KEY = "pay-method";
  var cur = null;
  try { cur = localStorage.getItem(KEY); } catch (e) {}
  if (list.indexOf(cur) < 0) cur = list[0] || "paypay";

  function detect(text) {
    if (/kyash\.me\//i.test(text || "")) return "kyash";
    if (/paypay\.ne\.jp\//i.test(text || "")) return "paypay";
    return null;
  }
  /* 入力チェック（各画面から使う）。問題なければ "" */
  window.PayMethods = {
    on: on, list: list,
    current: function () { return cur; },
    check: function (link) {
      var m = detect(link);
      if (!list.length) return "ただいまお支払いを受け付けていません。しばらくしてからお試しください（送金はしないでください）。";
      if (!m) return list.map(function (x) { return LABEL[x] + "（" + EXAMPLE[x].replace("...", "…") + "）"; }).join("か") + "の送金リンクを貼り付けてください。";
      if (!on[m]) return "ただいま" + LABEL[m] + "でのお支払いは止めています。" + list.map(function (x) { return LABEL[x]; }).join("・") + "の送金リンクを貼り付けてください。";
      return "";
    }
  };

  var panel = document.getElementById("payPanel");
  var input = document.getElementById("payLink");
  if (!panel || !input) return;
  var field = input.closest("label") || input.parentNode;

  /* 選ぶボタンと一言の案内 */
  var box = document.createElement("div");
  box.className = "paymethod";
  box.style.cssText = "margin:10px 0 6px";
  var row = document.createElement("div");
  row.setAttribute("role", "radiogroup");
  row.setAttribute("aria-label", "お支払い方法");
  row.style.cssText = "display:flex;gap:8px;flex-wrap:wrap";
  var btns = {};
  list.forEach(function (m) {
    var b = document.createElement("button");
    b.type = "button"; b.textContent = LABEL[m]; b.setAttribute("role", "radio");
    b.style.cssText = "flex:1;min-width:110px;padding:10px 12px;border-radius:10px;font-weight:800;cursor:pointer;border:2px solid #999;background:transparent;color:inherit";
    b.addEventListener("click", function () { choose(m, true); });
    btns[m] = b; row.append(b);
  });
  var hint = document.createElement("p");
  hint.style.cssText = "margin:6px 0 0;font-size:13px;opacity:.85";
  if (list.length > 1) box.append(row);
  box.append(hint);
  field.parentNode.insertBefore(box, field);

  /* パネルの文字の書き換え（PayPay ⇄ Kyash）。テキストとリンク例だけを触る */
  var SWAP = {
    kyash: [[/PayPay ?アプリ/g, "Kyashアプリ"], [/PayPay/g, "Kyash"], [/https:\/\/pay\.paypay\.ne\.jp\/(…|\.\.\.)?/g, "https://kyash.me/payments/$1"]],
    paypay: [[/Kyash ?アプリ/g, "PayPayアプリ"], [/Kyash/g, "PayPay"], [/https:\/\/kyash\.me\/payments\/(…|\.\.\.)?/g, "https://pay.paypay.ne.jp/$1"]]
  };
  function swapText(root, m) {
    var rules = SWAP[m];
    var walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var n, todo = [];
    while ((n = walk.nextNode())) { if (!box.contains(n)) todo.push(n); }
    todo.forEach(function (t) {
      var v = t.nodeValue, w = v;
      rules.forEach(function (r) { w = w.replace(r[0], r[1]); });
      if (w !== v) t.nodeValue = w;
    });
    input.placeholder = EXAMPLE[m];
  }
  var obs = new MutationObserver(function () { apply(); });
  function watch() { obs.observe(panel, { childList: true, subtree: true, characterData: true }); }
  function apply() {
    obs.disconnect();
    try {
      if (list.length) swapText(panel, cur);
      list.forEach(function (m) {
        var sel = m === cur;
        btns[m].setAttribute("aria-checked", sel ? "true" : "false");
        btns[m].style.borderColor = sel ? "#f5c542" : "#999";
        btns[m].style.background = sel ? "rgba(245,197,66,.18)" : "transparent";
      });
      hint.textContent = !list.length ? "ただいまお支払いを受け付けていません。しばらくしてからお試しください（送金はしないでください）。"
        : list.length > 1 ? "PayPay・Kyashどちらのリンクでも、貼れば自動で判別します。" : "";
      hint.hidden = !hint.textContent;
      box.hidden = field.hidden;   /* 保留中・支払い済みの表示ではリンク欄と一緒に隠す */
    } finally { watch(); }
  }
  function choose(m, byUser) {
    if (list.indexOf(m) < 0) return;
    cur = m;
    if (byUser) { try { localStorage.setItem(KEY, m); } catch (e) {} }
    apply();
  }
  input.addEventListener("input", function () { var m = detect(input.value); if (m && m !== cur && on[m]) choose(m, true); });
  new MutationObserver(apply).observe(field, { attributes: true, attributeFilter: ["hidden"] });
  apply();
})();
