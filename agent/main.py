from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv, find_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, HttpUrl

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# ---------------------------------------------------------------------------
# Инициализация окружения и логирования
# ---------------------------------------------------------------------------

load_dotenv(find_dotenv())

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("procurement_agent")

# ---------------------------------------------------------------------------
# Настройки LLM (OpenAI-совместимый endpoint Cloud.ru)
# ---------------------------------------------------------------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv(
    "OPENAI_BASE_URL"
)  # например: https://foundation-models.api.cloud.ru/v1/
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY is not set, LLM-вызовы упадут.")

_llm_client: Optional[AsyncOpenAI] = None


def get_llm_client() -> AsyncOpenAI:
    """Ленивая инициализация клиента OpenAI-совместимого API."""
    global _llm_client
    if _llm_client is None:
        kwargs: Dict[str, Any] = {"api_key": OPENAI_API_KEY}
        if OPENAI_BASE_URL:
            kwargs["base_url"] = OPENAI_BASE_URL.rstrip("/")
        _llm_client = AsyncOpenAI(**kwargs)
    return _llm_client


# ---------------------------------------------------------------------------
# Настройки MCP-серверов
# ---------------------------------------------------------------------------

SUPPLIER_MCP_URL = os.getenv("SUPPLIER_MCP_URL", "http://127.0.0.1:8000/mcp")
FX_MCP_URL = os.getenv("FX_MCP_URL", "http://127.0.0.1:8001/mcp")
NOTIFICATION_MCP_URL = os.getenv("NOTIFICATION_MCP_URL", "http://127.0.0.1:8002/mcp")

# ---------------------------------------------------------------------------
# Модели данных
# ---------------------------------------------------------------------------


class ParsedItem(BaseModel):
    """Позиция закупки, как её возвращает LLM-парсер."""

    sku: str = Field(..., description="Текстовый запрос под поиск товара в Printful")
    quantity: int = Field(..., ge=1)
    max_unit_price: Optional[float] = Field(
        None,
        description="Максимальная цена за штуку (в валюте пользователя), если указана",
    )


class ParsedRequest(BaseModel):
    """Распарсенный запрос пользователя."""

    target_currency: str = Field(
        "",
        description="В какой валюте показать итоги (например, 'EUR', 'USD', 'RUB')",
    )
    budget: Optional[float] = Field(
        None, description="Общий бюджет (если указан пользователем)"
    )
    webhook_url: Optional[HttpUrl] = Field(
        None, description="URL вебхука для отправки плана закупки"
    )
    items: List[ParsedItem] = Field(default_factory=list)


class Totals(BaseModel):
    currency: str
    total_net: float
    total_items: int


# ---------------------------------------------------------------------------
# Промпт для LLM-парсера
# ---------------------------------------------------------------------------
PARSE_REQUEST_SYSTEM_PROMPT = """
Ты — AI-ассистент по закупке мерча через поставщика Printful.

Твоя задача — по русскому текстовому описанию запроса сформировать
структурированный JSON следующего вида:

{
  "target_currency": "EUR",
  "budget": 3000.0,
  "webhook_url": "https://example.com/hook",
  "items": [
    {
      "sku": "hoodie",
      "quantity": 50,
      "max_unit_price": 50.0
    }
  ]
}

ГЛАВНОЕ ТРЕБОВАНИЕ: поле items[].sku — это КРАТКИЙ ПОИСКОВЫЙ КЛЮЧ
ДЛЯ КАТАЛОГА PRINTFUL. ОЧЕНЬ ВАЖНО, ЧТОБЫ ОН БЫЛ ПРАВИЛЬНЫМ.

Правила для items[].sku:

1) Используй только базовые английские названия типов товаров:
   - "hoodie"          — для худи, толстовок, тёплых кофт с капюшоном;
   - "t-shirt"         — для футболок, лонгсливов;
   - "mug"             — для кружек, чашек;
   - "sweatshirt"      — для свитшотов без капюшона;
   - "tote bag"        — для шопперов, сумок;
   - "cap"             — для кепок, бейсболок;
   - "hat"             — для шапок;
   - "sticker"         — для стикеров, наклеек;
   - "notebook"        — для блокнотов;
   - "backpack"        — для рюкзаков;
   - "phone case"      — для чехлов на телефон;
   - если ничего не подходит, выбери максимально близкий тип.

2) НЕ используй лишние слова в sku:
   - НЕЛЬЗЯ: "unisex hoodie", "black hoodie", "conference hoodie", "logo mug".
   - НУЖНО:  "hoodie", "t-shirt", "mug".
   - Допускаются только варианты из списка выше (1–2 слова максимум).

3) Перевод с русского на sku:
   - "худи", "толстовки", "толстовка", "кофты с капюшоном" → "hoodie"
   - "футболки", "футболка", "лонгсливы" → "t-shirt"
   - "кружки", "чашки", "кружка", "чашка" → "mug"
   - "свитшоты", "толстовки без капюшона" → "sweatshirt"
   - "шопперы", "сумки", "эко-сумки" → "tote bag"
   - "кепки", "бейсболки" → "cap"
   - "шапки", "beanie" → "hat"
   - "стикеры", "наклейки" → "sticker"
   - "блокноты" → "notebook"
   - "рюкзаки" → "backpack"

4) Никаких характеристик (цвет, размер, логотип, качество) внутрь sku НЕ добавляй.
   Вся дополнительная информация учитывается только в текстовом описании
   запроса пользователя, но НЕ попадает в sku.

Примеры ПРАВИЛЬНОГО формирования items[].sku:

- "хочу худи с логотипом для сотрудников" →
  sku: "hoodie"

- "нужны футболки и кружки для конференции" →
  items:
    { "sku": "t-shirt", "quantity": ... },
    { "sku": "mug", "quantity": ... }

- "шопперы и кепки с логотипом" →
  "tote bag" и "cap"

---

Остальные поля:

2) items[].quantity
   - Целое число штук для каждой позиции.

3) items[].max_unit_price
   - Если в запросе указана максимальная цена за единицу — укажи её
     как число (float) в валюте пользователя, которую ты распознал.
   - Если явно не указано ограничение по цене за штуку — ставь null.

4) budget
   - Если пользователь указал общий бюджет (например, "до 3000 евро",
     "бюджет 500 тысяч рублей") — распарсь его в число (float) в той же валюте.
   - Если общего бюджета нет — ставь null.

5) target_currency
   - Определи, в какой валюте пользователь хочет видеть итоги:
     "EUR", "USD", "RUB" и т.п.
   - Если явно не сказано — выбери наиболее естественную валюту из контекста
     (например, если в тексте "евро" — EUR, "доллары" — USD, "рублей" — RUB).
   - Если совсем непонятно — используй "USD".

6) webhook_url
   - Если пользователь дал URL, куда отправить план закупки — сохрани его
     как строку.
   - Если URL нет — ставь null.

Отвечай СТРОГО валидным JSON по этой схеме, без дополнительных полей и комментариев.
"""


# ---------------------------------------------------------------------------
# Вызов LLM для парсинга пользовательского текста
# ---------------------------------------------------------------------------


async def parse_user_request(user_text: str) -> ParsedRequest:
    """Преобразовать свободный текст пользователя в структурированный ParsedRequest через LLM."""
    client = get_llm_client()

    messages = [
        {"role": "system", "content": PARSE_REQUEST_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Текст запроса:\n{user_text}\n\nВерни ТОЛЬКО JSON без комментариев.",
        },
    ]

    try:
        resp = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        content = resp.choices[0].message.content
        data = json.loads(content)
        parsed = ParsedRequest.model_validate(data)
        logger.info("Parsed user request: %s", parsed.model_dump(mode="json"))
        return parsed
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка при парсинге запроса через LLM: %s", exc)
        # Фоллбек: одна позиция, всё по умолчанию
        fallback = ParsedRequest(
            target_currency="USD",
            budget=None,
            webhook_url=None,
            items=[
                ParsedItem(
                    sku="generic merch item",
                    quantity=1,
                    max_unit_price=None,
                )
            ],
        )
        return fallback


# ---------------------------------------------------------------------------
# Базовый помощник для вызова MCP-серверов
# ---------------------------------------------------------------------------


async def _call_mcp_tool_json(
    server_url: str,
    tool_name: str,
    arguments: Dict[str, Any] | None,
) -> Dict[str, Any] | str:
    """Вспомогательная функция: вызвать MCP-tool и вернуть JSON-ответ или строку-ошибку."""
    logger.info(
        "Calling MCP tool %s on %s with args=%r", tool_name, server_url, arguments
    )
    try:
        async with streamablehttp_client(server_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments or {})

        # result: CallToolResult
        logger.info("MCP tool %s result: %r", tool_name, result)
        if getattr(result, "is_error", False):
            err = getattr(result, "error", None)
            msg = getattr(err, "message", None) if err else None
            msg = msg or "unknown MCP error"
            return f"Error calling tool '{tool_name}': {msg}"

        # 1) Предпочитаем result.data (развёрнутый structured_content)
        data = getattr(result, "data", None)
        if data is not None:
            return data

        # 2) structured_content как dict / список dict-ов
        struct = getattr(result, "structured_content", None)
        if struct is not None:
            if isinstance(struct, list) and struct and isinstance(struct[0], dict):
                return struct[0]
            if isinstance(struct, dict):
                return struct

        # 3) Фоллбек — пробуем распарсить JSON из content[0].text
        contents = getattr(result, "content", None) or []
        for c in contents:
            text = getattr(c, "text", None)
            if not text:
                continue
            try:
                return json.loads(text)
            except Exception:
                # не JSON — вернём как текст
                return text

        return {}
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Ошибка при вызове MCP tool %s на %s: %s", tool_name, server_url, exc
        )
        return f"Error calling tool '{tool_name}': {exc}"


# ---------------------------------------------------------------------------
# Обёртки над конкретными MCP-серверами
# ---------------------------------------------------------------------------


async def call_supplier_mcp(items: List[ParsedItem]) -> Dict[str, Any] | str:
    """Вызвать supplier-pricing-mcp/get_offers_for_items."""
    args = {
        "items": [item.model_dump(mode="json") for item in items],
        "max_suppliers_per_item": 3,
    }
    resp = await _call_mcp_tool_json(SUPPLIER_MCP_URL, "get_offers_for_items", args)
    logger.info("supplier-pricing-mcp response: %r", resp)
    return resp


async def call_fx_mcp(amount: float, base: str, quote: str) -> Dict[str, Any] | str:
    """Вызвать fx-rates-mcp/convert_amount."""
    args = {"amount": amount, "base": base, "quote": quote}
    resp = await _call_mcp_tool_json(FX_MCP_URL, "convert_amount", args)
    logger.info("fx-rates-mcp response: %r", resp)
    return resp


async def send_plan_webhook(url: str, plan: Dict[str, Any]) -> Dict[str, Any] | str:
    """Вызвать notification-mcp/send_procurement_plan_webhook."""
    args = {"url": url, "plan": plan}
    resp = await _call_mcp_tool_json(
        NOTIFICATION_MCP_URL,
        "send_procurement_plan_webhook",
        args,
    )
    logger.info("notification-mcp response: %r", resp)
    return resp


# ---------------------------------------------------------------------------
# Агрегация результатов поставщика
# ---------------------------------------------------------------------------


def _aggregate_totals_from_supplier_response(
    supplier_resp: Dict[str, Any] | str,
) -> Totals:
    """
    Посчитать общую сумму и количество по ответу supplier-pricing-mcp.

    Поддерживает два формата:
    1) Уже "чистый" словарь от MCP-сервера:
       {
         "currency": "...",
         "items": [...],
         "total_min_cost": ...
       }

    2) Обёртка результата mcp.call_tool:
       {
         "_meta": ...,
         "content": [...],
         "structuredContent": {
             "currency": "...",
             "items": [...],
             "total_min_cost": ...
         },
         "isError": false
       }
    """
    # Строка или что-то ещё странное — считаем, что ничего не купили
    if isinstance(supplier_resp, str):
        return Totals(currency="USD", total_net=0.0, total_items=0)

    if not isinstance(supplier_resp, dict):
        return Totals(currency="USD", total_net=0.0, total_items=0)

    # Если это "обёртка" от MCP — разворачиваем до structuredContent
    if "structuredContent" in supplier_resp and isinstance(
        supplier_resp["structuredContent"],
        dict,
    ):
        supplier_resp = supplier_resp["structuredContent"]
    elif "structured_content" in supplier_resp and isinstance(
        supplier_resp["structured_content"],
        dict,
    ):
        # На будущее, если библиотека вернёт snake_case
        supplier_resp = supplier_resp["structured_content"]

    # Далее работаем уже с "плоским" объектом вида:
    # {"currency": "...", "items": [...], "total_min_cost": ...}
    currency = str(supplier_resp.get("currency") or "USD")

    total_net_raw = supplier_resp.get("total_min_cost", 0.0)
    try:
        total_net = float(total_net_raw)
    except (TypeError, ValueError):
        total_net = 0.0

    total_items = 0
    for item_block in supplier_resp.get("items", []) or []:
        item = item_block.get("item") or {}
        qty_raw = item.get("quantity", 0)
        try:
            qty = int(qty_raw)
        except (TypeError, ValueError):
            qty = 0
        total_items += qty

    return Totals(currency=currency, total_net=total_net, total_items=total_items)



# ---------------------------------------------------------------------------
# Построение плана закупки end-to-end
# ---------------------------------------------------------------------------


async def build_procurement_plan(user_text: str) -> Dict[str, Any]:
    """
    Главная функция пайплайна:

    user_text → LLM-парсер → supplier-pricing-mcp (Printful) →
    fx-rates-mcp → notification-mcp → итоговый JSON-план.
    """
    # 1) Парсинг запроса
    parsed_request = await parse_user_request(user_text)

    # 2) Вызов поставщика
    supplier_resp = await call_supplier_mcp(parsed_request.items)
    totals_supplier = _aggregate_totals_from_supplier_response(supplier_resp)

    # 3) Конвертация валюты (если нужно)
    fx_raw: Dict[str, Any] | str | None = None
    totals_target = Totals(
        currency=parsed_request.target_currency or totals_supplier.currency,
        total_net=totals_supplier.total_net,
        total_items=totals_supplier.total_items,
    )

    if (
        parsed_request.target_currency
        and parsed_request.target_currency != totals_supplier.currency
    ):
        fx_raw = await call_fx_mcp(
            amount=totals_supplier.total_net,
            base=totals_supplier.currency,
            quote=parsed_request.target_currency,
        )
        # Пытаемся аккуратно вытащить amount_quote
        if isinstance(fx_raw, dict):
            aq = fx_raw.get("amount_quote")
            try:
                converted = float(aq)
                totals_target = Totals(
                    currency=parsed_request.target_currency,
                    total_net=converted,
                    total_items=totals_supplier.total_items,
                )
            except (TypeError, ValueError):
                # если не получилось — оставляем как есть
                totals_target = Totals(
                    currency=parsed_request.target_currency,
                    total_net=totals_supplier.total_net,
                    total_items=totals_supplier.total_items,
                )
        else:
            # fx-ошибка (строка) — просто копируем сумму
            totals_target = Totals(
                currency=parsed_request.target_currency,
                total_net=totals_supplier.total_net,
                total_items=totals_supplier.total_items,
            )

    # 4) Отправка по вебхуку (если есть)
    webhook_result: Dict[str, Any] | str | None = None

    webhook_url_str: Optional[str] = None
    if parsed_request.webhook_url is not None:
        webhook_url_str = str(parsed_request.webhook_url)

    # Собираем план (без webhook_result, чтобы не сериализовывать HttpUrl)
    plan: Dict[str, Any] = {
        "request": parsed_request.model_dump(mode="json"),
        "supplier_offers": supplier_resp,
        "totals_supplier_currency": totals_supplier.model_dump(mode="json"),
        "totals_target_currency": totals_target.model_dump(mode="json"),
        "fx_metadata": fx_raw,
    }

    if webhook_url_str:
        webhook_result = await send_plan_webhook(webhook_url_str, plan)
        plan["webhook_result"] = webhook_result

    return plan


# ---------------------------------------------------------------------------
# CLI-обёртка
# ---------------------------------------------------------------------------


async def _run_cli() -> None:
    print("=== AI агент закупок (MCP + OpenAI + Printful) ===")
    print("Введи запрос, например:")
    print(
        "Нужно подготовить мерч к конференции: худи, футболки и кружки, "
        "покажи итог в EUR и отправь план в мой вебхук https://example.com/hook"
    )
    print("\nВводи текст, пустая строка — конец:\n")

    lines: List[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)

    user_text = "\n".join(lines).strip()
    if not user_text:
        print("Пустой запрос, выходим.")
        return

    print("\n>>> Строю план закупки...\n")

    plan = await build_procurement_plan(user_text)

    print(">>> JSON-план:")
    print(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(_run_cli())
