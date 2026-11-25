# Google 検索への掲載

公開 URL は `https://nyanko-daiko-tnshop.duckdns.org`。
検索用のページは `/`・`/nyanko`・`/tsum`・`/puni`・`/ticket`・`/support`・`/source`（`sitemap.xml` に出るもの）。

## 公開時の設定

VPS の Docker Compose は既存の `SITE_HOST` を使って `SITE_URL=https://${SITE_HOST}` をアプリに渡す。
GitHub Actions の `SITE_DOMAIN` が `nyanko-daiko-tnshop.duckdns.org` になっていることを確認してから、通常の VPS デプロイ手順で公開する。
他の環境では `SITE_URL=https://nyanko-daiko-tnshop.duckdns.org` を環境変数に設定する。
URL に末尾スラッシュ以外のパス・認証情報・クエリは含めない。

公開後、次の URL が HTTP 200 を返すことを確認する。

- https://nyanko-daiko-tnshop.duckdns.org/robots.txt
- https://nyanko-daiko-tnshop.duckdns.org/sitemap.xml

## 掲載申請

1. https://search.google.com/search-console にサイト所有者の Google アカウントでログインする。
2. 「URL プレフィックス」で `https://nyanko-daiko-tnshop.duckdns.org/` を追加する。
3. 画面に提示された所有権確認を実施する。Google Analytics や Google タグマネージャーを既に使っていれば、その確認方法も選べる。HTML タグ方式を選ぶ場合は、発行された確認タグを公開ページの head に追加して再公開する必要がある。
4. 「サイトマップ」で `sitemap.xml` を送信する。
5. 「URL 検査」でトップページと、上の各ページをそれぞれ検査し、「インデックス登録をリクエスト」する。
6. 後日「ページのインデックス登録」で結果を確認する。申請やサイトマップ送信は掲載・順位・掲載時期を保証するものではない。

管理画面・API・注文状況・ヘルスチェックは検索対象外にする。robots.txt はアクセス制御ではないので、既存の認証はそのまま必要。
