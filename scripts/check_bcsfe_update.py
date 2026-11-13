"""bcsfe に新しいバージョンが出ているか調べる（GitHub Actions から呼ばれる）。

- requirements.txt に書かれている bcsfe==X.Y.Z と、PyPI の最新版を比べる
- 同じメジャーバージョン（3.x）の中で新しいものがあるときだけ「更新する」と判断する
  （4.0 のような大きな変更は、勝手に上げずに人が確認する）
- 結果は標準出力と、GitHub Actions の出力（GITHUB_OUTPUT）に書く

手元での確認用に、環境変数 BCSFE_LATEST_OVERRIDE で「最新版」を差し替えられる。
"""
import json
import os
import re
import sys
import urllib.request


def parse(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def decide(current: str, latest: str):
    """(更新するか, 理由)"""
    c, l = parse(current), parse(latest)
    if l <= c:
        return False, "すでに最新です"
    if l[0] != c[0]:
        return False, f"メジャーバージョンが変わるため自動では更新しません（{current} → {latest}）。内容を確認してください"
    return True, "新しいバージョンがあります"


def main() -> int:
    text = open("requirements.txt", encoding="utf-8").read()
    m = re.search(r"^bcsfe==([0-9][0-9.]*)\s*$", text, re.M)
    if not m:
        print("requirements.txt に bcsfe==X.Y.Z の行が見つかりません")
        return 1
    current = m.group(1)
    latest = os.environ.get("BCSFE_LATEST_OVERRIDE") or json.load(
        urllib.request.urlopen("https://pypi.org/pypi/bcsfe/json", timeout=30))["info"]["version"]
    upgrade, reason = decide(current, latest)
    print(f"現在: {current} / 最新: {latest} → {reason}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"current={current}\nlatest={latest}\nupgrade={'true' if upgrade else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
