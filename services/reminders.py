"""Reminder parsing/routing and safe Sheets persistence. No LLM-controlled deletion."""
import asyncio
import calendar
import datetime as dt
import json
import re
import uuid
from services.timezone import now_astana, parse_flexible_datetime, parse_ru_relative_datetime
from services import state

HEADERS = ["reminder_id", "created_at", "target_user", "remind_at", "text", "status", "recurrence",
           "created_by", "anchor_day", "deliveries"]
dispatch_lock = asyncio.Lock()

def normalize_text(text):
    return re.sub(r"\bнаопмни\b|\bнапомнни\b", "напомни", str(text).lower().replace("ё", "е"))

def normalize_target(value):
    t = normalize_text(value)
    d = bool(re.search(r"диан|diana", t))
    v = bool(re.search(r"влад|vlad", t))
    if (d and v) or re.search(r"семь|обоим|обоих|нам|всем|вместе", t):
        return "Семья"
    if d:
        return "Диана"
    if v:
        return "Влад"
    raise ValueError("Укажите получателя: Влад, Диана или нам обоим.")

def extract_target(text, author):
    t = normalize_text(text)
    if re.search(r"(?:напомни(?:ть)?|напоминание)\s+(?:пожалуйста\s+)?(?:нам|обоим|семье|всем)\b|\bдля\s+(?:нас|обоих|семьи)\b", t):
        return "Семья"
    # Only recipient constructions; "взять у Дианы" is not an address.
    d = re.search(r"(?:напомни(?:ть)?\s+(?:пожалуйста\s+)?|для\s+|напоминание\s+)(диане|дианы)\b|\bдиане\s+напомни", t)
    v = re.search(r"(?:напомни(?:ть)?\s+(?:пожалуйста\s+)?|для\s+|напоминание\s+)(владу|влада|владиславу)\b|\bвладу\s+напомни", t)
    if (d or v) and re.search(r"(?:диане\s+и\s+владу|владу\s+и\s+диане|владиславу\s+и\s+диане)", t):
        return "Семья"
    return "Диана" if d else "Влад" if v else author

def is_add(text):
    t = normalize_text(text).strip()
    return bool(re.search(r"^(?:ада[, !]*\s*)?(?:напомни(?:ть)?\b|(?:диане|владу)\s+напомни\b|(?:поставь|добавь|создай)\s+напоминани)", t))

def recurrence_from_text(text):
    t = normalize_text(text)
    if re.search(r"ежемесяч|каждый месяц", t):
        return "monthly"
    if re.search(r"ежеднев|каждый день", t):
        return "daily"
    if re.search(r"еженедель|каждую неделю", t):
        return "weekly"
    return "once"

def extract_task(text):
    t = normalize_text(text)
    t = re.sub(r"^(?:ада[, !]*\s*)?", "", t)
    t = re.sub(r"^(?:(?:диане|владу)\s+)?(?:напомни(?:ть)?|(?:поставь|добавь|создай)\s+напоминание)\s*", "", t)
    t = re.sub(r"^(?:пожалуйста\s+)?(?:(?:диане|владу|владиславу)(?:\s+и\s+(?:диане|владу))?|нам(?:\s+обоим)?|мне|обоим)\b", "", t)
    t = re.sub(r"\b(?:для дианы|для влада|каждый день|каждый месяц|каждую неделю|ежедневно|ежемесячно|еженедельно)\b", "", t)
    t = re.sub(r"\b(?:через)\s+(?:\d+|полчаса|час|минуту|день|[а-я]+)(?:\s+(?:мин\w*|час\w*|дн\w*|день))?", "", t)
    t = re.sub(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b", "", t)
    t = re.sub(r"\b(?:сегодня|завтра|послезавтра)\b", "", t)
    t = re.sub(r"\b(?:в|во|на)\s+(?:\d{1,2}(?:[:.]\d{2})?|один|два|три|четыре|пять|шесть|семь|восемь|девять|десять|одиннадцать|двенадцать)\b(?:\s+(?:утра|вечера|дня|утром|вечером))?", "", t)
    t = re.sub(r"\b(?:в|во)\s+(?:понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)\b", "", t)
    t = re.sub(r"^\s*(?:о том[, ]+что(?: нужно)?|что нужно|чтобы|про то[, ]+что)\s*", "", t)
    return re.sub(r"\s+", " ", t).strip(" ,.—-")

def worksheet():
    from services.sheets import _get_or_create_worksheet
    ws = _get_or_create_worksheet("Reminders", HEADERS, rows=100, cols=len(HEADERS))
    existing = ws.row_values(1)
    if existing[:7] != HEADERS[:7]:
        raise RuntimeError("Неожиданная схема Reminders; миграция остановлена")
    if any(value and value != HEADERS[i] for i, value in enumerate(existing[7:10], 7)):
        raise RuntimeError("Колонки H:J Reminders уже заняты другой схемой")
    if len(existing) < len(HEADERS):
        if ws.col_count < len(HEADERS):
            ws.resize(cols=len(HEADERS))
        ws.update(range_name="H1:J1", values=[HEADERS[7:]])
    return ws

def records():
    from services.sheets import _get_all_records_safe
    return _get_all_records_safe(worksheet())

def pending():
    return [r for r in records() if str(r.get("status") or "").lower() in {"pending", "active", ""}
            and r.get("text") and r.get("remind_at")]

def add(target, when, text, recurrence="once", author="", reminder_id=None):
    # A retried confirmation may arrive after the scheduled time. Return the existing ID first.
    if reminder_id:
        previous = next((r for r in records() if r.get("reminder_id") == reminder_id), None)
        if previous:
            return previous
    parsed = parse_flexible_datetime(when)
    if not parsed or parsed <= now_astana():
        raise ValueError("Время должно быть в будущем (Астана, UTC+5).")
    if recurrence not in {"once", "daily", "weekly", "monthly"}:
        raise ValueError("Неизвестный повтор")
    text = str(text or "").strip()
    if not text:
        raise ValueError("Нужен текст напоминания")
    target = normalize_target(target)
    ws = worksheet()
    rid = reminder_id or "REM_" + uuid.uuid4().hex[:12]
    for r in records():
        if r.get("reminder_id") == rid:
            return r
    row = [rid, now_astana().strftime("%Y-%m-%d %H:%M:%S"), target,
           parsed.strftime("%Y-%m-%d %H:%M:%S"), text, "pending", recurrence,
           author, parsed.day, "{}"]
    ws.append_row(row, table_range="A1:J1", value_input_option="RAW")
    return dict(zip(HEADERS, row))

def update_by_id(reminder_id, changes):
    ws = worksheet()
    from services.sheets import _get_all_records_safe
    for idx, r in enumerate(_get_all_records_safe(ws), 2):
        if r.get("reminder_id") == reminder_id:
            values = [changes.get(h, r.get(h, "")) for h in HEADERS]
            ws.update(range_name=f"A{idx}:J{idx}", values=[values], value_input_option="RAW")
            return True
    return False

def next_occurrence(when, recurrence, now, anchor_day=None):
    old = parse_flexible_datetime(when)
    if not old:
        raise ValueError("Неверная дата")
    if recurrence in {"daily", "weekly"}:
        step = dt.timedelta(days=1 if recurrence == "daily" else 7)
        return old + step * max(1, (now-old)//step + 1)
    if recurrence == "monthly":
        anchor = int(anchor_day or old.day)
        year, month = old.year, old.month
        while True:
            month += 1
            if month == 13:
                year, month = year+1, 1
            candidate = old.replace(year=year, month=month,
                                    day=min(anchor, calendar.monthrange(year, month)[1]))
            if candidate > now:
                return candidate
    return None

def group_address(target):
    """Target identifies people in ONE group message, never a private destination."""
    import html
    from config import VLAD_TELEGRAM_ID, DIANA_TELEGRAM_ID
    name = normalize_target(target)
    mapping = {"Влад": VLAD_TELEGRAM_ID, "Диана": DIANA_TELEGRAM_ID}
    names = ["Влад", "Диана"] if name == "Семья" else [name]
    return " и ".join(f'<a href="tg://user?id={int(mapping[n])}">{html.escape(n)}</a>'
                      if str(mapping.get(n) or "").isdigit() else html.escape(n) for n in names)

async def deliver_due(bot, now=None):
    from config import FAMILY_CHAT_ID
    from services.telegram_safe import safe_send_message
    import html
    import logging
    now = now or now_astana()
    async with dispatch_lock:
        for reminder in await asyncio.to_thread(pending):
            try:
                when = parse_flexible_datetime(reminder["remind_at"])
                if not when or when > now:
                    continue
                rid = reminder["reminder_id"]
                recurrence = str(reminder.get("recurrence") or "once").lower()
                if recurrence not in {"once", "daily", "weekly", "monthly"}:
                    raise ValueError("Неизвестный повтор: " + recurrence)
                next_dt = next_occurrence(when, recurrence, now, reminder.get("anchor_day"))
                try:
                    deliveries = json.loads(reminder.get("deliveries") or "{}")
                except (ValueError, TypeError):
                    deliveries = {}
                if not isinstance(deliveries, dict):
                    deliveries = {}
                occurrence = when.isoformat()
                key = "group:" + str(FAMILY_CHAT_ID)
                if not FAMILY_CHAT_ID:
                    raise ValueError("FAMILY_CHAT_ID не настроен")
                if deliveries.get(key) != occurrence:
                    await safe_send_message(bot, FAMILY_CHAT_ID,
                        f"⏰ {group_address(reminder['target_user'])}, напоминаю\n\n"
                        f"{html.escape(str(reminder['text']))}\n\n"
                        f"📅 {when.strftime('%d.%m.%Y %H:%M')} · Астана\n"
                        f"ID: {html.escape(str(rid))}", parse_mode="HTML")
                    deliveries[key] = occurrence
                    if not await asyncio.to_thread(update_by_id, rid, {"deliveries": json.dumps(deliveries)}):
                        raise RuntimeError("Не найдена строка для подтверждения доставки")
                changes = {"status": "sent"} if next_dt is None else {
                    "remind_at": next_dt.strftime("%Y-%m-%d %H:%M:%S"), "deliveries": "{}",
                    "anchor_day": reminder.get("anchor_day") or when.day}
                await asyncio.to_thread(update_by_id, rid, changes)
            except Exception:
                logging.exception("Reminder delivery/ack failed; kept for retry")

def format_list(items):
    if not items:
        return "Активных напоминаний не найдено."
    repeats = {"daily": "каждый день", "weekly": "каждую неделю", "monthly": "каждый месяц", "once": "однократно"}
    return "📋 Напоминания\n\n" + "\n\n".join(
        f"{i}. {r.get('target_user')} · {r.get('remind_at')}\n"
        f"{r.get('text')}\n{repeats.get(r.get('recurrence'), 'однократно')} · ID: {r.get('reminder_id')}"
        for i,r in enumerate(items, 1))

def find_matches(items, query, author):
    t = normalize_text(query)
    ids = re.findall(r"\brem_[a-z0-9_-]+\b", t)
    if ids:
        return [r for r in items if str(r["reminder_id"]).lower() in ids]
    if re.search(r"диан", t):
        items = [r for r in items if normalize_target(r["target_user"]) in {"Диана", "Семья"}]
    elif re.search(r"влад", t):
        items = [r for r in items if normalize_target(r["target_user"]) in {"Влад", "Семья"}]
    elif re.search(r"\bмои\b|\bмне\b", t):
        items = [r for r in items if normalize_target(r["target_user"]) in {author, "Семья"}]
    t = re.sub(r"\b(?:удали\w*|убери|отмени\w*|напомин\w*|покажи|список|какие|есть|активные|все|мои|мне|для|про|об|о|на|диан\w*|влад\w*|пожалуйста)\b", " ", t)
    tokens = re.findall(r"[а-яa-z0-9]+", t)
    return [r for r in items if all(token in normalize_text(r["text"]+" "+str(r["remind_at"])) for token in tokens)]

async def handle(message, text, author):
    from services.telegram_safe import safe_answer
    key = state.dialogue_key(message.chat.id, message.from_user.id)
    t = normalize_text(text)
    if await confirm_plan(message, t, author):
        return True
    draft = state.get("reminder_draft", key)
    deletion = state.get("reminder_delete", key)
    if deletion and t.strip() in {"да", "подтверждаю", "нет", "отмена"}:
        if t.strip() in {"нет", "отмена"}:
            state.delete("reminder_delete", key)
            await safe_answer(message, "Удаление отменено."); return True
        async with dispatch_lock:
            active_ids = {r["reminder_id"] for r in await asyncio.to_thread(pending)}
            count = 0
            for rid in deletion["ids"]:
                if rid in active_ids:
                    count += bool(await asyncio.to_thread(update_by_id, rid, {"status": "cancelled"}))
        state.delete("reminder_delete", key)
        state.delete("reminder_draft", key)
        await safe_answer(message, f"Отменила напоминаний: {count}. История сохранена в таблице.")
        return True
    if draft and t.strip() in {"отмена", "нет"}:
        state.delete("reminder_draft", key)
        await safe_answer(message, "Создание напоминания отменено."); return True
    if t.strip() == "/reminders":
        items = await asyncio.to_thread(pending)
        state.put("reminder_list", key, {"ids": [x["reminder_id"] for x in items]})
        await safe_answer(message, format_list(items), parse_mode=None)
        return True
    management = ("напомин" in t or "rem_" in t) and re.search(r"удали|отмени|убери|покажи|список|какие", t)
    if management and not is_add(t):
        state.delete("debt_draft", key)
        items = await asyncio.to_thread(pending)
        matches = find_matches(items, t, author)
        if re.search(r"удали|отмени|убери", t) and matches:
            state.delete("reminder_plan", key)
            state.delete("edit_plan", key)
            state.put("reminder_delete", key, {"ids": [r["reminder_id"] for r in matches]})
            await safe_answer(message, format_list(matches) + "\n\nОтменить именно эти напоминания? «Да» / «Нет».")
        elif not matches and items:
            return False
        else:
            await safe_answer(message, format_list(matches))
        state.put("reminder_list", key, {"ids": [r["reminder_id"] for r in matches]})
        return True
    continuing = draft and not is_add(t) and bool(re.search(r"^(?:сегодня|завтра|послезавтра|в |во |на |через |\d)", t.strip()))
    if not is_add(t) and not continuing:
        return False
    if re.search(r"\b(?:ей|ему|об этом|то же|то самое)\b", t):
        return False
    state.delete("debt_draft", key)
    state.delete("reminder_plan", key)
    state.delete("reminder_delete", key)
    combined = draft["text"] + " " + t if continuing else t
    if (re.search(r"\b\d{4}-\d{2}-\d{2}\b|\d{1,2}[- ]?(?:го|числа|число)", combined)
            or (re.search(r"кажд|ежед|ежем|ежен", combined) and recurrence_from_text(combined) == "once")):
        state.put("reminder_draft", key, {"text": combined})
        return False
    when = parse_ru_relative_datetime(combined)
    task = extract_task(combined)
    if not when or not task:
        state.put("reminder_draft", key, {"text": draft["text"] if continuing else text})
        return False
    target = (draft.get("target") if continuing else None) or extract_target(combined, author)
    rec = (draft.get("recurrence") if continuing else None) or recurrence_from_text(combined)
    saved = await asyncio.to_thread(add, target, when, task, rec, author,
                                   f"REM_{message.chat.id}_{message.message_id}")
    state.delete("reminder_draft", key)
    await safe_answer(message, "✅ Напоминание сохранено\n\n"
        f"Кому: {saved['target_user']}\nКогда: {saved['remind_at']} · Астана\n"
        f"Что: {saved['text']}\nПовтор: {rec}\nID: {saved['reminder_id']}\n\n"
        "Сообщение придёт в семейную группу с указанным адресатом.",
        parse_mode=None)
    return True


async def confirm_plan(message, text, author):
    from services.telegram_safe import safe_answer
    key = state.dialogue_key(message.chat.id, message.from_user.id)
    plan = state.get("reminder_plan", key)
    if not plan or text.strip() not in {"да", "подтверждаю", "нет", "отмена"}:
        return False
    if text.strip() in {"нет", "отмена"}:
        state.delete("reminder_plan", key)
        await safe_answer(message, "Отменила создание напоминаний.")
        return True
    saved = []
    async with dispatch_lock:
        for idx, point in enumerate(plan["times"]):
            saved.append(await asyncio.to_thread(add, plan["target"], point, plan["text"],
                plan["recurrence"], author, f"{plan['id']}_{idx}"))
    state.delete("reminder_plan", key)
    state.delete("reminder_draft", key)
    await safe_answer(message, "✅ Сохранила. Доставка в семейную группу.\n\n" + format_list(saved), parse_mode=None)
    return True

async def handle_model(message, parsed, author):
    """LLM may propose tasks using chat context. Dates/targets validated; writes confirmed."""
    from services.telegram_safe import safe_answer
    key = state.dialogue_key(message.chat.id, message.from_user.id)
    intent = parsed.get("intent")
    if intent == "get_reminders":
        items = await asyncio.to_thread(pending)
        state.put("reminder_list", key, {"ids": [x["reminder_id"] for x in items]})
        await safe_answer(message, format_list(items), parse_mode=None)
        return True
    if intent == "delete_reminder":
        items = await asyncio.to_thread(pending)
        ids = parsed.get("reminder_ids") or []
        if isinstance(ids, str):
            ids = [ids]
        selected = [x for x in items if x["reminder_id"] in ids]
        if not selected:
            query = parsed.get("search_query") or ""
            selected = find_matches(items, query, author) if query.strip() else []
        if not selected:
            await safe_answer(message, "Не определила, какие напоминания отменить. "
                              "Укажи ID из /reminders или текст задачи.")
            return True
        state.delete("debt_draft", key)
        state.delete("reminder_plan", key)
        state.delete("edit_plan", key)
        state.put("reminder_delete", key, {"ids": [x["reminder_id"] for x in selected]})
        await safe_answer(message, format_list(selected) + "\n\nОтменить именно эти? «Да» / «Нет».", parse_mode=None)
        return True
    if intent != "add_reminder":
        return False
    text = str(parsed.get("reminder_text") or "").strip()
    times = parsed.get("reminder_times") or []
    target_raw = parsed.get("reminder_target") or author
    rec = str(parsed.get("recurrence") or "once").lower()
    if isinstance(times, str):
        times = [times]
    valid = []
    try:
        target = normalize_target(target_raw)
        if not isinstance(times, list) or len(times) > 12:
            raise ValueError("За один запрос можно поставить до 12 напоминаний.")
        for point in times:
            when = parse_flexible_datetime(point)
            if not when or when <= now_astana():
                raise ValueError("Уточни будущую дату и время по Астане.")
            value = when.strftime("%Y-%m-%d %H:%M:%S")
            if value not in valid:
                valid.append(value)
        if rec not in {"once", "daily", "weekly", "monthly"}:
            raise ValueError("Поддерживаются разовые, ежедневные, еженедельные и ежемесячные напоминания.")
        if not text or not valid:
            raise ValueError(parsed.get("clarification_question") or "Что и в какое время напомнить?")
    except ValueError as error:
        state.put("reminder_draft", key, {"text": text, "target": target_raw, "recurrence": rec})
        await safe_answer(message, str(error))
        return True
    state.delete("debt_draft", key)
    state.delete("reminder_delete", key)
    state.delete("edit_plan", key)
    state.put("reminder_plan", key, {"times": valid, "target": target, "text": text, "recurrence": rec,
                                   "id": f"REM_{message.chat.id}_{message.message_id}"})
    await safe_answer(message, f"Проверь напоминание\n\nКому: {target}\nВремя: " + "; ".join(valid)
                      + f" · Астана\nЧто: {text}\nПовтор: {rec}\nДоставка: семейная группа"
                      + "\n\nПоставить? «Да» / «Нет».", parse_mode=None)
    return True


from services.sheets import _serialized
worksheet = _serialized(worksheet)
records = _serialized(records)
pending = _serialized(pending)
add = _serialized(add)
update_by_id = _serialized(update_by_id)
