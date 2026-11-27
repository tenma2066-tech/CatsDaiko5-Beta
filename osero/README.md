# UltraOSERO

自由な盤面とルールで、最大6人まで遊べるオセロ。普段は見やすい真上からの盤面で、石を返すときは3Dで裏返ります。大きな一手では、カメラが寄って回り込む演出が入ります。

## ファイル

| ファイル | 役割 |
|---|---|
| `index.html` | ゲーム画面（Three.js r128 を cdnjs から読み込みます） |
| `engine.js` | ルールとCPU。ページ、CPU用のWeb Worker、サーバーで同じものを使います |
| `server.js` | オンライン対戦用のサーバー。外部パッケージなしで、Node 18 以上で動きます |

## ローカルで遊ぶ

`index.html` と `engine.js` を同じフォルダに置き、何かしらのHTTPサーバーで開きます。CPUはWorkerで動くので、`file://` で直接開くよりサーバー経由のほうが確実です。

```sh
node server.js          # http://localhost:8080
```

## VPSに置く

```sh
# 3ファイルを /opt/ultraosero に置く
PORT=8080 node /opt/ultraosero/server.js
```

常駐させる場合は、systemd のユニットを作ります（`/etc/systemd/system/ultraosero.service`）。

```ini
[Unit]
Description=UltraOSERO
After=network.target

[Service]
WorkingDirectory=/opt/ultraosero
ExecStart=/usr/bin/node server.js
Environment=PORT=8080
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```

Nginx で HTTPS とリバースプロキシを設定します（証明書は Let's Encrypt / certbot で取得）。

```nginx
server {
  server_name osero.example.com;
  location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;
  }
}
```

## オンラインの流れ

1. 「新しいゲーム」でルールと席を決める（「人間」の席が参加枠になる）
2. 「オンライン」から「部屋を作る」を押し、表示された4文字のコードを友達に伝える
3. 友達はコードを入れて参加する。ホストが「対局開始」を押す（空いた席にはCPUが入る）

石を置けるかどうかはサーバーが判定します。ページを再読み込みしても、同じ席に戻れます。途中で退出した席は、CPU（ふつう）が引き継ぎます。

## 仕組みのメモ

- 盤面：標準・十字・八角形・ダイヤ・島。サイズは6〜16。壁ブロックは点対称に置かれ、返すことはできない
- 3人以上：自分以外の石なら、誰の石でも挟んで返せる
- CPU：αβ探索に反復深化と置換表を組み合わせている。超つよいは、2人対戦なら残り14マスから完全に読み切る。3人以上は「自分以外は全員敵」とみなして読む
- 演出：返した枚数、角、方向の数、連鎖、逆転、決着の一手から「大技度」を計算し、小・特大の2段階で演出する。設定はフル / 短め / オフ、タップでスキップできる

## 今後の候補

- 持ち時間の設定と、演出中に時間を止める処理
- CPUを worker_threads に移し、サーバーを止めずに考えさせる
- 戦績とレーティング（SQLite）、観戦一覧、スタンプ
- 特殊な石（爆弾、固定石）とルールエディタ
