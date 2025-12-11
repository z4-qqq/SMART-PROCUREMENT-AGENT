"""MCP сервер supplier-pricing-mcp (Printful).

Предоставляет инструменты для:
- подбора предложений поставщика Printful по списку позиций;
- поиска товаров и вариантов в каталоге Printful.
"""

import os

from dotenv import load_dotenv, find_dotenv
from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from mcp_instance import mcp

# Загрузка .env
load_dotenv(find_dotenv())

PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")


def init_tracing() -> None:
    """Минимальная инициализация OpenTelemetry-трейсинга."""
    resource = Resource(attributes={SERVICE_NAME: "supplier-pricing-mcp"})
    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(ConsoleSpanExporter())
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)


init_tracing()

# Регистрируем инструменты (важно: импорт после init_tracing)
from tools.get_offers_for_items import get_offers_for_items  # noqa: E402,F401
from tools.search_printful_catalog import search_printful_catalog  # noqa: E402,F401


@mcp.prompt()
def example_prompt(query: str = "") -> str:
    """Пример MCP-промпта (опционально)."""
    return f"Сформируй план закупок по мерчу Printful по запросу: {query}"


def main() -> None:
    """Запуск MCP-сервера с HTTP-транспортом."""
    print("=" * 60)
    print("🌐 ЗАПУСК MCP СЕРВЕРА supplier-pricing-mcp (Printful)")
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
