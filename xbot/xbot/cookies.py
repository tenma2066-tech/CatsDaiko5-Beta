"""iPhone でログインした X の「ログイン済みの印」（Cookie）を読み取る。"""
import json
import re
import time

X_DOMAINS = (".x.com", "x.com", ".twitter.com", "twitter.com")


def parse(raw_json: str, auth_token: str, ct0: str):
    """Cookie-Editor の書き出し（JSON）か、auth_token / ct0 の値から、Playwright 用の Cookie を作る。"""
    out = {}
    raw_json = (raw_json or "").strip()
    if raw_json:
        try:
            data = json.loads(raw_json)
        except ValueError:
            raise ValueError("貼り付けた内容を読み取れませんでした。Cookie-Editor の「Export → JSON」をそのまま貼ってください。")
        if isinstance(data, dict):
            data = data.get("cookies") or [data]
        for c in data if isinstance(data, list) else []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            dom = str(c.get("domain") or ".x.com")
            if not any(dom.endswith(d.lstrip(".")) for d in X_DOMAINS) or "twitter.com" in dom:
                continue
            out[c["name"]] = _cookie(c["name"], str(c.get("value", "")), c.get("httpOnly", False))
    auth_token = (auth_token or "").strip().strip('"')
    ct0 = (ct0 or "").strip().strip('"')
    if auth_token:
        out["auth_token"] = _cookie("auth_token", auth_token, True)
    if ct0:
        out["ct0"] = _cookie("ct0", ct0, False)
    if "auth_token" not in out:
        raise ValueError("auth_token が見つかりません。x.com にログインした状態で、Cookie-Editor から取り出してください。")
    if not re.fullmatch(r"[0-9a-fA-F]{20,80}", out["auth_token"]["value"]):
        raise ValueError("auth_token の形が正しくありません（英数字40文字ほど）。値だけを貼ってください。")
    if "ct0" in out and not re.fullmatch(r"[0-9a-zA-Z]{20,200}", out["ct0"]["value"]):
        raise ValueError("ct0 の形が正しくありません。値だけを貼ってください。")
    return list(out.values())


def _cookie(name, value, http_only):
    return {"name": name, "value": value, "domain": ".x.com", "path": "/", "secure": True,
            "httpOnly": bool(http_only), "sameSite": "None", "expires": time.time() + 365 * 86400}
