from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, Field

from mcp_instance import mcp
from .printful_client import get_printful_client, PrintfulApiError

logger = logging.getLogger(__name__)


class ItemRequest(BaseModel):
    """Описание позиции из агента закупок.

    ВАЖНО: теперь sku трактуем как ТЕКСТОВЫЙ ЗАПРОС в каталог Printful.
    Примеры:
      - "unisex hoodie black L"
      - "black hoodie L"
      - "premium t-shirt white M"
    """

    sku: str = Field(..., description="Текстовый запрос/идентификатор позиции")
    quantity: int = Field(..., ge=1, description="Запрошенное количество")
    max_unit_price: Optional[float] = Field(
        None, description="Максимальная цена за штуку в валюте поставщика (если есть лимит)"
    )


class SupplierOffer(BaseModel):
    """Оффер одного поставщика по позиции."""

    supplier: str = Field(..., description="Идентификатор поставщика, например 'printful'")
    sku: str
    unit_price: float
    currency: str
    quantity_available: Optional[int] = Field(
        None, description="Доступное количество (если известно)"
    )
    variant_id: Optional[int] = Field(
        None, description="Printful catalog variant ID"
    )
    description: Optional[str] = Field(
        None, description="Человекочитаемое описание варианта из каталога Printful"
    )


class ItemOffers(BaseModel):
    """Офферы по конкретной позиции запроса."""

    item: ItemRequest
    offers: List[SupplierOffer]


class BulkOffersResult(BaseModel):
    """Агрегированный результат по всем запрошенным позициям."""

    currency: str
    items: List[ItemOffers]
    total_min_cost: float
    unavailable_skus: List[str]
    resolved_variants: Dict[str, int] = Field(
        default_factory=dict,
        description="Динамически подобранный маппинг sku -> Printful catalog_variant_id",
    )


async def _resolve_variant_for_item(
    client,
    item: ItemRequest,
    max_products_to_scan: int = 30,
    max_variants_per_product: int = 10,
) -> Optional[Tuple[int, str]]:
    """
    Подбираем подходящий Printful variant_id для позиции item.

    Алгоритм (упрощённый):
      1. Берём item.sku как текст поиска (query).
      2. Ищем продукты по имени.
      3. Берём первый подходящий product.
      4. Берём первые несколько variants этого продукта.
      5. Выбираем первый variant, возвращаем (variant_id, variant_name).

    Если ничего не нашли — возвращаем None.
    """
    query = item.sku.replace("_", " ")
    logger.info("Resolving Printful variant for sku=%r via query=%r", item.sku, query)

    try:
        products = await client.search_products_by_name(
            query=query,
            limit_products=3,
            scan_limit=max_products_to_scan,
        )
    except PrintfulApiError as exc:
        logger.error("Printful search_products_by_name error for sku=%s: %s", item.sku, exc)
        return None

    if not products:
        logger.info("No products found in Printful catalog for query=%r", query)
        return None

    # Простая стратегия: берём первый продукт, потом первый вариант.
    for product in products:
        product_id = product.get("id")
        if not isinstance(product_id, int):
            continue

        try:
            variants = await client.list_variants_for_product(
                product_id=product_id,
                limit_variants=max_variants_per_product,
            )
        except PrintfulApiError as exc:
            logger.error(
                "Printful list_variants_for_product error for product_id=%s: %s",
                product_id,
                exc,
            )
            continue

        if not variants:
            continue

        v = variants[0]
        variant_id = v.get("id")
        if not isinstance(variant_id, int):
            continue
        variant_name = v.get("name") or ""
        logger.info(
            "Resolved sku=%r -> variant_id=%s (%s)", item.sku, variant_id, variant_name
        )
        return variant_id, variant_name

    logger.info("Unable to resolve variant for sku=%r via query=%r", item.sku, query)
    return None


def _format_offers_human_readable(result: BulkOffersResult) -> str:
    lines: List[str] = []
    lines.append("Результаты подбора офферов (Printful):\n")

    if not result.items:
        lines.append("Нет позиций в запросе.")
        return "\n".join(lines)

    for item_offers in result.items:
        item = item_offers.item
        lines.append(
            f"- {item.sku} — запрошено {item.quantity} шт., "
            f"офферов: {len(item_offers.offers)}"
        )
        for offer in item_offers.offers:
            total = offer.unit_price * item.quantity
            lines.append(
                f"  • {offer.supplier}: {offer.unit_price:.2f} {offer.currency} за штуку, "
                f"~{total:.2f} {offer.currency} за позицию "
                f"(variant_id={offer.variant_id}, desc={offer.description})"
            )

    lines.append(
        f"\nМинимальная суммарная стоимость по всем позициям: "
        f"{result.total_min_cost:.2f} {result.currency}"
    )

    if result.unavailable_skus:
        lines.append("\nПозиции без офферов:")
        for sku in result.unavailable_skus:
            lines.append(f"- {sku}")

    if result.resolved_variants:
        lines.append("\nСоответствие sku → Printful variant_id:")
        for sku, vid in result.resolved_variants.items():
            lines.append(f"- {sku} -> {vid}")

    return "\n".join(lines)


@mcp.tool(description="Получить офферы поставщика Printful для списка SKU (динамический поиск)")
async def get_offers_for_items(
    items: List[ItemRequest],
    max_suppliers_per_item: int = 3,  # сейчас есть только Printful
) -> CallToolResult:
    """
    Тул для агента закупок:
    - трактует item.sku как текстовый запрос в каталог Printful;
    - на лету ищет подходящий catalog_variant_id;
    - запрашивает цену;
    - фильтрует по max_unit_price;
    - возвращает агрегированный BulkOffersResult + resolved_variants.
    """
    logger.info(
        "get_offers_for_items called with %d items, max_suppliers_per_item=%d",
        len(items),
        max_suppliers_per_item,
    )

    try:
        client = get_printful_client()
    except Exception as exc:  # noqa: BLE001
        logger.error("Printful client not configured: %s", exc)
        unavailable = [item.sku for item in items]
        result = BulkOffersResult(
            currency="USD",
            items=[ItemOffers(item=item, offers=[]) for item in items],
            total_min_cost=0.0,
            unavailable_skus=unavailable,
            resolved_variants={},
        )
        text = (
            "Printful API не сконфигурирован (нет PRINTFUL_API_KEY). "
            "Все позиции помечены как недоступные.\n\n"
            + _format_offers_human_readable(result)
        )
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent=result.model_dump(),
        )

    offers_by_item: List[ItemOffers] = []
    total_cost = 0.0
    unavailable_skus: List[str] = []
    resolved_variants: Dict[str, int] = {}

    for item in items:
        # 1. Разрешаем sku -> variant_id через поиск по каталогу
        resolved = await _resolve_variant_for_item(client, item)
        if not resolved:
            offers_by_item.append(ItemOffers(item=item, offers=[]))
            unavailable_skus.append(item.sku)
            continue

        variant_id, variant_name = resolved
        resolved_variants[item.sku] = variant_id

        # 2. Получаем цену варианта
        try:
            unit_price, currency = await client.get_variant_price(variant_id)
        except PrintfulApiError as exc:
            logger.error(
                "Failed to fetch price for sku=%s, variant_id=%s: %s",
                item.sku,
                variant_id,
                exc,
            )
            offers_by_item.append(ItemOffers(item=item, offers=[]))
            unavailable_skus.append(item.sku)
            continue

        # 3. Фильтрация по max_unit_price
        if item.max_unit_price is not None and unit_price > item.max_unit_price:
            logger.info(
                "Price %.2f %s for sku=%s exceeds max_unit_price=%.2f; skipping offer",
                unit_price,
                currency,
                item.sku,
                item.max_unit_price,
            )
            offers_by_item.append(ItemOffers(item=item, offers=[]))
            unavailable_skus.append(item.sku)
            continue

        offer = SupplierOffer(
            supplier="printful",
            sku=item.sku,
            unit_price=unit_price,
            currency=currency,
            quantity_available=None,
            variant_id=variant_id,
            description=variant_name,
        )

        offers_by_item.append(ItemOffers(item=item, offers=[offer]))
        total_cost += unit_price * item.quantity

    # Валюта — из первого оффера, если есть
    currency = "USD"
    for io in offers_by_item:
        if io.offers:
            currency = io.offers[0].currency
            break

    result = BulkOffersResult(
        currency=currency,
        items=offers_by_item,
        total_min_cost=total_cost,
        unavailable_skus=unavailable_skus,
        resolved_variants=resolved_variants,
    )

    text_summary = _format_offers_human_readable(result)

    logger.info(
        "Returning BulkOffersResult: total_min_cost=%.2f %s", total_cost, currency
    )

    return CallToolResult(
        content=[TextContent(type="text", text=text_summary)],
        structuredContent=result.model_dump(),
    )
