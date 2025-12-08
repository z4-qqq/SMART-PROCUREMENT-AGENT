"""Утилиты и ToolResult для notification-mcp."""

from __future__ import annotations

from typing import Any, Dict, List

from mcp.types import TextContent
from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    """Унифицированный формат результата инструмента MCP."""

    content: List[TextContent] = Field(
        default_factory=list, description="Список человеко-читаемых сообщений."
    )
    structured_content: Dict[str, Any] = Field(
        default_factory=dict, description="Машиночитаемый результат."
    )
    meta: Dict[str, Any] = Field(
        default_factory=dict, description="Дополнительные метаданные."
    )
