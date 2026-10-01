"""Веб-слой мини-аппа Telegram (только чтение).

Работает в том же процессе, что и бот, на порту PORT (его выдаёт Railway).
Любой сбой веб-части не должен ронять бота: запуск обёрнут в try/except.

Безопасность: Telegram передаёт в мини-апп подписанную строку initData.
Сервер проверяет подпись токеном бота и пускает только Влада и Диану
(по Telegram ID, через config.get_authorized_user_name).
"""
import hashlib
import hmac
import json
import asyncio
import logging
import os
from pathlib import Path
import time
from urllib.parse import parse_qsl

from aiohttp import web

import config

logger = logging.getLogger(__name__)

# initData старше этого срока считаем недействительной (защита от повторного
# использования украденной строки).
INIT_DATA_MAX_AGE_SECONDS = 24 * 60 * 60

INIT_DATA_HEADER = "X-Telegram-Init-Data"

PAGE_PATH = Path(__file__).with_name("webapp_page.html")
PAGE_HTML = PAGE_PATH.read_text(encoding="utf-8")


def verify_init_data(init_data: str, bot_token: str, max_age: int = INIT_DATA_MAX_AGE_SECONDS,
                     now: float | None = None) -> dict | None:
    """Проверяет подпись initData по алгоритму Telegram.

    Возвращает словарь полей (user уже разобран из JSON) или None, если подпись
    неверна, строка пуста/повреждена или устарела.
    """
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    current = time.time() if now is None else now
    if auth_date <= 0 or current - auth_date > max_age:
        return None

    if "user" in pairs:
        try:
            pairs["user"] = json.loads(pairs["user"])
        except ValueError:
            return None
    return pairs


def authorize_request(request: web.Request) -> str | None:
    """Имя члена семьи по подписанному initData из заголовка или None."""
    init_data = request.headers.get(INIT_DATA_HEADER, "")
    data = verify_init_data(init_data, config.TELEGRAM_BOT_TOKEN or "")
    if not data:
        return None
    user = data.get("user")
    if not isinstance(user, dict) or "id" not in user:
        return None
    return config.get_authorized_user_name(user["id"])


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def handle_page(request: web.Request) -> web.Response:
    return web.Response(text=PAGE_HTML, content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-store"})


async def handle_me(request: web.Request) -> web.Response:
    name = authorize_request(request)
    if not name:
        return web.json_response({"error": "forbidden"}, status=403)
    return web.json_response({"name": name})


async def handle_dashboard(request: web.Request) -> web.Response:
    viewer = authorize_request(request)
    if not viewer:
        return web.json_response({"error": "forbidden"}, status=403)
    from services.dashboard import ALLOWED_DAYS, ALLOWED_PERIODS, FAMILY, get_dashboard

    force = request.query.get("refresh") == "1"
    period = request.query.get("period", "1").strip().lower()
    days = None
    months = 1
    if period.endswith("d"):                         # «7d» — последние 7 календарных дней
        try:
            days = int(period[:-1])
        except ValueError:
            days = None
        if days not in ALLOWED_DAYS:
            days = None
    else:
        try:
            months = int(period)
        except ValueError:
            months = 1
        if months not in ALLOWED_PERIODS:
            months = 1
    other = next((n for n in FAMILY if n != viewer), None)
    who = request.query.get("who", "family")
    person = {"me": viewer, "other": other}.get(who)   # family и всё прочее -> вся семья
    category = request.query.get("cat", "").strip()[:80] or None
    query = request.query.get("q", "").strip()[:60] or None
    try:
        data = await asyncio.to_thread(get_dashboard, force, months, person, category, query, days)
    except Exception as e:  # noqa: BLE001
        logger.error("Дашборд: ошибка расчёта: %s", e)
        return web.json_response({"error": "unavailable"}, status=503)
    data = dict(data, viewer=viewer, other=other)
    return web.json_response(data, headers={"Cache-Control": "no-store"})


async def handle_loans(request: web.Request) -> web.Response:
    if not authorize_request(request):
        return web.json_response({"error": "forbidden"}, status=403)
    from services import dashboard
    from services.loans_view import compute_loans

    def build():
        raw = dashboard.get_raw(request.query.get("refresh") == "1")
        return compute_loans(raw["transactions"], raw["now"])

    try:
        data = await asyncio.to_thread(build)
    except Exception as e:  # noqa: BLE001
        logger.error("Кредиты: ошибка расчёта: %s", e)
        return web.json_response({"error": "unavailable"}, status=503)
    return web.json_response(data, headers={"Cache-Control": "no-store"})


async def handle_lists(request: web.Request) -> web.Response:
    if not authorize_request(request):
        return web.json_response({"error": "forbidden"}, status=403)
    from services.lists_view import get_lists

    force = request.query.get("refresh") == "1"
    try:
        data = await asyncio.to_thread(get_lists, force)
    except Exception as e:  # noqa: BLE001
        logger.error("Списки: ошибка загрузки: %s", e)
        return web.json_response({"error": "unavailable"}, status=503)
    return web.json_response(data, headers={"Cache-Control": "no-store"})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/app", handle_page)
    app.router.add_get("/api/me", handle_me)
    app.router.add_get("/api/dashboard", handle_dashboard)
    app.router.add_get("/api/lists", handle_lists)
    app.router.add_get("/api/loans", handle_loans)
    return app


def webapp_url() -> str:
    """Публичный адрес страницы мини-аппа (задаётся в Railway → Variables)."""
    base = (os.getenv("WEBAPP_URL") or "").strip().rstrip("/")
    return f"{base}/app" if base else ""


async def start_webapp() -> web.AppRunner | None:
    """Запускает веб-сервер рядом с ботом. Возвращает runner или None.

    Никогда не бросает исключение: бот важнее мини-аппа.
    """
    if (os.getenv("WEBAPP_ENABLED", "true").strip().lower() in ("0", "false", "no", "off")):
        logger.info("Мини-апп выключен (WEBAPP_ENABLED=false)")
        return None
    try:
        port = int(os.getenv("PORT", "8080"))
        runner = web.AppRunner(build_app())
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info("Мини-апп запущен на порту %s", port)
        return runner
    except Exception as e:  # noqa: BLE001 - веб-слой не должен ронять бота
        logger.error("Мини-апп не запустился: %s", e)
        return None
