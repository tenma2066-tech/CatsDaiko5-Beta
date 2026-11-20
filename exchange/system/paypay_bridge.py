#!/usr/bin/env python3
"""
paypay_bridge.py -- 同梱の paypay_api.py を使う薄いラッパ

- BOT側からは receive_link() だけ呼べば「受取リンクを受け取って着金させる」まで完了する。
  一時保留 (42007013) は held、結果が確定しない時は unknown として返すので、
  呼び出し側は find_incoming() で入金履歴の裏を取ってから換金する。

"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import paypay_api

_lock = threading.Lock()
_last_refresh = 0.0


class PayPayError(Exception):
    pass


def account_info() -> dict | None:
    """アクティブアカウントの {access_token, refresh_token, phone, device_uuid}"""
    return paypay_api.load_tokens()


def _tokens() -> tuple[str, str]:
    t = account_info()
    if not t or not t.get("access_token"):
        raise PayPayError("PayPay セッションがありません。管理者に連絡してください。")
    return t["access_token"], t.get("device_uuid") or ""


REFRESH_COOLDOWN_SEC = 300     # 共有トークンなので頻繁に叩かない


def refresh(force: bool = False) -> bool:
    """accessToken を更新して paypay_accounts.json に書き戻す。

    force はセッション切れが確定した時だけに使う。ユーザー入力を起点に
    force で叩けるようにしておくと、無効リンクの連投でリフレッシュを
    連打され、アカウントごと失効させられる。
    """
    global _last_refresh
    with _lock:
        if not force and time.time() - _last_refresh < REFRESH_COOLDOWN_SEC:
            return True
        t = account_info()
        if not t or not t.get("refresh_token"):
            return False
        at, rt = paypay_api.refresh_access_token(t["refresh_token"], t.get("device_uuid"))
        if not at:
            return False
        paypay_api.save_tokens(at, rt, t.get("phone", ""), t.get("device_uuid", ""))
        _last_refresh = time.time()
        return True


def _with_retry(fn, *a, allow_refresh: bool = True, **kw):
    """1度目が None なら refresh して1回だけ再試行。

    ただし force=True を無条件に使うと、存在しないリンクを投げられるたびに
    リフレッシュが走る。連打されるとレート制限でアカウントごと失効し、
    決済が全部止まる。クールダウン付きの通常 refresh に留める。
    """
    at, du = _tokens()
    res = fn(at, du, *a, **kw)
    if res is None and allow_refresh and refresh():
        at, du = _tokens()
        res = fn(at, du, *a, **kw)
    return res


def alive() -> bool:
    at, du = _tokens()
    if paypay_api.alive(at, du, verbose=False):
        return True
    if refresh(force=True):
        at, du = _tokens()
        return paypay_api.alive(at, du, verbose=False)
    return False


def get_balance() -> dict | None:
    return _with_retry(lambda at, du: paypay_api.get_balance_info(at, du))


def get_profile() -> dict | None:
    return _with_retry(lambda at, du: paypay_api.get_balance(at, du))


def extract_code(value: str) -> str:
    return paypay_api._extract_verification_code(value)


def link_info(code_or_url: str) -> dict | None:
    """受け取らずにリンクの中身だけ見る (subWalletSplit もここで返る)"""
    code = extract_code(code_or_url)
    if not code:
        return None
    return _with_retry(lambda at, du: paypay_api.get_p2p_link_info(at, du, code))


# order_status の分類 (未知のステータスは受取を試す)
_FINAL_SUCCESS = {"COMPLETED", "COMPLETE", "RECEIVED", "SUCCESS", "SUCCEEDED", "DONE"}
_FINAL_FAIL = {"REJECTED", "EXPIRED", "CANCELLED", "CANCELED", "FAILED",
               "DECLINED", "TIMEOUT", "TIMED_OUT"}

_STATUS_JP = {
    "COMPLETED": "既に受取済み", "COMPLETE": "既に受取済み", "RECEIVED": "既に受取済み",
    "CANCELED": "送信者がキャンセル済み", "CANCELLED": "送信者がキャンセル済み",
    "EXPIRED": "期限切れ", "REJECTED": "受取拒否済み", "DECLINED": "受取拒否済み",
}


def classify_status(status) -> str:
    """'success'(受取済) / 'fail'(確定失敗) / 'try'(PENDING等・受取を試す)"""
    s = str(status or "").upper()
    if s in _FINAL_SUCCESS:
        return "success"
    if s in _FINAL_FAIL:
        return "fail"
    return "try"


def status_text(status) -> str:
    s = str(status or "?").upper()
    return _STATUS_JP.get(s, s)


def inspect_link(code_or_url: str) -> dict:
    """受け取らずに検査。
    戻り: {"ok":bool, "reason":str, "error":str, "info":dict|None}
    ok=True は「今から受け取れる状態」を意味する。
    """
    code = extract_code(code_or_url)
    if not code:
        return {"ok": False, "reason": "BAD_LINK",
                "error": "リンク/コードを読み取れない", "info": None}
    info = link_info(code)
    if not info:
        return {"ok": False, "reason": "BAD_LINK",
                "error": "リンク情報を取得できない (無効・期限切れ・別アカウント宛)",
                "info": None}
    info["code"] = code
    kind = classify_status(info.get("order_status"))
    if kind != "try":
        return {"ok": False, "reason": "NOT_PENDING",
                "error": f"受取不可: {status_text(info.get('order_status'))}",
                "info": info}
    return {"ok": True, "reason": "", "error": "", "info": info}


# ============================================================
# 受け取り (保留対策つき)
#
# ■ 保留とは
#   受取API (acceptP2PSendMoneyLink) の応答に 42007013 が入っていたら、PayPayの
#   詐欺防止機能で受け取りが「一時保留」になっている。送信者がPayPayアプリの
#   確認画面で承認すると、こちらが何もしなくても**自動で着金**し、リンクは
#   完了状態になる。
#
# ■ リンクの状態だけで「着金した」と判断してはいけない
#   完了状態のリンクは「保留が承認されてBOTに着金した」のか「他人が受け取った」
#   のか区別できない。保留中のリンクを別のアカウントで受け取るだけで完了になる
#   ので、状態だけを見て換金すると払い出し分をタダで持っていかれる。
#   必ず**BOT自身の取引履歴に、同じ orderId の入金があるか**で裏を取る。
#   履歴を読めなかった時は「着金した」とも「取られた」とも決めない。
# ============================================================

HOLD_CODE = "42007013"     # 保留を表す resultCode (固定)

# 取引履歴で「その入金は成立していない」ことを示す状態
_HISTORY_VOID = _FINAL_FAIL | {"REFUNDED"}
# 取引履歴で「まだ確定していない」ことを示す状態 (確定するまで換金しない)
_HISTORY_PENDING = {"PENDING", "PROCESSING", "IN_PROGRESS", "ON_HOLD", "HOLD", "WAITING"}

# 受取が失敗した時の resultCode → 日本語。実測で判明したものを足していく。
_ACCEPT_HINTS = {
    "S0001": "リンクの状態が変わっている (既に受取済み/キャンセル済み)",
    "S1001": "パスコードが違う可能性",
    "S9999": "PayPay側で一時的に弾かれた (リスク検知・レート制限の可能性)",
}


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def result_code(data) -> str:
    return str(((data or {}).get("header") or {}).get("resultCode") or "")


def is_hold_response(data) -> bool:
    """受取APIの応答が「一時保留」か。

    resultCode に加えて応答全体も見る (保留コードが error 側に入って返る事がある)。
    """
    if result_code(data) == HOLD_CODE:
        return True
    try:
        blob = json.dumps(data or {}, ensure_ascii=True)
    except (TypeError, ValueError):
        blob = str(data)
    return HOLD_CODE in blob


def accept_once(code: str, passcode: str | None, info: dict, at: str, du: str) -> dict:
    """acceptP2PSendMoneyLink を1回だけ投げて、結果を5つに分ける。

      ok        着金した
      held      一時保留 (送信者が承認すると自動で着金する)
      auth      トークン切れ (401/403)
      unknown   通った可能性を否定できない (通信断・5xx・JSONでない応答)
      rejected  resultCode 付きで受け取れなかった

    共有の paypay_api 版は resultCode を print して捨てるので、保留なのか
    パスコード違いなのか二重受取なのかが全部同じ扱いになる。だから自前で投げる。
    """
    body = {
        "requestId": info.get("request_id", ""),
        "orderId": info.get("order_id", ""),
        "verificationCode": code,
        "senderMessageId": info.get("message_id", ""),
        "senderChannelUrl": info.get("chat_room_id", ""),
    }
    if passcode:
        body["passcode"] = passcode
    out = {"kind": "unknown", "http": 0, "result_code": "", "result_message": "",
           "payload": {}}

    session = None
    try:
        session, hdrs = paypay_api._api_session(at, du)
        r = session.post(
            f"{paypay_api.BASE_APP4}/bff/v2/acceptP2PSendMoneyLink",
            params={"payPayLang": "ja"}, json=body,
            headers={**hdrs, "Content-Type": "application/json"},
        )
    except Exception as e:
        # 送信後に切れた可能性があるので「失敗」ではなく「不明」
        out["result_message"] = f"通信失敗: {type(e).__name__}: {e}"
        return out
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    out["http"] = r.status_code
    try:
        data = r.json()
    except Exception:
        data = None
    if not isinstance(data, dict):
        if r.status_code in (401, 403):
            out["kind"] = "auth"
        out["result_message"] = f"HTTP {r.status_code}: 応答がJSONでない"
        return out

    header = data.get("header") or {}
    rc = str(header.get("resultCode") or "")
    out["result_code"] = rc
    out["result_message"] = str(header.get("resultMessage") or "")
    if rc == "S0000":
        out["kind"] = "ok"
        out["payload"] = data.get("payload") or {}
    elif is_hold_response(data):
        # 401/403 より先に見る。保留がエラー系のHTTPステータスで返ってきても
        # 「再ログインが必要」と取り違えないように。
        out["kind"] = "held"
    elif r.status_code in (401, 403):
        out["kind"] = "auth"
    elif r.status_code >= 500 or not rc:
        out["kind"] = "unknown"
    else:
        out["kind"] = "rejected"
    return out


def _token_after_auth_error(used_at: str) -> tuple[str, str] | None:
    """401/403 の後に使うトークン → (at, du)。更新できなければ None。

    keep_session や同じアカウントを使う別BOTが既に更新していればそれを使い、
    自分で更新するのはそうでない時だけ (refresh_token が入れ替わるので無駄に回さない)。
    ここは「セッション切れが確定した」経路なので force で更新する。冷却中の
    refresh() は何もせず True を返すため、同じ切れたトークンで投げ直すことになる。
    """
    t = account_info() or {}
    at = t.get("access_token")
    if at and at != used_at:
        return at, t.get("device_uuid") or ""
    if not refresh(force=True):
        return None
    t = account_info() or {}
    if not t.get("access_token"):
        return None
    return t["access_token"], t.get("device_uuid") or ""


def _history_page(at: str, du: str, page_key, page_size: int) -> tuple[int, dict | None]:
    """getPaymentHistory を1ページ取る → (HTTPステータス, payload|None)。

    共有の paypay_api 版は応答の全文 (入金者の名前などを含む) をコンソールへ出し、
    HTTPセッションも閉じない。保留の監視で繰り返し呼ぶので自前で叩く。
    """
    params = {"pageSize": str(page_size), "orderTypes": "", "paymentMethodTypes": "",
              "isOverdraftOnly": "false", "payPayLang": "ja"}
    if page_key:
        params["nextPageKey"] = page_key
    session = None
    try:
        session, hdrs = paypay_api._api_session(at, du)
        r = session.get(f"{paypay_api.BASE_APP4}/bff/v3/getPaymentHistory",
                        params=params, headers=hdrs)
    except Exception as e:
        print(f"[paypay] 取引履歴の取得に失敗: {type(e).__name__}")
        return 0, None
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
    try:
        data = r.json()
    except Exception:
        return r.status_code, None
    if isinstance(data, dict) and result_code(data) == "S0000":
        return r.status_code, data.get("payload") or {}
    return r.status_code, None


def _item_ids(item: dict) -> set[str]:
    return {str(item.get(k) or "").strip()
            for k in ("orderId", "subOrderId", "transactionId")} - {""}


def find_incoming(order_id: str, pages: int = 5,
                  page_size: int = 30) -> tuple[str, dict | None]:
    """BOT自身の取引履歴に、この orderId の「入金」があるか。

    戻り値 (state, item):
      "found"    入金として載っている (item はその履歴)
      "pending"  載っているが、まだ確定していない
      "absent"   pages ページ分さかのぼっても無い
      "error"    履歴を読めなかった (★「無い」と混ぜない)
      "no_id"    照合に使う orderId が無い

    照合は orderId / subOrderId / transactionId の完全一致だけ。金額では照合しない
    (同じ額の他人の入金と取り違える)。同じ番号でも出金は入金として数えない。
    """
    order_id = str(order_id or "").strip()
    if not order_id:
        return "no_id", None
    try:
        at, du = _tokens()
    except PayPayError:
        return "error", None

    page_key, retried, pending = None, False, None
    for _ in range(max(1, int(pages))):
        http, payload = _history_page(at, du, page_key, page_size)
        if payload is None and http in (401, 403) and not retried:
            retried = True
            tok = _token_after_auth_error(at)
            if tok:
                at, du = tok
                http, payload = _history_page(at, du, page_key, page_size)
        if payload is None:
            return "error", None
        for item in payload.get("paymentInfoList") or []:
            if order_id not in _item_ids(item):
                continue
            if not paypay_api._is_incoming(item.get("orderType")):
                continue
            st = str(item.get("orderStatus") or "").upper()
            if st in _HISTORY_VOID:
                continue
            if st in _HISTORY_PENDING:
                pending = item
                continue
            return "found", item
        page_key = payload.get("nextPageKey")
        if not payload.get("hasNextPage") or not page_key:
            break
    if pending is not None:
        return "pending", pending
    return "absent", None


def history_amount(item: dict | None) -> int:
    """取引履歴1件の金額 (取れなければ 0)"""
    for k in ("totalAmount", "amount", "receivedAmount"):
        v = (item or {}).get(k)
        if isinstance(v, dict):
            v = v.get("amount", v.get("value"))
        n = abs(_int(v))
        if n > 0:
            return n
    return 0


def settled_amount(*observed) -> int:
    """着金額として使う額。観測できた額 (リンク表示・受取応答・履歴) のうち最小。

    どれかが食い違った時に、多い方で計算して払いすぎることが無いようにする。
    """
    vals = [_int(v) for v in observed if _int(v) > 0]
    return min(vals) if vals else 0


def _res(status: str, **kw) -> dict:
    d = {"status": status, "ok": status in ("received", "already_received"),
         "reason": "", "error": "", "amount": 0, "order_id": "", "sender": "",
         "code": "", "result_code": "", "result_message": "", "http": 0,
         "link_info": None}
    d.update(kw)
    return d


def _link_base(code: str, info: dict) -> dict:
    return {"code": code, "order_id": str(info.get("order_id") or ""),
            "sender": info.get("sender_name", "?"), "link_info": info}


def confirm_completed(code: str, info: dict, pages: int = 5) -> dict:
    """完了済みのリンクを、BOT自身の入金履歴で裏を取る。

    載っていれば already_received。それ以外は unknown (反映待ちの可能性があるので、
    「他人が受け取った」と決めるのは呼び出し側が時間を置いてから行う)。
    """
    base = _link_base(code, info)
    state, item = find_incoming(base["order_id"], pages)
    if state == "found":
        return _res("already_received",
                    amount=settled_amount(info.get("amount"), history_amount(item)),
                    **base)
    why = {
        "pending": "入金履歴には載っているが、まだ確定していない",
        "absent": ("リンクは受取済みになっているが、このBOTの入金履歴に無い "
                   "(反映待ち、または他の人が受け取った)"),
        "error": "入金履歴を確認できなかった",
        "no_id": "リンクに取引番号が無く、入金履歴と照合できない",
    }[state]
    return _res("unknown", reason=f"VERIFY_{state.upper()}", error=why, **base)


def receive_link(code_or_url: str, passcode: str | None = None,
                 info: dict | None = None, *,
                 history_pages: int = 5) -> dict:
    """受取リンクを受け取る (保留対策つき)。

    info を渡さなければ直前にリンクを見直してから投げる (古い下見結果で投げない)。

    戻り値の status:
      received          今受け取った
      already_received  リンクは完了済みで、BOTの入金履歴に載っている
      held              一時保留。送信者がアプリで承認すると自動で着金する
      unknown           受け取れたか確定できない → 監視して決着させる
      failed            受け取れていない。reason は BAD_LINK / NOT_PENDING /
                        NEED_PASSCODE / NO_TOKEN / RELOGIN / ACCEPT_FAILED
    "ok" は received / already_received の時だけ True。
    """
    code = extract_code(code_or_url)
    if not code:
        return _res("failed", reason="BAD_LINK", error="リンク/コードを読み取れない")
    if info is None:
        chk = inspect_link(code)
        info = chk.get("info")
        if not info:
            return _res("failed", reason=chk.get("reason") or "BAD_LINK",
                        error=chk.get("error") or "リンク情報を取得できない", code=code)

    kind = classify_status(info.get("order_status"))
    if kind == "success":
        return confirm_completed(code, info, history_pages)
    if kind == "fail":
        return _res("failed", reason="NOT_PENDING",
                    error=f"受取不可: {status_text(info.get('order_status'))}",
                    **_link_base(code, info))
    if info.get("has_password") and not passcode:
        return _res("failed", reason="NEED_PASSCODE",
                    error="このリンクはパスコード付き。パスコードも入力して。",
                    **_link_base(code, info))
    try:
        at, du = _tokens()
    except PayPayError as e:
        return _res("failed", reason="NO_TOKEN", error=str(e), **_link_base(code, info))

    r = accept_once(code, passcode, info, at, du)
    if r["kind"] == "auth":
        tok = _token_after_auth_error(at)
        if not tok:
            return _res("failed", reason="RELOGIN", http=r["http"],
                        error=("PayPayのセッションが切れていて更新もできない。"
                               "`/paypayログイン` で入り直して。"),
                        **_link_base(code, info))
        # 1回目が実は通っていた場合に備えて、状態を見直してから投げ直す
        chk = inspect_link(code)
        info2 = chk.get("info")
        if not info2:
            return _res("unknown", reason="RECHECK_FAILED", http=r["http"],
                        error="セッション更新後にリンクを確認できなかった",
                        **_link_base(code, info))
        kind2 = classify_status(info2.get("order_status"))
        if kind2 == "success":
            return confirm_completed(code, info2, history_pages)
        if kind2 == "fail":
            return _res("failed", reason="NOT_PENDING",
                        error=f"受取不可: {status_text(info2.get('order_status'))}",
                        **_link_base(code, info2))
        info = info2
        at, du = tok
        r = accept_once(code, passcode, info, at, du)
        if r["kind"] == "auth":
            return _res("failed", reason="RELOGIN", http=r["http"],
                        error=("PayPayのセッションを更新しても受取APIに弾かれた。"
                               "`/paypayログイン` で入り直して。"),
                        **_link_base(code, info))
    return _after_accept(code, info, r, history_pages)


def _after_accept(code: str, info: dict, r: dict, pages: int) -> dict:
    rc = {"result_code": r["result_code"], "result_message": r["result_message"],
          "http": r["http"]}
    base = _link_base(code, info)
    if r["kind"] == "ok":
        p = r["payload"] or {}
        amount = settled_amount(info.get("amount"),
                                p.get("amount") or p.get("receivedAmount"))
        return _res("received", amount=amount, **rc, **base)
    if r["kind"] == "held":
        return _res("held", **rc, **base,
                    error=("受け取りが一時保留になった。"
                           "送信者がPayPayアプリで承認すると自動で着金する。"))

    # unknown / rejected: 通ったかどうかをリンクの状態で確かめる
    chk = inspect_link(code)
    info2 = chk.get("info")
    if not info2:
        return _res("unknown", reason="RECHECK_FAILED", **rc, **base,
                    error="受取APIの後にリンクを確認できなかった")
    kind = classify_status(info2.get("order_status"))
    if kind == "success":
        res = confirm_completed(code, info2, pages)
        res.update(rc)
        return res
    if kind == "fail":
        return _res("failed", reason="NOT_PENDING", **rc, **_link_base(code, info2),
                    error=f"受取不可: {status_text(info2.get('order_status'))}")
    if r["kind"] == "unknown":
        return _res("unknown", reason="NO_RESPONSE", **rc, **base,
                    error=f"受取APIの結果が分からない ({r['result_message'] or r['http']})")

    code_ = r["result_code"]
    msg = r["result_message"]
    hint = _ACCEPT_HINTS.get(code_, "")
    return _res("failed", reason="ACCEPT_FAILED", **rc, **base,
                error=(f"受取できなかった (コード {code_}" + (f" / {msg}" if msg else "") + ")"
                       + (f"\n{hint}" if hint else "")))


# ============================================================
# 払い出し (Kyash → PayPay の換金で使う)
#
# 投げた後に通信が切れた時は「失敗」にしない。PayPay側で処理されていれば
# 送金は成立しているので、投げ直すと二重払いになる。"unknown" で返して
# 呼び出し側は UNCERTAIN で止める。接続前に落ちたと言い切れる時だけ failed。
# ============================================================

_payout_lock = threading.Lock()


def _post_once(path: str, body: dict, at: str, du: str) -> dict:
    import httpx
    out = {"kind": "unknown", "http": 0, "result_code": "", "result_message": "",
           "payload": {}}
    session = None
    try:
        session, hdrs = paypay_api._api_session(at, du)
        r = session.post(f"{paypay_api.BASE_APP4}{path}", params={"payPayLang": "ja"},
                         json=body, headers={**hdrs, "Content-Type": "application/json"})
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        out["kind"] = "not_sent"
        out["result_message"] = f"接続できない: {type(e).__name__}"
        return out
    except Exception as e:
        out["result_message"] = f"通信失敗: {type(e).__name__}: {e}"
        return out
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
    out["http"] = r.status_code
    try:
        data = r.json()
    except Exception:
        data = None
    if not isinstance(data, dict):
        if r.status_code in (401, 403):
            out["kind"] = "auth"
        out["result_message"] = f"HTTP {r.status_code}: 応答がJSONでない"
        return out
    header = data.get("header") or {}
    rc = str(header.get("resultCode") or "")
    out["result_code"] = rc
    out["result_message"] = str(header.get("resultMessage") or "")
    if rc == "S0000":
        out["kind"] = "ok"
        out["payload"] = data.get("payload") or {}
    elif r.status_code in (401, 403):
        out["kind"] = "auth"
    elif r.status_code >= 500 or not rc:
        out["kind"] = "unknown"
    else:
        out["kind"] = "rejected"
    return out


def _payout(path: str, body: dict) -> dict:
    """1回だけ投げる。401/403 (処理されていない) の時だけトークンを更新して投げ直す。"""
    with _payout_lock:
        try:
            at, du = _tokens()
        except PayPayError as e:
            return {"status": "failed", "error": str(e), "reason": "NO_TOKEN"}
        r = _post_once(path, body, at, du)
        if r["kind"] == "auth":
            tok = _token_after_auth_error(at)
            if not tok:
                return {"status": "failed", "reason": "RELOGIN",
                        "error": "PayPayのセッションが切れていて更新もできない"}
            r = _post_once(path, body, *tok)
            if r["kind"] == "auth":
                return {"status": "failed", "reason": "RELOGIN",
                        "error": "PayPayのセッションを更新しても弾かれた"}
    rc = {"result_code": r["result_code"], "result_message": r["result_message"],
          "http": r["http"]}
    if r["kind"] == "ok":
        return {"status": "sent", "payload": r["payload"], **rc}
    if r["kind"] in ("rejected", "not_sent"):
        return {"status": "failed", "reason": "REJECTED", **rc,
                "error": (f"PayPayに断られた (コード {r['result_code'] or '-'}"
                          + (f" / {r['result_message']}" if r["result_message"] else "") + ")")}
    return {"status": "unknown", "reason": "NO_RESPONSE", **rc,
            "error": f"結果が分からない ({r['result_message'] or r['http']})"}


def payout_link(amount: int, passcode: str | None = None) -> dict:
    """送金リンクを作る。戻り: {"status": "sent"|"failed"|"unknown", "link", "order_id", ...}"""
    from uuid import uuid4
    body = {"requestId": str(uuid4()), "amount": int(amount), "socketConnection": "P2P",
            "theme": "default-sendmoney", "source": "sendmoney_home_sns"}
    if passcode:
        body["passcode"] = passcode
    res = _payout("/bff/v2/executeP2PSendMoneyLink", body)
    if res["status"] == "sent":
        p = res.get("payload") or {}
        res["link"] = str(p.get("link") or "")
        res["order_id"] = str(p.get("orderId") or "")
        if not res["link"]:
            res["status"] = "unknown"
            res["error"] = "リンク作成は成功扱いだが、応答にリンクが無い"
    return res


def payout_direct(amount: int, external_id: str) -> dict:
    """登録済みのPayPay垢 (externalId) へ直接送る。戻り: {"status", "order_id", ...}"""
    from uuid import uuid4
    if not external_id:
        return {"status": "failed", "reason": "NO_DEST", "error": "送り先のPayPay IDが無い"}
    body = {"amount": int(amount), "theme": "DEFAULT", "requestId": str(uuid4()),
            "externalReceiverId": str(external_id), "ackRiskError": False,
            "source": "P2P", "socketConnection": "P2P"}
    res = _payout("/p2p/v3/executeP2PSendMoney", body)
    if res["status"] == "sent":
        res["order_id"] = str((res.get("payload") or {}).get("orderId") or "")
    return res


def sendable_balance() -> int | None:
    """送れる残高 (マネー + マネーライト。ポイントは送れないので含めない)"""
    b = get_balance()
    if not b:
        return None
    try:
        return int(b.get("money") or 0) + int(b.get("money_light") or 0)
    except (TypeError, ValueError):
        return None


# ============================================================
def mask_phone(phone: str) -> str:
    p = str(phone or "")
    return f"{p[:3]}****{p[-2:]}" if len(p) >= 6 else "***"


def logout() -> list[str]:
    """保存済みトークンを消す。消えたファイル名を返す。

    消す前にランダムデータで上書きする。PayPayトークンは1本で残高を
    動かせるので、復元できる残骸が残ったままでは「失効させた」ことにならない。
    """
    removed = []
    for path in (paypay_api.ACCOUNTS_FILE, paypay_api.TOKEN_FILE):
        if not os.path.exists(path):
            continue
        try:
            size = os.path.getsize(path)
            with open(path, "r+b") as f:
                f.write(os.urandom(size))
                f.flush()
                os.fsync(f.fileno())
            os.remove(path)
            removed.append(os.path.basename(path))
        except Exception as e:
            print(f"[paypay] {os.path.basename(path)} を消せなかった: {e}")
    return removed


if __name__ == "__main__":
    t = account_info()
    print("アカウント:", (t or {}).get("phone", "なし"))
    print("alive:", alive())
    b = get_balance()
    print("残高:", b)
