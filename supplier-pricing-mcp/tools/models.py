"""Pydantic-модели для supplier-pricing-mcp."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


class PurchaseItem(BaseModel):
    """Запрос на закупку одной позиции."""

    sku: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description=(
            "Название или артикул товара. "
            "Например: 'laptop', 'wireless mouse', 'monitor 24 inch'."
        ),
        examples=["laptop", "wireless mouse"],
    )
    quantity: int = Field(
        ...,
        gt=0,
        le=100_000,
        description="Желаемое количество единиц товара.",
        examples=[10],
    )
    max_unit_price: Optional[float] = Field(
        None,
        gt=0,
        description="Ограничение на цену за единицу (опционально).",
        examples=[1500.0],
    )

    @field_validator("sku")
    @classmethod
    def _strip_sku(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("sku не может быть пустым")
        return cleaned


class ProductSummary(BaseModel):
    """Краткое описание товара из каталога."""

    product_id: str = Field(..., description="Идентификатор товара во внешнем каталоге.")
    title: str = Field(..., description="Название товара.")
    price: float = Field(..., gt=0, description="Цена за единицу.")
    currency: str = Field(
        ...,
        description="Код валюты (ISO 4217), например 'USD' или 'RUB'.",
        examples=["USD"],
    )
    image_url: Optional[HttpUrl] = Field(
        None, description="Ссылка на изображение или карточку товара."
    )


class SupplierOffer(BaseModel):
    """Конкретное предложение поставщика по товару."""

    supplier_id: str = Field(..., description="ID поставщика.")
    supplier_name: str = Field(..., description="Название поставщика.")
    sku: str = Field(..., description="Искомый sku / название позиции.")
    external_product_id: str = Field(
        ..., description="ID товара во внешнем API поставщика."
    )
    unit_price: float = Field(
        ..., gt=0, description="Цена за единицу в валюте 'currency'."
    )
    currency: str = Field(
        ...,
        description="Код валюты (ISO 4217), например 'USD'.",
        examples=["USD"],
    )
    delivery_days: Optional[int] = Field(
        None, description="Оценка срока поставки в днях, если есть."
    )
    product_url: Optional[HttpUrl] = Field(
        None, description="Ссылка на карточку товара (страница / изображение)."
    )


class ItemOffers(BaseModel):
    """Список найденных офферов по одной позиции закупки."""

    item: PurchaseItem
    offers: List[SupplierOffer] = Field(
        default_factory=list,
        description="Подходящие офферы, отсортированные по цене по возрастанию.",
    )


class BulkOffersResult(BaseModel):
    """Результат поиска по нескольким позициям закупки."""

    currency: str = Field(
        ...,
        description="Валюта каталога, в которой указаны цены.",
        examples=["USD"],
    )
    items: List[ItemOffers] = Field(
        default_factory=list,
        description="Результаты для каждой позиции закупки.",
    )
    total_min_cost: float = Field(
        ...,
        ge=0,
        description=(
            "Суммарная минимальная возможная стоимость закупки по "
            "лучшим предложениям на каждую позицию (если офферы нашлись)."
        ),
    )
    unavailable_skus: List[str] = Field(
        default_factory=list,
        description="Перечень sku, по которым не удалось найти предложения.",
    )
