"""Парсер цен и остатков товаров: Wildberries (.ru / .kz), Kaspi (.kz / l.kaspi.kz) и Ozon (.ru / .kz)."""

import re
import json
import aiohttp
import asyncio
from typing import Optional


HEADERS_MOBILE = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

HEADERS_JSON = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


def detect_marketplace(url: str) -> Optional[str]:
    u = url.lower()
    if any(d in u for d in ["wildberries.ru", "wildberries.kz", "wb.ru", "wb.kz"]):
        return "Wildberries"
    if any(d in u for d in ["kaspi.kz", "l.kaspi.kz"]):
        return "Kaspi"
    if any(d in u for d in ["ozon.ru", "ozon.kz"]):
        return "Ozon"
    return None


async def resolve_redirects(url: str) -> tuple[str, str]:
    """Разворачивает короткую ссылку и возвращает (финальный_url, html_контент)."""
    try:
        async with aiohttp.ClientSession(headers=HEADERS_MOBILE) as session:
            async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                final_url = str(resp.url)
                html = await resp.text()
                return final_url, html
    except Exception as e:
        print(f"[PriceTracker Redirect] Ошибка для {url}: {e}")
        return url, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 1. WILDBERRIES (WB)
# ═══════════════════════════════════════════════════════════════════════════════

async def parse_wildberries(url: str) -> Optional[dict]:
    match = re.search(r'/catalog/(\d+)', url)
    if not match:
        return None
    nm_id = match.group(1)

    # Пробуем API цен для Казахстана
    api_urls = [
        f"https://card.wb.ru/cards/v2/detail?appType=1&curr=kzt&dest=-1257786&nm={nm_id}",
        f"https://card.wb.ru/cards/v1/detail?appType=1&curr=kzt&dest=-1257786&nm={nm_id}",
    ]

    for api_url in api_urls:
        try:
            async with aiohttp.ClientSession(headers=HEADERS_JSON) as session:
                async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    products = data.get("data", {}).get("products", [])
                    if not products:
                        continue

                    p = products[0]
                    title = p.get("name", f"Товар WB {nm_id}")

                    # Поиск цены во всех возможных полях WB API
                    price = 0.0
                    if p.get("salePriceU"):
                        price = float(p["salePriceU"]) / 100.0
                    elif p.get("priceU"):
                        price = float(p["priceU"]) / 100.0

                    # Если цены в корне нет, смотрим в размерах
                    if price == 0 and p.get("sizes"):
                        for s in p["sizes"]:
                            pr = s.get("price")
                            if pr:
                                total = pr.get("total") or pr.get("basic") or 0
                                if total > 0:
                                    price = float(total) / 100.0
                                    break

                    total_qty = p.get("totalQuantity", 0)
                    if total_qty == 0 and p.get("sizes"):
                        for s in p["sizes"]:
                            for st in s.get("stocks", []):
                                total_qty += st.get("qty", 0)

                    in_stock = total_qty > 0 or price > 0

                    if price > 0:
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


# ═══════════════════════════════════════════════════════════════════════════════
# 2. KASPI МАГАЗИН
# ═══════════════════════════════════════════════════════════════════════════════

async def parse_kaspi(url: str) -> Optional[dict]:
    final_url = url
    html = ""

    # Если ссылка короткая (l.kaspi.kz), разворачиваем её
    if "l.kaspi.kz" in url.lower():
        final_url, html = await resolve_redirects(url)

    # Ищем ID товара в URL
    match = re.search(r'-(\d+)(?:/|\?|$)', final_url) or re.search(r'/p/[^/]+-(\d+)', final_url)
    product_id = match.group(1) if match else None

    # Если в URL ID не найден, ищем его в HTML страницы редиректа
    title_from_html = None
    if not product_id and html:
        id_match = re.search(r'kaspi\.kz/shop/p/[^"]*?-(\d+)', html) or re.search(r'data-product-id="(\d+)"', html)
        if id_match:
            product_id = id_match.group(1)

        t_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
        if t_match:
            title_from_html = t_match.group(1).replace("– Магазин на Kaspi.kz", "").strip()

    if not product_id:
        return None

    # Запрашиваем цены по Астане (город 710000000)
    api_url = f"https://kaspi.kz/yml/product-view/p/{product_id}?c=710000000"
    headers = {
        **HEADERS_JSON,
        "Referer": "https://kaspi.kz/",
    }

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    title = data.get("title") or title_from_html or f"Товар Kaspi {product_id}"
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
        print(f"[PriceTracker Kaspi API] Ошибка: {e}")

    # Если API заблокирован, но из HTML удалось достать заголовок
    if title_from_html:
        return {
            "marketplace": "Kaspi",
            "item_id": product_id,
            "title": title_from_html,
            "price": 0.0,
            "in_stock": True,
            "url": f"https://kaspi.kz/shop/p/-{product_id}/",
        }

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 3. OZON (.RU / .KZ)
# ═══════════════════════════════════════════════════════════════════════════════

async def parse_ozon(url: str) -> Optional[dict]:
    final_url, html = await resolve_redirects(url)
    if not html:
        try:
            async with aiohttp.ClientSession(headers=HEADERS_MOBILE) as session:
                async with session.get(final_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    html = await resp.text()
        except Exception:
            pass

    match = re.search(r'/product/(?:[^/]+-)?(\d+)', final_url)
    sku = match.group(1) if match else "ozon_item"

    # Извлекаем заголовок
    title = "Товар Ozon"
    t_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html) or re.search(r'<title>([^<]+)</title>', html)
    if t_match:
        t_clean = t_match.group(1).replace(" - купить в интернет-магазине OZON", "").replace(" - OZON", "").strip()
        if "доступ ограничен" not in t_clean.lower() and "ой!" not in t_clean.lower():
            title = t_clean

    # Извлекаем цену
    price = 0.0
    p_match = (
        re.search(r'"price":\s*"(\d+)"', html) or
        re.search(r'itemprop="price"\s+content="(\d+)"', html) or
        re.search(r'data-price="(\d+)"', html) or
        re.search(r'(\d[\d\s]*)\s*₸', html)
    )
    if p_match:
        digits = "".join(c for c in p_match.group(1) if c.isdigit())
        if digits:
            price = float(digits)

    in_stock = "нет в наличии" not in html.lower()

    return {
        "marketplace": "Ozon",
        "item_id": sku,
        "title": title,
        "price": price,
        "in_stock": in_stock,
        "url": final_url,
    }


async def fetch_product_info(url: str) -> Optional[dict]:
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