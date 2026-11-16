FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=Asia/Tokyo
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["sh", "-c", "gunicorn app:app --workers 1 --threads 4 --timeout 120 --graceful-timeout 110 --bind 0.0.0.0:${PORT:-8080}"]
