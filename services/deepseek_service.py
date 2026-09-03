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
Ты — Ада, женщина, оператор-аналитик и умная помощница семьи Влада и Дианы.
Текущий год: 2026. Часовой пояс: Астана (UTC+5).

ТВОЙ ТОН И ГРАММАТИКА:
- Ты говоришь СТРОГО от ЖЕНСКОГО лица: «удалила», «записала», «нашла», «посмотрела», «поняла». Запрещено говорить в мужском роде («удалил», «записал»)!
- Живой, дружелюбный тон с лёгкой иронией. reply ВСЕГДА на русском.

ГРАФИК СЕМЬИ:
- ВЫХОДНЫЕ: Воскресенье и Понедельник.
- РАБОЧИЕ ДНИ: Вторник, Среда, Четверг, Пятница, Суббота.

СТРОЖАЙШИЙ ЗАПРЕТ НА ГАЛЛЮЦИНАЦИИ ТРАТ:
- Опирайся ТОЛЬКО на факты. Если в блоке [ТРАНЗАКЦИИ ЗА СЕГОДНЯ] пусто — значит СЕГОДНЯ ещё никто ничего не покупал!

КАТЕГОРИИ РАСХОДОВ (19 категорий):
{format_category_list(EXPENSE_CATEGORIES)}

КАТЕГОРИИ ДОХОДОВ:
{format_category_list(INCOME_CATEGORIES)}

СТРОГИЕ ПОДКАТЕГОРИИ:
{_SUBCATEGORIES_PROMPT}

{BANK_ALIASES_PROMPT}

ФОРМАТ ОТВЕТА:
Ты ОБЯЗАНА отвечать ТОЛЬКО валидным JSON-объектом с полями:
- "intent": "transaction" | "need_clarification" | "correct_any_record" | "split_transaction" | "add_installment" | "close_installment" | "get_installments" | "cancel_subscription" | "get_subscriptions" | "add_reminder" | "delete_reminder" | "get_reminders" | "add_shopping" | "clear_shopping" | "get_shopping" | "add_trip" | "get_trips" | "get_limits" | "generate_limits" | "get_summary" | "get_income" | "get_weather" | "delete_transaction" | "chat"
- "reply": "Твой ответ пользователю от женского лица"
- "weather_target": "today" | "tomorrow" | "after_tomorrow" | "week" (если intent="get_weather")
- "transaction": объект транзакции (если intent="transaction" или "need_clarification")
- "clarification_options": список от 2 до 4 вариантов (если intent="need_clarification")
- "updates": список изменений (если intent="correct_any_record")
- "search_query": строка поиска (если intent="delete_transaction")
"""


def _clean_json_content(content: str) -> dict:
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

    # ── Единое системное сообщение для DeepSeek ──
    system_sections = [
        SYSTEM_PROMPT_TEMPLATE,
        f"ТЕКУЩЕЕ ВРЕМЯ В АСТАНЕ: {now.strftime('%Y-%m-%d %H:%M:%S (%A)')}",
        f"КОНТЕКСТ ДНЯ: {time_hint}",
        f"[ТРАНЗАКЦИИ ЗА СЕГОДНЯ ({today_prefix})]:\n{_format_history_compact(today_txs) if today_txs else 'Сегодня покупок ещё НЕ БЫЛО.'}",
        f"[АРХИВ ПРЕДЫДУЩИХ ОПЕРАЦИЙ]:\n{_format_history_compact(past_txs[-30:])}",
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
        chat_lines = [f"{m['sender']}: {m['text']}" for m in chat_history[-25:]]
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