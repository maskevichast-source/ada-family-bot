"""Утренний статус для Влада: здоровье бота и баланс ИИ (DeepSeek, OpenAI).

Раз в сутки утром уходит ТОЛЬКО в личку Владу (не в общий чат): проверки Google Sheets и ключей
ИИ, резервные копии, остаток денег у DeepSeek и OpenAI, примерный запас в днях.

DeepSeek отдаёт баланс прямо по API-ключу (GET /user/balance).
OpenAI остаток по обычному ключу не отдаёт: официального эндпоинта нет. Поэтому остаток считается
по расходу: нужен админ-ключ OpenAI (OPENAI_ADMIN_KEY) и стартовая точка — сколько долларов было
на балансе и на какую дату (OPENAI_BALANCE_START_USD, OPENAI_BALANCE_START_DATE). Остаток =
стартовая сумма минус расходы с этой даты по Costs API. Это оценка: данные о расходах могут
отставать, бонусные кредиты и возвраты не учитываются. Без этих переменных бот показывает
только, что ключ работает, и подсказывает, как включить расчёт.
"""
import asyncio
import datetime
import os
import time

from services import state
from services.timezone import ASTANA_TZ

START_TIME = time.time()

DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"

HISTORY_NS = "status_hist"
HISTORY_DAYS = 14

SETUP_HELP = (
    "Как включить баланс OpenAI в утреннем статусе\n\n"
    "У OpenAI нет способа узнать остаток денег обычным ключом, поэтому бот считает его сам: "
    "стартовая сумма минус расходы с выбранной даты.\n\n"
    "1. Зайди на platform.openai.com под аккаунтом владельца организации. В настройках организации "
    "найди раздел с админ-ключами (Admin keys) и создай новый админ-ключ. Он начинается с sk-admin-. "
    "Скопируй его сразу: потом его не покажут.\n"
    "2. Посмотри остаток на счёте: раздел Billing, строка Credit balance. Запомни сумму в долларах.\n"
    "3. Открой Railway, проект бота, вкладку Variables и добавь три переменные:\n"
    "   OPENAI_ADMIN_KEY = админ-ключ из шага 1\n"
    "   OPENAI_BALANCE_START_USD = сумма из шага 2, например 18.50\n"
    "   OPENAI_BALANCE_START_DATE = сегодняшняя дата, например 2026-10-04\n"
    "4. После перезапуска напиши /status: в строке OpenAI появится остаток.\n\n"
    "Каждый раз, когда пополняешь OpenAI, обнови две переменные: новая сумма (остаток плюс пополнение) "
    "и дата пополнения.\n\n"
    "DeepSeek настраивать не нужно: баланс читается по уже заданному DEEPSEEK_API_KEY.\n\n"
    "Пороги предупреждений (по желанию, тоже в Variables): DEEPSEEK_LOW_BALANCE (по умолчанию 2), "
    "OPENAI_LOW_BALANCE_USD (по умолчанию 3), STATUS_LOW_DAYS (по умолчанию 7 дней запаса)."
)


def status_due(now: datetime.datetime, start_hour: int = 9) -> bool:
    """Пора слать утренний статус: с start_hour до 12:00 по Астане (окно на случай позднего перезапуска)."""
    return start_hour <= now.hour < 12


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, "")).replace(",", ".").strip())
    except ValueError:
        return default


def _num(value) -> float:
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return 0.0


def _fmt(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


async def _http_get_json(url: str, headers: dict, params: dict | None = None, timeout: int = 20):
    """(HTTP-статус, JSON или {}). Отдельная функция, чтобы в тестах подменять сеть."""
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.get(url, headers=headers, params=params) as response:
            try:
                data = await response.json(content_type=None)
            except Exception:
                data = {}
            return response.status, data if isinstance(data, dict) else {}


# ── история баланса и запас в днях ─────────────────────────────────────────

def record_balance(name: str, total: float, today: datetime.date) -> list[dict]:
    """Запоминает баланс на сегодня (одна запись в сутки), хранит последние HISTORY_DAYS дней."""
    history = state.get(HISTORY_NS, name) or []
    history = [h for h in history if h.get("date") != today.isoformat()]
    history.append({"date": today.isoformat(), "total": total})
    history = history[-HISTORY_DAYS:]
    state.put(HISTORY_NS, name, history)
    return history


def daily_burn(history: list[dict]) -> float | None:
    """Средний расход в сутки по истории баланса (пополнения игнорируются). None — данных мало."""
    points = sorted(history, key=lambda h: h["date"])
    if len(points) < 2:
        return None
    spent = 0.0
    for before, after in zip(points, points[1:]):
        drop = _num(before["total"]) - _num(after["total"])
        if drop > 0:
            spent += drop
    first = datetime.date.fromisoformat(points[0]["date"])
    last = datetime.date.fromisoformat(points[-1]["date"])
    days = (last - first).days
    return spent / days if days > 0 and spent > 0 else None


def days_left(total: float, burn: float | None) -> int | None:
    if burn is None or burn <= 0:
        return None
    return int(total / burn)


# ── DeepSeek ───────────────────────────────────────────────────────────────

async def deepseek_status(today: datetime.date, api_key: str | None = None) -> dict:
    key = api_key or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        return {"configured": False}
    try:
        status, data = await _http_get_json(DEEPSEEK_BALANCE_URL, {"Authorization": f"Bearer {key}"})
    except Exception as error:
        return {"configured": True, "ok": False, "error": f"не ответил ({type(error).__name__})"}
    if status in (401, 403):
        return {"configured": True, "ok": False, "error": "ключ не принят"}
    if status != 200:
        return {"configured": True, "ok": False, "error": f"ответ {status}"}
    balances = []
    for info in data.get("balance_infos") or []:
        total = _num(info.get("total_balance"))
        currency = str(info.get("currency") or "")
        history = record_balance(f"deepseek:{currency}", total, today)
        burn = daily_burn(history)
        balances.append({
            "currency": currency, "total": total,
            "granted": _num(info.get("granted_balance")), "topped_up": _num(info.get("topped_up_balance")),
            "burn": burn, "days": days_left(total, burn),
        })
    return {"configured": True, "ok": True, "available": bool(data.get("is_available", True)), "balances": balances}


# ── OpenAI ─────────────────────────────────────────────────────────────────

def _parse_start_date(raw: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(raw or "").strip())
    except ValueError:
        return None


async def openai_costs_since(admin_key: str, start: datetime.date, now: datetime.datetime) -> dict:
    """Расход с даты start по Costs API: {"spent": $, "last7": $ за последние 7 суток, "days": число суток}."""
    start_ts = int(datetime.datetime.combine(start, datetime.time(0, 0), tzinfo=ASTANA_TZ).timestamp())
    week_ago_ts = int((now - datetime.timedelta(days=7)).timestamp())
    spent = last7 = 0.0
    params = {"start_time": start_ts, "bucket_width": "1d", "limit": 180}
    for _ in range(20):                                   # страховка от бесконечной пагинации
        status, data = await _http_get_json(OPENAI_COSTS_URL, {"Authorization": f"Bearer {admin_key}"}, params)
        if status in (401, 403):
            raise PermissionError("админ-ключ не принят")
        if status != 200:
            raise RuntimeError(f"ответ {status}")
        for bucket in data.get("data") or []:
            amount = sum(_num((r.get("amount") or {}).get("value")) for r in bucket.get("results") or [])
            spent += amount
            if int(bucket.get("start_time") or 0) >= week_ago_ts:
                last7 += amount
        if data.get("has_more") and data.get("next_page"):
            params = {**params, "page": data["next_page"]}
        else:
            break
    days = max(1, min(7, (now.date() - start).days))
    return {"spent": spent, "last7": last7, "days": days}


async def openai_status(now: datetime.datetime) -> dict:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return {"configured": False}
    result = {"configured": True}
    try:
        status, _ = await _http_get_json(OPENAI_MODELS_URL, {"Authorization": f"Bearer {key}"})
        if status in (401, 403):
            return {**result, "ok": False, "error": "ключ не принят"}
        if status != 200:
            return {**result, "ok": False, "error": f"ответ {status}"}
    except Exception as error:
        return {**result, "ok": False, "error": f"не ответил ({type(error).__name__})"}
    result["ok"] = True

    admin = os.getenv("OPENAI_ADMIN_KEY")
    start_usd = os.getenv("OPENAI_BALANCE_START_USD")
    start_date = _parse_start_date(os.getenv("OPENAI_BALANCE_START_DATE", ""))
    if not (admin and start_usd and start_date):
        result["balance"] = None                         # расчёт не настроен
        return result
    try:
        costs = await openai_costs_since(admin, start_date, now)
    except Exception as error:
        result["balance"] = None
        result["balance_error"] = str(error) or type(error).__name__
        return result
    start_amount = _num(start_usd)
    remaining = start_amount - costs["spent"]
    burn = costs["last7"] / costs["days"] if costs["days"] else None
    result["balance"] = {
        "start": start_amount, "since": start_date.isoformat(), "spent": costs["spent"],
        "remaining": remaining, "burn": burn, "days": days_left(max(remaining, 0), burn),
    }
    return result


# ── здоровье бота ──────────────────────────────────────────────────────────

def sheets_health(now: datetime.datetime) -> dict:
    """Читает таблицу и считает вчерашние записи. Блокирующая — вызывать через to_thread."""
    from services import sheets
    started = time.time()
    try:
        today = now.date()
        yesterday = today - datetime.timedelta(days=1)
        rows = sheets.get_transactions_for_period(yesterday.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))
        return {"ok": True, "seconds": time.time() - started, "yesterday": len(rows)}
    except Exception as error:
        return {"ok": False, "error": f"{type(error).__name__}: {str(error)[:80]}"}


def backup_info() -> dict:
    last = ""
    try:
        for key, value in state.entries("scheduler"):
            if str(key).startswith("backup:") and isinstance(value, dict) and value.get("done_at"):
                last = max(last, str(value["done_at"]))
    except Exception:
        pass
    return {"configured": True, "last": last}


async def collect(now: datetime.datetime) -> dict:
    """Собирает все проверки. Сбой одной не мешает остальным."""
    async def safe(coro, label):
        try:
            return await coro
        except Exception as error:
            return {"ok": False, "error": f"{label}: {type(error).__name__}"}

    deepseek, openai_data, sheets_data = await asyncio.gather(
        safe(deepseek_status(now.date()), "DeepSeek"),
        safe(openai_status(now), "OpenAI"),
        safe(asyncio.to_thread(sheets_health, now), "Sheets"),
    )
    try:
        backup_data = backup_info()
    except Exception:
        backup_data = {"configured": False}
    return {"deepseek": deepseek, "openai": openai_data, "sheets": sheets_data, "backup": backup_data}


# ── текст ──────────────────────────────────────────────────────────────────

def _days_text(days: int | None) -> str:
    return f", хватит примерно на {days} дн." if days is not None else ""


def _deepseek_line(data: dict, problems: list) -> str:
    if not data.get("configured"):
        return "DeepSeek: ключ не задан ⚠️"
    if not data.get("ok"):
        problems.append("DeepSeek")
        return f"DeepSeek: {data.get('error')} ⚠️"
    balances = data.get("balances") or []
    if not balances:
        return "DeepSeek: ключ работает, баланс не вернулся"
    low_limit = _env_float("DEEPSEEK_LOW_BALANCE", 2.0)
    low_days = _env_float("STATUS_LOW_DAYS", 7)
    parts = []
    warn = not data.get("available", True)
    for b in balances:
        text = f"{_fmt(b['total'])} {b['currency']}"
        if b.get("granted"):
            text += f" (пополнено {_fmt(b['topped_up'])}, бонус {_fmt(b['granted'])})"
        if b.get("burn"):
            text += f", тратится около {_fmt(b['burn'])} в сутки{_days_text(b.get('days'))}"
        parts.append(text)
        if b["total"] < low_limit or (b.get("days") is not None and b["days"] < low_days):
            warn = True
    if warn:
        problems.append("пора пополнить DeepSeek")
    return "DeepSeek: " + "; ".join(parts) + (" ⚠️ пора пополнять" if warn else "")


def _openai_line(data: dict, problems: list) -> str:
    if not data.get("configured"):
        return "OpenAI: ключ не задан ⚠️"
    if not data.get("ok"):
        problems.append("OpenAI")
        return f"OpenAI: {data.get('error')} ⚠️"
    balance = data.get("balance")
    if balance is None:
        extra = f" Расчёт остатка не получился ({data['balance_error']})." if data.get("balance_error") else \
            " Остаток не настроен, как включить: /status настройка"
        if data.get("balance_error"):
            problems.append("расчёт остатка OpenAI")
        return "OpenAI: ключ работает." + extra
    low_limit = _env_float("OPENAI_LOW_BALANCE_USD", 3.0)
    low_days = _env_float("STATUS_LOW_DAYS", 7)
    remaining = balance["remaining"]
    text = (f"OpenAI: остаток по расчёту около ${_fmt(max(remaining, 0))} "
            f"(с {balance['since']} потрачено ${_fmt(balance['spent'])})")
    if balance.get("burn"):
        text += f", в среднем ${_fmt(balance['burn'])} в сутки{_days_text(balance.get('days'))}"
    warn = remaining < low_limit or (balance.get("days") is not None and balance["days"] < low_days)
    if warn:
        problems.append("пора пополнить OpenAI")
        text += " ⚠️ пора пополнять"
    return text


def format_report(data: dict, now: datetime.datetime) -> tuple[str, int]:
    """(текст сообщения, число проблем). Простой текст без разметки."""
    problems: list[str] = []
    lines = []

    uptime_from = datetime.datetime.fromtimestamp(START_TIME, ASTANA_TZ).strftime("%d.%m %H:%M")
    version = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "")[:7]
    lines.append(f"Бот: работает{', версия ' + version if version else ''}, запущен {uptime_from}")

    sheets_data = data.get("sheets") or {}
    if sheets_data.get("ok"):
        lines.append(f"Google Sheets: ок ({_fmt(sheets_data['seconds'], 1)} с), вчера записано: {sheets_data['yesterday']}")
    else:
        problems.append("Google Sheets")
        lines.append(f"Google Sheets: не читается ({sheets_data.get('error', 'нет данных')}) ⚠️")

    backup_data = data.get("backup") or {}
    if not backup_data.get("configured"):
        problems.append("нет резервных копий")
        lines.append("Резервные копии: не настроены ⚠️ (/backup подскажет, как включить)")
    elif backup_data.get("last"):
        stamp = datetime.datetime.fromisoformat(backup_data["last"]).strftime("%d.%m.%Y %H:%M")
        lines.append(f"Резервные копии: последняя {stamp}")
    else:
        lines.append("Резервные копии: настроены, ещё не делались (первая в воскресенье ночью)")

    lines.append(_deepseek_line(data.get("deepseek") or {}, problems))
    lines.append(_openai_line(data.get("openai") or {}, problems))

    head = f"☀️ Утренний статус Ады · {now.strftime('%d.%m.%Y')}"
    verdict = "Всё в порядке ✅" if not problems else f"Есть что проверить ⚠️ ({len(problems)})"
    return f"{head}\n{verdict}\n\n" + "\n".join(lines), len(problems)


async def build_report(now: datetime.datetime) -> str:
    data = await collect(now)
    return format_report(data, now)[0]
