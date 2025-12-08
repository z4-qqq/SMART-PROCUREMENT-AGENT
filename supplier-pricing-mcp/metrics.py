"""Prometheus-метрики для supplier-pricing-mcp."""

from prometheus_client import Counter

API_CALLS = Counter(
    "supplier_pricing_api_calls_total",
    "Общее количество вызовов инструментов supplier-pricing-mcp",
    ["service", "endpoint", "status"],
)
