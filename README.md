# Smart Procurement Agent (MCP + OpenAI)

AI-агент закупок, который:

- понимает текстовые запросы вроде  
  _«Сделай мерч к конференции на 50 человек: худи, футболки и кружки, покажи итог в EUR и отправь план в вебхук…»_;
- строит план закупки и сам ходит в внешние сервисы через **MCP-серверы**:
  - `supplier-pricing-mcp` — поставщик (Printful / fakestoreapi.com);
  - `fx-rates-mcp` — конвертация валют;
  - `notification-mcp` — отправка плана по вебхуку;
- отдаёт **JSON-план закупки** и человекочитаемое резюме;
- имеет простую **web-оболочку** (чат в браузере).

Проект сделан как демонстрация **бизнес-ориентированного AI-агента**, интегрированного с публичным API через MCP.

---

## Архитектура

Репозиторий логически делится на несколько компонент:

- `agent/`  
  LLM-агент закупок:
  - `main.py` — логика агента (парсинг запроса, работа с MCP, режимы planner/tools-agent);
  - `web_app.py` — FastAPI + HTML-чат;
  - `.env` — ключи OpenAI и настройки агента.

- `supplier-pricing-mcp/`  
  MCP-сервер поставщика:
  - `server.py` — HTTP MCP-сервер;
  - `tools/get_offers_for_items.py` — подбор офферов поставщика;
  - интеграция:
    - **Printful API** (если доступен из региона и задан `PRINTFUL_API_KEY`);
    - **fakestoreapi.com** как fallback-поставщик;
    - финальный demo-fallback, если всё внешнее недоступно.

- `fx-rates-mcp/`  
  MCP-сервер курса валют:
  - `tools/convert_amount.py` — конвертация сумм:
    - пробует внешний FX API с `FX_API_ACCESS_KEY`;
    - если не получается — использует статический fallback-курс (например, USD→EUR).

- `notification-mcp/`  
  MCP-сервер уведомлений:
  - `tools/send_procurement_plan_webhook.py` — POST JSON-плана на указанный URL.

- `scripts/`  
  Вспомогательные скрипты:
  - отладка поискового MCP по поставщику (Printful/fakestore).

- `docker-compose.yaml`  
  Оркестрация всех сервисов:
  - поднимает MCP-серверы и агента;
  - пробрасывает нужные порты.

---

## Быстрый запуск через Docker Compose (рекомендуется)

### 1. Клонируем репозиторий

```bash
git clone <URL_РЕПОЗИТОРИЯ> SMART-PROCUREMENT-AGENT
cd SMART-PROCUREMENT-AGENT
```

### 2. Создаём `.env` файлы

#### `agent/.env`

Минимально:

```env
# OpenAI
OPENAI_API_KEY=sk-...

# Опционально: своё имя модели
OPENAI_MODEL_NAME=gpt-4.1-mini

# Режим работы агента:
# - planner (по умолчанию) — классический пайплайн: парсинг → MCP-вызовы → сводка.
# - tools  — LLM сам решает, какие MCP-инструменты вызывать (tools-agent режим).
AGENT_MODE=planner
```

> В docker-compose URL-ы MCP уже задаются через `environment`, поэтому в `agent/.env`
> достаточно ключа OpenAI и режима агента.

#### `supplier-pricing-mcp/.env`

```env
# Валюта поставщика
SUPPLIER_CURRENCY=USD

# Режим работы реального поставщика:
# - true  — пытаться работать с Printful, если есть ключ и регион позволяет;
# - false — сразу переходить к fakestoreapi.com (или demo-fallback).
USE_PRINTFUL=false

# Printful API (если доступен из региона)
# PRINTFUL_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# PRINTFUL_API_BASE=https://api.printful.com
```

Если `USE_PRINTFUL=true` и задан `PRINTFUL_API_KEY`, MCP сначала попробует Printful.  
Если Printful недоступен (регион, сеть, ошибка) — MCP автоматически свалится в fakestoreapi.com.

#### `fx-rates-mcp/.env`

```env
# Ключ внешнего FX API (опционально, в демо можно не задавать)
# FX_API_ACCESS_KEY=xxxxxxx

# Базовый URL внешнего FX API (пример — exchangerate.host или fxratesapi)
# FX_API_BASE_URL=https://api.exchangerate.host/convert
```

Если FX API не настроен или падает, `convert_amount` вернёт сумму с fallback-курсом
(и отметит `provider=fallback_static`, `fallback_used=true`).

#### `notification-mcp/.env`

Можно оставить пустым или использовать под свои настройки логирования и т.п.:

```env
# Пока обязательных переменных нет
```

### 3. Запускаем всё через docker-compose

```bash
docker compose up -d --build
```

Проверяем:

```bash
docker compose ps
```

Ожидаемый результат (пример):

```text
NAME                   IMAGE                                          COMMAND               SERVICE                STATUS         PORTS
fx-rates-mcp           smart-procurement-agent-fx-rates-mcp           "python server.py"    fx-rates-mcp           Up            0.0.0.0:8001->8001/tcp
notification-mcp       smart-procurement-agent-notification-mcp       "python server.py"    notification-mcp       Up            0.0.0.0:8002->8002/tcp
procurement-agent      smart-procurement-agent-agent                  "python web_app.py"   agent                  Up            0.0.0.0:8080->8080/tcp
supplier-pricing-mcp   smart-procurement-agent-supplier-pricing-mcp   "python server.py"    supplier-pricing-mcp   Up            0.0.0.0:8000->8000/tcp
```

### 4. Открываем web-чат

Локально:

```text
http://localhost:8080/
```

Если развёрнуто на удалённой VM — `http://<PUBLIC_IP>:8080/`
(или через домен/HTTPS, см. ниже).

В веб-чате можно запросить, например:

> Сделай мерч к конференции на 50 человек: худи, футболки и кружки, покажи итог в EUR

Агент:

1. распарсит запрос (позиции, количество, валюту, вебхук, бюджет);
2. вызовет `supplier-pricing-mcp`:
   - сначала попробует Printful (если включён),
   - при необходимости свалится на fakestoreapi.com,
3. посчитает сумму в валюте поставщика;
4. через `fx-rates-mcp` переведёт в целевую валюту (EUR);
5. при наличии webhook-URL вызовет `notification-mcp` и отправит JSON-план;
6. вернёт:
   - человекочитаемый текст (в markdown, рендерится на фронте),
   - подробный JSON-план (можно раскрыть «Показать JSON-план закупки»).

---

## Режимы работы агента

Агент поддерживает два режима (настраивается через `AGENT_MODE`):

- `planner` (по умолчанию)  
  Классический «оркестраторный» пайплайн:
  - LLM парсит текст в структурированный запрос;
  - Python-код сам вызывает `supplier-pricing-mcp`, `fx-rates-mcp`, `notification-mcp`;
  - LLM делает финальную сводку по уже готовому плану.

- `tools` (tools-agent режим)  
  Агент отдаёт LLM-модели список MCP-инструментов, и она сама решает,
  какие вызывать и в каком порядке, используя OpenAI tools:
  - более «автономный» режим;
  - удобно для демонстрации, как LLM сама режиссирует MCP-вызовы.

Переключение:

```env
AGENT_MODE=planner   # или tools
```

---

## Как устроен поставщик (supplier-pricing-mcp)

В `tools/get_offers_for_items.py` реализован каскад:

1. **Printful**  
   Если `USE_PRINTFUL=true` и есть `PRINTFUL_API_KEY`:
   - по каждому `sku` строится текстовый запрос (например, `"hoodie"`, `"t-shirt"`, `"mug"`);
   - через Printful Catalog API ищется подходящий product/variant;
   - выбирается один вариант и формируется оффер:
     ```json
     {
       "supplier": "printful",
       "sku": "hoodie",
       "unit_price": 12.34,
       "currency": "USD",
       "variant_id": 20556,
       "description": "Gildan 18500 Unisex Heavy Blend Hoodie ..."
     }
     ```

2. **FakeStore fallback**  
   Если Printful недоступен (регион, сеть, ошибка) или отключён:
   - MCP идёт в `https://fakestoreapi.com/products`;
   - подбирает «лучший» товар по простому скорингу названия/категории;
   - отдаёт оффер с `supplier="fakestoreapi"`.

3. **Demo-fallback**  
   Если не достучались ни до Printful, ни до fakestoreapi:
   - возвращается структура с `total_min_cost=0.0`,
   - все позиции попадают в `unavailable_skus`,
   - `provider="demo_fallback"`, `fallback_used=true`.

Все режимы возвращают один и тот же формат `structuredContent`, чтобы
агенту и фронту не приходилось различать источники.

---

## FX-курс (fx-rates-mcp)

`tools/convert_amount.py`:

- пытается дернуть внешний FX API (настраивается через `.env`, например `FX_API_ACCESS_KEY`);
- если:
  - ключ не задан,
  - API вернул ошибку,
  - не нашлась нужная пара,

  то MCP возвращает конвертацию по **запасному курсу** (fallback) и помечает это в ответе:

```json
{
  "base": "USD",
  "quote": "EUR",
  "amount_base": 123.45,
  "amount_quote": 111.11,
  "rate": 0.90,
  "provider": "fallback_static",
  "fallback_used": true,
  "warning": "FX API не вернул корректный курс, использован fallback-курс (fallback_static).",
  "raw": { ... }
}
```

Агент пересчитывает сумму по этому курсу, но в UI можно подсветить,
что это приблизительное значение.

---

## Notification-MCP

`notification-mcp/tools/send_procurement_plan_webhook.py`:

- принимает:
  - `url` — строка с вебхуком;
  - `plan` — JSON-план закупки;
- делает HTTP `POST` с `application/json`;
- возвращает:
  - статус ответа (`status_code`, `ok`),
  - тело ответа (если есть).

Если пользователь в запросе не указал вебхук — MCP не вызывается, план просто строится и показывается.

---

## Локальный запуск без Docker (для разработки)

Если хочешь крутить всё руками:

1. Установи зависимости в каждом модуле (`agent/`, `supplier-pricing-mcp/`, …):

   ```bash
   cd agent
   pip install -r requirements.txt
   ```

2. Запусти MCP-серверы в отдельных терминалах:

   ```bash
   cd supplier-pricing-mcp
   python server.py

   cd fx-rates-mcp
   python server.py

   cd notification-mcp
   python server.py
   ```

3. Запусти агента:

   ```bash
   cd agent
   python web_app.py
   ```

И далее всё так же открываешь `http://localhost:8080/`.

---

## Продакшен-деплой (кратко)

Для продакшена (например, на Ubuntu 22.04 с публичным IP):

1. Скопировать репозиторий на сервер.
2. Настроить `.env` файлы (как в секции выше).
3. Запустить:

   ```bash
   docker compose up -d --build
   ```

4. Поставить **nginx** как reverse-proxy:
   - слушает `80/443`,
   - проксирует на `http://127.0.0.1:8080`.
5. Выпустить Let’s Encrypt-сертификат через `certbot --nginx`
   для домена вроде `smart_procurement_agent.ru`.

После этого агент будет доступен по HTTPS по красивому доменному имени.

---

## Дальнейшее развитие

Идеи, как развивать проект:

- Добавить ещё MCP-серверы:
  - ERP/CRM,
  - реальных локальных поставщиков с публичными API,
  - внутренние справочники компаний.
- Усложнить стратегию подбора:
  - несколько поставщиков,
  - мультивалютные прайсы,
  - ограничения по срокам поставки.
- Сохранение истории и планов в базу (PostgreSQL/SQLite).
- Авторизация / multitenancy / организация по отделам.

Но уже в текущем виде проект демонстрирует:

- **мультиагентную архитектуру через MCP**,
- **интеграцию с реальными и публичными API (Printful / fakestore)**,
- **бизнес-сценарий закупок** с автоматизацией от текста до плана и вебхука.