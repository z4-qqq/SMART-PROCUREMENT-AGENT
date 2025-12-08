"""Инструмент получения курса валюты base→quote."""

from __future__ import annotations

import os

import httpx
from fastmcp import Context
from mcp.types import TextContent
from opentelemetry import trace
from pydantic import Field

from mcp.shared.exceptions import McpError, ErrorData
from mcp_instance import mcp
from metrics import API_CALLS
from .models import ExchangeRateResponse
from .utils import ToolResult, _parse_float_env, format_api_error, require_base_currency

tracer = trace.get_tracer(__name__)


@mcp.tool(
    name="get_exchange_rate",
    description=(
        "💱 Получить курс валюты base→quote из публичного FX API. "
        "Используется для оценки стоимости закупок в целевой валюте."
    ),
)
async def get_exchange_rate(
    base: str = Field(
        default="USD",
        description="Базовая валюта, например 'USD'.",
    ),
    quote: str = Field(
        default="RUB",
        description="Целевая валюта, например 'RUB'.",
    ),
    ctx: Context = None,
) -> ToolResult:
    """Возвращает курс валюты base→quote.

    Для демо используется публичное FX API (например, exchangerate.host).

    Args:
        base: Базовая валюта (ISO 4217).
        quote: Целевая валюта (ISO 4217).
        ctx: Контекст MCP для логирования и прогресса.

    Returns:
        ToolResult: человекочитаемый текст и ExchangeRateResponse в structured_content.

    Raises:
        McpError: при ошибках параметров или HTTP-ошибках FX API.
    """
    if ctx is None:
        ctx = Context()

    base = base.upper().strip()
    quote = quote.upper().strip()

    with tracer.start_as_current_span("get_exchange_rate") as span:
        span.set_attribute("fx.base", base)
        span.set_attribute("fx.quote", quote)

        await ctx.info("🚀 Запрашиваем курс валют.")
        await ctx.report_progress(progress=0, total=100)

        API_CALLS.labels(
            service="fx-rates-mcp",
            endpoint="get_exchange_rate",
            status="started",
        ).inc()

        if len(base) != 3 or len(quote) != 3:
            await ctx.error("❌ Валюты должны быть трёхбуквенными кодами ISO 4217.")
            raise McpError(
                ErrorData(
                    code=-32602,
                    message="Параметры 'base' и 'quote' должны быть в формате ISO 4217 (3 буквы).",
                )
            )

        api_base = os.getenv("FX_API_BASE", "https://api.exchangerate.host/latest")
        timeout = _parse_float_env(
            os.getenv("FX_HTTP_TIMEOUT"),
            default=10.0,
            min_value=1.0,
            max_value=60.0,
        )

        await ctx.info("📡 Обращаемся к публичному FX API.")
        await ctx.report_progress(progress=40, total=100)

        params = {"base": base, "symbols": quote}

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(api_base, params=params)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as e:
            error_text = format_api_error(
                e.response.text if e.response is not None else "",
                e.response.status_code if e.response is not None else 0,
            )
            await ctx.error(f"❌ HTTP ошибка FX API: {error_text}")

            API_CALLS.labels(
                service="fx-rates-mcp",
                endpoint="get_exchange_rate",
                status="error",
            ).inc()

            raise McpError(
                ErrorData(
                    code=-32603,
                    message=f"Не удалось получить курс валют.\n\n{error_text}",
                )
            )
        except Exception as e:
            await ctx.error(f"💥 Неожиданная ошибка при обращении к FX API: {e}")

            API_CALLS.labels(
                service="fx-rates-mcp",
                endpoint="get_exchange_rate",
                status="error",
            ).inc()

            raise McpError(
                ErrorData(
                    code=-32603,
                    message=f"Неожиданная ошибка при обращении к FX API: {e}",
                )
            )

        await ctx.report_progress(progress=80, total=100)

        # Пример для exchangerate.host: { "base": "USD", "rates": { "RUB": 92.5 }, ... }
        rates = data.get("rates") or {}
        rate_value = rates.get(quote)

        if rate_value is None:
            await ctx.error("❌ FX API не вернул курс для указанной валютной пары.")
            raise McpError(
                ErrorData(
                    code=-32603,
                    message=f"Курс для пары {base}->{quote} не найден в ответе FX API.",
                )
            )

        result = ExchangeRateResponse(
            base=base,
            quote=quote,
            rate=float(rate_value),
        )

        await ctx.info("✅ Курс валют получен успешно.")
        await ctx.report_progress(progress=100, total=100)

        API_CALLS.labels(
            service="fx-rates-mcp",
            endpoint="get_exchange_rate",
            status="success",
        ).inc()

        human_text = (
            f"Курс валют: 1 {result.base} = {result.rate:.4f} {result.quote}"
        )
        span.set_attribute("fx.rate", result.rate)
        span.set_attribute("success", True)

        return ToolResult(
            content=[TextContent(type="text", text=human_text)],
            structured_content=result.model_dump(),
            meta={
                "endpoint": "get_exchange_rate",
                "api_base": api_base,
            },
        )
