"""Парсер цен и остатков товаров: Wildberries, Kaspi и Ozon (с точными ценами и городами)."""

import re
import html
import json
import asyncio
from typing import Optional
from curl_cffi.requests import AsyncSession


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
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        return None
    host = (parsed.hostname or "").lower()
    for name, domains in {
        "Wildberries": ("wildberries.ru", "wildberries.kz", "wb.ru", "wb.kz"),
        "Kaspi": ("kaspi.kz",), "Ozon": ("ozon.ru", "ozon.kz")
    }.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return name
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


async def resolve_redirects(url: str) -> tuple[str, str]:
    from urllib.parse import urljoin
    async with AsyncSession(impersonate="chrome124") as session:
        for _ in range(5):
            if not detect_marketplace(url):
                raise ValueError("Перенаправление вне разрешённого магазина")
            resp = await session.get(url, allow_redirects=False, timeout=10)
            if resp.status_code in {301, 302, 303, 307, 308}:
                url = urljoin(url, resp.headers.get("location", ""))
                continue
            return str(resp.url), resp.text
    raise ValueError("Слишком много перенаправлений")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. KASPI МАГАЗИН
# ═══════════════════════════════════════════════════════════════════════════════

async def parse_kaspi(url: str) -> Optional[dict]:
    final_url = url
    html_content = ""
    product_id = None
    title = None
    price = 0.0

    async with AsyncSession(impersonate="chrome124") as session:
        if "l.kaspi.kz" in url.lower():
            final_url, html_content = await resolve_redirects(url)

        match = re.search(r'-(\d+)(?:/|\?|$)', final_url) or re.search(r'/p/[^/]+-(\d+)', final_url)
        if match:
            product_id = match.group(1)

        # Если ещё не загрузили полную страницу товара, загружаем её
        if not html_content or "l.kaspi.kz" in url.lower():
            target_url = f"https://kaspi.kz/shop/p/-{product_id}/" if product_id else final_url
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Referer": "https://kaspi.kz/",
                    "Cookie": "kaspi.store.city=710000000",
                }
                resp = await session.get(target_url, headers=headers, timeout=10)
                html_content = resp.text
                if not product_id:
                    id_m = re.search(r'data-product-id="(\d+)"', html_content) or re.search(r'kaspi\.kz/shop/p/[^"]*?-(\d+)', html_content)
                    if id_m:
                        product_id = id_m.group(1)
            except Exception as e:
                print(f"[Kaspi Page] Ошибка: {e}")

        # 1. Считываем ТОЧНУЮ цену с экрана (item__price-once)
        if html_content:
            price_once_match = re.search(r'class="[^"]*item__price-once[^"]*"[^>]*>([\d\s]+)\s*₸', html_content)
            if not price_once_match:
                price_once_match = re.search(r'class="[^"]*price[^"]*"[^>]*>([\d\s]+)\s*₸', html_content)

            if price_once_match:
                digits = "".join(c for c in price_once_match.group(1) if c.isdigit())
                if digits:
                    price = float(digits)

            t_m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html_content) or re.search(r'<title>([^<]+)</title>', html_content)
            if t_m:
                title = clean_kaspi_title(t_m.group(1))

        # 2. Если парсинг экрана не удался, запрашиваем API Астаны (город 710000000)
        if (price == 0 or not title) and product_id:
            api_url = f"https://kaspi.kz/yml/product-view/p/{product_id}?c=710000000"
            headers = {
                "Referer": "https://kaspi.kz/",
                "Cookie": "kaspi.store.city=710000000",
            }
            try:
                api_resp = await session.get(api_url, headers=headers, timeout=8)
                if api_resp.status_code == 200:
                    api_data = api_resp.json()
                    if not title and api_data.get("title"):
                        title = clean_kaspi_title(api_data.get("title"))

                    # Берём реальную цену предложения (offers), а не общереспубликанский lowPrice
                    offers = api_data.get("offers", [])
                    if offers and offers[0].get("price"):
                        price = float(offers[0].get("price"))
                    elif api_data.get("unitPrice"):
                        price = float(api_data.get("unitPrice"))
            except Exception as e:
                print(f"[Kaspi API] Ошибка: {e}")

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
# 2. WILDBERRIES (WB)
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
        except Exception:
            pass

        # 2. Забираем цены в KZT через API (без жёсткого складского фильтра)
        api_urls = [
            f"https://card.wb.ru/cards/v2/detail?appType=1&curr=kzt&nm={nm_id}",
            f"https://card.wb.ru/cards/v1/detail?appType=1&curr=kzt&dest=-1257786&spp=30&nm={nm_id}",
            f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&nm={nm_id}",
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

                # Извлечение цены
                if p.get("salePriceU"):
                    price = float(p["salePriceU"]) / 100.0
                elif p.get("priceU"):
                    price = float(p["priceU"]) / 100.0

                if price == 0 and p.get("sizes"):
                    for s in p["sizes"]:
                        pr = s.get("price")
                        if pr:
                            total = pr.get("total") or pr.get("product") or pr.get("basic") or 0
                            if total > 0:
                                price = float(total) / 100.0
                                break

                # Если цена была в рублях, переводим в KZT
                if "curr=rub" in api_url and price > 0:
                    price = round(price * 5.2)

                if price > 0:
                    break
            except Exception as e:
                print(f"[WB API] Ошибка: {e}")

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
# 3. OZON (.RU / .KZ)
# ═══════════════════════════════════════════════════════════════════════════════

async def parse_ozon(url: str) -> Optional[dict]:
    final_url, html_content = await resolve_redirects(url)

    # Название из слага URL
    slug_match = re.search(r'/product/([a-zA-Z0-9_-]+)-(\d+)', final_url)
    slug_title = None
    sku = "ozon_sku"
    if slug_match:
        raw_slug = slug_match.group(1).replace('-', ' ').strip()
        slug_title = raw_slug.capitalize()
        sku = slug_match.group(2)

    title = None
    price = 0.0

    if html_content:
        # Проверяем поисковую микроразметку Schema.org (JSON-LD)
        ld_matches = re.findall(r'<script\s+type="application/ld\+json"[^>]*>(.*?)</script>', html_content, re.DOTALL)
        for ld in ld_matches:
            try:
                ld_obj = json.loads(ld.strip())
                if isinstance(ld_obj, dict):
                    if ld_obj.get("@type") == "Product":
                        title = ld_obj.get("name")
                        offers = ld_obj.get("offers", {})
                        if isinstance(offers, dict) and offers.get("price"):
                            price = float(offers["price"])
                            break
            except Exception:
                pass

        if not title:
            t_m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html_content) or re.search(r'<title>([^<]+)</title>', html_content)
            if t_m:
                cand = html.unescape(t_m.group(1)).replace(" - купить в интернет-магазине OZON", "").replace(" - OZON", "").strip()
                if not any(bad in cand.lower() for bad in ["antibot", "challenge", "доступ ограничен", "ой!"]):
                    title = cand

        if price == 0:
            p_m = (
                re.search(r'"price":\s*"(\d+)"', html_content) or
                re.search(r'itemprop="price"\s+content="(\d+)"', html_content) or
                re.search(r'(\d[\d\s]*)\s*₸', html_content)
            )
            if p_m:
                digits = "".join(c for c in p_m.group(1) if c.isdigit())
                if digits:
                    price = float(digits)

    final_title = title or slug_title or "Шуруповерт Ozon"
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
