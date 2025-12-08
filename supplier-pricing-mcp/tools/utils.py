"""Утилиты и общий тип результата для инструментов MCP."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from mcp.types import TextContent
from pydantic import BaseModel, Field

from mcp.shared.exceptions import McpError, ErrorData


class ToolResult(BaseModel):
    """
    Обертка для результата MCP-инструмента.

    content:
        Человеко-читаемые данные (для LLM/пользователя).
    structured_content:
        Структурированные данные для программной обработки.
    meta:
        Служебная информация (endpoint, параметры и т.п.).
    """

    content: List[TextContent] = Field(
        default_factory=list, description="Список контент-блоков для ответа."
    )
    structured_content: Dict[str, Any] = Field(
        default_factory=dict, description="Машиночитаемое тело ответа."
    )
    meta: Dict[str, Any] = Field(
        default_factory=dict, description="Дополнительные метаданные."
    )


def _require_env_vars(names: List[str]) -> Dict[str, str]:
    """
    Проверяет наличие обязательных переменных окружения.

    Args:
        names: Список имен env-переменных.

    Returns:
        Словарь {name: value}.

    Raises:
        McpError: если хотя бы одна переменная отсутствует.
    """
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        raise McpError(
            ErrorData(
                code=-32602,
                message=(
                    "Отсутствуют обязательные переменные окружения: "
                    + ", ".join(missing)
                ),
            )
        )

    return {name: os.getenv(name, "") for name in names}


def format_api_error(response_text: str, status_code: int) -> str:
    """
    Преобразует ответ внешнего API в понятное сообщение об ошибке.

    Args:
        response_text: Тело ответа в виде строки.
        status_code: HTTP-код ответа.

    Returns:
        Отформатированное текстовое описание ошибки.
    """
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        return f"Ошибка API (HTTP {status_code}): {response_text}"

    code = data.get("code", "unknown")
    message = data.get("message") or response_text

    base_msg = f"Ошибка API (код {code}, HTTP {status_code}): {message}"

    if status_code == 401:
        return (
            "Ошибка аутентификации при обращении к внешнему API.\n\n"
            "Проверьте конфигурацию и учетные данные.\n\n"
            f"Детали: {message}"
        )

    return base_msg
