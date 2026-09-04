"""Парсер цен и остатков товаров: Wildberries, Kaspi Магазин и Ozon."""

import re
import aiohttp
import asyncio
from typing import Optional


HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


def detect_marketplace(url: str) -> Optional[str]:
    url_lower = url.lower()
    if "wildberries.ru" in url_lower or "wb.ru" in url_lower:
        return "Wildberries"
    if "kaspi.kz/shop" in url_lower:
        return "Kaspi"
    if "ozon.ru" in url_lower:
        return "Ozon"
    return None


async def parse_wildberries(url: str) -> Optional[dict]:
    """Парсинг WB через открытый мобильный API (цены в KZT, склад Астана/Казахстан)."""
    match = re.search(r'/catalog/(\d+)', url)
    if not match:
        return None
    nm_id = match.group(1)

    api_url = f"https://card.wb.ru/cards/v2/detail?appType=1&curr=kzt&dest=-1257786&nm={nm_id}"
    try:
        async with aiohttp.ClientSession(headers=HEADERS_BROWSER) as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                products = data.get("data", {}).get("products", [])
                if not products:
                    return None

                p = products[0]
                title = p.get("name", f"Товар WB {nm_id}")

                # Цены в API WB передаются в тиынах/копейках (* 100)
                raw_price = p.get("salePriceU") or p.get("priceU") or 0
                price = float(raw_price) / 100.0

                # Проверка наличия
                total_qty = p.get("totalQuantity", 0)
                in_stock = total_qty > 0

                return {
                    "marketplace": "Wildberries",
                    "item_id": nm_id,
                    "title": title,
                    "price": price,
                    "in_stock": in_stock,
                    "url": f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
                }
    except Exception as e:
        print(f"[PriceTracker WB] Ошибка: {e}")
        return None


async def parse_kaspi(url: str) -> Optional[dict]:
    """Парсинг Kaspi Магазина (город Астана: 710000000)."""
    match = re.search(r'-(\d+)(?:/|\?|$)', url)
    if not match:
        match = re.search(r'/p/[^/]+-(\d+)', url)
    if not match:
        return None
    product_id = match.group(1)

    api_url = f"https://kaspi.kz/yml/product-view/p/{product_id}?c=710000000"
    headers = {
        **HEADERS_BROWSER,
        "Referer": "https://kaspi.kz/",
    }

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

                title = data.get("title", f"Товар Kaspi {product_id}")
                price = float(data.get("unitPrice") or data.get("minPrice") or 0)
                in_stock = bool(data.get("available", False) or price > 0)

                return {
                    "marketplace": "Kaspi",
                    "item_id": product_id,
                    "title": title,
                    "price": price,
                    "in_stock": in_stock,
                    "url": f"https://kaspi.kz/shop/p/product-{product_id}/",
                }
    except Exception as e:
        print(f"[PriceTracker Kaspi] Ошибка: {e}")
        return None


async def parse_ozon(url: str) -> Optional[dict]:
    """Парсинг Ozon через мобильные заголовки."""
    match = re.search(r'/product/(?:[^/]+-)?(\d+)', url)
    if not match:
        return None
    sku = match.group(1)

    api_url = f"https://api.ozon.ru/composer-api.bx/page/json/v2?url=/product/{sku}/"
    headers = {
        **HEADERS_BROWSER,
        "Referer": "https://www.ozon.ru/",
    }

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Извлекаем JSON стейт Ozon
                    main_state = data.get("widgetStates", {})
                    title = f"Товар Ozon {sku}"
                    price = 0.0
                    in_stock = True

                    for key, val in main_state.items():
                        if "webProductHeading" in key:
                            title_data = json.loads(val)
                            title = title_data.get("title", title)
                        if "webPrice" in key:
                            price_data = json.loads(val)
                            raw_p = price_data.get("price", "0")
                            clean_p = "".join(c for c in str(raw_p) if c.isdigit())
                            if clean_p:
                                price = float(clean_p)

                    return {
                        "marketplace": "Ozon",
                        "item_id": sku,
                        "title": title,
                        "price": price,
                        "in_stock": in_stock,
                        "url": f"https://www.ozon.ru/product/{sku}/",
                    }
                else:
                    return None
    except Exception as e:
        print(f"[PriceTracker Ozon] Ошибка: {e}")
        return None


async def fetch_product_info(url: str) -> Optional[dict]:
    """Универсальная точка входа для получения данных по ссылке."""
    marketplace = detect_marketplace(url)
    if not marketplace:
        return None

    if marketplace == "Wildberries":
        return await parse_wildberries(url)
    elif marketplace == "Kaspi":
        return await parse_kaspi(url)
    elif marketplace == "Ozon":
        return await parse_ozon(url)

    return None
