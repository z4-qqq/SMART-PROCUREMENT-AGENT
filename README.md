# Smart Procurement Agent (MCP)

AI-агент для автоматизации закупок, построенный на **MCP-серверах** и LLM (OpenAI-совместимый endpoint, в т.ч. Cloud.ru Foundation Models).

Агент умеет:

- разобрать текстовый запрос на закупку (позиции, количества, бюджеты, валюту, вебхук);
- подобрать офферы от поставщиков через отдельный MCP-сервер;
- при необходимости пересчитать итог в нужную валюту;
- сформировать JSON-план закупки;
- по желанию отправить итоговый план на внешний вебхук;
- показать весь процесс в виде LLM-чата в веб-интерфейсе.

---

## Архитектура

Репозиторий состоит из четырёх сервисов:

```text
SMART-PROCUREMENT-AGENT/
├── agent/               # LLM-агент + web UI (FastAPI)
├── supplier-pricing-mcp/ # MCP-сервер: поиск офферов поставщиков
├── fx-rates-mcp/         # MCP-сервер: курсы валют и конвертация
└── notification-mcp/     # MCP-сервер: отправка плана закупок на вебхук
```

### Поток end-to-end

1. Пользователь пишет запрос в веб-чате (или в CLI агента).
2. `agent` вызывает LLM, чтобы распарсить запрос в структурированный JSON:
   - список позиций `items`;
   - целевая валюта `target_currency`;
   - (опционально) общий бюджет `budget`;
   - (опционально) URL вебхука `webhook_url`.
3. Агент дергает MCP-сервер `supplier-pricing-mcp.get_offers_for_items`:
   - для каждой позиции подбираются офферы поставщиков;
   - вычисляется минимальная возможная суммарная стоимость.
4. Если валюта офферов отличается от целевой, агент вызывает `fx-rates-mcp.convert_amount`.
5. Если задан вебхук — агент отправляет готовый план на `notification-mcp.send_procurement_plan_webhook`.
6. Агент отдаёт:
   - JSON-план (для интеграций и отладки),
   - человекочитаемое резюме (для веб-чата).

История диалога хранится на бэке (in-memory), поэтому можно писать:
> «теперь добавь ещё 3 монитора»  
и агент пересчитает общий план с учётом предыдущих сообщений.

---

## Технологии

- **Python 3.12+**
- **MCP** (`fastmcp`, `modelcontextprotocol`, `mcp.client.streamable_http`)
- **OpenAI SDK** (`openai.AsyncOpenAI`)  
  – можно работать как с `api.openai.com`, так и с OpenAI-совместимыми endpoint’ами, например Cloud.ru Foundation Models.
- **FastAPI + Uvicorn** — веб-оболочка (чат).
- **httpx** — HTTP-клиент для публичных API и вебхуков.
- **pydantic v2** — типизация и валидация моделей данных.
- **python-dotenv** — работа с `.env`.

---

## Установка

Рекомендуется единое виртуальное окружение для всего проекта.

```bash
git clone <url_репозитория>
cd SMART-PROCUREMENT-AGENT

python -m venv .venv
source .venv/bin/activate    # Linux / macOS
# .venv\Scripts\activate     # Windows

pip install --upgrade pip
```

Установить зависимости (общий минимум):

```bash
pip install \
  fastapi uvicorn \
  openai \
  fastmcp modelcontextprotocol \
  httpx \
  pydantic \
  python-dotenv \
  prometheus-client \
  opentelemetry-sdk opentelemetry-api
```

Или:
```
pip install -r requirements.txt
```

---

## Настройка окружения

### 1. MCP-серверы

В каждом каталоге MCP есть `.env.example`. Скопируй его в `.env` и при необходимости поправь значения.

#### `supplier-pricing-mcp/.env`

```bash
cd supplier-pricing-mcp
cp .env.example .env
```

Пример содержимого:

```env
SUPPLIER_API_BASE=https://fakestoreapi.com
SUPPLIER_DEFAULT_CURRENCY=USD
SUPPLIER_HTTP_TIMEOUT=10.0

HOST=0.0.0.0
PORT=8000
LOG_LEVEL=INFO
```

#### `fx-rates-mcp/.env`

```bash
cd fx-rates-mcp
cp .env.example .env
```

Пример:

```env
FX_API_BASE=https://api.exchangerate.host/latest
FX_HTTP_TIMEOUT=10.0
FX_DEFAULT_BASE_CURRENCY=RUB

HOST=0.0.0.0
PORT=8001
LOG_LEVEL=INFO
```

#### `notification-mcp/.env`

```bash
cd notification-mcp
cp .env.example .env
```

Пример:

```env
NOTIFICATION_HTTP_TIMEOUT=10.0

HOST=0.0.0.0
PORT=8002
LOG_LEVEL=INFO
```

> Для теста вебхуков удобно использовать [webhook.site](https://webhook.site) — просто подставь туда свой уникальный URL.

---

### 2. Агент и LLM

В каталоге `agent` создай `.env`:

```bash
cd agent
cat > .env << 'EOF'
OPENAI_API_KEY=sk-...                 # или токен Cloud.ru
OPENAI_MODEL=gpt-4o-mini              # или другой, например gpt-4.1-mini

# Если используешь Cloud.ru Foundation Models:
# OPENAI_BASE_URL=https://foundation-models.api.cloud.ru/v1/

# URL MCP-серверов (по умолчанию — локальные порты)
SUPPLIER_MCP_URL=http://127.0.0.1:8000/mcp
FX_MCP_URL=http://127.0.0.1:8001/mcp
NOTIFICATION_MCP_URL=http://127.0.0.1:8002/mcp

# Порт веб-интерфейса агента
WEB_PORT=8080
EOF
```

---

## Запуск end-to-end

Открой **4 терминала** (или tmux-сессию) и подними всё по очереди.

### 1. supplier-pricing-mcp

```bash
cd supplier-pricing-mcp
source ../.venv/bin/activate
python server.py
# Лог: "MCP Server: http://0.0.0.0:8000/mcp"
```

### 2. fx-rates-mcp

```bash
cd fx-rates-mcp
source ../.venv/bin/activate
python server.py
# MCP Server: http://0.0.0.0:8001/mcp
```

### 3. notification-mcp

```bash
cd notification-mcp
source ../.venv/bin/activate
python server.py
# MCP Server: http://0.0.0.0:8002/mcp
```

### 4. Веб-оболочка агента

```bash
cd agent
source ../.venv/bin/activate
python web_app.py
# FastAPI: http://0.0.0.0:8080
```

Затем открой в браузере:

```text
http://localhost:8080
```

---

## Как пользоваться веб-чатйком

1. Введите запрос в стиле:

   > Нужно купить 10 ноутбуков среднего уровня до 80 000 ₽ за штуку  
   > и 5 мониторов 27" до 25 000 ₽ за штуку.  
   > Хочу видеть итоговую сумму в EUR и отправить план на вебхук https://webhook.site/...

2. Агент:
   - распарсит запрос;
   - вызовет MCP-сервера;
   - покажет ответ:
     - краткое текстовое резюме,
     - агрегированную сумму и количество позиций,
     - кнопочку «Показать JSON-план закупки» (раскрывающийся блок с полным JSON).

3. Можно продолжить диалог:

   > Ок, добавь ещё 3 монитора, но уложись в общий бюджет 300 000 ₽.

   Агент использует историю диалога (на бэке) и пересчитает план.

---

## CLI-режим (без веба)

Если хочешь протестировать только агента, можно запустить CLI:

```bash
cd agent
source ../.venv/bin/activate
python main.py
```

Дальше — вводишь запрос в консоли, агент печатает:

- JSON-план закупки;
- краткое резюме.

---

## Структура каталогов (подробно)

```text
SMART-PROCUREMENT-AGENT/
├── agent/
│   ├── main.py         # основной агент: LLM + вызовы MCP
│   ├── web_app.py      # FastAPI-приложение с веб-чатйком
│   ├── .env            # настройки агента (LLM + MCP_URL + WEB_PORT)
│   └── ...             # вспомогательные файлы (по мере необходимости)
│
├── supplier-pricing-mcp/
│   ├── server.py       # запуск MCP сервера (streamable-http)
│   ├── mcp_instance.py # единый FastMCP()
│   ├── tools/          # инструменты MCP: поиск офферов
│   ├── .env.example
│   └── .env
│
├── fx-rates-mcp/
│   ├── server.py       # MCP сервер курсов валют
│   ├── mcp_instance.py
│   ├── tools/          # get_exchange_rate, convert_amount
│   ├── .env.example
│   └── .env
│
└── notification-mcp/
    ├── server.py       # MCP сервер уведомлений
    ├── mcp_instance.py
    ├── tools/          # send_procurement_plan_webhook
    ├── .env.example
    └── .env
```

---

## Что можно доработать

Идеи для дальнейшего развития:

- заменить in-memory историю диалогов на Redis/БД;
- добавить аутентификацию и многопользовательский режим;
- поддержать реальные API поставщиков (ERP, маркетплейсы, внутренние REST-сервисы);
- хранить и визуализировать историю планов закупок;
- добавить графики (динамика цен, экономия vs бюджет и т.д.).

---

