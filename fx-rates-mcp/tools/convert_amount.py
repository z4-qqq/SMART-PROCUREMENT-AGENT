"""Инструмент конвертации суммы из base в quote."""

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
from .models import ConvertAmountResponse
from .utils import ToolResult, _parse_float_env, format_api_error

tracer = trace.get_tracer(__name__)


@mcp.tool(
    name="convert_amount",
    description=(
        "🔁 Конвертирует сумму из валюты base в валюту quote, "
        "используя публичный FX API. Удобно для пересчёта бюджетов и цен."
    ),
)
async def convert_amount(
    amount: float = Field(
        ...,
        ge=0,
        description="Сумма для конвертации в базовой валюте.",
        examples=[100.0],
    ),
    base: str = Field(
        default="USD",
        description="Базовая валюта (например, 'USD').",
    ),
    quote: str = Field(
        default="RUB",
        description="Целевая валюта (например, 'RUB').",
    ),
    ctx: Context = None,
) -> ToolResult:
    """Конвертирует сумму из base в quote через публичное FX API.

    Args:
        amount: Сумма в базовой валюте.
        base: Базовая валюта (ISO 4217).
        quote: Целевая валюта (ISO 4217).
        ctx: Контекст для логирования и прогресса.

    Returns:
        ToolResult: человекочитаемый текст и ConvertAmountResponse в structured_content.

    Raises:
        McpError: при ошибках параметров или при обращении к FX API.
    """
    if ctx is None:
        ctx = Context()

    base = base.upper().strip()
    quote = quote.upper().strip()

    with tracer.start_as_current_span("convert_amount") as span:
        span.set_attribute("fx.base", base)
        span.set_attribute("fx.quote", quote)
        span.set_attribute("fx.amount_base", amount)

        await ctx.info("🚀 Запускаем конвертацию суммы.")
        await ctx.report_progress(progress=0, total=100)

        API_CALLS.labels(
            service="fx-rates-mcp",
            endpoint="convert_amount",
            status="started",
        ).inc()

        if amount < 0:
            await ctx.error("❌ Сумма для конвертации не может быть отрицательной.")
            raise McpError(
                ErrorData(
                    code=-32602,
                    message="Параметр 'amount' должен быть >= 0.",
                )
            )

        if len(base) != 3 or len(quote) != 3:
            await ctx.error("❌ Валюты должны быть трёхбуквенными кодами ISO 4217.")
            raise McpError(
                ErrorData(
                    code=-32602,
                    message=(
                        "Параметры 'base' и 'quote' должны быть в формате "
                        "ISO 4217 (3 буквы)."
                    ),
                )
            )

        api_base = os.getenv("FX_API_BASE", "https://api.exchangerate.host/latest")
        timeout = _parse_float_env(
            os.getenv("FX_HTTP_TIMEOUT"),
            default=10.0,
            min_value=1.0,
            max_value=60.0,
        )
        params = {"base": base, "symbols": quote}

        await ctx.info("💱 Получаем курс для конвертации через FX API.")
        await ctx.report_progress(progress=40, total=100)
        span.set_attribute("fx.api_base", api_base)

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
            await ctx.error(f"❌ HTTP ошибка FX API при конвертации: {error_text}")

            API_CALLS.labels(
                service="fx-rates-mcp",
                endpoint="convert_amount",
                status="error",
            ).inc()

            raise McpError(
                ErrorData(
                    code=-32603,
                    message=f"Не удалось получить курс валют для конвертации.\n\n{error_text}",
                )
            )
        except Exception as e:
            await ctx.error(f"💥 Неожиданная ошибка при обращении к FX API: {e}")

            API_CALLS.labels(
                service="fx-rates-mcp",
                endpoint="convert_amount",
                status="error",
            ).inc()

            raise McpError(
                ErrorData(
                    code=-32603,
                    message=f"Неожиданная ошибка при обращении к FX API: {e}",
                )
            )

        await ctx.report_progress(progress=80, total=100)

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

        rate = float(rate_value)
        amount_quote = amount * rate

        result = ConvertAmountResponse(
            base=base,
            quote=quote,
            rate=rate,
            amount_base=amount,
            amount_quote=amount_quote,
        )

        await ctx.info("✅ Конвертация успешно выполнена.")
        await ctx.report_progress(progress=100, total=100)

        API_CALLS.labels(
            service="fx-rates-mcp",
            endpoint="convert_amount",
            status="success",
        ).inc()

        span.set_attribute("fx.rate", rate)
        span.set_attribute("fx.amount_quote", amount_quote)
        span.set_attribute("success", True)

        human_text = (
            f"{amount:.2f} {base} = {amount_quote:.2f} {quote} "
            f"(курс {rate:.4f})"
        )

        return ToolResult(
            content=[TextContent(type="text", text=human_text)],
            structured_content=result.model_dump(),
            meta={"endpoint": "convert_amount"},
        )
