"""
MCP-tool convert_amount для fx-rates-mcp.

Задача:
- Конвертировать сумму между валютами (например, USD -> EUR).
- Использовать публичное FX API (по умолчанию https://api.exchangerate.host),
  поддерживающее параметр access_key.
- Если API недоступно или не возвращает курс — НЕ падать, а
  вернуть осмысленный fallback-результат, чтобы агент не ломался.

Структурированный ответ:
{
  "base": "USD",
  "quote": "EUR",
  "amount_base": 123.45,
  "amount_quote": 112.34,
  "rate": 0.9094,
  "provider": "https://api.exchangerate.host",
  "fallback_used": false,
  "warning": null,
  "raw": {...}  # сырое тело ответа FX API (может быть пустым)
}
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

import httpx

from mcp_instance import mcp

logger = logging.getLogger(__name__)

# Базовый URL публичного FX API (по умолчанию exchangerate.host)
FX_API_BASE_URL = os.getenv(
    "FX_API_BASE_URL",
    "https://api.exchangerate.host",
)

# Access key для API, если он требуется (exchangerate.host / fixer и т.п.)
FX_API_ACCESS_KEY = os.getenv("FX_API_ACCESS_KEY")

# Простая статическая табличка на крайний случай
# (если и HTTP, и нормальный ответ отсутствуют).
_FX_FALLBACK_RATES: Dict[Tuple[str, str], float] = {
    ("USD", "EUR"): 0.9,
    ("EUR", "USD"): 1.11,
    ("USD", "RUB"): 90.0,
    ("RUB", "USD"): 1.0 / 90.0,
    ("EUR", "RUB"): 98.0,
    ("RUB", "EUR"): 1.0 / 98.0,
}


async def _fetch_rate_http(base: str, quote: str) -> Tuple[Optional[float], Dict[str, Any]]:
    """
    Пытается получить курс base->quote через публичное FX API.

    Используем endpoint:
      GET {FX_API_BASE_URL}/convert?from=USD&to=EUR&amount=1[&access_key=...]

    Для exchangerate.host структура ответа (apilayer-стек) примерно такая:
    {
      "success": true,
      "query": {"from": "USD", "to": "EUR", "amount": 1},
      "info": {"rate": 0.91},
      "result": 0.91
    }

    При ошибке (например, без access_key):
    {
      "success": false,
      "error": {
        "code": 101,
        "type": "missing_access_key",
        "info": "You have not supplied an API Access Key. [Required format: access_key=YOUR_ACCESS_KEY]"
      }
    }
    """
    url = FX_API_BASE_URL.rstrip("/") + "/convert"

    params = {
        "from": base.upper(),
        "to": quote.upper(),
        "amount": 1,
    }

    # Если ключ задан — передаём его как access_key (как просит API)
    if FX_API_ACCESS_KEY:
        params["access_key"] = FX_API_ACCESS_KEY
        logger.info(
            "FX HTTP: GET %s params(from=%s,to=%s,amount=1,access_key=***hidden***)",
            url,
            params["from"],
            params["to"],
        )
    else:
        logger.info("FX HTTP: GET %s params=%r (без access_key)", url, params)

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    rate: Optional[float] = None

    if isinstance(data, dict):
        # Если success=false — вероятно, проблема с ключом или запросом
        if data.get("success") is False:
            err = data.get("error") or {}
            logger.warning(
                "FX HTTP returned error for %s->%s: %s",
                base,
                quote,
                err,
            )
        # сначала пробуем info.rate
        info = data.get("info")
        if isinstance(info, dict) and "rate" in info:
            try:
                rate = float(info["rate"])
            except (TypeError, ValueError):
                rate = None

        # если нет info.rate, но есть result (для amount=1)
        if rate is None and "result" in data:
            try:
                rate = float(data["result"])
            except (TypeError, ValueError):
                rate = None

    return rate, data if isinstance(data, dict) else {}


def _fallback_rate(base: str, quote: str) -> Tuple[float, str]:
    """
    Возвращает резервный курс, если HTTP API недоступно или вернуло
    что-то странное.

    Всегда возвращает какой-то rate (хотя бы 1.0), чтобы tool НЕ падал.
    """
    b = base.upper()
    q = quote.upper()

    if b == q:
        return 1.0, "identity"

    key = (b, q)
    if key in _FX_FALLBACK_RATES:
        return _FX_FALLBACK_RATES[key], "fallback_static"

    # Если пары нет ни в статике, ни в HTTP — последняя линия обороны:
    # считаем, что курс 1:1, но помечаем как fallback_identity.
    logger.warning(
        "FX fallback: нет предопределённого курса для пары %s->%s, "
        "используем 1.0 как identity.",
        b,
        q,
    )
    return 1.0, "fallback_identity"


@mcp.tool()
async def convert_amount(
    amount: float,
    base: str,
    quote: str,
) -> Dict[str, Any]:
    """
    Конвертация суммы между валютами.

    Параметры:
    - amount: сумма в валюте base.
    - base: код исходной валюты (например, "USD").
    - quote: код целевой валюты (например, "EUR").

    Поведение:
    - пытается вызвать публичное FX API (FX_API_BASE_URL + /convert),
      при наличии FX_API_ACCESS_KEY передаёт его как access_key;
    - если API недоступно или не вернуло корректный курс, используется fallback;
    - НИКОГДА не выбрасывает исключения для клиента (не шлёт MCP-ERROR),
      всегда возвращает структурированный результат.
    """
    base = base.upper()
    quote = quote.upper()

    logger.info(
        "convert_amount called: amount=%.4f, base=%s, quote=%s",
        amount,
        base,
        quote,
    )

    # Если валюты совпадают — просто возвращаем ту же сумму
    if base == quote:
        logger.info("FX: base и quote совпадают (%s), конвертация не требуется.", base)
        converted = float(amount)
        return {
            "base": base,
            "quote": quote,
            "amount_base": float(amount),
            "amount_quote": converted,
            "rate": 1.0,
            "provider": "identity",
            "fallback_used": False,
            "warning": None,
            "raw": {},
        }

    rate: Optional[float] = None
    raw: Dict[str, Any] = {}
    provider: str = FX_API_BASE_URL
    fallback_used = False
    warning: Optional[str] = None

    # 1) Пытаемся получить курс из HTTP API
    try:
        rate, raw = await _fetch_rate_http(base, quote)
        logger.info("FX HTTP result for %s->%s: rate=%r", base, quote, rate)
    except Exception as exc:  # noqa: BLE001
        logger.exception("FX HTTP error for %s->%s: %s", base, quote, exc)
        rate = None
        warning = (
            f"FX API {FX_API_BASE_URL} недоступно или вернуло ошибку, "
            "использован fallback-курс."
        )

    # 2) Если HTTP не дал валидный rate — fallback
    if rate is None:
        fallback_used = True
        rate, provider = _fallback_rate(base, quote)
        if warning is None:
            warning = (
                "FX API не вернул корректный курс, использован fallback-курс "
                f"({provider})."
            )

    # 3) Считаем итоговую сумму
    try:
        amount_base = float(amount)
    except (TypeError, ValueError):
        amount_base = 0.0

    amount_quote = float(amount_base * rate)

    logger.info(
        "FX final: %.4f %s -> %.4f %s (rate=%.6f, provider=%s, fallback_used=%s)",
        amount_base,
        base,
        amount_quote,
        quote,
        rate,
        provider,
        fallback_used,
    )

    return {
        "base": base,
        "quote": quote,
        "amount_base": amount_base,
        "amount_quote": amount_quote,
        "rate": rate,
        "provider": provider,
        "fallback_used": fallback_used,
        "warning": warning,
        "raw": raw,
    }
