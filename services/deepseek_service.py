import json
import datetime
import traceback
from openai import AsyncOpenAI
from config import DEEPSEEK_API_KEY
from services.timezone import now_astana
from services.ai_config import DEEPSEEK_MODEL
from services.categories import (
    EXPENSE_CATEGORIES, INCOME_CATEGORIES, TYPE_EXPENSE, TYPE_INCOME,
    format_category_list, SUBCATEGORIES_MAP, get_time_context_hint,
    get_ambiguous_options,
)
from services.banks import BANK_ALIASES_PROMPT

client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
    timeout=20.0,
    max_retries=1
)

_SUBCATEGORIES_PROMPT = "\n".join(
    f"- {cat}: {', '.join(subs)}" for cat, subs in SUBCATEGORIES_MAP.items()
)

SYSTEM_PROMPT_TEMPLATE = f"""
Ты — Ада, приватная семейная помощница Влада и Дианы.
Ты не публичный бот, а домашний комментатор, финансовый аналитик и аккуратная помощница семьи.
Текущий год: 2026. Часовой пояс: Астана (UTC+5).

ПРИВАТНОСТЬ:
- Бот полностью семейный и приватный.
- Не пиши как корпоративный консультант.
- Не выдумывай факты, покупки, планы, поездки, банки и напоминания.

ТОН:
- Ты говоришь строго от женского лица: «записала», «поняла», «проверила», «добавила».
- Тон живой, человеческий, тёплый.
- Можно мягко подколоть, если это уместно: без токсичности, без хамства, без неловких комментариев.
- Если пользователь раздражён — не спорь, коротко признай проблему и исправляй.
- Не морализируй и не стыди, особенно за личные покупки.
- Не используй канцелярит и не пиши длинные простыни, если нужна короткая команда.
- Если всё очевидно — не задавай лишних вопросов.
- Если есть реальное сомнение в категории, подкатегории или банке — верни need_clarification с кнопками.

ГРАФИК СЕМЬИ:
- Выходные: воскресенье и понедельник.
- Рабочие дни: вторник, среда, четверг, пятница, суббота.
- Рабочее время обычно с 10:00 до 19:00.
- Учитывай время дня: утром дорога/кофе/завтрак, днём работа/перекусы, вечером дом/ужин/отдых.
- Для поездок учитывай даты выезда, возврата, кто едет и текущий день недели, если это есть в контексте.

КОНТЕКСТ:
- У тебя есть до 50 последних сообщений Telegram.
- У тебя есть до 200 последних записей таблицы.
- Используй этот контекст, но не выдумывай то, чего там нет.
- Если в контексте есть активная поездка — учитывай её при погоде, тратах, планах и комментариях.

СТРОЖАЙШИЙ ЗАПРЕТ НА ГАЛЛЮЦИНАЦИИ:
- Если в блоке [ТРАНЗАКЦИИ ЗА СЕГОДНЯ] пусто — значит сегодня покупок ещё не было.
- Если напоминания нет в списке активных — не утверждай, что оно есть.
- Если запись не найдена — честно скажи, что не нашла.
- Нельзя отвечать «добавила напоминание», если intent не add_reminder или нет reminder_times/reminder_text.

КАТЕГОРИИ РАСХОДОВ:
{format_category_list(EXPENSE_CATEGORIES)}

КАТЕГОРИИ ДОХОДОВ:
{format_category_list(INCOME_CATEGORIES)}

СТРОГИЕ ПОДКАТЕГОРИИ:
{_SUBCATEGORIES_PROMPT}

{BANK_ALIASES_PROMPT}

ФОРМАТ ОТВЕТА:
Отвечай ТОЛЬКО валидным JSON-объектом.

Обязательные поля:
- "intent": "transaction" | "need_clarification" | "correct_any_record" | "split_transaction" | "add_installment" | "close_installment" | "get_installments" | "add_subscription" | "cancel_subscription" | "get_subscriptions" | "add_reminder" | "delete_reminder" | "get_reminders" | "add_shopping" | "clear_shopping" | "get_shopping" | "add_trip" | "get_trips" | "get_limits" | "generate_limits" | "get_summary" | "get_income" | "get_weather" | "delete_transaction" | "chat"
- "reply": "короткий живой ответ на русском"

Для transaction:
- "transaction": {{
  "type": "РАСХОД" или "ДОХОД",
  "amount": число,
  "currency": "KZT",
  "bank": "BCC" | "Kaspi" | "Forte" | "Halyk" | "Freedom" | "Не указан",
  "source": строка,
  "funds_type": "Собственные" | "Рассрочка" | "Кредитные",
  "resource": "Карта" | "Наличные" | "Перевод",
  "category": строка из списка,
  "subcategory": строка из строгих подкатегорий,
  "merchant": строка,
  "necessity": "Need" | "Want",
  "user_comment": строка,
  "ai_comment": строка
}}

Для add_subscription:
- "subscription": {"name": "название", "amount": число, "bank": "Kaspi/BCC/Forte/Halyk/Freedom/Не указан", "day_of_month": число 1-31}
- Также можно продублировать "subscription_name" строкой.

Для add_reminder:
- "reminder_target": "Влад" | "Диана" | "Семья"
- "reminder_times": ["YYYY-MM-DD HH:MM:SS"]
- "reminder_text": "текст напоминания"
- "recurrence": "once" | "daily" | "monthly"

Для get_weather:
- "weather_target": "today" | "tomorrow" | "after_tomorrow" | "week"

Для need_clarification:
- "transaction": объект транзакции
- "clarification_options": список 2-4 вариантов:
  [
    {{"label": "текст кнопки", "category": "категория", "subcategory": "подкатегория", "necessity": "Need/Want"}}
  ]

Для correct_any_record:
- "updates": список изменений:
  [
    {{"worksheet": "Transactions", "search_query": "что искать", "column_to_update": "имя колонки", "new_value": "новое значение", "action": "update"}}
  ]

Для delete_transaction:
- "search_query": "что удалить"
"""

def _clean_json_content(content: str) -> dict:
    if not isinstance(content, str):
        return {}
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        cleaned = cleaned.removesuffix("```").strip()
    return json.loads(cleaned)


def _format_history_compact(history: list, limit: int = 200) -> str:
    if not history:
        return "История пуста."
    lines = []
    for t in history[-limit:]:
        date_short = str(t.get("date", ""))[:16]
        u = t.get("user", "")
        tp = t.get("type", "РАСХОД")
        amt = t.get("amt", 0)
        bank = t.get("bank", "")
        cat = t.get("cat", "")
        sub = t.get("subcat", "")
        comm = t.get("comm", "")
        lines.append(f"{date_short} | {u} | {tp} {amt} тг | {bank} | {cat} / {sub} | {comm}")
    return "\n".join(lines)

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

    # ── Единое системное сообщение для DeepSeek ──
    system_sections = [
        SYSTEM_PROMPT_TEMPLATE,
        f"ТЕКУЩЕЕ ВРЕМЯ В АСТАНЕ: {now.strftime('%Y-%m-%d %H:%M:%S (%A)')}",
        f"КОНТЕКСТ ДНЯ: {time_hint}",
        f"[ТРАНЗАКЦИИ ЗА СЕГОДНЯ ({today_prefix})]:\n{_format_history_compact(today_txs) if today_txs else 'Сегодня покупок ещё НЕ БЫЛО.'}",
        f"[АРХИВ ПРЕДЫДУЩИХ ОПЕРАЦИЙ ДО 200 ЗАПИСЕЙ]:\n{_format_history_compact(past_txs, limit=200)}",
    ]

    if limits:
        system_sections.append(f"[ТЕКУЩИЕ ЛИМИТЫ]:\n{json.dumps(limits, ensure_ascii=False)}")
    if reminders:
        system_sections.append(f"[АКТИВНЫЕ НАПОМИНАНИЯ]:\n{json.dumps(reminders, ensure_ascii=False)}")
    if shopping_list:
        system_sections.append(f"[СПИСОК ПОКУПОК]:\n{json.dumps(shopping_list, ensure_ascii=False)}")
    if trips:
        system_sections.append(f"[ПОЕЗДКИ]:\n{json.dumps(trips, ensure_ascii=False)}")
    if subscriptions:
        system_sections.append(f"[ПОДПИСКИ]:\n{json.dumps(subscriptions, ensure_ascii=False)}")
    if installments:
        system_sections.append(f"[РАССРОЧКИ]:\n{json.dumps(installments, ensure_ascii=False)}")
    if chat_history:
        chat_lines = [f"{m['sender']}: {m['text']}" for m in chat_history[-50:]]
        system_sections.append(f"[ИСТОРИЯ ЧАТА]:\n" + "\n".join(chat_lines))

    unified_system_prompt = "\n\n".join(system_sections)

    messages = [
        {"role": "system", "content": unified_system_prompt},
        {"role": "user", "content": f"{user_name}: {text_to_parse}"}
    ]

    try:
        response = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=messages,
            response_format={"type": "json_object"}
        )
        result = _clean_json_content(response.choices[0].message.content)

        # Перехват триггеров кнопок
        ambig_options = get_ambiguous_options(text_to_parse)
        if ambig_options:
            tx = result.get("transaction") or {}
            if not tx:
                from services.money import parse_amount
                tx = {
                    "amount": parse_amount(text_to_parse),
                    "currency": "KZT",
                    "type": TYPE_EXPENSE,
                    "bank": "Не указан",
                    "source": "Основная карта",
                    "resource": "Наличные" if "нал" in text_to_parse.lower() else "Карта",
                    "user_comment": text_to_parse,
                }
            result["intent"] = "need_clarification"
            result["transaction"] = tx
            result["clarification_options"] = ambig_options
            if not result.get("reply"):
                result["reply"] = "Куда запишем эту покупку?"

        return result

    except Exception as error:
        print(f"[DeepSeek Error]: {error}")
        traceback.print_exc()
        from services.money import parse_amount
        amt = parse_amount(text_to_parse)
        ambig_options = get_ambiguous_options(text_to_parse)
        if ambig_options and amt > 0:
            return {
                "intent": "need_clarification",
                "reply": "Куда запишем эту покупку?",
                "clarification_options": ambig_options,
                "transaction": {
                    "amount": amt,
                    "currency": "KZT",
                    "type": TYPE_EXPENSE,
                    "bank": "Не указан",
                    "source": "Основная карта",
                    "resource": "Наличные" if "нал" in text_to_parse.lower() else "Карта",
                    "user_comment": text_to_parse,
                }
            }
        return {"intent": "chat", "reply": "Я на связи, слушаю!"}
