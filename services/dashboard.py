"""Данные для дашборда мини-аппа (только чтение).

Цифры считает код, тем же способом, что /report и предупреждение о лимите:
берутся `amt` и тип операции из get_transactions_for_period, лимиты — из листа
Limits. Один запрос в Google Sheets покрывает сразу прошлый и текущий месяц.
"""
import datetime
import threading
import time

from services.categories import is_income_type
from services.money import parse_amount
from services.timezone import ASTANA_TZ, now_astana, parse_flexible_datetime

# Порог «жёлтой» зоны совпадает с предупреждением о лимите в main.py (85%).
WARN_RATIO = 0.85

CACHE_TTL_SECONDS = 90
EMPTY_CACHE_TTL_SECONDS = 15   # пустой результат мог быть сбоем чтения Sheets
MIN_REFRESH_INTERVAL_SECONDS = 15

_MONTHS_RU = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def _days_in_month(year: int, month: int) -> int:
    nxt = datetime.date(year + (month == 12), month % 12 + 1, 1)
    return (nxt - datetime.date(year, month, 1)).days


def _month_label(year: int, month: int) -> str:
    return f"{_MONTHS_RU[month - 1]} {year}"


def _delta_pct(current: float, previous: float):
    if previous <= 0:
        return None
    return round((current - previous) / previous * 100)


def limit_status(spent: float, limit: float | None) -> str:
    """none — лимита нет; ok; warn — от 85%; over — потрачено больше лимита."""
    if not limit or limit <= 0:
        return "none"
    if spent > limit:
        return "over"
    if spent / limit >= WARN_RATIO:
        return "warn"
    return "ok"


def compute_dashboard(transactions: list[dict], limits: dict, now: datetime.datetime) -> dict:
    """Чистая функция: ничего не читает из сети, удобна для тестов."""
    year, month, today = now.year, now.month, now.day
    cur_start = datetime.datetime(year, month, 1, tzinfo=ASTANA_TZ)

    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    prev_start = datetime.datetime(prev_year, prev_month, 1, tzinfo=ASTANA_TZ)
    # То же число, что сегодня: сравниваем «на сегодняшний день», а не с
    # итогом месяца (иначе в начале месяца всё выглядело бы заниженным).
    prev_cutoff = min(prev_start + datetime.timedelta(days=today), cur_start)

    cur_income = cur_expense = prev_income = prev_expense = 0.0
    by_cat: dict[str, float] = {}
    cur_count = 0

    for t in transactions:
        dt = parse_flexible_datetime(t.get("date"))
        if dt is None:
            continue
        amount = parse_amount(t.get("amt", 0))
        income = is_income_type(t.get("type"))
        if dt >= cur_start:
            cur_count += 1
            if income:
                cur_income += amount
            else:
                cur_expense += amount
                cat = str(t.get("cat") or "").strip() or "Без категории"
                by_cat[cat] = by_cat.get(cat, 0.0) + amount
        elif prev_start <= dt < prev_cutoff:
            if income:
                prev_income += amount
            else:
                prev_expense += amount

    categories = []
    for name, spent in by_cat.items():
        limit = float(limits.get(name) or 0) or None
        categories.append({
            "name": name,
            "spent": round(spent, 2),
            "share_pct": round(spent / cur_expense * 100) if cur_expense > 0 else 0,
            "limit": limit,
            "limit_pct": round(spent / limit * 100) if limit else None,
            "status": limit_status(spent, limit),
        })
    categories.sort(key=lambda c: c["spent"], reverse=True)

    return {
        "month_label": _month_label(year, month),
        "day": today,
        "days_in_month": _days_in_month(year, month),
        "updated_at": now.strftime("%H:%M"),
        "operations": cur_count,
        "income": round(cur_income, 2),
        "expense": round(cur_expense, 2),
        "balance": round(cur_income - cur_expense, 2),
        "prev": {
            "label": _MONTHS_RU[prev_month - 1],
            "expense": round(prev_expense, 2),
            "income": round(prev_income, 2),
            "expense_delta_pct": _delta_pct(cur_expense, prev_expense),
            "income_delta_pct": _delta_pct(cur_income, prev_income),
        },
        "categories": categories,
    }


def load_dashboard(now: datetime.datetime | None = None) -> dict:
    """Читает Google Sheets и считает дашборд (синхронно, без кэша)."""
    from services.sheets import get_category_limits, get_transactions_for_period

    now = now or now_astana()
    prev_year, prev_month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    start = datetime.date(prev_year, prev_month, 1).strftime("%Y-%m-%d")
    # +1 день: период считается как [start, end), а сегодня должно попасть.
    end = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    transactions = get_transactions_for_period(start, end)
    limits = get_category_limits()
    return compute_dashboard(transactions, limits, now)


_cache: dict = {"at": 0.0, "ttl": 0, "data": None}
_lock = threading.Lock()


def get_dashboard(force: bool = False, loader=load_dashboard, clock=time.monotonic) -> dict:
    """Дашборд с кэшем на 1–2 минуты, чтобы не упираться в лимиты Google Sheets."""
    with _lock:
        age = clock() - _cache["at"]
        if _cache["data"] is not None:
            fresh = age < _cache["ttl"]
            too_soon = age < MIN_REFRESH_INTERVAL_SECONDS
            if (fresh and not force) or too_soon:
                return _cache["data"]
        data = loader()
        _cache.update(
            at=clock(),
            data=data,
            ttl=CACHE_TTL_SECONDS if data.get("operations") or data["prev"]["expense"] else EMPTY_CACHE_TTL_SECONDS,
        )
        return data


def reset_cache() -> None:
    with _lock:
        _cache.update(at=0.0, ttl=0, data=None)
