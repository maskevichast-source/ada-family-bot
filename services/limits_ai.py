"""Пересчёт лимитов бюджета по истории трат: считает код (services/limits_engine.py).

Старый промпт для DeepSeek (LIMITS_SYSTEM_PROMPT) оставлен в файле, но больше не используется."""

import asyncio
import datetime
import json
from openai import AsyncOpenAI
from config import DEEPSEEK_API_KEY
from services.ai_config import DEEPSEEK_MODEL
from services.categories import (
    EXPENSE_CATEGORIES, DEFAULT_EXPENSE_LIMITS, TYPE_EXPENSE,
)
from services.sheets import get_last_200_transactions, save_category_limits
from services.timezone import now_astana

client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

LIMITS_SYSTEM_PROMPT = f"""
Ты — Ада, финансовый аналитик. На основе истории трат семьи за последние месяцы
сгенерируй реалистичные месячные лимиты по категориям.

Категории (используй ТОЛЬКО эти названия):
{chr(10).join(f"- {cat}" for cat in EXPENSE_CATEGORIES)}

Правила:
1. Анализируй средние траты за последние 3 месяца по каждой категории.
2. Лимит должен быть на 10-20% выше среднего (буфер на непредвиденное).
3. Если в категории почти не тратят — ставь минимальный лимит 5000-10000 тг.
4. Критичные категории (еда, жильё, транспорт) — лимит должен покрывать реальные траты.
5. Не занижай лимиты до нереалистичных цифр — это демотивирует.
6. Верни ТОЛЬКО JSON с полем "limits": {{"категория": сумма, ...}}
"""


SNAPSHOT_NAMESPACE = "limits_snapshot"


def _load_inputs(now):
    """Один проход по Google Sheets: транзакции за 6 месяцев, лимиты, закреплённые."""
    from services import sheets
    from services.limits_engine import _shift

    fy, fm = _shift(now.year, now.month, -6)
    start = f"{fy:04d}-{fm:02d}-01"
    end = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    return (
        sheets.get_transactions_for_period(start, end),
        sheets.get_category_limits(),
        sheets.get_pinned_categories(),
    )


def build_plan(now=None) -> dict | None:
    """Считает новые лимиты, ничего не записывая. None — если данных нет
    (в т.ч. если чтение таблицы не удалось: тогда лимиты трогать нельзя)."""
    from services.limits_engine import compute_limits

    now = now or now_astana()
    transactions, current, pinned = _load_inputs(now)
    if not transactions:
        return None
    result = compute_limits(transactions, now, current, pinned)
    return {"result": result, "old": current, "now": now}


def preview_limits_text() -> str:
    """Черновик для команды /limits_plan: ничего не записывает."""
    from services.limits_engine import format_summary

    plan = build_plan()
    if not plan:
        return "Не удалось прочитать траты из таблицы, поэтому ничего не считаю. Лимиты не тронуты."
    return format_summary(plan["result"], plan["old"], plan["now"], preview=True)


def recalc_and_apply() -> tuple[dict, str] | None:
    """Пересчитывает и ЗАПИСЫВАЕТ лимиты. Перед записью сохраняет снимок старых
    (для /limits_undo). Возвращает (новые лимиты, текст сводки) или None."""
    from services import state
    from services.limits_engine import format_summary

    plan = build_plan()
    if not plan:
        return None
    result, old, now = plan["result"], plan["old"], plan["now"]
    state.put(SNAPSHOT_NAMESPACE, "last", {
        "saved_at": now.isoformat(),
        "limits": old,
        "pinned": sorted(result["pinned"]),
    })
    save_category_limits(result["limits"], pinned=set(result["pinned"]))
    return result["limits"], format_summary(result, old, now)


def restore_previous_limits() -> str:
    """Откат к лимитам до последнего пересчёта (команда /limits_undo)."""
    from services import state

    snap = state.get(SNAPSHOT_NAMESPACE, "last")
    if not snap or not snap.get("limits"):
        return "Нечего откатывать: снимка прежних лимитов нет."
    save_category_limits(snap["limits"], pinned=set(snap.get("pinned") or []))
    state.delete(SNAPSHOT_NAMESPACE, "last")
    return f"Вернула лимиты, которые были до пересчёта от {str(snap.get('saved_at', ''))[:10]}."


async def generate_limits_from_history() -> dict:
    """Пересчитать лимиты по истории трат (детерминированно, без ИИ).

    Имя и тип результата сохранены для старых вызовов (интент «сгенерируй
    лимиты»). При ошибке лимиты остаются как были, а не сбрасываются в дефолт.
    """
    try:
        out = await asyncio.to_thread(recalc_and_apply)
        return out[0] if out else {}
    except Exception as e:
        print(f"[Лимиты] Ошибка пересчёта: {e}")
        return {}
