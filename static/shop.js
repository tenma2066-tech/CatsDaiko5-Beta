/* ツムツム・ぷにぷに共通の「注文まわり」。
   支払いパネル・このブラウザの注文の一覧・削除・注文番号での再開・完了後のクーポン表示をまとめてある。
   各画面（templates/tsum.html・puni.html）は、メニューの選び方と結果の見せ方だけを書き、

     const orders = ShopOrders({ shop:'tsum', payments:true, couponMin:200,
                                 isBusy:()=>!!curJob, watch, afterClose:renderCart,
                                 review:{min:20,max:400,pastUrl:''}, isAdmin:false,
                                 result:{ has:o=>!!o.result, label:'引き継ぎ情報を見る', name:'引き継ぎ情報', show:o=>… } });

   のように呼ぶ。支払いの決まり（保留は送り直さない・支払い済みは再送金なしで再開）はここだけに書く。 */
(function () {
  "use strict";
  var $ = function (s) { return document.querySelector(s); };
  var yen = function (n) { return Number(n || 0).toLocaleString("ja-JP") + "円"; };
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  var STATE_JA = { unpaid: "お支払い待ち", paying: "確認中", hold: "保留中（自動で確認しています）", paid: "お支払い済み・開始待ち",
                   starting: "開始処理中", started: "実行中", done: "完了", dead: "自動では確認できませんでした" };
  var HOLD_MSG = "保留中です。送金した方がアプリで保留を解除する（Kyashは明細に反映される）と、自動で確認して実行します。" +
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

  /* 注文の前に、クーポン・チケットでいくらになるかを見せる（確定はサーバー。ここは見込み）。
     コードの頭で種類が分かる：FR-＝1回無料／PS-＝期間無料／SP-＝30%引き／それ以外（NK-）＝ min 円以上で10%引き */
  window.couponPreview = function (total, code, min) {
    code = String(code || "").normalize("NFKC").toUpperCase().replace(/\s/g, "");
    if (!code || !total) return { pay: total, note: "" };
    if (/^(FR|PS)-/.test(code)) return { pay: 0, note: (code.indexOf("PS-") === 0 ? "期間無料チケット" : "無料チケット") + "で 0円（使えるかは、進むときに確認します）" };
    if (/^SP-/.test(code)) { var p = Math.floor(total * 70 / 100); return { pay: p, note: "支援クーポン（30%引き）で " + yen(total - p) + " 引き" }; }
    if (total >= min) { var q = Math.floor(total * 9 / 10); return { pay: q, note: "クーポン（10%引き）で " + yen(total - q) + " 引き" }; }
    return { pay: total, note: "このクーポンは " + min + "円以上のご注文で使えます（いまは " + yen(total) + "）" };
  };

  window.ShopOrders = function (cfg) {
    var SEEN_KEY = cfg.shop + "-seen-jobs";
    var payOrder = null, payAmount = 0, orderTimer = null;

    function seenJobs() { try { var v = JSON.parse(sessionStorage.getItem(SEEN_KEY) || "[]"); return Array.isArray(v) ? v : []; } catch (e) { return []; } }
    function markSeen(job) { try { var v = seenJobs(); if (!v.includes(job)) sessionStorage.setItem(SEEN_KEY, JSON.stringify(v.concat(job).slice(-20))); } catch (e) {} }
    /* 終わりまで見届けた実行は、この端末に覚えておく（別のタブで開き直しても、同じ完了をもう一度自動で開かない） */
    var DONE_KEY = cfg.shop + "-done-jobs";
    function doneJobs() { try { var v = JSON.parse(localStorage.getItem(DONE_KEY) || "[]"); return Array.isArray(v) ? v : []; } catch (e) { return []; } }
    function markDone(job) { if (!job) return; try { var v = doneJobs(); if (!v.includes(job)) localStorage.setItem(DONE_KEY, JSON.stringify(v.concat(job).slice(-30))); } catch (e) {} }
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
        var r = await postJson("/api/orders/" + payOrder + "/pay", { link: link, human: window.Human ? await window.Human.get(8000) : "" });
        var j = await r.json();
        if (window.Human) window.Human.reset();   /* 確認の印は1回しか使えない */
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
        loadResume();
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
      if (!linkField().hidden) {   /* PayPay・Kyash のどちらか（いま受け付けている方）。paymethods.js が無ければ両方を通す */
        var bad = window.PayMethods ? window.PayMethods.check(link)
          : (/pay\.paypay\.ne\.jp\/|kyash\.me\/payments\//i.test(link) ? "" : "PayPayかKyashの送金リンクを貼り付けてください。");
        if (bad) { $("#payErr").textContent = bad; return; }
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
      /* 終わっている実行も含める：保留の解除や管理者の操作のあと、次に確かめるまでのあいだに終わっていることがある */
      var live = list.find(function (o) { return o.job && !seenJobs().includes(o.job) && (o.state === "started" || (o.state === "done" && !doneJobs().includes(o.job))); });
      if (live && !cfg.isBusy() && !($("#reviewPanel") && !$("#reviewPanel").hidden)) {   /* 評価の入力中は、画面を切り替えない */ if (!$("#payPanel").hidden && payOrder === live.order) { $("#payPanel").hidden = true; document.body.style.overflow = ""; } cfg.watch(live.job); }
      var box = $("#resumeList"); box.textContent = "";
      $("#s-resume").hidden = !list.length;
      list.forEach(function (o) {
        var d = el("div", "code"), t = el("div");
        t.append(el("small", null, STATE_JA[o.state] || o.state), el("strong", null, yen(o.amount) + "（注文 " + o.order + "）"));
        if (o.state === "done" && o.coupon_issued && !o.locked)   /* 次回クーポンは、あとから開いた人にも見せる */
          t.append(el("small", null, "🎟 次回クーポン " + o.coupon_issued + "（" + cfg.couponMin + "円以上のご注文で10%引き・1回きり）"));
        if (o.state === "done" && o.unknown && !cfg.result.has(o))
          t.append(el("small", null, "途中で止まり、結果を確認できませんでした。注文番号を管理者に伝えてください。"));
        if (o.state === "paid" && o.retry) t.append(el("small", null, o.resume
          ? "途中で止まりました（" + o.resume + "）。お支払いは有効です。続きから実行できます（再送金は不要）。"
          : "前回は実行できませんでした。お支払いは有効です（再送金は不要）。"));
        d.append(t);
        var b = el("button", "btn primary"); b.type = "button";
        if (o.state === "done" && o.locked && gateOn) {
          b.textContent = "評価を書いて結果を見る";
          b.addEventListener("click", function () { reviewThenShow(o); }); d.append(b);
        } else if (o.state === "done" && cfg.result.has(o)) {
          b.textContent = typeof cfg.result.label === "function" ? cfg.result.label(o) : cfg.result.label;
          b.addEventListener("click", function () { showResult(o); }); d.append(b);
        } else if (o.job && (o.state === "started" || o.state === "done")) {
          b.textContent = o.state === "done" ? "結果を見る" : "状況を見る";
          b.addEventListener("click", function () { cfg.watch(o.job); }); d.append(b);
        } else if (o.state === "dead" || (o.state === "done" && o.unknown)) {
          b.textContent = "注文番号をコピー";
          b.addEventListener("click", async function () { b.textContent = (await copyText(o.order)) ? "コピーしました。管理者に伝えてください" : "コピーできません"; });
          d.append(b);
        } else if (["unpaid", "hold", "paid"].includes(o.state)) {
          b.textContent = o.state === "paid" ? (o.resume ? "続きから実行する" : "実行を開始する") : o.state === "hold" ? "状況を確認する" : "支払い画面を開く";
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
      /* 開いたままの支払いパネルの注文が、もう先へ進んでいる（別のタブ・管理者の操作）→ 古い画面を残さない */
      if (mine && !$("#payPanel").hidden) {
        if (mine.state === "done" || mine.state === "started") { $("#payPanel").hidden = true; document.body.style.overflow = ""; payOrder = null; }
        else if (mine.state === "dead" && !$("#payGo").disabled) {
          $("#payErr").textContent = "自動では確認できませんでした。お支払い済みの場合は、注文番号を管理者に伝えてください（再送金は不要です）。";
          $("#payGo").disabled = true;
        }
      }
      if (list.some(function (o) { return WAITING.includes(o.state) || (o.state === "started" && o.job) || (o.state === "paid" && o.retry && o.long); })) startOrderWatch(); else stopOrderWatch();
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

    /* ---------- 実績（評価と感想）：完了した注文の結果は、評価を受け取ってからサーバーが渡す ----------
       サーバーは、評価がまだの注文の結果を返さない（/status は result_locked、注文一覧は locked）。
       ここでは入力欄（static/review.js）を出し、送信できたら結果を取り直すだけ。画面側で隠しているわけではない。 */
    var gateOn = !!(cfg.review && window.ReviewForm && $("#reviewPanel"));
    function openReview(order, job, then) {
      var form = window.ReviewForm($("#reviewForm"), { min: cfg.review.min, max: cfg.review.max, pastUrl: cfg.review.pastUrl,
        target: function () { return { order: order, job: job }; },
        onDone: function () { resetReview(); if (then) then(); else loadResume(); } });
      $("#reviewPanel").hidden = false; document.body.style.overflow = "hidden";
      form.focus();
    }
    /* 実行が完了したが、結果が「評価待ち」で届かなかったとき。then＝送信後に結果を取り直す処理 */
    function requireReview(job, then) { if (gateOn && job) openReview("", job, then); }
    function resetReview() {
      if ($("#reviewPanel")) $("#reviewPanel").hidden = true;
      if ($("#payPanel").hidden) document.body.style.overflow = "";
    }
    /* 一覧の「評価を書いて結果を見る」：送信できたら、届くようになった結果をそのまま開く */
    function reviewThenShow(o) {
      openReview(o.order, o.job || "", async function () {
        await loadResume();
        try {
          var fresh = (await myOrders()).find(function (x) { return x.order === o.order; });
          if (fresh && cfg.result.has(fresh) && !cfg.isBusy()) cfg.result.show(fresh);
        } catch (e) {}
      });
    }
    /* 一覧から完了注文の結果を開く。実行中の画面があるときは、取り違えないよう先にそちらを終わらせてもらう */
    function showResult(o) {
      if (cfg.isBusy()) { alert("いま実行中の注文があります。完了してから、もう一度開いてください。"); return; }
      cfg.result.show(o);
    }

    return { api: api, postJson: postJson, copyText: copyText, seenJobs: seenJobs, markSeen: markSeen, markDone: markDone,
             begin: begin, loadResume: loadResume, loadCoupon: loadCoupon,
             requireReview: requireReview, resetReview: resetReview };
  };
})();
