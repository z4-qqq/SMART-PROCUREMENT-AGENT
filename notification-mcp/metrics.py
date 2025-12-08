"""Prometheus-метрики для notification-mcp."""

from prometheus_client import Counter

API_CALLS = Counter(
    "notification_api_calls_total",
    "Общее количество вызовов инструментов notification-mcp",
    ["service", "endpoint", "status"],
)
