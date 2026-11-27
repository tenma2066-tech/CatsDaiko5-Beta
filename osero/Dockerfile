# UltraOSERO 用 Dockerfile（依存ゼロ・最小構成）。
# server.js は標準モジュールのみ使用するので package.json も node_modules も不要。
# 3 ファイルだけをコンテナに入れて起動する。
FROM node:20-alpine

WORKDIR /app

COPY server.js ./
COPY engine.js ./
COPY index.html ./

ENV NODE_ENV=production
ENV PORT=8080
EXPOSE 8080

# 一般ユーザで実行（root のまま動かさない）
USER node

CMD ["node", "server.js"]
