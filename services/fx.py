"""Курс валют для автоматической конвертации сумм в чеках/переводах в KZT.

Используется бесплатный курс без ключа API (open.er-api.com, обновляется
ежедневно). Если источник недоступен — не падаем и не выдумываем курс,
а честно возвращаем None; вызывающий код должен переспросить сумму в KZT
у пользователя, а не притвориться, что сконвертировал.
"""

import asyncio
import json
import time
import urllib.request

_CACHE_TTL_SECONDS = 6 * 60 * 60  # курс обновляется раз в сутки у источника, кэшируем на 6 часов
_cache: dict[str, tuple[float, float]] = {}  # currency -> (rate_to_kzt, fetched_at)

_KNOWN_CURRENCIES = {"USD", "EUR", "RUB", "CNY", "GBP", "TRY", "KGS", "UZS", "AED", "AMD", "GEL"}


def _fetch_rate_sync(currency: str) -> float | None:
    url = f"https://open.er-api.com/v6/latest/{currency}"
    try:
        with urllib.request.urlopen(url, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rate = data.get("rates", {}).get("KZT")
        return float(rate) if rate else None
    except Exception as e:
        print(f"[Курс валют] Не удалось получить курс {currency}: {e}")
        return None


async def get_kzt_rate(currency: str) -> float | None:
    """Сколько тенге за 1 единицу указанной валюты. None, если не удалось узнать."""
    cur = str(currency or "").strip().upper()
    if not cur or cur == "KZT":
        return 1.0

    cached = _cache.get(cur)
    if cached and (time.time() - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]

    rate = await asyncio.to_thread(_fetch_rate_sync, cur)
    if rate:
        _cache[cur] = (rate, time.time())
    return rate


async def convert_to_kzt(amount: float, currency: str) -> tuple[float, float] | None:
    """Вернуть (сумма в KZT, курс) или None, если курс получить не удалось."""
    rate = await get_kzt_rate(currency)
    if not rate:
        return None
    return round(float(amount) * rate, 2), rate
