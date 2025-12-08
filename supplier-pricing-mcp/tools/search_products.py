"""Инструмент для поиска товаров в публичном каталоге поставщика."""

from __future__ import annotations

import os
from typing import List

import httpx
from fastmcp import Context
from mcp.types import TextContent
from opentelemetry import trace
from pydantic import Field

from mcp.shared.exceptions import McpError, ErrorData
from mcp_instance import mcp
from metrics import API_CALLS
from .models import ProductSummary
from .utils import ToolResult, format_api_error

tracer = trace.get_tracer(__name__)


@mcp.tool(
    name="search_products",
    description=(
        "🔎 Поиск товаров по подстроке в названии через публичный каталог "
        "поставщика (например, Fake Store API)."
    ),
)
async def search_products(
    query: str = Field(
        ...,
        description=(
            "Поисковая строка по названию товара. "
            "Например: 'laptop', 'bag', 'shirt'."
        ),
    ),
    limit: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Максимальное количество возвращаемых товаров (1–50).",
    ),
    ctx: Context = None,
) -> ToolResult:
    """
    Ищет товары в публичном каталоге по подстроке в названии.

    Алгоритм:
    1. Загружает весь список товаров из внешнего API.
    2. Фильтрует по вхождению строки `query` в название товара (без учета регистра).
    3. Сортирует по цене по возрастанию.
    4. Возвращает не более `limit` результатов.

    Args:
        query: Поисковый запрос.
        limit: Ограничение по количеству результатов.
        ctx: Контекст MCP — используется для логов и прогресса.

    Returns:
        ToolResult с кратким текстом и структурированным списком товаров.

    Raises:
        McpError: при ошибках валидации, HTTP-ошибках или проблемах с API.
    """
    if ctx is None:
        # На платформе контекст будет передан автоматически,
        # но подстрахуемся на случай локального вызова.
        ctx = Context()

    with tracer.start_as_current_span("search_products") as span:
        span.set_attribute("query", query)
        span.set_attribute("limit", limit)

        await ctx.info("🚀 Запускаем поиск товаров в каталоге.")
        await ctx.report_progress(progress=0, total=100)

        API_CALLS.labels(
            service="supplier-pricing-mcp",
            endpoint="search_products",
            status="started",
        ).inc()

        cleaned_query = query.strip()
        if not cleaned_query:
            await ctx.error("❌ Пустой поисковый запрос.")
            raise McpError(
                ErrorData(
                    code=-32602,
                    message="Параметр 'query' не может быть пустым.",
                )
            )

        base_url = os.getenv("SUPPLIER_API_BASE", "https://fakestoreapi.com").rstrip("/")
        timeout = float(os.getenv("SUPPLIER_HTTP_TIMEOUT", "10.0"))
        currency = os.getenv("SUPPLIER_DEFAULT_CURRENCY", "USD")

        api_url = f"{base_url}/products"
        span.set_attribute("api_url", api_url)

        try:
            await ctx.info("📡 Загружаем каталог товаров.")
            await ctx.report_progress(progress=25, total=100)

            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(api_url)
                response.raise_for_status()
                products = response.json()

        except httpx.HTTPStatusError as e:
            error_text = format_api_error(
                e.response.text if e.response is not None else "",
                e.response.status_code if e.response is not None else 0,
            )
            await ctx.error(f"❌ HTTP ошибка при запросе каталога: {error_text}")
            span.set_attribute("error", "http_status_error")

            API_CALLS.labels(
                service="supplier-pricing-mcp",
                endpoint="search_products",
                status="error",
            ).inc()

            raise McpError(
                ErrorData(
                    code=-32603,
                    message=f"Не удалось получить каталог товаров.\n\n{error_text}",
                )
            )

        except Exception as e:
            await ctx.error(f"💥 Неожиданная ошибка при запросе каталога: {e}")
            span.set_attribute("error", str(e))

            API_CALLS.labels(
                service="supplier-pricing-mcp",
                endpoint="search_products",
                status="error",
            ).inc()

            raise McpError(
                ErrorData(
                    code=-32603,
                    message=f"Неожиданная ошибка при обращении к каталогу: {e}",
                )
            )

        await ctx.info("🔍 Фильтруем товары по запросу.")
        await ctx.report_progress(progress=60, total=100)

        needle = cleaned_query.lower()
        matches: List[ProductSummary] = []

        if isinstance(products, list):
            for product in products:
                title = str(product.get("title", "")).strip()
                if needle in title.lower():
                    summary = ProductSummary(
                        product_id=str(product.get("id")),
                        title=title or f"Product {product.get('id')}",
                        price=float(product.get("price", 0.0)),
                        currency=currency,
                        image_url=product.get("image") or None,
                    )
                    matches.append(summary)
        else:
            await ctx.warning("⚠️ Внешний API вернул неожиданный формат (не список).")

        matches.sort(key=lambda p: p.price)
        matches = matches[:limit]

        await ctx.info(f"✅ Найдено товаров: {len(matches)}.")
        await ctx.report_progress(progress=100, total=100)

        API_CALLS.labels(
            service="supplier-pricing-mcp",
            endpoint="search_products",
            status="success",
        ).inc()

        if matches:
            lines = [
                f"- {item.title} — {item.price} {item.currency}"
                for item in matches
            ]
            human_text = (
                f"Найдено товаров: {len(matches)}\n\n" + "\n".join(lines)
            )
        else:
            human_text = "По заданному запросу не найдено ни одного товара."

        span.set_attribute("results_count", len(matches))
        span.set_attribute("success", True)

        return ToolResult(
            content=[TextContent(type="text", text=human_text)],
            structured_content={
                "query": query,
                "limit": limit,
                "currency": currency,
                "items": [m.model_dump() for m in matches],
            },
            meta={"endpoint": "search_products"},
        )
