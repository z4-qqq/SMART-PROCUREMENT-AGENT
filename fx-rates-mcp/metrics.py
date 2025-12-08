"""Prometheus-метрики для fx-rates-mcp."""

from prometheus_client import Counter

API_CALLS = Counter(
    "fx_rates_api_calls_total",
    "Общее количество вызовов инструментов fx-rates-mcp",
    ["service", "endpoint", "status"],
)
