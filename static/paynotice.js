/* 「Kyashの決済に移行しました」のお知らせ（管理者以外・画面を開くたびに出す）。
   出すかどうかはサーバーが決める（Kyash を受け付けていて、管理者でないときだけ、このスクリプトを読み込む）。
   data-paypay="1" … PayPay もまだ使えるときは、その一言を添える。 */
(function () {
  "use strict";
  var me = document.currentScript;
  var paypay = me && me.getAttribute("data-paypay") === "1";
  var offPct = parseInt((me && me.getAttribute("data-off")) || "0", 10) || 0;   /* PayPay 休止中の全品割引（%） */
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
    if (!paypay) {   /* PayPay を止めているあいだは、目立つように伝える */
      var off = document.createElement("p");
      off.style.cssText = "margin:0 0 10px;padding:8px 10px;border-radius:10px;background:#fdecec;color:#b42323;font-weight:800;font-size:14px";
      off.textContent = "⚠ PayPay は使えなくなりました。お支払いは Kyash でお願いします🙇";
    }
    if (offPct > 0) {
      var deal = document.createElement("p");
      deal.style.cssText = "margin:0 0 10px;padding:10px;border-radius:10px;background:#fff4cc;border:2px solid #ffc42e;color:#17181c;font-weight:800;font-size:17px;text-align:center";
      deal.textContent = "🎉 そのかわり、いまは全品 " + offPct + "%オフ！";
      var dealSub = document.createElement("small");
      dealSub.style.cssText = "display:block;font-weight:600;font-size:12px;color:#5b6472;margin-top:2px";
      dealSub.textContent = "お支払いの金額から自動で割り引きます（支援BOXは対象外）";
      deal.append(dealSub);
    }
    /* Kyash を知らない人向けの、たたんで読める説明 */
    var what = document.createElement("details");
    what.style.cssText = "margin:0 0 14px;border:1px solid #e2e6ee;border-radius:10px;padding:8px 12px;background:#f7f9fc";
    var wsum = document.createElement("summary");
    wsum.textContent = "Kyash（キャッシュ）ってなに？";
    wsum.style.cssText = "cursor:pointer;font-weight:700;font-size:14px;color:#17181c";
    var wp = document.createElement("p");
    wp.style.cssText = "margin:8px 0 0;font-size:13px;line-height:1.75;color:#3a4150";
    wp.textContent = "スマホで使える、無料のお財布アプリです。銀行口座・コンビニ・セブン銀行ATM・他のカードからお金をチャージして、相手に「送金リンク」を送って支払えます。App Store / Google Play で「Kyash」と検索すると入れられます。";
    var wsteps = document.createElement("ol");
    wsteps.style.cssText = "margin:8px 0 0;padding-left:1.3em;font-size:13px;line-height:1.7;color:#3a4150";
    ["Kyashアプリを入れて、使う分だけチャージする",
     "表示された金額ちょうどの送金リンクを作る",
     "そのリンク（https://kyash.me/payments/…）をコピーして、お支払い画面に貼る"].forEach(function (t) {
      var li = document.createElement("li"); li.textContent = t; wsteps.append(li);
    });
    what.append(wsum, wp, wsteps);
    var ok = document.createElement("button");
    ok.type = "button";
    ok.textContent = "OK";
    ok.style.cssText = "display:block;width:100%;padding:12px;border-radius:12px;border:2px solid #17181c;background:#ffc42e;color:#17181c;font-weight:800;font-size:16px;cursor:pointer";
    function close() { wrap.remove(); document.removeEventListener("keydown", onKey); }
    function onKey(e) { if (e.key === "Escape") close(); }
    ok.addEventListener("click", close);
    wrap.addEventListener("click", function (e) { if (e.target === wrap) close(); });
    document.addEventListener("keydown", onKey);
    box.append(h, p, ol, note, what, ok);
    if (!paypay) p.after(off);
    if (offPct > 0) (paypay ? p : off).after(deal);
    wrap.append(box);
    document.body.append(wrap);
    ok.focus();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", show);
  else show();
})();
