"""Кредитная нагрузка для мини-аппа (только чтение).

Платежи по кредитам и рассрочкам в таблице разложены по разным категориям
(«Электроника», «Финансовые расходы»...), поэтому определяем их по словам в названии,
подкатегории и комментарии. Это эвристика: мини-апп честно об этом пишет.
"""
import datetime

from services.categories import is_income_type
from services.money import parse_amount
from services.timezone import parse_flexible_datetime

LOAN_KEYWORDS = ("кредит", "рассрочк", "погашени", "kaspi red", "ozen")
HISTORY_MONTHS = 3
MAX_PAYEES = 12


def is_loan_payment(t: dict) -> bool:
    if is_income_type(t.get("type")):
        return False
    text = " ".join(str(t.get(k) or "") for k in ("merchant", "subcat", "comm")).lower()
    return any(k in text for k in LOAN_KEYWORDS)


def _payee(t: dict) -> str:
    merchant = " ".join(str(t.get("merchant") or "").split())
    if merchant:
        return merchant
    return " ".join(str(t.get("comm") or "").split())[:40] or "Без названия"


def compute_loans(transactions: list[dict], now: datetime.datetime) -> dict:
    from services.dashboard import _MONTHS_RU, add_months

    months = [add_months(now.year, now.month, -i) for i in range(HISTORY_MONTHS - 1, -1, -1)]   # старые первыми
    pay_total = {m: 0.0 for m in months}
    pay_count = {m: 0 for m in months}
    income = {m: 0.0 for m in months}
    payees: dict[str, dict] = {}

    for t in transactions:
        dt = parse_flexible_datetime(t.get("date"))
        key = (dt.year, dt.month) if dt else None
        if key not in pay_total:
            continue
        amount = parse_amount(t.get("amt", 0))
        if is_income_type(t.get("type")):
            income[key] += amount
        elif is_loan_payment(t):
            pay_total[key] += amount
            pay_count[key] += 1
            name = _payee(t)
            p = payees.setdefault(name.lower(), {"name": name, "by_month": {}})
            p["by_month"][key] = p["by_month"].get(key, 0.0) + amount

    cur = months[-1]
    rows = []
    for p in payees.values():
        seen = len(p["by_month"])
        rows.append({
            "name": p["name"],
            "amount": round(p["by_month"].get(cur, 0.0), 2),
            "months_seen": seen,
            "avg": round(sum(p["by_month"].values()) / seen, 2),
        })
    # сначала те, кто платит в этом месяце, по убыванию; затем прошлые
    rows.sort(key=lambda r: (r["amount"] <= 0, -(r["amount"] or r["avg"])))

    return {
        "months": [{
            "label": _MONTHS_RU[m - 1],
            "total": round(pay_total[(y, m)], 2),
            "count": pay_count[(y, m)],
            "income": round(income[(y, m)], 2),
            "share_pct": round(pay_total[(y, m)] / income[(y, m)] * 100) if income[(y, m)] > 0 else None,
        } for y, m in months],
        "payees": rows[:MAX_PAYEES],
        "note": "Определено по словам «кредит», «рассрочка», «погашение», Kaspi Red, ozen в названии, подкатегории и комментарии.",
    }
