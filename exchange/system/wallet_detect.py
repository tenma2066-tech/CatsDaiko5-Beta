#!/usr/bin/env python3
"""
wallet_detect.py -- 送金リンクの残高種別判定 (マネー / マネラ)

PayPay の実データでは prepaid = PayPayマネーライト (= マネラ)。
「PREPAID をマネー扱いする」実装をよく見かけるが、それだとマネラが素通りする:
    getBalanceInfo: emoneyBalanceInfo → マネー / prepaidBalanceInfo → マネーライト
    subWalletSplit: senderEmoneyAmount → マネー / senderPrepaidAmount → マネラ

受取(accept)より前の getP2PLinkInfo のレスポンスに subWalletSplit が入るので、
「受け取る前に」マネラを弾ける = 弾いた時は相手の金に一切触れずに済む。
ただし PayPay 側が split を返さないケースがあるため unknown の扱いも必ず設定する。

戻り値の wallet_type:
    money_only  マネーのみ
    light_only  マネラのみ
    mixed       混在
    unknown     判定不能 (splitが無い)
"""
from __future__ import annotations

import json
import sys

for _s in (sys.stdout, sys.stderr):  # cp932コンソール対策
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

LABELS = {
    "money_only": "マネー",
    "light_only": "マネラ",
    "mixed": "マネー＆マネラ混在",
    "unknown": "不明",
}

EMOJI = {
    "money_only": "💴",
    "light_only": "🪙",
    "mixed": "🔀",
    "unknown": "❔",
}

# 実データは subWalletSplit。他は名称違いの保険 (PayPay側の改名に耐えるため)
_SPLIT_KEYS = (
    "subWalletSplit", "walletSplit", "subWalletSplitInfo",
    "paymentMethodSplit", "amountBreakdown", "paymentBreakdown",
    "walletBreakdown", "paymentMethodBreakdown",
)

# マネラ(マネーライト)を示す語。prepaid を最優先で判定する
_LIGHT_WORDS = ("prepaid", "lite", "light")
# マネー(出金可能な残高)を示す語。
# "wallet" "balance" のような汎用語は入れない。"senderWallet" のような
# ただの入れ物キーをマネーと解釈すると、中身が PREPAID でもマネー扱いになる。
_MONEY_WORDS = ("emoney", "money", "cash")

DEFAULT_POLICY = {
    "money_only": {"mode": "allow", "extra_fee_percent": 0.0},
    "mixed":      {"mode": "allow", "extra_fee_percent": 5.0},
    "light_only": {"mode": "deny",  "extra_fee_percent": 20.0},
    # 判定できない = マネラを見逃している可能性がある。黙って通すと自動送金まで
    # 走ってしまうので既定は保留 (管理者が /残高種別設定 で allow に変えられる)
    "unknown":    {"mode": "hold",  "extra_fee_percent": 0.0},
}


def _to_int(v) -> int:
    try:
        return abs(int(v or 0))
    except (TypeError, ValueError):
        try:
            return abs(int(float(v)))
        except (TypeError, ValueError):
            return 0


def _is_receiver(text) -> bool:
    """受信側(自分)の残高を指すキー/種別か"""
    return "receiver" in str(text or "").lower()


def _kind_of(text: str) -> str | None:
    """キー名 / walletType 文字列から 'light' か 'money' を判定"""
    t = str(text or "").lower()
    if not t:
        return None
    if any(w in t for w in _LIGHT_WORDS):   # prepaid を money より先に見る
        return "light"
    if any(w in t for w in _MONEY_WORDS):
        return "money"
    return None


def _find_split(obj, depth: int = 0):
    """subWalletSplit 相当のキーを再帰探索"""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for k in _SPLIT_KEYS:
            if obj.get(k):
                return obj[k]
        for v in obj.values():
            if isinstance(v, (dict, list)):
                found = _find_split(v, depth + 1)
                if found:
                    return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_split(v, depth + 1)
            if found:
                return found
    return None


def parse_split(split) -> tuple[int, int]:
    """split構造 → (マネー額, マネラ額)

    実形式:
      {"senderEmoneyAmount": 500, "senderPrepaidAmount": 1500,
       "receiverEmoneyAmount": 0, "receiverPrepaidAmount": 0}
      → receiver* は受信側(自分)の残高状態であって送金内訳ではないので必ず除外する。
      リスト形式 [{"walletType": "PREPAID", "amount": 1500}, ...] にも対応。
    """
    money = light = 0

    if isinstance(split, dict):
        for k, v in split.items():
            kl = str(k).lower()
            if "receiver" in kl:   # 自分側の残高 → 送金内訳ではない
                continue
            if isinstance(v, dict):
                # {"emoney": {"amount": 500}} / {"senderWallet": {"walletType": "PREPAID", ...}}
                amt = _to_int(v.get("amount") or v.get("value") or v.get("subAmount"))
                # 明示された種別を必ず優先する。入れ物のキー名 (senderWallet 等) を
                # 先に見ると、中身が PREPAID でもマネー扱いになって取りこぼす。
                inner = (v.get("walletType") or v.get("subWalletType")
                         or v.get("type") or v.get("method"))
                if _is_receiver(inner):
                    continue
                kind = _kind_of(inner) or _kind_of(k)
            elif isinstance(v, list):
                m2, l2 = parse_split(v)
                money += m2
                light += l2
                continue
            else:
                amt = _to_int(v)
                kind = _kind_of(k)
            if not amt or not kind:
                continue
            if kind == "light":
                light += amt
            else:
                money += amt
        return money, light

    if isinstance(split, list):
        for e in split:
            if not isinstance(e, dict):
                continue
            wt = ""
            for k in ("walletType", "subWalletType", "type", "name", "key", "method"):
                if e.get(k):
                    wt = str(e.get(k))
                    break
            # リスト形式でも receiver 系 (自分側の残高) は必ず捨てる。
            # ここを素通しすると受信側残高が送金内訳として計上され、判定が反転する。
            if _is_receiver(wt) or any(_is_receiver(k) for k in e.keys()):
                continue
            amt = 0
            for k in ("amount", "value", "totalAmount", "subAmount"):
                if e.get(k) is not None:
                    amt = _to_int(e.get(k))
                    break
            kind = _kind_of(wt)
            if not amt or not kind:
                continue
            if kind == "light":
                light += amt
            else:
                money += amt
        return money, light

    return 0, 0


def detect(source) -> tuple[str, int, int]:
    """link_info (または生レスポンス) → (wallet_type, マネー額, マネラ額)"""
    if not source:
        return "unknown", 0, 0

    split = None
    if isinstance(source, dict):
        for k in _SPLIT_KEYS:
            if source.get(k):
                split = source[k]
                break
    if split is None:
        split = _find_split(source)

    money, light = parse_split(split) if split is not None else (0, 0)

    # split が無い/読めない時は walletType 等の単一指定を見る
    if money == 0 and light == 0 and isinstance(source, dict):
        amt = _to_int(source.get("amount") or source.get("price") or source.get("subAmount"))
        for key in ("walletType", "subWalletType", "paymentMethod", "paymentType"):
            kind = _kind_of(source.get(key))
            if not kind:
                continue
            marker = amt if amt > 0 else 1   # 額が無くても種別だけは確定させる
            if kind == "light":
                light = marker
            else:
                money = marker
            break

    if money > 0 and light > 0:
        return "mixed", money, light
    if money > 0:
        return "money_only", money, 0
    if light > 0:
        return "light_only", 0, light

    try:
        print("[wallet_detect] 判定不能。source dump:\n"
              + json.dumps(source, ensure_ascii=False, indent=1, default=str)[:1200])
    except Exception:
        pass
    return "unknown", 0, 0


def light_ratio(wallet_type: str, money: int, light: int) -> float:
    """マネラ比率 0.0〜1.0"""
    if wallet_type == "light_only":
        return 1.0
    if wallet_type == "money_only":
        return 0.0
    total = money + light
    return (light / total) if total > 0 else 0.0


def effective_type(cfg: dict, wallet_type: str, money: int, light: int) -> str:
    """マネラ比率が閾値を超える混在は light_only と同じ扱いにする。

    そのままだと ¥10,000 のうち ¥1 だけマネーを混ぜるだけで light_only の
    拒否ポリシーを回避でき、99.99%マネラの入金が +5% で通ってしまう。
    """
    if wallet_type != "mixed":
        return wallet_type
    try:
        limit = float(cfg.get("mixed_max_light_ratio", 0.5))
    except (TypeError, ValueError):
        limit = 0.5
    if limit <= 0 or limit >= 1:
        return wallet_type
    if light_ratio(wallet_type, money, light) > limit:
        return "light_only"
    return wallet_type


def policy_for(cfg: dict, wallet_type: str) -> dict:
    pol = (cfg.get("wallet_policy") or {}).get(wallet_type)
    base = DEFAULT_POLICY.get(wallet_type, DEFAULT_POLICY["unknown"])
    if not isinstance(pol, dict):
        return dict(base)
    out = dict(base)
    out.update(pol)
    if out.get("mode") not in ("allow", "deny", "hold"):
        out["mode"] = base["mode"]
    return out


def extra_fee_percent(cfg: dict, wallet_type: str, money: int, light: int) -> float:
    """マネラ分に対する追加手数料(%)。

    mixed_fee_mode:
      "flat"     (既定) … 種別が該当したら設定値をそのまま加算
      "prorated"        … マネラ比率に応じて按分。マネー9割の入金に満額課金しない

    既定値は store.DEFAULT_CONFIG と揃えること。ここだけ "prorated" にしていると、
    cfg を素の dict で渡した経路だけ課金額が変わって説明がつかなくなる。
    """
    pol = policy_for(cfg, wallet_type)
    extra = float(pol.get("extra_fee_percent", 0.0) or 0.0)
    if extra <= 0:
        return 0.0
    if wallet_type == "mixed" and (cfg.get("mixed_fee_mode", "flat") == "prorated"):
        return round(extra * light_ratio(wallet_type, money, light), 3)
    return extra


def summary_line(wallet_type: str, money: int, light: int) -> str:
    """埋め込み表示用の1行"""
    label = f"{EMOJI.get(wallet_type, '❔')} {LABELS.get(wallet_type, wallet_type)}"
    if wallet_type == "mixed":
        return f"{label} (マネー ¥{money:,} / マネラ ¥{light:,} = {light_ratio(wallet_type, money, light) * 100:.0f}%マネラ)"
    if wallet_type == "money_only":
        return f"{label} (¥{money:,})"
    if wallet_type == "light_only":
        return f"{label} (¥{light:,})"
    return f"{label} (内訳が取得できず)"


if __name__ == "__main__":
    samples = [
        ("実データ(混在)", {"amount": 2000, "subWalletSplit": {
            "senderEmoneyAmount": 500, "senderPrepaidAmount": 1500,
            "receiverEmoneyAmount": 99999, "receiverPrepaidAmount": 88888}}),
        ("マネーのみ", {"amount": 1000, "subWalletSplit": {
            "senderEmoneyAmount": 1000, "senderPrepaidAmount": 0}}),
        ("マネラのみ", {"amount": 3000, "subWalletSplit": {
            "senderEmoneyAmount": 0, "senderPrepaidAmount": 3000}}),
        ("リスト形式", {"amount": 800, "subWalletSplit": [
            {"walletType": "PREPAID", "amount": 800}]}),
        ("walletTypeのみ", {"amount": 1500, "walletType": "EMONEY"}),
        ("入れ子", {"payload": {"pendingP2PInfo": {"amount": 1200,
            "subWalletSplit": {"senderPrepaidAmount": 1200}}}}),
        ("split無し", {"amount": 500}),
        # --- レビューで見つかった取りこぼしパターン (すべてマネラ側に落ちること) ---
        ("入れ物キー+PREPAID", {"amount": 2000, "subWalletSplit": {
            "senderWallet": {"walletType": "PREPAID", "amount": 2000}}}),
        ("balanceキー+PREPAID", {"amount": 2000, "subWalletSplit": {
            "senderBalanceDetail": {"type": "PREPAID", "amount": 2000}}}),
        ("list+receiver混入", {"amount": 2000, "subWalletSplit": [
            {"key": "receiverEmoneyAmount", "amount": 120000},
            {"key": "senderPrepaidAmount", "amount": 2000}]}),
        ("list+RECEIVER種別", {"amount": 2000, "subWalletSplit": [
            {"walletType": "RECEIVER_EMONEY", "amount": 50000},
            {"walletType": "PREPAID", "amount": 2000}]}),
        ("ネスト内emoney", {"amount": 800, "subWalletSplit": {
            "emoneyDetail": {"amount": 500}, "prepaidDetail": {"amount": 300}}}),
    ]
    cfg = {"wallet_policy": DEFAULT_POLICY, "mixed_fee_mode": "prorated"}
    for name, s in samples:
        t, m, l = detect(s)
        print(f"{name:14} → {t:10} money={m:6} light={l:6} "
              f"extra={extra_fee_percent(cfg, t, m, l):5.2f}% "
              f"mode={policy_for(cfg, t)['mode']:5} | {summary_line(t, m, l)}")
