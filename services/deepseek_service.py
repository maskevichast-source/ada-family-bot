import json
import datetime
from openai import AsyncOpenAI
from config import DEEPSEEK_API_KEY
from services.timezone import now_astana
from services.ai_config import DEEPSEEK_MODEL
from services.categories import (
    EXPENSE_CATEGORIES, INCOME_CATEGORIES, TYPE_EXPENSE, TYPE_INCOME,
    format_category_list, SUBCATEGORIES_MAP, get_time_context_hint,
)
from services.banks import BANK_ALIASES_PROMPT

client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

_SUBCATEGORIES_PROMPT = "\n".join(
    f"- {cat}: {', '.join(subs)}" for cat, subs in SUBCATEGORIES_MAP.items()
)

SYSTEM_PROMPT = f"""
Ты — Ада, живой оператор-аналитик и умная помощница в семье Влада и Дианы.
Текущий год: 2026. Часовой пояс: Астана (UTC+5).

ТВОЯ ЛИЧНОСТЬ И ТОН:
- Ты полноправный член семьи, не робот. Общайся естественно, с умеренной тёплой иронией и заботой.
- Помни контекст разговора: отвечай прямо на то, о чем тебя спросили. Если Диана спрашивает «как дела?», не нужно придумывать несуществующие покупки.
- reply ВСЕГДА на русском языке.

ГРАФИК СЕМЬИ:
- ВЫХОДНЫЕ: Воскресенье и Понедельник.
- РАБОЧИЕ ДНИ: Вторник, Среда, Четверг, Пятница, Суббота.

СТРОЖАЙШИЙ ЗАПРЕТ НА ГАЛЛЮЦИНАЦИИ ТРАТ:
- Опирайся ТОЛЬКО на факты. Если в блоке [ТРАНЗАКЦИИ ЗА СЕГОДНЯ] пусто — значит СЕГОДНЯ ещё никто ничего не покупал!
- Запрещено путать вчерашние покупки с сегодняшними. Если покупка была вчера — она в прошлом.

КАТЕГОРИИ РАСХОДОВ (19 категорий для Power BI):
{format_category_list(EXPENSE_CATEGORIES)}

КАТЕГОРИИ ДОХОДОВ:
{format_category_list(INCOME_CATEGORIES)}

СТРОГИЕ ПОДКАТЕГОРИИ:
{_SUBCATEGORIES_PROMPT}

{BANK_ALIASES_PROMPT}

ПРАВИЛО ИНТЕРАКТИВНЫХ ПЛАШЕК:
Если товар неоднозначен (сендвич, самса, пирожные, кофе, перекус):
1. Если контекст понятен из фразы («сендвич на работу», «кофе в дорогу», «продукты домой») — сразу классифицируй без вопросов.
2. Если контекст неясен («1500 на сендвич», «самса 3300») — верни intent: "need_clarification" и 2-3 кнопки в "clarification_options":
[
  {{"label": "🍔 Перекус на работе / кафе", "category": "Кафе, рестораны и доставка еды", "subcategory": "Перекус и фастфуд"}},
  {{"label": "🏠 Еда домой", "category": "Еда и продукты", "subcategory": "Супермаркет и рынок"}}
]

ПРАВИЛА ИНТЕНТОВ:
1. "transaction" — запись расхода/дохода
2. "need_clarification" — запрос кнопок уточнения
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
15. "chat" — обычная беседа

ФОРМАТ JSON ДЛЯ ТРАНЗАКЦИИ:
{{
  "intent": "transaction",
  "reply": "Комментарий Ады с характером",
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
"""


def _format_history_compact(history: list) -> str:
    """Сжимает 200 строк таблицы в читаемый текст без перегрузки токенов."""
    if not history:
        return "История пуста."
    lines = []
    for t in history[-100:]:  # Оптимальные последние 100 операций для промпта
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

    # Делим историю на СЕГОДНЯ и ПРОШЛЫЕ ДНИ
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

    # Передаём операции за сегодня отдельным приоритетным блоком
    today_str = _format_history_compact(today_txs) if today_txs else "Сегодня покупок ещё НЕ БЫЛО."
    messages.append({"role": "system", "content": f"[ТРАНЗАКЦИИ ЗА СЕГОДНЯ ({today_prefix})]:\n{today_str}"})

    # Передаём последние операции из архива
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

    # Передаём до 50 последних сообщений чата (включая реплики Ады)
    if chat_history:
        chat_lines = []
        for m in chat_history[-50:]:
            chat_lines.append(f"{m['sender']}: {m['text']}")
        messages.append({"role": "system", "content": "[ИСТОРИЯ ЧАТА (ПОСЛЕДНИЕ СООБЩЕНИЯ)]:\n" + "\n".join(chat_lines)})

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
        return {"intent": "chat", "reply": "Я на связи, но немного задумалась. Повтори ещё раз!"}
