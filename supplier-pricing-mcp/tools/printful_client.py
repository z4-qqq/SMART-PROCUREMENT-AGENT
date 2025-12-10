from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


class PrintfulApiError(Exception):
    """Ошибка при обращении к Printful API."""


@dataclass
class PrintfulClient:
    """
    Минимальный async-клиент для Printful Catalog API (V2).

    Используем:
      - GET /v2/catalog-products
      - GET /v2/catalog-products/{id}/catalog-variants
      - GET /v2/catalog-variants/{id}/prices
    """

    api_key: str
    base_url: str = "https://api.printful.com"
    default_currency: str = "USD"
    selling_region_name: Optional[str] = None
    timeout: float = 10.0

    @classmethod
    def from_env(cls) -> "PrintfulClient":
        api_key = os.getenv("PRINTFUL_API_KEY")
        if not api_key:
            raise RuntimeError(
                "PRINTFUL_API_KEY is required to query Printful API. "
                "Создай приватный token в Printful и задай его в окружении."
            )
        base_url = os.getenv("PRINTFUL_BASE_URL", "https://api.printful.com").rstrip("/")
        currency = os.getenv("PRINTFUL_CURRENCY", "USD")
        region = os.getenv("PRINTFUL_REGION") or None
        client = cls(
            api_key=api_key,
            base_url=base_url,
            default_currency=currency,
            selling_region_name=region,
        )
        logger.info(
            "PrintfulClient initialized: base_url=%s, currency=%s, region=%s",
            client.base_url,
            client.default_currency,
            client.selling_region_name,
        )
        return client

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "smart-procurement-agent/printful-mcp",
        }

    async def _get_json(self, url: str, params: Dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(url, headers=self._headers(), params=params)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise PrintfulApiError(
                    f"Printful API HTTP {exc.response.status_code}: {exc.response.text}"
                ) from exc
            except httpx.RequestError as exc:
                raise PrintfulApiError(f"Printful API request failed: {exc}") from exc
        return resp.json()

    @staticmethod
    def _extract_data(obj: Any) -> List[Dict[str, Any]]:
        """
        В Printful v2 ответы обычно:
          - {"data": [...], "paging": {...}, "_links": {...}}
        или просто: [...]
        """
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict) and "data" in obj:
            data = obj["data"]
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return [data]
        return []

    async def list_catalog_products(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/v2/catalog-products"
        params: Dict[str, Any] = {
            "limit": limit,
            "offset": offset,
        }
        if self.selling_region_name:
            params["selling_region_name"] = self.selling_region_name

        logger.info("GET %s params=%s", url, params)
        raw = await self._get_json(url, params=params)
        return self._extract_data(raw)

    async def search_products_by_name(
        self,
        query: str,
        limit_products: int = 10,
        scan_limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Грубый поиск по имени: забираем первые scan_limit продуктов и
        фильтруем по подстроке в name.
        """
        products = await self.list_catalog_products(limit=scan_limit, offset=0)
        q = query.lower()
        filtered = [
            p for p in products
            if q in str(p.get("name", "")).lower()
        ]
        return filtered[:limit_products]

    async def list_variants_for_product(
        self,
        product_id: int,
        limit_variants: int = 50,
    ) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/v2/catalog-products/{product_id}/catalog-variants"
        params: Dict[str, Any] = {"limit": limit_variants}
        logger.info("GET %s params=%s", url, params)
        raw = await self._get_json(url, params=params)
        return self._extract_data(raw)

    async def get_variant_price(self, variant_id: int) -> Tuple[float, str]:
        """
        Получить базовую цену для catalog-varianta из Printful.

        GET /v2/catalog-variants/{id}/prices
        Возвращаем (unit_price, currency) — минимальную цену из массива.
        """
        url = f"{self.base_url}/v2/catalog-variants/{variant_id}/prices"
        params: Dict[str, Any] = {}
        if self.selling_region_name:
            params["selling_region_name"] = self.selling_region_name
        if self.default_currency:
            params["currency"] = self.default_currency

        logger.info("GET %s params=%s", url, params)
        raw = await self._get_json(url, params=params)

        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, dict):
            raise PrintfulApiError(
                f"Unexpected prices payload for variant {variant_id}: {raw!r}"
            )

        currency = data.get("currency") or self.default_currency or "USD"
        prices: List[float] = []

        for tech in data.get("techniques", []) or []:
            val = tech.get("discounted_price") or tech.get("price")
            if val is None:
                continue
            try:
                prices.append(float(val))
            except (TypeError, ValueError):
                logger.warning("Failed to parse technique price %r", val)

        product = data.get("product") or {}
        for placement in product.get("placements", []) or []:
            val = placement.get("discounted_price") or placement.get("price")
            if val is None:
                continue
            try:
                prices.append(float(val))
            except (TypeError, ValueError):
                logger.warning("Failed to parse placement price %r", val)

        if not prices:
            raise PrintfulApiError(f"No price info returned for variant_id={variant_id}")

        unit_price = min(prices)
        return unit_price, currency


_client: Optional[PrintfulClient] = None


def get_printful_client() -> PrintfulClient:
    """Ленивая инициализация клиента Printful, чтобы шарить его между тулзами."""
    global _client
    if _client is None:
        _client = PrintfulClient.from_env()
    return _client
