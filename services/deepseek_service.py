import json
import datetime
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
    timeout=15.0,
    max_retries=1
)

_SUBCATEGORIES_PROMPT = "\n".join(
    f"- {cat}: {', '.join(subs)}" for cat, subs in SUBCATEGORIES_MAP.items()
)

SYSTEM_PROMPT = f"""
Ты — Ада, оператор-аналитик и умная помощница семьи Влада и Дианы.
Текущий год: 2026. Часовой пояс: Астана (UTC+5).

ГРАФИК СЕМЬИ:
- ВЫХОДНЫЕ: Воскресенье и Понедельник.
- РАБОЧИЕ ДНИ: Вторник, Среда, Четверг, Пятница, Суббота.

ТВОЙ ТОН:
- Живой, дружелюбный, с лёгкой тёплой иронией. reply ВСЕГДА на русском.

СТРОЖАЙШИЙ ЗАПРЕТ НА ГАЛЛЮЦИНАЦИИ ТРАТ:
- Опирайся ТОЛЬКО на факты. Если в блоке [ТРАНЗАКЦИИ ЗА СЕГОДНЯ] пусто — значит СЕГОДНЯ ещё никто ничего не покупал!

КАТЕГОРИИ РАСХОДОВ (19 категорий):
{format_category_list(EXPENSE_CATEGORIES)}

КАТЕГОРИИ ДОХОДОВ:
{format_category_list(INCOME_CATEGORIES)}

СТРОГИЕ ПОДКАТЕГОРИИ:
{_SUBCATEGORIES_PROMPT}

{BANK_ALIASES_PROMPT}

ПРАВИЛО ПОГОДЫ:
Если пользователь спрашивает о погоде:
- "какая погода", "погода сейчас" -> intent: "get_weather", "weather_target": "today"
- "погода на завтра", "завтра нужен зонт" -> intent: "get_weather", "weather_target": "tomorrow"
- "погода на послезавтра" -> intent: "get_weather", "weather_target": "after_tomorrow"
- "погода на неделю", "прогноз на 5 дней", "на выходные" -> intent: "get_weather", "weather_target": "week"

УНИВЕРСАЛЬНОЕ ПРАВИЛО СОМНЕНИЙ И КНОПОК:
Если категория покупки неоднозначна:
1. Верни intent: "need_clarification".
2. В поле "clarification_options" передай от 2 до 4 понятных вариантов с эмодзи.
3. В поле "reply" задай короткий вопрос: «Куда запишем покупку?»

ПРАВИЛА ИНТЕНТОВ:
"transaction", "need_clarification", "correct_any_record", "split_transaction",
"add_installment", "close_installment", "get_installments", "cancel_subscription",
"get_subscriptions", "add_reminder", "delete_reminder", "get_reminders",
"add_shopping", "clear_shopping", "get_shopping", "add_trip", "get_trips",
"get_limits", "generate_limits", "get_summary", "get_income", "get_weather",
"delete_transaction", "chat".
"""


def _clean_json_content(content: str) -> dict:
    """Надёжное извлечение JSON без падения от markdown-тегов ```json."""
    if not isinstance(content, str):
        return {}
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        cleaned = cleaned.removesuffix("```").strip()
    return json.loads(cleaned)


def _format_history_compact(history: list) -> str:
    if not history:
        return "История пуста."
    lines = []
    for t in history[-60:]:
        date_short = str(t.get("date", ""))[:16]
        u = t.get("user", "")
        tp = t.get("type", "РАСХОД")
        amt = t.get("amt", 0)
        cat = t.get("cat", "")
        sub = t.get("subcat", "")
        comm = t.get("comm", "")
        lines.append(f"{date_short} | {u} | {tp} {amt} тг | {cat} ({sub}) | {comm}")
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

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.append({"role": "system", "content": f"ТЕКУЩЕЕ ВРЕМЯ В АСТАНЕ: {now.strftime('%Y-%m-%d %H:%M:%S (%A)')}"})
    messages.append({"role": "system", "content": f"КОНТЕКСТ ДНЯ: {time_hint}"})

    today_str = _format_history_compact(today_txs) if today_txs else "Сегодня покупок ещё НЕ БЫЛО."
    messages.append({"role": "system", "content": f"[ТРАНЗАКЦИИ ЗА СЕГОДНЯ ({today_prefix})]:\n{today_str}"})
    past_str = _format_history_compact(past_txs[-30:])
    messages.append({"role": "system", "content": f"[АРХИВ ПРЕДЫДУЩИХ ОПЕРАЦИЙ]:\n{past_str}"})

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
        chat_lines = [f"{m['sender']}: {m['text']}" for m in chat_history[-30:]]
        messages.append({"role": "system", "content": "[ИСТОРИЯ ЧАТА]:\n" + "\n".join(chat_lines)})

    messages.append({"role": "user", "content": f"{user_name}: {text_to_parse}"})

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
        print(f"[DeepSeek] Ошибка или таймаут: {error}")
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