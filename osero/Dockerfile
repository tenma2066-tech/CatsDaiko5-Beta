# UltraOSERO 用 Dockerfile（依存ゼロ・最小構成）。
# server.js は標準モジュールのみ使用するので package.json も node_modules も不要。
# 4 ファイルだけをコンテナに入れて起動する。戦績は /app/data（ホストの osero-data）に保存する。
FROM node:20-alpine

WORKDIR /app

COPY server.js ./
COPY cpu_worker.js ./
COPY engine.js ./
COPY index.html ./

ENV NODE_ENV=production
ENV PORT=8080
ENV OSERO_DATA_DIR=/app/data
RUN mkdir -p /app/data && chown node:node /app/data
EXPOSE 8080

# 一般ユーザで実行（root のまま動かさない）
USER node

CMD ["node", "server.js"]
