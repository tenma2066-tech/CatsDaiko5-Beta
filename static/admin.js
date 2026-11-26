/* 管理画面：売上のグラフ・注文の検索と詳細・サーバーの点検。
   データは /admin/api/…（管理者のログインが必要）から取る。文字はすべて textContent で入れる（HTML として解釈させない）。 */
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "class") e.className = attrs[k]; else if (k === "style") e.style.cssText = attrs[k]; else e.setAttribute(k, attrs[k]);
    });
    if (text != null) e.textContent = text;
    return e;
  }
  function sv(tag, attrs) {
    var e = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    return e;
  }
  function yen(n) { return Math.round(n || 0).toLocaleString("ja-JP") + "円"; }
  function num(n) { return Math.round(n || 0).toLocaleString("ja-JP"); }
  function compact(n) {
    n = Math.round(n || 0);
    if (Math.abs(n) >= 100000000) return (n / 100000000).toFixed(n % 100000000 ? 1 : 0).replace(/\.0$/, "") + "億";
    if (Math.abs(n) >= 10000) return (n / 10000).toFixed(n % 10000 ? 1 : 0).replace(/\.0$/, "") + "万";
    return n.toLocaleString("ja-JP");
  }
  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function colors() { return { nyanko: cssVar("--s1"), tsum: cssVar("--s2"), puni: cssVar("--s3"), other: cssVar("--s4"),
                               grid: cssVar("--grid"), axis: cssVar("--axis"), panel: cssVar("--panel") }; }
  var DARK_TEXT = { other: true };   /* 黄色の上の文字は黒（白だと読めない） */
  async function getJSON(url) {
    var r = await fetch(url, { cache: "no-store", credentials: "same-origin" });
    var j = {}; try { j = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error(j.error || ("読み込めませんでした（" + r.status + "）"));
    return j;
  }
  function jstDate(d) {
    var p = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(d);
    var o = {}; p.forEach(function (x) { o[x.type] = x.value; });
    return o.year + "-" + o.month + "-" + o.day;
  }
  function niceMax(v) {
    if (v <= 0) return 100;
    var p = Math.pow(10, Math.floor(Math.log10(v))), f = v / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
  }

  /* ---------- ツールチップ（1つを使い回す） ---------- */
  function tipFor(box) {
    var t = $(".tip", box);
    if (!t) { t = el("div", { class: "tip", role: "status" }); t.hidden = true; box.append(t); }
    return t;
  }
  function showTip(box, x, y, title, rows) {
    var t = tipFor(box); t.textContent = "";
    t.append(el("div", { class: "t" }, title));
    rows.forEach(function (r) {
      var row = el("div", { class: "r" });
      if (r.color) row.append(el("span", { class: "k", style: "background:" + r.color }));
      row.append(el("span", null, r.label), el("b", null, r.value));
      t.append(row);
    });
    t.hidden = false;
    var w = box.clientWidth, tw = t.offsetWidth, th = t.offsetHeight;
    var left = Math.min(Math.max(0, x + 12), Math.max(0, w - tw));
    if (x + 12 + tw > w) left = Math.max(0, x - tw - 12);
    t.style.left = left + "px"; t.style.top = Math.max(0, y - th - 8) + "px";
  }
  function hideTip(box) { var t = $(".tip", box); if (t) t.hidden = true; }

  /* ---------- 縦棒（積み上げ）。series：[{key,label,color,values[]}]。labels：区切りの名前 ---------- */
  function columns(box, labels, series, opts) {
    opts = opts || {};
    box.textContent = "";
    var W = Math.max(280, box.clientWidth || 320), H = opts.height || 220, L = 46, R = 8, T = 10, B = 24;
    var n = labels.length, iw = W - L - R, ih = H - T - B;
    var totals = labels.map(function (_, i) { return series.reduce(function (a, s) { return a + (s.values[i] || 0); }, 0); });
    var max = niceMax(Math.max.apply(null, totals.concat([0])));
    var svg = sv("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": opts.aria || "グラフ" });
    var c = colors();
    for (var g = 0; g <= 4; g++) {
      var gy = T + ih - ih * g / 4;
      svg.append(sv("line", { x1: L, x2: W - R, y1: gy, y2: gy, stroke: g ? c.grid : c.axis, "stroke-width": 1 }));
      var tl = sv("text", { x: L - 6, y: gy + 4, "text-anchor": "end" }); tl.textContent = compact(max * g / 4); svg.append(tl);
    }
    var band = iw / Math.max(1, n), bw = Math.max(2, Math.min(24, band * 0.7));
    var every = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(iw / 58))));
    labels.forEach(function (lab, i) {
      var cx = L + band * i + band / 2, base = T + ih, y = base, top = -1;
      series.forEach(function (s, si) { if ((s.values[i] || 0) > 0) top = si; });
      series.forEach(function (s, si) {
        var v = s.values[i] || 0; if (v <= 0) return;
        var h = ih * v / max, gap = y < base ? 2 : 0, hh = Math.max(0, h - gap), yy = y - gap - hh;
        var r = si === top ? Math.min(4, hh, bw / 2) : 0, x0 = cx - bw / 2, x1 = cx + bw / 2;
        var d = "M" + x0 + "," + (yy + hh) + "V" + (yy + r) + (r ? "Q" + x0 + "," + yy + " " + (x0 + r) + "," + yy : "") +
                "H" + (x1 - r) + (r ? "Q" + x1 + "," + yy + " " + x1 + "," + (yy + r) : "") + "V" + (yy + hh) + "Z";
        svg.append(sv("path", { d: d, fill: s.color, "data-i": i }));
        y = yy;
      });
      if (i % every === 0) {
        var xl = sv("text", { x: cx, y: H - 6, "text-anchor": "middle" }); xl.textContent = lab; svg.append(xl);
      }
      var hit = sv("rect", { x: L + band * i, y: T, width: band, height: ih, fill: "transparent", tabindex: 0,
                             "aria-label": lab + " " + yen(totals[i]) });
      function show(ev) {
        var rect = box.getBoundingClientRect();
        var px = ev && ev.clientX != null ? ev.clientX - rect.left : (cx / W) * rect.width;
        var py = ev && ev.clientY != null ? ev.clientY - rect.top : 30;
        $$("path[data-i]", svg).forEach(function (p) { p.style.opacity = p.getAttribute("data-i") == i ? "1" : "0.45"; });
        var rows = series.length > 1 ? series.slice().reverse().map(function (s) { return { color: s.color, label: s.label, value: (opts.money === false ? num(s.values[i]) + (opts.unit || "") : yen(s.values[i])) }; }) : [];
        rows.push({ label: series.length > 1 ? "合計" : (series[0] && series[0].label) || "", value: opts.money === false ? num(totals[i]) + (opts.unit || "") : yen(totals[i]) });
        if (opts.extra) rows = rows.concat(opts.extra(i));
        showTip(box, px, py, opts.titles ? opts.titles[i] : lab, rows);
      }
      function clear() { $$("path[data-i]", svg).forEach(function (p) { p.style.opacity = ""; }); hideTip(box); }
      hit.addEventListener("pointermove", show); hit.addEventListener("pointerenter", show);
      hit.addEventListener("pointerleave", clear); hit.addEventListener("focus", function () { show(null); }); hit.addEventListener("blur", clear);
      svg.append(hit);
    });
    if (!totals.some(function (v) { return v > 0; })) {
      var e = sv("text", { x: L + iw / 2, y: T + ih / 2, "text-anchor": "middle" }); e.textContent = "この期間の入金はありません"; svg.append(e);
    }
    box.append(svg);
  }

  /* ---------- 折れ線（1本）：十字線が近い区切りに吸い付く ---------- */
  function line(box, labels, values, opts) {
    opts = opts || {};
    box.textContent = "";
    var W = Math.max(280, box.clientWidth || 320), H = 180, L = 46, R = 56, T = 12, B = 24, n = labels.length;
    var iw = W - L - R, ih = H - T - B, c = colors(), color = c.nyanko;
    var lo = Math.min(0, Math.min.apply(null, values.concat([0]))), hi = niceMax(Math.max.apply(null, values.concat([0])));
    var X = function (i) { return L + (n <= 1 ? iw / 2 : iw * i / (n - 1)); };
    var Y = function (v) { return T + ih - ih * (v - lo) / (hi - lo || 1); };
    var svg = sv("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": opts.aria || "累計のグラフ" });
    for (var g = 0; g <= 4; g++) {
      var v = lo + (hi - lo) * g / 4, gy = Y(v);
      svg.append(sv("line", { x1: L, x2: W - R, y1: gy, y2: gy, stroke: g ? c.grid : c.axis, "stroke-width": 1 }));
      var tl = sv("text", { x: L - 6, y: gy + 4, "text-anchor": "end" }); tl.textContent = compact(v); svg.append(tl);
    }
    var every = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(iw / 58))));
    labels.forEach(function (lab, i) { if (i % every) return; var t = sv("text", { x: X(i), y: H - 6, "text-anchor": "middle" }); t.textContent = lab; svg.append(t); });
    if (n) {
      var d = values.map(function (v, i) { return (i ? "L" : "M") + X(i) + "," + Y(v); }).join("");
      svg.append(sv("path", { d: d + "L" + X(n - 1) + "," + Y(lo) + "L" + X(0) + "," + Y(lo) + "Z", fill: color, "fill-opacity": 0.1 }));
      svg.append(sv("path", { d: d, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
      svg.append(sv("circle", { cx: X(n - 1), cy: Y(values[n - 1]), r: 4, fill: color, stroke: c.panel, "stroke-width": 2 }));
      var endl = sv("text", { x: X(n - 1) + 8, y: Y(values[n - 1]) + 4 }); endl.textContent = compact(values[n - 1]); svg.append(endl);
    }
    var cross = sv("line", { y1: T, y2: T + ih, stroke: c.axis, "stroke-width": 1, visibility: "hidden" }), dot = sv("circle", { r: 4, fill: color, stroke: c.panel, "stroke-width": 2, visibility: "hidden" });
    svg.append(cross, dot);
    var hit = sv("rect", { x: L, y: T, width: iw, height: ih, fill: "transparent", tabindex: 0, "aria-label": "累計の値を見る" });
    var cur = n - 1;
    function at(i) {
      if (!n) return;
      cur = Math.max(0, Math.min(n - 1, i));
      cross.setAttribute("x1", X(cur)); cross.setAttribute("x2", X(cur)); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", X(cur)); dot.setAttribute("cy", Y(values[cur])); dot.setAttribute("visibility", "visible");
      var rect = box.getBoundingClientRect();
      showTip(box, X(cur) / W * rect.width, Y(values[cur]) / H * rect.height, opts.titles ? opts.titles[cur] : labels[cur],
              [{ color: color, label: "累計", value: yen(values[cur]) }]);
    }
    hit.addEventListener("pointermove", function (ev) { var r = svg.getBoundingClientRect(); at(Math.round(((ev.clientX - r.left) / r.width * W - L) / (iw / Math.max(1, n - 1)))); });
    hit.addEventListener("pointerleave", function () { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hideTip(box); });
    hit.addEventListener("focus", function () { at(cur); });
    hit.addEventListener("keydown", function (ev) { if (ev.key === "ArrowLeft") { ev.preventDefault(); at(cur - 1); } if (ev.key === "ArrowRight") { ev.preventDefault(); at(cur + 1); } });
    hit.addEventListener("blur", function () { hideTip(box); });
    svg.append(hit);
    box.append(svg);
  }

  function legend(box, series) {
    box.textContent = "";
    series.forEach(function (s) { var it = el("span"); it.append(el("i", { style: "background:" + s.color }), document.createTextNode(s.label)); box.append(it); });
  }
  function table(box, head, rows) {
    box.textContent = "";
    var t = el("table", { class: "tbl" }), th = el("thead"), tr = el("tr");
    head.forEach(function (h) { tr.append(el("th", { scope: "col" }, h)); }); th.append(tr); t.append(th);
    var tb = el("tbody");
    rows.forEach(function (r) { var row = el("tr"); r.forEach(function (v) { row.append(el("td", null, v)); }); tb.append(row); });
    t.append(tb); box.append(t);
  }

  /* ---------- 売上タブ ---------- */
  var sales = { loaded: false, range: "30", busy: false };
  function rangeDates(key) {
    var now = new Date(), today = jstDate(now), y = +today.slice(0, 4), m = +today.slice(5, 7);
    var pad = function (v) { return (v < 10 ? "0" : "") + v; };
    var back = function (days) { return jstDate(new Date(now.getTime() - days * 86400000)); };
    if (key === "today") return [today, today];
    if (key === "month") return [y + "-" + pad(m) + "-01", today];
    if (key === "lastmonth") { var py = m === 1 ? y - 1 : y, pm = m === 1 ? 12 : m - 1, last = new Date(Date.UTC(py, pm, 0)).getUTCDate(); return [py + "-" + pad(pm) + "-01", py + "-" + pad(pm) + "-" + pad(last)]; }
    if (key === "year") return [y + "-01-01", today];
    if (key === "all") return ["all", today];
    return [back(+key - 1), today];
  }
  function delta(cur, prev, upGood) {
    if (!prev) return cur ? { text: "前の期間：0", cls: "" } : { text: "前の期間と同じ", cls: "" };
    var p = Math.round((cur - prev) * 100 / prev);
    if (!p) return { text: "前の期間と同じ", cls: "" };
    var good = (p > 0) === upGood;
    return { text: (p > 0 ? "▲ +" : "▼ ") + p + "%（前の期間 " + compact(prev) + "）", cls: good ? "up" : "down" };
  }
  function kpi(box, label, value, sub, d) {
    var k = el("div", { class: "kpi" });
    k.append(el("small", null, label), el("b", null, value));
    if (d) k.append(el("span", { class: d.cls }, d.text));
    if (sub) k.append(el("span", null, sub));
    box.append(k);
  }
  async function loadSales() {
    if (sales.busy) return;
    sales.busy = true; sales.loaded = true;
    $$("#sec-sales .chart").forEach(function (c) { c.classList.add("loading"); });
    var from = $("#sfrom").value || "", to = $("#sto").value || "";
    if (sales.range === "all") from = "all";
    var q = "from=" + encodeURIComponent(from) + "&to=" + encodeURIComponent(to) + "&unit=" + encodeURIComponent($("#sunit").value) + "&series=" + encodeURIComponent($("#sseries").value);
    $("#scsv").href = "/admin/api/sales.csv?from=" + encodeURIComponent(from) + "&to=" + encodeURIComponent(to);
    try { renderSales(await getJSON("/admin/api/sales?" + q)); }
    catch (e) { $("#srange").textContent = e.message; }
    finally { sales.busy = false; $$("#sec-sales .chart").forEach(function (c) { c.classList.remove("loading"); }); }
  }
  function renderSales(d) {
    sales.data = d;
    var c = colors(), unitJa = { hour: "1時間ごと", day: "1日ごと", week: "1週ごと", month: "1か月ごと" }[d.unit];
    var f = new Intl.DateTimeFormat("ja-JP", { timeZone: "Asia/Tokyo", year: "numeric", month: "numeric", day: "numeric" });
    $("#srange").textContent = f.format(new Date(d.from * 1000)) + " 〜 " + f.format(new Date(d.to * 1000)) + "（" + unitJa + "）";
    if (sales.range === "all" && !$("#sfrom").value) $("#sfrom").value = jstDate(new Date(d.from * 1000));
    var t = d.totals, p = d.prev;
    $("#snet").textContent = yen(t.net);
    var nd = delta(t.net, p.net, true); $("#snetsub").textContent = "純売上（受け取った金額 − 返金）　" + nd.text; $("#snetsub").className = "sub " + nd.cls;
    var k = $("#skpi"); k.textContent = "";
    kpi(k, "受け取った金額", yen(t.gross), null, delta(t.gross, p.gross, true));
    kpi(k, "入金の件数", num(t.count) + "件", null, delta(t.count, p.count, true));
    kpi(k, "1件あたり", yen(t.avg), null, delta(t.avg, p.avg, true));
    kpi(k, "1日あたり（純売上）", yen(t.per_day));
    kpi(k, "返金", yen(t.refund), null, t.refund || p.refund ? delta(t.refund, p.refund, false) : null);
    kpi(k, "割り引いた額", yen(t.discount), "クーポン・セール");
    kpi(k, "お客さん", num(t.customers) + "人", "初めて " + num(t.new_customers) + "・リピート " + num(t.repeat_customers), delta(t.customers, p.customers, true));
    kpi(k, "いちばん売れた区切り", d.best ? yen(d.best.yen) : "-", d.best ? d.best.label : null);
    var pick = $("#sseries").value;
    var keys = pick === "all" ? d.series : [pick];
    var series = keys.map(function (key) { return { key: key, label: d.series_labels[key], color: c[key], values: d.buckets.map(function (b) { return b.yen[key]; }) }; });
    var labels = d.buckets.map(function (b) { return b.label; });
    $("#st1").textContent = pick === "all" ? "売上（部門別）" : "売上（" + d.series_labels[pick] + "）";
    columns($("#cTrend"), labels, series, { aria: "区切りごとの売上", extra: function (i) {
      var b = d.buckets[i], n = Object.keys(b.count).reduce(function (a, x) { return a + b.count[x]; }, 0);
      var rows = [{ label: "件数", value: num(n) + "件" }]; if (b.refund) rows.push({ label: "返金", value: "−" + yen(b.refund) }); return rows; } });
    legend($("#lTrend"), series.length > 1 ? series : []);
    table($("#tTrend"), ["区切り"].concat(series.map(function (s) { return s.label; }), ["合計", "件数", "返金"]),
          d.buckets.map(function (b) { return [b.label].concat(keys.map(function (k2) { return num(b.yen[k2]); }),
            [num(b.total), num(Object.keys(b.count).reduce(function (a, x) { return a + b.count[x]; }, 0)), b.refund ? num(b.refund) : ""]); }));
    line($("#cCum"), labels, d.buckets.map(function (b) { return b.cumulative; }), { aria: "累計の売上" });
    /* 部門の割合：1本の帯（28px）。狭すぎて文字が入らない部分は、文字を出さない（表に数字がある） */
    var share = $("#cShare"); share.textContent = "";
    var whole = d.by_series.reduce(function (a, s) { return a + s.yen; }, 0), bar = el("div", { class: "seg", role: "img", "aria-label": "部門の割合" });
    d.by_series.forEach(function (s) {
      if (!s.yen) return;
      var pct = Math.round(s.yen * 100 / whole), part = el("div", { style: "flex:" + s.yen + " 1 0;background:" + c[s.key] + ";color:" + (DARK_TEXT[s.key] ? "#0b0b0b" : "#fff"),
                                                              title: s.label + " " + yen(s.yen) + "（" + pct + "%）", tabindex: 0 });
      part.textContent = pct >= 9 ? pct + "%" : "";
      bar.append(part);
    });
    if (whole) share.append(bar); else share.append(el("div", { class: "empty" }, "この期間の入金はありません"));
    legend($("#lShare"), d.by_series.filter(function (s) { return s.yen; }).map(function (s) { return { label: s.label, color: c[s.key] }; }));
    table($("#tShare"), ["部門", "金額", "件数", "割合"], d.by_shop.filter(function (s) { return s.count; }).map(function (s) {
      return [s.label, num(s.yen) + "円", num(s.count) + "件", whole ? Math.round(s.yen * 100 / whole) + "%" : "-"]; }));
    /* よく売れたメニュー：横の棒（色＝部門）。値は棒の右に */
    var mb = $("#cMenus"); mb.textContent = "";
    var top = d.menus.length ? d.menus[0].yen : 0;
    if (!d.menus.length) mb.append(el("div", { class: "empty" }, "この期間の入金はありません"));
    d.menus.forEach(function (m) {
      var row = el("div", { class: "row1" }), lab = el("span", { class: "lab" });
      lab.append(el("i", { style: "display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:" + c[m.series] }), document.createTextNode(m.label));
      row.append(lab, el("span", { class: "val" }, yen(m.yen) + "・" + num(m.orders) + "件"));
      var tr = el("div", { class: "track" }); tr.append(el("div", { class: "fill", style: "width:" + Math.max(1, m.yen * 100 / (top || 1)) + "%;background:" + c[m.series] })); row.append(tr);
      mb.append(row);
    });
    if (d.menus.length) legend(mb.appendChild(el("div", { class: "legend" })), keys.filter(function (k3) { return d.menus.some(function (m) { return m.series === k3; }); }).map(function (k3) { return { label: d.series_labels[k3], color: c[k3] }; }));
    var one = function (vals) { return [{ key: "all", label: "売上", color: c.nyanko, values: vals }]; };
    columns($("#cHours"), d.hours.map(function (h) { return h.h + "時"; }), one(d.hours.map(function (h) { return h.yen; })), { height: 170, aria: "時間帯ごとの売上",
      extra: function (i) { return [{ label: "件数", value: num(d.hours[i].count) + "件" }]; } });
    columns($("#cWeek"), d.weekdays.map(function (w) { return w.label; }), one(d.weekdays.map(function (w) { return w.yen; })), { height: 170, aria: "曜日ごとの売上",
      titles: d.weekdays.map(function (w) { return w.label + "曜日"; }), extra: function (i) { return [{ label: "件数", value: num(d.weekdays[i].count) + "件" }]; } });
    var a = $("#sall"); a.textContent = "";
    kpi(a, "これまでの入金", yen(d.all_time.gross), d.all_time.since ? f.format(new Date(d.all_time.since * 1000)) + " から" : null);
    kpi(a, "これまでの件数", num(d.all_time.count) + "件");
    kpi(a, "これまでのお客さん", num(d.all_time.customers) + "人");
  }
  function initSales() {
    $$("#sf [data-range]").forEach(function (b) {
      b.addEventListener("click", function () {
        $$("#sf [data-range]").forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
        sales.range = b.dataset.range;
        var r = rangeDates(sales.range); $("#sfrom").value = r[0] === "all" ? "" : r[0]; $("#sto").value = r[1];
        loadSales();
      });
    });
    ["#sfrom", "#sto"].forEach(function (s) { $(s).addEventListener("change", function () { sales.range = "custom"; $$("#sf [data-range]").forEach(function (x) { x.setAttribute("aria-pressed", "false"); }); loadSales(); }); });
    ["#sunit", "#sseries"].forEach(function (s) { $(s).addEventListener("change", loadSales); });
    var r = rangeDates("30"); $("#sfrom").value = r[0]; $("#sto").value = r[1];
    var redraw = null;
    window.addEventListener("resize", function () { clearTimeout(redraw); redraw = setTimeout(function () { if (sales.data && $("#sec-sales").classList.contains("on")) renderSales(sales.data); }, 200); });
    if (window.matchMedia) window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () { if (sales.data) renderSales(sales.data); });
  }

  /* ---------- 注文の詳細 ---------- */
  function kv(pairs) {
    var dl = el("dl", { class: "kv" });
    pairs.forEach(function (p) { if (p[1] === "" || p[1] == null) return; dl.append(el("dt", null, p[0]), el("dd", null, String(p[1]))); });
    return dl;
  }
  function card(title, sub) { var c = el("div", { class: "card" }); c.append(el("h2", null, title)); if (sub) c.append(el("p", { class: "sub" }, sub)); return c; }
  function postForm(csrf, action, fields, label, cls, confirmText) {
    var f = el("form", { method: "post" });
    f.append(el("input", { type: "hidden", name: "csrf", value: csrf }), el("input", { type: "hidden", name: "action", value: action }));
    Object.keys(fields || {}).forEach(function (k) { f.append(el("input", { type: "hidden", name: k, value: fields[k] })); });
    var b = el("button", { class: "btn " + (cls || "") }, label); if (confirmText) b.setAttribute("data-confirm", confirmText);
    f.append(b); return f;
  }
  async function openOrder(id) {
    var dlg = $("#odlg"), body = $("#odbody");
    body.textContent = ""; body.append(el("div", { class: "empty" }, "読み込んでいます…"));
    dlg.hidden = false; document.body.style.overflow = "hidden";
    var d;
    try { d = await getJSON("/admin/api/order/" + encodeURIComponent(id)); }
    catch (e) { body.textContent = ""; body.append(el("div", { class: "empty" }, e.message)); return; }
    body.textContent = "";
    var lv = d.live, h = d.history || {};
    var top = card(((lv && lv.dept) || h.shop || "") + "　" + ((lv && lv.state_ja) || h.state_ja || ""));
    top.append(kv([["注文番号", d.order], ["作成", (lv && lv.created) || h.when], ["金額", yen((lv ? lv.amount : h.amount) || 0) + (lv && lv.base != null && lv.base !== lv.amount ? "（割引前 " + yen(lv.base) + "）" : "")],
                   ["内容", (lv && lv.summary) || h.summary], ["USERID", (lv && lv.uid) || h.user], ["IP", lv && lv.ip],
                   ["最後の結果", (lv && lv.diag) || h.diag], ["クーポン", lv && lv.coupon ? lv.coupon + (lv.coupon_used ? "（使用済み）" : "") : ""],
                   ["発行したクーポン", lv && lv.coupon_issued], ["途中経過", lv && lv.progress],
                   ["メモ", !lv ? "期限が過ぎて、注文の中身はもう残っていません（台帳の記録だけ）" : ""]]));
    if (lv && lv.breakdown && lv.breakdown.length) {
      top.append(el("h3", null, "内訳"));
      table(top.appendChild(el("div", { class: "tblwrap" })), ["項目", "数", "単価"], lv.breakdown.map(function (b) { return [b.label, num(b.count), num(b.unit) + "円"]; }));
    }
    body.append(top);
    var pay = card("お支払い");
    if (d.pay) pay.append(kv([["状態", d.pay.paid ? "入金済み" : d.pay.state], ["時刻", d.pay.when], ["受け取った金額", d.pay.amount != null ? yen(d.pay.amount) : ""],
                             ["送金者", d.pay.sender], ["送金者ID", d.pay.sender_id], ["PayPay の返事", d.pay.status]]));
    else pay.append(el("p", { class: "sub" }, lv && lv.admin_free ? "管理者の無料実行です。" : lv && lv.free_coupon ? "無料クーポン・チケットで0円の注文です。" : "送金リンクの記録はありません。"));
    body.append(pay);
    if (d.job) {
      var jc = card("実行", d.job.queue ? "順番待ち " + d.job.queue + "番目" : "状態：" + d.job.status + (d.job.error ? "・" + d.job.error : ""));
      if (d.job.lines && d.job.lines.length) jc.append(el("div", { class: "logbox" }, d.job.lines.join("\n")));
      jc.append(el("div", { class: "logbox" }, (d.job.log || []).join("\n") || "（ログはまだありません）"));
      var ops = el("div", { class: "row" });
      if (d.job.queue) {
        ops.append(postForm(d.csrf, "job_front", { order: d.order }, "先頭にする"),
                   postForm(d.csrf, "job_cancel", { order: d.order }, "取り消す", "danger", "この順番待ちを取り消しますか？\nお支払いは注文に残ります。"));
      } else if (d.job.status === "running" && d.job.mode === "puni") {
        ops.append(postForm(d.csrf, "job_stop", { order: d.order }, "止める", "danger", "この周回を止めますか？\nいまの1周が終わると止まり、そこまでの結果で完了になります。"));
      }
      var again = el("button", { class: "btn", type: "button" }, "↻ ログを更新"); again.addEventListener("click", function () { openOrder(id); }); ops.append(again);
      jc.append(ops); body.append(jc);
    }
    if (lv && lv.outcome && lv.outcome.length) { var oc = card("結果の控え"); oc.append(el("div", { class: "logbox" }, lv.outcome.join("\n"))); body.append(oc); }
    var tlc = card("時系列", "状態とメモが変わった時刻（この版から記録します）");
    var ul = el("ul", { class: "tl" });
    d.events.forEach(function (e) { var li = el("li"); li.append(el("b", null, e.state_ja + "　"), el("span", { class: "sub" }, e.when)); if (e.text) li.append(el("div", { class: "sub" }, e.text)); ul.append(li); });
    if (!d.events.length) ul.append(el("li", null, "記録はまだありません"));
    tlc.append(ul); body.append(tlc);
    if (d.review) { var rc = card("実績"); rc.append(el("div", null, "★".repeat(d.review.stars) + "☆".repeat(5 - d.review.stars) + "　" + d.review.when + (d.review.hidden ? "（非表示）" : "")), el("p", { class: "sub", style: "white-space:pre-wrap" }, d.review.text)); body.append(rc); }
    var nc = card("メモ・返金", "自分用のメモです（お客さんには見えません）。返金した額を入れると、売上から差し引きます。PayPay での返金そのものは、アプリから送ってください。");
    var nf = el("form", { method: "post" });
    nf.append(el("input", { type: "hidden", name: "csrf", value: d.csrf }), el("input", { type: "hidden", name: "action", value: "order_note" }), el("input", { type: "hidden", name: "order", value: d.order }));
    var ta = el("textarea", { name: "note", maxlength: "500", "aria-label": "メモ" }); ta.value = d.note.note || "";
    nf.append(el("label", null, "メモ"), ta, el("label", null, "返金した額（円）" + (d.note.refund_at ? "・記録 " + d.note.refund_at : "")));
    var rf = el("input", { name: "refund", inputmode: "numeric", maxlength: "7" }); rf.value = d.note.refund || ""; nf.append(rf);
    var rn = el("input", { name: "refund_note", maxlength: "100", placeholder: "例：一部失敗のため半額" }); rn.value = d.note.refund_note || "";
    nf.append(el("label", null, "返金の理由（任意）"), rn, el("button", { class: "btn pri full" }, "保存"));
    nc.append(nf); body.append(nc);
    if ((lv && lv.uid) || h.user) {
      var bc = card("このお客さんの受付を止める", "新しい注文と意見箱の投稿を止めます。結果の受け取りと実績の入力は止めません。");
      var bf = el("form", { method: "post" });
      bf.append(el("input", { type: "hidden", name: "csrf", value: d.csrf }), el("input", { type: "hidden", name: "action", value: "block_order" }), el("input", { type: "hidden", name: "order", value: d.order }));
      bf.append(el("label", null, "期間（日・空欄なら無期限）"), el("input", { name: "days", inputmode: "numeric", maxlength: "3" }));
      if (lv && lv.ip) { var lb = el("label", { style: "font-weight:400" }); var cb = el("input", { type: "checkbox", name: "with_ip", value: "1" }); lb.append(cb, document.createTextNode(" IP（" + lv.ip + "）も止める（同じ回線のほかの人も止まります）")); bf.append(lb); }
      var bb = el("button", { class: "btn danger full", "data-confirm": "このお客さんからの新しい注文を止めますか？" }, "受付を止める"); bf.append(bb);
      bc.append(bf); body.append(bc);
    }
    if (d.actions.length) {
      var ac = card("この注文への管理操作");
      d.actions.forEach(function (a) { var r = el("div", { class: "hl" }), tx = el("div", { class: "tx" }); tx.append(el("b", null, a.action + "　"), el("span", { class: "sub" }, a.when)); if (a.msg) tx.append(el("div", { class: "sub" }, a.msg)); r.append(tx); ac.append(r); });
      body.append(ac);
    }
  }
  function closeOrder() { $("#odlg").hidden = true; document.body.style.overflow = ""; if (/^#o:/.test(location.hash)) history.replaceState(null, "", "#history"); }

  /* ---------- 注文を探す ---------- */
  async function search(ev) {
    if (ev) ev.preventDefault();
    var box = $("#oresult"); box.textContent = ""; box.append(el("div", { class: "empty" }, "探しています…"));
    var q = ["q=" + encodeURIComponent($("#oq").value), "state=" + encodeURIComponent($("#ostate").value), "shop=" + encodeURIComponent($("#oshop").value),
             "from=" + encodeURIComponent($("#ofrom").value), "to=" + encodeURIComponent($("#oto").value)].join("&");
    var d;
    try { d = await getJSON("/admin/api/orders?" + q); } catch (e) { box.textContent = ""; box.append(el("div", { class: "empty" }, e.message)); return; }
    box.textContent = "";
    if (!d.orders.length) { box.append(el("div", { class: "empty" }, "見つかりませんでした")); return; }
    box.append(el("p", { class: "sub" }, d.orders.length + "件" + (d.orders.length >= 100 ? "（多いので新しい100件だけ）" : "")));
    d.orders.forEach(function (o) {
      var row = el("button", { class: "h", type: "button", style: "display:block;width:100%;text-align:left;background:none;border:0;border-top:1px solid var(--soft);color:inherit;font:inherit;cursor:pointer;padding:10px 0" });
      var l1 = el("div", { class: "l1" });
      l1.append(el("span", { class: "chip" + (o.state === "done" ? " ok" : o.state === "dead" ? " bad" : "") }, o.state_ja));
      if (o.paid) l1.append(el("span", { class: "chip ok" }, "入金済"));
      if (o.refund) l1.append(el("span", { class: "chip warn" }, "返金 " + num(o.refund) + "円"));
      l1.append(el("span", { class: "chip" }, o.when), el("span", { class: "chip" }, o.shop), el("span", { class: "amt" }, num(o.amount) + "円"));
      row.append(l1, el("div", { class: "l2" }, o.summary));
      row.append(el("div", { class: "l2" }, "USERID " + o.user + (o.sender ? "　送金者 " + o.sender : "") + "　" + o.order.slice(0, 10) + "…" + (o.note ? "　📝 " + o.note.slice(0, 40) : "")));
      row.addEventListener("click", function () { openOrder(o.order); });
      box.append(row);
    });
  }

  /* ---------- サーバーの点検 ---------- */
  var healthLoaded = false;
  async function loadHealth() {
    healthLoaded = true;
    var box = $("#health"); box.textContent = ""; box.append(el("div", { class: "empty" }, "確認しています…"));
    var d;
    try { d = await getJSON("/admin/api/health"); } catch (e) { box.textContent = ""; box.append(el("div", { class: "empty" }, e.message)); return; }
    box.textContent = "";
    var icon = { ok: "✅", warn: "⚠️", bad: "⛔" }, word = { ok: "正常", warn: "注意", bad: "要対応" };
    d.items.forEach(function (it) {
      var r = el("div", { class: "hl" }), tx = el("div", { class: "tx" });
      tx.append(el("b", null, it.name + "　"), el("span", { class: "chip " + it.level }, icon[it.level] + " " + word[it.level]), el("div", { class: "l2" }, it.text));
      r.append(tx); box.append(r);
    });
    var s = d.slots, r2 = el("div", { class: "hl" }), t2 = el("div", { class: "tx" });
    t2.append(el("b", null, "実行枠　"), el("div", { class: "l2" }, "ふつう " + s.running + "/" + s.normal + " 実行中・待ち " + s.queued + "　／　長時間 " + s.long_running + "/" + s.long + " 実行中・待ち " + s.long_queued));
    r2.append(t2); box.append(r2);
    box.append(el("p", { class: "sub" }, "バージョン " + d.version + "・稼働 " + Math.floor(d.uptime / 3600) + "時間" + Math.floor(d.uptime % 3600 / 60) + "分"));
  }

  /* ---------- 管理操作の記録 ---------- */
  async function loadLog() {
    var box = $("#alog"); if (!box) return;
    var d;
    try { d = await getJSON("/admin/api/log"); } catch (e) { box.textContent = ""; box.append(el("div", { class: "empty" }, e.message)); return; }
    box.textContent = "";
    if (!d.items.length) box.append(el("div", { class: "empty" }, "まだありません"));
    d.items.forEach(function (a) {
      var r = el("div", { class: "hl" }), tx = el("div", { class: "tx" }), l1 = el("div", { class: "l1" });
      l1.append(el("span", { class: "chip" + (a.failed ? " bad" : "") }, a.label), el("span", { class: "chip" }, a.when));
      if (a.target) l1.append(el("code", null, a.target.slice(0, 20)));
      tx.append(l1);
      if (a.msg) tx.append(el("div", { class: "l2" }, a.msg));
      tx.append(el("div", { class: "l2", style: "font-size:11.5px" }, "IP " + a.ip));
      r.append(tx); box.append(r);
    });
  }

  /* ---------- まとめ ---------- */
  function onTab() {
    setTimeout(function () {
      if ($("#sec-sales") && $("#sec-sales").classList.contains("on") && !sales.loaded) loadSales();
      if ($("#sec-sales") && $("#sec-sales").classList.contains("on") && sales.data) renderSales(sales.data);
      if ($("#sec-ops") && $("#sec-ops").classList.contains("on") && !healthLoaded) { loadHealth(); loadLog(); }
    }, 0);
  }
  function filterList(input, items) {
    if (!input) return;
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      items().forEach(function (r) { r.style.display = (!q || (r.dataset.q || "").toLowerCase().indexOf(q) >= 0) ? "" : "none"; });
    });
  }
  document.addEventListener("DOMContentLoaded", function () {
    if (!$("#sec-sales")) return;   /* ログイン前の画面 */
    initSales();
    $$(".tab, [data-go]").forEach(function (b) { b.addEventListener("click", onTab); });
    document.addEventListener("click", function (e) {
      var t = e.target.closest && e.target.closest("[data-order]");
      if (t) { e.preventDefault(); openOrder(t.getAttribute("data-order")); }
    });
    $("#odclose").addEventListener("click", closeOrder);
    $("#odlg").addEventListener("click", function (e) { if (e.target === $("#odlg")) closeOrder(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !$("#odlg").hidden) closeOrder(); });
    $("#osearch").addEventListener("submit", search);
    $("#healthReload").addEventListener("click", loadHealth);
    filterList($("#pmq"), function () { return $$("#pml label"); });
    filterList($("#cq"), function () { return $$("#clist > .h"); });
    var m = /^#o:([A-Za-z0-9_-]{4,40})$/.exec(location.hash);
    if (m) { var tab = $('.tab[data-sec="history"]'); if (tab) tab.click(); openOrder(m[1]); }
    onTab();
  });
})();
