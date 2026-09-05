from services.timezone import month_label
"""Обработка текстовых сообщений от пользователя (все интенты)."""

import asyncio
import datetime
import re

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram import types

from config import get_authorized_user_name
from services import reminders as reminder_service, debts, state
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
from services.pending_receipts import has_pending, pop_pending, ack_pending
from services.pending_clarifications import (
    set_clarification, get_clarification, pop_clarification, ack_clarification,
)
from services.memory import get_chat_history, add_chat_message
from services.voice import transcribe_voice
from services.timezone import now_astana, parse_ru_relative_datetime
from services.analytics import analyze_budget_leaks
from services.reports import generate_pdf_report, generate_excel_export
from services.price_tracker import fetch_product_info, detect_marketplace


def _to_number_or_blank(value):
    return parse_amount(value)


def _format_currency(value):
    from services.money import parse_amount
    amount = parse_amount(value)
    return f"{amount:,.{0 if amount.is_integer() else 2}f}".replace(",", " ")


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


def _build_clarification_keyboard(options: list[dict], token: str) -> InlineKeyboardMarkup:
    buttons = []
    for idx, opt in enumerate(options):
        buttons.append([InlineKeyboardButton(text=opt["label"], callback_data=f"clarify_opt:{token}:{idx}")])
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


async def handle_category_clarification_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    data = callback.data

    if not data.startswith("clarify_opt:"):
        return

    key = state.dialogue_key(chat_id, callback.from_user.id)
    clarification = get_clarification(key)
    parts = data.split(":")
    if not clarification or len(parts) != 3 or parts[1] != clarification.get("token"):
        await callback.answer("Это чужое или устаревшее уточнение.", show_alert=True)
        return
    try:
        idx = int(parts[2])
    except ValueError:
        await callback.answer("Неверный выбор."); return
    options = clarification.get("options", [])
    if idx < 0 or idx >= len(options):
        await callback.answer("Неверный выбор."); return

    chosen = options[idx]
    tx = clarification["transaction"]
    tx["category"] = chosen["category"]
    tx["subcategory"] = chosen.get("subcategory", "")
    tx["necessity"] = chosen.get("necessity") or normalize_necessity(tx.get("necessity"), tx["category"])

    await asyncio.to_thread(append_transaction, tx)
    ack_clarification(key)
    report = _format_confirmation_report(tx, f"Категория выбрана: {chosen['label']}.")

    try:
        await callback.message.edit_text(report)
    except Exception:
        try:
            await callback.message.edit_text(report, parse_mode=None)
        except Exception:
            pass

    add_chat_message(chat_id, "Ада", report)
    await callback.answer("Записано!")


async def handle_text(message: Message):
    await _process_text_message(message, message.text or "")


async def handle_voice(message: Message):
    voice = message.voice or message.audio
    if not voice:
        return
    if (voice.file_size or 0) > 20 * 1024 * 1024:
        await safe_answer(message, "Аудио слишком большое. Максимум 20 МБ."); return
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
    if not text.startswith("/"):
        add_chat_message(chat_id, user_name, text)
    t_clean = text.strip().lower()
    key = state.dialogue_key(chat_id, message.from_user.id)

    # Explicit commands have precedence over a receipt comment/draft.
    try:
        if await reminder_service.handle(message, text, user_name):
            return
        if await debts.handle(message, text, user_name):
            return
    except Exception:
        import logging
        logging.exception("Could not persist family action")
        await safe_answer(message, "Не удалось подтвердить сохранение. Проверь список перед повтором; "
                          "ошибка доступа к таблице не означает, что записи нет.")
        return


    # Drafts are in LLM context; unrelated speech must not be consumed as a receipt comment.
    from services import edits
    if await edits.handle_confirmation(message, text):
        return

    if has_pending(key) and t_clean in {"без комментария", "без комментариев", "сохрани чек"}:
        await resolve_pending(message, {"intent":"receipt_comment", "comment":"без комментария"}, text)
        return

    # ── ПРЯМОЙ ПЕРЕХВАТ 0: МГНОВЕННАЯ СМЕНА БАНКА ПОСЛЕДНЕЙ ТРАТЫ ──
    bank_match = re.fullmatch(r'(?:измени|поменяй|исправь)\s+(?:банк|карту)(?:\s+(?:в\s+)?последн\w+\s+(?:записи|трате|покупке))?\s+(?:на\s+)?([a-zA-Zа-яА-Я]+)[.!]?', t_clean)
    if bank_match:
        target_bank = bank_match.group(1).upper()
        if target_bank in ["BCC", "KASPI", "HALYK", "FORTE", "FREEDOM", "НАЛОМ", "НАЛИЧНЫЕ", "БЦК", "КАСПИ", "ХАЛЫК", "ФОРТЕ", "ФРИДОМ", "НАРОДНЫЙ"]:
            if target_bank in ["БЦК"]: target_bank = "BCC"
            if target_bank in ["КАСПИ"]: target_bank = "Kaspi"
            if target_bank in ["НАЛОМ", "НАЛИЧНЫЕ"]: target_bank = "Наличные"

            updated_rec = await asyncio.to_thread(update_last_transaction_bank_and_source, target_bank, user_name)
            if updated_rec:
                res = f"✅ Исправила в последней записи ({updated_rec.get('user_comment')}, {_format_currency(updated_rec.get('amount'))} KZT): банк изменён на **{updated_rec.get('bank')}** ({updated_rec.get('source')})."
            else:
                res = "Не нашла последнюю запись в таблице для изменения банка."
            await safe_answer(message, res)
            return

    # ── ПРЯМОЙ ПЕРЕХВАТ 1: ДЕТЕКТОР УТЕЧЕК БЮДЖЕТА ──
    if t_clean in {"/leaks", "утечки"} or any(k in t_clean for k in ["утечки бюджета", "микротраты", "куда уходят деньги", "куда утекают деньги", "мелкие траты", "на что уходит мелочь"]):
        leak_data = await asyncio.to_thread(analyze_budget_leaks)
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
    if (any(k in t_clean for k in ["погода", "погоду", "прогноз погоды"])
            and not re.search(r"купил|купила|потратил|оплатил|добавь", t_clean)):
        target = "today"
        if "недел" in t_clean or "5 дней" in t_clean or "выходн" in t_clean:
            target = "week"
        elif "послезавтра" in t_clean:
            target = "after_tomorrow"
        elif "завтра" in t_clean:
            target = "tomorrow"

        forecast = await get_weather_forecast(target=target)
        res = forecast or "Не удалось связаться с погодной станцией Open-Meteo."
        await safe_answer(message, res)
        return

    # ── ПРЯМОЙ ПЕРЕХВАТ 6: ДИАГНОСТИКА ──
    if t_clean in {"debug", "/debug"}:
        debug_text = await asyncio.to_thread(debug_transactions_snapshot)
        await safe_answer(message, f"```\n{debug_text}\n```")
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
        await safe_answer(message, res)
        return

    try:
        await message.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass

    # Complete conversational context, including drafts, debts and today's full history.
    from services.context import collect
    context = await collect(message)
    chat_history = get_chat_history(chat_id)
    parsed = await parse_and_analyze(user_text=text, user_name=user_name,
                                     chat_history=chat_history, **context)

    intent = parsed.get("intent", "chat")
    reply = parsed.get("reply", "")

    if await reminder_service.handle_model(message, parsed, user_name):
        return
    if await debts.handle_model(message, parsed, user_name):
        return
    if intent in {"receipt_comment", "resolve_clarification"}:
        await resolve_pending(message, parsed, text)
        return
    if intent == "get_weather":
        target = parsed.get("weather_target", "today")
        if target not in {"today","tomorrow","after_tomorrow","week"}:
            target = "today"
        forecast = await get_weather_forecast(target=target)
        await safe_answer(message, forecast or "Погодный сервис сейчас недоступен.")
        return
    if intent == "split_transaction":
        split = parsed.get("split") or {}
        parts = split.get("parts") or []
        if len(parts) != 2:
            await safe_answer(message,"Укажи две категории и суммы частей."); return
        try:
            success = await asyncio.to_thread(split_last_transaction_by_amount,
                parse_amount(split.get("amount")), parse_amount(parts[0].get("amount")),
                parts[0].get("category"), parts[0].get("comment",""),
                parse_amount(parts[1].get("amount")), parts[1].get("category"), parts[1].get("comment",""),
                split.get("transaction_id",""), f"SPLIT_{chat_id}_{message.message_id}")
        except ValueError:
            success = False
        await safe_answer(message, "✅ Разделила операцию на две категории." if success else
                          "Не удалось разделить: нужна одна исходная запись и точное совпадение суммы частей.")
        return

    # Блокировка кнопок при командах удаления
    is_delete_or_edit_command = any(k in t_clean for k in ["удали", "удалить", "поменяй", "измени", "исправь", "замени", "отмени"])

    ambig_options = parsed.get("clarification_options") or get_ambiguous_options(text)
    if (
        not is_delete_or_edit_command
        and (intent in {"need_clarification", "transaction"})
        and parse_amount((parsed.get("transaction") or {}).get("amount", 0)) > 0
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

        tx["transaction_id"] = f"TG_{chat_id}_{message.message_id}_0"
        older = get_clarification(key)
        if older and older.get("transaction", {}).get("transaction_id") != tx["transaction_id"]:
            await asyncio.to_thread(append_transaction, older["transaction"])
            ack_clarification(key)
            await safe_answer(message, "Предыдущую ожидающую категорию сохранила в её резервную категорию, чтобы не потерять операцию.")
        token = set_clarification(key, tx, ambig_options)
        kb = _build_clarification_keyboard(ambig_options, token)
        await safe_answer(message, prompt_text, reply_markup=kb)
        return

    # Если ИИ/локальные правила пометили как need_clarification, но код решил,
    # что ситуация очевидна и кнопки не нужны — записываем как обычную транзакцию.
    if intent == "need_clarification" and (parsed.get("transaction") or {}).get("amount"):
        intent = "transaction"

    # Newest-first keyword lookup retained, but mutation is confirmed by stable ID.
    if intent in {"delete_transaction", "correct_any_record"}:
        # Support legacy model output targeting Reminders through common editor.
        updates = parsed.get("updates") or []
        reminder_updates = [op for op in updates if op.get("worksheet") == "Reminders"]
        if reminder_updates:
            if len(reminder_updates) != len(updates) or any(op.get("action") != "delete" for op in reminder_updates):
                await safe_answer(message,"Изменяй напоминания отдельным запросом."); return
            await reminder_service.handle_model(message, {"intent":"delete_reminder",
                "search_query": reminder_updates[0].get("search_query","")},user_name)
            return
        await edits.propose(message, parsed)
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

        lines = [f"📊 **Сводка за {month_label(now)}:**"]
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
        await safe_answer(message, res)
        return

    # ТРАНЗАКЦИЯ (ОДНОЗНАЧНАЯ)
    if intent == "transaction":
        tx = parsed.get("transaction", {})
        if not tx:
            await safe_answer(message, reply or "Не поняла, что записывать.")
            return

        tx["transaction_id"] = f"TG_{chat_id}_{message.message_id}_0"
        tx_type = str(tx.get("type") or TYPE_EXPENSE).strip().upper()
        tx_type = TYPE_INCOME if tx_type in {"INCOME", "ДОХОД"} else TYPE_EXPENSE
        tx["type"] = tx_type
        valid_categories = INCOME_CATEGORIES if tx_type == TYPE_INCOME else EXPENSE_CATEGORIES
        fallback_cat = FALLBACK_INCOME_CATEGORY if tx_type == TYPE_INCOME else FALLBACK_EXPENSE_CATEGORY

        cat = normalize_category(tx.get("category"), valid_categories, fallback_cat)
        tx["category"] = cat
        cat, valid_sub = validate_transaction_category_subcategory(cat, tx.get("subcategory"), valid_categories, fallback_cat)
        tx["category"] = cat
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
        await safe_answer(message, report)
        return

    # РАССРОЧКИ
    if intent == "add_installment":
        data = parsed.get("installment", {})
        if data:
            data["user"] = user_name
            data["id"] = f"INST_{chat_id}_{message.message_id}"
        saved = await asyncio.to_thread(add_installment, data) if data else None
        res = "Записала рассрочку." if saved else "Не смогла сохранить рассрочку."
        await safe_answer(message, res)
        return

    if intent == "close_installment":
        query = parsed.get("search_query", "")
        saved = await asyncio.to_thread(close_installment, query) if query else None
        res = "Закрыла рассрочку." if saved else "Не нашла рассрочку или не удалось сохранить изменение."
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
                saved = await asyncio.to_thread(add_or_update_subscription, name, amount, bank, day, sub.get("paid_this_month") is True)
                res = f"Записала подписку {saved.get('name', name)}: {_format_currency(amount)} тг/мес, день списания — {max(1, min(day, 31))}."
            except Exception as e:
                print(f"[Подписки] Ошибка сохранения: {e}")
                res = "Не смогла сохранить подписку в таблицу. Проверь Google Sheets."
        else:
            res = "Не хватает названия или суммы подписки."
        await safe_answer(message, res)
        return

    if intent == "cancel_subscription":
        name = parsed.get("subscription_name", "")
        saved = await asyncio.to_thread(deactivate_subscription, name) if name else None
        res = "Отменила подписку." if saved else "Не нашла подписку или не удалось сохранить изменение."
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
        await safe_answer(message, res)
        return

    # СПИСОК ПОКУПОК
    if intent == "add_shopping":
        items = parsed.get("shopping_items", [])
        saved = await asyncio.to_thread(add_shopping_items, items, user_name) if items else []
        res = f"Добавила в список покупок: {len(saved)}." if saved else "Не определила товары для добавления."
        await safe_answer(message, res)
        return

    if intent == "clear_shopping":
        items = parsed.get("shopping_items", [])
        removed = await asyncio.to_thread(mark_shopping_items_done, items) if items else []
        res = f"Вычеркнула из списка: {len(removed)}." if removed else "Не нашла товары для вычёркивания."
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
        await safe_answer(message, res)
        return

    # ПОЕЗДКИ
    if intent == "add_trip":
        destination = parsed.get("destination", "")
        dates = parsed.get("dates", "")
        budget = _to_number_or_blank(parsed.get("budget", 0))
        notes = parsed.get("notes", "")
        saved = await asyncio.to_thread(add_trip_plan, destination, dates, budget, notes) if destination else None
        res = "Записала поездку." if saved else "Не определила место поездки."
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
        lines = [f"💰 **Доходы за {month_label(now)}:** {_format_currency(total)} тг"]
        for t in incomes:
            lines.append(f"  - {t.get('cat')}: {_format_currency(t.get('amt'))} тг ({t.get('comm')})")
        res = "\n".join(lines)
        await safe_answer(message, res)
        return

    # ОБЫЧНЫЙ ЧАТ
    res = reply or "Чем могу помочь?"
    await safe_answer(message, res)


async def resolve_pending(message, parsed, text):
    key = state.dialogue_key(message.chat.id, message.from_user.id)
    if parsed["intent"] == "resolve_clarification":
        draft = get_clarification(key)
        try:
            index = int(parsed.get("clarification_index"))
            if not draft or not 0 <= index < len(draft["options"]): raise ValueError()
        except (TypeError, ValueError):
            await safe_answer(message,"Не нашла ожидающий выбор категории. Пришли новую операцию."); return
        tx, option = draft["transaction"], draft["options"][index]
        tx.update(category=option["category"], subcategory=option.get("subcategory",""),
                  necessity=option.get("necessity") or normalize_necessity(tx.get("necessity"),option["category"]))
        await asyncio.to_thread(append_transaction,tx)
        ack_clarification(key)
        await safe_answer(message,_format_confirmation_report(tx,f"Категория: {option['label']}"))
        return
    pending = pop_pending(key)
    if not pending:
        await safe_answer(message,"У тебя нет ожидающего комментария чека."); return
    transactions, owner = pending
    comment = str(parsed.get("comment") or text)
    if comment.lower().strip() in {"без комментария","без комментариев"}:
        comment = ""
    for update in parsed.get("receipt_updates") or []:
        try:
            index = int(update.get("index"))
            if not 0 <= index < len(transactions): continue
        except (TypeError,ValueError):
            continue
        tx = transactions[index]
        valid = INCOME_CATEGORIES if tx.get("type") == TYPE_INCOME else EXPENSE_CATEGORIES
        fallback = FALLBACK_INCOME_CATEGORY if valid is INCOME_CATEGORIES else FALLBACK_EXPENSE_CATEGORY
        cat, sub = validate_transaction_category_subcategory(update.get("category"),update.get("subcategory"),valid,fallback)
        tx.update(category=cat,subcategory=sub,necessity=normalize_necessity(update.get("necessity"),cat))
    for tx in transactions:
        existing = str(tx.get("user_comment") or "")
        if comment and comment not in existing:
            tx["user_comment"] = f"{existing} ({comment})".strip()
        tx["user"] = owner
    # Persist enriched comment/categories before first write; retry cannot lose amendments.
    state.put("receipts",key,{"transactions":transactions,"user_name":owner})
    for tx in transactions:
        await asyncio.to_thread(append_transaction,tx)
    ack_pending(key)
    await safe_answer(message,"📸 Сохранила чек:\n\n" + "\n".join(
        f"{_format_currency(tx['amount'])} KZT · {tx['category']} · {tx.get('user_comment','')}" for tx in transactions))
