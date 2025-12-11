from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple, TypedDict

import httpx
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, HttpUrl, ValidationError

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# ---------------------- ИНИЦИАЛИЗАЦИЯ ----------------------

load_dotenv()

logger = logging.getLogger("procurement_agent")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("CLOUDRU_OPENAI_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://foundation-models.api.cloud.ru/v1/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY (или CLOUDRU_OPENAI_KEY) не задан в .env")

client = OpenAI(
    api_key=OPENAI_API_KEY,
    base_url=OPENAI_BASE_URL,
)

SUPPLIER_MCP_URL = os.getenv("SUPPLIER_MCP_URL", "http://127.0.0.1:8000/mcp")
FX_MCP_URL = os.getenv("FX_MCP_URL", "http://127.0.0.1:8001/mcp")
NOTIFICATION_MCP_URL = os.getenv("NOTIFICATION_MCP_URL", "http://127.0.0.1:8002/mcp")


# ---------------------- МОДЕЛИ ДАННЫХ ----------------------


class ProcurementItem(BaseModel):
    sku: str = Field(..., description="Краткий SKU / тип товара, например 'hoodie', 't-shirt', 'mug'")
    quantity: int = Field(..., description="Количество единиц")
    max_unit_price: Optional[float] = Field(
        default=None,
        description="Максимальная цена за единицу, если указана пользователем",
    )


class ParsedRequest(BaseModel):
    target_currency: str = Field(..., description="Целевая валюта для итоговой суммы, например 'EUR'")
    budget: Optional[float] = Field(default=None, description="Общий бюджет, если указан")
    webhook_url: Optional[HttpUrl] = Field(default=None, description="Вебхук для отправки плана, если указан")
    items: List[ProcurementItem]


@dataclass
class Totals:
    currency: str
    total_net: float
    total_items: int


class ToolInvocation(TypedDict, total=False):
    name: str
    args: Dict[str, Any]
    result: Any


# ---------------------- ПРОМПТЫ LLM ----------------------


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
ДЛЯ КАТАЛОГА PRINTFUL.

Правила для items[].sku:

1) Используй только базовые английские названия типов товаров:
   - "hoodie"          — худи, толстовки с капюшоном;
   - "t-shirt"         — футболки, лонгсливы;
   - "mug"             — кружки, чашки;
   - "sweatshirt"      — свитшоты без капюшона;
   - "tote bag"        — шопперы, сумки;
   - "cap"             — кепки, бейсболки;
   - "hat"             — шапки;
   - "sticker"         — стикеры, наклейки;
   - "notebook"        — блокноты;
   - "backpack"        — рюкзаки;
   - "phone case"      — чехлы для телефонов.

2) НЕ используй лишние слова в sku:
   - НЕЛЬЗЯ: "unisex hoodie", "black hoodie", "conference hoodie".
   - НУЖНО:  "hoodie", "t-shirt", "mug" и т.п.

3) Маппинг с русского:
   - "худи", "толстовки", "толстовка", "кофты с капюшоном" → "hoodie"
   - "футболки", "футболка", "лонгсливы" → "t-shirt"
   - "кружки", "чашки" → "mug"
   - "свитшоты" → "sweatshirt"
   - "шопперы", "сумки" → "tote bag"
   - "кепки", "бейсболки" → "cap"
   - "шапки" → "hat"
   - "стикеры", "наклейки" → "sticker"
   - "блокноты" → "notebook"
   - "рюкзаки" → "backpack"

4) Важно: никакие характеристики (цвет, размер, логотип) внутрь sku НЕ добавляй.
   Они остаются только в исходном тексте пользователя.

Остальные поля:

- items[].quantity — целое число.
- items[].max_unit_price — если есть ограничение на цену за штуку, иначе null.
- budget — общий бюджет, если указан, иначе null.
- target_currency — "EUR", "USD", "RUB" и т.п.
  Если явно не сказано — выбери наиболее естественную валюту из контекста.
- webhook_url — URL, если пользователь его указал, иначе null.

Отвечай ТОЛЬКО валидным JSON по этой схеме, без комментариев и лишнего текста.
"""


SUMMARIZE_PLAN_SYSTEM_PROMPT = """
Ты — помощник по закупкам для бизнеса.

Тебе дан:
- текстовый запрос пользователя;
- JSON-план закупки (request, supplier_offers, totals_* и т.п.).

Твоя задача — кратко, по-человечески объяснить:
- какие товары и в каком количестве будут закуплены;
- у какого поставщика и по какой ориентировочной цене;
- общую стоимость (в валюте поставщика и, если есть, в целевой валюте);
- упомянуть, если какие-то позиции не удалось подобрать.

Пиши по-русски, структурировано, без JSON в ответе.
"""


TOOLS_AGENT_SYSTEM_PROMPT = """
Ты — AI-агент по закупкам мерча, умеющий вызывать внешние инструменты (tools)
через MCP-сервера.

У тебя есть три инструмента:

1) supplier_get_offers(items, max_suppliers_per_item)
   → подбирает офферы поставщика (Printful) по списку товаров и возвращает JSON
   с полями:
     - currency (например, "USD")
     - items[...]
     - total_min_cost (минимальная суммарная стоимость)
     - unavailable_skus
     - resolved_variants (sku -> catalog_variant_id)

2) fx_convert_amount(amount, base, quote)
   → конвертирует сумму amount из валюты base в валюту quote и возвращает JSON:
     {
       "base": "USD",
       "quote": "EUR",
       "amount_base": ...,
       "amount_quote": ...,
       "rate": ...,
       "provider": "...",
       "fallback_used": false/true,
       "warning": ...,
       "raw": {...}
     }

3) notify_send_plan(url, plan)
   → может отправить произвольный JSON-план по HTTP-вебхуку.
     (Этот инструмент можно вызывать после того, как у тебя есть финальный план.)

Твоя ЗАДАЧА в режиме tools-agent:

1) Понять запрос пользователя: какие товары, в каком количестве, бюджет, целевая валюта,
   вебхук для отправки плана.

2) Использовать инструменты:
   - supplier_get_offers — чтобы получить предложения от поставщика.
   - fx_convert_amount — чтобы пересчитать общую сумму в целевую валюту, если она
     отличается от валюты поставщика.
   - notify_send_plan — по желанию, чтобы отправить план на указанный вебхук.

3) Когда ты закончишь вызывать инструменты и у тебя будет вся информация,
   СФОРМИРУЙ ФИНАЛЬНЫЙ JSON-ПЛАН СЛЕДУЮЩЕГО ВИДА (СТРОГИЙ JSON, БЕЗ ТЕКСТОВЫХ
   КОММЕНТАРИЕВ):

{
  "request": {
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
  },
  "supplier_offers": { ... JSON как вернул supplier_get_offers ... },
  "totals_supplier_currency": {
    "currency": "USD",
    "total_net": 123.45,
    "total_items": 50
  },
  "totals_target_currency": {
    "currency": "EUR",
    "total_net": 115.67,
    "total_items": 50
  },
  "fx": { ... JSON как вернул fx_convert_amount ... } или null,
  "webhook_result": { ... JSON как вернул notify_send_plan ... } или null
}

Пояснения:

- В "request" положи структурированное представление запроса пользователя.
- В "supplier_offers" положи последний результат вызова supplier_get_offers.
- totals_supplier_currency:
    - currency = supplier_offers.currency
    - total_net = supplier_offers.total_min_cost
    - total_items = сумма quantity по всем позициям.
- totals_target_currency:
    - если целевая валюта равна валюте поставщика, просто скопируй total_net
      и currency, total_items тоже копируется;
    - если отличается — используй amount_quote и quote из результата fx_convert_amount.
- fx — JSON из fx_convert_amount; если конвертация не нужна — поставь null.
- webhook_result — результат notify_send_plan (если вызывался), иначе null.

ТВОЙ ФИНАЛЬНЫЙ ОТВЕТ БЕЗ tool_calls ДОЛЖЕН БЫТЬ СТРОГИМ JSON ПО ЭТОЙ СХЕМЕ,
без префиксов/суффиксов, без Markdown, без комментариев.
"""


# ---------------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------------------


def _history_to_messages(history: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, str]]:
    messages: List[Dict[str, str]] = []
    if not history:
        return messages
    for msg in history:
        role = msg.get("role")
        content = msg.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            messages.append({"role": role, "content": content})
    return messages


def _extract_structured_from_mcp_result(result: Any) -> Any:
    """
    Аккуратно достаём JSON из результата MCP-инструмента.
    Поддерживает:
    - result.structuredContent / result.structured_content
    - JSON внутри content[0].text.
    """
    # 1) structuredContent / structured_content
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        inner = sc.get("structuredContent") or sc.get("structured_content")
        if isinstance(inner, dict):
            return inner
        return sc

    sc2 = getattr(result, "structured_content", None)
    if isinstance(sc2, dict):
        inner = sc2.get("structuredContent") or sc2.get("structured_content")
        if isinstance(inner, dict):
            return inner
        return sc2

    # 2) content[...].text как JSON
    contents = getattr(result, "content", None) or []
    for c in contents:
        text = getattr(c, "text", None)
        if not text:
            continue
        try:
            outer = json.loads(text)
        except Exception:
            continue
        if isinstance(outer, dict):
            inner = outer.get("structuredContent") or outer.get("structured_content")
            if isinstance(inner, dict):
                return inner
            return outer

    # 3) Вернуть текст первого content, если вообще ничего не получилось
    if contents:
        t0 = getattr(contents[0], "text", None)
        if t0:
            return t0

    return {}


async def _call_mcp_tool_json(
    mcp_url: str,
    tool_name: str,
    args: Dict[str, Any],
) -> Any:
    """
    Вызвать MCP-tool и вернуть JSON/строку из его structuredContent/content.

    Никогда не бросает исключения наружу — в случае ошибки вернёт строку
    вида "Error calling tool 'name': ..." (чтобы агент не падал).
    """
    logger.info("Calling MCP tool %s on %s with args=%r", tool_name, mcp_url, args)
    try:
        async with streamablehttp_client(mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, args)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error calling MCP tool %s: %s", tool_name, exc)
        return f"Error calling tool '{tool_name}': {exc}"

    payload = _extract_structured_from_mcp_result(result)
    logger.info("%s response: %r", tool_name, payload)
    return payload


async def call_supplier_mcp(
    items: List[Dict[str, Any]],
    max_suppliers_per_item: int = 3,
) -> Any:
    args = {
        "items": items,
        "max_suppliers_per_item": max_suppliers_per_item,
    }
    return await _call_mcp_tool_json(SUPPLIER_MCP_URL, "get_offers_for_items", args)


async def call_fx_mcp(amount: float, base: str, quote: str) -> Any:
    args = {
        "amount": float(amount),
        "base": str(base),
        "quote": str(quote),
    }
    return await _call_mcp_tool_json(FX_MCP_URL, "convert_amount", args)


async def send_plan_webhook(url: str, plan: Dict[str, Any]) -> Any:
    args = {
        "url": url,
        "plan": plan,
    }
    return await _call_mcp_tool_json(NOTIFICATION_MCP_URL, "send_procurement_plan_webhook", args)


def _aggregate_totals_from_supplier_response(
    supplier_resp: Dict[str, Any] | str,
) -> Totals:
    """
    Посчитать общую сумму и количество по ответу supplier-pricing-mcp.

    Поддерживает два формата:
    1) Уже "чистый" словарь:
       {"currency": "...", "items": [...], "total_min_cost": ...}

    2) Обёртка MCP:
       {"_meta":..., "content":[...], "structuredContent": {...}, "isError":...}
    """
    if isinstance(supplier_resp, str):
        return Totals(currency="USD", total_net=0.0, total_items=0)

    if not isinstance(supplier_resp, dict):
        return Totals(currency="USD", total_net=0.0, total_items=0)

    # Разворачиваем structuredContent, если это обёртка
    if "structuredContent" in supplier_resp and isinstance(
        supplier_resp["structuredContent"],
        dict,
    ):
        supplier_resp = supplier_resp["structuredContent"]
    elif "structured_content" in supplier_resp and isinstance(
        supplier_resp["structured_content"],
        dict,
    ):
        supplier_resp = supplier_resp["structured_content"]

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


# ---------------------- LLM: ПАРСИНГ ЗАПРОСА ----------------------


async def parse_user_request(
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> ParsedRequest:
    messages = [{"role": "system", "content": PARSE_REQUEST_SYSTEM_PROMPT}]
    messages.extend(_history_to_messages(history))
    messages.append({"role": "user", "content": user_text})

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.0,
    )
    content = resp.choices[0].message.content or ""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse JSON from LLM: %s; raw content=%r", exc, content)
        # минимальный fallback
        return ParsedRequest(
            target_currency="USD",
            budget=None,
            webhook_url=None,
            items=[],
        )

    try:
        return ParsedRequest.model_validate(data)
    except ValidationError as exc:
        logger.error("ParsedRequest validation error: %s; data=%r", exc, data)
        return ParsedRequest(
            target_currency=data.get("target_currency", "USD"),
            budget=data.get("budget"),
            webhook_url=None,
            items=[
                ProcurementItem(
                    sku="fallback_item",
                    quantity=1,
                    max_unit_price=None,
                )
            ],
        )


# ---------------------- LLM: САММАРИ ДЛЯ ПОЛЬЗОВАТЕЛЯ ----------------------


async def summarize_plan_for_user(
    plan: Dict[str, Any],
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": SUMMARIZE_PLAN_SYSTEM_PROMPT},
    ]
    messages.extend(_history_to_messages(history))

    messages.append(
        {
            "role": "user",
            "content": (
                "Вот исходный запрос пользователя (на русском):\n\n"
                f"{user_text}\n\n"
                "И вот JSON-план закупки (не показывай его целиком в ответе):\n\n"
                f"{json.dumps(plan, ensure_ascii=False)}"
            ),
        }
    )

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.2,
    )
    return resp.choices[0].message.content or ""


# ---------------------- TOOLS для OpenAI (режим tools-agent) ----------------------


TOOLS_SPEC: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "supplier_get_offers",
            "description": "Получить офферы поставщика по списку позиций закупки через supplier-pricing-mcp.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "Позиции закупки: sku, quantity, max_unit_price.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "sku": {"type": "string"},
                                "quantity": {"type": "integer"},
                                "max_unit_price": {
                                    "type": ["number", "null"],
                                    "description": "Максимальная цена за единицу или null.",
                                },
                            },
                            "required": ["sku", "quantity"],
                        },
                    },
                    "max_suppliers_per_item": {
                        "type": "integer",
                        "description": "Максимальное число офферов на позицию.",
                        "default": 3,
                    },
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fx_convert_amount",
            "description": "Конвертировать сумму между валютами через fx-rates-mcp.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number"},
                    "base": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["amount", "base", "quote"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notify_send_plan",
            "description": "Отправить произвольный JSON-план на вебхук через notification-mcp.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "plan": {
                        "type": "object",
                        "description": "JSON-план, который нужно отправить.",
                    },
                },
                "required": ["url", "plan"],
            },
        },
    },
]


async def _run_tools_agent_dialog(
    parsed_request: ParsedRequest,
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
    max_steps: int = 8,
) -> Tuple[List[ToolInvocation], str]:
    """
    Запускает LLM в режиме tools-agent: модель сама решает, когда вызывать
    supplier_get_offers / fx_convert_amount / notify_send_plan.

    Возвращает:
    - список вызовов инструментов (tool_trace);
    - финальное текстовое сообщение ассистента (final_assistant_message).
    """
    tool_trace: List[ToolInvocation] = []

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": TOOLS_AGENT_SYSTEM_PROMPT},
    ]
    messages.extend(_history_to_messages(history))

    # Дадим модели уже распарсенный запрос, чтобы она не изобретала JSON с нуля
    messages.append(
        {
            "role": "assistant",
            "content": (
                "Я уже распарсил запрос пользователя в структуру (request):\n\n"
                f"{parsed_request.model_dump_json(indent=2, ensure_ascii=False)}\n\n"
                "Используй эту структуру как основу при вызовах инструментов и в финальном JSON-плане."
            ),
        }
    )

    messages.append(
        {
            "role": "user",
            "content": user_text,
        }
    )

    final_assistant_message: str = ""

    for step in range(max_steps):
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            tools=TOOLS_SPEC,
            tool_choice="auto",
            temperature=0.1,
        )

        msg = resp.choices[0].message

        # Если модель хочет вызвать инструменты
        if msg.tool_calls:
            # Сохраняем assistant-сообщение с описанием tool_calls
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )

            # Выполняем каждый tool_call
            for tc in msg.tool_calls:
                name = tc.function.name
                args_str = tc.function.arguments or "{}"
                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError:
                    args = {}

                if name == "supplier_get_offers":
                    items = args.get("items") or []
                    max_suppliers = args.get("max_suppliers_per_item", 3)
                    result = await call_supplier_mcp(items, max_suppliers)
                elif name == "fx_convert_amount":
                    amount = float(args.get("amount", 0.0))
                    base = str(args.get("base", "USD"))
                    quote = str(args.get("quote", "USD"))
                    result = await call_fx_mcp(amount, base, quote)
                elif name == "notify_send_plan":
                    url = args.get("url")
                    plan = args.get("plan") or {}
                    result = await send_plan_webhook(str(url), plan) if url else {
                        "error": "notify_send_plan called without url"
                    }
                else:
                    result = {"error": f"Unknown tool {name}"}

                tool_trace.append(
                    ToolInvocation(
                        name=name,
                        args=args,
                        result=result,
                    )
                )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": name,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

            continue  # следующий шаг цикла: модель увидит ответы tool и решит, что дальше

        # Если инструментов больше нет — это финальный ответ ассистента
        final_assistant_message = msg.content or ""
        break

    return tool_trace, final_assistant_message


def _find_last_tool_result(
    tool_trace: List[ToolInvocation],
    name: str,
) -> Any:
    for inv in reversed(tool_trace):
        if inv.get("name") == name:
            return inv.get("result")
    return None


# ---------------------- СТАРЫЙ РЕЖИМ: PIPELINE ----------------------


async def build_procurement_plan(
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """
    Классический, детерминированный пайплайн:
    - LLM парсит запрос → ParsedRequest;
    - MCP supplier-pricing-mcp → офферы;
    - MCP fx-rates-mcp → конвертация;
    - MCP notification-mcp → отправка плана (если есть webhook_url);
    - собирается JSON-план.
    """
    logger.info("Building procurement plan (pipeline) for: %s", user_text)

    parsed = await parse_user_request(user_text, history=history)
    logger.info("Parsed user request: %r", parsed.model_dump(mode="json"))

    items_payload = [
        {
            "sku": it.sku,
            "quantity": it.quantity,
            "max_unit_price": it.max_unit_price,
        }
        for it in parsed.items
    ]

    supplier_resp = await call_supplier_mcp(items_payload, max_suppliers_per_item=3)
    totals_supplier = _aggregate_totals_from_supplier_response(supplier_resp)

    fx_resp: Any = None
    totals_target = Totals(
        currency=parsed.target_currency,
        total_net=totals_supplier.total_net,
        total_items=totals_supplier.total_items,
    )

    if totals_supplier.currency.upper() != parsed.target_currency.upper() and totals_supplier.total_net > 0:
        fx_resp = await call_fx_mcp(
            amount=totals_supplier.total_net,
            base=totals_supplier.currency,
            quote=parsed.target_currency,
        )
        if isinstance(fx_resp, dict):
            try:
                converted_total = float(fx_resp.get("amount_quote") or 0.0)
            except (TypeError, ValueError):
                converted_total = totals_supplier.total_net
            totals_target = Totals(
                currency=parsed.target_currency,
                total_net=converted_total,
                total_items=totals_supplier.total_items,
            )

    webhook_result: Any = None
    if parsed.webhook_url:
        webhook_result = await send_plan_webhook(
            str(parsed.webhook_url),
            plan={  # отправляем "почти готовый" план
                "request": parsed.model_dump(mode="json"),
                "supplier_offers": supplier_resp,
                "totals_supplier_currency": asdict(totals_supplier),
                "totals_target_currency": asdict(totals_target),
                "fx": fx_resp,
            },
        )

    plan: Dict[str, Any] = {
        "request": parsed.model_dump(mode="json"),
        "supplier_offers": supplier_resp,
        "totals_supplier_currency": asdict(totals_supplier),
        "totals_target_currency": asdict(totals_target),
        "fx": fx_resp,
        "webhook_result": webhook_result,
        "_meta": {
            "mode": "pipeline",
        },
    }
    return plan


# ---------------------- НОВЫЙ РЕЖИМ: TOOLS-AGENT ----------------------


async def build_procurement_plan_tools_agent(
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """
    Режим tools-agent:
    - LLM парсит запрос (как раньше) → ParsedRequest;
    - затем запускается диалог с моделью с tools=..., где она сама решает,
      когда вызывать supplier_get_offers / fx_convert_amount / notify_send_plan;
    - мы собираем трассу вызовов инструментов (tool_trace);
    - на основе результатов MCP-серверов считаем тоталы и собираем план
      (схема такая же, как в pipeline, чтобы web_app и README не ломались).
    """
    logger.info("Building procurement plan (tools-agent) for: %s", user_text)

    parsed = await parse_user_request(user_text, history=history)
    logger.info("Parsed user request (tools-agent): %r", parsed.model_dump(mode="json"))

    tool_trace, agent_final_message = await _run_tools_agent_dialog(
        parsed_request=parsed,
        user_text=user_text,
        history=history,
    )

    # 1) supplier_offers — берём из tool_trace, если LLM вызывал supplier_get_offers
    supplier_resp = _find_last_tool_result(tool_trace, "supplier_get_offers")

    # Если агент так и не вызвал supplier_get_offers — вызываем сами (fallback)
    if supplier_resp is None:
        logger.info("tools-agent: no supplier_get_offers tool call, fallback to direct MCP call")
        items_payload = [
            {
                "sku": it.sku,
                "quantity": it.quantity,
                "max_unit_price": it.max_unit_price,
            }
            for it in parsed.items
        ]
        supplier_resp = await call_supplier_mcp(items_payload, max_suppliers_per_item=3)
        tool_trace.append(
            ToolInvocation(
                name="supplier_get_offers (auto)",
                args={"items": items_payload, "max_suppliers_per_item": 3},
                result=supplier_resp,
            )
        )

    totals_supplier = _aggregate_totals_from_supplier_response(supplier_resp)

    # 2) fx — если агент вызывал fx_convert_amount с нужной парой, возьмём оттуда
    fx_resp = _find_last_tool_result(tool_trace, "fx_convert_amount")
    totals_target = Totals(
        currency=parsed.target_currency,
        total_net=totals_supplier.total_net,
        total_items=totals_supplier.total_items,
    )

    need_fx = (
        totals_supplier.currency.upper() != parsed.target_currency.upper()
        and totals_supplier.total_net > 0
    )

    if need_fx:
        if not isinstance(fx_resp, dict) or fx_resp.get("base", "").upper() != totals_supplier.currency.upper():
            # Агент не дал подходящий fx или вообще не вызвал — вызываем сами
            logger.info("tools-agent: fx_convert_amount missing or mismatched, fallback to direct MCP call")
            fx_resp = await call_fx_mcp(
                amount=totals_supplier.total_net,
                base=totals_supplier.currency,
                quote=parsed.target_currency,
            )
            tool_trace.append(
                ToolInvocation(
                    name="fx_convert_amount (auto)",
                    args={
                        "amount": totals_supplier.total_net,
                        "base": totals_supplier.currency,
                        "quote": parsed.target_currency,
                    },
                    result=fx_resp,
                )
            )

        if isinstance(fx_resp, dict):
            try:
                converted_total = float(fx_resp.get("amount_quote") or 0.0)
            except (TypeError, ValueError):
                converted_total = totals_supplier.total_net
            totals_target = Totals(
                currency=parsed.target_currency,
                total_net=converted_total,
                total_items=totals_supplier.total_items,
            )

    # 3) webhook — по-прежнему отправляем после расчёта полного плана
    webhook_result: Any = None
    if parsed.webhook_url:
        # План уже считаем полностью, чтобы отправить "настоящий" JSON
        tentative_plan = {
            "request": parsed.model_dump(mode="json"),
            "supplier_offers": supplier_resp,
            "totals_supplier_currency": asdict(totals_supplier),
            "totals_target_currency": asdict(totals_target),
            "fx": fx_resp,
        }
        webhook_result = await send_plan_webhook(
            str(parsed.webhook_url),
            plan=tentative_plan,
        )

    plan: Dict[str, Any] = {
        "request": parsed.model_dump(mode="json"),
        "supplier_offers": supplier_resp,
        "totals_supplier_currency": asdict(totals_supplier),
        "totals_target_currency": asdict(totals_target),
        "fx": fx_resp,
        "webhook_result": webhook_result,
        "_meta": {
            "mode": "tools-agent",
            "tool_trace": tool_trace,
            "agent_final_message": agent_final_message,
        },
    }
    return plan


# ---------------------- CLI ----------------------


async def _run_cli() -> None:
    parser = argparse.ArgumentParser(
        description="Smart Procurement Agent (MCP + OpenAI)",
    )
    parser.add_argument(
        "--mode",
        choices=["pipeline", "tools-agent"],
        default=os.getenv("AGENT_MODE", "pipeline"),
        help="Режим работы агента: 'pipeline' (детерминированный) или 'tools-agent' (LLM вызывает MCP-инструменты).",
    )
    args = parser.parse_args()

    mode = args.mode

    print("=== AI агент закупок (MCP + OpenAI) ===")
    print(f"Режим: {mode}")
    print(
        "Введи запрос, например:\n"
        "Нужно купить 10 ноутбуков до 80 000 ₽ за штуку и 5 мониторов, "
        "бюджет 500 000 ₽, покажи итог в EUR и отправь план в мой вебхук https://example.com/hook\n"
    )
    print("Вводи текст, пустая строка — конец:\n")

    history: List[Dict[str, str]] = []

    while True:
        lines: List[str] = []
        while True:
            try:
                line = input()
            except EOFError:
                line = ""
            if not line.strip():
                break
            lines.append(line)
        user_text = "\n".join(lines).strip()
        if not user_text:
            break

        print("\n\n>>> Строю план закупки...\n")

        if mode == "tools-agent":
            plan = await build_procurement_plan_tools_agent(user_text, history=history)
        else:
            plan = await build_procurement_plan(user_text, history=history)

        summary = await summarize_plan_for_user(plan, user_text, history=history)

        print(summary)
        print(">>> JSON-план:")
        print(json.dumps(plan, ensure_ascii=False, indent=2))

        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": summary})

    print("Завершено.")


if __name__ == "__main__":
    try:
        asyncio.run(_run_cli())
    except KeyboardInterrupt:
        sys.exit(0)
