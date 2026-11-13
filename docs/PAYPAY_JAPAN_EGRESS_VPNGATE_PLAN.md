# PayPay 日本向け通信経路（VPN Gate）実装計画

作成日: 2026-11-14
対象: `tenma2066-tech/CatsDaiko5-Beta`
作業ブランチ: `feature/paypay-japan-egress-vpngate`
基準: Beta `main`（安定版 `CatsDaiko5` は変更しない）

## 目的

Render（Singapore）で動作する既存サイトの画面・注文フロー・セーブ処理を維持したまま、PayPay向けの外向き通信だけを日本国内の出口経由にできるか検証・実装する。

## 既存実装の確認

- Beta `main` の `app.py` が既存Web UI、注文、管理者画面、ワーカーの統合入口。
- `paypay_gateway.py` がPayPay OAuth、OTP、送金リンク確認・受取を担当し、HTTP通信は `aiohttp.ClientSession` を使用。
- `render.yaml` はPython runtime、Singapore region、Gunicorn 1 worker / 4 threads。現状の注文受付は `SHOP_LIVE_ORDERS_ENABLED=0`。
- PR #3で元の顧客UIと購入フローがBeta `main` に統合済み。
- PR #9（Cloud Run / Firestore）は別Draftブランチであり、この作業では変更・マージしない。

## VPN Gateの技術的な適合性

VPN Gateは一般的なHTTP/SOCKSプロキシではなく、SoftEther / OpenVPN / L2TP / SSTPのVPN中継サービスである。公式サーバー一覧はOpenVPN設定を配布しているが、接続先はボランティア運営で、稼働・IP・性能が変動する。

OpenVPNクライアントで通常のVPN経路を作るにはTUNデバイスとネットワーク管理権限が必要になる構成が一般的。Renderの現在のBlueprint（`render.yaml`）には `/dev/net/tun` のデバイス割当や `NET_ADMIN` capabilityを指定する項目がなく、Render上でOpenVPNトンネルを直接起動できるとは確認できていない。従って、Render上でVPN Gateをそのまま起動する実装は、動作確認なしに追加しない。

## 重要なセキュリティ制約

VPN Gateの公開中継サーバーは第三者ボランティアが運営する。VPN Gate公式ポリシーでは接続元IP、接続先ホスト名/IP/ポート等の接続ログが記録される旨が説明されている。VPNトンネルの暗号化は出口サーバー運営者への信頼を代替しない。HTTPSの証明書検証を無効化してはならない。

PayPayの電話番号・パスワード・OTP・セッションCookie・アクセストークン・送金リンク等の秘密情報を、運営者の信頼性を確認できない公開VPN出口へ送る構成はデフォルト有効にしない。VPN Gate経由のPayPay実アカウント認証・決済テストは実施しない。

## 実装方針（元のシステムを壊さない）

1. `app.py`、HTML、顧客向けルート、注文状態遷移、価格計算、ワーカー、保存形式は変更しない。
2. PayPay通信層だけを独立したtransport adapterに分離する。
3. デフォルトは既存の直接通信を維持する（後方互換）。
4. 日本向け出口は明示的な環境変数でのみ有効化し、設定不備・接続失敗時はfail-closed。直接通信への自動フォールバックは禁止。
5. PayPay以外のHTTP通信へプロキシ設定を波及させない。
6. 接続先ホストをPayPay公式ホストに固定し、TLS証明書検証を必須とする。
7. ログにはプロキシURLの認証情報、PayPay資格情報、Cookie、OTP、トークン、リンクコード、本文を出さない。
8. 診断はIP/接続状態/応答時間/エラー分類などの非機密メタデータだけに限定する。

## 実装を開始するための技術ゲート

次のどちらかが確認できるまで、VPN Gate専用の稼働コードを追加しない。

- Render上で必要なTUNデバイスと権限を安全に利用できる公式サポートが確認できる。
- TUNを必要としない、管理者・運営者・通信経路の信頼性を検証できる日本国内のアプリケーション層egress gatewayを別途用意できる。

単に `aiohttp` の `proxy=` にVPN GateのOpenVPN設定を渡すような、実際には接続できない疑似実装は作らない。

## 検証計画

- 既存PayPay gatewayのテストを基準にして回帰を防ぐ。
- transport adapter単体で、直接通信（デフォルト）、proxy指定、接続失敗、TLS検証、許可ホスト制限をテストする。
- モックHTTPサーバーでOAuth/OTP/リンク確認/受取の各リクエストが同じtransportを通ることを確認する。
- 実PayPayログイン・実決済は別途明示承認まで実施しない。
- Renderのサービス・環境変数・データはユーザーの明示承認まで変更しない。
- `SHOP_LIVE_ORDERS_ENABLED=0` を維持する。

## 公式資料

- VPN Gate: https://www.vpngate.net/ja/
- VPN Gate overview: https://www.vpngate.net/en/about_overview.aspx
- VPN Gate anti-abuse / logging policy: https://www.vpngate.net/en/about_abuse.aspx
- Render deploy documentation: https://render.com/docs/deploys

## 次の作業

1. Render上で利用可能なネットワーク機能・制限を追加確認する。
2. VPN Gate OpenVPN接続を必要とする部分と、HTTP(S)プロキシで代替可能な部分を区別する。
3. 実現可能な場合のみ、PayPay通信層のadapter分離を実装し、Beta専用PRで提出する。
4. 元の画面・購入フローの差分がゼロであることを比較・テストする。
