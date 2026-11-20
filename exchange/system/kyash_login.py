#!/usr/bin/env python3
"""
kyash_login.py -- BOT側のKyashにターミナルからログインする (パスワードが画面に出ない)

    py system\\kyash_login.py            # ログイン
    py system\\kyash_login.py status     # 今のアカウント・トークン残り・残高
    py system\\kyash_login.py refresh    # トークンを手動更新
    py system\\kyash_login.py logout     # トークンを削除

Discord の /kyashログイン と同じ手順 (kyash_bridge.login_step1/2) を呼ぶ。
"""
from __future__ import annotations

import getpass
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import kyash_api as ka  # noqa: E402
import kyash_bridge as kyash  # noqa: E402


def cmd_status() -> int:
    if not ka.is_ready():
        print("未ログイン。`py system\\kyash_login.py` で入って。")
        return 1
    a = kyash.account()
    print(f"アカウント: {a['email']} / @{a['user_name'] or '?'} (publicId {a['public_id']})")
    print(f"トークン残り: {a['remaining_h']:.0f} 時間")
    print(f"疎通: {'OK' if kyash.alive() else 'NG'}")
    b = kyash.balance_detail()
    if b:
        print(f"残高: ¥{int(b.get('amount') or 0):,} (マネー ¥{int(b.get('kyashMoney') or 0):,} / "
              f"バリュー ¥{int(b.get('kyashValue') or 0):,})")
    return 0


def cmd_refresh() -> int:
    try:
        ka.refresh_session()
    except ka.KyashError as e:
        print(f"更新に失敗: {e.text()}")
        return 1
    print("更新した。")
    return cmd_status()


def cmd_logout() -> int:
    removed = kyash.logout()
    print(f"削除した: {', '.join(removed) or 'なし'}")
    return 0


def cmd_login() -> int:
    email = input("Kyashのメールアドレス: ").strip()
    pw = getpass.getpass("Kyashのパスワード (表示されない): ")
    st = kyash.login_step1(email, pw)
    pw = ""
    if st.get("status") == "ok":
        print("✅ ログインした (認証リンクは不要だった)")
        return cmd_status()
    if st.get("status") != "link_required":
        print(f"❌ {st.get('error')}")
        return 1
    print("\nKyashからSMS（またはメール）でログイン用のリンクが届く。")
    print("リンクを開かずにコピーして、ここに貼って Enter。")
    pasted = input("リンク: ").strip()
    res = kyash.login_step2(st, pasted)
    if res.get("status") != "ok":
        print(f"❌ {res.get('error')}")
        return 1
    print("✅ ログインした。次回からは同じ端末として扱われ、リンク認証は省かれる。")
    return cmd_status()


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    return {"status": cmd_status, "refresh": cmd_refresh, "logout": cmd_logout}.get(
        arg, cmd_login)()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中止した。")
        sys.exit(1)
