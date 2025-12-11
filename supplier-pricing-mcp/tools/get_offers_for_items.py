"""Инструмент get_offers_for_items для supplier-pricing-mcp.

Каскад режимов работы:

1) REAL (Printful) — если:
   - USE_PRINTFUL=true (по умолчанию),
   - задан PRINTFUL_API_KEY,
   - и Printful API отвечает.

2) FAKESTORE FALLBACK — если:
   - Printful отключён флагом,
   - или нет PRINTFUL_API_KEY,
   - или Printful упал/недоступен по сети.
   В этом режиме используем публичный fakestoreapi.com как "поставщика".

3) DEMO FALLBACK — если:
   - даже fakestoreapi.com недоступен.
   Возвращаем структуру с нулевой стоимостью, но корректного формата,
   чтобы агент и UI не падали.

Переменные окружения:

- PRINTFUL_API_KEY   — ключ Printful (если нет, сразу идём в fakestore).
- PRINTFUL_API_BASE  — базовый URL Printful (по умолчанию https://api.printful.com).
- USE_PRINTFUL       — "true"/"false", чтобы принудительно отключить Printful.
- SUPPLIER_CURRENCY  — валюта поставщика (по умолчанию USD).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Tuple

import httpx

from mcp_instance import mcp

logger = logging.getLogger(__name__)

PRINTFUL_API_KEY = os.getenv("PRINTFUL_API_KEY")
PRINTFUL_API_BASE = os.getenv("PRINTFUL_API_BASE", "https://api.printful.com")
USE_PRINTFUL = os.getenv("USE_PRINTFUL", "true").lower() in ("1", "true", "yes", "y")
DEFAULT_CURRENCY = os.getenv("SUPPLIER_CURRENCY", "USD")


# ======================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ======================================================================


def _normalize_sku_to_query(sku: str) -> str:
    """Простейшее сопоставление внутреннего sku → текстовый запрос в каталог Printful."""
    raw = (sku or "").strip()
    normalized = raw.lower()

    aliases = {
        "unisex hoodie": "hoodie",
        "hoodie": "hoodie",
        "hoodie_unisex": "hoodie",
        "sweatshirt": "hoodie",
        "hoodie sweatshirt": "hoodie",

        "unisex t-shirt": "t-shirt",
        "t-shirt": "t-shirt",
        "tshirt": "t-shirt",
        "tee": "t-shirt",
        "tee shirt": "t-shirt",

        "mug": "mug",
        "coffee mug": "mug",
        "cup": "mug",
    }

    for key, query in aliases.items():
        if key in normalized:
            return query

    # По умолчанию ищем как есть
    return raw or "product"


async def _fetch_printful_product_and_variants(
    client: httpx.AsyncClient,
    query: str,
) -> Tuple[Dict[str, Any] | None, List[Dict[str, Any]]]:
    """Поиск товара в Printful по текстовому запросу + получение вариантов.

    Возвращает:
      (product_dict | None, [variants...])
    """
    headers = {"Authorization": f"Bearer {PRINTFUL_API_KEY}"}

    # 1) Ищем товары по запросу
    resp = await client.get(
        f"{PRINTFUL_API_BASE}/catalog/products",
        params={"search": query, "limit": 1},
        headers=headers,
        timeout=15.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    products = payload.get("result") or []
    if not products:
        return None, []

    product = products[0]
    product_id = product.get("id")

    # 2) Подтягиваем подробности + варианты
    resp2 = await client.get(
        f"{PRINTFUL_API_BASE}/catalog/products/{product_id}",
        headers=headers,
        timeout=15.0,
    )
    resp2.raise_for_status()
    payload2 = resp2.json()
    result = payload2.get("result") or {}
    variants = result.get("variants") or []

    return product, variants


def _build_demo_structured(
    items: List[Dict[str, Any]],
    reason: str,
) -> Dict[str, Any]:
    """Построить структуру ответа в финальном демо-режиме (без реального поставщика)."""
    item_blocks: List[Dict[str, Any]] = []
    unavailable_skus: List[str] = []

    for item in items:
        sku = str((item or {}).get("sku") or "").strip()
        if sku:
            unavailable_skus.append(sku)
        item_blocks.append(
            {
                "item": item,
                "offers": [],
            }
        )

    structured = {
        "currency": DEFAULT_CURRENCY,
        "items": item_blocks,
        "total_min_cost": 0.0,
        "unavailable_skus": unavailable_skus,
        "resolved_variants": {},
        "provider": "demo_fallback",
        "fallback_used": True,
        "reason": reason,
    }
    return structured


def _format_summary_text(structured: Dict[str, Any], reason_prefix: str | None = None) -> str:
    """Сформировать человекочитаемый текстовый отчёт по результатам/фоллбеку."""
    provider = structured.get("provider") or "unknown"
    fallback_used = bool(structured.get("fallback_used", False))

    if provider == "printful" and not fallback_used:
        title = "Результаты подбора офферов (Printful)"
    elif provider == "fakestoreapi":
        title = "Результаты подбора офферов (FakeStore API)"
    else:
        title = "Результаты подбора офферов (демо-поставщик)"

    lines: List[str] = [title, ""]

    if reason_prefix:
        lines.append(reason_prefix)
    if structured.get("reason"):
        lines.append(f"Причина: {structured['reason']}")
    if reason_prefix or structured.get("reason"):
        lines.append("")

    # Перебираем позиции
    for block in structured.get("items", []) or []:
        item = block.get("item") or {}
        sku = item.get("sku")
        qty = item.get("quantity")
        try:
            qty_int = int(qty or 0)
        except (TypeError, ValueError):
            qty_int = 0

        offers = block.get("offers") or []
        lines.append(f"- {sku} — запрошено {qty_int} шт., офферов: {len(offers)}")

        for offer in offers:
            supplier = offer.get("supplier") or provider
            unit_raw = offer.get("unit_price", 0.0)
            try:
                unit_price = float(unit_raw or 0.0)
            except (TypeError, ValueError):
                unit_price = 0.0
            curr = offer.get("currency") or structured.get("currency") or "USD"
            variant_id = offer.get("variant_id")
            desc = offer.get("description") or ""
            position_total = unit_price * qty_int
            lines.append(
                f"  • {supplier}: {unit_price:.2f} {curr} за штуку, "
                f"~{position_total:.2f} {curr} за позицию "
                f"(variant_id={variant_id}, desc={desc})"
            )

    # Итого
    lines.append("")
    total_raw = structured.get("total_min_cost", 0.0)
    try:
        total = float(total_raw or 0.0)
    except (TypeError, ValueError):
        total = 0.0
    curr = structured.get("currency") or "USD"
    lines.append(f"Минимальная суммарная стоимость по всем позициям: {total:.2f} {curr}")

    # Позиции без офферов
    unavailable = structured.get("unavailable_skus") or []
    if unavailable:
        lines.append("")
        lines.append("Позиции без офферов:")
        for sku in unavailable:
            lines.append(f"- {sku}")

    # Соответствие sku → variant_id
    resolved_variants = structured.get("resolved_variants") or {}
    if resolved_variants:
        lines.append("")
        lines.append("Соответствие sku → variant_id:")
        for sku, vid in resolved_variants.items():
            lines.append(f"- {sku} -> {vid}")

    return "\n".join(lines)


def _wrap_tool_result(structured: Dict[str, Any], text: str) -> Dict[str, Any]:
    """Обёртка в формат, с которым уже работает агент.

    Возвращаем envelope:
    { "_meta": ..., "content": [...], "structuredContent": {...}, "isError": False }
    """
    return {
        "_meta": None,
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ],
        "structuredContent": structured,
        "isError": False,
    }


# ======================================================================
# PRINTFUL: основной провайдер
# ======================================================================


async def _get_offers_from_printful(
    items: List[Dict[str, Any]],
    max_suppliers_per_item: int = 3,
) -> Dict[str, Any]:
    """Реальный режим: ходим в Printful API и подбираем варианты под каждую позицию."""
    if not PRINTFUL_API_KEY:
        raise RuntimeError("PRINTFUL_API_KEY не задан")

    item_blocks: List[Dict[str, Any]] = []
    unavailable_skus: List[str] = []
    resolved_variants: Dict[str, int] = {}
    total_min_cost: float = 0.0

    async with httpx.AsyncClient() as client:
        for item in items:
            sku = str((item or {}).get("sku") or "").strip()
            qty_raw = (item or {}).get("quantity", 0)
            try:
                qty = int(qty_raw or 0)
            except (TypeError, ValueError):
                qty = 0

            max_price_raw = (item or {}).get("max_unit_price", None)
            try:
                max_price = float(max_price_raw) if max_price_raw is not None else None
            except (TypeError, ValueError):
                max_price = None

            if not sku or qty <= 0:
                unavailable_skus.append(sku or "<empty>")
                item_blocks.append({"item": item, "offers": []})
                continue

            query = _normalize_sku_to_query(sku)
            try:
                product, variants = await _fetch_printful_product_and_variants(
                    client, query=query
                )
            except httpx.HTTPError as e:
                logger.warning(
                    "Ошибка HTTP при обращении к Printful для sku=%s, query=%s: %s",
                    sku,
                    query,
                    e,
                )
                unavailable_skus.append(sku)
                item_blocks.append({"item": item, "offers": []})
                continue

            if not product or not variants:
                unavailable_skus.append(sku)
                item_blocks.append({"item": item, "offers": []})
                continue

            variant = variants[0]
            variant_id = variant.get("id")
            variant_name = variant.get("name") or ""
            resolved_variants[sku] = variant_id

            unit_price_raw = variant.get("price")
            try:
                unit_price = float(unit_price_raw or 0.0)
            except (TypeError, ValueError):
                unit_price = 0.99  # демо-стоимость

            if max_price is not None and unit_price > max_price:
                unavailable_skus.append(sku)
                item_blocks.append({"item": item, "offers": []})
                continue

            position_total = unit_price * qty
            total_min_cost += position_total

            offer = {
                "supplier": "printful",
                "sku": sku,
                "unit_price": unit_price,
                "currency": DEFAULT_CURRENCY,
                "quantity_available": None,
                "variant_id": variant_id,
                "description": variant_name,
            }

            item_blocks.append(
                {
                    "item": item,
                    "offers": [offer][:max_suppliers_per_item],
                }
            )

    structured = {
        "currency": DEFAULT_CURRENCY,
        "items": item_blocks,
        "total_min_cost": round(total_min_cost, 2),
        "unavailable_skus": unavailable_skus,
        "resolved_variants": resolved_variants,
        "provider": "printful",
        "fallback_used": False,
        "reason": None,
    }
    return structured


# ======================================================================
# FAKESTOREAPI: fallback-провайдер
# ======================================================================


def _pick_best_fakestore_product(
    products: List[Dict[str, Any]],
    sku_query: str,
) -> Dict[str, Any] | None:
    """Простейший скоринг fakestore-продуктов под текстовый sku."""
    if not products:
        return None

    query = (sku_query or "").strip().lower()
    if not query:
        # Если запрос пустой — просто берём первый продукт
        return products[0]

    best = None
    best_score = 0.0

    for p in products:
        title = str(p.get("title") or "").lower()
        category = str(p.get("category") or "").lower()

        score = 0.0
        if query in title:
            score += 3.0
        if query in category:
            score += 2.0

        for word in query.split():
            if word and word in title:
                score += 1.0
            if word and word in category:
                score += 0.5

        if score > best_score:
            best_score = score
            best = p

    if best is None:
        # Если ничего не подошло — берём первый, чтобы хоть что-то вернуть
        return products[0]

    return best


async def _get_offers_from_fakestore(
    items: List[Dict[str, Any]],
    max_suppliers_per_item: int = 3,
    reason_from_printful: str | None = None,
) -> Dict[str, Any]:
    """Fallback-режим: используем fakestoreapi.com как поставщика."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://fakestoreapi.com/products",
            timeout=15.0,
        )
        resp.raise_for_status()
        products = resp.json()
        if not isinstance(products, list):
            raise RuntimeError("Неверный формат ответа fakestoreapi.com (ожидался список).")

    item_blocks: List[Dict[str, Any]] = []
    unavailable_skus: List[str] = []
    resolved_variants: Dict[str, int] = {}
    total_min_cost: float = 0.0

    for item in items:
        sku = str((item or {}).get("sku") or "").strip()
        qty_raw = (item or {}).get("quantity", 0)
        try:
            qty = int(qty_raw or 0)
        except (TypeError, ValueError):
            qty = 0

        max_price_raw = (item or {}).get("max_unit_price", None)
        try:
            max_price = float(max_price_raw) if max_price_raw is not None else None
        except (TypeError, ValueError):
            max_price = None

        if not sku or qty <= 0:
            unavailable_skus.append(sku or "<empty>")
            item_blocks.append({"item": item, "offers": []})
            continue

        product = _pick_best_fakestore_product(products, sku)
        if not product:
            unavailable_skus.append(sku)
            item_blocks.append({"item": item, "offers": []})
            continue

        product_id = product.get("id")
        title = product.get("title") or ""
        price_raw = product.get("price", 0.0)
        try:
            unit_price = float(price_raw or 0.0)
        except (TypeError, ValueError):
            unit_price = 0.0

        if max_price is not None and unit_price > max_price:
            unavailable_skus.append(sku)
            item_blocks.append({"item": item, "offers": []})
            continue

        position_total = unit_price * qty
        total_min_cost += position_total
        resolved_variants[sku] = int(product_id) if product_id is not None else -1

        offer = {
            "supplier": "fakestoreapi",
            "sku": sku,
            "unit_price": unit_price,
            "currency": DEFAULT_CURRENCY,  # fakestoreapi не отдаёт валюту, считаем USD
            "quantity_available": None,
            "variant_id": product_id,
            "description": title,
        }

        item_blocks.append(
            {
                "item": item,
                "offers": [offer][:max_suppliers_per_item],
            }
        )

    reason = reason_from_printful or "Printful отключен или недоступен, используется fakestoreapi.com."
    structured = {
        "currency": DEFAULT_CURRENCY,
        "items": item_blocks,
        "total_min_cost": round(total_min_cost, 2),
        "unavailable_skus": unavailable_skus,
        "resolved_variants": resolved_variants,
        "provider": "fakestoreapi",
        "fallback_used": True,
        "reason": reason,
    }
    return structured


# ======================================================================
# MCP TOOL
# ======================================================================


@mcp.tool()
async def get_offers_for_items(
    items: List[Dict[str, Any]],
    max_suppliers_per_item: int = 3,
) -> Dict[str, Any]:
    """Подбор предложений поставщика по списку позиций.

    Алгоритм:
      1. Если USE_PRINTFUL=true и есть PRINTFUL_API_KEY — пробуем Printful.
      2. Если Printful отключён/не сконфигурирован/упал — пробуем fakestoreapi.com.
      3. Если и fakestoreapi.com недоступен — отдаём демо-структуру с нулевой стоимостью.
    """
    printful_reason: str | None = None

    # 1. Пытаемся использовать Printful (если разрешено и сконфигурировано)
    if USE_PRINTFUL and PRINTFUL_API_KEY:
        try:
            structured = await _get_offers_from_printful(
                items, max_suppliers_per_item=max_suppliers_per_item
            )
            text = _format_summary_text(structured)
            return _wrap_tool_result(structured, text)
        except Exception as e:
            printful_reason = f"Printful API недоступно или вернуло ошибку: {e!r}"
            logger.exception("get_offers_for_items: %s", printful_reason)
    else:
        if not USE_PRINTFUL:
            printful_reason = "USE_PRINTFUL=false (Printful отключен через конфигурацию)."
            logger.warning("get_offers_for_items: %s", printful_reason)
        elif not PRINTFUL_API_KEY:
            printful_reason = "PRINTFUL_API_KEY не задан, обращение к Printful невозможно."
            logger.error("get_offers_for_items: %s", printful_reason)

    # 2. Пытаемся использовать fakestoreapi.com как fallback-поставщика
    try:
        structured = await _get_offers_from_fakestore(
            items,
            max_suppliers_per_item=max_suppliers_per_item,
            reason_from_printful=printful_reason,
        )
        text = _format_summary_text(structured)
        return _wrap_tool_result(structured, text)
    except Exception as e2:
        fakestore_reason = f"FakeStore API недоступно или вернуло ошибку: {e2!r}"
        logger.exception("get_offers_for_items (fakestore fallback): %s", fakestore_reason)

    # 3. Финальный демо-режим, если реально не достучались ни до Printful, ни до fakestoreapi
    combined_reason_parts = []
    if printful_reason:
        combined_reason_parts.append(printful_reason)
    combined_reason_parts.append("fakestoreapi.com недоступен или вернул ошибку.")
    reason = " ; ".join(combined_reason_parts)

    structured = _build_demo_structured(items, reason=reason)
    text = _format_summary_text(
        structured,
        reason_prefix="Работаем в финальном демо-режиме: реальный поставщик недоступен.",
    )
    return _wrap_tool_result(structured, text)
