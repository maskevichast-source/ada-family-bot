"""Напоминания семьи: гарантированная немедленная запись в Google Таблицу и доставка в группу."""

import asyncio
import calendar
import datetime as dt
import json
import logging
import re
import uuid
import html

from services.timezone import now_astana, parse_flexible_datetime, parse_ru_relative_datetime
from services.telegram_safe import safe_answer, safe_send_message
import config

HEADERS = [
    "reminder_id", "created_at", "target_user", "remind_at",
    "text", "status", "recurrence", "created_by", "anchor_day", "deliveries",
    "until",
]

dispatch_lock = asyncio.Lock()


def normalize_text(text):
    return re.sub(r"\bнаопмни\b|\bнапомнни\b", "напомни", str(text or "").lower().replace("ё", "е"))


def normalize_target(value, default_author="Влад"):
    t = normalize_text(value)
    d = bool(re.search(r"диан|diana", t))
    v = bool(re.search(r"влад|vlad", t))
    if (d and v) or any(w in t for w in ["семь", "обоим", "обоих", "нам", "всем", "вместе"]):
        return "Семья"
    if d:
        return "Диана"
    if v:
        return "Влад"
    return default_author


def is_add(text):
    t = normalize_text(text)
    return bool(re.search(r"\bнапомни(?:ть)?\b|(?:поставь|добавь|создай)\s+напоминани", t))


# Старое имя — используется в нескольких местах ниже.
is_add_cmd = is_add


def extract_target_from_text(text, author):
    return _explicit_target(text) or author


def _explicit_target(text):
    """Как extract_target_from_text, но None, если в тексте НЕТ явного
    указания на Диану/Влада/обоих — не путать с "не назвали, берём автора"."""
    t = normalize_text(text)
    both_named = bool(re.search(
        r"(?:диане|дианы)\s*(?:,|и)\s*(?:владу|влада)\b|(?:владу|влада)\s*(?:,|и)\s*(?:диане|дианы)\b", t
    ))
    if both_named or any(w in t for w in ["нам обоим", "обоим", "обоих", "семье", "для нас", "для семьи"]):
        return "Семья"
    if re.search(r"(?:напомни(?:ть)?\s+(?:пожалуйста\s+)?|для\s+)(?:диане|дианы)\b|\bдиане\s+напомни", t):
        return "Диана"
    if re.search(r"(?:напомни(?:ть)?\s+(?:пожалуйста\s+)?|для\s+)(?:владу|влада)\b|\bвладу\s+напомни", t):
        return "Влад"
    return None


# Короткое имя, которого ждут тесты.
extract_target = extract_target_from_text


def extract_clean_task(text):
    t = normalize_text(text)
    t = re.sub(r"^(?:ада[, !]*\s*)?", "", t)
    t = re.sub(
        r"^(?:напомни(?:ть)?|(?:поставь|добавь|создай)\s+напоминание)\s*"
        r"(?:пожалуйста\s+)?(?:диане|владу|мне|нам(?:\s+обоим)?|обоим)?\s*",
        "", t,
    )
    t = re.sub(r"^(?:пожалуйста\s+)?(?:диане|владу|мне|нам(?:\s+обоим)?|обоим)\b\s*", "", t)
    t = re.sub(
        r"\b(?:каждый\s+день|ежедневно|каждую\s+неделю|еженедельно|каждый\s+понедельник|"
        r"каждый\s+вторник|каждую\s+среду|каждый\s+четверг|каждую\s+пятницу|каждую\s+субботу|"
        r"каждое\s+воскресенье|каждый\s+месяц|ежемесячно)\b",
        "", t,
    )
    t = re.sub(r"\b(?:сегодня|завтра|послезавтра)\b", "", t)
    t = re.sub(r"\b(?:в|во|на)\s+\d{1,2}(?:[:.]\d{2})?(?:\s*(?:утра|вечера|дня))?\b", "", t)
    t = re.sub(r"\b(?:в девять вечера|в \d+ вечера|утром|вечером)\b", "", t)
    t = re.sub(r"^\s*(?:о том[, ]+что(?: нужно)?|что нужно|чтобы|про то[, ]+что)\s*", "", t)
    return re.sub(r"\s+", " ", t).strip(" ,.—-")


# Короткое имя, которого ждут тесты.
extract_task = extract_clean_task


def recurrence_from_text(text) -> str:
    t = normalize_text(text)
    if any(w in t for w in ["каждый день", "ежедневно"]):
        return "daily"
    if any(w in t for w in ["каждую неделю", "каждый понедельник", "каждый вторник", "каждую среду",
                             "каждый четверг", "каждую пятницу", "каждую субботу", "каждое воскресенье",
                             "еженедельно"]):
        return "weekly"
    if any(w in t for w in ["каждый месяц", "ежемесячно"]):
        return "monthly"
    return "once"


def next_occurrence(prev_dt, recurrence, now=None, anchor_day=None):
    """Следующее срабатывание повторяющегося напоминания.

    - daily/weekly: добавляет период, но если результат всё ещё в прошлом
      относительно `now` (например, бот был выключен несколько дней) —
      докручивает до ближайшего будущего момента, а не шлёт пачку
      просроченных напоминаний за каждый пропущенный день.
    - monthly: пытается попасть на anchor_day (исходный день месяца); если
      в следующем месяце столько дней нет (31 февраля и т.п.) — берёт
      последний день месяца, а через месяц пробует вернуться к anchor_day.
    """
    now = now or prev_dt
    rec = str(recurrence or "once").lower()

    if rec == "daily":
        candidate = prev_dt + dt.timedelta(days=1)
        while candidate <= now:
            candidate += dt.timedelta(days=1)
        return candidate

    if rec == "weekly":
        candidate = prev_dt + dt.timedelta(days=7)
        while candidate <= now:
            candidate += dt.timedelta(days=7)
        return candidate

    if rec == "monthly":
        day = anchor_day or prev_dt.day
        year, month = prev_dt.year, prev_dt.month + 1
        if month > 12:
            month = 1
            year += 1
        last_day = calendar.monthrange(year, month)[1]
        return prev_dt.replace(year=year, month=month, day=min(day, last_day))

    return None


def worksheet():
    from services.sheets import _get_or_create_worksheet, _table_range
    ws = _get_or_create_worksheet("Reminders", HEADERS, rows=100, cols=len(HEADERS))
    existing = ws.row_values(1)
    if existing[:7] != HEADERS[:7]:
        raise RuntimeError("Лист Reminders: первые 7 колонок не совпадают с ожидаемой схемой.")
    # Если в хвосте (H и дальше) уже что-то есть и это НЕ наши поля —
    # значит колонки заняты чужими данными, дописывать поверх них опасно.
    if len(existing) > 7 and existing[7:] != HEADERS[7:len(existing)]:
        raise RuntimeError("Лист Reminders: в дополнительных колонках посторонние данные, миграция остановлена.")
    if len(existing) < len(HEADERS):
        if ws.col_count < len(HEADERS):
            ws.resize(cols=len(HEADERS))
        # H1 — первая из "хвостовых" колонок (после первых 7 базовых).
        tail_range = f"H1:{_table_range(len(HEADERS))[3:]}"
        ws.update(range_name=tail_range, values=[HEADERS[7:]])
    return ws


def records():
    from services.sheets import _get_all_records_safe
    rows = _get_all_records_safe(worksheet())
    for idx, r in enumerate(rows, start=2):
        r["row_idx"] = idx
    return rows


def pending():
    return [
        r for r in records()
        if str(r.get("status") or "").lower() in {"pending", "active", ""}
        and r.get("text") and r.get("remind_at")
    ]


def find_matches(items, query, author):
    """Найти среди items (записи Reminders) те, что подходят под запрос на
    удаление/поиск — не удаляя ничего, просто фильтрует. Используется, когда
    нужно явно показать, что именно будет затронуто, прежде чем действовать."""
    t = normalize_text(query)
    target = None
    if re.search(r"\bдиан[еы]\b", t):
        target = "Диана"
    elif re.search(r"\bвлад[ау]\b", t):
        target = "Влад"

    stop = {
        "удали", "удалить", "напоминание", "напоминания", "сними", "отмени",
        "про", "об", "о", "покажи", "диане", "дианы", "владу", "влада",
    }
    keywords = [w for w in re.findall(r"\w+", t) if w not in stop]

    results = []
    for item in items:
        if target and str(item.get("target_user", "")) != target:
            continue
        row_str = " ".join(str(v) for v in item.values()).lower()
        if keywords and not all(k in row_str for k in keywords):
            continue
        results.append(item)
    return results


def _soft_delete_by_id(reminder_id: str) -> dict | None:
    ws = worksheet()
    for r in pending():
        if r.get("reminder_id") == reminder_id:
            ws.update_cell(r["row_idx"], 6, "cancelled")
            return r
    return None


def _soft_delete_by_keyword(query: str) -> list[dict]:
    """Помечает подходящие напоминания как 'cancelled', не удаляя строки —
    чтобы история осталась в таблице. Если в запросе есть "все"/"всё" —
    отменяет ВСЕ совпадения, а не только первое найденное (раньше "про
    персен удали все напоминания" отменяло по одному за раз, и приходилось
    повторять команду N раз для N совпадений)."""
    stop = {
        "удали", "удалить", "напоминание", "напоминания", "сними", "отмени",
        "про", "об", "о", "все", "всё", "их", "эти",
    }
    normalized = normalize_text(query)
    bulk = bool(re.search(r"\bвс[еёя]\b", normalized))
    keywords = [w for w in re.findall(r"\w+", normalized) if w not in stop]
    if not keywords:
        return []

    ws = worksheet()
    matched = []
    for r in reversed(pending()):
        row_str = " ".join(str(v) for v in r.values()).lower()
        if all(k in row_str for k in keywords):
            ws.update_cell(r["row_idx"], 6, "cancelled")
            matched.append(r)
            if not bulk:
                break
    return matched


def add(target, when, text, recurrence="once", author="Влад", reminder_id=None, until=None):
    """Прямое сохранение в лист Reminders без промежуточных черновиков.

    until — дата, после которой повторяющееся напоминание перестаёт
    приходить (например "напоминай две недели"). Для разового
    напоминания (recurrence="once") не используется.
    """
    parsed = parse_flexible_datetime(when)
    if not parsed:
        raise ValueError("Не удалось распознать время напоминания.")

    clean_text = str(text or "").strip()
    if not clean_text:
        raise ValueError("Текст напоминания не может быть пустым.")

    target_user = normalize_target(target, author)

    until_str = ""
    if until:
        until_dt = parse_flexible_datetime(until)
        until_str = until_dt.strftime("%Y-%m-%d") if until_dt else str(until)[:10]

    if reminder_id:
        # Идемпотентность: повторный вызов с тем же reminder_id (например,
        # ретрай после сетевой ошибки) не должен плодить вторую строку.
        existing = next((r for r in records() if r.get("reminder_id") == reminder_id), None)
        if existing:
            return existing

    rid = reminder_id or f"REM_{now_astana().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
    now_str = now_astana().strftime("%Y-%m-%d %H:%M:%S")
    time_str = parsed.strftime("%Y-%m-%d %H:%M:%S")

    row = [
        rid, now_str, target_user, time_str, clean_text,
        "pending", recurrence, author, parsed.day, "{}", until_str,
    ]

    ws = worksheet()
    from services.sheets import _table_range
    ws.append_row(row, table_range=_table_range(len(HEADERS)), value_input_option="RAW")
    return dict(zip(HEADERS, row))


def update_by_id(reminder_id, changes):
    ws = worksheet()
    from services.sheets import _get_all_records_safe, _table_range
    for idx, r in enumerate(_get_all_records_safe(ws), 2):
        if r.get("reminder_id") == reminder_id:
            values = [changes.get(h, r.get(h, "")) for h in HEADERS]
            col_letter = _table_range(len(HEADERS))[3:]
            ws.update(range_name=f"A{idx}:{col_letter}{idx}", values=[values], value_input_option="RAW")
            return True
    return False


def format_list(items):
    if not items:
        return "Активных напоминаний не найдено."
    lines = ["📋 **Активные напоминания:**\n"]
    for idx, r in enumerate(items, 1):
        target = r.get("target_user", "Семья")
        time_val = str(r.get("remind_at", ""))
        text = str(r.get("text", ""))
        rec = str(r.get("recurrence", "once"))
        rec_names = {"daily": "ежедневно", "weekly": "еженедельно", "monthly": "ежемесячно"}
        rec_str = f" ({rec_names[rec]})" if rec in rec_names else ""
        until = str(r.get("until") or "").strip()
        until_str = f" до {until}" if until and rec in rec_names else ""
        lines.append(f"{idx}. **{target}**: {text}\n   ⏰ {time_val}{rec_str}{until_str} | ID: `{r.get('reminder_id')}`")
    return "\n\n".join(lines)


async def handle(message, text, author):
    """Прямой и мгновенный перехват голосовых и текстовых команд напоминаний."""
    from services import state
    t_clean = normalize_text(text)
    key = state.dialogue_key(message.chat.id, message.from_user.id)

    # 0. Подтверждение удаления, запрошенного ниже
    draft = state.get("reminder_delete", key)
    if draft:
        if t_clean in {"да", "подтверждаю", "удали", "удалить"}:
            if draft.get("ids"):
                deleted_list = []
                for rid in draft["ids"]:
                    d = await asyncio.to_thread(_soft_delete_by_id, rid)
                    if d:
                        deleted_list.append(d)
            else:
                deleted_list = await asyncio.to_thread(_soft_delete_by_keyword, draft.get("query", ""))
            state.delete("reminder_delete", key)
            if deleted_list:
                if len(deleted_list) == 1:
                    d = deleted_list[0]
                    await safe_answer(message, f"✅ Отменила напоминание: {d.get('text')} ({d.get('remind_at')})")
                else:
                    lines = [f"✅ Отменила {len(deleted_list)} напоминани(й):"]
                    lines += [f"• {d.get('text')} ({d.get('remind_at')})" for d in deleted_list]
                    await safe_answer(message, "\n".join(lines), parse_mode=None)
            else:
                await safe_answer(message, "Не нашла такое напоминание в таблице.")
            return True
        if t_clean in {"нет", "отмена", "не надо", "не удаляй"}:
            state.delete("reminder_delete", key)
            await safe_answer(message, "Хорошо, не удаляю.")
            return True

    # 0б. Подтверждение создания сразу НЕСКОЛЬКИХ напоминаний (несколько
    # времён за раз — переспрашиваем, чтобы не наплодить лишнего одной
    # ошибкой распознавания).
    plan = state.get("reminder_plan", key)
    if plan:
        if t_clean in {"да", "подтверждаю", "давай", "ок", "окей"}:
            saved = []
            for t_str in plan["times"]:
                try:
                    saved.append(await asyncio.to_thread(
                        add, plan["target"], t_str, plan["text"], plan.get("recurrence", "once"), author,
                        None, plan.get("until"),
                    ))
                except Exception as e:
                    logging.exception(f"[Reminder Plan Error]: {e}")
            state.delete("reminder_plan", key)
            await safe_answer(message, f"✅ Поставила {len(saved)} напоминани(й) для {plan['target']}: «{plan['text']}».", parse_mode=None)
            return True
        if t_clean in {"нет", "отмена", "не надо"}:
            state.delete("reminder_plan", key)
            await safe_answer(message, "Хорошо, не ставлю.")
            return True

    # 0в. Продолжение диалога "знаем кому/что, не знали когда" — предыдущее
    # сообщение спросило время, это сообщение — просто ответ со временем,
    # без слова "напомни".
    time_draft = state.get("reminder_draft", key)
    if time_draft:
        when_dt = parse_ru_relative_datetime(text)
        if when_dt:
            try:
                saved = await asyncio.to_thread(
                    add, time_draft["target"], when_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    time_draft["text"], time_draft.get("recurrence", "once"), author,
                    None, time_draft.get("until"),
                )
                state.delete("reminder_draft", key)
                await safe_answer(
                    message,
                    f"✅ Поставила напоминание для {saved['target_user']} на {saved['remind_at']} — {saved['text']}.",
                    parse_mode=None,
                )
                return True
            except Exception as e:
                logging.exception(f"[Reminder Draft Error]: {e}")

    # 1. Просмотр списка
    if any(k in t_clean for k in ["покажи напоминания", "список напоминаний", "какие напоминания", "/reminders"]):
        items = await asyncio.to_thread(pending)
        await safe_answer(message, format_list(items), parse_mode=None)
        return True

    # 2. Удаление — сначала спрашиваем подтверждение, чтобы случайное
    # "удали напоминание" (неверно распознанное слово) не стирало нужное
    # сразу же, без возможности передумать.
    if "напоминани" in t_clean and any(k in t_clean for k in ["удали", "сними", "отмени"]):
        state.put("reminder_delete", key, {"query": text})
        await safe_answer(message, "Удалить это напоминание? Напиши «да» или «нет».")
        return True

    # 3. Создание напоминания
    is_add_cmd = is_add(text)
    if not is_add_cmd:
        return False

    when_dt = parse_ru_relative_datetime(text)
    task = extract_clean_task(text)
    target = extract_target_from_text(text, author)
    rec = recurrence_from_text(text)

    if when_dt and task:
        try:
            saved = await asyncio.to_thread(add, target, when_dt.strftime("%Y-%m-%d %H:%M:%S"), task, rec, author)
            await safe_answer(
                message,
                f"✅ **Напоминание сохранено в таблицу!**\n\n"
                f"• **Кому:** {saved['target_user']}\n"
                f"• **Когда:** {saved['remind_at']} (Астана)\n"
                f"• **Что:** {saved['text']}\n"
                f"• **Повтор:** {rec}\n"
                f"• **ID:** `{saved['reminder_id']}`\n\n"
                f"_Уведомление придёт в семейный чат вовремя._",
                parse_mode=None
            )
            return True
        except Exception as e:
            logging.exception(f"[Reminder Add Error]: {e}")
            await safe_answer(message, f"Ошибка записи напоминания в таблицу: {e}")
            return True

    if not when_dt and task:
        # Кому и что — понятно локально (явно названы Влад/Диана/оба), не
        # хватает только времени. Запоминаем контекст на случай, если
        # следующим сообщением придёт просто время без слова "напомни" —
        # но саму реплику отдаём модели (она сформулирует уточняющий
        # вопрос естественнее, чем шаблон).
        explicit_target = _explicit_target(text)
        if explicit_target:
            state.put("reminder_draft", key, {"target": explicit_target, "text": task, "recurrence": rec})

    return False


async def handle_model(message, parsed, author):
    """Если локальный парсер не справился, парсинг завершает DeepSeek."""
    from services import state
    key = state.dialogue_key(message.chat.id, message.from_user.id)
    intent = parsed.get("intent")

    if intent == "get_reminders":
        items = await asyncio.to_thread(pending)
        await safe_answer(message, format_list(items), parse_mode=None)
        return True

    if intent == "delete_reminder":
        ids = parsed.get("reminder_ids") or []
        query = parsed.get("search_query") or ""
        if not ids and not query:
            return False
        state.put("reminder_delete", key, {"ids": ids, "query": query})
        await safe_answer(message, "Удалить это напоминание? Напиши «да» или «нет».")
        return True

    if intent != "add_reminder":
        return False

    text = str(parsed.get("reminder_text") or "").strip()
    times = parsed.get("reminder_times") or []
    target_raw = parsed.get("reminder_target") or author
    rec = str(parsed.get("recurrence") or "once").lower()
    until = parsed.get("recurrence_until") or None
    clarification_q = parsed.get("clarification_question")

    if isinstance(times, str):
        times = [times]

    if not text:
        return False

    if not times:
        # Знаем кому и что, не знаем когда — запоминаем и спрашиваем,
        # вместо того чтобы потерять контекст к следующему сообщению.
        if not clarification_q:
            return False
        state.put("reminder_draft", key, {"target": target_raw, "text": text, "recurrence": rec, "until": until})
        await safe_answer(message, clarification_q)
        return True

    if len(times) > 1:
        # Несколько времён сразу — подтверждаем, прежде чем плодить записи.
        state.put("reminder_plan", key, {"target": target_raw, "text": text, "times": times, "recurrence": rec, "until": until})
        summary = "\n".join(f"• {t_str}" for t_str in times)
        until_line = f"\nПовтор: {rec}, до {until}." if until and rec != "once" else (f"\nПовтор: {rec}." if rec != "once" else "")
        await safe_answer(
            message,
            f"Поставить {len(times)} напоминания для {target_raw} «{text}»?\n{summary}{until_line}\nПодтверди: да/нет.",
            parse_mode=None,
        )
        return True

    saved_list = []
    for t_str in times:
        try:
            saved = await asyncio.to_thread(add, target_raw, t_str, text, rec, author)
            saved_list.append(saved)
        except Exception as e:
            logging.error(f"[Add Reminder Model Error]: {e}")

    if saved_list:
        await safe_answer(
            message,
            f"✅ **Напоминание сохранено в таблицу!**\n\n"
            f"• **Кому:** {saved_list[0]['target_user']}\n"
            f"• **Когда:** {saved_list[0]['remind_at']}\n"
            f"• **Что:** {saved_list[0]['text']}\n"
            f"• **ID:** `{saved_list[0]['reminder_id']}`",
            parse_mode=None
        )
        return True

    return False


def _mention_for(target, vlad_id, diana_id):
    if target == "Диана" and diana_id:
        return f'<a href="tg://user?id={diana_id}">Диана</a>'
    if target == "Влад" and vlad_id:
        return f'<a href="tg://user?id={vlad_id}">Влад</a>'
    # "Семья" или что угодно ещё (например неразобранное "Владислав и Диана")
    # — одно сообщение с упоминанием обоих, а не два отдельных.
    vlad_part = f'<a href="tg://user?id={vlad_id}">Влад</a>' if vlad_id else "Влад"
    diana_part = f'<a href="tg://user?id={diana_id}">Диана</a>' if diana_id else "Диана"
    return f"{vlad_part} и {diana_part}"


async def deliver_due(bot, now=None):
    """Фоновая отправка в семейный чат."""
    now = now or now_astana()
    async with dispatch_lock:
        for r in await asyncio.to_thread(pending):
            try:
                when = parse_flexible_datetime(r.get("remind_at"))
                if not when or when > now:
                    continue

                target = r.get("target_user", "Семья")
                text_val = r.get("text", "")
                mention = _mention_for(target, config.VLAD_TELEGRAM_ID, config.DIANA_TELEGRAM_ID)

                msg = (
                    f"⏰ <b>Напоминание для {mention}:</b>\n\n"
                    f"{html.escape(text_val)}\n\n"
                    f"<i>{when.strftime('%d.%m.%Y %H:%M')} · Астана</i>"
                )
                # Если отправка упадёт — исключение уйдёт в except ниже и
                # статус НЕ изменится: напоминание останется pending и
                # будет отправлено повторно на следующем цикле.
                await safe_send_message(bot, config.FAMILY_CHAT_ID, msg, parse_mode="HTML")

                rec = str(r.get("recurrence", "once")).lower()
                ws = worksheet()
                until_str = str(r.get("until") or "").strip()
                until_dt = parse_flexible_datetime(until_str) if until_str else None
                if rec in {"daily", "weekly", "monthly"} and not (until_dt and now.date() >= until_dt.date()):
                    anchor = r.get("anchor_day")
                    next_dt = next_occurrence(when, rec, now, anchor_day=int(anchor) if anchor else None)
                    if next_dt and not (until_dt and next_dt.date() > until_dt.date()):
                        ws.update_cell(r["row_idx"], 4, next_dt.strftime("%Y-%m-%d %H:%M:%S"))
                    else:
                        ws.update_cell(r["row_idx"], 6, "sent")
                else:
                    ws.update_cell(r["row_idx"], 6, "sent")

            except Exception as e:
                logging.exception(f"[Deliver Reminder Error]: {e}")
