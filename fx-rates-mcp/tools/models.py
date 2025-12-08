"""Модели Pydantic для fx-rates-mcp."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExchangeRateResponse(BaseModel):
    """Модель ответа с курсом валюты."""

    base: str = Field(
        ...,
        description="Базовая валюта (например, 'USD').",
        examples=["USD"],
    )
    quote: str = Field(
        ...,
        description="Котируемая валюта (например, 'RUB').",
        examples=["RUB"],
    )
    rate: float = Field(
        ...,
        gt=0,
        description="Курс: 1 base = rate quote.",
        examples=[92.5],
    )


class ConvertAmountResponse(BaseModel):
    """Результат конвертации суммы."""

    base: str = Field(
        ...,
        description="Базовая валюта исходной суммы.",
        examples=["USD"],
    )
    quote: str = Field(
        ...,
        description="Целевая валюта конвертации.",
        examples=["RUB"],
    )
    rate: float = Field(
        ...,
        gt=0,
        description="Использованный курс (1 base = rate quote).",
        examples=[92.5],
    )
    amount_base: float = Field(
        ...,
        ge=0,
        description="Исходная сумма в базовой валюте.",
        examples=[100.0],
    )
    amount_quote: float = Field(
        ...,
        ge=0,
        description="Результат конвертации в целевой валюте.",
        examples=[9250.0],
    )
