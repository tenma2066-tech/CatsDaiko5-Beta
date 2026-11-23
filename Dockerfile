FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=Asia/Tokyo
WORKDIR /app
# tor：LINEログインのIPローテーション用（既定では使わない。TSUM_USE_TOR=1 のときだけ起動する）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tor \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt
COPY . .
RUN chmod +x docker-entrypoint.sh
# 入口スクリプトが（必要なら）Tor を起動してから gunicorn を起動する。
CMD ["./docker-entrypoint.sh"]
