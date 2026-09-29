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
import logging
import os
import time
from urllib.parse import parse_qsl

from aiohttp import web

import config

logger = logging.getLogger(__name__)

# initData старше этого срока считаем недействительной (защита от повторного
# использования украденной строки).
INIT_DATA_MAX_AGE_SECONDS = 24 * 60 * 60

INIT_DATA_HEADER = "X-Telegram-Init-Data"

PAGE_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Ада — семейный бюджет</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root { color-scheme: light dark; }
  body {
    margin: 0; padding: 24px 16px;
    font-family: -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
    background: var(--tg-theme-bg-color, #fff);
    color: var(--tg-theme-text-color, #111);
  }
  h1 { font-size: 22px; margin: 0 0 8px; }
  p { margin: 4px 0; color: var(--tg-theme-hint-color, #666); }
</style>
</head>
<body>
<h1 id="title">Ада</h1>
<p id="status">Проверяю вход…</p>
<script>
(async () => {
  const tg = window.Telegram && window.Telegram.WebApp;
  const status = document.getElementById("status");
  const title = document.getElementById("title");
  if (!tg || !tg.initData) {
    status.textContent = "Открой мини-апп из Telegram, через кнопку в боте.";
    return;
  }
  tg.ready();
  tg.expand();
  try {
    const r = await fetch("/api/me", { headers: { "X-Telegram-Init-Data": tg.initData } });
    if (!r.ok) { status.textContent = "Доступ закрыт."; return; }
    const me = await r.json();
    title.textContent = "Привет, " + me.name + "!";
    status.textContent = "Вход подтверждён. Дашборд появится на следующем шаге.";
  } catch (e) {
    status.textContent = "Не удалось связаться с сервером.";
  }
})();
</script>
</body>
</html>
"""


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


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/app", handle_page)
    app.router.add_get("/api/me", handle_me)
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
