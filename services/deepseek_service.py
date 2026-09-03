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

client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

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

УНИВЕРСАЛЬНОЕ ПРАВИЛО СОМНЕНИЙ И КНОПОК:
Если категория или назначение покупки НЕ ОЧЕВИДНЫ из текста (например, «кроссовки» — спорт или обувь? «массаж» — лечение или спа? «яндекс» — такси или доставка? «сендвич» — перекус или домой?):
1. НЕ УГАДЫВАЙ НАУГАД.
2. Верни intent: "need_clarification".
3. В поле "clarification_options" передай от 2 до 4 понятных вариантов с эмодзи:
   [
     {{"label": "👟 Повседневная обувь", "category": "Одежда и обувь", "subcategory": "Обувь"}},
     {{"label": "🏃 Спортивная для тренировок", "category": "Спорт и фитнес", "subcategory": "Спортинвентарь"}}
   ]
4. В поле "reply" задай короткий вопрос: «Уточни, куда записать покупку?»

ПРАВИЛА ИНТЕНТОВ:
1. "transaction" — однозначная запись расхода/дохода
2. "need_clarification" — требуется выбор из 2-4 кнопок
3. "correct_any_record" — исправление/удаление строки в таблице
4. "split_transaction" — разделить трату
5. "add_installment", "close_installment", "get_installments" — рассрочки
6. "cancel_subscription", "get_subscriptions" — подписки
7. "add_reminder", "delete_reminder", "get_reminders" — напоминания
8. "add_shopping", "clear_shopping", "get_shopping" — список покупок
9. "add_trip", "get_trips" — поездки
10. "get_limits", "generate_limits" — лимиты
11. "get_summary" — сводка за месяц
12. "get_income" — доходы
13. "get_weather" — погода
14. "delete_transaction" — удаление операции
15. "chat" — разговорная беседа
"""


def _format_history_compact(history: list) -> str:
    if not history:
        return "История пуста."
    lines = []
    for t in history[-80:]:
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
    past_str = _format_history_compact(past_txs[-40:])
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
        chat_lines = [f"{m['sender']}: {m['text']}" for m in chat_history[-50:]]
        messages.append({"role": "system", "content": "[ИСТОРИЯ ЧАТА]:\n" + "\n".join(chat_lines)})

    messages.append({"role": "user", "content": f"{user_name}: {text_to_parse}"})

    try:
        response = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=messages,
            response_format={"type": "json_object"}
        )
        result = json.loads(response.choices[0].message.content)

        # ── ПЕРЕХВАТ 1: Проверка по матрице частых триггеров ──
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
                result["reply"] = "Уточни, куда отнести эту покупку:"

        # ── ПЕРЕХВАТ 2: Если DeepSeek сам вернул альтернативы из промпта ──
        tx = result.get("transaction") or {}
        alts = tx.get("alternatives") or result.get("alternatives") or []
        if alts and not result.get("clarification_options"):
            options = []
            for a in alts[:4]:
                options.append({"label": f"📌 {a}", "category": a, "subcategory": ""})
            result["intent"] = "need_clarification"
            result["clarification_options"] = options
            if not result.get("reply"):
                result["reply"] = "Уточни категорию покупки:"

        return result
    except Exception as error:
        print(f"[DeepSeek] Ошибка запроса: {error}")
        return {"intent": "chat", "reply": "Я на связи, но немного задумалась. Повтори ещё раз!"}
