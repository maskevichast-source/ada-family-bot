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
from config import FAMILY_CHAT_ID, VLAD_TELEGRAM_ID, DIANA_TELEGRAM_ID

HEADERS = [
    "reminder_id", "created_at", "target_user", "remind_at",
    "text", "status", "recurrence", "created_by", "anchor_day", "deliveries"
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


def extract_target_from_text(text, author):
    t = normalize_text(text)
    if any(w in t for w in ["нам обоим", "обоим", "семье", "для нас", "для семьи"]):
        return "Семья"
    if re.search(r"(?:напомни(?:ть)?\s+(?:пожалуйста\s+)?|для\s+)(?:диане|дианы)\b|\bдиане\s+напомни", t):
        return "Диана"
    if re.search(r"(?:напомни(?:ть)?\s+(?:пожалуйста\s+)?|для\s+)(?:владу|влада)\b|\bвладу\s+напомни", t):
        return "Влад"
    return author


def extract_clean_task(text):
    t = str(text or "").strip()
    t = re.sub(r"^(?:ада[, !]*\s*)?", "", t, flags=re.I)
    t = re.sub(r"^(?:(?:диане|владу)\s+)?(?:напомни(?:ть)?|(?:поставь|добавь|создай)\s+напоминание)\s*", "", t, flags=re.I)
    t = re.sub(r"^(?:пожалуйста\s+)?(?:диане|владу|мне|нам(?:\s+обоим)?|обоим)\b\s*", "", t, flags=re.I)
    t = re.sub(r"\b(?:сегодня|завтра|послезавтра)\b", "", t, flags=re.I)
    t = re.sub(r"\b(?:в|во|на)\s+\d{1,2}(?:[:.]\d{2})?(?:\s*(?:утра|вечера|дня))?\b", "", t, flags=re.I)
    t = re.sub(r"\b(?:в девять вечера|в \d+ вечера|утром|вечером)\b", "", t, flags=re.I)
    t = re.sub(r"^\s*(?:о том[, ]+что(?: нужно)?|что нужно|чтобы|про то[, ]+что)\s*", "", t, flags=re.I)
    return t.strip(" ,.—-")


def worksheet():
    from services.sheets import _get_or_create_worksheet
    ws = _get_or_create_worksheet("Reminders", HEADERS, rows=100, cols=len(HEADERS))
    existing = ws.row_values(1)
    if len(existing) < len(HEADERS):
        if ws.col_count < len(HEADERS):
            ws.resize(cols=len(HEADERS))
        ws.update(range_name="H1:J1", values=[HEADERS[7:]])
    return ws


def records():
    from services.sheets import _get_all_records_safe
    return _get_all_records_safe(worksheet())


def pending():
    return [
        r for r in records()
        if str(r.get("status") or "").lower() in {"pending", "active", ""}
        and r.get("text") and r.get("remind_at")
    ]


def add(target, when, text, recurrence="once", author="Влад", reminder_id=None):
    """Прямое сохранение в лист Reminders без промежуточных черновиков."""
    parsed = parse_flexible_datetime(when)
    if not parsed:
        raise ValueError("Не удалось распознать время напоминания.")

    clean_text = str(text or "").strip()
    if not clean_text:
        raise ValueError("Текст напоминания не может быть пустым.")

    target_user = normalize_target(target, author)
    rid = reminder_id or f"REM_{now_astana().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
    now_str = now_astana().strftime("%Y-%m-%d %H:%M:%S")
    time_str = parsed.strftime("%Y-%m-%d %H:%M:%S")

    row = [
        rid, now_str, target_user, time_str, clean_text,
        "pending", recurrence, author, parsed.day, "{}"
    ]

    ws = worksheet()
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


def format_list(items):
    if not items:
        return "Активных напоминаний не найдено."
    lines = ["📋 **Активные напоминания:**\n"]
    for idx, r in enumerate(items, 1):
        target = r.get("target_user", "Семья")
        time_val = str(r.get("remind_at", ""))
        text = str(r.get("text", ""))
        rec = str(r.get("recurrence", "once"))
        rec_str = " (ежедневно)" if rec == "daily" else (" (ежемесячно)" if rec == "monthly" else "")
        lines.append(f"{idx}. **{target}**: {text}\n   ⏰ {time_val}{rec_str} | ID: `{r.get('reminder_id')}`")
    return "\n\n".join(lines)


async def handle(message, text, author):
    """Прямой и мгновенный перехват голосовых и текстовых команд напоминаний."""
    t_clean = normalize_text(text)

    # 1. Просмотр списка
    if any(k in t_clean for k in ["покажи напоминания", "список напоминаний", "какие напоминания", "/reminders"]):
        items = await asyncio.to_thread(pending)
        await safe_answer(message, format_list(items), parse_mode=None)
        return True

    # 2. Удаление
    if "напоминани" in t_clean and any(k in t_clean for k in ["удали", "сними", "отмени"]):
        from services.sheets import delete_record_by_keyword
        deleted = await asyncio.to_thread(delete_record_by_keyword, "Reminders", text)
        if deleted:
            await safe_answer(message, f"✅ Отменила напоминание: {deleted.get('text')} ({deleted.get('remind_at')})")
        else:
            await safe_answer(message, "Не нашла такое напоминание в таблице.")
        return True

    # 3. Создание напоминания
    is_add_cmd = bool(re.search(r"\bнапомни(?:ть)?\b|(?:поставь|добавь|создай)\s+напоминани", t_clean))
    if not is_add_cmd:
        return False

    when_dt = parse_ru_relative_datetime(text)
    task = extract_clean_task(text)
    target = extract_target_from_text(text, author)
    rec = "daily" if any(w in t_clean for w in ["каждый день", "ежедневно"]) else "once"

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

    return False


async def handle_model(message, parsed, author):
    """Если локальный парсер не справился, парсинг завершает DeepSeek и СРАЗУ пишет в таблицу."""
    intent = parsed.get("intent")
    if intent == "get_reminders":
        items = await asyncio.to_thread(pending)
        await safe_answer(message, format_list(items), parse_mode=None)
        return True

    if intent == "delete_reminder":
        query = parsed.get("search_query") or ""
        from services.sheets import delete_record_by_keyword
        deleted = await asyncio.to_thread(delete_record_by_keyword, "Reminders", query)
        if deleted:
            await safe_answer(message, f"✅ Удалила напоминание: {deleted.get('text')} ({deleted.get('remind_at')})")
        else:
            await safe_answer(message, "Не нашла такое напоминание.")
        return True

    if intent != "add_reminder":
        return False

    text = str(parsed.get("reminder_text") or "").strip()
    times = parsed.get("reminder_times") or []
    target_raw = parsed.get("reminder_target") or author
    rec = str(parsed.get("recurrence") or "once").lower()

    if isinstance(times, str):
        times = [times]

    if not times or not text:
        return False

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


async def deliver_due(bot):
    """Фоновая отправка в семейный чат."""
    now = now_astana()
    async with dispatch_lock:
        for r in await asyncio.to_thread(pending):
            try:
                when = parse_flexible_datetime(r.get("remind_at"))
                if not when or when > now:
                    continue

                rid = r.get("reminder_id")
                target = r.get("target_user", "Семья")
                text_val = r.get("text", "")

                mention = "Влад и Диана"
                if target == "Диана" and DIANA_TELEGRAM_ID:
                    mention = f'<a href="tg://user?id={DIANA_TELEGRAM_ID}">Диана</a>'
                elif target == "Влад" and VLAD_TELEGRAM_ID:
                    mention = f'<a href="tg://user?id={VLAD_TELEGRAM_ID}">Влад</a>'

                msg = (
                    f"⏰ <b>Напоминание для {mention}:</b>\n\n"
                    f"{html.escape(text_val)}\n\n"
                    f"<i>{when.strftime('%d.%m.%Y %H:%M')} · Астана</i>"
                )
                await safe_send_message(bot, FAMILY_CHAT_ID, msg, parse_mode="HTML")

                rec = str(r.get("recurrence", "once")).lower()
                from services.sheets import mark_reminder_done
                await asyncio.to_thread(mark_reminder_done, r.get("row_idx", 2), rec, r.get("remind_at"))

            except Exception as e:
                logging.exception(f"[Deliver Reminder Error]: {e}")
