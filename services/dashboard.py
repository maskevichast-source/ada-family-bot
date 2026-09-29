"""Данные для дашборда мини-аппа (только чтение).

Цифры считает код, тем же способом, что /report и предупреждение о лимите:
берутся `amt` и тип операции из get_transactions_for_period, лимиты — из листа
Limits. Сырые данные за 2 года читаются ОДНИМ запросом в Google Sheets и
кэшируются; переключение периода и человека считается уже в памяти.
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

ALLOWED_PERIODS = (1, 3, 6, 12)   # месяцев, включая текущий
MAX_EXPENSE_ROWS = 200            # сколько крупнейших трат отдаём в страницу
FETCH_MONTHS_BACK = 23            # хватает для «год» и сравнения с предыдущим годом

FAMILY = ("Влад", "Диана")

_MONTHS_RU = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]
_MONTHS_PREP = [  # «в …»
    "январе", "феврале", "марте", "апреле", "мае", "июне",
    "июле", "августе", "сентябре", "октябре", "ноябре", "декабре",
]
_MONTHS_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _days_in_month(year: int, month: int) -> int:
    ny, nm = add_months(year, month, 1)
    return (datetime.date(ny, nm, 1) - datetime.date(year, month, 1)).days


def _month_start(year: int, month: int) -> datetime.datetime:
    return datetime.datetime(year, month, 1, tzinfo=ASTANA_TZ)


def _period_label(year: int, month: int, months: int) -> str:
    if months == 1:
        return f"{_MONTHS_RU[month - 1]} {year}"
    sy, sm = add_months(year, month, -(months - 1))
    return f"{_MONTHS_SHORT[sm - 1]} {sy} – {_MONTHS_SHORT[month - 1]} {year}"


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


def _user_of(t: dict) -> str:
    from config import normalize_family_user_name
    raw = str(t.get("user") or "").strip()
    return normalize_family_user_name(raw) or raw


def _short_text(t: dict) -> str:
    for key in ("merchant", "comm", "subcat"):
        value = str(t.get(key) or "").strip()
        if value:
            return value[:80]
    return ""


def compute_dashboard(transactions: list[dict], limits: dict, now: datetime.datetime,
                      months: int = 1, person: str | None = None) -> dict:
    """Чистая функция: ничего не читает из сети, удобна для тестов.

    months — сколько календарных месяцев включая текущий (1/3/6/12).
    person — имя (Влад/Диана) или None для всей семьи.
    """
    year, month = now.year, now.month
    sy, sm = add_months(year, month, -(months - 1))
    start = _month_start(sy, sm)
    end = datetime.datetime(now.year, now.month, now.day, tzinfo=ASTANA_TZ) + datetime.timedelta(days=1)

    py, pm = add_months(sy, sm, -months)
    prev_start = _month_start(py, pm)
    # Сравниваем с тем же «отрезком» прошлого периода, а не с его итогом —
    # иначе в начале периода всё выглядело бы заниженным.
    prev_cutoff = min(prev_start + (end - start), start)

    cur_income = cur_expense = prev_income = prev_expense = 0.0
    by_cat: dict[str, float] = {}
    expenses: list[dict] = []
    cur_count = 0

    for t in transactions:
        if person and _user_of(t) != person:
            continue
        dt = parse_flexible_datetime(t.get("date"))
        if dt is None:
            continue
        amount = parse_amount(t.get("amt", 0))
        income = is_income_type(t.get("type"))
        if start <= dt < end:
            cur_count += 1
            if income:
                cur_income += amount
            else:
                cur_expense += amount
                cat = str(t.get("cat") or "").strip() or "Без категории"
                by_cat[cat] = by_cat.get(cat, 0.0) + amount
                expenses.append({
                    "date": dt.strftime("%d.%m.%Y"),
                    "amount": round(amount, 2),
                    "category": cat,
                    "text": _short_text(t),
                    "user": _user_of(t),
                    "_ts": dt.timestamp(),
                })
        elif prev_start <= dt < prev_cutoff:
            if income:
                prev_income += amount
            else:
                prev_expense += amount

    # Лимиты в таблице месячные и общие на семью: на период их умножаем на число
    # месяцев, а для отдельного человека не показываем (лимит не на него).
    categories = []
    for name, spent in by_cat.items():
        limit = float(limits.get(name) or 0) * months if not person else 0
        limit = limit or None
        categories.append({
            "name": name,
            "spent": round(spent, 2),
            "share_pct": round(spent / cur_expense * 100) if cur_expense > 0 else 0,
            "limit": limit,
            "limit_pct": round(spent / limit * 100) if limit else None,
            "status": limit_status(spent, limit),
        })
    categories.sort(key=lambda c: c["spent"], reverse=True)

    expenses.sort(key=lambda e: (e["amount"], e["_ts"]), reverse=True)
    top = [{k: v for k, v in e.items() if k != "_ts"} for e in expenses[:MAX_EXPENSE_ROWS]]

    if months == 1:
        prev_label = _MONTHS_RU[pm - 1]
        prev_phrase = f"в {_MONTHS_PREP[pm - 1]}"
    else:
        prev_label = f"предыдущие {months} мес."
        prev_phrase = f"в предыдущие {months} мес."

    return {
        "period": months,
        "who": person or "family",
        "month_label": _period_label(year, month, months),
        "day": now.day,
        "days_in_month": _days_in_month(year, month),
        "updated_at": now.strftime("%H:%M"),
        "operations": cur_count,
        "income": round(cur_income, 2),
        "expense": round(cur_expense, 2),
        "balance": round(cur_income - cur_expense, 2),
        "prev": {
            "label": prev_label,
            "phrase": prev_phrase,
            "expense": round(prev_expense, 2),
            "income": round(prev_income, 2),
            "expense_delta_pct": _delta_pct(cur_expense, prev_expense),
            "income_delta_pct": _delta_pct(cur_income, prev_income),
        },
        "categories": categories,
        "expenses": top,
        "expenses_total": len(expenses),
    }


def load_raw(now: datetime.datetime | None = None) -> dict:
    """Один запрос в Google Sheets: транзакции за 2 года и лимиты."""
    from services.sheets import get_category_limits, get_transactions_for_period

    now = now or now_astana()
    fy, fm = add_months(now.year, now.month, -FETCH_MONTHS_BACK)
    start = datetime.date(fy, fm, 1).strftime("%Y-%m-%d")
    # +1 день: период считается как [start, end), а сегодня должно попасть.
    end = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "transactions": get_transactions_for_period(start, end),
        "limits": get_category_limits(),
        "now": now,
    }


def load_dashboard(now: datetime.datetime | None = None, months: int = 1,
                   person: str | None = None) -> dict:
    """Читает Sheets и считает дашборд (синхронно, без кэша)."""
    raw = load_raw(now)
    return compute_dashboard(raw["transactions"], raw["limits"], raw["now"], months, person)


_cache: dict = {"at": 0.0, "ttl": 0, "raw": None}
_lock = threading.Lock()


def _get_raw(force: bool, loader, clock) -> dict:
    with _lock:
        age = clock() - _cache["at"]
        if _cache["raw"] is not None:
            fresh = age < _cache["ttl"]
            too_soon = age < MIN_REFRESH_INTERVAL_SECONDS
            if (fresh and not force) or too_soon:
                return _cache["raw"]
        raw = loader()
        _cache.update(
            at=clock(),
            raw=raw,
            ttl=CACHE_TTL_SECONDS if raw["transactions"] else EMPTY_CACHE_TTL_SECONDS,
        )
        return raw


def get_dashboard(force: bool = False, months: int = 1, person: str | None = None,
                  loader=load_raw, clock=time.monotonic) -> dict:
    """Дашборд с кэшем сырых данных на 1–2 минуты (лимиты Google Sheets)."""
    if months not in ALLOWED_PERIODS:
        months = 1
    raw = _get_raw(force, loader, clock)
    return compute_dashboard(raw["transactions"], raw["limits"], raw["now"], months, person)


def reset_cache() -> None:
    with _lock:
        _cache.update(at=0.0, ttl=0, raw=None)
