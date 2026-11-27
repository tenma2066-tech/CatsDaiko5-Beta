/* 「Kyashの決済に移行しました」のお知らせ（管理者以外・画面を開くたびに出す）。
   出すかどうかはサーバーが決める（Kyash を受け付けていて、管理者でないときだけ、このスクリプトを読み込む）。
   data-paypay="1" … PayPay もまだ使えるときは、その一言を添える。 */
(function () {
  "use strict";
  var me = document.currentScript;
  var paypay = me && me.getAttribute("data-paypay") === "1";
  function show() {
    var wrap = document.createElement("div");
    wrap.setAttribute("role", "dialog");
    wrap.setAttribute("aria-modal", "true");
    wrap.setAttribute("aria-labelledby", "kyNoticeTitle");
    wrap.style.cssText = "position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;padding:16px";
    var box = document.createElement("div");
    box.style.cssText = "background:#fff;color:#17181c;max-width:420px;width:100%;border-radius:16px;border:3px solid #17181c;padding:20px 18px 16px;" +
                        "box-shadow:0 10px 30px rgba(0,0,0,.3);font-size:15px;line-height:1.7;max-height:90vh;overflow:auto";
    var h = document.createElement("h2");
    h.id = "kyNoticeTitle";
    h.textContent = "Kyashの決済に移行しました！";
    h.style.cssText = "margin:0 0 10px;font-size:20px;line-height:1.4";
    var p = document.createElement("p");
    p.style.margin = "0 0 8px";
    p.textContent = "お支払いは Kyash の送金リンクでできます。";
    var ol = document.createElement("ol");
    ol.style.cssText = "margin:0 0 8px;padding-left:1.3em";
    ["メニューを選んで「お支払いへ進む」を押す",
     "Kyashアプリで、表示された金額ちょうどの送金リンクを作る",
     "できたリンク（https://kyash.me/payments/…）をコピーして、お支払い画面に貼り付ける"].forEach(function (t) {
      var li = document.createElement("li"); li.textContent = t; ol.append(li);
    });
    var note = document.createElement("p");
    note.style.cssText = "margin:0 0 14px;font-size:13px;color:#5b6472";
    note.textContent = "入金が確認でき次第、自動で実行します。" + (paypay ? "PayPay の送金リンクも、これまでどおり使えます。" : "");
    var ok = document.createElement("button");
    ok.type = "button";
    ok.textContent = "OK";
    ok.style.cssText = "display:block;width:100%;padding:12px;border-radius:12px;border:2px solid #17181c;background:#ffc42e;color:#17181c;font-weight:800;font-size:16px;cursor:pointer";
    function close() { wrap.remove(); document.removeEventListener("keydown", onKey); }
    function onKey(e) { if (e.key === "Escape") close(); }
    ok.addEventListener("click", close);
    wrap.addEventListener("click", function (e) { if (e.target === wrap) close(); });
    document.addEventListener("keydown", onKey);
    box.append(h, p, ol, note, ok);
    wrap.append(box);
    document.body.append(wrap);
    ok.focus();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", show);
  else show();
})();
