"""Обработка текстовых сообщений от пользователя (все интенты)."""

import asyncio
import datetime
import re

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram import types

from config import get_authorized_user_name
from services.categories import (
    TYPE_EXPENSE, TYPE_INCOME,
    EXPENSE_CATEGORIES, INCOME_CATEGORIES,
    FALLBACK_EXPENSE_CATEGORY, FALLBACK_INCOME_CATEGORY,
    normalize_category, normalize_subcategory,
    validate_transaction_category_subcategory,
    get_ambiguous_options,
)
from services.money import parse_amount
from services.banks import normalize_bank_source
from services.deepseek_service import parse_and_analyze
from services.sheets import (
    append_transaction, get_last_200_transactions, get_category_limits,
    get_pending_reminders, add_reminder,
    add_shopping_items, get_shopping_items, mark_shopping_items_done,
    add_trip_plan, get_planned_trips,
    get_active_subscriptions, deactivate_subscription, add_or_update_subscription,
    add_installment, get_installments, close_installment,
    split_last_transaction_by_amount, find_and_update_record, delete_record_by_keyword,
    get_transactions_for_period, find_recent_duplicate_transaction,
    debug_transactions_snapshot, normalize_necessity,
    update_last_transaction_bank_and_source,
)
from services.charts import generate_expense_chart
from services.limits_ai import generate_limits_from_history
from services.weather import get_weather_forecast
from services.telegram_safe import safe_answer
from services.pending_receipts import has_pending, pop_pending
from services.pending_clarifications import (
    set_clarification, get_clarification, pop_clarification,
)
from services.memory import get_chat_history, add_chat_message
from services.voice import transcribe_voice
from services.timezone import now_astana, parse_ru_relative_datetime
from services.analytics import analyze_budget_leaks
from services.reports import generate_pdf_report, generate_excel_export
from services.price_tracker import fetch_product_info, detect_marketplace


def _to_number_or_blank(value):
    if value is None:
        return 0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0


def _format_currency(value):
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (ValueError, TypeError):
        return str(value)


def _extract_marketplace_url(text: str) -> str | None:
    urls = re.findall(r"https?://\S+", text or "")
    for url in urls:
        clean = url.rstrip(".,);]\n")
        if detect_marketplace(clean):
            return clean
    return None


def _format_confirmation_report(tx: dict, ai_comment: str = "") -> str:
    amt = _format_currency(tx.get("amount", 0))
    curr = tx.get("currency", "KZT")
    if tx.get("resource") == "Наличные":
        bank_source = "Наличные"
    else:
        bank_source = tx.get("source") or tx.get("bank", "Не указан")

    cat = tx.get("category", "")
    comm = str(tx.get("user_comment", "") or "").strip()
    comm_str = f" ({comm})" if comm else ""

    lines = [
        "✍️ **Записано:**",
        f"• {amt} {curr} | {bank_source} | {cat}{comm_str}",
    ]
    if ai_comment and not any(bad in ai_comment.lower() for bad in ["задумалась", "повтори", "на связи"]):
        lines.append(f"\n💬 {ai_comment}")
    return "\n".join(lines)


def _build_clarification_keyboard(options: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for idx, opt in enumerate(options):
        buttons.append([InlineKeyboardButton(text=opt["label"], callback_data=f"clarify_opt:{idx}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _should_ask_clarification(text: str, parsed: dict, options: list[dict] | None) -> bool:
    """Не плодим кнопки, если категория очевидна; спрашиваем только при сомнении."""
    if not options:
        return False

    low = text.lower().replace("ё", "е")
    no_button_markers = [
        "на работу", "на работе", "в офис", "с собой", "домой", "для дома",
        "продукты домой", "такси", "налог", "штраф", "коммунал", "аренда",
        "интернет", "проезд", "автобус", "аптека", "лекарств",
    ]
    if any(m in low for m in no_button_markers):
        return False

    confidence = parsed.get("confidence")
    try:
        if confidence is not None and float(confidence) >= 0.82:
            return False
    except (TypeError, ValueError):
        pass

    return True


def _is_add_reminder_command(raw_text: str) -> bool:
    low = raw_text.lower().replace("ё", "е").strip()
    if any(x in low for x in ["покажи", "список", "какие", "удали", "удалить", "отмени", "убери"]):
        return False
    return (
        low.startswith("напомни")
        or low.startswith("напомнить")
        or "поставь напоминание" in low
        or "добавь напоминание" in low
        or "создай напоминание" in low
    )


def _extract_reminder_text(raw_text: str) -> str:
    t = raw_text.strip()
    low = t.lower().replace("ё", "е")

    markers = [
        "о том, что нужно",
        "о том что нужно",
        "о том, что",
        "о том что",
        "что нужно",
        "чтобы",
        "про то, что",
        "про то что",
    ]
    for marker in markers:
        idx = low.find(marker)
        if idx >= 0:
            return t[idx + len(marker):].strip(" .—-«»") or "Напоминание"

    cleaned = re.sub(r"(?i)\bнапомни(ть)?\b", "", t).strip()
    cleaned = re.sub(r"(?i)\b(сегодня|завтра|послезавтра)\b", "", cleaned).strip()
    cleaned = re.sub(r"(?i)\b(в|на)\s+\d{1,2}(:\d{2})?\b", "", cleaned).strip()
    cleaned = re.sub(r"(?i)\b(утра|утром|дня|днем|вечера|вечером)\b", "", cleaned).strip()
    return cleaned.strip(" .—-«»") or "Напоминание"


def _extract_reminder_target(raw_text: str, user_name: str) -> str:
    low = raw_text.lower().replace("ё", "е")

    # "у Дианы взять крем" — это задача текущему пользователю, а не Диане.
    if any(x in low for x in ["для дианы", "диане напомни", "напомни диане"]):
        return "Диана"
    if any(x in low for x in ["для влада", "владу напомни", "напомни владу", "владиславу напомни"]):
        return "Влад"

    return user_name or "Семья"


async def _try_handle_reminder_directly(message: Message, text: str, user_name: str, chat_id: int) -> bool:
    """Надёжный локальный перехват простых напоминаний.

    Это чинит кейс со скринов: бот не должен говорить "добавила", если строка
    не появилась в Reminders.
    """
    if not _is_add_reminder_command(text):
        return False

    remind_dt = parse_ru_relative_datetime(text)
    if not remind_dt:
        res = "Во сколько поставить напоминание? Например: «сегодня в 21:00» или «завтра в 9 утра»."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return True

    target = _extract_reminder_target(text, user_name)
    reminder_text = _extract_reminder_text(text)

    try:
        saved = await asyncio.to_thread(
            add_reminder,
            target,
            remind_dt.strftime("%Y-%m-%d %H:%M:%S"),
            reminder_text,
            "once",
        )
        active = await asyncio.to_thread(get_pending_reminders)
        saved_id = saved.get("reminder_id") or saved.get("id")
        exists = any((r.get("reminder_id") or r.get("id")) == saved_id for r in active)
        if not exists:
            raise RuntimeError("Напоминание добавлено, но не найдено при повторном чтении Reminders.")

        who = "тебе" if target == user_name else f"для {target}"
        res = (
            f"Готово, поставила {who} на {remind_dt.strftime('%d.%m %H:%M')} — "
            f"{reminder_text}. Проверила: запись есть в таблице."
        )
    except Exception as e:
        print(f"[Напоминания] Не удалось сохранить: {e}")
        res = (
            "Не смогла сохранить напоминание в таблицу. "
            "Я не буду делать вид, что всё записала — проверь доступ к Google Sheets."
        )

    add_chat_message(chat_id, "Ада", res)
    await safe_answer(message, res)
    return True


async def handle_category_clarification_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    data = callback.data

    if not data.startswith("clarify_opt:"):
        return

    idx = int(data.split(":")[1])
    clarification = pop_clarification(chat_id)

    if not clarification:
        await callback.answer("Уточнение устарело. Запиши заново.", show_alert=True)
        return

    options = clarification.get("options", [])
    if idx >= len(options):
        await callback.answer("Ошибка выбора.")
        return

    chosen = options[idx]
    tx = clarification["transaction"]
    tx["category"] = chosen["category"]
    tx["subcategory"] = chosen.get("subcategory", "")
    tx["necessity"] = chosen.get("necessity") or normalize_necessity(tx.get("necessity"), tx["category"])

    await asyncio.to_thread(append_transaction, tx)
    report = _format_confirmation_report(tx, f"Категория выбрана: {chosen['label']}.")
    add_chat_message(chat_id, "Ада", report)

    try:
        await callback.message.edit_text(report)
    except Exception:
        try:
            await callback.message.edit_text(report, parse_mode=None)
        except Exception:
            pass

    await callback.answer("Записано!")


async def handle_text(message: Message):
    await _process_text_message(message, message.text or "")


async def handle_voice(message: Message):
    voice = message.voice or message.audio
    if not voice:
        return
    try:
        file = await message.bot.get_file(voice.file_id)
        downloaded = await message.bot.download_file(file.file_path)
        file_bytes = downloaded.read()
    except Exception as e:
        await safe_answer(message, "Не удалось скачать голосовое сообщение.")
        return

    ext = "oga"
    if file.file_path and "." in file.file_path:
        ext = file.file_path.rsplit(".", 1)[-1]

    text = await transcribe_voice(file_bytes, filename=f"voice.{ext}")
    if not text:
        await safe_answer(message, "Не удалось распознать голосовое сообщение.")
        return

    await safe_answer(message, f"🎤 «{text}»")
    await _process_text_message(message, text)


async def _process_text_message(message: Message, text: str):
    user_name = get_authorized_user_name(message.from_user.id, message.from_user.first_name) or message.from_user.first_name or "Пользователь"
    chat_id = message.chat.id
    add_chat_message(chat_id, user_name, text)
    t_clean = text.strip().lower()

    # 1. Чек ожидает комментария
    if has_pending(chat_id):
        pending = pop_pending(chat_id)
        if pending:
            transactions, receipt_user = pending
            lines = ["📸 **Записано по чеку:**"]
            for tx in transactions:
                original_item = str(tx.get("user_comment", "") or "").strip()
                if original_item and original_item != text:
                    tx["user_comment"] = f"{original_item} ({text})"
                else:
                    tx["user_comment"] = text
                tx["user"] = receipt_user
                await asyncio.to_thread(append_transaction, tx)
                lines.append(
                    f"• {_format_currency(tx.get('amount'))} {tx.get('currency')} | "
                    f"{tx.get('bank')} | {tx.get('category')} ({tx['user_comment']})"
                )
            rep = "\n".join(lines)
            add_chat_message(chat_id, "Ада", rep)
            await safe_answer(message, rep)
            return

    # 2. Перехват ответа на уточнение
    clarification = get_clarification(chat_id)
    if clarification:
        matched_opt = None
        for opt in clarification.get("options", []):
            words = re.findall(r'\w+', opt["label"].lower())
            if any(w in t_clean for w in words if len(w) > 3):
                matched_opt = opt
                break
        if not matched_opt:
            if any(k in t_clean for k in ["работ", "кафе", "собой", "перекус", "офис", "зал", "спорт"]):
                matched_opt = clarification["options"][0]
            elif any(k in t_clean for k in ["дом", "продукт", "семь", "каждый день", "обычн"]):
                matched_opt = clarification["options"][-1]

        if matched_opt:
            pop_clarification(chat_id)
            tx = clarification["transaction"]
            tx["category"] = matched_opt["category"]
            tx["subcategory"] = matched_opt.get("subcategory", "")
            tx["user_comment"] = f"{tx.get('user_comment', '')} ({text})".strip()
            tx["necessity"] = matched_opt.get("necessity") or normalize_necessity(tx.get("necessity"), tx["category"])
            await asyncio.to_thread(append_transaction, tx)
            report = _format_confirmation_report(tx, f"Поняла, это {matched_opt['label']}!")
            add_chat_message(chat_id, "Ада", report)
            await safe_answer(message, report)
            return

    # Прямой надёжный перехват добавления напоминаний — без зависимости от ИИ.
    # Стоит после pending receipt/clarification, чтобы не украсть комментарий к чеку.
    if await _try_handle_reminder_directly(message, text, user_name, chat_id):
        return

    # ── ПРЯМОЙ ПЕРЕХВАТ 0: МГНОВЕННАЯ СМЕНА БАНКА ПОСЛЕДНЕЙ ТРАТЫ ──
    bank_match = re.search(r'(?:измени|поменяй|запиши|исправь)?\s*(?:что\s+это\s+)?(?:оплата\s+через|банк\s+на|карту\s+на|с\s+карты|банк)\s+([a-zA-Zа-яА-Я]+)', t_clean)
    if bank_match:
        target_bank = bank_match.group(1).upper()
        if target_bank in ["BCC", "KASPI", "HALYK", "FORTE", "FREEDOM", "НАЛОМ", "НАЛИЧНЫЕ", "БЦК", "КАСПИ"]:
            if target_bank in ["БЦК"]: target_bank = "BCC"
            if target_bank in ["КАСПИ"]: target_bank = "Kaspi"
            if target_bank in ["НАЛОМ", "НАЛИЧНЫЕ"]: target_bank = "Наличные"

            updated_rec = await asyncio.to_thread(update_last_transaction_bank_and_source, target_bank)
            if updated_rec:
                res = f"✅ Исправила в последней записи ({updated_rec.get('user_comment')}, {_format_currency(updated_rec.get('amount'))} KZT): банк изменён на **{updated_rec.get('bank')}** ({updated_rec.get('source')})."
            else:
                res = "Не нашла последнюю запись в таблице для изменения банка."
            add_chat_message(chat_id, "Ада", res)
            await safe_answer(message, res)
            return

    # ── ПРЯМОЙ ПЕРЕХВАТ 1: ДЕТЕКТОР УТЕЧЕК БЮДЖЕТА ──
    if t_clean in {"/leaks", "утечки"} or any(k in t_clean for k in ["утечки бюджета", "микротраты", "куда уходят деньги", "куда утекают деньги", "мелкие траты", "на что уходит мелочь"]):
        leak_data = await asyncio.to_thread(analyze_budget_leaks)
        add_chat_message(chat_id, "Ада", leak_data["text"])
        await safe_answer(message, leak_data["text"])
        return

    # ── ПРЯМОЙ ПЕРЕХВАТ 2: ГЕНЕРАЦИЯ PDF-ОТЧЁТА ──
    if t_clean in {"/report", "pdf"} or any(k in t_clean for k in ["отчет в pdf", "отчёт в pdf", "выгрузи отчет в pdf", "скачать pdf"]):
        try:
            await message.bot.send_chat_action(chat_id=chat_id, action="upload_document")
            pdf_bytes = await asyncio.to_thread(generate_pdf_report)
            pdf_file = BufferedInputFile(pdf_bytes, filename=f"Finance_Report_{now_astana().strftime('%Y_%m')}.pdf")
            await message.answer_document(document=pdf_file, caption="📑 Ваш официальный семейный финансовый отчёт за месяц.")
            return
        except Exception as e:
            print(f"[PDF Report] Ошибка: {e}")
            await safe_answer(message, "Не удалось сформировать PDF-отчёт.")
            return

    # ── ПРЯМОЙ ПЕРЕХВАТ 3: ЭКСПОРТ В EXCEL (.XLSX) ──
    if t_clean in {"/export", "excel"} or any(k in t_clean for k in ["выгрузи в excel", "экспорт в excel", "скачать выписку", "выгрузи выписку", "скачать excel"]):
        try:
            await message.bot.send_chat_action(chat_id=chat_id, action="upload_document")
            excel_bytes = await asyncio.to_thread(generate_excel_export)
            excel_file = BufferedInputFile(excel_bytes, filename=f"Family_Finance_{now_astana().strftime('%Y_%m')}.xlsx")
            await message.answer_document(document=excel_file, caption="📊 Ваша полная выписка в формате Excel со всеми операциями.")
            return
        except Exception as e:
            print(f"[Excel Export] Ошибка: {e}")
            await safe_answer(message, "Не удалось сформировать файл Excel.")
            return

    # ── ПРЯМОЙ ПЕРЕХВАТ 4: ГРАФИКИ ──
    if any(k in t_clean for k in ["график", "диаграмм", "чарт", "дашборд"]) or t_clean in {"/chart", "chart"}:
        try:
            image_bytes = await asyncio.to_thread(generate_expense_chart)
            if image_bytes:
                photo_file = BufferedInputFile(image_bytes, filename="chart.png")
                await message.answer_photo(photo=photo_file, caption="📊 Финансовый дашборд за текущий месяц.")
            else:
                await safe_answer(message, "Нет данных для построения графика.")
        except Exception as error:
            print(f"[График] Ошибка: {error}")
            await safe_answer(message, "Не удалось построить график.")
        return

    # ── ПРЯМОЙ ПЕРЕХВАТ 5: ПОГОДА ──
    if any(k in t_clean for k in ["погода", "погоду", "прогноз", "зонт"]):
        target = "today"
        if "недел" in t_clean or "5 дней" in t_clean or "выходн" in t_clean:
            target = "week"
        elif "послезавтра" in t_clean:
            target = "after_tomorrow"
        elif "завтра" in t_clean:
            target = "tomorrow"

        forecast = await get_weather_forecast(target=target)
        res = forecast or "Не удалось связаться с погодной станцией Open-Meteo."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # ── ПРЯМОЙ ПЕРЕХВАТ 6: ДИАГНОСТИКА ──
    if t_clean in {"debug", "/debug"}:
        debug_text = await asyncio.to_thread(debug_transactions_snapshot)
        await safe_answer(message, f"```\n{debug_text}\n```")
        return

    # ── ПРЯМОЙ ПЕРЕХВАТ 7: РАЗДЕЛИТЬ ТРАНЗАКЦИЮ ──
    if "раздели" in t_clean and "транзакцию" in t_clean:
        match = re.search(r"раздели\s+транзакцию\s+(\d+)[\s:]*(.+?)[\s]*\|\s*(.+?)", text, re.IGNORECASE)
        if match:
            target_amount = float(match.group(1))
            part1_desc = match.group(2).strip()
            part2_desc = match.group(3).strip()
            p1_match = re.match(r"(.+?)\s*[-—]\s*(\d+)", part1_desc)
            p2_match = re.match(r"(.+?)\s*[-—]\s*(\d+)", part2_desc)
            if p1_match and p2_match:
                part1_cat = normalize_category(p1_match.group(1).strip(), EXPENSE_CATEGORIES, FALLBACK_EXPENSE_CATEGORY)
                part1_amt = float(p1_match.group(2))
                part2_cat = normalize_category(p2_match.group(1).strip(), EXPENSE_CATEGORIES, FALLBACK_EXPENSE_CATEGORY)
                part2_amt = float(p2_match.group(2))
                success = await asyncio.to_thread(
                    split_last_transaction_by_amount,
                    target_amount, part1_amt, part1_cat, part1_desc,
                    part2_amt, part2_cat, part2_desc
                )
                res = f"Разделила: {part1_cat} — {part1_amt} тг, {part2_cat} — {part2_amt} тг." if success else "Не нашла такую транзакцию."
                add_chat_message(chat_id, "Ада", res)
                await safe_answer(message, res)
                return
        await safe_answer(message, "Формат: раздели транзакцию <сумма>: <категория> — <сумма> | <категория> — <сумма>")
        return

    # ── ПРЯМОЙ ПЕРЕХВАТ 8: ЦЕНЫ ПО ССЫЛКЕ ──
    product_url = _extract_marketplace_url(text)
    if product_url:
        try:
            await message.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        info = await fetch_product_info(product_url)
        if info:
            stock = "в наличии" if info.get("in_stock") else "нет в наличии"
            price = _format_currency(info.get("price", 0)) if info.get("price") else "цена не найдена"
            res = (
                f"🛒 **{info.get('marketplace')}**\n"
                f"{info.get('title')}\n"
                f"Цена: {price} тг\n"
                f"Статус: {stock}\n"
                f"{info.get('url')}"
            )
        else:
            res = "Не смогла разобрать ссылку на товар."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    try:
        await message.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass

    # ── ЗАПРОС К ИИ ──
    (
        history,
        limits,
        reminders,
        shopping_list,
        trips,
        subscriptions,
        installments,
    ) = await asyncio.gather(
        asyncio.to_thread(get_last_200_transactions),
        asyncio.to_thread(get_category_limits),
        asyncio.to_thread(get_pending_reminders),
        asyncio.to_thread(get_shopping_items),
        asyncio.to_thread(get_planned_trips),
        asyncio.to_thread(get_active_subscriptions),
        asyncio.to_thread(get_installments),
    )
    chat_history = get_chat_history(chat_id)

    parsed = await parse_and_analyze(
        user_text=text, user_name=user_name,
        history=history, chat_history=chat_history,
        shopping_list=shopping_list, limits=limits,
        reminders=reminders, trips=trips,
        subscriptions=subscriptions, installments=installments,
    )

    intent = parsed.get("intent", "chat")
    reply = parsed.get("reply", "")

    # Блокировка кнопок при командах удаления
    is_delete_or_edit_command = any(k in t_clean for k in ["удали", "удалить", "поменяй", "измени", "исправь", "замени", "отмени"])

    ambig_options = parsed.get("clarification_options") or get_ambiguous_options(text)
    if (
        not is_delete_or_edit_command
        and (intent in {"need_clarification", "transaction"} or parse_amount(text) > 0)
        and _should_ask_clarification(text, parsed, ambig_options)
    ):
        tx = parsed.get("transaction") or {}
        if not tx.get("amount"):
            tx["amount"] = parse_amount(text)
        if not tx.get("currency"):
            tx["currency"] = "KZT"
        if not tx.get("type"):
            tx["type"] = TYPE_EXPENSE
        if "нал" in text.lower():
            tx["resource"] = "Наличные"
            tx["bank"] = "Не указан"
            tx["source"] = "Основная карта"
        else:
            tx["source"] = normalize_bank_source(tx.get("bank"), tx.get("source"))
        tx["user"] = user_name
        tx["user_comment"] = text

        amt_str = _format_currency(tx.get("amount", 0))
        curr = tx.get("currency", "KZT")
        res_label = ", наличные" if tx.get("resource") == "Наличные" else ""

        if not reply or any(bad in reply.lower() for bad in ["задумалась", "повтори", "на связи", "ошибка"]):
            prompt_text = f"Куда запишем эту покупку ({amt_str} {curr}{res_label})?"
        else:
            prompt_text = reply

        kb = _build_clarification_keyboard(ambig_options)
        set_clarification(chat_id, tx, ambig_options)
        add_chat_message(chat_id, "Ада", prompt_text)
        await message.answer(prompt_text, reply_markup=kb)
        return

    # Если ИИ/локальные правила пометили как need_clarification, но код решил,
    # что ситуация очевидна и кнопки не нужны — записываем как обычную транзакцию.
    if intent == "need_clarification" and (parsed.get("transaction") or {}).get("amount"):
        intent = "transaction"

    # ── УДАЛЕНИЕ И ПРАВКА ЗАПИСЕЙ ──
    if intent in {"delete_transaction", "correct_any_record"} or is_delete_or_edit_command:
        updates = parsed.get("updates", [])
        if updates:
            results = []
            for upd in updates:
                worksheet = upd.get("worksheet", "Transactions")
                search_query = upd.get("search_query", "")
                action = upd.get("action", "update")
                if action == "delete":
                    deleted = await asyncio.to_thread(delete_record_by_keyword, worksheet, search_query)
                    results.append("удалила" if deleted else "не нашла")
                else:
                    column = upd.get("column_to_update", "")
                    new_value = upd.get("new_value", "")
                    updated = await asyncio.to_thread(find_and_update_record, worksheet, search_query, column, new_value)
                    results.append("обновила" if updated else "не нашла")
            res = reply or f"Результат: {', '.join(results)}."
            add_chat_message(chat_id, "Ада", res)
            await safe_answer(message, res)
            return

        query = parsed.get("search_query", "") or text
        deleted = await asyncio.to_thread(delete_record_by_keyword, "Transactions", query)
        if deleted:
            res = f"Удалила покупку: {deleted.get('category')} на {_format_currency(deleted.get('amount'))} тг ({deleted.get('user_comment')})."
        else:
            res = reply or "Не нашла такую запись в таблице для удаления."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # ── СВОДКА И БАЛАНС ЗА МЕСЯЦ ──
    if intent == "get_summary":
        now = now_astana()
        start = now.replace(day=1).strftime("%Y-%m-%d")
        if now.month == 12:
            end = now.replace(year=now.year + 1, month=1, day=1).strftime("%Y-%m-%d")
        else:
            end = now.replace(month=now.month + 1, day=1).strftime("%Y-%m-%d")

        transactions = await asyncio.to_thread(get_transactions_for_period, start, end)
        total_income = 0.0
        total_expense = 0.0
        by_category = {}
        for t in transactions:
            amt = _to_number_or_blank(t.get("amt"))
            t_type = str(t.get("type") or TYPE_EXPENSE)
            if t_type == TYPE_INCOME:
                total_income += amt
            else:
                total_expense += amt
                cat = str(t.get("cat") or "Прочее")
                by_category[cat] = by_category.get(cat, 0) + amt

        lines = [f"📊 **Сводка за {now.strftime('%B %Y')}:**"]
        lines.append(f"💰 Доходы: {_format_currency(total_income)} тг")
        lines.append(f"💸 Расходы: {_format_currency(total_expense)} тг")
        lines.append(f"📈 Баланс: {_format_currency(total_income - total_expense)} тг\n")
        lines.append("📉 **По категориям:**")
        if by_category:
            for cat, amt in sorted(by_category.items(), key=lambda x: -x[1]):
                lines.append(f"  - {cat}: {_format_currency(amt)} тг")
        else:
            lines.append("  _(В этом месяце расходов ещё не записано)_")

        res = "\n".join(lines)
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # ТРАНЗАКЦИЯ (ОДНОЗНАЧНАЯ)
    if intent == "transaction":
        tx = parsed.get("transaction", {})
        if not tx:
            await safe_answer(message, reply or "Не поняла, что записывать.")
            return

        tx_type = str(tx.get("type") or TYPE_EXPENSE).strip().upper()
        valid_categories = INCOME_CATEGORIES if tx_type == TYPE_INCOME else EXPENSE_CATEGORIES
        fallback_cat = FALLBACK_INCOME_CATEGORY if tx_type == TYPE_INCOME else FALLBACK_EXPENSE_CATEGORY

        cat = normalize_category(tx.get("category"), valid_categories, fallback_cat)
        tx["category"] = cat
        _, valid_sub = validate_transaction_category_subcategory(cat, tx.get("subcategory"), valid_categories, fallback_cat)
        tx["subcategory"] = valid_sub or normalize_subcategory(None, cat, "")
        tx["necessity"] = normalize_necessity(tx.get("necessity"), cat)
        tx["source"] = normalize_bank_source(tx.get("bank"), tx.get("source"))

        if not tx.get("bank"): tx["bank"] = "Не указан"
        if not tx.get("currency"): tx["currency"] = "KZT"
        if not tx.get("funds_type"): tx["funds_type"] = "Собственные"
        if not tx.get("resource"): tx["resource"] = "Карта"
        tx["user"] = user_name
        if not tx.get("merchant"): tx["merchant"] = ""
        if not tx.get("user_comment"): tx["user_comment"] = text
        if not tx.get("ai_comment"): tx["ai_comment"] = reply or ""

        amount = parse_amount(tx.get("amount", 0))
        duplicate = await asyncio.to_thread(find_recent_duplicate_transaction, amount, text)
        if duplicate:
            dup_warning = f"⚠️ _Записала, но похоже на недавний дубль ({_format_currency(amount)} тг в {str(duplicate.get('date', ''))[-8:]})._"
            reply = f"{reply}\n\n{dup_warning}" if reply else dup_warning

        try:
            await asyncio.to_thread(append_transaction, tx)
        except Exception:
            await safe_answer(message, "Не смогла записать трату в таблицу. Не буду делать вид, что записала — проверь Google Sheets.")
            return
        report = _format_confirmation_report(tx, reply)
        add_chat_message(chat_id, "Ада", report)
        await safe_answer(message, report)
        return

    # РАССРОЧКИ
    if intent == "add_installment":
        data = parsed.get("installment", {})
        if data:
            await asyncio.to_thread(add_installment, data)
        res = reply or "Записала рассрочку."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "close_installment":
        query = parsed.get("search_query", "")
        if query:
            await asyncio.to_thread(close_installment, query)
        res = reply or "Закрыла рассрочку."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "get_installments":
        items = await asyncio.to_thread(get_installments)
        if items:
            lines = ["📋 **Активные рассрочки:**"]
            for item in items:
                lines.append(
                    f"- {item.get('description')} ({item.get('bank')}): "
                    f"{_format_currency(item.get('total_amount'))} тг, "
                    f"{_format_currency(item.get('monthly_payment'))} тг/мес"
                )
            res = "\n".join(lines)
        else:
            res = "Нет активных рассрочек."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # ПОДПИСКИ
    if intent == "add_subscription":
        sub = parsed.get("subscription") or {}
        name = sub.get("name") or parsed.get("subscription_name", "")
        amount = parse_amount(sub.get("amount", parsed.get("amount", 0)))
        bank = sub.get("bank") or parsed.get("bank", "Не указан")
        day = int(parse_amount(sub.get("day_of_month", parsed.get("day_of_month", now_astana().day))) or now_astana().day)
        if name and amount > 0:
            try:
                saved = await asyncio.to_thread(add_or_update_subscription, name, amount, bank, max(1, min(day, 31)))
                res = reply or f"Записала подписку {saved.get('name', name)}: {_format_currency(amount)} тг/мес, день списания — {max(1, min(day, 31))}."
            except Exception as e:
                print(f"[Подписки] Ошибка сохранения: {e}")
                res = "Не смогла сохранить подписку в таблицу. Проверь Google Sheets."
        else:
            res = reply or "Не хватает названия или суммы подписки."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "cancel_subscription":
        name = parsed.get("subscription_name", "")
        if name:
            await asyncio.to_thread(deactivate_subscription, name)
        res = reply or "Отменила подписку."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "get_subscriptions":
        items = await asyncio.to_thread(get_active_subscriptions)
        if items:
            lines = ["📋 **Активные подписки:**"]
            for item in items:
                lines.append(f"- {item.get('name')}: {_format_currency(item.get('amount'))} тг/мес ({item.get('bank')})")
            res = "\n".join(lines)
        else:
            res = "Нет активных подписок."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # НАПОМИНАНИЯ
    if intent == "add_reminder":
        target = parsed.get("reminder_target", user_name)
        times = parsed.get("reminder_times", [])
        text_rem = parsed.get("reminder_text", "")
        recurrence = parsed.get("recurrence", "once")
        if not times:
            await safe_answer(message, reply or "Во сколько поставить напоминание?")
            return
        try:
            saved_ids = []
            for time_str in times:
                saved = await asyncio.to_thread(add_reminder, target, time_str, text_rem, recurrence)
                saved_ids.append(saved.get("reminder_id") or saved.get("id"))
            active = await asyncio.to_thread(get_pending_reminders)
            active_ids = {(r.get("reminder_id") or r.get("id")) for r in active}
            if any(saved_id not in active_ids for saved_id in saved_ids if saved_id):
                raise RuntimeError("Не все напоминания найдены после записи.")
        except Exception as e:
            print(f"[Напоминания] Ошибка сохранения: {e}")
            await safe_answer(message, "Не смогла сохранить напоминание в таблицу. Не буду врать, что записала — проверь Google Sheets.")
            return
        res = reply or "Поставила напоминание и проверила, что оно сохранилось."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "delete_reminder":
        query = parsed.get("search_query", "")
        if query:
            await asyncio.to_thread(delete_record_by_keyword, "Reminders", query)
        res = reply or "Удалила напоминание."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "get_reminders":
        items = await asyncio.to_thread(get_pending_reminders)
        if items:
            lines = ["📋 **Активные напоминания:**"]
            for item in items:
                lines.append(f"- {item.get('target_user')}: {item.get('text')} ({item.get('remind_at')})")
            res = "\n".join(lines)
        else:
            res = "Нет активных напоминаний."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # СПИСОК ПОКУПОК
    if intent == "add_shopping":
        items = parsed.get("shopping_items", [])
        if items:
            await asyncio.to_thread(add_shopping_items, items, user_name)
        res = reply or "Добавила в список покупок."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "clear_shopping":
        items = parsed.get("shopping_items", [])
        if items:
            await asyncio.to_thread(mark_shopping_items_done, items)
        res = reply or "Убрала из списка."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "get_shopping":
        items = await asyncio.to_thread(get_shopping_items)
        if items:
            lines = ["🛒 **Список покупок:**"]
            for item in items:
                lines.append(f"- {item.get('item')}")
            res = "\n".join(lines)
        else:
            res = "Список покупок пуст."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # ПОЕЗДКИ
    if intent == "add_trip":
        destination = parsed.get("destination", "")
        dates = parsed.get("dates", "")
        budget = _to_number_or_blank(parsed.get("budget", 0))
        notes = parsed.get("notes", "")
        if destination:
            await asyncio.to_thread(add_trip_plan, destination, dates, budget, notes)
        res = reply or "Записала поездку."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "get_trips":
        items = await asyncio.to_thread(get_planned_trips)
        if items:
            lines = ["✈️ **Запланированные поездки:**"]
            for item in items:
                lines.append(f"- {item.get('destination')} ({item.get('dates')}): {_format_currency(item.get('budget'))} тг")
            res = "\n".join(lines)
        else:
            res = "Нет запланированных поездок."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # ЛИМИТЫ
    if intent == "get_limits":
        current_limits = await asyncio.to_thread(get_category_limits)
        if current_limits:
            lines = ["📊 **Текущие лимиты:**"]
            for cat, limit in current_limits.items():
                lines.append(f"- {cat}: {_format_currency(limit)} тг")
            res = "\n".join(lines)
        else:
            res = "Лимиты не заданы."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "generate_limits":
        try:
            new_limits = await generate_limits_from_history()
            if new_limits:
                lines = ["📊 **Сгенерированные лимиты:**"]
                for cat, limit in new_limits.items():
                    lines.append(f"- {cat}: {_format_currency(limit)} тг")
                res = "\n".join(lines)
            else:
                res = "Недостаточно данных для генерации лимитов."
        except Exception as error:
            print(f"[Лимиты] Ошибка генерации: {error}")
            res = "Не удалось сгенерировать лимиты."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # ДОХОДЫ
    if intent == "get_income":
        now = now_astana()
        start = now.replace(day=1).strftime("%Y-%m-%d")
        if now.month == 12:
            end = now.replace(year=now.year + 1, month=1, day=1).strftime("%Y-%m-%d")
        else:
            end = now.replace(month=now.month + 1, day=1).strftime("%Y-%m-%d")

        transactions = await asyncio.to_thread(get_transactions_for_period, start, end)
        incomes = [t for t in transactions if str(t.get("type")) == TYPE_INCOME]
        total = sum(_to_number_or_blank(t.get("amt")) for t in incomes)
        lines = [f"💰 **Доходы за {now.strftime('%B %Y')}:** {_format_currency(total)} тг"]
        for t in incomes:
            lines.append(f"  - {t.get('cat')}: {_format_currency(t.get('amt'))} тг ({t.get('comm')})")
        res = "\n".join(lines)
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # ОБЫЧНЫЙ ЧАТ
    res = reply or "Чем могу помочь?"
    add_chat_message(chat_id, "Ада", res)
    await safe_answer(message, res)
