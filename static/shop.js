/* ツムツム・ぷにぷに共通の「注文まわり」。
   支払いパネル・このブラウザの注文の一覧・削除・注文番号での再開・完了後のクーポン表示をまとめてある。
   各画面（templates/tsum.html・puni.html）は、メニューの選び方と結果の見せ方だけを書き、

     const orders = ShopOrders({ shop:'tsum', payments:true, couponMin:200,
                                 isBusy:()=>!!curJob, watch, afterClose:renderCart,
                                 result:{ has:o=>!!o.result, label:'引き継ぎ情報を見る', name:'引き継ぎ情報', show:o=>… } });

   のように呼ぶ。支払いの決まり（保留は送り直さない・支払い済みは再送金なしで再開）はここだけに書く。 */
(function () {
  "use strict";
  var $ = function (s) { return document.querySelector(s); };
  var yen = function (n) { return Number(n || 0).toLocaleString("ja-JP") + "円"; };
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  var STATE_JA = { unpaid: "お支払い待ち", paying: "確認中", hold: "保留中（自動で確認しています）", paid: "お支払い済み・開始待ち",
                   starting: "開始処理中", started: "実行中", done: "完了", dead: "自動では確認できませんでした" };
  var HOLD_MSG = "保留中です。送金した方がPayPayアプリで保留を解除すると、自動で確認して実行します。" +
                 "同じリンクを送り直したり、再送金したりしないでください。この画面は閉じても大丈夫です。";
  var WAITING = ["hold", "paying", "starting"];
  /* ほかの画面の注文番号を入れたときの行き先 */
  function homeOf(o) {
    if (o.tsum) return ["/tsum", "ツムツム代行"];
    if (o.puni) return ["/puni", "ぷにぷに代行"];
    if (o.product === "support") return ["/support", "支援BOX"];
    if (o.product === "source") return ["/source", "ソースコード購入"];
    if (o.product) return ["/ticket", "チケット購入"];
    return ["/nyanko", "にゃんこ代行"];
  }

  /* アップデート中（503 updating）は数秒おきに自動でやり直す。メンテナンスなど、ほかの 503 はすぐ返す。 */
  async function api(url, opts, tries) {
    tries = tries || 40;
    for (var i = 0; ; i++) {
      var r = await fetch(url, Object.assign({ cache: "no-store" }, opts || {}));
      if (r.status !== 503 || i >= tries - 1) return r;
      var j = {};
      try { j = await r.clone().json(); } catch (e) {}
      if (j.code !== "updating") return r;
      var n = $("#runErr"); if (n) n.textContent = j.error || "ただいまアップデート中です。自動で続きます…";
      await new Promise(function (ok) { setTimeout(ok, 5000); });
    }
  }
  function postJson(url, body) {
    return api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  }
  async function copyText(val) {
    try { await navigator.clipboard.writeText(val); return true; } catch (e) {}
    try { /* http や一部のアプリ内ブラウザでは clipboard が使えないので、古い方法で */
      var t = el("textarea"); t.value = val; t.setAttribute("readonly", "");
      t.style.position = "fixed"; t.style.opacity = "0"; document.body.append(t); t.select();
      var ok = document.execCommand("copy"); t.remove(); return ok;
    } catch (e) { return false; }
  }

  window.ShopOrders = function (cfg) {
    var SEEN_KEY = cfg.shop + "-seen-jobs";
    var payOrder = null, payAmount = 0, orderTimer = null;

    function seenJobs() { try { var v = JSON.parse(sessionStorage.getItem(SEEN_KEY) || "[]"); return Array.isArray(v) ? v : []; } catch (e) { return []; } }
    function markSeen(job) { try { var v = seenJobs(); if (!v.includes(job)) sessionStorage.setItem(SEEN_KEY, JSON.stringify(v.concat(job).slice(-20))); } catch (e) {} }
    async function myOrders() {
      var r = await fetch("/api/orders/mine", { cache: "no-store" });
      var j = await r.json();
      return (j.orders || []).filter(function (o) { return o[cfg.shop]; });
    }

    /* ---------- 支払いパネル ---------- */
    var linkField = function () { return $("#payLink").closest("label"); };
    /* state … unpaid＝リンクを貼る／paid＝支払い済み（再送金なしで開始）／hold＝保留（確認だけ） */
    function openPay(order, amount, state, coupons) {
      payOrder = order; payAmount = amount; state = state || "unpaid";
      var fresh = state === "unpaid";
      $("#payYen").textContent = yen(amount);
      $("#payOrderNo").textContent = "注文番号：" + order + "（控えておくと、別の端末からも続きを見られます）";
      $("#payLink").value = ""; linkField().hidden = !fresh; $("#payInfo").hidden = !fresh;
      $("#payGo").disabled = false;
      $("#payGo").textContent = fresh ? "支払って実行する" : state === "paid" ? "実行を開始する（再送金なし）" : "今すぐ確認する";
      $("#payCancel").textContent = state === "hold" ? "閉じる（確認は続きます）" : fresh ? "やめる" : "閉じる";
      $("#payErr").textContent = state === "hold" ? HOLD_MSG : state === "paid" ? "お支払いは確認済みです。ボタンを押すと実行を開始します。" : "";
      var c = $("#payCoupon");
      if (c) { c.hidden = !(coupons && coupons.length); c.textContent = (coupons || []).map(function (b) { return "🎟 " + b.label + "：" + yen(b.unit); }).join("\n"); }
      $("#payPanel").hidden = false; document.body.style.overflow = "hidden";
      (fresh ? $("#payLink") : $("#payGo")).focus();
      if (state === "hold") startOrderWatch();
    }
    function closePay() { $("#payPanel").hidden = true; document.body.style.overflow = ""; cfg.afterClose(); loadResume(); }
    async function payNow(link) {
      var btn = $("#payGo"); btn.disabled = true;
      $("#payErr").textContent = "決済を確認しています…（画面を閉じずにお待ちください）";
      try {
        var r = await postJson("/api/orders/" + payOrder + "/pay", { link: link });
        var j = await r.json();
        if (!r.ok) {
          $("#payErr").textContent = j.error || "決済を確認できませんでした。";
          if (j.hold) { openPay(payOrder, payAmount, "hold"); return; }   /* PayPay 側の保留：同じリンクを送り直さない */
          if (j.code === "started") { closePay(); return; }
          if (j.paid) {   /* 支払いは済んでいて、開始だけできなかった（待機枠がいっぱいなど）→ 再送金なしで押し直せる */
            linkField().hidden = true; $("#payInfo").hidden = true; btn.textContent = "実行を開始する（再送金なし）";
            $("#payErr").textContent = (j.error || "開始できませんでした。") + " お支払いは完了しています（再送金は不要です）。";
          }
          btn.disabled = ["dead", "order_missing", "done"].includes(j.code);
          return;
        }
        $("#payPanel").hidden = true; document.body.style.overflow = "";
        cfg.watch(j.job);
      } catch (e) { $("#payErr").textContent = "通信エラーです。もう一度お試しください（同じリンクのままで大丈夫です）。"; btn.disabled = false; }
    }
    /* /run の返事（注文ができた）を受け取って、支払いへ進める */
    function begin(j) {
      var coupons = (j.breakdown || []).filter(function (b) { return b.coupon; });
      if (j.state === "paid" && j.prepaid) { openPay(j.order, j.amount, "paid"); payNow(""); return; }
      openPay(j.order, j.amount, "unpaid", coupons);
    }
    $("#payCancel").addEventListener("click", closePay);
    $("#payPanel").addEventListener("click", function (e) { if (e.target === $("#payPanel")) closePay(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !$("#payPanel").hidden) closePay(); });
    $("#payLink").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); $("#payGo").click(); } });
    $("#payGo").addEventListener("click", function () {
      var link = $("#payLink").value.trim();
      if (!linkField().hidden && !/pay\.paypay\.ne\.jp\//i.test(link)) {
        $("#payErr").textContent = "PayPayの送金リンク（https://pay.paypay.ne.jp/…）を貼り付けてください。"; return;
      }
      payNow(link);
    });

    /* ---------- このブラウザの注文（未払い・保留・支払い済み・実行中・結果） ---------- */
    function startOrderWatch() { if (!orderTimer) orderTimer = setInterval(loadResume, 15000); }
    function stopOrderWatch() { clearInterval(orderTimer); orderTimer = null; }
    async function loadResume() {
      if (!cfg.payments) return;
      var list;
      try { list = await myOrders(); } catch (e) { return; }
      /* 実行が始まった注文（保留の解除・別タブ・開き直し）→ まだ見ていなければ状況を開く */
      var live = list.find(function (o) { return o.job && o.state === "started" && !seenJobs().includes(o.job); });
      if (live && !cfg.isBusy()) { if (!$("#payPanel").hidden && payOrder === live.order) { $("#payPanel").hidden = true; document.body.style.overflow = ""; } cfg.watch(live.job); }
      var box = $("#resumeList"); box.textContent = "";
      $("#s-resume").hidden = !list.length;
      list.forEach(function (o) {
        var d = el("div", "code"), t = el("div");
        t.append(el("small", null, STATE_JA[o.state] || o.state), el("strong", null, yen(o.amount) + "（注文 " + o.order + "）"));
        if (o.state === "paid" && o.retry) t.append(el("small", null, "前回は実行できませんでした。お支払いは有効です（再送金は不要）。"));
        d.append(t);
        var b = el("button", "btn primary"); b.type = "button";
        if (o.state === "done" && cfg.result.has(o)) {
          b.textContent = cfg.result.label;
          b.addEventListener("click", function () { cfg.result.show(o); }); d.append(b);
        } else if (o.job && (o.state === "started" || o.state === "done")) {
          b.textContent = o.state === "done" ? "結果を見る" : "状況を見る";
          b.addEventListener("click", function () { cfg.watch(o.job); }); d.append(b);
        } else if (o.state === "dead") {
          b.textContent = "注文番号をコピー";
          b.addEventListener("click", async function () { b.textContent = (await copyText(o.order)) ? "コピーしました。管理者に伝えてください" : "コピーできません"; });
          d.append(b);
        } else if (["unpaid", "hold", "paid"].includes(o.state)) {
          b.textContent = o.state === "paid" ? "実行を開始する" : o.state === "hold" ? "状況を確認する" : "支払い画面を開く";
          b.addEventListener("click", function () { openPay(o.order, o.amount, o.state); });
          d.append(b);
        }
        /* 完了・停止・未払いは、本人が一覧から消せる（表示だけ・管理台帳には残る） */
        if (["done", "dead", "unpaid"].includes(o.state)) {
          var del = el("button", "btn del", "削除"); del.type = "button";
          del.addEventListener("click", function () { forgetOrder(o, d); });
          d.append(del);
        }
        box.append(d);
      });
      /* 保留が解けて支払い済みになった注文を、開いたままの支払いパネルにも反映する */
      var mine = list.find(function (o) { return o.order === payOrder; });
      if (mine && mine.state === "paid" && !$("#payPanel").hidden && !$("#payGo").disabled && $("#payGo").textContent === "今すぐ確認する") openPay(mine.order, mine.amount, "paid");
      if (list.some(function (o) { return WAITING.includes(o.state) || (o.state === "started" && o.job); })) startOrderWatch(); else stopOrderWatch();
    }
    async function forgetOrder(o, row) {
      var msg = (o.state === "done" && cfg.result.has(o))
        ? "この完了注文を一覧から消します。あとで注文番号を入れれば、" + cfg.result.name + "をもう一度表示できます（完了から3日間）。よろしいですか？"
        : "この注文を一覧から消します（注文番号で元に戻せます）。よろしいですか？";
      if (!confirm(msg)) return;
      try {
        var r = await api("/api/orders/" + encodeURIComponent(o.order) + "/forget", { method: "POST" });
        if (r.ok) { row.remove(); loadResume(); }
        else { var j = await r.json().catch(function () { return {}; }); alert(j.error || "削除できませんでした。"); }
      } catch (e) { alert("通信に失敗しました。"); }
    }

    /* ---------- 別の端末・画面を閉じた場合：注文番号で再開 ---------- */
    async function claimOrder() {
      var input = $("#claimNo"), err = $("#claimErr"), no = (input.value || "").trim();
      err.textContent = "";
      if (!no) { err.textContent = "注文番号を入れてください。"; return; }
      var btn = $("#claimGo"); btn.disabled = true;
      try {
        var r = await api("/api/orders/" + encodeURIComponent(no) + "/claim", { method: "POST" });
        var j = {}; try { j = await r.json(); } catch (e) {}
        if (!r.ok) { err.textContent = j.error || "再開できませんでした。番号を確認してください。"; return; }
        if (!j[cfg.shop]) {   /* ほかの画面の注文。呼び戻しはできているので、その画面へ案内する */
          var to = homeOf(j), a = el("a", null, to[1] + " の画面を開く →"); a.href = to[0];
          err.textContent = "この番号は「" + to[1] + "」の注文です。"; err.append(" ", a); return;
        }
        input.value = "";
        await loadResume();
        $("#s-resume").hidden = false;
        $("#s-resume").scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (e) { err.textContent = "通信に失敗しました。少し待ってからお試しください。"; }
      finally { btn.disabled = false; }
    }
    if (cfg.payments) {
      $("#claimGo").addEventListener("click", claimOrder);
      $("#claimNo").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); claimOrder(); } });
    } else { var c = $("#s-claim"); if (c) c.hidden = true; }

    /* 完了した「その注文」で出た次回クーポンだけを表示する（前の注文のクーポンを取り違えない） */
    async function loadCoupon(job) {
      if (!cfg.payments || !job) return;
      for (var i = 0; i < 4; i++) {   /* クーポンは完了の数秒後に発行されるので、少し待ちながら確かめる */
        try {
          var o = (await myOrders()).find(function (x) { return x.job === job && x.coupon_issued; });
          if (o) { $("#coupBox").hidden = false; $("#coupBox").textContent = "🎟 次回使えるクーポン " + o.coupon_issued + "（次回 " + cfg.couponMin + "円以上のご注文で10%引き・1回きり・30日間）"; return; }
        } catch (e) {}
        await new Promise(function (ok) { setTimeout(ok, 1500 + i * 1500); });
      }
    }

    /* 完了した注文に、まだ実績（星と感想）が無ければ入力欄を出す。書かなくても結果は見られる（お願いだけ）。 */
    async function offerReview(job) {
      var box = $("#reviewBox");
      if (!box || !cfg.review || !window.ReviewForm || !cfg.payments || !job) return;
      var o = null;
      for (var i = 0; i < 3 && !o; i++) {   /* 完了が注文に反映されるまで数秒かかることがある */
        try { o = (await myOrders()).find(function (x) { return x.job === job && x.state === "done"; }); } catch (e) {}
        if (!o) await new Promise(function (ok) { setTimeout(ok, 1500); });
      }
      if (!o || o.reviewed) return;
      window.ReviewForm($("#reviewForm"), { min: cfg.review.min, max: cfg.review.max, pastUrl: cfg.review.pastUrl,
        target: function () { return { order: o.order, job: job }; },
        onDone: function (saved) {
          $("#reviewForm").textContent = "";
          $("#reviewForm").append(el("p", "okbox", saved ? "ありがとうございました！実績に掲載しました。" : "ありがとうございました！"));
        } });
      box.hidden = false;
    }

    return { api: api, postJson: postJson, copyText: copyText, seenJobs: seenJobs, markSeen: markSeen,
             begin: begin, loadResume: loadResume, loadCoupon: loadCoupon, offerReview: offerReview };
  };
})();
