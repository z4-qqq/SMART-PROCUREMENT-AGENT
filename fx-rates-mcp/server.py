"""MCP сервер fx-rates-mcp.

Предоставляет инструменты для:
- получения курсов валют (get_exchange_rate);
- конвертации сумм (convert_amount).
"""

import os

from dotenv import load_dotenv, find_dotenv
from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from mcp_instance import mcp

load_dotenv(find_dotenv())

PORT = int(os.getenv("PORT", "8001"))
HOST = os.getenv("HOST", "0.0.0.0")


def init_tracing() -> None:
    """Инициализация OpenTelemetry-трейсинга."""
    resource = Resource(attributes={SERVICE_NAME: "fx-rates-mcp"})
    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(ConsoleSpanExporter())
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)


init_tracing()

# Импорт инструментов (важно делать после инициализации трейсинга)
from tools.get_exchange_rate import get_exchange_rate  # noqa: E402,F401
from tools.convert_amount import convert_amount  # noqa: E402,F401


@mcp.prompt()
def fx_prompt(query: str = "") -> str:
    """Пример MCP-промпта для LLM."""
    return (
        "Ты работаешь с курсами валют. "
        f"Пользовательский запрос: {query}"
    )


def main() -> None:
    """Запуск MCP сервера fx-rates-mcp с HTTP транспортом."""
    print("=" * 60)
    print("🌐 ЗАПУСК MCP СЕРВЕРА fx-rates-mcp")
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
