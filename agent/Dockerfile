# agent/Dockerfile

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
 && rm -rf /var/lib/apt/lists/*

# Зависимости агента
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Исходники агента
COPY . .

# По умолчанию агент слушает 8080 (можно переопределить WEB_PORT)
ENV WEB_PORT=8080

EXPOSE 8080

# Запуск FastAPI-приложения с веб-чати-ком
CMD ["python", "web_app.py"]
