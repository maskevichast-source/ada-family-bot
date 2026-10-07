"""Прогноз расходов до конца месяца, сравнение категорий между периодами.

Постоянные платежи (жильё, связь и подписки, кредиты) в прогноз идут как есть: они обычно списываются в начале
месяца, и растягивать их по дням значит завышать итог. Остальные траты растягиваем по среднему дневному темпу."""
import calendar
import datetime

from services.limits_engine import ONE_OFF_EXEMPT_CATEGORIES

MIN_DAY_FOR_FORECAST = 5
MIN_DAY_FOR_WARNING = 7
WARN_RATIO = 1.15                 # прогноз выше лимита на 15% и больше
CHANGE_MIN_TENGE = 3000
CHANGE_MIN_PERCENT = 15


def _money(value) -> str:
    return f"{float(value):,.0f}".replace(",", " ")


def month_forecast(now: datetime.datetime, month_txs: list[dict]) -> dict:
    """month_txs — операции с 1-го числа по сегодня включительно (формат get_transactions_for_period)."""
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    day = max(1, min(now.day, days_in_month))
    spent, income = {}, 0.0
    for t in month_txs:
        amount = float(t.get("amt") or 0)
        if str(t.get("type")) == "ДОХОД":
            income += amount
        else:
            cat = str(t.get("cat") or "Прочее")
            spent[cat] = spent.get(cat, 0.0) + amount
    projected = {cat: (value if cat in ONE_OFF_EXEMPT_CATEGORIES else value / day * days_in_month)
                 for cat, value in spent.items()}
    return {"day": day, "days_in_month": days_in_month, "spent": spent, "projected": projected,
            "spent_total": sum(spent.values()), "projected_total": sum(projected.values()), "income": income}


def forecast_line(f: dict) -> str | None:
    if f["day"] < MIN_DAY_FOR_FORECAST or f["spent_total"] <= 0:
        return None
    line = (f"🔮 Прогноз на конец месяца: расходы около {_money(f['projected_total'])} тг "
            f"(сейчас {_money(f['spent_total'])} тг, {f['day']}-й день из {f['days_in_month']}).")
    if f["income"] > 0:
        line += f" Доходы пока {_money(f['income'])} тг."
    return line


def risky_categories(f: dict, limits: dict) -> list[dict]:
    """Категории, где факт ещё в пределах лимита, а при текущем темпе месяц закончится выше него."""
    if f["day"] < MIN_DAY_FOR_WARNING:
        return []
    out = []
    for cat, limit in limits.items():
        limit = float(limit or 0)
        if limit <= 0 or cat in ONE_OFF_EXEMPT_CATEGORIES:
            continue
        spent = f["spent"].get(cat, 0.0)
        projected = f["projected"].get(cat, 0.0)
        if 0 < spent < limit and projected >= limit * WARN_RATIO:
            out.append({"cat": cat, "spent": spent, "limit": limit, "projected": projected})
    return sorted(out, key=lambda r: -(r["projected"] - r["limit"]))


def warning_text(item: dict, f: dict) -> str:
    return (f"📈 Категория «{item['cat']}»: на {f['day']}-й день потрачено {_money(item['spent'])} из "
            f"{_money(item['limit'])} тг. В таком темпе к концу месяца выйдет около {_money(item['projected'])} тг, "
            f"то есть выше лимита на {_money(item['projected'] - item['limit'])} тг. Пока можно притормозить.")


def by_category(txs: list[dict]) -> dict:
    out = {}
    for t in txs:
        if str(t.get("type")) == "ДОХОД":
            continue
        cat = str(t.get("cat") or "Прочее")
        out[cat] = out.get(cat, 0.0) + float(t.get("amt") or 0)
    return out


def category_changes(cur: dict, prev: dict, top: int = 3) -> str | None:
    """«Выросли: … Снизились: …» между двумя периодами. Мелкие колебания (меньше 3000 тг или 15%) не показываем."""
    rows = []
    for cat in set(cur) | set(prev):
        a, b = cur.get(cat, 0.0), prev.get(cat, 0.0)
        diff = a - b
        if abs(diff) < CHANGE_MIN_TENGE:
            continue
        pct = (diff / b * 100) if b > 0 else None
        if pct is not None and abs(pct) < CHANGE_MIN_PERCENT:
            continue
        rows.append((cat, diff, pct))

    def fmt(row):
        cat, diff, pct = row
        sign = "+" if diff > 0 else "−"
        tail = f", {sign}{abs(pct):.0f}%" if pct is not None else ", раньше не было"
        return f"- {cat}: {sign}{_money(abs(diff))} тг{tail}"
    ups = sorted([r for r in rows if r[1] > 0], key=lambda r: -r[1])[:top]
    downs = sorted([r for r in rows if r[1] < 0], key=lambda r: r[1])[:top]
    if not ups and not downs:
        return None
    lines = []
    if ups:
        lines += ["\nЧто выросло по сравнению с прошлым периодом:"] + [fmt(r) for r in ups]
    if downs:
        lines += ["\nЧто снизилось:"] + [fmt(r) for r in downs]
    return "\n".join(lines)
