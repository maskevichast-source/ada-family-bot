"""Генерация лимитов бюджета на основе истории трат через DeepSeek."""

import json
from openai import AsyncOpenAI
from config import DEEPSEEK_API_KEY
from services.ai_config import DEEPSEEK_MODEL
from services.categories import (
    EXPENSE_CATEGORIES, DEFAULT_EXPENSE_LIMITS, TYPE_EXPENSE,
)
from services.sheets import get_last_200_transactions, save_category_limits

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


async def generate_limits_from_history() -> dict:
    """Сгенерировать лимиты на основе истории трат."""
    try:
        transactions = get_last_200_transactions()

        # Группируем траты по категориям за последние 3 месяца
        from collections import defaultdict
        from datetime import datetime

        monthly_by_cat = defaultdict(lambda: defaultdict(float))

        for t in transactions:
            if str(t.get("type")) != TYPE_EXPENSE:
                continue
            date_str = str(t.get("date", ""))
            if len(date_str) >= 7:
                month_key = date_str[:7]  # YYYY-MM
                cat = str(t.get("cat") or "Прочее")
                try:
                    amt = float(str(t.get("amt", 0)).replace(",", "."))
                    monthly_by_cat[cat][month_key] += amt
                except (ValueError, TypeError):
                    pass

        # Формируем контекст для ИИ
        context = []
        for cat, months in monthly_by_cat.items():
            avg = sum(months.values()) / max(len(months), 1)
            context.append(f"{cat}: среднее {avg:.0f} тг/мес (данные за {len(months)} мес)")

        messages = [
            {"role": "system", "content": LIMITS_SYSTEM_PROMPT},
            {"role": "user", "content": f"История трат:\n{chr(10).join(context)}\n\nСгенерируй лимиты."}
        ]

        response = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=messages,
            response_format={"type": "json_object"}
        )

        result = json.loads(response.choices[0].message.content)
        limits = result.get("limits", {})

        # Валидация: все категории из EXPENSE_CATEGORIES должны быть
        final_limits = {}
        for cat in EXPENSE_CATEGORIES:
            final_limits[cat] = float(limits.get(cat, DEFAULT_EXPENSE_LIMITS.get(cat, 10000)))

        save_category_limits(final_limits)
        return final_limits

    except Exception as e:
        print(f"[Лимиты AI] Ошибка генерации: {e}")
        # Возвращаем дефолтные
        save_category_limits(DEFAULT_EXPENSE_LIMITS)
        return DEFAULT_EXPENSE_LIMITS


