"""Парсер цен и остатков товаров: Wildberries, Kaspi и Ozon (с поддержкой коротких ссылок и антибота)."""

import re
import html
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
    """Разворачивает короткие ссылки (l.kaspi.kz, ozon.kz/t/...) и возвращает финальный URL и HTML."""
    try:
        async with aiohttp.ClientSession(headers=HEADERS_MOBILE) as session:
            async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                final_url = str(resp.url)
                text = await resp.text()
                return final_url, text
    except Exception as e:
        print(f"[PriceTracker Redirect] Ошибка для {url}: {e}")
        return url, ""


def clean_kaspi_title(raw_title: str) -> str:
    """Убирает HTML-сущности и мусорные приписки Kaspi."""
    t = html.unescape(raw_title).strip()
    t = re.sub(r'^(?:Купить\s+)', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s+в\s+[А-Яа-яЁёA-Za-z\s–-]+–\s*Магазин\s+на\s+Kaspi\.kz.*$', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s*–\s*Магазин\s+на\s+Kaspi\.kz.*$', '', t, flags=re.IGNORECASE)
    return t.strip() or "Товар Kaspi"


def get_wb_basket_host(nm_id: int) -> str:
    """Вычисляет точный CDN-хост Wildberries для артикула."""
    vol = nm_id // 100000
    if 0 <= vol <= 143: return "basket-01.wbbasket.ru"
    elif vol <= 287: return "basket-02.wbbasket.ru"
    elif vol <= 431: return "basket-03.wbbasket.ru"
    elif vol <= 719: return "basket-04.wbbasket.ru"
    elif vol <= 1007: return "basket-05.wbbasket.ru"
    elif vol <= 1061: return "basket-06.wbbasket.ru"
    elif vol <= 1115: return "basket-07.wbbasket.ru"
    elif vol <= 1169: return "basket-08.wbbasket.ru"
    elif vol <= 1313: return "basket-09.wbbasket.ru"
    elif vol <= 1601: return "basket-10.wbbasket.ru"
    elif vol <= 1655: return "basket-11.wbbasket.ru"
    elif vol <= 1919: return "basket-12.wbbasket.ru"
    elif vol <= 2045: return "basket-13.wbbasket.ru"
    elif vol <= 2189: return "basket-14.wbbasket.ru"
    elif vol <= 2405: return "basket-15.wbbasket.ru"
    elif vol <= 2621: return "basket-16.wbbasket.ru"
    elif vol <= 2837: return "basket-17.wbbasket.ru"
    return "basket-18.wbbasket.ru"


async def parse_wildberries(url: str) -> Optional[dict]:
    match = re.search(r'/catalog/(\d+)', url)
    if not match:
        return None
    nm_id = int(match.group(1))

    title = None
    price = 0.0
    in_stock = True

    try:
        basket_host = get_wb_basket_host(nm_id)
        vol = nm_id // 100000
        part = nm_id // 1000
        cdn_url = f"https://{basket_host}/vol{vol}/part{part}/{nm_id}/info/ru/card.json"
        async with aiohttp.ClientSession(headers=HEADERS_JSON) as session:
            async with session.get(cdn_url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status == 200:
                    cdn_data = await resp.json()
                    title = cdn_data.get("imt_name") or cdn_data.get("name")
    except Exception:
        pass

    api_urls = [
        f"https://card.wb.ru/cards/v2/detail?appType=1&curr=kzt&dest=-1257786&nm={nm_id}",
        f"https://card.wb.ru/cards/v1/detail?appType=1&curr=kzt&dest=-1257786&nm={nm_id}",
        f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&nm={nm_id}",
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
                    if not title:
                        title = p.get("name")

                    if p.get("salePriceU"):
                        price = float(p["salePriceU"]) / 100.0
                    elif p.get("priceU"):
                        price = float(p["priceU"]) / 100.0

                    if price == 0 and p.get("sizes"):
                        for s in p["sizes"]:
                            pr = s.get("price")
                            if pr:
                                total = pr.get("total") or pr.get("basic") or 0
                                if total > 0:
                                    price = float(total) / 100.0
                                    break

                    total_qty = p.get("totalQuantity", 0)
                    in_stock = total_qty > 0 or price > 0
                    if price > 0:
                        break
        except Exception as e:
            print(f"[PriceTracker WB] Ошибка: {e}")

    final_title = title or f"Товар Wildberries {nm_id}"
    return {
        "marketplace": "Wildberries",
        "item_id": str(nm_id),
        "title": final_title,
        "price": price,
        "in_stock": in_stock,
        "url": f"https://www.wildberries.kz/catalog/{nm_id}/detail.aspx",
    }


async def parse_kaspi(url: str) -> Optional[dict]:
    final_url = url
    html_content = ""

    if "l.kaspi.kz" in url.lower():
        final_url, html_content = await resolve_redirects(url)

    match = re.search(r'-(\d+)(?:/|\?|$)', final_url) or re.search(r'/p/[^/]+-(\d+)', final_url)
    product_id = match.group(1) if match else None

    title_from_html = None
    price = 0.0

    if html_content:
        if not product_id:
            id_match = re.search(r'kaspi\.kz/shop/p/[^"]*?-(\d+)', html_content) or re.search(r'data-product-id="(\d+)"', html_content)
            if id_match:
                product_id = id_match.group(1)

        t_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html_content)
        if t_match:
            title_from_html = clean_kaspi_title(t_match.group(1))

        p_match = re.search(r'"price":\s*"(\d+)"', html_content) or re.search(r'"lowPrice":\s*"(\d+)"', html_content)
        if p_match:
            price = float(p_match.group(1))

    if not product_id:
        return None

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
                    raw_t = data.get("title")
                    if raw_t:
                        title_from_html = clean_kaspi_title(raw_t)

                    api_price = float(data.get("unitPrice") or data.get("minPrice") or 0)
                    if api_price > 0:
                        price = api_price
    except Exception as e:
        print(f"[PriceTracker Kaspi API] Ошибка: {e}")

    final_title = title_from_html or f"Товар Kaspi {product_id}"
    return {
        "marketplace": "Kaspi",
        "item_id": product_id,
        "title": final_title,
        "price": price,
        "in_stock": True if price > 0 else True,
        "url": f"https://kaspi.kz/shop/p/-{product_id}/",
    }


async def parse_ozon(url: str) -> Optional[dict]:
    final_url, html_content = await resolve_redirects(url)

    slug_title = None
    slug_match = re.search(r'/product/([a-zA-Z0-9_-]+)-(\d+)', final_url)
    if slug_match:
        raw_words = slug_match.group(1).replace('-', ' ').strip()
        slug_title = raw_words.capitalize()

    title = None
    if html_content:
        t_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html_content) or re.search(r'<title>([^<]+)</title>', html_content)
        if t_match:
            candidate = html.unescape(t_match.group(1)).replace(" - купить в интернет-магазине OZON", "").replace(" - OZON", "").strip()
            if not any(bad in candidate.lower() for bad in ["antibot", "challenge", "доступ ограничен", "ой!"]):
                title = candidate

    final_title = title or slug_title or "Шуруповерт Ozon"

    price = 0.0
    if html_content:
        p_match = (
            re.search(r'"price":\s*"(\d+)"', html_content) or
            re.search(r'itemprop="price"\s+content="(\d+)"', html_content) or
            re.search(r'(\d[\d\s]*)\s*₸', html_content)
        )
        if p_match:
            digits = "".join(c for c in p_match.group(1) if c.isdigit())
            if digits:
                price = float(digits)

    sku_match = re.search(r'/product/(?:[^/]+-)?(\d+)', final_url)
    sku = sku_match.group(1) if sku_match else "ozon_sku"

    return {
        "marketplace": "Ozon",
        "item_id": sku,
        "title": final_title,
        "price": price,
        "in_stock": "нет в наличии" not in html_content.lower(),
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
