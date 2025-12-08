"""MCP сервер notification-mcp.

Предоставляет инструменты для:
- отправки плана закупок на вебхук (send_procurement_plan_webhook).
"""

import os

from dotenv import load_dotenv, find_dotenv
from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from mcp_instance import mcp

load_dotenv(find_dotenv())

PORT = int(os.getenv("PORT", "8002"))
HOST = os.getenv("HOST", "0.0.0.0")


def init_tracing() -> None:
    """Инициализирует OpenTelemetry-трейсинг для notification-mcp."""
    resource = Resource(attributes={SERVICE_NAME: "notification-mcp"})
    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(ConsoleSpanExporter())
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)


init_tracing()

# Импорт инструмента
from tools.send_procurement_plan_webhook import (  # noqa: E402,F401
    send_procurement_plan_webhook,
)


@mcp.prompt()
def notification_prompt(summary: str = "") -> str:
    """Пример MCP-промпта."""
    return (
        "Ты отвечаешь за отправку уведомлений о планах закупок на внешние вебхуки. "
        f"Краткое описание операции: {summary}"
    )


def main() -> None:
    """Запуск MCP сервера notification-mcp с HTTP транспортом."""
    print("=" * 60)
    print("🌐 ЗАПУСК MCP СЕРВЕРА notification-mcp")
    print("=" * 60)
    print(f"🚀 MCP Server: http://{HOST}:{PORT}/mcp")
    print("=" * 60)

    mcp.run(
        transport="streamable-http",
        host=HOST,
        port=PORT,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
