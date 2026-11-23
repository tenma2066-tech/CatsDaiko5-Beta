import uuid as _uuid, secrets, json, time, os
import requests, msgpack
import tsum_login as L

_HERE = os.path.dirname(os.path.abspath(__file__))
_PENDING_FILE = os.path.join(_HERE, "tsum_guest_pending.json")


def _jdump_atomic(path, obj, **kw):
    kw.setdefault("ensure_ascii", False)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, **kw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise


def _pending_load():
    try:
        d = json.load(open(_PENDING_FILE, encoding="utf-8-sig"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def save_pending(uuid_str, migration_id, note=""):
    """migration.nhn を呼ぶ *前* に uuid を記録する。
    引き継ぎはお客さんのアカウントをこの uuid へ移す操作なので、応答が取れなくても
    uuid さえ残っていれば guest_recover(uuid) で救出できる。ここを飛ばすと
    通信エラー1回でアカウントが永久に取り出せなくなる。"""
    try:
        data = _pending_load()
        data.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "uuid": uuid_str,
                     "migration_id": str(migration_id), "status": "pending", "note": note})
        _jdump_atomic(_PENDING_FILE, data, indent=2)
        print(f"[guest] 移行前に uuid を保存: {uuid_str} (old_id={migration_id})")
    except Exception as e:
        print(f"[guest] pending 保存失敗: {e}")


def mark_pending_done(uuid_str, new_id=None):
    """新しい引き継ぎ情報をお客さんに渡せた時点で、救出待ちから外す。"""
    try:
        data = _pending_load()
        changed = False
        for row in data:
            if row.get("uuid") == uuid_str and row.get("status") == "pending":
                row["status"] = "done"
                row["new_id"] = new_id
                changed = True
        if changed:
            _jdump_atomic(_PENDING_FILE, data, indent=2)
    except Exception as e:
        print(f"[guest] pending 更新失敗: {e}")


def _auth_guest(uuid_str, proxies=None, tries=3):
    p = msgpack.packb({"uuid": uuid_str, "termsResult": L.TERMS_RESULT, "country": "JP"}, use_bin_type=False)
    _px = proxies if proxies is not None else L._creds_proxy()
    last = ""
    for i in range(tries):
        try:
            r = requests.post(L.AUTH_BASE + "/auth/v3.8/authentication/GUEST", data=p,
                              headers=L.auth_headers(p), proxies=_px, timeout=20)
            j = r.json()
            if isinstance(j, dict) and j.get("userToken"):
                return j
            last = "retcode=%s" % _retcode(j) if isinstance(j, dict) else "HTTP %s" % r.status_code
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:100])
        if i < tries - 1:
            time.sleep(0.6 * (i + 1))
    print("[guest] auth GUEST 失敗: %s" % last)
    return {"_error": last}


def _common(userid="", hsh="", checkval=""):
    return [("userid", userid), ("appver", L.APPVER), ("resver", L.RESVER), ("mstver", L.MSTVER),
            ("hash", hsh), ("os", "2"), ("lang", "ja"), ("checkval", checkval),
            ("countrycode", "JP"), ("growthyinfo", L.growthyinfo())]


_PUB_CACHE = {"pub": None, "kid": None, "ts": 0.0}
_GAME_SESS = None


def _game_proxies(proxies=None):
    """bot_config.json の tor_game_api が false の場合は直接通信する。"""
    try:
        d = json.load(open(L._BOT_CONFIG_FILE, encoding="utf-8-sig"))
        if not d.get("tor_game_api", False):
            return None
    except Exception:
        pass
    return proxies if proxies is not None else L._creds_proxy()


def _get_pub_key(s, proxies=None):
    now = time.time()
    if _PUB_CACHE["pub"] and (now - _PUB_CACHE["ts"] < 600):
        return _PUB_CACHE["pub"], _PUB_CACHE["kid"]
    _px = _game_proxies(proxies)
    j = s.get(L.GAME_BASE + "api/getPublicKey.nhn?appver=" + L.APPVER, timeout=15, proxies=_px or {}).json()
    pub = j.get("public_key") or j.get("publicKey")
    kid = str(j.get("kid", ""))
    if pub:
        _PUB_CACHE["pub"] = pub
        _PUB_CACHE["kid"] = kid
        _PUB_CACHE["ts"] = now
    return pub, kid


def _game_post_once(path, pairs, proxies=None):
    global _GAME_SESS
    if _GAME_SESS is None:
        _GAME_SESS = requests.Session()
        _GAME_SESS.headers.update({"User-Agent": "tsumtsum/%s" % L.APPVER})
    s = _GAME_SESS
    _px = _game_proxies(proxies)
    s.proxies = _px or {}
    key = secrets.token_bytes(32)
    pub, kid = _get_pub_key(s, proxies)
    enc = L.aes_gcm_encrypt(L.build_form(pairs), key)
    hdr = {"Content-Type": "application/tsum", "X-Encrypted-Session-Key": L.rsa_oaep(key, pub), "X-Key-Id": kid}
    r = s.post(L.GAME_BASE + path, data=enc, headers=hdr, timeout=20)
    return json.loads(L.aes_gcm_decrypt(r.content, key))


def _game_post(path, pairs, proxies=None):
    """失敗しても {} を返さない。
    {} を返すと呼び出し側が『IDかパスワードが違う』と誤って判断してしまうため、
    通信・復号エラーは _error 付きの辞書で区別できるようにする。"""
    try:
        resp = _game_post_once(path, pairs, proxies)
    except Exception as e:
        return {"_error": type(e).__name__, "_error_detail": str(e)[:150]}
    # retcode 201（要アップデート）は appver を取り直して1回だけ再試行する
    if isinstance(resp, dict) and resp.get("retcode") == 201:
        try:
            L.resolve_appver(force=True)
            print(f"[guest] {path} が retcode201 → appver={L.APPVER} で再試行")
            resp = _game_post_once(path, pairs, proxies)
        except Exception as e:
            return {"_error": type(e).__name__, "_error_detail": str(e)[:150]}
    return resp


def _post_failed(resp):
    return not isinstance(resp, dict) or bool(resp.get("_error"))


def _retcode(obj):
    if not isinstance(obj, dict):
        return None
    rc = obj.get("retcode")
    if rc is None:
        return None
    try:
        return int(rc)
    except (TypeError, ValueError):
        return None


def _login(ut, proxies=None):
    lg = _game_post("login.nhn", [("rankdt", 0), ("accesstoken", ut), ("trigger", 0)] + _common(), proxies)
    ui = (lg.get("userinfo") or {}) if isinstance(lg, dict) else {}
    rc = _retcode(lg)
    rm = (lg.get("retmsg") or "") if isinstance(lg, dict) else ""
    if rc != 0 or not ui.get("checkval"):
        is_banned = (rc == 29) or any(k in rm for k in ("停止", "不正利用", "BAN", "利用制限"))
        return {"ok": False, "retcode": rc, "retmsg": rm, "banned": is_banned}
    return {"ok": True, "userToken": ut, "userid": ui.get("userid"), "hash": lg.get("hash"), "checkval": ui.get("checkval")}


def guest_create(proxies=None, on_step=None):
    u = str(_uuid.uuid4())
    if on_step:
        try:
            on_step("ゲスト認証APIと通信しています...")
        except Exception:
            pass
    g = _auth_guest(u, proxies)
    ut = g.get("userToken")
    if not ut:
        return None, "ゲスト認証に失敗しました"
    if on_step:
        try:
            on_step("ゲームサーバーに初期登録(login)中...")
        except Exception:
            pass
    sess = _login(ut, proxies)
    if not sess or not sess.get("ok"):
        return None, "ゲスト登録(login)に失敗しました"
    sess["uuid"] = u
    save_pending(u, "(新規作成)", note="guest_create")
    return sess, None


_TRANSFERS_FILE = os.path.join(_HERE, "tsum_guest_transfers.json")


def normalize_transfer_input(migration_id, password):
    """全角英数・記号の半角化、余分な空白・ゼロ幅スペースの除去を行う。"""
    import unicodedata, re
    mid = unicodedata.normalize("NFKC", str(migration_id or "")).strip().lower()
    mid = re.sub(r"[\s\u200b\ufeff\u3000\-_:：]", "", mid)
    pw = unicodedata.normalize("NFKC", str(password or "")).strip()
    pw = re.sub(r"[\r\n\t\u200b\ufeff\u3000]", "", pw)
    return mid, pw


def lookup_known_transfer(mid):
    """過去の引き継ぎ履歴から mid に関連する記録を探す。"""
    try:
        if os.path.exists(_TRANSFERS_FILE):
            data = json.load(open(_TRANSFERS_FILE, encoding="utf-8-sig"))
            if isinstance(data, list):
                for row in reversed(data):
                    if row.get("old_migration_id") == mid:
                        return "already_transferred", row
                    if row.get("new_id") == mid:
                        return "known_new_id", row
    except Exception:
        pass
    return None, None


def guest_takeover(migration_id, password, proxies=None):
    mid, pw = normalize_transfer_input(migration_id, password)
    if not mid or not pw:
        return None, "引き継ぎ番号またはパスワードが空です"

    # 既に以前の代行で使われた old_migration_id かどうかを事前チェック
    trans_type, trans_row = lookup_known_transfer(mid)
    if trans_type == "already_transferred":
        nid = trans_row.get("new_id")
        npw = trans_row.get("new_pw")
        return None, (f"この引き継ぎ番号【{mid}】は前回の代行で既に引き継ぎ完了しています。\n"
                      f"前回発行された【引き継ぎ番号: {nid} / パスワード: {npw}】でログインしてください。")

    u = str(_uuid.uuid4())
    g = _auth_guest(u, proxies)
    ut = g.get("userToken")
    if not ut:
        return None, "ゲスト認証に失敗しました"

    # migration.nhn はアカウントを uuid u へ移す操作。応答が取れなくても救出できるよう、
    # 呼ぶ前に必ず uuid を保存しておく。
    save_pending(u, mid)

    # 試行するパスワードの候補リスト (入力値 -> known_new_pw があればその値)
    pw_candidates = [pw]
    if trans_type == "known_new_id":
        known_pw = trans_row.get("new_pw")
        if known_pw and known_pw != pw:
            pw_candidates.append(known_pw)

    mig = None
    rc = None
    ok = False
    for try_pw in pw_candidates:
        mig = _game_post("migration.nhn",
                         [("accesstoken", ut), ("id", str(mid)), ("password", str(try_pw))] + _common(), proxies)
        if _post_failed(mig):
            # 通信・復号エラー。サーバー側では移行が完了している可能性があるので、
            # 「IDかパスワードが違う」とは絶対に言わない（uuid は保存済み）。
            return None, ("通信エラーで引き継ぎ結果を確認できませんでした。\n"
                          "時間をおいてから再度お試しいただくか、ゲーム内でお確かめください。")
        rc = _retcode(mig)
        ok = (rc == 0) or bool(mig.get("session_id") or mig.get("userKey"))
        if rc is not None and rc != 0:
            ok = False
        if ok:
            break
        # retcode 2010 (ID/PW違い) でかつ別候補があるなら次を試す
        if rc == 2010 and len(pw_candidates) > 1 and try_pw != pw_candidates[-1]:
            print(f"[guest] パスワード {try_pw} が retcode2010 → 履歴の正しいパスワード {pw_candidates[-1]} で再試行")
            continue

    if not ok:
        mark_pending_done(u, None)   # サーバーが明確に拒否＝移行していないので救出対象から外す
        rm = mig.get("retmsg") or ""
        hint = ""
        if trans_type == "known_new_id":
            hint = "\n※この引き継ぎ番号はBotが発行したものです。ゲーム内で再度「引き継ぎ準備」を行い、新しい番号とパスワードを発行してください。"
        return None, ("引き継ぎに失敗しました(引き継ぎ番号かパスワードが違う/期限切れ)"
                      + (("\n" + rm) if rm else "")
                      + hint
                      + (f"\n(retcode={rc})" if rc is not None else ""))
    sess = None
    last_res = None
    for _try in range(3):
        if _try > 0 and isinstance(proxies, dict):
            # リトライは新しいTor回線(=別IP)で試す。同じ回線のままだと、たまたま
            # 繋がりの悪い出口ノードに当たっただけで3回とも同じ理由で失敗し続ける。
            proxies = {k: L.fresh_socks_identity(v) for k, v in proxies.items()}
        g2 = _auth_guest(u, proxies)
        ut2 = g2.get("userToken") or ut
        res = _login(ut2, proxies)
        if res and res.get("ok"):
            sess = res
            ut = ut2
            break
        last_res = res
        if res and res.get("banned"):
            # アカウントBANの場合は回線変えても無意味なので即リトライ終了
            break
        time.sleep(0.5)
    if not sess:
        # 移行は済んでいるがログインできない。checkval が空なので以降の API は必ず失敗する。
        # 盛り処理を走らせても無駄なので、その旨をフラグで伝える。
        is_banned = bool(last_res and last_res.get("banned"))
        b_msg = (last_res.get("retmsg") if last_res else "") or "不正利用が確認されたため、こちらのアカウントは停止されております。"
        sess = {"userToken": ut, "userid": str(mig.get("userid") or ""), "hash": "", "checkval": "",
                "login_failed": True, "banned": is_banned, "ban_msg": b_msg}
    sess["uuid"] = u
    sess["migrated_userKey"] = mig.get("userKey")
    sess["migrated"] = True
    return sess, None


def guest_recover(uuid_str, proxies=None):
    g = _auth_guest(uuid_str, proxies)
    ut = g.get("userToken")
    if not ut:
        return None, "再認証に失敗しました"
    sess = _login(ut, proxies)
    if not sess or not sess.get("ok"):
        return None, "loginに失敗しました"
    sess["uuid"] = uuid_str
    mid, pw = guest_issue_transfer(sess, proxies)
    if mid:
        return (mid, pw), None
    return None, "引き継ぎ発行に失敗しました: " + str(pw)


def guest_issue_transfer(sess, proxies=None, migration_id=None):
    if not (sess.get("userid") and sess.get("checkval")):
        return None, "セッションが不完全なため引き継ぎを発行できません"
    if not migration_id:
        # ツムツムサーバーの要件: 8文字以上、2種以上の文字、スペース不可
        # 英小文字5文字＋数字3文字をシャッフルして必ず2種以上を満たす
        _letters = [secrets.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(5)]
        _digits = [secrets.choice("0123456789") for _ in range(3)]
        _chars = _letters + _digits
        secrets.SystemRandom().shuffle(_chars)
        mid = "".join(_chars)
    else:
        mid = migration_id
    r = _game_post("migrationId.nhn",
                   [("accesstoken", sess["userToken"]), ("id", mid)]
                   + _common(sess["userid"], sess.get("hash", ""), sess["checkval"]), proxies)
    if isinstance(r, dict) and r.get("password"):
        return mid, r.get("password")
    return None, (r.get("retmsg") if isinstance(r, dict) else str(r))[:160]
