"""Данные для дашборда мини-аппа (только чтение).

Цифры считает код, тем же способом, что /report и предупреждение о лимите:
берутся `amt` и тип операции из get_transactions_for_period, лимиты — из листа
Limits. Сырые данные за 2 года читаются ОДНИМ запросом в Google Sheets и
кэшируются; переключение периода и человека считается уже в памяти.
"""
import datetime
import re
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
ALLOWED_DAYS = (7,)               # скользящие окна в календарных днях: сегодня и предыдущие
MAX_EXPENSE_ROWS = 200            # сколько крупнейших трат отдаём в страницу
FETCH_MONTHS_BACK = 23            # хватает для «год» и сравнения с предыдущим годом

FAMILY = ("Влад", "Диана")

MAX_MERCHANTS = 15
MAX_QUERY_CHARS = 60

_MONTHS_RU = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]
_MONTHS_PREP = [  # «в …»
    "январе", "феврале", "марте", "апреле", "мае", "июне",
    "июле", "августе", "сентябре", "октябре", "ноябре", "декабре",
]
_WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
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


NOTE_MAX_CHARS = 160


def _title_and_note(t: dict) -> tuple[str, str]:
    """Заголовок строки траты (магазин или получатель, иначе подкатегория) и
    заметка — комментарий, который семья написала к трате (он поясняет, что это было).
    Комментарий ИИ не берём: он короткий, разговорный и ничего не поясняет."""
    title = str(t.get("merchant") or "").strip() or str(t.get("subcat") or "").strip()
    note = " ".join(str(t.get("comm") or "").split())
    if note.lower() == title.lower():
        note = ""
    if len(note) > NOTE_MAX_CHARS:
        note = note[: NOTE_MAX_CHARS - 1].rstrip() + "…"
    return title[:80], note


def _cumulative(by_day: dict[int, float], length: int) -> list[float]:
    total, out = 0.0, []
    for day in range(1, length + 1):
        total += by_day.get(day, 0.0)
        out.append(round(total, 2))
    return out


def _build_dynamics(months, now, sy, sm, py, pm, day_cur, day_prev, by_month) -> dict:
    """Данные для графиков.

    Вид «месяц»: накопленные расходы по дням (до сегодня) и пунктир прошлого
    месяца целиком; столбики по неделям (1–7, 8–14, …).
    Виды «3 мес.» и дальше: столбики по месяцам периода.
    """
    if months == 1:
        dim = _days_in_month(now.year, now.month)
        weeks = []
        for w in range((now.day - 1) // 7 + 1):
            first, last = w * 7 + 1, min(w * 7 + 7, dim)
            value = sum(v for d, v in day_cur.items() if first <= d <= last)
            weeks.append({"label": f"{first}–{last}", "value": round(value, 2)})
        return {
            "kind": "daily",
            "days_in_month": dim,
            "cur": _cumulative(day_cur, now.day),
            "prev": _cumulative(day_prev, _days_in_month(py, pm)),
            "prev_label": _MONTHS_RU[pm - 1],
            "bars": weeks,
        }
    bars = []
    for i in range(months):
        y, m = add_months(sy, sm, i)
        label = _MONTHS_SHORT[m - 1] + (f" {str(y)[2:]}" if m == 1 or i == 0 else "")
        bars.append({"label": label, "value": round(by_month.get((y, m), 0.0), 2)})
    return {"kind": "monthly", "bars": bars}


def _bank_label(t: dict) -> str:
    """Откуда потрачено: наличные, иначе карта/источник, иначе банк."""
    if str(t.get("resource") or "").strip().lower() == "наличные":
        return "Наличные"
    return str(t.get("source") or "").strip() or str(t.get("bank") or "").strip() or "Не указан"


def _norm_key(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _matches(query: str, title: str, note: str, cat: str, subcat: str, amount: float) -> bool:
    """Поиск: по тексту (название, заметка, категория) и по сумме («480» найдёт 480 000)."""
    q = query.lower()
    if any(q in field.lower() for field in (title, note, cat, subcat)):
        return True
    digits = re.sub(r"\D", "", q)
    return bool(digits) and digits in str(int(round(amount)))


def _share_rows(totals: dict, counts: dict, total: float, limit: int | None = None, names: dict | None = None) -> list[dict]:
    rows = [{"name": (names or {}).get(k, k), "spent": round(v, 2), "count": counts.get(k, 0),
             "share_pct": round(v / total * 100) if total > 0 else 0}
            for k, v in totals.items()]
    rows.sort(key=lambda r: r["spent"], reverse=True)
    return rows[:limit] if limit else rows


def compute_dashboard(transactions: list[dict], limits: dict, now: datetime.datetime,
                      months: int = 1, person: str | None = None,
                      category: str | None = None, query: str | None = None,
                      days: int | None = None) -> dict:
    """Чистая функция: ничего не читает из сети, удобна для тестов.

    months — сколько календарных месяцев включая текущий (1/3/6/12).
    person — имя (Влад/Диана) или None для всей семьи.
    category — если задана, считаем только расходы этой категории (детализация).
    query — поиск по списку трат (итоги периода от него не зависят).
    """
    query = " ".join(str(query or "").split())[:MAX_QUERY_CHARS]
    category = (category or "").strip() or None
    year, month = now.year, now.month
    sy, sm = add_months(year, month, -(months - 1))
    start = _month_start(sy, sm)
    end = datetime.datetime(now.year, now.month, now.day, tzinfo=ASTANA_TZ) + datetime.timedelta(days=1)

    py, pm = add_months(sy, sm, -months)
    prev_start = _month_start(py, pm)
    # Сравниваем с тем же «отрезком» прошлого периода, а не с его итогом —
    # иначе в начале периода всё выглядело бы заниженным.
    prev_cutoff = min(prev_start + (end - start), start)

    # Скользящее окно «последние N календарных дней» (кнопка «7 дн.»): сегодня и N-1 предыдущих;
    # сравнение — с предыдущими N днями целиком. Месячные лимиты в этом режиме не показываем.
    days = days if days in ALLOWED_DAYS else None
    today0 = datetime.datetime(now.year, now.month, now.day, tzinfo=ASTANA_TZ)
    if days:
        months = 1
        start = today0 - datetime.timedelta(days=days - 1)
        prev_start = start - datetime.timedelta(days=days)
        prev_cutoff = start

    cur_income = cur_expense = prev_income = prev_expense = 0.0
    by_cat: dict[str, float] = {}
    expenses: list[dict] = []
    incomes: list[dict] = []
    cur_count = 0
    # для графиков: расходы по дням (только вид «месяц») и по месяцам периода
    day_cur: dict[int, float] = {}
    day_prev: dict[int, float] = {}
    by_month: dict[tuple[int, int], float] = {}
    # разрезы текущего периода
    by_bank: dict[str, float] = {}; bank_n: dict[str, int] = {}
    by_merchant: dict[str, float] = {}; merchant_n: dict[str, int] = {}; merchant_names: dict[str, dict] = {}
    by_subcat: dict[str, float] = {}; subcat_n: dict[str, int] = {}
    people_exp = {n: 0.0 for n in FAMILY}; people_inc = {n: 0.0 for n in FAMILY}
    people_cat: dict[str, dict[str, float]] = {n: {} for n in FAMILY}
    matched_count = 0; matched_sum = 0.0
    # последние 7 календарных дней (сегодня и 6 предыдущих) и предыдущие 7 — для сравнения
    today0 = datetime.datetime(now.year, now.month, now.day, tzinfo=ASTANA_TZ)
    week_start = today0 - datetime.timedelta(days=6)
    week_prev_start = week_start - datetime.timedelta(days=7)
    week_end = today0 + datetime.timedelta(days=1)
    week_days: dict[datetime.date, float] = {}
    week_prev_total = 0.0

    for t in transactions:
        if person and _user_of(t) != person:
            continue
        dt = parse_flexible_datetime(t.get("date"))
        if dt is None:
            continue
        amount = parse_amount(t.get("amt", 0))
        income = is_income_type(t.get("type"))
        cat = str(t.get("cat") or "").strip() or "Без категории"
        if category and (income or cat != category):
            continue          # детализация категории: только расходы этой категории
        if not income:
            if week_start <= dt < week_end:
                week_days[dt.date()] = week_days.get(dt.date(), 0.0) + amount
            elif week_prev_start <= dt < week_start:
                week_prev_total += amount
        if months == 1 and not days and not income and prev_start <= dt < start:
            day_prev[dt.day] = day_prev.get(dt.day, 0.0) + amount   # весь прошлый месяц, для пунктира
        if start <= dt < end:
            cur_count += 1
            user_name = _user_of(t)
            if income:
                cur_income += amount
                i_title, i_note = _title_and_note(t)
                incomes.append({
                    "date": dt.strftime("%d.%m.%Y"),
                    "amount": round(amount, 2),
                    "category": cat,
                    "text": i_title,
                    "note": i_note,
                    "user": user_name,
                    "_ts": dt.timestamp(),
                })
                if user_name in people_inc:
                    people_inc[user_name] += amount
            else:
                cur_expense += amount
                by_month[(dt.year, dt.month)] = by_month.get((dt.year, dt.month), 0.0) + amount
                if months == 1:
                    day_cur[dt.day] = day_cur.get(dt.day, 0.0) + amount
                by_cat[cat] = by_cat.get(cat, 0.0) + amount
                title, note = _title_and_note(t)
                sub = str(t.get("subcat") or "").strip()
                bank = _bank_label(t)
                by_bank[bank] = by_bank.get(bank, 0.0) + amount
                bank_n[bank] = bank_n.get(bank, 0) + 1
                by_subcat[sub or "Без подкатегории"] = by_subcat.get(sub or "Без подкатегории", 0.0) + amount
                subcat_n[sub or "Без подкатегории"] = subcat_n.get(sub or "Без подкатегории", 0) + 1
                mkey = _norm_key(t.get("merchant"))
                if mkey:
                    by_merchant[mkey] = by_merchant.get(mkey, 0.0) + amount
                    merchant_n[mkey] = merchant_n.get(mkey, 0) + 1
                    spell = merchant_names.setdefault(mkey, {})
                    original = str(t.get("merchant")).strip()
                    spell[original] = spell.get(original, 0) + 1
                if user_name in people_exp:
                    people_exp[user_name] += amount
                    people_cat[user_name][cat] = people_cat[user_name].get(cat, 0.0) + amount
                if query and not _matches(query, title, note, cat, sub, amount):
                    continue
                matched_count += 1
                matched_sum += amount
                expenses.append({
                    "date": dt.strftime("%d.%m.%Y"),
                    "amount": round(amount, 2),
                    "category": cat,
                    "subcat": sub,
                    "text": title,
                    "note": note,
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
        limit = float(limits.get(name) or 0) * months if not (person or days) else 0
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
    incomes.sort(key=lambda e: e["_ts"], reverse=True)
    income_rows = [{k: v for k, v in e.items() if k != "_ts"} for e in incomes]

    dynamics = _build_dynamics(months, now, sy, sm, py, pm, day_cur, day_prev, by_month)

    merchant_display = {k: max(v, key=v.get) for k, v in merchant_names.items()}
    banks = _share_rows(by_bank, bank_n, cur_expense)
    merchants = _share_rows(by_merchant, merchant_n, cur_expense, MAX_MERCHANTS, merchant_display)
    subcats = _share_rows(by_subcat, subcat_n, cur_expense) if category else []
    people = []
    if not person:
        for name in FAMILY:
            top_cats = sorted(people_cat[name].items(), key=lambda kv: kv[1], reverse=True)[:3]
            people.append({
                "name": name,
                "expense": round(people_exp[name], 2),
                "income": round(people_inc[name], 2),
                "share_pct": round(people_exp[name] / cur_expense * 100) if cur_expense > 0 else 0,
                "top": [{"category": c, "spent": round(v, 2)} for c, v in top_cats],
            })
    week_list = []
    for i in range(7):
        day = (week_start + datetime.timedelta(days=i)).date()
        week_list.append({"label": f"{_WEEKDAYS_SHORT[day.weekday()]} {day.day}", "date": day.strftime("%d.%m"),
                          "value": round(week_days.get(day, 0.0), 2)})
    week_total = round(sum(d["value"] for d in week_list), 2)
    week = {"days": week_list, "total": week_total, "avg": round(week_total / 7, 2),
            "prev_total": round(week_prev_total, 2), "delta_pct": _delta_pct(week_total, week_prev_total)}
    if days:
        dynamics = {"kind": "days", "bars": week_list}
    category_limit = None
    if category and not (person or days):
        lim = float(limits.get(category) or 0) * months or None
        spent = by_cat.get(category, 0.0)
        category_limit = {"limit": lim, "pct": round(spent / lim * 100) if lim else None,
                          "status": limit_status(spent, lim)} if lim else None

    if days:
        prev_label = f"предыдущие {days} дней"
        prev_phrase = f"в предыдущие {days} дней"
    elif months == 1:
        prev_label = _MONTHS_RU[pm - 1]
        prev_phrase = f"в {_MONTHS_PREP[pm - 1]}"
    else:
        prev_label = f"предыдущие {months} мес."
        prev_phrase = f"в предыдущие {months} мес."

    return {
        "period": months,
        "who": person or "family",
        "month_label": (f"{days} дней · {start.strftime('%d.%m')} – {today0.strftime('%d.%m')}"
                        if days else _period_label(year, month, months)),
        "period_days": days,
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
        "dynamics": dynamics,
        "week": week,
        "expenses": top,
        "incomes": income_rows,
        "expenses_total": len(expenses),
        "banks": banks,
        "merchants": merchants,
        "subcats": subcats,
        "people": people,
        "category": category,
        "category_limit": category_limit,
        "search": {"q": query, "count": matched_count, "sum": round(matched_sum, 2)} if query else None,
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
                  category: str | None = None, query: str | None = None, days: int | None = None,
                  loader=load_raw, clock=time.monotonic) -> dict:
    """Дашборд с кэшем сырых данных на 1–2 минуты (лимиты Google Sheets)."""
    if months not in ALLOWED_PERIODS:
        months = 1
    raw = _get_raw(force, loader, clock)
    data = compute_dashboard(raw["transactions"], raw["limits"], raw["now"], months, person, category, query, days)
    if not category:
        # «Внимание» всегда про текущий месяц и всю семью, независимо от выбранного вида
        from services.dashboard_alerts import compute_alerts
        data["alerts"] = compute_alerts(raw["transactions"], raw["limits"], raw["now"])
    return data


def get_raw(force: bool = False, loader=load_raw, clock=time.monotonic) -> dict:
    """Сырые данные (кэшированные) для других разделов мини-аппа."""
    return _get_raw(force, loader, clock)


def reset_cache() -> None:
    with _lock:
        _cache.update(at=0.0, ttl=0, raw=None)
