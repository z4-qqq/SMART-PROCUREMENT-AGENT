"""Инструмент отправки плана закупок на внешний вебхук (POST JSON)."""

from __future__ import annotations

import json
import os
from typing import Any, Dict

import httpx
from fastmcp import Context
from mcp.types import TextContent
from opentelemetry import trace
from pydantic import Field, HttpUrl

from mcp.shared.exceptions import McpError, ErrorData
from mcp_instance import mcp
from metrics import API_CALLS
from .models import WebhookResult
from .utils import ToolResult

tracer = trace.get_tracer(__name__)


@mcp.tool(
    name="send_procurement_plan_webhook",
    description=(
        "📤 Отправляет план закупок (JSON) на указанный вебхук методом POST. "
        "Используется для live-demo: можно подключить webhook.site, "
        "внутренние сервисы компании или другие интеграции."
    ),
)
async def send_procurement_plan_webhook(
    url: HttpUrl = Field(
        ...,
        description=(
            "URL вебхука, на который нужно отправить план закупок. "
            "Должен поддерживать HTTP POST с JSON."
        ),
    ),
    plan: Dict[str, Any] = Field(
        ...,
        description=(
            "Структурированный план закупок (JSON-объект), который нужно отправить."
        ),
        examples=[
            {
                "total_cost": 123456.78,
                "currency": "RUB",
                "items": [
                    {"sku": "laptop", "quantity": 10, "total": 550000.0},
                ],
            }
        ],
    ),
    ctx: Context = None,
) -> ToolResult:
    """Отправляет план закупок на внешний вебхук.

    Args:
        url: URL вебхука (HttpUrl), который принимает POST JSON.
        plan: JSON-объект с планом закупок.
        ctx: Контекст MCP для логирования и отслеживания прогресса.

    Returns:
        ToolResult: человекочитаемое резюме и WebhookResult в structured_content.

    Raises:
        McpError: при ошибках HTTP или валидации параметров.
    """
    if ctx is None:
        ctx = Context()

    with tracer.start_as_current_span("send_procurement_plan_webhook") as span:
        span.set_attribute("webhook.url", str(url))

        await ctx.info("🚀 Отправляем план закупок на вебхук.")
        await ctx.report_progress(progress=0, total=100)

        API_CALLS.labels(
            service="notification-mcp",
            endpoint="send_procurement_plan_webhook",
            status="started",
        ).inc()

        timeout_str = os.getenv("NOTIFICATION_HTTP_TIMEOUT", "10.0")
        try:
            timeout = float(timeout_str)
        except ValueError:
            timeout = 10.0

        await ctx.info("📡 Делаем POST запрос на указанный URL.")
        await ctx.report_progress(progress=50, total=100)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(str(url), json=plan)
        except Exception as e:
            await ctx.error(f"💥 Ошибка при отправке вебхука: {e}")

            API_CALLS.labels(
                service="notification-mcp",
                endpoint="send_procurement_plan_webhook",
                status="error",
            ).inc()

            raise McpError(
                ErrorData(
                    code=-32603,
                    message=f"Не удалось отправить вебхук: {e}",
                )
            )

        ok = 200 <= response.status_code < 300

        try:
            resp_body = response.json()
        except json.JSONDecodeError:
            resp_body = {}

        result = WebhookResult(
            url=url,
            status_code=response.status_code,
            ok=ok,
            response_body=resp_body,
        )

        await ctx.report_progress(progress=100, total=100)

        if ok:
            await ctx.info("✅ Вебхук успешно обработал запрос.")
            status_label = "success"
            human_text = (
                f"План закупок успешно отправлен на {url}. "
                f"HTTP статус: {response.status_code}."
            )
        else:
            await ctx.warning(
                f"⚠️ Вебхук вернул неуспешный статус: {response.status_code}."
            )
            status_label = "error"
            human_text = (
                f"План закупок был отправлен на {url}, "
                f"но вебхук вернул статус {response.status_code}."
            )

        API_CALLS.labels(
            service="notification-mcp",
            endpoint="send_procurement_plan_webhook",
            status=status_label,
        ).inc()

        span.set_attribute("http.status_code", response.status_code)
        span.set_attribute("success", ok)

        return ToolResult(
            content=[TextContent(type="text", text=human_text)],
            structured_content=result.model_dump(),
            meta={
                "endpoint": "send_procurement_plan_webhook",
            },
        )
