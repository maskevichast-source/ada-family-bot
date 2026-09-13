"""Модуль финансовых целей и семейных копилок."""

import asyncio
import re
import uuid
from services.timezone import now_astana, parse_flexible_datetime
from services.money import parse_amount
from services.telegram_safe import safe_answer

HEADERS = ["goal_id", "date_created", "name", "target_amount", "current_amount", "deadline", "status"]

HELP = (
    "🎯 Копилки\n\n"
    "«Создай цель Отпуск 500000»\n«Создай цель Отпуск 500000 до 1 июля»\n"
    "«Закинь в копилку Отпуск 20000»\n«Покажи цели» / «Покажи копилки»\n\n"
    "Сумма — в KZT."
)

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
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Название цели не может быть пустым.")
    amt = parse_amount(target_amount)
    if amt <= 0:
        raise ValueError("Сумма цели должна быть положительной.")

    deadline_str = ""
    if deadline:
        deadline_dt = parse_flexible_datetime(deadline)
        deadline_str = deadline_dt.strftime("%Y-%m-%d") if deadline_dt else str(deadline).strip()[:10]

    ws = _worksheet()
    now = now_astana()
    goal_id = f"GOAL_{now.strftime('%Y%m%d')}_{uuid.uuid4().hex[:4]}"
    row = [
        goal_id,
        now.strftime("%Y-%m-%d"),
        clean_name,
        amt,
        0.0,
        deadline_str,
        "active"
    ]
    ws.append_row(row, value_input_option="RAW")
    return dict(zip(HEADERS, row))


def deposit_to_goal(name_query: str, amount: float):
    amt = parse_amount(amount)
    if amt <= 0:
        raise ValueError("Сумма пополнения должна быть положительной.")
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
    new_amt = cur + amt
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


_ADD_RE = re.compile(
    r"^(?:создай|добавь|заведи|новая)\s+цел[ьи]\s+(.+?)\s+(\d[\d\s]*)"
    r"(?:\s*(?:тенге|тг|kzt))?\s*(?:,?\s*до\s+(.+))?$",
    re.IGNORECASE,
)
_DEPOSIT_RE = re.compile(
    r"^(?:закинь|положи|пополни|добавь)\s+(?:в\s+копилк[уи]|копилк[уи]|(?:на\s+цель))?\s*"
    r"(.+?)\s+(?:на\s+)?(\d[\d\s]*)\s*(?:тенге|тг|kzt)?$",
    re.IGNORECASE,
)


def is_add(text: str) -> bool:
    t = str(text or "").strip().lower().replace("ё", "е")
    return bool(re.search(r"\b(?:созда[йть]+|добавь|заведи|новая)\s+цел[ьи]\b", t))


def is_deposit(text: str) -> bool:
    t = str(text or "").strip().lower().replace("ё", "е")
    return bool(re.search(r"\b(?:копилк[ауи]|цель)\b", t)) and any(
        w in t for w in ["закинь", "положи", "пополни"]
    )


def is_list(text: str) -> bool:
    t = str(text or "").strip().lower().replace("ё", "е")
    return bool(re.search(r"\bпокажи\b.*\b(?:цел[ьи]|копилк)", t)) or bool(
        re.search(r"\bмои\s+цели\b", t)
    )


async def handle(message, text: str, author: str) -> bool:
    """Прямой локальный перехват — без похода к ИИ для типовых фраз."""
    t = str(text or "").strip()

    if is_list(t):
        items = await asyncio.to_thread(get_goals)
        await safe_answer(message, format_goals_list(items), parse_mode=None)
        return True

    m = _ADD_RE.match(t)
    if m:
        name, amount_str, deadline = m.group(1), m.group(2), m.group(3)
        try:
            goal = await asyncio.to_thread(add_goal, name, amount_str, deadline or "")
            until_line = f" до {goal['deadline']}" if goal.get("deadline") else ""
            await safe_answer(
                message,
                f"🎯 Завела цель «{goal['name']}»: {_format_currency(goal['target_amount'])} KZT{until_line}.",
            )
        except ValueError as e:
            await safe_answer(message, str(e))
        return True

    m = _DEPOSIT_RE.match(t)
    if m:
        name_query, amount_str = m.group(1).strip(" ,."), m.group(2)
        try:
            goal = await asyncio.to_thread(deposit_to_goal, name_query, amount_str)
        except ValueError as e:
            await safe_answer(message, str(e))
            return True
        if not goal:
            await safe_answer(message, f"Не нашла активную цель «{name_query}». Покажи цели, чтобы свериться с названием.")
            return True
        target = parse_amount(goal.get("target_amount", 0))
        curr = parse_amount(goal.get("current_amount", 0))
        pct = int((curr / target) * 100) if target > 0 else 0
        done = " 🎉 Цель достигнута!" if curr >= target > 0 else ""
        await safe_answer(
            message,
            f"✅ Пополнила «{goal['name']}»: {_format_currency(curr)} из {_format_currency(target)} KZT ({pct}%).{done}",
        )
        return True

    return False


async def handle_model(message, parsed: dict, author: str) -> bool:
    """Фолбэк через ИИ — для формулировок, которые не поймал handle()."""
    intent = parsed.get("intent")
    goal_data = parsed.get("goal") or {}

    if intent == "get_goals":
        items = await asyncio.to_thread(get_goals)
        await safe_answer(message, format_goals_list(items), parse_mode=None)
        return True

    if intent != "add_goal" and intent != "deposit_goal":
        return False

    action = goal_data.get("action") or ("deposit" if intent == "deposit_goal" else "create")
    name = str(goal_data.get("name") or "").strip()
    if not name:
        return False

    try:
        if action == "deposit":
            goal = await asyncio.to_thread(deposit_to_goal, name, goal_data.get("amount"))
            if not goal:
                await safe_answer(message, f"Не нашла активную цель «{name}».")
                return True
            target = parse_amount(goal.get("target_amount", 0))
            curr = parse_amount(goal.get("current_amount", 0))
            pct = int((curr / target) * 100) if target > 0 else 0
            done = " 🎉 Цель достигнута!" if curr >= target > 0 else ""
            await safe_answer(message, f"✅ Пополнила «{goal['name']}»: {_format_currency(curr)} из {_format_currency(target)} KZT ({pct}%).{done}")
        else:
            goal = await asyncio.to_thread(
                add_goal, name, goal_data.get("target_amount"), goal_data.get("deadline") or ""
            )
            until_line = f" до {goal['deadline']}" if goal.get("deadline") else ""
            await safe_answer(message, f"🎯 Завела цель «{goal['name']}»: {_format_currency(goal['target_amount'])} KZT{until_line}.")
    except ValueError as e:
        await safe_answer(message, str(e))
    return True
