"""Модели Pydantic для notification-mcp."""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field, HttpUrl


class WebhookResult(BaseModel):
    """Результат вызова вебхука."""

    url: HttpUrl = Field(..., description="URL вызванного вебхука.")
    status_code: int = Field(
        ...,
        description="HTTP статус-код, возвращённый вебхуком.",
        examples=[200],
    )
    ok: bool = Field(
        ...,
        description="True, если статус-код в диапазоне 200–299.",
    )
    response_body: Dict[str, Any] = Field(
        default_factory=dict,
        description="Тело ответа вебхука (если удалось распарсить как JSON).",
    )
