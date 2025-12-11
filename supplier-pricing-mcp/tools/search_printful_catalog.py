from __future__ import annotations

import logging
from typing import List, Optional

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, Field

from mcp_instance import mcp
from .printful_client import get_printful_client, PrintfulApiError

logger = logging.getLogger(__name__)


class PrintfulVariantInfo(BaseModel):
    variant_id: int
    size: Optional[str] = None
    color: Optional[str] = None
    name: str
    image: Optional[str] = None


class PrintfulProductInfo(BaseModel):
    product_id: int
    name: str
    brand: Optional[str] = None
    variant_count: Optional[int] = None
    variants: List[PrintfulVariantInfo] = Field(default_factory=list)


class PrintfulCatalogSearchResult(BaseModel):
    query: str
    products: List[PrintfulProductInfo]


def _format_catalog_search_human_readable(search: PrintfulCatalogSearchResult) -> str:
    lines: List[str] = []
    lines.append(
        f"Поиск по каталогу Printful по запросу: {search.query!r}\n"
    )

    if not search.products:
        lines.append("Ничего не найдено.")
        return "\n".join(lines)

    for product in search.products:
        lines.append(
            f"- product_id={product.product_id}, brand={product.brand!r}, "
            f"name={product.name!r}, variants_total={product.variant_count}"
        )
        if not product.variants:
            continue
        for v in product.variants:
            lines.append(
                f"  • variant_id={v.variant_id}, size={v.size!r}, "
                f"color={v.color!r}, name={v.name!r}"
            )

    return "\n".join(lines)


@mcp.tool(description="Поиск товаров и вариантов в каталоге Printful")
async def search_printful_catalog(
    query: str = Field(..., description="Подстрока в названии товара (например 'hoodie' или 't-shirt')"),
    limit_products: int = Field(10, ge=1, le=50, description="Максимум продуктов в ответе"),
    limit_variants_per_product: int = Field(5, ge=1, le=50, description="Максимум вариантов на продукт"),
) -> CallToolResult:
    """
    Тул для интерактивного поиска по каталогу:
    - ищем продукты по подстроке в name
    - для каждого продукта подтягиваем до limit_variants_per_product вариантов
    - возвращаем структуру с product_id / variant_id / size / color / name
    """
    try:
        client = get_printful_client()
    except Exception as exc:  # noqa: BLE001
        logger.error("Printful client not configured: %s", exc)
        empty = PrintfulCatalogSearchResult(query=query, products=[])
        text = (
            "Printful API не сконфигурирован (нет PRINTFUL_API_KEY), "
            "поиск по каталогу недоступен."
        )
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent=empty.model_dump(),
        )

    logger.info(
        "search_printful_catalog: query=%r, limit_products=%d, limit_variants_per_product=%d",
        query,
        limit_products,
        limit_variants_per_product,
    )

    try:
        products_raw = await client.search_products_by_name(
            query=query,
            limit_products=limit_products,
            scan_limit=max(limit_products * 10, 50),
        )
    except PrintfulApiError as exc:
        logger.error("Printful search_products_by_name error: %s", exc)
        empty = PrintfulCatalogSearchResult(query=query, products=[])
        text = f"Ошибка при обращении к Printful API: {exc}"
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent=empty.model_dump(),
        )

    products: List[PrintfulProductInfo] = []

    for p in products_raw:
        product_id = p.get("id")
        if not isinstance(product_id, int):
            continue

        name = p.get("name") or ""
        brand = p.get("brand")
        variant_count = p.get("variant_count")

        variants: List[PrintfulVariantInfo] = []
        try:
            variants_raw = await client.list_variants_for_product(
                product_id=product_id,
                limit_variants=limit_variants_per_product,
            )
        except PrintfulApiError as exc:
            logger.error("Printful list_variants_for_product error: %s", exc)
            variants_raw = []

        for v in variants_raw[:limit_variants_per_product]:
            vid = v.get("id")
            if not isinstance(vid, int):
                continue
            v_name = v.get("name") or ""
            size = v.get("size")
            color = v.get("color")
            image = v.get("image")
            variants.append(
                PrintfulVariantInfo(
                    variant_id=vid,
                    size=size,
                    color=color,
                    name=v_name,
                    image=image,
                )
            )

        products.append(
            PrintfulProductInfo(
                product_id=product_id,
                name=name,
                brand=brand,
                variant_count=variant_count,
                variants=variants,
            )
        )

    search_result = PrintfulCatalogSearchResult(
        query=query,
        products=products,
    )

    text_summary = _format_catalog_search_human_readable(search_result)

    return CallToolResult(
        content=[TextContent(type="text", text=text_summary)],
        structuredContent=search_result.model_dump(),
    )
