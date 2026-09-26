"""Аналитический модуль: выявление «утечек бюджета» и анализ микропривычек семьи."""

import calendar
import datetime
from collections import defaultdict
from services.sheets import get_transactions_for_period
from services.categories import TYPE_EXPENSE, TYPE_INCOME
from services.timezone import now_astana
from services.money import parse_amount


def _format_currency(value):
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (ValueError, TypeError):
        return str(value)


def detect_amount_anomaly(
    user: str, category: str, amount: float,
    lookback_days: int = 90, min_history: int = 3, threshold_ratio: float = 1.5,
) -> str | None:
    """Если новая трата заметно (в threshold_ratio раз) больше предыдущего
    максимума в этой же категории у этого же человека за lookback_days дней —
    возвращает короткое текстовое описание факта для generate_budget_reflection
    (kind="anomaly"). Иначе None — молчим, не каждая крупная покупка обязана
    получать отдельный комментарий.

    min_history нужен, чтобы не поднимать шум на первой же покупке в новой
    категории — сравнивать почти не с чем, значит и "аномалии" никакой нет."""
    from services.sheets import get_last_200_transactions
    if not user or not category or not amount or amount <= 0:
        return None
    cutoff = datetime.datetime.now() - datetime.timedelta(days=lookback_days)
    amounts = []
    for t in get_last_200_transactions():
        if t.get("cat") != category or str(t.get("user") or "").strip() != user:
            continue
        if t.get("type") != TYPE_EXPENSE:
            continue
        date_str = str(t.get("date") or "")[:19]
        try:
            tx_dt = datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if tx_dt < cutoff:
            continue
        amt = t.get("amt")
        if isinstance(amt, (int, float)) and amt > 0:
            amounts.append(amt)
    if len(amounts) < min_history:
        return None
    prev_max = max(amounts)
    if amount <= prev_max * threshold_ratio:
        return None
    return (
        f"Новая трата у {user} в категории «{category}»: {_format_currency(amount)} тг — "
        f"заметно больше предыдущего максимума в этой категории за последние "
        f"{lookback_days} дней ({_format_currency(prev_max)} тг, всего {len(amounts)} "
        f"покупок в истории за этот период)."
    )


def detect_category_pace_anomalies(
    lookback_months: int = 3, min_history_months: int = 2,
    ratio_threshold: float = 1.7, min_amount: float = 5000,
) -> str | None:
    """Раз в неделю (вызывается из finance_report_scheduler) сравнивает темп
    трат с начала текущего месяца по сегодня в категориях, у которых НЕТ
    заданного лимита (их не видит check_limit_warnings), с обычным темпом
    той же категории за тот же диапазон чисел месяца в lookback_months
    прошлых месяцев. Возвращает готовый текст-факт для
    generate_budget_reflection(kind="category_pace"), либо None, если
    подходящих категорий не нашлось.

    Сознательно сравнивает "число месяца к числу месяца" (1..today.day), а
    не итог прошлых месяцев целиком — иначе в начале месяца любая категория
    выглядела бы заниженной просто потому, что месяц ещё не закончился."""
    from services.sheets import get_category_limits

    now = now_astana()
    today_day = now.day
    limited_categories = set(get_category_limits().keys())

    cur_start = now.date().replace(day=1).strftime("%Y-%m-%d")
    cur_end = (now.date() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    cur_by_cat: dict[str, float] = defaultdict(float)
    for t in get_transactions_for_period(cur_start, cur_end):
        if t["type"] != TYPE_EXPENSE:
            continue
        cur_by_cat[t["cat"]] += t["amt"] or 0

    # Категорию без единой траты в этом месяце сравнивать не с чем и незачем.
    if not cur_by_cat:
        return None

    history_by_cat: dict[str, list[float]] = defaultdict(list)
    y, m = now.year, now.month
    for _ in range(lookback_months):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        last_day_of_month = calendar.monthrange(y, m)[1]
        day_cap = min(today_day, last_day_of_month)
        month_start = datetime.date(y, m, 1).strftime("%Y-%m-%d")
        month_end = (datetime.date(y, m, day_cap) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        month_by_cat: dict[str, float] = defaultdict(float)
        for t in get_transactions_for_period(month_start, month_end):
            if t["type"] != TYPE_EXPENSE:
                continue
            month_by_cat[t["cat"]] += t["amt"] or 0
        # Категория без трат в конкретном прошлом месяце намеренно НЕ
        # получает запись 0 — иначе "обычно 0, а сейчас много" смазывало бы
        # сравнение с "истории просто пока не было".
        for cat, amt in month_by_cat.items():
            history_by_cat[cat].append(amt)

    findings = []
    for cat, cur_amount in sorted(cur_by_cat.items(), key=lambda kv: -kv[1]):
        if cat in limited_categories or cur_amount < min_amount:
            continue
        history = history_by_cat.get(cat, [])
        if len(history) < min_history_months:
            continue
        avg_prior = sum(history) / len(history)
        if avg_prior <= 0 or cur_amount < avg_prior * ratio_threshold:
            continue
        findings.append(
            f"«{cat}»: с начала месяца по {today_day}-е число потрачено "
            f"{_format_currency(cur_amount)} тг, обычно к этому же числу — около "
            f"{_format_currency(avg_prior)} тг (среднее за {len(history)} прошл. мес.)."
        )

    if not findings:
        return None
    return (
        "Категории БЕЗ заданного лимита (их не проверяет обычное предупреждение о лимите), "
        "где темп трат в этом месяце заметно выше обычного:\n" + "\n".join(findings)
    )


def analyze_budget_leaks(year: int = None, month: int = None) -> dict:
    """Анализирует скрытые утечки бюджета за указанный месяц."""
    now = now_astana()
    y = year or now.year
    m = month or now.month

    start_date = datetime.date(y, m, 1).strftime("%Y-%m-%d")
    if m == 12:
        end_date = datetime.date(y + 1, 1, 1).strftime("%Y-%m-%d")
    else:
        end_date = datetime.date(y, m + 1, 1).strftime("%Y-%m-%d")

    transactions = get_transactions_for_period(start_date, end_date)
    expenses = [t for t in transactions if str(t.get("type", "")).strip() != TYPE_INCOME]

    total_expense = sum(parse_amount(t.get("amt", 0)) for t in expenses)

    # Категории утечек
    leaks = {
        "tobacco": {"name": "🚬 Сигареты и стики IQOS", "sum": 0.0, "count": 0, "items": []},
        "drinks": {"name": "⚡ Энергетики и кофе на вынос", "sum": 0.0, "count": 0, "items": []},
        "snacks": {"name": "🍔 Фастфуд, самса и перекусы", "sum": 0.0, "count": 0, "items": []},
        "taxi": {"name": "🚕 Такси (короткие поездки)", "sum": 0.0, "count": 0, "items": []},
        "misc_wants": {"name": "🛍 Прочие мелкие «хотелки» (до 2000 тг)", "sum": 0.0, "count": 0, "items": []},
    }

    for t in expenses:
        amt = parse_amount(t.get("amt", 0))
        comm = str(t.get("comm", "")).lower()
        subcat = str(t.get("subcat", "")).lower()
        cat = str(t.get("cat", "")).lower()
        nec = str(t.get("nec", "Want")).lower()

        # 1. Сигареты и стики
        if any(k in comm for k in ["сигарет", "стик", "iqos", "пачка"]) or "сигареты" in subcat:
            leaks["tobacco"]["sum"] += amt
            leaks["tobacco"]["count"] += 1
            leaks["tobacco"]["items"].append(f"{amt} тг ({t.get('comm')})")

        # 2. Энергетики и кофе
        elif any(k in comm for k in ["энергетик", "кофе", "gorilla", "red bull", "flash", "зебра", "zebra"]) or "энергетики" in subcat or "кофе" in subcat:
            leaks["drinks"]["sum"] += amt
            leaks["drinks"]["count"] += 1
            leaks["drinks"]["items"].append(f"{amt} тг ({t.get('comm')})")

        # 3. Фастфуд, самса, выпечка, кола к чаю
        elif any(k in comm for k in ["самса", "сендвич", "сэндвич", "донер", "шаурма", "сосиска в тесте", "беляш", "пирожн", "вафли", "кола"]) or "перекус" in subcat or "снеки" in subcat:
            leaks["snacks"]["sum"] += amt
            leaks["snacks"]["count"] += 1
            leaks["snacks"]["items"].append(f"{amt} тг ({t.get('comm')})")

        # 4. Такси
        elif "такси" in subcat or "такси" in comm or "yandex.go" in str(t.get("merchant", "")).lower():
            leaks["taxi"]["sum"] += amt
            leaks["taxi"]["count"] += 1
            leaks["taxi"]["items"].append(f"{amt} тг ({t.get('comm')})")

        # 5. Прочие мелкие Want-траты до 2000 тг
        elif amt <= 2000 and "want" in nec:
            leaks["misc_wants"]["sum"] += amt
            leaks["misc_wants"]["count"] += 1

    total_leaks = sum(g["sum"] for g in leaks.values())
    pct = int((total_leaks / total_expense * 100)) if total_expense > 0 else 0

    month_name = datetime.date(y, m, 1).strftime("%B %Y")

    lines = [
        f"🔍 **Анализ «утечек бюджета» за {month_name}:**\n",
        f"Суммарно на мелкие ежедневные слабости ушло: **{_format_currency(total_leaks)} KZT** ({pct}% от всех трат месяца)!\n",
    ]

    for g in leaks.values():
        if g["sum"] > 0:
            lines.append(f"• **{g['name']}**: {_format_currency(g['sum'])} KZT _({g['count']} покупок)_")

    if total_leaks > 0:
        lines.append(
            f"\n💡 _Для сравнения: {_format_currency(total_leaks)} KZT — это почти треть бюджета на отпуск или несколько крупных покупок для дома. Стоит держать руку на пульсе!_"
        )
    else:
        lines.append("\n✅ В этом месяце мелких импульсивных трат пока не зафиксировано. Чистая финансовая дисциплина!")

    return {
        "text": "\n".join(lines),
        "total_leaks": total_leaks,
        "percent": pct,
        "details": leaks,
    }

