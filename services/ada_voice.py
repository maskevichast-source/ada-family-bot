"""«Голос» Ады: живая реплика по поводу и ответы в разговоре.

Разделение труда:
  • запись траты и строка «Записано» — код (как и раньше, по факту записи);
  • ПОВОД для реплики определяет КОД по данным таблицы (повтор за неделю, всплеск суммы,
    лимит, серия трат за день, доход…). Нет повода — Ада молчит, модель не вызывается;
  • саму реплику пишет отдельный вызов модели (по умолчанию deepseek-v4-pro без режима
    размышления), которой передаются ПОСЧИТАННЫЕ факты, последние сообщения чата и её
    собственные последние реплики (чтобы не повторяться).
  • в разговоре (intent «chat») тот же голос получает тот же контекст, что и разбор (200 операций,
    50 сообщений, лимиты, напоминания…).

Выключатель: ADA_VOICE=off возвращает прежнее поведение. При любой ошибке модели — тишина
(для траты) или прежний ответ разбора (для разговора), запись никогда не страдает.
"""
import asyncio
import calendar
import datetime
import os
import statistics

from services import state
from services.analytics import _format_currency
from services.categories import HARMFUL_CATEGORY, TYPE_EXPENSE, TYPE_INCOME
from services.timezone import now_astana

VOICE_MODEL = os.environ.get("DEEPSEEK_VOICE_MODEL", "").strip()
COOLDOWN_NS = "voice_cooldown"
COOLDOWN_MINUTES = 10
LIMIT_WARN_RATIO = 0.8
BIG_ABSOLUTE = 50_000
SILENCE = {"", "-", "—", "…", "...", "нет", "молчу", "none", "null"}


def enabled() -> bool:
    return os.environ.get("ADA_VOICE", "on").strip().lower() not in {"off", "0", "false", "нет"}


def _model() -> str:
    from services.ai_config import DEEPSEEK_REASONING_MODEL
    return VOICE_MODEL or DEEPSEEK_REASONING_MODEL


# ───────────────────────── факты и повод (всё считает код) ─────────────────────────

def _parse_dt(value) -> datetime.datetime | None:
    text = str(value or "").strip()
    for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return datetime.datetime.strptime(text[:size], fmt)
        except ValueError:
            continue
    return None


def _num(value) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def build_facts(tx: dict, user: str, history: list, limits: dict | None, now: datetime.datetime | None = None) -> dict:
    """Возвращает {"facts": [строки], "strict": bool}. Пустой "facts" = повода нет.

    history — записи БЕЗ текущей траты (формат get_last_200_transactions)."""
    now = (now or now_astana()).replace(tzinfo=None)
    amount = _num(tx.get("amount"))
    category = str(tx.get("category") or "")
    facts, strict = [], False

    if tx.get("type") == TYPE_INCOME:
        facts.append(f"Поступление: {_format_currency(amount)} ₸ ({category}). Это доход, а не трата.")
        return {"facts": facts, "strict": False}
    if amount <= 0:
        return {"facts": [], "strict": False}

    rows = []
    for t in history or []:
        dt = _parse_dt(t.get("date"))
        if dt and t.get("type") == TYPE_EXPENSE:
            rows.append((dt, t))

    today = [(dt, t) for dt, t in rows if dt.date() == now.date()]
    week_ago = now - datetime.timedelta(days=7)
    same_cat_user_7d = [(dt, t) for dt, t in rows if t.get("cat") == category and t.get("user") == user and dt >= week_ago]
    same_cat_today = [(dt, t) for dt, t in today if t.get("cat") == category and t.get("user") == user]

    # 1. привычка: сигареты / энергетики / алкоголь
    if category == HARMFUL_CATEGORY and len(same_cat_user_7d) >= 2:
        n = len(same_cat_user_7d) + 1
        facts.append(f"Это {n}-я покупка из «вредного» (сигареты/энергетики/алкоголь) у {user} за 7 дней.")
        if n >= 5:
            strict = True

    # 2. необычно крупная сумма
    own = [_num(t.get("amt")) for dt, t in rows
           if t.get("cat") == category and t.get("user") == user and _num(t.get("amt")) > 0
           and dt >= now - datetime.timedelta(days=90)]
    if len(own) >= 5:
        median, top = statistics.median(own), max(own)
        if amount >= median * 3 and amount > top * 1.2:
            facts.append(f"Сумма {_format_currency(amount)} ₸ — в {amount / median:.0f} раз больше обычной "
                         f"({_format_currency(median)} ₸) и больше прежнего максимума ({_format_currency(top)} ₸) в этой категории.")
            strict = strict or amount >= median * 5
    if amount >= BIG_ABSOLUTE and not any("больше обычной" in f for f in facts):
        facts.append(f"Крупная покупка: {_format_currency(amount)} ₸.")

    # 3. серии за день
    if len(same_cat_today) >= 2 and category != HARMFUL_CATEGORY:
        facts.append(f"Это {len(same_cat_today) + 1}-я трата в этой категории за сегодня у {user} "
                     f"(до этого: {', '.join(_short(t) for _, t in same_cat_today[-3:])}).")
    if len([1 for _, t in today if t.get("user") == user]) >= 6:
        facts.append(f"Это уже {len([1 for _, t in today if t.get('user') == user]) + 1}-я трата у {user} за сегодня.")

    # 4. лимит категории
    limit = _num((limits or {}).get(category))
    if limit > 0:
        month_prefix = now.strftime("%Y-%m")
        spent = amount + sum(_num(t.get("amt")) for dt, t in rows
                             if t.get("cat") == category and dt.strftime("%Y-%m") == month_prefix)
        ratio = spent / limit
        month_progress = now.day / calendar.monthrange(now.year, now.month)[1]
        if ratio >= 1:
            facts.append(f"Лимит категории «{category}» превышен: {_format_currency(spent)} из {_format_currency(limit)} ₸ "
                         f"(месяц прошёл на {month_progress:.0%}).")
            strict = True
        elif ratio >= LIMIT_WARN_RATIO and ratio > month_progress + 0.15:
            facts.append(f"По категории «{category}» уже {ratio:.0%} лимита ({_format_currency(spent)} из "
                         f"{_format_currency(limit)} ₸), а месяц прошёл на {month_progress:.0%}.")

    # 5. поздно ночью — только лёгкое «повод», строгим не делаем
    if (now.hour >= 23 or now.hour < 4) and category in {"Кафе, рестораны и доставка еды", HARMFUL_CATEGORY}:
        facts.append(f"Покупка поздно ночью ({now.strftime('%H:%M')}).")

    return {"facts": facts, "strict": strict}


def _short(t: dict) -> str:
    text = str(t.get("comm") or t.get("subcat") or t.get("cat") or "").strip()
    return f"{text[:30]} {_format_currency(t.get('amt'))} ₸"


def _recent_ada_comments(chat_history: list | None, limit: int = 5) -> list:
    found = []
    for m in reversed(chat_history or []):
        if m.get("sender") != "Ада":
            continue
        text = str(m.get("text") or "")
        if "💬" in text:
            found.append(text.split("💬", 1)[1].strip()[:200])
        if len(found) >= limit:
            break
    return list(reversed(found))


def _chat_tail(chat_history: list | None, limit: int = 12) -> str:
    lines = [f"{m.get('sender')}: {str(m.get('text') or '')[:300]}" for m in (chat_history or [])[-limit:]]
    return "\n".join(lines) if lines else "(пока пусто)"


def _cooldown_ok(user: str, strict: bool, income: bool, now: datetime.datetime) -> bool:
    if strict or income:
        return True
    last = state.get(COOLDOWN_NS, user)
    if last:
        try:
            if now - datetime.datetime.fromisoformat(last["at"]) < datetime.timedelta(minutes=COOLDOWN_MINUTES):
                return False
        except (KeyError, TypeError, ValueError):
            pass
    return True


# ───────────────────────── промты ─────────────────────────

PERSONA = """Ты — Ада, живая участница семейного чата Влада и Дианы: ведёшь их бюджет и давно их знаешь.
Говоришь строго от женского лица. Пишешь так, как пишет близкий человек в мессенджере, а не как бот или финансовый консультант.

ХАРАКТЕР:
- Дружеская, с лёгким юмором, внимательная. Замечаешь связи: что человек уже покупал сегодня, о чём вы говорили
  только что, как это выглядит рядом с его обычными тратами.
- Бывает разумно строгой: когда факты правда тревожные (лимит превышен, резкий скачок, привычка набирает
  обороты) — прямо и коротко, одной ясной мыслью, без нотаций и без «стоит подумать».
- Не хвалишь всё подряд и не морализируешь. Личные траты — личные.
- Никогда не выдумываешь факты, суммы, планы. Используешь ТОЛЬКО то, что дано в блоках ниже.
- Не цитируешь названия категорий в кавычках. Называешь вещь по-человечески (пачка, кофе, такси).
- Запрещены обороты: «приобретены», «осуществлена покупка», «трата зафиксирована», «стоит обратить внимание»,
  «возможно, что-то», «из категории».
- Каждый раз по-новому: не начинай одинаково и не повторяй свои прошлые реплики.

ПРИМЕРЫ (как звучит живая Ада и как нет):
Факт: 4-я пачка за 7 дней.   ✔ «Четвёртая пачка за неделю — давай хоть до выходных дотянем без пятой.»   ✘ «Снова покупка из категории Алкоголь, табак…»
Факт: такси 4 200 ₸, обычно ~1 500.   ✔ «Ого, вдвое-втрое дороже обычного. Далеко ехал или тариф кусается?»   ✘ «Сумма превышает среднюю.»
Факт: 3-я трата на кофе за сегодня.   ✔ «Третий кофе до обеда, ты там вообще спишь?»   ✘ «Вы часто покупаете кофе.»
Факт: лимит на еду превышен, месяц прошёл на 60%.   ✔ «Лимит на еду уже пробит, а до конца месяца ещё две недели. Придётся жёстче с доставкой.»
Факт: поступление дохода.   ✔ «Зарплата пришла — наконец-то можно выдохнуть. Лимиты на месяц не забудь держать в голове.»
"""

COMMENT_RULES = """ЗАДАЧА СЕЙЧАС: человек только что внёс трату (она уже записана кодом, ты ничего не записываешь).
Напиши ОДНУ короткую реплику к этой записи — 1–2 предложения, не больше 220 символов, на русском, без markdown,
без кавычек вокруг всего текста, без слов «Записала»/«Записано» (это уже сказано).
Опирайся на ФАКТЫ ниже (их посчитал код) и на недавний разговор. Если ничего живого сказать нельзя — верни пустую строку."""

CHAT_RULES = """ЗАДАЧА СЕЙЧАС: просто поговорить. Ответь на последнее сообщение живо, как участник чата: 1–4 предложения.
Учитывай контекст ниже: недавний разговор, траты, лимиты, время и день. Можно задать один встречный вопрос.
ВАЖНО: в этом ответе ты ничего не записываешь, не меняешь и не ставишь — не говори, что что-то сделала.
Если человек просит действие (записать, напомнить, удалить), скажи, как это написать, коротко и по-человечески.
Не выдумывай данные: если чего-то нет в контексте — так и скажи. Без markdown-заголовков."""


async def _call(system: str, user: str, *, max_tokens: int, timeout: float) -> str:
    from services.deepseek_service import client

    async def _once(extra: dict | None):
        kwargs = dict(model=_model(), max_tokens=max_tokens, temperature=0.85,
                      messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        if extra:
            kwargs["extra_body"] = extra
        resp = await client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()

    try:
        try:
            text = await asyncio.wait_for(_once({"thinking": {"type": "disabled"}}), timeout=timeout)
        except asyncio.TimeoutError:
            raise
        except Exception:
            text = await asyncio.wait_for(_once(None), timeout=timeout)
    except Exception as error:
        print(f"[Голос Ады] Ошибка: {error}")
        return ""
    return _clean(text)


def _clean(text: str) -> str:
    text = (text or "").strip().strip('"«»').strip()
    if text.lower() in SILENCE:
        return ""
    return text[:600]


# ───────────────────────── публичные функции ─────────────────────────

async def comment_for_transaction(tx: dict, user: str, history: list | None, limits: dict | None,
                                  chat_history: list | None) -> str:
    """Живая реплика к записанной трате или "" (повода нет / модель молчит / ошибка)."""
    if not enabled():
        return ""
    try:
        now = now_astana()
        found = build_facts(tx, user, history or [], limits, now)
        if not found["facts"]:
            return ""
        income = tx.get("type") == TYPE_INCOME
        if not _cooldown_ok(user, found["strict"], income, now.replace(tzinfo=None)):
            return ""
        weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        user_block = (
            f"Сейчас {now.strftime('%H:%M')}, {weekdays[now.weekday()]}. Пишет {user}.\n"
            f"Запись: {_format_currency(tx.get('amount'))} ₸, {tx.get('subcategory') or tx.get('category')}"
            f"{', ' + str(tx.get('merchant')) if tx.get('merchant') else ''}"
            f"{' — «' + str(tx.get('user_comment'))[:80] + '»' if tx.get('user_comment') else ''}.\n\n"
            "ФАКТЫ (посчитаны кодом, других нет):\n- " + "\n- ".join(found["facts"]) + "\n\n"
            + ("ТОН: здесь нужна прямота и разумная строгость — одной ясной фразой, без нотации.\n\n" if found["strict"]
               else "ТОН: по-дружески, можно с юмором.\n\n")
            + "НЕДАВНИЙ РАЗГОВОР В ЧАТЕ:\n" + _chat_tail(chat_history) + "\n\n"
            + "ТВОИ ПОСЛЕДНИЕ РЕПЛИКИ (не повторяй их начало и мысль):\n"
            + ("\n".join(f"- {c}" for c in _recent_ada_comments(chat_history)) or "(пока нет)")
        )
        text = await _call(PERSONA + "\n" + COMMENT_RULES, user_block, max_tokens=180, timeout=9.0)
        if text:
            state.put(COOLDOWN_NS, user, {"at": now.replace(tzinfo=None).isoformat()})
        return text
    except Exception as error:
        print(f"[Голос Ады] Пропущено: {error}")
        return ""


async def comment_for_transactions(txs: list, user: str, history: list | None, limits: dict | None,
                                   chat_history: list | None) -> str:
    """Для чека из нескольких позиций — одна реплика по самой «поводной» позиции."""
    for tx in txs or []:
        text = await comment_for_transaction(tx, user, history, limits, chat_history)
        if text:
            return text
    return ""


async def chat_reply(user: str, text: str, context_sections: list | None, fallback: str = "") -> str:
    """Живой ответ в разговоре. Возвращает fallback (ответ разбора), если голос выключен или упал."""
    if not enabled() or not context_sections:
        return fallback
    system = PERSONA + "\n" + CHAT_RULES + "\n\n" + "\n\n".join(context_sections)
    reply = await _call(system, f"{user}: {text}", max_tokens=420, timeout=14.0)
    return reply or fallback
