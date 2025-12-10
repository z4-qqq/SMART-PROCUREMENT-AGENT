# scripts/debug_search_printful_mcp.py

import asyncio
import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


SUPPLIER_MCP_URL = os.getenv("SUPPLIER_MCP_URL", "http://127.0.0.1:8000/mcp")


def extract_structured_content(result) -> dict:
    """
    Аккуратно достаём structuredContent из CallToolResult
    для текущей версии mcp-клиента.
    """

    # 1) Для твоей версии — есть camelCase-атрибут structuredContent
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        # Внутри него у тебя ещё один ключ "structuredContent"
        inner = sc.get("structuredContent")
        if isinstance(inner, dict):
            return inner
        return sc

    # 2) На всякий случай — snake_case, если обновишь библиотеку
    sc2 = getattr(result, "structured_content", None)
    if isinstance(sc2, dict):
        inner = sc2.get("structuredContent") or sc2.get("structured_content")
        if isinstance(inner, dict):
            return inner
        return sc2

    # 3) Фоллбек: парсим JSON из content[0].text
    contents = getattr(result, "content", None) or []
    for c in contents:
        text = getattr(c, "text", None)
        if not text:
            continue
        try:
            outer = json.loads(text)
        except Exception:
            continue
        if isinstance(outer, dict):
            inner = outer.get("structuredContent") or outer.get("structured_content")
            if isinstance(inner, dict):
                return inner
            return outer

    # Если совсем не нашли — пустой dict
    return {}


async def debug_query(query: str) -> None:
    async with streamablehttp_client(SUPPLIER_MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "search_printful_catalog",
                {
                    "query": query,
                    "limit_products": 5,
                    "limit_variants_per_product": 5,
                },
            )

    print("RAW RESULT:", result, "\n")

    data = extract_structured_content(result)
    print(f"=== query={query!r} ===")
    print(json.dumps(data, ensure_ascii=False, indent=2))


async def main() -> None:
    # попробуем несколько типичных запросов
    await debug_query("unisex hoodie")
   # await debug_query("t-shirt")
   # await debug_query("mug")


if __name__ == "__main__":
    asyncio.run(main())
