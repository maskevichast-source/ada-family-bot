"""Аналитический модуль: выявление «утечек бюджета» и анализ микропривычек семьи."""

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

