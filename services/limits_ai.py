"""Генерация лимитов бюджета на новый месяц через ИИ.

Раньше эта логика была продублирована почти дословно в main.py
(generate_monthly_limits_via_ai) и в handlers/text_handler.py
(generate_limits_logic), с чуть разными промптами и списками категорий.
Из-за этого они могли незаметно разойтись. Теперь оба места вызывают
одну функцию отсюда.
"""

import json

from openai import AsyncOpenAI

from config import DEEPSEEK_API_KEY
from services.ai_config import DEEPSEEK_MODEL
from services.categories import DEFAULT_EXPENSE_LIMITS, EXPENSE_CATEGORIES, TYPE_EXPENSE, format_category_list

client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")


async def generate_ai_limits(transactions_history: list) -> dict:
    """Проанализировать историю трат и предложить лимиты по категориям на новый месяц."""
    prompt = f"""
Ты — финансовый аналитик Ада. Проанализируй историю операций семьи Влада и Дианы в Астане:
{json.dumps(transactions_history[-200:], ensure_ascii=False)}

Учитывай только записи с type = "{TYPE_EXPENSE}" (доходы в лимиты не включай).

Придумай адекватные, сбалансированные лимиты на следующий месяц по категориям:
{format_category_list(EXPENSE_CATEGORIES)}

Верни STRICT JSON, где ключи — точные названия категорий из списка выше,
а значения — числа (суммы в KZT). Больше ничего лишнего.
"""
    try:
        response = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        limits = json.loads(response.choices[0].message.content)
        return limits if isinstance(limits, dict) and limits else dict(DEFAULT_EXPENSE_LIMITS)
    except Exception as error:
        print(f"[Лимиты] Не удалось сгенерировать лимиты через ИИ: {error}")
        return dict(DEFAULT_EXPENSE_LIMITS)
