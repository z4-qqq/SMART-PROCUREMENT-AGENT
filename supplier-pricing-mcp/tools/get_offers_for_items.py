"""Инструмент для подбора предложений поставщика по списку позиций."""

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
from .models import PurchaseItem, SupplierOffer, ItemOffers, BulkOffersResult
from .utils import ToolResult, format_api_error

tracer = trace.get_tracer(__name__)


@mcp.tool(
    name="get_offers_for_items",
    description=(
        "📦 Подбор лучших предложений поставщика по списку позиций закупки "
        "на основе публичного каталога."
    ),
)
async def get_offers_for_items(
    items: List[PurchaseItem] = Field(
        ...,
        description=(
            "Список позиций для закупки: sku, quantity, max_unit_price (опционально)."
        ),
    ),
    max_suppliers_per_item: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Максимальное количество офферов на одну позицию.",
    ),
    ctx: Context = None,
) -> ToolResult:
    """
    Подбирает предложения поставщика по каждой позиции закупки.

    Алгоритм:
    1. Загружает список товаров из публичного каталога.
    2. Для каждого PurchaseItem ищет товары, где sku входит в название.
    3. Строит SupplierOffer для каждого совпадения.
    4. Сортирует офферы по цене и обрезает до max_suppliers_per_item.
    5. Считает минимально возможную суммарную стоимость закупки.

    Args:
        items: Позиции закупки.
        max_suppliers_per_item: Ограничение по числу офферов для одной позиции.
        ctx: Контекст для логирования и прогресса.

    Returns:
        ToolResult с человекочитаемым резюме и BulkOffersResult в structured_content.

    Raises:
        McpError: при ошибках параметров или при обращении к внешнему API.
    """
    if ctx is None:
        ctx = Context()

    with tracer.start_as_current_span("get_offers_for_items") as span:
        span.set_attribute("items_count", len(items))
        span.set_attribute("max_suppliers_per_item", max_suppliers_per_item)

        await ctx.info("🚀 Запускаем подбор офферов для списка позиций.")
        await ctx.report_progress(progress=0, total=100)

        API_CALLS.labels(
            service="supplier-pricing-mcp",
            endpoint="get_offers_for_items",
            status="started",
        ).inc()

        if not items:
            await ctx.error("❌ Список items пуст.")
            raise McpError(
                ErrorData(
                    code=-32602,
                    message="Список 'items' не может быть пустым.",
                )
            )

        base_url = os.getenv("SUPPLIER_API_BASE", "https://fakestoreapi.com").rstrip("/")
        timeout = float(os.getenv("SUPPLIER_HTTP_TIMEOUT", "10.0"))
        currency = os.getenv("SUPPLIER_DEFAULT_CURRENCY", "USD")

        api_url = f"{base_url}/products"
        span.set_attribute("api_url", api_url)

        # Этап 1: загрузка каталога
        try:
            await ctx.info("📡 Этап 1/3: загрузка каталога товаров.")
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
                endpoint="get_offers_for_items",
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
                endpoint="get_offers_for_items",
                status="error",
            ).inc()

            raise McpError(
                ErrorData(
                    code=-32603,
                    message=f"Неожиданная ошибка при обращении к каталогу: {e}",
                )
            )

        if not isinstance(products, list):
            await ctx.error("❌ Внешний API вернул неожиданный формат (не список).")
            raise McpError(
                ErrorData(
                    code=-32603,
                    message="Каталог товаров имеет неожиданный формат (ожидался список).",
                )
            )

        # Этап 2: подбор офферов по каждой позиции
        await ctx.info("📄 Этап 2/3: подбор офферов по позициям.")
        await ctx.report_progress(progress=60, total=100)

        result_items: List[ItemOffers] = []
        unavailable_skus: List[str] = []
        total_min_cost = 0.0

        for item in items:
            query = item.sku.strip().lower()
            offers: List[SupplierOffer] = []

            for product in products:
                raw_title = str(product.get("title", ""))
                title = raw_title.strip()
                if not title:
                    continue

                if query in title.lower():
                    price = float(product.get("price", 0.0))
                    offer = SupplierOffer(
                        supplier_id="demo_supplier",
                        supplier_name="Demo Supplier API",
                        sku=item.sku,
                        external_product_id=str(product.get("id")),
                        unit_price=price,
                        currency=currency,
                        delivery_days=None,
                        product_url=product.get("image") or None,
                    )

                    if item.max_unit_price is not None and offer.unit_price > item.max_unit_price:
                        # Дороже лимита – пропускаем
                        continue

                    offers.append(offer)

            offers.sort(key=lambda o: o.unit_price)
            offers = offers[:max_suppliers_per_item]

            if not offers:
                unavailable_skus.append(item.sku)
            else:
                best = offers[0]
                total_min_cost += best.unit_price * item.quantity

            result_items.append(
                ItemOffers(
                    item=item,
                    offers=offers,
                )
            )

        bulk_result = BulkOffersResult(
            currency=currency,
            items=result_items,
            total_min_cost=total_min_cost,
            unavailable_skus=unavailable_skus,
        )

        # Этап 3: формирование человекочитаемого резюме
        await ctx.info("📝 Этап 3/3: формирование итогового резюме.")
        await ctx.report_progress(progress=100, total=100)

        API_CALLS.labels(
            service="supplier-pricing-mcp",
            endpoint="get_offers_for_items",
            status="success",
        ).inc()

        lines: List[str] = []
        for item_offers in result_items:
            item = item_offers.item
            if not item_offers.offers:
                lines.append(f"- {item.sku} — нет подходящих предложений.")
                continue

            best = item_offers.offers[0]
            total_for_item = best.unit_price * item.quantity
            lines.append(
                f"- {item.sku}: {item.quantity} шт. по {best.unit_price} "
                f"{best.currency} (минимум), всего {total_for_item:.2f} {best.currency}"
            )

        if lines:
            human_text = (
                "Результаты подбора офферов:\n\n" + "\n".join(lines) +
                f"\n\nМинимальная суммарная стоимость: {total_min_cost:.2f} {currency}"
            )
        else:
            human_text = "По всем запрошенным позициям не удалось найти ни одного предложения."

        if unavailable_skus:
            human_text += (
                "\n\nПозиции без офферов:\n- " + "\n- ".join(unavailable_skus)
            )

        span.set_attribute("unavailable_count", len(unavailable_skus))
        span.set_attribute("success", True)

        return ToolResult(
            content=[TextContent(type="text", text=human_text)],
            structured_content=bulk_result.model_dump(),
            meta={"endpoint": "get_offers_for_items"},
        )
