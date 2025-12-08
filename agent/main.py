from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, HttpUrl, ValidationError

from openai import AsyncOpenAI
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# -------------------------------------------------------------
# Логирование и конфиг
# -------------------------------------------------------------

logger = logging.getLogger("procurement_agent")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

load_dotenv()


@dataclass
class Settings:
    model: str
    supplier_mcp_url: str
    fx_mcp_url: str
    notification_mcp_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            supplier_mcp_url=os.getenv("SUPPLIER_MCP_URL", "http://127.0.0.1:8000/mcp"),
            fx_mcp_url=os.getenv("FX_MCP_URL", "http://127.0.0.1:8001/mcp"),
            notification_mcp_url=os.getenv(
                "NOTIFICATION_MCP_URL", "http://127.0.0.1:8002/mcp"
            ),
        )


settings = Settings.from_env()

# -------------------------------------------------------------
# Модели домена
# -------------------------------------------------------------


class PurchaseItem(BaseModel):
    """Позиция закупки, как её должен понять агент."""

    sku: str = Field(..., description="Название или артикул товара")
    quantity: int = Field(..., gt=0, description="Количество единиц товара")
    max_unit_price: Optional[float] = Field(
        default=None,
        gt=0,
        description="Максимальная цена за единицу в целевой валюте (опционально)",
    )


class ParsedRequest(BaseModel):
    """Распарсенный из LLM запрос пользователя."""

    target_currency: str = Field(
        "RUB",
        description="Целевая валюта для итогового плана (например, RUB/EUR/USD)",
    )
    budget: Optional[float] = Field(
        default=None,
        gt=0,
        description="Общий бюджет в целевой валюте (опционально)",
    )
    webhook_url: Optional[HttpUrl] = Field(
        default=None,
        description="URL вебхука для отправки готового плана (опционально)",
    )
    items: List[PurchaseItem]


class Totals(BaseModel):
    """Агрегированные итоги из supplier-pricing-mcp."""

    currency: str
    total_net: float
    total_items: int


# -------------------------------------------------------------
# Вызов MCP-серверов (streamable-http)
# -------------------------------------------------------------


async def _call_mcp_tool(server_url: str, tool_name: str, arguments: Dict[str, Any]) -> Any:
    """
    Вызов MCP-тулзы через streamable-http.

    На выходе — уже распакованный Python-объект (dict/list/...),
    а не сырой CallToolResult.
    """
    logger.info("Calling MCP tool %s on %s with args=%s", tool_name, server_url, arguments)

    async with streamablehttp_client(server_url) as (read, write, get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            session_id = get_session_id()
            if session_id is not None:
                logger.debug("MCP session ID: %s", session_id)

            result = await session.call_tool(tool_name, arguments=arguments)
            return _unwrap_tool_result(result)


def _unwrap_tool_result(result: Any) -> Any:
    """
    Аккуратно достаём полезные данные из CallToolResult.

    Приоритет:
    1) result.structured_content (BulkOffersResult / ConvertAmountResponse / WebhookResult и т.п.)
    2) JSON-контент в первой части result.content
       - если type == "json" → part.json
       - если type == "text" → пытаемся json.loads(part.text) и берём structured_content,
         если он там есть
    3) иначе возвращаем как есть.
    """
    # 1) structured_content — идеальный вариант
    structured = getattr(result, "structured_content", None)
    if structured not in (None, {}, []):
        return structured

    # 2) content[0]
    content = getattr(result, "content", None)
    if content:
        part = content[0]
        part_type = getattr(part, "type", None)

        # JSON-контент (для Content type="json")
        if part_type == "json":
            # У JSONContent поле .json — это уже Python-объект (dict/list/…)
            return getattr(part, "json", None)

        # Текстовый контент — часто в нём лежит JSON строкой
        if part_type == "text":
            text = getattr(part, "text", "")
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                    # Если это “обёртка” ToolResult — достаем structured_content
                    if isinstance(parsed, dict) and "structured_content" in parsed:
                        return parsed["structured_content"]
                    return parsed
                except Exception:
                    # Это просто текст, не JSON — отдадим как есть
                    return text

    # 3) Fallback — отдаём объект как есть (на крайний случай)
    return result


# -------------------------------------------------------------
# LLM (OpenAI)
# -------------------------------------------------------------

_llm_client: Optional[AsyncOpenAI] = None


def get_llm_client() -> AsyncOpenAI:
    """
    Ленивая инициализация клиента OpenAI.

    OPENAI_API_KEY читается из окружения.
    При необходимости можно задать OPENAI_BASE_URL
    (например, если используешь OpenAI-совместимый прокси/клауд).
    """
    global _llm_client
    if _llm_client is None:
        _llm_client = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL") or None,
        )
    return _llm_client


async def parse_user_request(
    raw_text: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> ParsedRequest:
    """
    LLM → строго типизированный ParsedRequest по JSON-схеме.

    history — список сообщений в формате OpenAI-чата:
    [{"role": "user"|"assistant", "content": "..."}]

    Мы используем её, чтобы понимать фразы типа
    "добавь ещё 3 монитора" на основе предыдущего контекста.
    """
    client = get_llm_client()

    system_prompt = (
        "Ты AI-ассистент отдела закупок. "
        "Твоя задача — разобрать ТЕКУЩИЙ запрос пользователя с учётом "
        "всего контекста диалога и выдать обновлённый список позиций закупки.\n\n"
        "Верни СТРОГО один JSON-объект по такой схеме:\n"
        "{\n"
        '  "target_currency": "RUB|EUR|USD|...",\n'
        '  "budget": число или null,\n'
        '  "webhook_url": "https://..." или null,\n'
        '  "items": [\n'
        "    {\n"
        '      "sku": "строка",\n'
        '      "quantity": целое > 0,\n'
        '      "max_unit_price": число или null\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "items — это итоговый список после применения всех пожеланий "
        "из диалога (включая текущий запрос). Никакого текста вне JSON."
    )

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt}
    ]

    # Добавляем историю диалога (юзер + ассистент)
    if history:
        messages.extend(history)

    # Текущий запрос — последним
    messages.append({"role": "user", "content": raw_text})

    resp = await client.chat.completions.create(
        model=settings.model,
        response_format={"type": "json_object"},
        messages=messages,
    )

    content = resp.choices[0].message.content
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        logger.error("LLM вернул невалидный JSON: %s", content)
        raise RuntimeError(f"LLM returned invalid JSON: {e}") from e

    try:
        parsed = ParsedRequest.model_validate(data)
    except ValidationError as e:
        logger.error("JSON не проходит валидацию ParsedRequest: %s", e)
        raise

    logger.info("Parsed user request: %s", parsed.model_dump())
    return parsed


# -------------------------------------------------------------
# Бизнес-логика: сборка плана закупок
# -------------------------------------------------------------


def _aggregate_totals_from_supplier_response(supplier_resp: Dict[str, Any]) -> Totals:
    """
    Используем формат BulkOffersResult из supplier-pricing-mcp:

    {
      "currency": "USD",
      "items": [
        {
          "item": { "sku": "...", "quantity": 10, "max_unit_price": ... },
          "offers": [ ... ]
        },
        ...
      ],
      "total_min_cost": 1234.56,
      "unavailable_skus": [...]
    }
    """
    currency = supplier_resp.get("currency") or "RUB"
    items = supplier_resp.get("items", [])
    total_min_cost = float(supplier_resp.get("total_min_cost") or 0.0)

    total_items = 0
    for item_entry in items:
        item = item_entry.get("item") or {}
        qty = item.get("quantity") or 0
        try:
            total_items += int(qty)
        except (TypeError, ValueError):
            continue

    return Totals(
        currency=currency,
        total_net=round(total_min_cost, 2),
        total_items=total_items,
    )


async def build_procurement_plan(
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """
    Главный e2e-пайплайн:

    1) LLM → ParsedRequest (с учётом истории диалога)
    2) supplier-pricing-mcp.get_offers_for_items
    3) fx-rates-mcp.convert_amount (если нужна другая валюта)
    4) notification-mcp.send_procurement_plan_webhook (если есть webhook_url)

    history — список сообщений в формате OpenAI-чата.
    """
    # 1. Парсим запрос пользователя с контекстом
    parsed = await parse_user_request(user_text, history=history)

    # 2. supplier-pricing-mcp: подбор офферов
    supplier_args = {
        "items": [
            {
                "sku": item.sku,
                "quantity": item.quantity,
                "max_unit_price": item.max_unit_price,
            }
            for item in parsed.items
        ],
        "max_suppliers_per_item": 3,
    }

    supplier_resp = await _call_mcp_tool(
        settings.supplier_mcp_url,
        "get_offers_for_items",
        supplier_args,
    )
    logger.info("supplier-pricing-mcp response: %s", supplier_resp)

    totals_supplier = _aggregate_totals_from_supplier_response(supplier_resp)

    # Превращаем Pydantic-модели в JSON-дружественные dict'ы
    request_data = parsed.model_dump(mode="json")
    totals_supplier_data = totals_supplier.model_dump(mode="json")

    plan: Dict[str, Any] = {
        "request": request_data,
        "supplier_offers": supplier_resp,
        "totals_supplier_currency": totals_supplier_data,
    }

    # 3. Если целевая валюта отличается — конвертируем через fx-rates-mcp
    # Но только если есть что конвертировать (total_net > 0)
    if (
        parsed.target_currency.upper() != totals_supplier.currency.upper()
        and totals_supplier.total_net > 0
    ):
        fx_args = {
            "amount": totals_supplier.total_net,
            "base": totals_supplier.currency,
            "quote": parsed.target_currency,
        }

        fx_resp = await _call_mcp_tool(
            settings.fx_mcp_url,
            "convert_amount",
            fx_args,
        )
        logger.info("fx-rates-mcp response: %s", fx_resp)

        if isinstance(fx_resp, dict) and "amount_quote" in fx_resp:
            converted_total = float(fx_resp.get("amount_quote") or 0.0)
            plan["totals_target_currency"] = {
                "currency": fx_resp.get("quote", parsed.target_currency),
                "total_net": round(converted_total, 2),
            }
            plan["fx"] = fx_resp
        else:
            # fx-rates-mcp вернул ошибку или неожиданный формат — не падаем,
            # просто оставляем суммы в валюте поставщика.
            logger.error(
                "fx-rates-mcp вернул не dict или без поля amount_quote: %r", fx_resp
            )
            plan["totals_target_currency"] = totals_supplier_data
    else:
        # либо валюты совпадают, либо сумма 0
        # если сумма 0 — просто считаем, что в целевой валюте тоже 0
        if totals_supplier.total_net == 0:
            plan["totals_target_currency"] = {
                "currency": parsed.target_currency,
                "total_net": 0.0,
            }
        else:
            plan["totals_target_currency"] = totals_supplier_data

    # Проверка бюджета (если задан)
    if parsed.budget is not None:
        within_budget = (
            plan["totals_target_currency"]["total_net"] <= parsed.budget
        )
        plan["budget"] = {
            "value": parsed.budget,
            "currency": parsed.target_currency,
            "within_budget": within_budget,
        }

    # 4. notification-mcp: отправляем вебхук, если есть
    if parsed.webhook_url:
        notif_args = {
            "url": str(parsed.webhook_url),
            "plan": plan,
        }
        notification_resp = await _call_mcp_tool(
            settings.notification_mcp_url,
            "send_procurement_plan_webhook",
            notif_args,
        )
        logger.info("notification-mcp response: %s", notification_resp)
        plan["notification"] = notification_resp

    return plan


# -------------------------------------------------------------
# Краткое человеческое резюме через LLM
# -------------------------------------------------------------


async def summarize_plan_for_user(
    plan: Dict[str, Any],
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    """
    Берём JSON-план, исходный текст текущего запроса и историю диалога,
    просим LLM дать короткое бизнес-резюме.
    """
    client = get_llm_client()

    system_prompt = (
        "Ты ассистент отдела закупок. "
        "На вход тебе дают историю диалога, текущий запрос пользователя и "
        "рассчитанный JSON-план закупки.\n"
        "Сделай краткое, понятное бизнес-резюме:\n"
        "- перечисли основные позиции и количества;\n"
        "- укажи общую сумму в целевой валюте и сравни с бюджетом (если есть);\n"
        "- если план был отправлен на вебхук, упомяни это;\n"
        "- учитывай, что пользователь мог ссылаться на предыдущие сообщения.\n"
        "Не выводи сырой JSON, только понятный текст."
    )

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt}
    ]

    if history:
        messages.extend(history)

    plan_json = json.dumps(plan, ensure_ascii=False, indent=2)

    messages.append(
        {
            "role": "user",
            "content": (
                "Текущий запрос пользователя:\n"
                f"{user_text}\n\n"
                "Рассчитанный JSON-план (последняя версия):\n"
                f"{plan_json}"
            ),
        }
    )

    resp = await client.chat.completions.create(
        model=settings.model,
        messages=messages,
    )

    return resp.choices[0].message.content


# -------------------------------------------------------------
# CLI для ручного запуска
# -------------------------------------------------------------


async def _run_cli() -> None:
    print("=== AI агент закупок (MCP + OpenAI) ===")
    print(
        "Введи запрос, например:\n"
        "Нужно купить 10 ноутбуков до 80 000 ₽ за штуку и 5 мониторов, "
        "бюджет 500 000 ₽, покажи итог в EUR и отправь план в мой вебхук https://example.com/hook\n"
    )

    print("Вводи текст, пустая строка — конец:\n")
    lines: List[str] = []
    while True:
        line = input()
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

    print("\n>>> Краткое резюме:")
    summary = await summarize_plan_for_user(plan, user_text)
    print(summary)


if __name__ == "__main__":
    asyncio.run(_run_cli())
