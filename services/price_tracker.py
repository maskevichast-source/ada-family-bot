"""Парсер цен и остатков товаров: Wildberries (.ru / .kz), Kaspi (.kz / l.kaspi.kz) и Ozon (.ru / .kz)."""

import re
import json
import aiohttp
import asyncio
from typing import Optional


HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


def detect_marketplace(url: str) -> Optional[str]:
    """Определяет маркетплейс с поддержкой доменов .ru, .kz и коротких ссылок."""
    url_lower = url.lower()
    if any(d in url_lower for d in ["wildberries.ru", "wildberries.kz", "wb.ru", "wb.kz"]):
        return "Wildberries"
    if any(d in url_lower for d in ["kaspi.kz/shop", "l.kaspi.kz/shop", "l.kaspi.kz", "kaspi.kz/p/"]):
        return "Kaspi"
    if any(d in url_lower for d in ["ozon.ru", "ozon.kz"]):
        return "Ozon"
    return None


async def resolve_redirects(url: str) -> str:
    """Разворачивает короткие ссылки мобильных приложений (l.kaspi.kz, ozon.kz/t/...)."""
    try:
        async with aiohttp.ClientSession(headers=HEADERS_BROWSER) as session:
            async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                return str(resp.url)
    except Exception as e:
        print(f"[PriceTracker] Ошибка редиректа для {url}: {e}")
        return url


async def parse_wildberries(url: str) -> Optional[dict]:
    """Парсинг WB (.ru / .kz) через открытый API цен в KZT."""
    match = re.search(r'/catalog/(\d+)', url)
    if not match:
        return None
    nm_id = match.group(1)

    api_url = f"https://card.wb.ru/cards/v2/detail?appType=1&curr=kzt&dest=-1257786&nm={nm_id}"
    try:
        async with aiohttp.ClientSession(headers=HEADERS_BROWSER) as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                products = data.get("data", {}).get("products", [])
                if not products:
                    return None

                p = products[0]
                title = p.get("name", f"Товар WB {nm_id}")

                # Цены в API WB передаются в тиынах (* 100)
                raw_price = p.get("salePriceU") or p.get("priceU") or 0
                price = float(raw_price) / 100.0

                total_qty = p.get("totalQuantity", 0)
                in_stock = total_qty > 0

                return {
                    "marketplace": "Wildberries",
                    "item_id": nm_id,
                    "title": title,
                    "price": price,
                    "in_stock": in_stock,
                    "url": f"https://www.wildberries.kz/catalog/{nm_id}/detail.aspx",
                }
    except Exception as e:
        print(f"[PriceTracker WB] Ошибка: {e}")
        return None


async def parse_kaspi(url: str) -> Optional[dict]:
    """Парсинг Kaspi Магазина (поддерживает короткие ссылки l.kaspi.kz)."""
    # Если это короткая ссылка из приложения Kaspi — сначала разворачиваем её
    resolved_url = url
    if "l.kaspi.kz" in url.lower():
        resolved_url = await resolve_redirects(url)

    # Ищем ID товара в развёрнутом URL
    match = re.search(r'-(\d+)(?:/|\?|$)', resolved_url)
    if not match:
        match = re.search(r'/p/[^/]+-(\d+)', resolved_url)
    if not match:
        match = re.search(r'c=(\d+)', resolved_url)
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
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
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
                    "url": f"https://kaspi.kz/shop/p/-{product_id}/",
                }
    except Exception as e:
        print(f"[PriceTracker Kaspi] Ошибка: {e}")
        return None


async def parse_ozon(url: str) -> Optional[dict]:
    """Парсинг Ozon (.ru / .kz, включая короткие ссылки ozon.kz/t/...)."""
    resolved_url = url
    if "/t/" in url.lower() or "ozon.kz/t" in url.lower():
        resolved_url = await resolve_redirects(url)

    # Ищем SKU в развёрнутой ссылке
    match = re.search(r'/product/(?:[^/]+-)?(\d+)', resolved_url)
    sku = match.group(1) if match else None

    try:
        async with aiohttp.ClientSession(headers=HEADERS_BROWSER) as session:
            async with session.get(resolved_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                html = await resp.text()

                # 1. Пробуем вытащить название
                title = "Товар Ozon"
                title_match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                if title_match:
                    title = title_match.group(1).replace(" - купить в интернет-магазине OZON", "").replace(" - OZON", "").strip()

                # 2. Пробуем вытащить цену
                price = 0.0
                price_match = re.search(r'"price":\s*"(\d+)"', html) or re.search(r'data-price="(\d+)"', html) or re.search(r'itemprop="price" content="(\d+)"', html)
                if price_match:
                    price = float(price_match.group(1))

                # 3. Наличие
                in_stock = "нет в наличии" not in html.lower() and "out_of_stock" not in html.lower()

                if not sku and match:
                    sku = match.group(1)

                return {
                    "marketplace": "Ozon",
                    "item_id": sku or "unknown",
                    "title": title,
                    "price": price,
                    "in_stock": in_stock,
                    "url": resolved_url,
                }
    except Exception as e:
        print(f"[PriceTracker Ozon] Ошибка: {e}")
        return None


async def fetch_product_info(url: str) -> Optional[dict]:
    """Универсальная точка входа: определяет маркетплейс и собирает данные."""
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