"""Блок «Внимание» для мини-аппа: то, на что стоит обратить внимание в этом месяце.

Всегда считается по текущему месяцу и всей семье, независимо от выбранного вида.
Правила те же, что у уведомлений бота (пороги совпадают с services/analytics.py):
  * лимит: категория от 85% лимита или уже в перерасходе;
  * крупная покупка: за последние 7 дней сумма в 1,5 раза и более выше прошлого
    максимума этого человека в этой категории за 90 дней (прошлых покупок ≥ 3);
  * темп: для категорий БЕЗ лимита траты с 1-го числа по сегодня в 1,7 раза и более
    выше обычного темпа за тот же отрезок трёх прошлых месяцев (сумма ≥ 5 000,
    прошлых месяцев с данными ≥ 2).
"""
import datetime

from services.categories import is_income_type
from services.money import parse_amount
from services.timezone import ASTANA_TZ, parse_flexible_datetime

BIG_RATIO = 1.5
BIG_MIN_PRIOR = 3
BIG_LOOKBACK_DAYS = 90
BIG_RECENT_DAYS = 7
PACE_RATIO = 1.7
PACE_MIN_AMOUNT = 5000
PACE_MIN_MONTHS = 2
MAX_ALERTS = 8


def _expenses(transactions):
    for t in transactions:
        dt = parse_flexible_datetime(t.get("date"))
        if dt is None or is_income_type(t.get("type")):
            continue
        yield dt, parse_amount(t.get("amt", 0)), t


def compute_alerts(transactions: list[dict], limits: dict, now: datetime.datetime) -> list[dict]:
    from services.dashboard import _title_and_note, _user_of, add_months, compute_dashboard, _month_start

    alerts: list[dict] = []

    # 1) лимиты
    month = compute_dashboard(transactions, limits, now, months=1)
    flagged = [c for c in month["categories"] if c["status"] in ("warn", "over")]
    flagged.sort(key=lambda c: (c["status"] != "over", -(c["limit_pct"] or 0)))
    for c in flagged[:5]:
        alerts.append({"type": "limit", "status": c["status"], "category": c["name"],
                       "spent": c["spent"], "limit": c["limit"], "pct": c["limit_pct"]})

    rows = list(_expenses(transactions))

    # 2) необычно крупные покупки за последнюю неделю
    recent_from = datetime.datetime(now.year, now.month, now.day, tzinfo=ASTANA_TZ) - datetime.timedelta(days=BIG_RECENT_DAYS - 1)
    big = []
    for dt, amount, t in rows:
        if dt < recent_from:
            continue
        cat = str(t.get("cat") or "").strip()
        user = _user_of(t)
        window_start = dt - datetime.timedelta(days=BIG_LOOKBACK_DAYS)
        prior = [a for d2, a, t2 in rows
                 if window_start <= d2 < dt and str(t2.get("cat") or "").strip() == cat and _user_of(t2) == user]
        if len(prior) >= BIG_MIN_PRIOR and amount >= BIG_RATIO * max(prior):
            title, _ = _title_and_note(t)
            big.append({"type": "big", "date": dt.strftime("%d.%m.%Y"), "amount": round(amount, 2),
                        "category": cat, "text": title, "user": user, "prev_max": round(max(prior), 2)})
    big.sort(key=lambda a: a["amount"], reverse=True)
    alerts.extend(big[:3])

    # 3) ускоренный темп в категориях без лимита
    cur_start = _month_start(now.year, now.month)
    spent_now: dict[str, float] = {}
    for dt, amount, t in rows:
        if dt >= cur_start:
            cat = str(t.get("cat") or "").strip() or "Без категории"
            spent_now[cat] = spent_now.get(cat, 0.0) + amount
    pace = []
    for cat, spent in spent_now.items():
        if limits.get(cat) or spent < PACE_MIN_AMOUNT:
            continue
        usual_months = []
        for back in (1, 2, 3):
            y, m = add_months(now.year, now.month, -back)
            ms = _month_start(y, m)
            me = ms + datetime.timedelta(days=now.day)
            total = sum(a for d2, a, t2 in rows
                        if ms <= d2 < me and (str(t2.get("cat") or "").strip() or "Без категории") == cat)
            if total > 0:
                usual_months.append(total)
        if len(usual_months) >= PACE_MIN_MONTHS:
            usual = sum(usual_months) / len(usual_months)
            if spent >= PACE_RATIO * usual:
                pace.append({"type": "pace", "category": cat, "spent": round(spent, 2),
                             "usual": round(usual, 2), "ratio": round(spent / usual, 1)})
    pace.sort(key=lambda a: a["spent"], reverse=True)
    alerts.extend(pace[:3])

    return alerts[:MAX_ALERTS]
