/* のぞき見よけ（気軽に中をのぞかれるのを減らすだけのもの。本当の守りはサーバー側にある）。
   右クリックのメニュー・ページの保存（Ctrl+S）・ソース表示（Ctrl+U）・開発者ツールのキーを止める。
   入力欄では止めない（貼り付け・コピーができなくなると困るため）。 */
(function () {
  "use strict";
  function editable(t) { return !!(t && t.closest && t.closest("input,textarea,[contenteditable],code,.code,pre")); }
  document.addEventListener("contextmenu", function (e) { if (!editable(e.target)) e.preventDefault(); });
  document.addEventListener("keydown", function (e) {
    var k = (e.key || "").toLowerCase(), mod = e.ctrlKey || e.metaKey;
    if (k === "f12" || (mod && (k === "s" || k === "u")) || (mod && e.shiftKey && (k === "i" || k === "j" || k === "c")) ||
        (e.metaKey && e.altKey && (k === "i" || k === "j" || k === "c" || k === "u"))) { e.preventDefault(); e.stopPropagation(); }
  }, true);
  document.addEventListener("dragstart", function (e) { if (e.target && e.target.tagName === "IMG") e.preventDefault(); });
})();
