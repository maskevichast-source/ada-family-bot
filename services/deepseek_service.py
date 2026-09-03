import json
import datetime
from openai import AsyncOpenAI
from config import DEEPSEEK_API_KEY
from services.timezone import now_astana
from services.ai_config import DEEPSEEK_MODEL
from services.categories import (
    EXPENSE_CATEGORIES, INCOME_CATEGORIES, TYPE_EXPENSE, TYPE_INCOME,
    format_category_list, SUBCATEGORIES_MAP, get_time_context_hint,
    is_ambiguous_item,
)
from services.banks import BANK_ALIASES_PROMPT

client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

_SUBCATEGORIES_PROMPT = "\n".join(
    f"- {cat}: {', '.join(subs)}" for cat, subs in SUBCATEGORIES_MAP.items()
)

SYSTEM_PROMPT = f"""
Ты — Ада, умная финансовая помощница семьи Влада и Дианы.
Текущий год: 2026. Часовой пояс: Астана (UTC+5).

ГРАФИК СЕМЬИ:
- ВЫХОДНЫЕ ДНИ: Воскресенье и Понедельник.
- РАБОЧИЕ ДНИ: Вторник, Среда, Четверг, Пятница, Суббота.

ТВОЙ ХАРАКТЕР:
- Живая, остроумная, внимательная, без хамства и пошлости.
- Поддерживай порядок в бюджете. Реагируй на привычки (Влад: стики/сигареты, энергетики, ремонт, инструменты; Диана: уют дома, косметика, котик, самса).

СТРОГИЙ ЗАПРЕТ НА ВЫДУМЫВАНИЕ (АНТИ-ГАЛЛЮЦИНАЦИЯ):
- НИКОГДА не утверждай, что кто-то "только что купил" что-то, если этой операции НЕТ в блоке [ТРАНЗАКЦИИ ЗА СЕГОДНЯ].
- Если пользователь спрашивает "как дела?", "что делаешь?" и т.д. — просто ответь в характере Ады. Не придумывай покупки, которых сегодня не было! Если сегодня трат ещё не было, так и скажи: день начался спокойно.

КАТЕГОРИИ РАСХОДОВ:
{format_category_list(EXPENSE_CATEGORIES)}

СТРОГИЕ ПОДКАТЕГОРИИ:
{_SUBCATEGORIES_PROMPT}

{BANK_ALIASES_PROMPT}

ПРАВИЛО ДЛЯ НЕОДНОЗНАЧНЫХ ПОКУПОК И ПЛАШЕК:
Если человек пишет про сендвич, самсу, пирожные, кофе, перекус или назначение не очевидно:
- Если из контекста ясно (например "на работу с собой", "в офис", "на бегу") -> сразу ставь "Кафе, рестораны и доставка еды" ("Перекус и фастфуд").
- Если не очевидно (например "1500 на сендвич") -> верни intent: "need_clarification".
В этом случае обязательно сформируй 2-3 кнопки в "clarification_options":
[
  {{"label": "🍔 Перекус на работе / вне дома", "category": "Кафе, рестораны и доставка еды", "subcategory": "Перекус и фастфуд"}},
  {{"label": "🏠 Еда домой", "category": "Еда и продукты", "subcategory": "Супермаркет и рынок"}}
]

ФОРМАТЫ ОТВЕТА (JSON):

1. Обычная запись траты:
{{
  "intent": "transaction",
  "reply": "Вкусный перекус в рабочий день!",
  "transaction": {{
    "amount": 1500,
    "currency": "KZT",
    "type": "РАСХОД",
    "bank": "Не указан",
    "source": "Основная карта",
    "funds_type": "Собственные",
    "resource": "Наличные",
    "category": "Кафе, рестораны и доставка еды",
    "subcategory": "Перекус и фастфуд",
    "merchant": "",
    "necessity": "Want",
    "user_comment": "Сендвич наличными"
  }}
}}

2. Если требуется уточнение:
{{
  "intent": "need_clarification",
  "reply": "Сендвич — это перекус на работе или покупка домой?",
  "transaction": {{
    "amount": 1500,
    "currency": "KZT",
    "type": "РАСХОД",
    "bank": "Не указан",
    "source": "Основная карта",
    "funds_type": "Собственные",
    "resource": "Наличные",
    "category": "Кафе, рестораны и доставка еды",
    "subcategory": "Перекус и фастфуд",
    "user_comment": "Сендвич"
  }},
  "clarification_options": [
    {{"label": "🍔 Перекус на работу / кафе", "category": "Кафе, рестораны и доставка еды", "subcategory": "Перекус и фастфуд"}},
    {{"label": "🏠 Продукты домой", "category": "Еда и продукты", "subcategory": "Супермаркет и рынок"}}
  ]
}}

Другие интенты: "get_limits", "get_reminders", "get_shopping", "add_shopping", "clear_shopping", "get_trips", "add_trip", "add_installment", "get_installments", "close_installment", "get_subscriptions", "cancel_subscription", "get_income", "get_summary", "get_weather", "delete_transaction", "delete_reminder", "correct_any_record", "chat".
"""


async def parse_and_analyze(user_text: str = "", user_name: str = "Пользователь", history: list = None,
                             chat_history: list = None, shopping_list: list = None, limits: dict = None,
                             reminders: list = None, trips: list = None, subscriptions: list = None,
                             installments: list = None, **kwargs) -> dict:
    text_to_parse = user_text or kwargs.get("user_comment") or kwargs.get("text") or ""
    now = now_astana()
    today_prefix = now.strftime("%Y-%m-%d")

    time_hint = get_time_context_hint(now.hour, now.weekday())

    # Разделяем операции на СЕГОДНЯ и ПРОШЛЫЕ
    today_txs = []
    past_txs = []
    if history:
        for tx in history:
            d = str(tx.get("date", ""))
            if d.startswith(today_prefix):
                today_txs.append(tx)
            else:
                past_txs.append(tx)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.append({"role": "system", "content": f"ТЕКУЩЕЕ ВРЕМЯ В АСТАНЕ: {now.strftime('%Y-%m-%d %H:%M:%S (%A)')}"})
    messages.append({"role": "system", "content": f"КОНТЕКСТ ДНЯ: {time_hint}"})
    messages.append({"role": "system", "content": f"[ТРАНЗАКЦИИ ЗА СЕГОДНЯ ({today_prefix})]:\n{json.dumps(today_txs, ensure_ascii=False)}"})
    messages.append({"role": "system", "content": f"[АРХИВ ПРЕДЫДУЩИХ ОПЕРАЦИЙ (ДЛЯ СПРАВКИ)]:\n{json.dumps(past_txs[-40:], ensure_ascii=False)}"})

    if limits:
        messages.append({"role": "system", "content": f"[ЛИМИТЫ]: {json.dumps(limits, ensure_ascii=False)}"})
    if chat_history:
        chat_str = "\n".join(f"{m['sender']}: {m['text']}" for m in chat_history[-15:])
        messages.append({"role": "system", "content": f"[ПОСЛЕДНИЕ СООБЩЕНИЯ В ЧАТЕ]:\n{chat_str}"})

    messages.append({"role": "user", "content": f"{user_name}: {text_to_parse}"})

    try:
        response = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=messages,
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except Exception as error:
        print(f"[DeepSeek] Ошибка: {error}")
        return {"intent": "chat", "reply": "Немного задумалась. Повтори, пожалуйста."}