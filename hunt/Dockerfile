# 彩狩 -IROGARI- 用 Dockerfile（依存ゼロ・最小構成）。
# server.js は標準モジュールのみ使用するので package.json も node_modules も不要。
FROM node:20-alpine

WORKDIR /app

COPY server.js ./
COPY engine.js ./
COPY accounts.js ./
COPY gacha.js ./
COPY index.html ./

ENV NODE_ENV=production
ENV PORT=8080
# アカウントと戦績の保存先（ホストの hunt-data をここへつなぐ）
ENV HUNT_DATA_DIR=/app/data
RUN mkdir -p /app/data && chown node:node /app/data
EXPOSE 8080

# 一般ユーザで実行（root のまま動かさない）
USER node

CMD ["node", "server.js"]
