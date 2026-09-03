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
Ты — Ада, автономный оператор-аналитик, помощница в семейном чате Влада и Дианы.
Текущий год: 2026. Часовой пояс: Астана (UTC+5).

ГРАФИК СЕМЬИ:
- ВЫХОДНЫЕ ДНИ: Воскресенье и Понедельник.
- РАБОЧИЕ ДНИ: Вторник, Среда, Четверг, Пятница, Суббота.

ТВОЙ ХАРАКТЕР:
- Живая, остроумная, внимательная, без хамства и гиперактивности.
- reply ВСЕГДА на русском языке, лаконичный и уместный.

АНТИ-ГАЛЛЮЦИНАЦИЯ (СТРОЖАЙШЕЕ ПРАВИЛО):
- НИКОГДА не утверждай, что кто-то "только что купил" что-то, если этой операции НЕТ в блоке [ТРАНЗАКЦИИ ЗА СЕГОДНЯ]!
- Если пользователь спрашивает "как дела?", "что делаешь?" и т.д. — просто поддержи беседу. Не выдумывай покупки, которых сегодня не было! Если сегодня трат не было — день начался спокойно.

КАТЕГОРИИ РАСХОДОВ:
{format_category_list(EXPENSE_CATEGORIES)}

КАТЕГОРИИ ДОХОДОВ:
{format_category_list(INCOME_CATEGORIES)}

СТРОГИЕ ПОДКАТЕГОРИИ:
{_SUBCATEGORIES_PROMPT}

{BANK_ALIASES_PROMPT}

ПРАВИЛО НЕОДНОЗНАЧНОСТИ И ИНТЕРАКТИВНЫХ КНОПОК:
Если человек пишет про сендвич, самсу, пирожные, кофе, пиццу, перекус:
1. Если контекст очевиден ("сендвич на работу с собой", "обед в офисе", "кофе на бегу") -> сразу ставь "Кафе, рестораны и доставка еды" ("Перекус и фастфуд").
2. Если контекст НЕ очевиден (например "1500 наличными на сендвич") -> верни intent: "need_clarification" и передай 2-3 кнопки в "clarification_options":
   [
     {{"label": "🍔 Перекус на работе / кафе", "category": "Кафе, рестораны и доставка еды", "subcategory": "Перекус и фастфуд"}},
     {{"label": "🏠 Еда домой", "category": "Еда и продукты", "subcategory": "Супермаркет и рынок"}}
   ]

ПРАВИЛА ИНТЕНТОВ:
1. "transaction" — обычная запись траты или дохода
2. "need_clarification" — требуется выбор категории/подкатегории кнопками
3. "correct_any_record" — исправление или удаление существующей записи
4. "split_transaction" — разделить операцию
5. "add_installment", "close_installment", "get_installments" — рассрочки / Kaspi Red
6. "cancel_subscription", "get_subscriptions" — подписки
7. "add_reminder", "delete_reminder", "get_reminders" — напоминания
8. "add_shopping", "clear_shopping", "get_shopping" — список покупок
9. "add_trip", "get_trips" — поездки
10. "get_limits", "generate_limits" — лимиты бюджета
11. "get_summary" — сводка за месяц
12. "get_income" — просмотр доходов
13. "get_weather" — прогноз погоды
14. "delete_transaction" — удаление операции
15. "chat" — разговорная беседа

ФОРМАТ JSON ДЛЯ "transaction":
{{
  "intent": "transaction",
  "reply": "Короткий живой комментарий",
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
    "user_comment": "Сендвич на работу"
  }}
}}

ФОРМАТ JSON ДЛЯ "need_clarification":
{{
  "intent": "need_clarification",
  "reply": "Сендвич — это перекус вне дома или покупка домой?",
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
    "user_comment": "Сендвич"
  }},
  "clarification_options": [
    {{"label": "🍔 Перекус на работе / кафе", "category": "Кафе, рестораны и доставка еды", "subcategory": "Перекус и фастфуд"}},
    {{"label": "🏠 Еда домой", "category": "Еда и продукты", "subcategory": "Супермаркет и рынок"}}
  ]
}}
"""


async def parse_and_analyze(user_text: str = "", user_name: str = "Пользователь", history: list = None,
                             chat_history: list = None, shopping_list: list = None, limits: dict = None,
                             reminders: list = None, trips: list = None, subscriptions: list = None,
                             installments: list = None, **kwargs) -> dict:
    text_to_parse = user_text or kwargs.get("user_comment") or kwargs.get("text") or ""
    now = now_astana()
    today_prefix = now.strftime("%Y-%m-%d")

    time_hint = get_time_context_hint(now.hour, now.weekday())

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
    messages.append({"role": "system", "content": f"[АРХИВ ПРЕДЫДУЩИХ ОПЕРАЦИЙ (ДЛЯ ИСТОРИИ)]:\n{json.dumps(past_txs[-40:], ensure_ascii=False)}"})

    if limits:
        messages.append({"role": "system", "content": f"[ТЕКУЩИЕ ЛИМИТЫ]:\n{json.dumps(limits, ensure_ascii=False)}"})
    if reminders:
        messages.append({"role": "system", "content": f"[АКТИВНЫЕ НАПОМИНАНИЯ]:\n{json.dumps(reminders, ensure_ascii=False)}"})
    if shopping_list:
        messages.append({"role": "system", "content": f"[СПИСОК ПОКУПОК]:\n{json.dumps(shopping_list, ensure_ascii=False)}"})
    if trips:
        messages.append({"role": "system", "content": f"[ПОЕЗДКИ]:\n{json.dumps(trips, ensure_ascii=False)}"})
    if subscriptions:
        messages.append({"role": "system", "content": f"[ПОДПИСКИ]:\n{json.dumps(subscriptions, ensure_ascii=False)}"})
    if installments:
        messages.append({"role": "system", "content": f"[РАССРОЧКИ]:\n{json.dumps(installments, ensure_ascii=False)}"})
    if chat_history:
        chat_context = "[ИСТОРИЯ ЧАТА]:\n" + "\n".join(f"{m['sender']}: {m['text']}" for m in chat_history[-20:])
        messages.append({"role": "system", "content": chat_context})

    messages.append({"role": "user", "content": f"{user_name}: {text_to_parse}"})

    try:
        response = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=messages,
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except Exception as error:
        print(f"[DeepSeek] Ошибка запроса: {error}")
        return {"intent": "chat", "reply": "Сбой связи. Попробуй ещё раз."}


parse_transaction_with_deepseek = parse_and_analyze
