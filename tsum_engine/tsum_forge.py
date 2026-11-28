import os, json, base64, time, secrets, urllib.parse, datetime, sys, io, argparse
import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes, serialization

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import builtins as _bi
try:
    _DBGP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tsum_debug.log")
    _DBG_LOG = open(_DBGP, "w" if (os.path.exists(_DBGP) and os.path.getsize(_DBGP) > 5_000_000) else "a",
                    encoding="utf-8", errors="replace")
except Exception:
    _DBG_LOG = None
def print(*a, **k):
    if _DBG_LOG is None:
        return
    k["file"] = _DBG_LOG; k["flush"] = True
    _bi.print(*a, **k)

BASE = "https://lgtmtm-game.linegame.jp/"
import tsum_login as _L
APPVER, RESVER, MSTVER = _L.APPVER, _L.RESVER, _L.MSTVER

def _sync_versions():
    """tsum_login 側で appver/resver/mstver が更新されたら取り込む。
    import 時のスナップショットのまま古いバージョンを送り続けるのを防ぐ。"""
    global APPVER, RESVER, MSTVER
    APPVER, RESVER, MSTVER = _L.APPVER, _L.RESVER, _L.MSTVER
    return APPVER, RESVER, MSTVER


def is_version_reject(resp):
    """retcode 201 (=「最新のバージョンにアップデートしてください」) かどうか。"""
    if not isinstance(resp, dict):
        return False
    if resp.get("retcode") != 201:
        return False
    msg = " ".join(str(resp.get(k, "")) for k in ("retmsg", "message", "errmsg", "error"))
    low = msg.lower()
    if any(k in msg for k in ("アップデート", "バージョン", "更新")):
        return True
    if any(k in low for k in ("version", "appver", "resver", "mstver", "update")):
        return True
    return not msg.strip()


def info_has_heart_data(info):
    """getInfo が本当に成功して userinfo を返したときだけ True。
    エラー応答(retcode!=0 / userinfo なし)を『ハート0』と誤読しないためのガード。"""
    if not isinstance(info, dict):
        return False
    rc = info.get("retcode")
    if rc is None or str(rc) != "0":
        return False
    ui = info.get("userinfo")
    return isinstance(ui, dict) and ("bheart" in ui or "pheart" in ui)


def info_has_coin_data(info):
    """コイン残量を信用してよい応答かどうか (insufficient_coin の誤判定防止)。"""
    if not isinstance(info, dict):
        return False
    rc = info.get("retcode")
    if rc is None or str(rc) != "0":
        return False
    ui = info.get("userinfo")
    return isinstance(ui, dict) and ("bcoin" in ui or "pcoin" in ui)


# ペアツム（partnerlist持ち）のIDセット。通常の単体ゲーム開始(gameStart.nhn)でサーバーエラー(8/997)になりやすいため
# 自動判定して通常単体ツム（ミッキー等）を優先する。
PAIR_TSUM_IDS = {
    364, 368, 376, 382, 384, 386, 388, 391, 393, 402, 404, 424, 431, 440,
    443, 462, 471, 473, 484, 490, 493, 495, 497, 499, 507, 514, 516, 518, 523, 533,
    549, 553, 556, 567, 578, 581, 586, 597, 603, 608, 625, 633, 642, 644,
    646, 652, 659, 668, 671, 673, 675, 679, 681, 690, 702, 709, 714, 717,
    721, 731, 733, 735, 743, 750, 760, 763, 765, 767, 771, 773, 776, 782,
    801, 814, 823, 832, 835, 850, 854, 862, 864, 866, 870, 876, 892, 895,
    912, 915, 931, 935, 939, 960, 964, 969, 971, 975, 979, 988, 995, 997, 1000,
    1005, 1009, 1012, 1020, 1027, 1032, 1038, 1045, 1047
}

def is_pair_tsum(tsumid, mast=None):
    try:
        tid = int(tsumid or 0)
    except (TypeError, ValueError):
        return False
    if tid in PAIR_TSUM_IDS:
        return True
    if isinstance(mast, dict):
        for t in (mast.get("tsummst") or []):
            if isinstance(t, dict) and t.get("tsumid") == tid:
                return bool(t.get("partnerlist"))
    return False


def owned_tsum_ids(info):
    """実際に所持しているツムIDのみを抽出する（未所持/未獲得を除外）。"""
    if not isinstance(info, dict):
        return []
    res = []
    for t in (info.get("tsuminfo") or []):
        if not isinstance(t, dict):
            continue
        tid = t.get("tsumid")
        if not tid:
            continue
        try:
            tid_int = int(tid)
        except (ValueError, TypeError):
            continue
        slv = int(t.get("skilllv") or 0)
        cnt = int(t.get("tsumcnt") or 0)
        lv = int(t.get("lv") or 0)
        lim = int(t.get("limitlv") or 0)
        if slv > 0 or cnt > 0 or lv > 0 or lim > 0:
            if tid_int not in res:
                res.append(tid_int)
    return res


def safe_playable_tsum(info, prefer_tid=None):
    """gameStart.nhn が確実に通る通常単体ツム（ペアツム除外）を選ぶ。"""
    owned = owned_tsum_ids(info) if isinstance(info, dict) else []
    if prefer_tid:
        try:
            pt = int(prefer_tid)
            if (not owned or pt in owned) and not is_pair_tsum(pt):
                return pt
        except (ValueError, TypeError):
            pass
    if isinstance(info, dict):
        sett = next((t.get("tsumid") for t in (info.get("tsuminfo") or [])
                     if isinstance(t, dict) and t.get("setflg") in (1, "1")), None)
        if sett:
            try:
                st = int(sett)
                if (not owned or st in owned) and not is_pair_tsum(st):
                    return st
            except (ValueError, TypeError):
                pass
    if 1 in owned:
        return 1
    for tid in owned:
        if not is_pair_tsum(tid):
            return tid
    return 1



# バージョン復旧の連打防止 (失敗直後はしばらく再探索しない)
_VER_FIX = {"ts": 0.0, "marked": False}
_VER_FIX_COOLDOWN = 120


def _version_recovery_combos():
    """201 を食らったときに試す (appver, resver, mstver) の候補。現行の組み合わせは除外。"""
    base = (_L.APPVER, _L.RESVER, _L.MSTVER)
    try:
        new_av = _L.resolve_appver(force=True) or base[0]
    except Exception as e:
        print(f"[ver] appver再解決に失敗: {e}")
        new_av = base[0]
    avs = [new_av] + ([base[0]] if base[0] != new_av else [])
    combos = []
    for av in avs:
        for rv in _L.resver_candidates(av):
            for mv in (base[2], _L.MSTVER_FALLBACK):
                c = (av, rv, mv)
                if c != base and c not in combos:
                    combos.append(c)
    return combos[:8]


import tsum_login
TERMINAL = tsum_login.DEVICE_ID

USERID   = ""
HASH     = ""
CHECKVAL = ""

def growthyinfo():
    ts = datetime.datetime.now().strftime("%Y%m%d %H%M%S")
    return json.dumps({
        "sdkVersion":"3.0","osVer":"9","terminalId":TERMINAL,
        "deviceName":"SM-A805N","country":"JP","language":"ja",
        "networkType":2,"carrier":"440/00","clientTimestamp":ts
    }, separators=(",",":"))

def aes_gcm_encrypt(pt, key):
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, pt.encode("utf-8"), None)
def aes_gcm_decrypt(data, key):
    return AESGCM(key).decrypt(data[:12], data[12:], None).decode("utf-8")
def rsa_oaep(key, pub_b64):
    pub = serialization.load_der_public_key(base64.b64decode(pub_b64))
    enc = pub.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                        algorithm=hashes.SHA256(), label=None))
    return base64.b64encode(enc).decode()

def qenc(v):
    return urllib.parse.quote(str(v), safe='')

def build_form(pairs):
    return "&".join(f"{k}={qenc(v)}" for k, v in pairs)

def _proxy_from_creds():
    """Forge が明示的な proxy 未指定(既定値)で作られたときに使う既定プロキシ。
    設定の実体は tsum_login.py の _creds_proxy() 1箇所にまとめてある。"""
    try:
        return _L._creds_proxy()
    except Exception:
        return None

_DEFAULT_PROXY = object()

def _proxy_from_value(proxy):
    if not proxy:
        return None
    if isinstance(proxy, dict):
        return {k: v for k, v in proxy.items() if v}
    return {"http": proxy, "https": proxy}

class Forge:
    _EV_CACHE = {"ts": 0.0, "cands": None}

    def __init__(self, userid=None, hashv=None, checkval=None, proxy=_DEFAULT_PROXY, user_token=None):
        self.s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=25,
            pool_maxsize=25,
        )
        self.s.mount("https://", adapter)
        self.s.mount("http://", adapter)
        self.s.headers.update({
            "User-Agent": "tsumtsum/%s" % APPVER,
            "Connection": "keep-alive",
        })
        _px = _proxy_from_creds() if proxy is _DEFAULT_PROXY else _proxy_from_value(proxy)
        if _px: self.s.proxies.update(_px)
        self.key = secrets.token_bytes(32)
        self.pub = None; self.kid = None; self.sid = None
        self.last_http_status = None
        self.last_error = None
        self.last_response = None
        self.last_gs_attempts = []
        self._last_multi_fail = None
        self.last_gs_response = None
        self._gs_combo = None
        self._last_info = None
        self.userid = userid or USERID
        self.hash = hashv or HASH
        self.checkval = checkval or CHECKVAL
        self.user_token = user_token

    def refresh_session(self):
        """セッション(X-Session-Id / checkval / hash)をサーバーで再ログインして更新する。"""
        self.sid = None
        if not self.user_token:
            return False
        try:
            print(f"[session] セッション再ログインを実行 (userid={self.userid})")
            lg = self.login(self.user_token)
            if isinstance(lg, dict) and lg.get("retcode") == 0:
                ui = lg.get("userinfo") or {}
                if lg.get("hash"):
                    self.hash = lg["hash"]
                if ui.get("checkval"):
                    self.checkval = ui["checkval"]
                if ui.get("userid"):
                    self.userid = str(ui["userid"])
                print(f"[session] 再ログイン成功: checkval={self.checkval[:4]}... hash={self.hash[:8]}...")
                return True
            else:
                print(f"[session] 再ログイン失敗: retcode={lg.get('retcode') if isinstance(lg, dict) else lg}")
        except Exception as e:
            print(f"[session] 再ログイン例外: {e}")
        return False

    def get_public_key(self, tries=3):
        _sync_versions()
        u = BASE + "api/getPublicKey.nhn?appver=" + _L.APPVER
        self.s.headers.update({"User-Agent": "tsumtsum/%s" % _L.APPVER})
        last = None
        for i in range(tries):
            try:
                r = self.s.get(u, timeout=10)
                if r.status_code != 200:
                    raise requests.exceptions.RequestException(f"HTTP {r.status_code}")
                j = r.json()
                if not (j.get("public_key") or j.get("publicKey")) and j.get("retcode") == 201:
                    _L.resolve_appver(force=True)
                    _sync_versions()
                    u = BASE + "api/getPublicKey.nhn?appver=" + _L.APPVER
                    self.s.headers.update({"User-Agent": "tsumtsum/%s" % _L.APPVER})
                    continue
                self.pub = j.get("public_key") or j.get("publicKey")
                self.kid = str(j.get("kid",""))
                print(f"[getPublicKey] {r.status_code} kid={self.kid}")
                return self.pub
            except (requests.exceptions.RequestException, ValueError) as e:
                last = e
                print(f"  [getPublicKey 一時失敗 {i+1}/{tries}: {str(e)[:120]}]")
                if i < tries - 1:
                    time.sleep(0.5 * (i + 1))
        raise requests.exceptions.RequestException(f"getPublicKey failed: {last}")

    def _headers(self):
        h = {
            "Content-Type": "application/tsum",
            "Connection": "keep-alive",
        }
        if self.sid:
            h["X-Session-Id"] = self.sid
        else:
            h["X-Encrypted-Session-Key"] = rsa_oaep(self.key, self.pub)
            h["X-Key-Id"] = self.kid
        return h

    def _reset_session(self):
        """appver を変えたら公開鍵/セッションも取り直す。"""
        self.pub = None
        self.kid = None
        self.sid = None
        self.s.headers.update({"User-Agent": "tsumtsum/%s" % _L.APPVER})

    @staticmethod
    def _needs_session_resync(resp):
        """サーバーが「一度タイトルに戻ります」（＝状態を取り直して再試行せよ）を返したか。
        retcode 6（セッション切れ）に加え、retcode 22（コイン残数などデータの同期ずれ。
        プレイ報酬でコインが変わった直後の上限解放などで出る）も対象にする。"""
        if not isinstance(resp, dict):
            return False
        rc = resp.get("retcode")
        if rc == 6:
            return True
        if rc == 22:
            msg = " ".join(str(resp.get(k, "")) for k in ("retmsg", "message", "errmsg"))
            return ("一致しません" in msg) or ("タイトルに戻" in msg) or (resp.get("retsubcode") == 2)
        return False

    def post(self, path, specific_pairs):
        resp = self._post_once(path, specific_pairs)
        # セッション切れ(retcode 6)・データ同期ずれ(retcode 22) の自動復旧。
        # どちらもサーバーが「一度タイトルに戻ります」= 取り直して再試行せよ、を意味する。
        # 再ログインで hash/checkval とコイン残数をサーバーの現在値に合わせてから1回だけやり直す。
        if self._needs_session_resync(resp) and not getattr(self, "_in_session_refresh", False):
            self._in_session_refresh = True
            try:
                if self.refresh_session():
                    resp = self._post_once(path, specific_pairs)
            finally:
                self._in_session_refresh = False

        if not is_version_reject(resp) or getattr(self, "_in_ver_recovery", False):
            return resp

        now = time.time()
        if now - _VER_FIX["ts"] < _VER_FIX_COOLDOWN:
            return resp
        _VER_FIX["ts"] = now

        base = (_L.APPVER, _L.RESVER, _L.MSTVER)
        fixed = False
        self._in_ver_recovery = True
        try:
            combos = _version_recovery_combos()
            print(f"[ver] {path} が retcode201 で拒否 → 候補 {len(combos)} 件を試行 (現行={base})")
            for av, rv, mv in combos:
                _L.apply_versions(av, rv, mv, cache=False)
                _sync_versions()
                self._reset_session()
                try:
                    retry = self._post_once(path, specific_pairs)
                except Exception as e:
                    print(f"[ver] 候補 appver={av} resver={rv} mstver={mv} で例外: {e}")
                    continue
                if isinstance(retry, dict) and not is_version_reject(retry):
                    _L.apply_versions(av, rv, mv, cache=True)
                    _sync_versions()
                    _VER_FIX["ts"] = 0.0
                    _VER_FIX["marked"] = False
                    fixed = True
                    print(f"[ver] 復旧しました: appver={av} resver={rv} mstver={mv}")
                    return retry
            print(f"[ver] 全候補が拒否されました。元のバージョン {base} に戻します")
        except Exception as e:
            print(f"[ver] 復旧処理で例外: {e}")
        finally:
            if not fixed:
                # 復旧できなかった場合だけ元のバージョンへ戻す
                _L.apply_versions(*base, cache=False)
                _sync_versions()
                self._reset_session()
            self._in_ver_recovery = False
        return resp

    def _post_once(self, path, specific_pairs):
        _sync_versions()
        if not self.pub: self.get_public_key()
        common = [
            ("userid", self.userid),("appver",_L.APPVER),("resver",_L.RESVER),("mstver",_L.MSTVER),
            ("hash",self.hash),("os","2"),("lang","ja"),("checkval",self.checkval),
            ("countrycode","JP"),("growthyinfo", growthyinfo()),
        ]
        pairs = [("requestid", str(secrets.randbelow(4_000_000_000)))] + specific_pairs + common
        body = build_form(pairs)
        if not getattr(self, "quiet", False): print(f"\n[POST] {path}\n  plain={body[:240]}...")
        self.last_error = None; self.last_http_status = None; self.last_response = None
        r = None
        for _attempt in range(3):
            try:
                if not self.pub: self.get_public_key()
                enc = aes_gcm_encrypt(body, self.key)
                r = self.s.post(BASE+path, data=enc, headers=self._headers(), timeout=20)
                break
            except requests.exceptions.RequestException as e:
                if isinstance(e, requests.exceptions.ProxyError):
                    self.last_error = "proxy_error"
                elif isinstance(e, requests.exceptions.Timeout):
                    self.last_error = "timeout"
                else:
                    self.last_error = "network_error"
                if not getattr(self, "quiet", False):
                    print(f"  [{self.last_error} {_attempt+1}/3:{str(e)[:100]}]")
                if _attempt < 2:
                    time.sleep(0.5 * (_attempt + 1))
        if r is None:
            print(f"  [{self.last_error or 'network_error'}] {path} を3回試して失敗")
            return None
        self.last_http_status = r.status_code
        if not getattr(self, "quiet", False): print(f"  -> HTTP {r.status_code} ({len(r.content)}B)")
        sid = r.headers.get("X-Session-Id")
        if sid: self.sid = sid
        try:
            dec = aes_gcm_decrypt(r.content, self.key)
            if not getattr(self, "quiet", False): print(f"  resp(decrypted)= {dec[:500]}")
            try:
                parsed = json.loads(dec)
            except Exception:
                parsed = dec
            self.last_response = parsed
            if isinstance(parsed, dict) and parsed.get("retcode") == 0 and not _VER_FIX.get("marked"):
                # 実際に通った組み合わせを1プロセスに1度だけ記録しておく
                _VER_FIX["marked"] = True
                _L.mark_versions_good(_L.APPVER, _L.RESVER, _L.MSTVER)
            return parsed
        except Exception as e:
            print(f"  [復号不可:{e}] raw={r.content[:120]}")
            self.last_error = "decrypt_failed"
            return None

    def login(self, user_token, rankdt=0, trigger=0):
        _sync_versions()
        if not self.pub: self.get_public_key()
        common = [
            ("userid",""),("appver",_L.APPVER),("resver",_L.RESVER),("mstver",_L.MSTVER),
            ("hash",HASH),("os","2"),("lang","ja"),("checkval",""),
            ("countrycode","JP"),("growthyinfo",growthyinfo()),
        ]
        pairs = [("rankdt",rankdt),("accesstoken",user_token),("trigger",trigger)] + common
        body = build_form(pairs)
        print(f"\n[POST] login.nhn\n  plain={body[:160]}...")
        enc = aes_gcm_encrypt(body, self.key)
        r = self.s.post(BASE+"login.nhn", data=enc, headers=self._headers(), timeout=20)
        print(f"  -> HTTP {r.status_code} ({len(r.content)}B)")
        sid=r.headers.get("X-Session-Id")
        if sid: self.sid=sid
        try:
            dec = aes_gcm_decrypt(r.content, self.key)
            print(f"  resp= {dec[:400]}")
            return json.loads(dec)
        except Exception as e:
            print(f"  [復号不可:{e}]"); return None

    def eventid_for_shard(self):
        return "9999" if str(self.userid)[:3] in ("128", "139") else "0"

    def game_retire(self):
        """サーバー上の進行中プレイ(hanging session)を破棄する。retcode 997等のセッションロックを解消。"""
        return self.post("gameRetire.nhn", [])

    def game_start(self, tsumid=860, hearttype=0, probmstver="2", eventid=None):
        if eventid is None:
            eventid = self.eventid_for_shard()
        resp = self.post("gameStart.nhn", [
            ("tsumid",tsumid),("eventid",eventid),("plazaeventid","0"),
            ("hearttype",hearttype),("probmstver",probmstver),("bonusflg","0"),
        ])
        # サーバー上に前回の未終了プレイが残っていると retcode 997 が返るため、gameRetire で破棄して1回再試行
        if isinstance(resp, dict) and resp.get("retcode") == 997:
            try:
                self.game_retire()
            except Exception:
                pass
            resp = self.post("gameStart.nhn", [
                ("tsumid",tsumid),("eventid",eventid),("plazaeventid","0"),
                ("hearttype",hearttype),("probmstver",probmstver),("bonusflg","0"),
            ])
        if isinstance(resp, dict):
            ei = resp.get("eventinfo") or {}
            self._last_eventinfo = ei
            self._last_gametype = ei.get("gametype", 0)
        return resp

    def get_mast(self):
        return self.post("getMast.nhn", [("kind", "3|7|11|14|")])

    def event_candidates(self, mast=None):
        shard_ev = self.eventid_for_shard()
        cand = [shard_ev]
        import time as _t
        if mast is None:
            try: mast = self.get_mast()
            except Exception: mast = None
        now = int(_t.time())
        rows = []
        if isinstance(mast, dict):
            for e in (mast.get("eventmst") or []):
                if not isinstance(e, dict): continue
                try:
                    sd = int(e.get("startdt") or 0); ed = int(e.get("enddt") or 0)
                    if sd <= now <= ed:
                        rows.append((int(e.get("pos") or 99), str(e.get("eventid"))))
                except Exception:
                    continue
        rows.sort()
        for _, eid in rows:
            if eid not in cand:
                cand.append(eid)
        for x in ("0", "9999"):
            if x not in cand:
                cand.append(x)
        return cand

    def get_event(self, eventid, updateflg="1", pos=None):
        # iOS公式クライアント解析 (RequestGetEvent::makeParams):
        # pos < 1 のときは pos パラメータ自体を付与しない。
        # pos を省略するとサーバーが現在挑戦中のアクティブカード情報を自動返却する。
        params = [
            ("eventid", str(eventid)),
            ("updateflg", str(updateflg)),
        ]
        if pos is not None:
            try:
                p_int = int(pos)
                if p_int > 0:
                    params.append(("pos", str(p_int)))
            except (ValueError, TypeError):
                pass
        return self.post("getEvent.nhn", params)

    def get_bingo(self, updateflg="1", pos=None):
        params = [("updateflg", str(updateflg))]
        if pos is not None:
            try:
                p_int = int(pos)
                if p_int > 0:
                    params.append(("pos", str(p_int)))
            except (ValueError, TypeError):
                pass
        return self.post("getBingo.nhn", params)

    def detect_active_event(self, mast=None):
        """ユーザーが現在挑戦中のイベントカード (challenge==1) を探す。
        未完了のものだけを返し、無ければ '0' (通常プレイ) を返す。"""
        candidates = self.event_candidates(mast)
        for eid in candidates:
            if str(eid) in ("0",):
                continue
            try:
                res = self.get_event(eid, updateflg="1")
                if isinstance(res, dict) and res.get("retcode") == 0:
                    ei = res.get("eventinfo") or {}
                    # challenge==1 は現在プレイ対象に設定されているカード
                    if ei.get("challenge") in (1, "1"):
                        comp_val = int(ei.get("compflg") or 0)
                        nowpt = int(ei.get("nowpt") or 0)
                        maxpt = int(ei.get("maxpt") or 0)
                        if comp_val == 0 and (maxpt == 0 or nowpt < maxpt):
                            return str(eid)
            except Exception:
                pass
        return "0"

    def set_profile(self, hometsum=None, trophyid=0, nickname=None):
        """サーバー上のマイツム(hometsum)・トロフィーを変更する。"""
        pairs = []
        if hometsum is not None:
            pairs.append(("hometsum", str(hometsum)))
        if trophyid is not None:
            pairs.append(("trophyid", str(trophyid)))
        if nickname is not None:
            pairs.append(("nickname", str(nickname)))
        return self.post("setProfile.nhn", pairs)

    def open_mission_dialog(self, dialogid="1"):
        did = f"{dialogid}|" if not str(dialogid).endswith("|") else str(dialogid)
        # TsumTsum client sends "types" (not "dialogid")
        return self.post("openMissionDialog.nhn", [("types", did)])

    def receive_mission_reward(self, mtype="1", mid="0", todayflg="0", limitedid="0"):
        # iOS公式クライアント解析 (RequestReceiveMissionReward::makeParams):
        # type=1/2 のときは limitedid は送らない。id も > 0 のときのみ付与する。
        params = [("type", str(mtype))]
        try:
            mid_val = int(mid or 0)
            if mid_val > 0:
                params.append(("id", str(mid_val)))
        except (ValueError, TypeError):
            pass
        if str(mtype) == "3":
            try:
                lid_val = int(limitedid or 0)
                if lid_val > 0:
                    params.append(("limitedid", str(lid_val)))
            except (ValueError, TypeError):
                pass
        params.append(("todayflg", str(todayflg)))
        return self.post("receiveMissionReward.nhn", params)

    def unblock_event_state(self, eventids=None, info=None):
        """イベントカードの演出・クリア報酬保留、デイリーミッション未受取による gameStart 拒否(retcode 997/8)を自動解消する。"""
        if eventids is None:
            try:
                eventids = self.cached_event_candidates()
            except Exception:
                eventids = ["157", "9999"]
        unblocked = False
        for eid in list(eventids):
            if str(eid) == "0":
                continue
            try:
                ev_res = self.get_event(eid, updateflg="1")
                if not isinstance(ev_res, dict) or ev_res.get("retcode") != 0:
                    continue
                ei = ev_res.get("eventinfo") or {}
                did = ei.get("dialogid")
                if did and int(did) > 0:
                    self.open_mission_dialog(did)
                    unblocked = True
                dbit = ei.get("dialogbit")
                if dbit and int(dbit) > 0:
                    self.open_mission_dialog(did or 1)
                    unblocked = True
                # カード完了時(nowpt == maxpt, challenge == 0)のダイアログ消去
                nowpt = int(ei.get("nowpt") or 0)
                maxpt = int(ei.get("maxpt") or 0)
                comp_val = int(ei.get("compflg") or 0)
                if comp_val == 1 or (maxpt > 0 and nowpt >= maxpt):
                    self.open_mission_dialog(did or 1)
                    unblocked = True
                # celllist 内の保留ダイアログ (state == 2) を解消
                for cell in (ei.get("celllist") or []):
                    if not isinstance(cell, dict):
                        continue
                    c_pos = cell.get("pos")
                    c_state = cell.get("state")
                    if c_state == 2 and c_pos is not None:
                        self.open_mission_dialog(c_pos)
                        unblocked = True
            except Exception as e:
                print(f"[unblock] event {eid} 解除処理例外: {e}")

        # Bingo 保留ダイアログも解除（bingoinfo 内に dialogid が明示されている場合のみ）
        if isinstance(info, dict):
            bgi = info.get("bingoinfo") or {}
            bdid = bgi.get("dialogid") if isinstance(bgi, dict) else None
            if bdid and int(bdid) > 0:
                try:
                    self.open_mission_dialog(bdid)
                    unblocked = True
                except Exception:
                    pass

        # デイリーミッション完了済み未受取(state=2)の自動受取
        if isinstance(info, dict):
            try:
                for mi in (info.get("usermissioninfo") or []):
                    if isinstance(mi, dict):
                        mtype = mi.get("type", 2)
                        for m in (mi.get("missionlist") or []):
                            if isinstance(m, dict) and m.get("state") == 2:
                                self.receive_mission_reward(
                                    mtype=mtype,
                                    mid=m.get("id", 0),
                                    todayflg=m.get("todayflg", 1)
                                )
                                unblocked = True
                cb = (info.get("comeback") or {}) if isinstance(info, dict) else {}
                if isinstance(cb, dict) and cb.get("rcvitemtype"):
                    self.post("receiveComebackMissionReward.nhn", [("rcvitemtype", str(cb["rcvitemtype"]))])
                    unblocked = True
            except Exception as _ie:
                print(f"[unblock] missioninfo/comeback 解除処理例外: {_ie}")

        return unblocked

    def game_end(self, playcode, score, coin=61, medal=5, exp=120, vanishcnt=None, boxcnt=None):
        # boxcnt=None のとき gameStart 結果から自動推定 (BOXイベント=gametype1 は boxlist の type を反映)
        if boxcnt is None:
            ei = getattr(self, "_last_eventinfo", None) or {}
            gametype = getattr(self, "_last_gametype", 0)
            if gametype == 1:
                bl = ei.get("boxlist") or []
                parts = [f"{b.get('type', 0)},0|" for b in bl if isinstance(b, dict) and b.get("type") is not None]
                boxcnt = "".join(parts) if parts else "0,0|"
            else:
                boxcnt = ""
        medal_int = int(medal) if medal is not None else 5
        otherresult = json.dumps({"playlog":{
            "coin":{"inval":[],"cnt":{"1":coin},"key":[1],"sum":str(coin),"req":"ne"},
            "medal":{"inval":[],"cnt":{"1":medal_int},"ani":{"1":medal_int},"key":[1],
                     "limit":{"time":"65.042282","ulpm":"70","blem":"0"},"sum":str(medal_int),"req":"ne"}
        }}, separators=(",",":"))
        return self.post("gameEnd.nhn", [
            ("score",score),("coin",coin),("medal",medal_int),("exp",exp),
            ("combo","17"),("chain","8"),("treasurecnt","0"),("fever","1"),
            ("skillcnt","0"),("extendflg","0"),
            ("basescore",score),("basecoin",coin),("basemedal",medal_int),
            ("maxmedal",str(max(87, medal_int))),("baseexp",exp),("playtime","65"),
            ("vanishcnt", vanishcnt or "860,0|9,0|12,0|309,0|4,0|"),
            ("playcode",playcode),
            ("bombcnt","0,0|1,0|2,0|3,0|4,0|5,0|6,0|"),
            ("cheat","0"),("sizecnt","0,89|1,1|2,0|"),
            ("eventpt",""),("boxcnt",boxcnt),("genericval",""),
            ("otherresult",otherresult),
        ])

    def get_info(self):
        return self.post("getInfo.nhn", [])

    def resources(self, info=None):
        data = info if isinstance(info, dict) else self.get_info()
        ui = (data.get("userinfo") if isinstance(data, dict) else None) or {}
        return {k: ui.get(k, 0) for k in ("bcoin","pcoin","bheart","pheart","bruby","pruby")}

    def gacha_compflg(self, gachaid, info=None):
        data = info if isinstance(info, dict) else self.get_info()
        for g in ((data.get("gachainfo") if isinstance(data, dict) else None) or []):
            if g.get("gachaid") == gachaid:
                return g.get("compflg")
        return None

    def gacha_multi(self, gachaid, paytype=1):
        return self.post("gachaResultMulti.nhn", [("gachaid", gachaid), ("paytype", paytype)])

    def gacha_single(self, gachaid, paytype=1):
        return self.post("gachaResult.nhn", [("gachaid", gachaid), ("paytype", paytype)])

    @staticmethod
    def _draw_state(resp, gachaid):
        cf = None
        if isinstance(resp, dict):
            for g in (resp.get("gachainfo") or []):
                if g.get("gachaid") == gachaid:
                    cf = g.get("compflg"); break
        bc = (resp.get("userinfo") or {}).get("bcoin") if isinstance(resp, dict) else None
        return cf, bc

    def complete_box(self, gachaid, min_coin=30_000_000, paytype=1, sleep_s=0.05, max_iter=2000, log=print, on_progress=None):
        info = self.get_info()
        if not isinstance(info, dict):
            return {"status": "network_error", "draws": 0}
        if not info_has_coin_data(info):
            return {"status": "api_error", "draws": 0, "resp": info}
        if self.gacha_compflg(gachaid, info) == 1:
            return {"status": "already_complete", "gachaid": gachaid}
        coin = self.resources(info)["bcoin"]
        if coin < min_coin:
            return {"status": "insufficient_coin", "have": coin, "need": min_coin}
        drawn = 0
        single = False
        self.quiet = True
        need_iter = coin // 30_000 + 100
        max_iter = max(max_iter, min(need_iter, 100_000))
        if log:
            log(f"  完売処理を開始 (所持コイン={coin:,} 想定最大={max_iter:,}ループ)")
        if on_progress:
            try:
                on_progress(0, coin, False)
            except Exception:
                pass
        for _ in range(max_iter):
            if coin < (30_000 if single else 300_000):
                if self.gacha_compflg(gachaid) == 1:
                    return {"status": "complete", "draws": drawn}
                return {"status": "coin_ran_out", "draws": drawn, "coin": coin}
            if not single:
                r = self.gacha_multi(gachaid, paytype)
                if r is None:
                    return {"status": "network_error", "draws": drawn}
                if isinstance(r, dict) and r.get("retcode") == 0:
                    drawn += 10
                    bc = (r.get("userinfo") or {}).get("bcoin")
                    if bc is not None:
                        coin = bc
                    else:
                        coin -= 300_000
                    if log and drawn % 100 == 0:
                        log(f"  ...{drawn}連 (コイン残={coin:,})")
                    if on_progress and (drawn % 100 == 0 or drawn == 10):
                        try:
                            on_progress(drawn, coin, False)
                        except Exception:
                            pass
                    if sleep_s:
                        time.sleep(sleep_s)
                    continue
                else:
                    if is_version_reject(r):
                        return {"status": "api_error", "draws": drawn, "resp": r}
                    # retcode 6/8 (セッション切れ・タイトル戻り) なら再ログインして再試行
                    if isinstance(r, dict) and r.get("retcode") in (6, 8):
                        if self.refresh_session():
                            r_retry = self.gacha_multi(gachaid, paytype)
                            if isinstance(r_retry, dict) and r_retry.get("retcode") == 0:
                                drawn += 10
                                bc = (r_retry.get("userinfo") or {}).get("bcoin")
                                if bc is not None:
                                    coin = bc
                                else:
                                    coin -= 300_000
                                if on_progress and (drawn % 100 == 0 or drawn == 10):
                                    try:
                                        on_progress(drawn, coin, False)
                                    except Exception:
                                        pass
                                if sleep_s:
                                    time.sleep(sleep_s)
                                continue
                    # 10連が失敗した（残り10体未満か、既に完売した）
                    if self.gacha_compflg(gachaid) == 1:
                        return {"status": "complete", "draws": drawn}
                    self._last_multi_fail = r
                    single = True
                    if log:
                        log(f"  10連が終了 → 残りを単発に切替（{drawn}連時点）")
                    if on_progress:
                        try:
                            on_progress(drawn, coin, True)
                        except Exception:
                            pass
                    continue
            else:
                r = self.gacha_single(gachaid, paytype)
                if r is None:
                    return {"status": "network_error", "draws": drawn}
                if isinstance(r, dict) and r.get("retcode") == 0:
                    drawn += 1
                    bc = (r.get("userinfo") or {}).get("bcoin")
                    if bc is not None:
                        coin = bc
                    else:
                        coin -= 30_000
                    if on_progress and (drawn % 10 == 0 or drawn == 1):
                        try:
                            on_progress(drawn, coin, True)
                        except Exception:
                            pass
                    if sleep_s:
                        time.sleep(sleep_s)
                    continue
                else:
                    if isinstance(r, dict) and r.get("retcode") in (6, 8):
                        if self.refresh_session():
                            r_retry = self.gacha_single(gachaid, paytype)
                            if isinstance(r_retry, dict) and r_retry.get("retcode") == 0:
                                drawn += 1
                                bc = (r_retry.get("userinfo") or {}).get("bcoin")
                                if bc is not None:
                                    coin = bc
                                else:
                                    coin -= 30_000
                                if on_progress and (drawn % 10 == 0 or drawn == 1):
                                    try:
                                        on_progress(drawn, coin, True)
                                    except Exception:
                                        pass
                                if sleep_s:
                                    time.sleep(sleep_s)
                                continue
                    if self.gacha_compflg(gachaid) == 1:
                        return {"status": "complete", "draws": drawn}
                    return {"status": "draw_error", "draws": drawn, "resp": str(r)[:200],
                            "multi_resp": str(getattr(self, "_last_multi_fail", None))[:200]}

        if self.gacha_compflg(gachaid) == 1:
            return {"status": "complete", "draws": drawn}
        return {"status": "max_reached", "draws": drawn}

    _WALLET_KEYS = ("bmedal", "bcoin", "bruby", "pruby")

    @classmethod
    def _wallet(cls, resp):
        ui = (resp.get("userinfo") if isinstance(resp, dict) else None) or {}
        return {k: ui.get(k) for k in cls._WALLET_KEYS}

    @staticmethod
    def _retmsg(r):
        if not isinstance(r, dict):
            return ""
        return " ".join(str(r.get(k)) for k in ("retmsg", "message", "errmsg", "error", "msg") if r.get(k))[:160]

    def draw_with_medals(self, gachaid, price=None, paytype=1, max_draws=10_000, sleep_s=0, log=print, on_progress=None):
        """メダルで引くガチャ（プレミアムBOX+）を、メダルが尽きるまで引く。

        実機で確かめたこと（2026-11-28）:
          - paytype=1 でメダル払いになる（サーバーがガチャの種類で通貨を決める）。2・3 以降は入力エラー。
          - メダル不足で引くと retcode 602「メダルの使用に失敗しました。一度タイトルに戻ります。」
          - 未受け取りのツムがあると retcode 37「ツムのプレゼントがあります。一度ホームに戻ります。」

        設計:
          1. 引く前に所持メダルと1回の値段（price）から引ける回数を計算し、足りない回は送らない
             （602 を出すとタイトルに戻され、あとの処理や引き継ぎ発行が失敗するため）。
          2. 10回分以上あれば10連、足りなければ単発。10連が断られたら入り直して単発だけにする。
          3. 引くたびに所持品を見比べる。メダルが減っていない・コインやルビーが減ったら、その場で止める。
          4. 「タイトルに戻ります」系のエラー（6/8/602）のあとは必ず入り直してから戻る。
        戻り値の status:
          done … 引ける分を引き切った ／ no_medal … 1回分に足りない（1回も送っていない）
          present_pending … プレゼント未受け取りで止まった ／ wrong_currency … メダル以外が減った
          stopped … サーバーに断られた ／ max_reached ／ network_error ／ api_error"""
        info = self.get_info()
        if not isinstance(info, dict):
            return {"status": "network_error", "draws": 0}
        w = self._wallet(info)
        if w["bmedal"] is None:
            return {"status": "api_error", "draws": 0, "resp": info}
        start = dict(w)
        medal = int(w["bmedal"] or 0)
        price = int(price) if price else None
        if log:
            log(f"  [BOX+] 開始 gachaid={gachaid} paytype={paytype} 値段={price} wallet={w}")
        if price and medal < price:
            return {"status": "no_medal", "draws": 0, "medal": medal, "price": price}
        drawn = 0
        multi_ok = True
        self.quiet = True

        def _done(status, **extra):
            out = {"status": status, "draws": drawn, "medal": medal, "price": price,
                   "spent_medal": int(start["bmedal"] or 0) - medal}
            out.update(extra)
            return out

        def _report(single):
            if on_progress:
                try:
                    on_progress(drawn, medal, single)
                except Exception:
                    pass

        _report(False)
        while drawn < max_draws:
            if price and medal < price:
                return _done("done")
            use_multi = multi_ok and (medal >= price * 10 if price else False) and (max_draws - drawn) >= 10
            n = 10 if use_multi else 1
            before = dict(w)
            r = self.gacha_multi(gachaid, paytype) if use_multi else self.gacha_single(gachaid, paytype)
            if isinstance(r, dict) and r.get("retcode") in (6, 8) and not use_multi:
                # セッション切れかもしれない。入り直して同じ単発を1回だけやり直す
                if self.refresh_session():
                    r = self.gacha_single(gachaid, paytype)
            if r is None:
                return _done("network_error")
            rc = r.get("retcode") if isinstance(r, dict) else None
            if rc != 0:
                msg = self._retmsg(r)
                if log:
                    log(f"  [BOX+] {'10連' if use_multi else '単発'}が断られた retcode={rc} msg={msg!r} （{drawn}連時点）")
                if is_version_reject(r):
                    return _done("api_error", resp=r)
                if rc == 37 or "プレゼント" in msg:
                    return _done("present_pending", retcode=rc, msg=msg)
                relogged = self.refresh_session() if rc in (6, 8, 602) or "タイトル" in msg else True
                if use_multi and relogged:
                    # 10連が使えない（または値段が違う）ガチャ。単発だけで続ける
                    multi_ok = False
                    fresh = self._wallet(self.get_info())
                    if fresh["bmedal"] is not None:
                        w = fresh
                        medal = int(w["bmedal"] or 0)
                    continue
                if rc == 602 and drawn > 0:
                    return _done("done")          # メダルが尽きた（値段が分からないときの終わり方）
                if rc == 602:
                    return _done("no_medal")
                return _done("stopped", retcode=rc, msg=msg)
            after = self._wallet(r)
            if after["bmedal"] is None:
                after = self._wallet(self.get_info())
            spent = {k: (before[k] or 0) - (after[k] or 0) for k in self._WALLET_KEYS
                     if before[k] is not None and after[k] is not None}
            if spent.get("bcoin", 0) > 0 or spent.get("bruby", 0) > 0 or spent.get("pruby", 0) > 0 \
                    or spent.get("bmedal", 0) <= 0:
                drawn += n
                if after["bmedal"] is not None:
                    medal = int(after["bmedal"])
                if log:
                    log(f"  [BOX+] メダル以外が減った／メダルが減っていない spent={spent} → 中止")
                return _done("wrong_currency", spent=spent)
            if drawn == 0 and log:
                log(f"  [BOX+] 最初の{'10連' if use_multi else '単発'}で減ったメダル={spent.get('bmedal')}（想定 {price * n if price else '不明'}）")
            if not price:
                price = max(1, int(spent["bmedal"]) // n)   # 値段が取れていなければ、実際に減った量から決める
            drawn += n
            w = after
            medal = int(after["bmedal"])
            if log and (drawn % 100 == 0):
                log(f"  [BOX+] ...{drawn}連 (メダル残={medal:,})")
            if drawn % 50 == 0 or drawn <= 10 or not use_multi:
                _report(not use_multi)
            if sleep_s:
                time.sleep(sleep_s)
        return _done("max_reached")

    def draw_pickup(self, gachaid, price=None, max_draws=15, paytype=1, sleep_s=0, log=print, on_progress=None):
        """ピックアップガチャ（コインで引く・回数の上限あり）を、上限まで単発で引く。
        - 上限（max_draws）を超えては送らない。足りないコインの回も送らない。
        - 引くたびに所持品を見比べ、コインが減っていない／メダル・ルビーが減ったら止める。
        - サーバーに断られたらそこで終わり（すでに上限まで引いてあるアカウントは、1回目で断られる）。
        戻り値の status: done（上限まで引いた）／limit（サーバーがもう引かせない）／no_coin／
          present_pending／wrong_currency／stopped／network_error／api_error"""
        info = self.get_info()
        if not isinstance(info, dict):
            return {"status": "network_error", "draws": 0}
        w = self._wallet(info)
        if w["bcoin"] is None:
            return {"status": "api_error", "draws": 0, "resp": info}
        start_coin = int(w["bcoin"] or 0)
        coin = start_coin
        price = int(price) if price else None
        drawn = 0
        self.quiet = True
        if log:
            log(f"  [pickup] 開始 gachaid={gachaid} 値段={price} 上限={max_draws} wallet={w}")

        def _done(status, **extra):
            out = {"status": status, "draws": drawn, "coin": coin, "price": price, "spent_coin": start_coin - coin}
            out.update(extra)
            return out

        while drawn < max_draws:
            if price and coin < price:
                return _done("no_coin")
            before = dict(w)
            r = self.gacha_single(gachaid, paytype)
            if isinstance(r, dict) and r.get("retcode") == 6 and self.refresh_session():
                r = self.gacha_single(gachaid, paytype)
            if r is None:
                return _done("network_error")
            rc = r.get("retcode") if isinstance(r, dict) else None
            if rc != 0:
                msg = self._retmsg(r)
                if log:
                    log(f"  [pickup] 断られた retcode={rc} msg={msg!r} （{drawn}回時点）")
                if is_version_reject(r):
                    return _done("api_error", resp=r)
                if rc in (6, 8, 602) or "タイトル" in msg:
                    self.refresh_session()
                if rc == 37 or "プレゼント" in msg:
                    return _done("present_pending", retcode=rc, msg=msg)
                if "コイン" in msg and "不足" in msg:
                    return _done("no_coin", retcode=rc, msg=msg)
                # それ以外の拒否は「もう引けない（上限）」として扱い、理由を一緒に返す
                return _done("limit", retcode=rc, msg=msg)
            after = self._wallet(r)
            if after["bcoin"] is None:
                after = self._wallet(self.get_info())
            spent = {k: (before[k] or 0) - (after[k] or 0) for k in self._WALLET_KEYS
                     if before[k] is not None and after[k] is not None}
            drawn += 1
            if after["bcoin"] is not None:
                coin = int(after["bcoin"])
            if spent.get("bmedal", 0) > 0 or spent.get("bruby", 0) > 0 or spent.get("pruby", 0) > 0 \
                    or spent.get("bcoin", 0) <= 0:
                if log:
                    log(f"  [pickup] コイン以外が減った／コインが減っていない spent={spent} → 中止")
                return _done("wrong_currency", spent=spent)
            if not price:
                price = int(spent["bcoin"])
            w = after
            if on_progress:
                try:
                    on_progress(drawn, coin)
                except Exception:
                    pass
            if sleep_s:
                time.sleep(sleep_s)
        return _done("done")

    def fill_coins(self, cap=200_000_000):
        info = self.get_info()
        cur = (info.get("userinfo") or {}).get("bcoin") or 0
        if cur >= cap:
            return cur
        add = cap - cur
        try:
            self.unblock_event_state(info=info)
        except Exception:
            pass
        play_tid = safe_playable_tsum(info)
        cands = [play_tid]
        if play_tid != 1:
            cands.append(1)
        for tid in cands:
            pc, r1, won_tid = self.game_start_any(tid, info=info, return_tid=True)
            if pc:
                r2 = self.game_end(pc, score=95000, coin=add, medal=5, exp=120,
                                   vanishcnt=f"{won_tid},0|9,0|12,0|309,0|4,0|")
                if isinstance(r2, dict) and r2.get("retcode") == 0:
                    return (r2.get("userinfo") or {}).get("bcoin", cur + add)
                try:
                    self.game_retire()
                except Exception:
                    pass
        return cur

    def release_tsum_lv(self, tsumid):
        return self.post("releaseTsumLv.nhn", [("tsumid", tsumid)])

    def cached_event_candidates(self, ttl=900):
        """開催中イベントIDの候補。getMast は重いのでプロセス内でキャッシュする。"""
        now = time.time()
        c = Forge._EV_CACHE
        if c["cands"] and (now - c["ts"] < ttl):
            return c["cands"]
        try:
            cands = self.event_candidates()
        except Exception as e:
            print(f"[gs] event_candidates失敗→fallback: {e}")
            cands = None
        if cands:
            c["cands"] = cands
            c["ts"] = now
        return cands or ["9999", "0"]

    def _hearttype_candidates(self, info=None):
        r = self.resources(info) if isinstance(info, dict) else {}
        hts = []
        if (r.get("bheart") or 0) > 0:
            hts.append(0)
        if (r.get("pheart") or 0) > 0:
            hts.append(1)
        for v in (0, 1):
            if v not in hts:
                hts.append(v)
        return hts

    def game_start_any(self, tsumid=None, info=None, max_try=12, return_tid=False):
        """eventid / hearttype を総当たりして playcode を取る。
        決め打ちだと retcode 8(パラメータエラー) や 23(ハート残数不一致) で即失敗するため。
        成功した組み合わせは覚えて、次回から最初に試す。"""
        try:
            self.unblock_event_state(info=info)
        except Exception:
            pass
        if info is None:
            info = getattr(self, "_last_info", None)

        # ペアツム・未所持ツム除外: safe_playable_tsum で確実に通る通常ツムへ
        if tsumid is None or is_pair_tsum(tsumid):
            tsumid = safe_playable_tsum(info, prefer_tid=tsumid)

        # サーバー上のマイツム(hometsum)とプレイ対象ツムが不一致なら同期する
        cur_home = self.current_mytsum(info)
        if cur_home != tsumid and tsumid is not None:
            try:
                sp_res = self.set_profile(hometsum=tsumid)
                if isinstance(sp_res, dict) and sp_res.get("retcode") == 0:
                    self._cur_hometsum = tsumid
            except Exception:
                pass

        active_ev = self.detect_active_event()
        evs = []
        if active_ev and active_ev != "0":
            evs.append(active_ev)
        for e in ("9999", "0", "157"):
            if e not in evs:
                evs.append(e)
        try:
            for e in self.cached_event_candidates():
                if e not in evs:
                    evs.append(e)
        except Exception:
            pass

        combos = []
        won = getattr(self, "_gs_combo", None)
        if won and won not in combos:
            combos.append(won)
        for ht in self._hearttype_candidates(info):
            for ev in evs:
                if (ev, ht) not in combos:
                    combos.append((ev, ht))

        attempts = []
        last = None
        for ev, ht in combos[:max_try]:
            r1 = self.game_start(tsumid=tsumid, hearttype=ht, eventid=ev)
            last = r1
            rc = (r1.get("retcode"), r1.get("retsubcode")) if isinstance(r1, dict) else None
            attempts.append((ev, ht, rc))
            pc = (r1.get("userinfo") or {}).get("playcode") or r1.get("playcode") if isinstance(r1, dict) else None
            if pc:
                self._gs_combo = (ev, ht)
                self.last_gs_attempts = attempts
                # BOXイベント(gametype=1)かどうかを記録。game_end の boxcnt 自動設定に使う
                ei = (r1.get("eventinfo") or {}) if isinstance(r1, dict) else {}
                self._last_eventinfo = ei
                self._last_gametype = ei.get("gametype", 0)
                if return_tid:
                    return pc, r1, tsumid
                return pc, r1
            if isinstance(r1, dict) and r1.get("retcode") in (1, 6, 8):
                # サーバー側で「タイトルに戻ります」(1, 8)やセッション切れ(6)が発生した場合は
                # セッションを再取得して回復させてから次の候補を試す
                self.refresh_session()
            elif isinstance(r1, dict) and r1.get("retcode") == 997:
                self.game_retire()
            elif isinstance(r1, dict) and r1.get("retcode") == 23:
                # 「ハート残数が一致しません」→ 手元のハート状態が古い。取り直して次の候補へ。
                fresh = self.get_info()
                if info_has_heart_data(fresh):
                    self._last_info = fresh
                    info = fresh
            time.sleep(0.1)
        self._gs_combo = None
        self.last_gs_attempts = attempts
        if return_tid:
            return None, last, tsumid
        return None, last

    def current_mytsum(self, info=None):
        if info is None:
            info = getattr(self, "_last_info", None)
        if isinstance(info, dict):
            for t in (info.get("tsuminfo") or []):
                if isinstance(t, dict) and t.get("setflg") in (1, "1", True):
                    return t.get("tsumid")
        return None

    def level_tsum_once(self, tsumid, vanish=200000, score=10_000_000, info=None):
        if info is None:
            info = getattr(self, "_last_info", None)
        play_tid = safe_playable_tsum(info, prefer_tid=tsumid)
        cur_home = self.current_mytsum(info)
        if cur_home != play_tid:
            try:
                sp_res = self.set_profile(hometsum=play_tid)
                if isinstance(sp_res, dict) and sp_res.get("retcode") == 0:
                    self._cur_hometsum = play_tid
            except Exception:
                pass
        pc, r1, won_tid = self.game_start_any(play_tid, info=info, return_tid=True)
        if not pc:
            self.refresh_session()
            if play_tid != 1:
                try:
                    self.set_profile(hometsum=1)
                except Exception:
                    pass
                pc, r1, won_tid = self.game_start_any(1, info=self.get_info(), return_tid=True)
            if not pc:
                self.last_gs_response = r1
                return None
        # EXPは vanishcnt の先頭ツム(tsumid)に全て付与される
        # 他のツムは絶対に tsumid と重複せず、また互いにも重複しないように一意化
        seen = {tsumid}
        board_extra = []
        for t in [won_tid, 1, 2, 4, 9, 12, 309, 860]:
            if t and t not in seen:
                seen.add(t)
                board_extra.append(t)
                if len(board_extra) >= 4:
                    break
        vanishcnt = f"{tsumid},{vanish}|" + "".join(f"{t},0|" for t in board_extra)
        return self.game_end(pc, score, vanishcnt=vanishcnt)


    def _tsum_lv(self, resp, tsumid):
        for t in (resp.get("tsuminfo") or []) if isinstance(resp, dict) else []:
            if t.get("tsumid") == tsumid:
                return t.get("lv"), t.get("limitlv")
        return None, None

    def hearts(self, info=None):
        r = self.resources(info)
        return (r.get("bheart") or 0) + (r.get("pheart") or 0)

    def max_tsum_level(self, tsumid, target=50, min_heart=10, sleep_s=0.6, max_iter=40, log=print, auto_fill_coins=False):
        info0 = self.get_info()
        if not isinstance(info0, dict):
            return {"status": "network_error"}
        if not info_has_heart_data(info0):
            # getInfo がエラー応答(retcode!=0 / userinfo なし)。ハート0と誤読しない。
            return {"status": "api_error", "resp": info0}
        
        # 既に target レベル以上の場合は即完了
        lv0, _ = self._tsum_lv(info0, tsumid)
        if lv0 is not None and lv0 >= target:
            return {"status": "done", "lv": lv0}

        cur_lv = lv0 if lv0 is not None else 1
        needed_games = max(1, min(10, -(-(target - cur_lv) // 5)))
        req_heart = min(min_heart, needed_games)
        h0 = self.hearts(info0)
        if h0 < req_heart:
            return {"status": "insufficient_heart", "have": h0, "need": req_heart}
        self._last_info = info0
        orig_home = self.current_mytsum(info0)

        # コイン自動補充が有効なら、上限解放費用（最大約6.3万）を事前確認
        if auto_fill_coins:
            try:
                cur_coin = (info0.get("userinfo") or {}).get("bcoin", 0)
                if cur_coin < 100_000:
                    self.fill_coins(cap=500_000)
            except Exception:
                pass

        try:
            # プレイ前に上限解放を試行（上限に達しているツムがEXP受取不可になるのを防止）
            self.release_tsum_lv(tsumid)
            last_lv = lv0
            stuck_count = 0
            for _ in range(max_iter):
                r = self.level_tsum_once(tsumid, info=self._last_info)
                if not isinstance(r, dict) or r.get("retcode") != 0:
                    _now = self.get_info()
                    if info_has_heart_data(_now):
                        self._last_info = _now
                        if self.hearts(_now) < 1:
                            return {"status": "heart_ran_out", "lv": last_lv}
                    time.sleep(1.5)
                    r = self.level_tsum_once(tsumid, info=self._last_info)
                    if not isinstance(r, dict) or r.get("retcode") != 0:
                        return {"status": "level_error", "lv": last_lv,
                                "resp": str(r if r is not None else self.last_gs_response)[:200],
                                "attempts": list(self.last_gs_attempts)}
                lv, _cap = self._tsum_lv(r, tsumid)
                if log: log(f"  tsum{tsumid}: lv={lv}")
                if lv is not None and lv >= target:
                    return {"status": "done", "lv": lv}
                ru = self.release_tsum_lv(tsumid)
                if isinstance(ru, dict) and ru.get("retcode") not in (0, None):
                    if auto_fill_coins:
                        try:
                            self.fill_coins(cap=500_000)
                            ru = self.release_tsum_lv(tsumid)
                        except Exception:
                            pass
                    if isinstance(ru, dict) and ru.get("retcode") not in (0, None):
                        return {"status": "release_error", "lv": lv, "resp": str(ru)[:200]}
                if lv == last_lv:
                    stuck_count += 1
                    if stuck_count >= 2:
                        if auto_fill_coins:
                            try:
                                self.fill_coins(cap=500_000)
                            except Exception:
                                pass
                        self.release_tsum_lv(tsumid)
                        r_extra = self.level_tsum_once(tsumid, info=self._last_info)
                        lv_extra, _ = self._tsum_lv(r_extra, tsumid)
                        if lv_extra is not None and lv_extra > (lv or 0):
                            lv = lv_extra
                            last_lv = lv
                            stuck_count = 0
                            if lv >= target:
                                return {"status": "done", "lv": lv}
                            continue
                        return {"status": "stuck", "lv": lv, "note": "上限開放できず(コイン不足?)"}
                else:
                    stuck_count = 0
                last_lv = lv
                time.sleep(sleep_s)
            return {"status": "max_iter", "lv": last_lv}
        finally:
            if orig_home:
                try:
                    self.set_profile(hometsum=orig_home)
                except Exception:
                    pass


def load_session(args):
    if args.userid and args.hash and args.checkval:
        return args.userid, args.hash, args.checkval

    if not getattr(args, "no_headless", False):
        try:
            import tsum_login
            print("[session] 通信ログイン(ヘッドレス)を試行中...")
            s = tsum_login.headless_session()
            if s and s.get("userid") and s.get("hash") and s.get("checkval"):
                print(f"[session] ★通信ログイン成功 userid={s['userid']} checkval={s['checkval']}")
                return (args.userid or s["userid"], args.hash or s["hash"], args.checkval or s["checkval"])
            print("[session] 通信ログイン不可(トークン失効?)→ エミュメモリ検出にフォールバック")
        except Exception as e:
            print(f"[session] 通信ログイン失敗: {e} → エミュメモリ検出にフォールバック")

    try:
        import tsum_session
        print("[session] ゲームメモリから自動検出中...")
        s = tsum_session.detect()
        print(f"[session] userid={s['userid']} checkval={s['checkval']} hash={s['hash'][:16]}...")
        if not all(s.values()):
            print("[session] 一部未検出。ゲームをホーム(ログイン後)まで進めてください。")
        return (args.userid or s["userid"], args.hash or s["hash"], args.checkval or s["checkval"])
    except Exception as e:
        print(f"[session] 自動検出失敗: {e}")
        return args.userid or USERID, args.hash or HASH, args.checkval or CHECKVAL


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ツムツム スコア/コイン/EXP 偽装 (session自動検出)")
    ap.add_argument("--score", type=int, default=None, help="記録するスコア(basescore)")
    ap.add_argument("--coin",  type=int, default=None, help="獲得コイン(basecoin・残高に加算)")
    ap.add_argument("--exp",   type=int, default=None, help="獲得EXP(baseexp・レベルに加算)")
    ap.add_argument("--medal", type=int, default=5,    help="獲得メダル(basemedal)")
    ap.add_argument("--tsumid",type=int, default=860,  help="使用ツムID")
    ap.add_argument("--userid", default=None, help="手動指定(省略時 自動検出)")
    ap.add_argument("--hash",   default=None, help="手動指定(省略時 自動検出)")
    ap.add_argument("--checkval", default=None, help="手動指定(省略時 自動検出)")
    ap.add_argument("--no-headless", action="store_true", help="通信ログインを使わずエミュメモリ検出にする")
    ap.add_argument("score_pos", nargs="?", type=int, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    score = args.score if args.score is not None else args.score_pos
    if score is None and args.coin is None and args.exp is None:
        score = 1000000

    uid, hsh, ckv = load_session(args)
    if not (uid and hsh and ckv):
        print("[中断] session定数が揃いません。ゲームにログインしてホームまで進めてください。")
        raise SystemExit(1)

    f = Forge(userid=uid, hashv=hsh, checkval=ckv)
    f.get_public_key()

    print("\n=== gameStart.nhn ===")
    r1 = f.game_start(tsumid=args.tsumid)
    playcode = None
    if isinstance(r1, dict):
        print("  retcode=", r1.get("retcode"))
        playcode = (r1.get("userinfo") or {}).get("playcode") or r1.get("playcode")
        bcoin = (r1.get("userinfo") or {}).get("bcoin")
        print(f"  playcode={playcode}  現在bcoin={bcoin}")
    if not playcode:
        print("[中断] playcode取得失敗(checkval失効? ゲーム再ログイン→再検出を)。")
        raise SystemExit(1)

    print("\n=== gameEnd.nhn (偽装送信) ===")
    r2 = f.game_end(playcode,
                    score=score if score is not None else 95000,
                    coin=args.coin if args.coin is not None else 61,
                    medal=args.medal,
                    exp=args.exp if args.exp is not None else 120)
    if isinstance(r2, dict):
        print("  retcode=", r2.get("retcode"), "retmsg=", r2.get("retmsg"))
        ui = r2.get("userinfo") or {}
        print(f"  → 反映後: bcoin={ui.get('bcoin')} lv={ui.get('lv')} bmedal={ui.get('bmedal')} earnedexp={ui.get('earnedexp')}")
        rl = (r2.get("rankinginfo") or {}).get("rankinglist") or []
        for e in rl:
            if e.get("selfflg") == 1:
                print(f"  ★自分の記録: name={e.get('name')} score={e.get('score')} order={e.get('order')}")
        open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gameend_response.json"),"w",encoding="utf-8").write(
            json.dumps(r2, ensure_ascii=False, indent=2))
