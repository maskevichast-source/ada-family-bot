"""Модуль финансовых целей и семейных копилок."""

import uuid
from services.timezone import now_astana
from services.money import parse_amount

HEADERS = ["goal_id", "date_created", "name", "target_amount", "current_amount", "deadline", "status"]


def _format_currency(value):
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except Exception:
        return str(value)


def _worksheet():
    from services.sheets import _get_or_create_worksheet
    return _get_or_create_worksheet("Goals", HEADERS, rows=50, cols=len(HEADERS))


def get_goals():
    from services.sheets import _get_all_records_safe
    records = _get_all_records_safe(_worksheet())
    return [r for r in records if r.get("name") and str(r.get("status", "active")).lower() == "active"]


def add_goal(name: str, target_amount: float, deadline: str = ""):
    ws = _worksheet()
    now = now_astana()
    goal_id = f"GOAL_{now.strftime('%Y%m%d')}_{uuid.uuid4().hex[:4]}"
    row = [
        goal_id,
        now.strftime("%Y-%m-%d"),
        name.strip(),
        parse_amount(target_amount),
        0.0,
        deadline.strip(),
        "active"
    ]
    ws.append_row(row, value_input_option="RAW")
    return dict(zip(HEADERS, row))


def deposit_to_goal(name_query: str, amount: float):
    ws = _worksheet()
    from services.sheets import _get_all_records_safe
    records = _get_all_records_safe(ws)
    target = None
    target_row = -1

    for idx, r in enumerate(records, start=2):
        if str(r.get("status", "active")).lower() == "active":
            if name_query.lower() in str(r.get("name", "")).lower():
                target = r
                target_row = idx
                break

    if not target:
        return None

    cur = parse_amount(target.get("current_amount", 0))
    new_amt = cur + parse_amount(amount)
    ws.update_cell(target_row, 5, new_amt)
    target["current_amount"] = new_amt
    return target


def format_goals_list(items):
    if not items:
        return "🎯 Активных целей пока нет. Напиши: «создай цель Отпуск 800000»."

    lines = ["🎯 **Семейные копилки и финансовые цели:**\n"]
    for g in items:
        target = parse_amount(g.get("target_amount", 0))
        curr = parse_amount(g.get("current_amount", 0))
        pct = int((curr / target) * 100) if target > 0 else 0
        filled = min(pct // 10, 10)
        bar = "█" * filled + "░" * (10 - filled)

        lines.append(
            f"• **{g.get('name')}**\n"
            f"  `[{bar}]` **{pct}%** ({_format_currency(curr)} из {_format_currency(target)} KZT)\n"
            + (f"  Срок: до {g.get('deadline')}\n" if g.get("deadline") else "")
        )
    return "\n".join(lines)
