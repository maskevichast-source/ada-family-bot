"""Парсер цен и остатков товаров: Wildberries, Kaspi и Ozon через эмуляцию Chrome 124 (curl_cffi)."""

import re
import html
import json
import asyncio
from typing import Optional
from curl_cffi.requests import AsyncSession


def detect_marketplace(url: str) -> Optional[str]:
    u = url.lower()
    if any(d in u for d in ["wildberries.ru", "wildberries.kz", "wb.ru", "wb.kz"]):
        return "Wildberries"
    if any(d in u for d in ["kaspi.kz", "l.kaspi.kz"]):
        return "Kaspi"
    if any(d in u for d in ["ozon.ru", "ozon.kz"]):
        return "Ozon"
    return None


def clean_kaspi_title(raw_title: str) -> str:
    t = html.unescape(raw_title).strip()
    t = re.sub(r'^(?:Купить\s+)', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s+в\s+[А-Яа-яЁёA-Za-z\s–-]+–\s*Магазин\s+на\s+Kaspi\.kz.*$', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s*–\s*Магазин\s+на\s+Kaspi\.kz.*$', '', t, flags=re.IGNORECASE)
    return t.strip() or "Товар Kaspi"


def get_wb_basket_host(nm_id: int) -> str:
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


# ═══════════════════════════════════════════════════════════════════════════════
# 1. WILDBERRIES (WB)
# ═══════════════════════════════════════════════════════════════════════════════

async def parse_wildberries(url: str) -> Optional[dict]:
    match = re.search(r'/catalog/(\d+)', url)
    if not match:
        return None
    nm_id = int(match.group(1))

    title = None
    price = 0.0
    in_stock = True

    async with AsyncSession(impersonate="chrome124") as session:
        # 1. Забираем точное название товара через CDN корзин
        try:
            basket_host = get_wb_basket_host(nm_id)
            vol = nm_id // 100000
            part = nm_id // 1000
            cdn_url = f"https://{basket_host}/vol{vol}/part{part}/{nm_id}/info/ru/card.json"
            resp = await session.get(cdn_url, timeout=8)
            if resp.status_code == 200:
                cdn_data = resp.json()
                title = cdn_data.get("imt_name") or cdn_data.get("name")
        except Exception as e:
            print(f"[WB CDN] Ошибка названия: {e}")

        # 2. Забираем цены в KZT через API
        api_urls = [
            f"https://card.wb.ru/cards/v2/detail?appType=1&curr=kzt&dest=-1257786&nm={nm_id}",
            f"https://card.wb.ru/cards/v1/detail?appType=1&curr=kzt&dest=-1257786&nm={nm_id}",
        ]

        for api_url in api_urls:
            try:
                resp = await session.get(api_url, timeout=8)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                products = data.get("data", {}).get("products", [])
                if not products:
                    continue

                p = products[0]
                if not title:
                    title = p.get("name")

                # Поиск цены в корне и в массиве размеров
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
                print(f"[WB API] Ошибка цены: {e}")

    final_title = title or f"Товар Wildberries {nm_id}"
    return {
        "marketplace": "Wildberries",
        "item_id": str(nm_id),
        "title": final_title,
        "price": price,
        "in_stock": in_stock,
        "url": f"https://www.wildberries.kz/catalog/{nm_id}/detail.aspx",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 2. KASPI МАГАЗИН
# ═══════════════════════════════════════════════════════════════════════════════

async def parse_kaspi(url: str) -> Optional[dict]:
    final_url = url
    html_content = ""
    product_id = None
    title = None
    price = 0.0

    async with AsyncSession(impersonate="chrome124") as session:
        # Разворачиваем короткие ссылки (l.kaspi.kz)
        try:
            resp = await session.get(url, allow_redirects=True, timeout=10)
            final_url = str(resp.url)
            html_content = resp.text
        except Exception as e:
            print(f"[Kaspi Redirect] Ошибка: {e}")

        # Ищем ID товара в URL
        match = re.search(r'-(\d+)(?:/|\?|$)', final_url) or re.search(r'/p/[^/]+-(\d+)', final_url)
        if match:
            product_id = match.group(1)

        # Если в URL ID нет, ищем в HTML страницы
        if html_content:
            if not product_id:
                id_m = re.search(r'kaspi\.kz/shop/p/[^"]*?-(\d+)', html_content) or re.search(r'data-product-id="(\d+)"', html_content)
                if id_m:
                    product_id = id_m.group(1)

            t_m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html_content)
            if t_m:
                title = clean_kaspi_title(t_m.group(1))

            # Поиск цены в микроразметке страницы
            p_m = re.search(r'"price":\s*"(\d+)"', html_content) or re.search(r'"lowPrice":\s*"(\d+)"', html_content) or re.search(r'data-price="(\d+)"', html_content)
            if p_m:
                price = float(p_m.group(1))

        # Запрос к API каталога Kaspi по Астане (город 710000000)
        if product_id:
            api_url = f"https://kaspi.kz/yml/product-view/p/{product_id}?c=710000000"
            headers = {
                "Referer": "https://kaspi.kz/",
                "Cookie": "kaspi.store.city=710000000",
            }
            try:
                api_resp = await session.get(api_url, headers=headers, timeout=8)
                if api_resp.status_code == 200:
                    api_data = api_resp.json()
                    raw_t = api_data.get("title")
                    if raw_t:
                        title = clean_kaspi_title(raw_t)

                    api_price = float(api_data.get("unitPrice") or api_data.get("minPrice") or 0)
                    if api_price > 0:
                        price = api_price
            except Exception as e:
                print(f"[Kaspi API] Ошибка цен: {e}")

    if not product_id and not title:
        return None

    final_title = title or f"Товар Kaspi {product_id}"
    return {
        "marketplace": "Kaspi",
        "item_id": product_id or "kaspi_item",
        "title": final_title,
        "price": price,
        "in_stock": True,
        "url": f"https://kaspi.kz/shop/p/-{product_id}/" if product_id else final_url,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 3. OZON (.RU / .KZ)
# ═══════════════════════════════════════════════════════════════════════════════

async def parse_ozon(url: str) -> Optional[dict]:
    final_url = url
    html_content = ""
    title = None
    price = 0.0

    async with AsyncSession(impersonate="chrome124") as session:
        try:
            resp = await session.get(url, allow_redirects=True, timeout=12)
            final_url = str(resp.url)
            html_content = resp.text
        except Exception as e:
            print(f"[Ozon Fetch] Ошибка: {e}")

    # Извлекаем понятное название из адреса (slug)
    slug_match = re.search(r'/product/([a-zA-Z0-9_-]+)-(\d+)', final_url)
    slug_title = None
    sku = "ozon_sku"
    if slug_match:
        raw_slug = slug_match.group(1).replace('-', ' ').strip()
        slug_title = raw_slug.capitalize()
        sku = slug_match.group(2)

    # Извлекаем название из OpenGraph
    if html_content:
        t_m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html_content) or re.search(r'<title>([^<]+)</title>', html_content)
        if t_m:
            candidate = html.unescape(t_m.group(1)).replace(" - купить в интернет-магазине OZON", "").replace(" - OZON", "").strip()
            # Фильтруем заглушки Cloudflare
            if not any(bad in candidate.lower() for bad in ["antibot", "challenge", "доступ ограничен", "ой!"]):
                title = candidate

        # Извлечение цены из микроразметки Schema.org / JSON-LD
        p_m = (
            re.search(r'"price":\s*"(\d+)"', html_content) or
            re.search(r'itemprop="price"\s+content="(\d+)"', html_content) or
            re.search(r'data-price="(\d+)"', html_content) or
            re.search(r'(\d[\d\s]*)\s*₸', html_content)
        )
        if p_m:
            digits = "".join(c for c in p_m.group(1) if c.isdigit())
            if digits:
                price = float(digits)

    final_title = title or slug_title or "Шуруповерт Ozon"
    in_stock = "нет в наличии" not in html_content.lower()

    return {
        "marketplace": "Ozon",
        "item_id": sku,
        "title": final_title,
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
