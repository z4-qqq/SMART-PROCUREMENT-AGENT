"""Утилиты и общий тип ToolResult для fx-rates-mcp."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from mcp.types import TextContent
from pydantic import BaseModel, Field

from mcp.shared.exceptions import McpError, ErrorData


class ToolResult(BaseModel):
    """Унифицированный формат результата инструмента MCP.

    content:
        Человеко-читаемый текст для LLM/пользователя.
    structured_content:
        Структурированные данные для программной обработки.
    meta:
        Дополнительные метаданные (endpoint, параметры и т.п.).
    """

    content: List[TextContent] = Field(
        default_factory=list, description="Список контент-блоков (обычно текст)."
    )
    structured_content: Dict[str, Any] = Field(
        default_factory=dict, description="Машиночитаемый результат."
    )
    meta: Dict[str, Any] = Field(
        default_factory=dict, description="Служебные метаданные."
    )


def _parse_float_env(
    value: str | None,
    default: float,
    min_value: float,
    max_value: float,
) -> float:
    """Парсит вещественное число из переменной окружения.

    Args:
        value: Строковое значение из env.
        default: Значение по умолчанию, если парсинг не удался.
        min_value: Минимально допустимое значение.
        max_value: Максимально допустимое значение.

    Returns:
        float: Распарсенное и провалидированное значение либо default.
    """
    if value is None:
        return default
    try:
        parsed = float(value)
        if parsed < min_value or parsed > max_value:
            return default
        return parsed
    except (TypeError, ValueError):
        return default


def format_api_error(response_text: str, status_code: int) -> str:
    """Форматирует ошибку API в понятное сообщение.

    Args:
        response_text: Тело ответа от API в виде строки.
        status_code: HTTP статус-код.

    Returns:
        Строка с человекочитаемым описанием ошибки.
    """
    try:
        error_data = json.loads(response_text)
        code = error_data.get("code", "unknown")
        message = error_data.get("message", response_text)

        error_msg = f"Ошибка API (код {code}): {message}"

        if status_code == 401:
            error_msg = (
                "Ошибка аутентификации.\n\n"
                "Что можно сделать:\n"
                "- Проверьте учетные данные\n"
                f"Детали: {message}"
            )

        return error_msg
    except json.JSONDecodeError:
        return f"Ошибка API (статус {status_code}): {response_text}"


def require_base_currency() -> str:
    """Возвращает базовую валюту по умолчанию, валидируя env.

    Returns:
        Строка с кодом валюты (например, 'RUB').

    Raises:
        McpError: если значение некорректно.
    """
    base = os.getenv("FX_DEFAULT_BASE_CURRENCY", "RUB").upper().strip()
    if len(base) != 3:
        raise McpError(
            ErrorData(
                code=-32602,
                message="Некорректная базовая валюта FX_DEFAULT_BASE_CURRENCY.",
            )
        )
    return base
