"""Единый экземпляр FastMCP для supplier-pricing-mcp."""

from fastmcp import FastMCP

# Один MCP-сервер на всё приложение
mcp = FastMCP("supplier-pricing-mcp")
