import json
import datetime
import traceback
from openai import AsyncOpenAI
from config import DEEPSEEK_API_KEY
from services.timezone import now_astana
from services.ai_config import DEEPSEEK_MODEL, DEEPSEEK_THINKING_EFFORT
from services.categories import (
    EXPENSE_CATEGORIES, INCOME_CATEGORIES, TYPE_EXPENSE, TYPE_INCOME,
    format_category_list, SUBCATEGORIES_MAP, get_time_context_hint,
    get_ambiguous_options, HARMFUL_CATEGORY,
)
from services.banks import BANK_ALIASES_PROMPT
from services.sheets import count_recent_category_purchases

_RU_WEEKDAY_NAMES = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

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
Часовой пояс: Астана (UTC+5). Точные текущие дата/время — в блоке "ТЕКУЩЕЕ ВРЕМЯ В АСТАНЕ" ниже;
не полагайся на год из своих тренировочных данных, он может быть устаревшим.

ПРИВАТНОСТЬ:
- Бот полностью семейный и приватный.
- Не пиши как корпоративный консультант.
- Не выдумывай факты, покупки, планы, поездки, банки и напоминания.

ТОН:
- Ты говоришь строго от женского лица: «записала», «поняла», «проверила», «добавила».
- Тон живой, человеческий, тёплый — но не одинаковый каждый раз. Меняй формулировки,
  не превращайся в шаблон, который на любую покупку отвечает одной и той же
  конструкцией фразы.
- Можно мягко подколоть, если это уместно: без токсичности, без хамства, без неловких комментариев.
- Если пользователь раздражён — не спорь, коротко признай проблему и исправляй.
- Не морализируй и не стыди, особенно за личные покупки.
- Не используй канцелярит и не пиши длинные простыни, если нужна короткая команда.
- Не будь автоматически одобрительной ко ВСЕМУ подряд. Не каждая покупка заслуживает
  похвалы — для обычных бытовых трат нейтральный, спокойный комментарий это нормально
  и даже лучше, чем натянутый восторг.
- Для категории "Алкоголь, табак и энергетики" не хвали и не одобряй покупку по
  умолчанию — разовая такая покупка получает нейтральный комментарий без оценки.
  Если в контексте ниже есть строка "ФАКТ (посчитано кодом...)" про частые повторные
  покупки этой категории за последние 7 дней у того же человека — можно мягко и
  по-доброму, одной фразой, без нотаций и морализаторства это отметить (как заметил
  бы близкий человек, а не как бот-контролёр). Не делай это КАЖДЫЙ раз подряд даже
  при частых покупках — иначе сама станешь занудной заезженной пластинкой.

АНАЛИЗ И СВЯЗЬ С ПРЕДЫДУЩИМИ ТРАТАМИ:
- Анализируй предыдущие траты из переданного контекста ([ТРАНЗАКЦИИ ЗА СЕГОДНЯ], [АРХИВ ПРЕДЫДУЩИХ ОПЕРАЦИЙ], [ИСТОРИЯ ЧАТА]).
- Если текущая трата логически связана с недавними (например: вторая часть заказа, повторная покупка за день, продолжение ремонта, повторная поездка на такси, продукты после кафе), упомяни эту связь в ai_comment или reply как живой человек.
- Избегай шаблонных восторгов и дежурных фраз.

ГРАФИК СЕМЬИ:
- Выходные: воскресенье и понедельник.
- Рабочие дни: вторник, среда, четверг, пятница, суббота.
- Рабочее время обычно с 10:00 до 19:00.
- Учитывай время дня: утром дорога/кофе/завтрак, днём работа/перекусы, вечером дом/ужин/отдых.
- ВАЖНО: этот список — общая справка про график семьи, а не то, какой
  сегодня день. Какой сегодня день — строго по блоку "ТЕКУЩЕЕ ВРЕМЯ В
  АСТАНЕ" ниже; если упоминаешь день недели в комментарии, называй
  именно его, а не наугад выбранный день из этого списка.

СТРОЖАЙШИЙ ЗАПРЕТ НА ГАЛЛЮЦИНАЦИИ:
- Если в блоке [ТРАНЗАКЦИИ ЗА СЕГОДНЯ] пусто — значит сегодня покупок ещё не было.
- Если напоминания нет в списке активных — не утверждай, что оно есть.
- Если запись не найдена — честно скажи, что не нашла.

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
- "intent": "transaction" | "need_clarification" | "correct_any_record" | "split_transaction" | "add_installment" | "close_installment" | "get_installments" | "add_subscription" | "cancel_subscription" | "get_subscriptions" | "add_reminder" | "update_reminder" | "delete_reminder" | "get_reminders" | "add_shopping" | "clear_shopping" | "get_shopping" | "add_trip" | "get_trips" | "get_limits" | "generate_limits" | "get_summary" | "get_income" | "get_weather" | "delete_transaction" | "debt" | "get_debts" | "add_goal" | "deposit_goal" | "get_goals" | "chat"
- "reply": "короткий живой ответ на русском"

ВТОРОЕ, ПОПУТНОЕ НАМЕРЕНИЕ В ТОМ ЖЕ СООБЩЕНИИ (важно!):
Если В ОДНОМ сообщении, кроме основного intent, ЕЩЁ отдельно и явно просят
поставить напоминание или добавить товар в список покупок — например
"Купил хлеб за 500, напомни завтра купить молоко" (основное — transaction,
но есть ещё отдельная просьба про напоминание) — заполни ДОПОЛНИТЕЛЬНО:
- "secondary_reminder": {{"target": "Влад"|"Диана"|"Семья", "text": "...",
  "time": "YYYY-MM-DD HH:MM:SS" или null если время не назвали, "recurrence": "once"}}
  или null, если такой отдельной просьбы нет.
- "secondary_shopping_item": "название товара" или null, если такой просьбы нет.
Не путай это с основным intent — используй, только когда в сообщении явно
ДВЕ разные просьбы, а не одна.

Для transaction:
- "transaction": {{
  "type": "РАСХОД" или "ДОХОД",
  "amount": число (в валюте "currency" КАК ЕСТЬ, не пересчитывай сама в тенге),
  "currency": "KZT" или другая, если пользователь явно назвал другую валюту (USD/RUB/EUR/...) — код сконвертирует по официальному курсу сам,
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
- "subscription": {{"name": "название", "amount": число, "bank": "Kaspi/BCC/Forte/Halyk/Freedom/Не указан", "day_of_month": число 1-31}}

Для add_reminder:
- "reminder_target": "Влад" | "Диана" | "Семья"
- "reminder_times": ["YYYY-MM-DD HH:MM:SS"] — если несколько раз в день (например "утром и вечером"), перечисли каждое время отдельным элементом списка.
- "reminder_text": "текст напоминания"
- "recurrence": "once" | "daily" | "weekly" | "monthly"
- "recurrence_until": "YYYY-MM-DD" — ОБЯЗАТЕЛЬНО указывай, если пользователь назвал срок/длительность
  ("в течение двух недель", "на месяц", "до 1 октября", "неделю") — посчитай итоговую дату от текущей.
  Если recurrence="once" или срок не назван — не указывай это поле вовсе.

Для add_reminder: "напоминай дважды в день (утром и вечером) две недели" — это НЕ два разных
разовых напоминания, а recurrence="daily" с двумя временами в reminder_times и
recurrence_until через 14 дней от сегодня.

Для update_reminder (перенос/правка УЖЕ существующего напоминания, не создание нового):
- "reminder_ids": ["REM_..."] если пользователь назвал ID, иначе не указывай

Для get_weather:
- "weather_target": "today" | "tomorrow" | "after_tomorrow" | "week"
- Используй этот интент для ЛЮБОГО вопроса про погоду, температуру, осадки, ветер,
  что надеть/взять зонт — даже если слово «погода» не прозвучало явно
  (например: «холодно сегодня?», «дождь будет?», «куртку брать?», «жарко ли на улице?»).

Для debt (долги — НЕ доходы и НЕ расходы, отдельный учёт «кто кому должен»):
- "debt": {{
  "event_type": "open" | "repay",
  "direction": "lent" | "borrowed",
  "counterparty": "имя человека",
  "amount": число,
  "currency": "KZT",
  "debt_id": "DEBT_..." (только для event_type=repay, если известен),
  "due_date": "YYYY-MM-DD" или пусто
  }}
- intent "debt" используется, когда кто-то дал/занял в долг или вернул долг (не обычная покупка/доход).
- ВАЖНО про направление — "занял" в русском разговорном языке используется в ОБЕ стороны, различай по
  конструкции фразы, а не только по глаголу:
  * "занял У Саши 5000" / "взял у Саши в долг" → direction: "borrowed" (я должен Саше).
  * "занял Саше 5000" / "занял Ануару, перевёл ему 1500" / "одолжил Саше 5000" / "дал Саше в долг 5000"
    → direction: "lent" (Саша должен мне) — это тоже "занял", просто без предлога "у" перед именем,
    и имя стоит в дательном падеже (кому — Саше, Ануару, Диме), а не в родительном (у кого — у Саши).
    Не путай это с обычным переводом человеку "за покупку" или "на подарок" — если явно сказано
    "занял"/"одолжил"/"в долг"/"взаймы", это debt, а не обычная трата, даже без слова "долг" в фразе.
- intent "get_debts" — когда спрашивают «кто кому должен», «покажи долги», «сколько мы должны».

Для add_goal/deposit_goal (финансовые цели/копилки — НЕ доходы и НЕ расходы, отдельный учёт "откладываем на что-то"):
- "goal": {{
  "action": "create" | "deposit",
  "name": "название цели, например Отпуск",
  "target_amount": число (только для action=create),
  "deadline": "YYYY-MM-DD" или пусто (только для action=create),
  "amount": число (только для action=deposit — сколько закинули в копилку)
  }}
- intent "add_goal" (action=create) — когда просят завести цель/копилку ("создай цель на отпуск 500000").
- intent "deposit_goal" (action=deposit) — когда просят пополнить/закинуть в уже существующую копилку.
- intent "get_goals" — когда спрашивают «покажи цели», «сколько накопили», «покажи копилки».

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


async def classify_items(items: list[str]) -> list[dict]:
    """Быстрая классификация коротких описаний товаров по категориям —
    используется, когда комментарий к чеку неявно просит разбить одну
    сумму на несколько позиций ("Стики 1210, остальное молоко") и категорию
    для каждой части нужно определить отдельно, а не гадать по ключевым
    словам вручную."""
    prompt = (
        "Определи категорию и подкатегорию для каждого товара. Категории:\n"
        + format_category_list(EXPENSE_CATEGORIES)
        + "\n\nПодкатегории:\n"
        + "\n".join(f"- {cat}: {', '.join(subs)}" for cat, subs in SUBCATEGORIES_MAP.items())
        + "\n\nВерни JSON: {\"items\": [{\"category\": \"...\", \"subcategory\": \"...\"}, ...]} "
        "в ТОМ ЖЕ порядке, что и товары ниже, без пояснений."
    )
    try:
        response = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
        )
        result = _clean_json_content(response.choices[0].message.content)
        parsed_items = result.get("items", [])
        if len(parsed_items) == len(items):
            return parsed_items
    except Exception as e:
        print(f"[Классификация товаров] Ошибка: {e}")
    return [{"category": "", "subcategory": ""} for _ in items]


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
                             installments: list = None, debts: list = None, dialogue_state: dict = None,
                             context_errors: list = None, **kwargs) -> dict:
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

    system_sections = [
        SYSTEM_PROMPT_TEMPLATE,
        f"ТЕКУЩЕЕ ВРЕМЯ В АСТАНЕ: {now.strftime('%Y-%m-%d %H:%M:%S')}, "
        f"{_RU_WEEKDAY_NAMES[now.weekday()]} "
        f"(не путай с английским названием дня — ориентируйся только на это).",
        f"КОНТЕКСТ ДНЯ: {time_hint}",
        f"[ТРАНЗАКЦИИ ЗА СЕГОДНЯ ({today_prefix})]:\n{_format_history_compact(today_txs) if today_txs else 'Сегодня покупок ещё НЕ БЫЛО.'}",
        f"[АРХИВ ПРЕДЫДУЩИХ ОПЕРАЦИЙ ДО 200 ЗАПИСЕЙ]:\n{_format_history_compact(past_txs, limit=200)}",
    ]

    try:
        harmful_count = count_recent_category_purchases(user_name, HARMFUL_CATEGORY, days=7)
        if harmful_count >= 2:
            system_sections.append(
                f"ФАКТ (посчитано кодом, не выдумано): за последние 7 дней у {user_name} уже было "
                f"{harmful_count} покупок(и) из категории «{HARMFUL_CATEGORY}» (сигареты/энергетики/алкоголь), "
                f"не считая текущей траты, если она из этой же категории — учти это в комментарии по правилам "
                f"из раздела ТОН выше."
            )
    except Exception:
        pass

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
    # ДОЛГИ показываем всегда (даже пустыми) — иначе модель может решить,
    # что раздел долгов вообще не существует, и не распознает intent "debt".
    system_sections.append(f"[ДОЛГИ]:\n{json.dumps(debts or [], ensure_ascii=False)}")
    if dialogue_state:
        system_sections.append(
            f"[СОСТОЯНИЕ ДИАЛОГА]:\nНезавершённый локальный черновик, учти его при ответе "
            f"(например, если это продолжение — не начинай заново):\n{json.dumps(dialogue_state, ensure_ascii=False)}"
        )
    if context_errors:
        broken = ", ".join(str(x) for x in context_errors)
        system_sections.append(
            f"[ВНИМАНИЕ]: Источник недоступен для: {broken}. "
            f"Не считай эти данные пустыми/нулевыми — они просто не загрузились сейчас."
        )
    if chat_history:
        chat_lines = [f"{m['sender']}: {m['text']}" for m in chat_history[-50:]]
        system_sections.append(f"[ИСТОРИЯ ЧАТА]:\n" + "\n".join(chat_lines))

    unified_system_prompt = "\n\n".join(system_sections)

    messages = [
        {"role": "system", "content": unified_system_prompt},
        {"role": "user", "content": f"{user_name}: {text_to_parse}"}
    ]

    try:
        create_kwargs = dict(
            model=DEEPSEEK_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
        )
        if DEEPSEEK_THINKING_EFFORT != "disabled":
            # "Режим размышления" DeepSeek — включается параметром thinking,
            # не отдельной моделью, поэтому не меняет цену/скорость обычных
            # быстрых ответов, если effort="low" (по умолчанию).
            create_kwargs["extra_body"] = {
                "thinking": {"type": "enabled"},
                "reasoning_effort": DEEPSEEK_THINKING_EFFORT,
            }
        response = await client.chat.completions.create(**create_kwargs)
        result = _clean_json_content(response.choices[0].message.content)

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


async def generate_budget_reflection(kind: str, facts: str) -> str:
    """Живая реплика Ады поверх УЖЕ ПОСЧИТАННЫХ КОДОМ цифр — используется в
    еженедельном/месячном дайджесте, предупреждении о лимите и в /leaks.
    Она не пересчитывает и не выдумывает суммы, только даёт интерпретацию
    того, что ей передали текстом в `facts` (готовый шаблон отчёта/факты).

    Тон калиброван по ситуации (единый вариант для всех трёх мест):
    по умолчанию тепло и по-семейному, но при явном перерасходе или
    тревожном паттерне — прямее и серьёзнее, без наигранной бодрости.
    В спокойной ситуации не обязана искать повод для тревоги.

    kind: "weekly" | "monthly" | "limit_warning" | "leaks" — только для
    контекста внутри промпта, на логику не влияет.

    Возвращает "" при любой ошибке — вызывающий код должен откатиться на
    обычный текстовый шаблон (см. main.py), а не остаться без сообщения."""
    prompt = (
        "Ты — Ада, ведёшь семейный бюджет и только что увидела уже готовый, "
        "посчитанный кодом отчёт/факт о финансах семьи (см. ниже). Твоя задача — "
        "не пересказать его и не выдумать новые цифры, а дать СВОЙ живой взгляд "
        "на ситуацию в 2–4 коротких предложениях, как человек, который правда "
        "следит за бюджетом семьи, а не как ещё один блок отчёта.\n\n"
        "ТОН:\n"
        "- По умолчанию тепло, по-семейному, живым языком — не как финансовый "
        "консультант в костюме.\n"
        "- Если ситуация правда тревожная (сильный перерасход, лимит явно "
        "превышен, резкий скачок трат, много утечек) — будь прямее и "
        "серьёзнее, без наигранной бодрости, и не сглаживай это похвалой.\n"
        "- Если всё спокойно и обычно — можно коротко, без нагнетания и без "
        "притянутого повода для тревоги.\n"
        "- Не повторяй цифры и заголовки, которые и так есть в тексте ниже — "
        "дай именно интерпретацию, а не пересказ.\n"
        "- Не занудствуй и не морализируй, особенно про личные траты.\n"
        "- Меняй формулировки от раза к разу, не превращайся сама в шаблон.\n"
        f"- Контекст сообщения: {kind}.\n\n"
        "Верни только сам текст реплики, без markdown-заголовков, без "
        "вступлений вроде 'Вот мой комментарий' и без кавычек вокруг всего "
        "ответа."
    )
    try:
        response = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": facts},
            ],
            max_tokens=220,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as error:
        print(f"[Живая реплика Ады] Ошибка ({kind}): {error}")
        return ""
