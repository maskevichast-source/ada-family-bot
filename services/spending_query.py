"""Ответы на вопросы про траты («сколько мы потратили на еду в сентябре», «когда последний раз платили за интернет»).

Модель только разбирает вопрос в структуру (период, фильтры, вид ответа). Все цифры считает этот модуль по таблице,
поэтому числа не придумываются, а текст ответа собирается кодом."""
import calendar
import datetime

from services.money import parse_amount

_MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
               "ноября", "декабря"]
_MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
               "ноябрь", "декабрь"]
KINDS = {"sum", "top", "last", "count", "breakdown"}
MAX_TOP = 10


def _money(value) -> str:
    return f"{float(value):,.0f}".replace(",", " ")


def _month_range(year: int, month: int):
    start = datetime.date(year, month, 1)
    end = datetime.date(year + 1, 1, 1) if month == 12 else datetime.date(year, month + 1, 1)
    return start, end


def resolve_period(period, today: datetime.date, date_from=None, date_to=None):
    """(начало, конец не включая, подпись) или None, если период не понят."""
    one = datetime.timedelta(days=1)
    try:
        if date_from and date_to:
            a, b = datetime.date.fromisoformat(str(date_from)[:10]), datetime.date.fromisoformat(str(date_to)[:10])
            if b < a:
                a, b = b, a
            return a, b + one, f"с {a:%d.%m.%Y} по {b:%d.%m.%Y}"
    except ValueError:
        return None
    p = str(period or "this_month").strip().lower()
    if p == "today":
        return today, today + one, "сегодня"
    if p == "yesterday":
        return today - one, today, "вчера"
    if p == "this_week":
        monday = today - datetime.timedelta(days=today.weekday())
        return monday, today + one, "на этой неделе"
    if p == "last_week":
        monday = today - datetime.timedelta(days=today.weekday())
        return monday - datetime.timedelta(days=7), monday, "на прошлой неделе"
    if p == "last_7_days":
        return today - datetime.timedelta(days=6), today + one, "за последние 7 дней"
    if p == "last_30_days":
        return today - datetime.timedelta(days=29), today + one, "за последние 30 дней"
    if p == "this_month":
        s, e = _month_range(today.year, today.month)
        return s, e, f"за {_MONTHS_NOM[today.month - 1]}"
    if p == "last_month":
        y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        s, e = _month_range(y, m)
        return s, e, f"за {_MONTHS_NOM[m - 1]}"
    if p == "this_year":
        return datetime.date(today.year, 1, 1), datetime.date(today.year + 1, 1, 1), f"за {today.year} год"
    if p == "last_year":
        return datetime.date(today.year - 1, 1, 1), datetime.date(today.year, 1, 1), f"за {today.year - 1} год"
    if p == "all":
        return datetime.date(2000, 1, 1), today + one, "за всё время"
    try:
        if len(p) == 7 and p[4] == "-":                              # «2026-09»
            y, m = int(p[:4]), int(p[5:7])
            if not 1 <= m <= 12:
                return None
            s, e = _month_range(y, m)
            return s, e, f"за {_MONTHS_NOM[m - 1]} {y}"
        if len(p) == 4 and p.isdigit():
            y = int(p)
            return datetime.date(y, 1, 1), datetime.date(y + 1, 1, 1), f"за {y} год"
    except ValueError:
        return None
    return None


def _contains(haystack, needle) -> bool:
    return str(needle or "").strip().lower() in str(haystack or "").lower()


def _matches(tx: dict, q: dict) -> bool:
    if q.get("category") and not (_contains(tx.get("cat"), q["category"]) or _contains(tx.get("subcat"), q["category"])):
        return False
    if q.get("subcategory") and not _contains(tx.get("subcat"), q["subcategory"]):
        return False
    if q.get("merchant"):
        blob = f'{tx.get("merchant", "")} {tx.get("comm", "")} {tx.get("subcat", "")}'
        if not _contains(blob, q["merchant"]):
            return False
    if q.get("bank") and not _contains(tx.get("bank"), q["bank"]) and not _contains(tx.get("source"), q["bank"]):
        return False
    if q.get("user") and str(tx.get("user") or "").strip().lower() != str(q["user"]).strip().lower():
        return False
    return True


def _describe(q: dict) -> str:
    parts = [str(q[k]) for k in ("category", "subcategory", "merchant") if q.get(k)]
    if q.get("bank"):
        parts.append(f"банк {q['bank']}")
    if q.get("user"):
        parts.append(f"({q['user']})")
    return " · ".join(parts)


def answer(q: dict, transactions: list[dict], period_text: str) -> str:
    """Текст ответа по уже отобранным за период операциям."""
    income = str(q.get("type") or "").upper().startswith("ДОХ")
    want = "ДОХОД" if income else "РАСХОД"
    noun = "доходы" if income else "траты"
    rows = [t for t in transactions if str(t.get("type")) == want and _matches(t, q)]
    what = _describe(q)
    head = f"{noun.capitalize()}{' (' + what + ')' if what else ''} {period_text}"
    if not rows:
        return f"{head}: ничего не нашла."
    total = sum(float(parse_amount(t.get("amt", 0)) or 0) for t in rows)
    kind = str(q.get("kind") or "sum").lower()
    if kind == "last":
        last = max(rows, key=lambda t: str(t.get("date")))
        day = str(last.get("date"))[:10]
        try:
            d = datetime.date.fromisoformat(day)
            day = f"{d.day} {_MONTHS_GEN[d.month - 1]} {d.year}"
        except ValueError:
            pass
        name = last.get("merchant") or last.get("subcat") or last.get("cat")
        return f"Последняя операция: {day}, {_money(last['amt'])} тг, {name}."
    if kind == "count":
        return f"{head}: {len(rows)} операций на {_money(total)} тг."
    if kind == "top":
        n = max(1, min(int(parse_amount(q.get("limit", 5)) or 5), MAX_TOP))
        best = sorted(rows, key=lambda t: -float(t.get("amt") or 0))[:n]
        lines = [f"{head}: топ-{len(best)} из {len(rows)} (всего {_money(total)} тг)"]
        for i, t in enumerate(best, 1):
            name = t.get("merchant") or t.get("subcat") or t.get("cat")
            lines.append(f"{i}. {_money(t['amt'])} тг, {name}, {str(t.get('date'))[:10]}")
        return "\n".join(lines)
    if kind == "breakdown":
        by = {}
        for t in rows:
            key = str(t.get("cat") or "Прочее")
            by[key] = by.get(key, 0) + float(t.get("amt") or 0)
        lines = [f"{head}: {_money(total)} тг, по категориям:"]
        for key, value in sorted(by.items(), key=lambda kv: -kv[1])[:10]:
            lines.append(f"- {key}: {_money(value)} тг ({value / total * 100:.0f}%)")
        return "\n".join(lines)
    return f"{head}: {_money(total)} тг ({len(rows)} операций)."


def run(q: dict, fetch, today: datetime.date) -> str:
    """fetch(start_iso, end_iso) -> операции за период (как sheets.get_transactions_for_period)."""
    q = q or {}
    if str(q.get("kind") or "sum").lower() not in KINDS:
        q = dict(q, kind="sum")
    resolved = resolve_period(q.get("period"), today, q.get("date_from"), q.get("date_to"))
    if not resolved:
        return "Не поняла период. Скажи, например: «за сентябрь», «за прошлую неделю» или «с 1 по 15 октября»."
    start, end, label = resolved
    rows = fetch(start.isoformat(), end.isoformat())
    return answer(q, rows, label)
