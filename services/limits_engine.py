"""Пересчёт месячных лимитов: считает код, а не ИИ.

Идея «смеси»: лимит строится из двух источников.
1) Собственные траты семьи за последние полные месяцы (ремонт и крупные
   платежи учитываются как есть).
2) Ориентир по структуре расходов из статистики (см. BENCHMARK_*), масштабированный
   под доход семьи. Он нужен там, где своих данных мало или нет вовсе
   (одежда, спорт, путешествия и т.п.), чтобы лимит не получался нулевым.

Формула для категории:
    own    — взвешенное среднее трат за месяц (новые месяцы весят больше)
    bench  — доля категории в ориентире × бюджет на траты
    если own >= bench:  base = own                         (лимит по факту)
    если own <  bench:  base = own + LIFT × (bench - own)    (подтяжка к ориентиру),
                        но не больше own × MAX_MULT
    если трат не было:  base = NO_HISTORY_LIFT × bench
    лимит = base × BUFFER, округление вверх до 1000, но не ниже MIN_LIMIT

Лимит НИКОГДА не ниже фактических трат: ориентир только подтягивает вверх
занижённые категории, но не урезает реальные обязательные платежи.
"""
import datetime
import math

from services.categories import EXPENSE_CATEGORIES, is_income_type
from services.money import parse_amount
from services.timezone import ASTANA_TZ, parse_flexible_datetime

# --- Параметры (все в одном месте, чтобы легко менять) ---------------------
SAVE_RATE = 0.20          # 20% дохода — на накопления/досрочные платежи (правило 50/30/20)
BUFFER = 1.10             # запас сверху
LIFT = 0.30               # какую часть разрыва «ориентир − факт» добавляем к лимиту
MAX_MULT = 2.0            # подтяжка не должна раздувать лимит больше чем вдвое к факту
NO_HISTORY_LIFT = 1.0     # если в категории трат не было — берём ориентир целиком
ROUND_TO = 1000
MIN_LIMIT = 5000          # ни один лимит не опускается ниже
MONTH_WEIGHTS = (3, 2, 1)     # для последнего, предпоследнего, третьего с конца месяца
MAX_HISTORY_MONTHS = 3
COMPLETE_MONTH_GRACE_DAYS = 5  # месяц «полный», если данные начались не позже 5-го числа
CHANGE_NOTICE_PCT = 5          # в сообщении показываем изменения от 5%

# --- Ориентир структуры расходов -------------------------------------------
# Источники: BLS Consumer Expenditure Survey 2024 (доли основных групп, США:
# жильё 33,4%, транспорт 17,0%, еда 12,9% [дома 7,9 + вне дома 5,0], здоровье 7,9%,
# развлечения 4,6%, одежда 2,5%, образование 2,0%, уход 1,2%, алкоголь 0,8% +
# табак 0,4%, пожертвования 2,9%, прочее 1,6%); правило 50/30/20 (Уоррен и
# Тьяги, 2005) — 20% дохода на накопления и долги. Разбивка групп BLS
# «жильё» и «развлечения» по нашим категориям, а также «Финансовые расходы» —
# экспертная оценка, не данные BLS.
_NON_FOOD_RAW = {
    "Жильё и коммунальные услуги": 24.0,
    "Дом и быт": 4.0,
    "Электроника и техника": 2.0,
    "Связь и подписки": 3.4,
    "Транспорт и авто": 17.0,
    "Здоровье и медицина": 7.9,
    "Развлечения и хобби": 2.8,
    "Спорт и фитнес": 1.0,
    "Питомцы": 1.0,
    "Путешествия": 2.0,
    "Одежда и обувь": 2.5,
    "Образование": 2.0,
    "Красота и уход": 1.2,
    "Алкоголь, табак и энергетики": 1.2,
    "Подарки, праздники и благотворительность": 2.9,
    "Финансовые расходы и переводы": 4.0,
    "Обязательные платежи и прочее": 1.6,
}
_FOOD_HOME_RAW = 7.9
_FOOD_AWAY_RAW = 5.0
# В США еда — 12,9% расходов, в Казахстане у среднего городского домохозяйства
# по данным БНС РК (III кв. 2025) — около 56,5%. Семья с доходом выше среднего
# лежит между ними; собственные траты семьи (~50% без ремонта) — тоже. Это допущение.
FOOD_SHARE = 0.40


def benchmark_shares() -> dict[str, float]:
    """Доли категорий в ориентире (сумма = 1)."""
    non_food_total = sum(_NON_FOOD_RAW.values())
    shares = {c: raw / non_food_total * (1 - FOOD_SHARE) for c, raw in _NON_FOOD_RAW.items()}
    food_total = _FOOD_HOME_RAW + _FOOD_AWAY_RAW
    shares["Еда и продукты"] = FOOD_SHARE * _FOOD_HOME_RAW / food_total
    shares["Кафе, рестораны и доставка еды"] = FOOD_SHARE * _FOOD_AWAY_RAW / food_total
    return shares


def _shift(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _round_up(value: float) -> int:
    return int(math.ceil(value / ROUND_TO - 1e-9) * ROUND_TO)


def _select_months(data_start: datetime.datetime | None, now: datetime.datetime):
    """Список (год, месяц, коэффициент_масштаба), новые первыми.

    Берём последние полные месяцы. Если полных нет (данные только начались) —
    берём текущий месяц, растянутый на весь месяц по числу прошедших дней.
    """
    months = []
    for back in range(1, MAX_HISTORY_MONTHS + 3):
        y, m = _shift(now.year, now.month, -back)
        start = datetime.datetime(y, m, 1, tzinfo=ASTANA_TZ)
        if data_start is not None and data_start <= start + datetime.timedelta(days=COMPLETE_MONTH_GRACE_DAYS):
            months.append((y, m, 1.0))
        if len(months) == MAX_HISTORY_MONTHS:
            break
    if months:
        return months, "complete"
    days_in = (datetime.date(*_shift(now.year, now.month, 1), 1) - datetime.date(now.year, now.month, 1)).days
    elapsed = max(now.day, 1)
    return [(now.year, now.month, days_in / elapsed)], "partial"


def compute_limits(transactions: list[dict], now: datetime.datetime,
                   current_limits: dict | None = None, pinned: set | None = None) -> dict:
    """Чистая функция. Ничего не читает и не пишет.

    transactions — как из get_transactions_for_period (ключи date, type, amt, cat).
    Возвращает словарь: limits (все категории), details, months, mode, income_avg,
    spendable, pinned.
    """
    current_limits = current_limits or {}
    pinned = set(pinned or ())

    dates = [parse_flexible_datetime(t.get("date")) for t in transactions]
    known = [d for d in dates if d is not None]
    data_start = min(known) if known else None

    months, mode = _select_months(data_start, now)
    weights = list(MONTH_WEIGHTS[: len(months)])
    weight_sum = sum(weights) or 1

    spend = {(y, m): {} for y, m, _ in months}
    income = {(y, m): 0.0 for y, m, _ in months}
    for t, dt in zip(transactions, dates):
        if dt is None or (dt.year, dt.month) not in spend:
            continue
        amount = parse_amount(t.get("amt", 0))
        if is_income_type(t.get("type")):
            income[(dt.year, dt.month)] += amount
        else:
            cat = str(t.get("cat") or "").strip()
            spend[(dt.year, dt.month)][cat] = spend[(dt.year, dt.month)].get(cat, 0.0) + amount

    def weighted(values_by_month) -> float:
        return sum(w * scale * values_by_month(y, m)
                   for w, (y, m, scale) in zip(weights, months)) / weight_sum

    own = {c: weighted(lambda y, m, c=c: spend[(y, m)].get(c, 0.0)) for c in EXPENSE_CATEGORIES}
    income_avg = weighted(lambda y, m: income[(y, m)])

    own_total = sum(own.values())
    spendable = income_avg * (1 - SAVE_RATE) if income_avg > 0 else own_total
    shares = benchmark_shares()

    limits, details = {}, {}
    for cat in EXPENSE_CATEGORIES:
        bench = shares[cat] * spendable
        o = own[cat]
        if o <= 0:
            base = NO_HISTORY_LIFT * bench
        elif o >= bench:
            base = o
        else:
            base = min(o + LIFT * (bench - o), o * MAX_MULT)
        calc = max(_round_up(base * BUFFER), MIN_LIMIT)
        is_pinned = cat in pinned and cat in current_limits
        limits[cat] = int(current_limits[cat]) if is_pinned else calc
        details[cat] = {"own": round(o), "bench": round(bench), "calc": calc, "pinned": is_pinned}

    return {
        "limits": limits,
        "details": details,
        "months": [(y, m) for y, m, _ in months],
        "mode": mode,
        "income_avg": round(income_avg),
        "spendable": round(spendable),
        "pinned": sorted(c for c, d in details.items() if d["pinned"]),
    }


_MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
              "сентябрь", "октябрь", "ноябрь", "декабрь"]


def _money(value: float) -> str:
    return f"{int(round(value)):,}".replace(",", " ")


def format_summary(result: dict, old_limits: dict, now: datetime.datetime, preview: bool = False) -> str:
    """Текст для семейного чата: было → стало, только заметные изменения."""
    new = result["limits"]
    title = "Так Ада пересчитала бы лимиты" if preview else "Ада пересчитала лимиты"
    lines = [f"📊 {title} на {_MONTHS_RU[now.month - 1]} {now.year}"]
    if result["mode"] == "partial":
        lines.append("(полных месяцев ещё нет — беру текущий, растянув на весь месяц)")
    else:
        used = ", ".join(_MONTHS_RU[m - 1] for _, m in result["months"])
        lines.append(f"(по данным: {used})")

    rows = []
    for cat in sorted(new, key=lambda c: new[c], reverse=True):
        old = old_limits.get(cat)
        pin = " 📌" if result["details"][cat]["pinned"] else ""
        if old and old > 0 and abs(new[cat] - old) / old * 100 < CHANGE_NOTICE_PCT:
            continue
        before = _money(old) if old else "—"
        rows.append(f"• {cat}: {before} → {_money(new[cat])}{pin}")
    lines.extend(rows or ["Существенных изменений нет."])

    total = sum(new.values())
    lines.append("")
    lines.append(f"Сумма лимитов: {_money(total)} ₸. Средний доход за месяц: {_money(result['income_avg'])} ₸.")
    if result["pinned"]:
        lines.append(f"📌 Закреплены и не менялись: {len(result['pinned'])}.")
    lines.append("Закрепить лимит: поставь «да» в колонке pinned листа Limits. Откатить: /limits_undo")
    return "\n".join(lines)
