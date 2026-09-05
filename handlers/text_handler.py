"""Обработка текстовых и голосовых сообщений (все 23 интента + долги + копилки)."""

import asyncio
import datetime
import re
import logging

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
    add_shopping_items, get_shopping_items, mark_shopping_items_done,
    add_trip_plan, get_planned_trips,
    get_active_subscriptions, deactivate_subscription, add_or_update_subscription,
    add_installment, get_installments, close_installment,
    split_last_transaction_by_amount, find_and_update_record, delete_record_by_keyword,
    get_transactions_for_period, find_recent_duplicate_transaction,
    debug_transactions_snapshot, normalize_necessity,
    update_last_transaction_bank_and_source,
)
from services.charts import generate_expense_chart, generate_trend_chart
from services.limits_ai import generate_limits_from_history
from services.weather import get_weather_forecast
from services.telegram_safe import safe_answer
from services.pending_receipts import has_pending, pop_pending
from services.pending_clarifications import (
    set_clarification, get_clarification, pop_clarification,
)
from services.memory import get_chat_history, add_chat_message
from services.voice import transcribe_voice
from services.timezone import now_astana, month_label
from services.analytics import analyze_budget_leaks
from services.reports import generate_pdf_report, generate_excel_export
from services import reminders as reminder_service
from services import debts as debts_service
from services import goals as goals_service


def _to_number_or_blank(value):
    return parse_amount(value)


def _format_currency(value):
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except Exception:
        return str(value)


def _format_confirmation_report(tx: dict, ai_comment: str = "") -> str:
    amt = _format_currency(tx.get("amount", 0))
    curr = tx.get("currency", "KZT")
    bank_source = "Наличные" if tx.get("resource") == "Наличные" else (tx.get("source") or tx.get("bank", "Не указан"))
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

    append_transaction(tx)
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
    raw_uid = message.from_user.id if message.from_user else None
    raw_fn = message.from_user.first_name if message.from_user else ""
    user_name = get_authorized_user_name(raw_uid, raw_fn) or "Пользователь"
    chat_id = message.chat.id
    add_chat_message(chat_id, user_name, text)
    t_clean = text.strip().lower().replace("ё", "е")

    # 1. ПРЯМАЯ ОБРАБОТКА НАПОМИНАНИЙ (без посредников и черновиков!)
    if await reminder_service.handle(message, text, user_name):
        return

    # 2. ПРЯМАЯ ОБРАБОТКА ДОЛГОВ И ВОЗВРАТОВ
    if await debts_service.handle(message, text, user_name):
        return

    # 3. ПРЯМАЯ ОБРАБОТКА КОПИЛОК И ЦЕЛЕЙ
    if any(k in t_clean for k in ["копилк", "цели", "цель"]):
        if any(k in t_clean for k in ["покажи", "список", "какие"]):
            goals = await asyncio.to_thread(goals_service.get_goals)
            res = goals_service.format_goals_list(goals)
            await safe_answer(message, res)
            return

        m_add = re.search(r"(?:создай|добавь|поставь)\s+цель\s+([a-zA-Zа-яА-Я0-9\s]+?)\s+(\d+)", text, re.I)
        if m_add:
            g_name = m_add.group(1).strip()
            g_amt = float(m_add.group(2))
            saved = await asyncio.to_thread(goals_service.add_goal, g_name, g_amt)
            await safe_answer(message, f"🎯 Создала копилку: **{saved['name']}** на {_format_currency(saved['target_amount'])} KZT!")
            return

        m_dep = re.search(r"(?:отложили|закинули|добавили|пополнили)\s+(?:в\s+копилку\s+)?([a-zA-Zа-яА-Я0-9\s]+?)\s+(\d+)", text, re.I)
        if m_dep:
            g_name = m_dep.group(1).strip()
            g_amt = float(m_dep.group(2))
            res_dep = await asyncio.to_thread(goals_service.deposit_to_goal, g_name, g_amt)
            if res_dep:
                await safe_answer(message, f"💰 Пополнили цель **{res_dep['name']}** на {_format_currency(g_amt)} KZT! Баланс: {_format_currency(res_dep['current_amount'])} KZT.")
            else:
                await safe_answer(message, "Не нашла такую цель для пополнения.")
            return

    # 4. Чек ожидает комментария
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
                tx["user"] = user_name
                append_transaction(tx)
                lines.append(
                    f"• {_format_currency(tx.get('amount'))} {tx.get('currency')} | "
                    f"{tx.get('bank')} | {tx.get('category')} ({tx['user_comment']})"
                )
            rep = "\n".join(lines)
            add_chat_message(chat_id, "Ада", rep)
            await safe_answer(message, rep)
            return

    # 5. Мгновенная смена банка последней траты
    bank_match = re.search(r'(?:измени|поменяй|запиши|исправь)?\s*(?:что\s+это\s+)?(?:оплата\s+через|банк\s+на|карту\s+на|с\s+карты|банк)\s+([a-zA-Zа-яА-Я]+)', t_clean)
    if bank_match:
        target_bank = bank_match.group(1).upper()
        if target_bank in ["BCC", "KASPI", "HALYK", "FORTE", "FREEDOM", "НАЛОМ", "НАЛИЧНЫЕ", "БЦК", "КАСПИ"]:
            if target_bank in ["БЦК"]: target_bank = "BCC"
            if target_bank in ["КАСПИ"]: target_bank = "Kaspi"
            if target_bank in ["НАЛОМ", "НАЛИЧНЫЕ"]: target_bank = "Наличные"

            updated_rec = update_last_transaction_bank_and_source(target_bank)
            if updated_rec:
                res = f"✅ Исправила в последней записи ({updated_rec.get('user_comment')}, {_format_currency(updated_rec.get('amount'))} KZT): банк изменён на **{updated_rec.get('bank')}** ({updated_rec.get('source')})."
            else:
                res = "Не нашла последнюю запись в таблице для изменения банка."
            add_chat_message(chat_id, "Ада", res)
            await safe_answer(message, res)
            return

    # 6. Детектор утечек бюджета
    if t_clean in {"/leaks", "утечки"} or any(k in t_clean for k in ["утечки бюджета", "микротраты", "куда уходят деньги", "мелкие траты"]):
        leak_data = analyze_budget_leaks()
        add_chat_message(chat_id, "Ада", leak_data["text"])
        await safe_answer(message, leak_data["text"])
        return

    # 7. Экспорт PDF и Excel
    if t_clean in {"/report", "pdf"} or any(k in t_clean for k in ["отчет в pdf", "отчёт в pdf", "выгрузи отчет в pdf"]):
        try:
            await message.bot.send_chat_action(chat_id=chat_id, action="upload_document")
            pdf_bytes = await asyncio.to_thread(generate_pdf_report)
            pdf_file = BufferedInputFile(pdf_bytes, filename=f"Finance_Report_{now_astana().strftime('%Y_%m')}.pdf")
            await message.answer_document(document=pdf_file, caption="📑 Официальный семейный финансовый отчёт за месяц.")
            return
        except Exception as e:
            logging.exception(f"[PDF Error]: {e}")
            await safe_answer(message, "Не удалось сформировать PDF-отчёт.")
            return

    if t_clean in {"/export", "excel"} or any(k in t_clean for k in ["выгрузи в excel", "экспорт в excel", "скачать excel"]):
        try:
            await message.bot.send_chat_action(chat_id=chat_id, action="upload_document")
            excel_bytes = await asyncio.to_thread(generate_excel_export)
            excel_file = BufferedInputFile(excel_bytes, filename=f"Family_Finance_{now_astana().strftime('%Y_%m')}.xlsx")
            await message.answer_document(document=excel_file, caption="📊 Полная выписка в формате Excel (.xlsx).")
            return
        except Exception as e:
            logging.exception(f"[Excel Error]: {e}")
            await safe_answer(message, "Не удалось сформировать файл Excel.")
            return

    # 8. Графики (/chart и /trend)
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

    if t_clean in {"/trend", "тренд"}:
        try:
            image_bytes = await asyncio.to_thread(generate_trend_chart)
            if image_bytes:
                photo_file = BufferedInputFile(image_bytes, filename="trend.png")
                await message.answer_photo(photo=photo_file, caption="📈 Динамика расходов по месяцам.")
            else:
                await safe_answer(message, "Нет данных для графика тренда.")
        except Exception as error:
            print(f"[Trend] Ошибка: {error}")
            await safe_answer(message, "Не удалось построить график тренда.")
        return

    # 9. Погода
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

    # 10. Диагностика
    if t_clean in {"debug", "/debug"}:
        debug_text = debug_transactions_snapshot()
        await safe_answer(message, f"```\n{debug_text}\n```")
        return

    # 11. Разделить транзакцию
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
                success = split_last_transaction_by_amount(
                    target_amount, part1_amt, part1_cat, part1_desc,
                    part2_amt, part2_cat, part2_desc
                )
                res = f"Разделила: {part1_cat} — {part1_amt} тг, {part2_cat} — {part2_amt} тг." if success else "Не нашла такую транзакцию."
                add_chat_message(chat_id, "Ада", res)
                await safe_answer(message, res)
                return
        await safe_answer(message, "Формат: раздели транзакцию <сумма>: <категория> — <сумма> | <категория> — <сумма>")
        return

    try:
        await message.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass

    # ── ЗАПРОС К DEEPSEEK ──
    history = get_last_200_transactions()
    limits = get_category_limits()
    reminders = reminder_service.pending()
    shopping_list = get_shopping_items()
    trips = get_planned_trips()
    subscriptions = get_active_subscriptions()
    installments = get_installments()
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

    # Обработка модели напоминаний
    if await reminder_service.handle_model(message, parsed, user_name):
        return

    # Обработка модели долгов
    if await debts_service.handle_model(message, parsed, user_name):
        return

    # Блокировка кнопок при командах удаления
    is_delete_or_edit_command = any(k in t_clean for k in ["удали", "удалить", "поменяй", "измени", "исправь", "замени", "отмени"])

    ambig_options = parsed.get("clarification_options") or get_ambiguous_options(text)
    if ambig_options and not is_delete_or_edit_command and (intent in {"need_clarification", "transaction"} or parse_amount(text) > 0):
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

        prompt_text = reply if (reply and not any(b in reply.lower() for b in ["задумалась", "повтори"])) else f"Куда запишем эту покупку ({amt_str} {curr}{res_label})?"

        kb = _build_clarification_keyboard(ambig_options)
        set_clarification(chat_id, tx, ambig_options)
        add_chat_message(chat_id, "Ада", prompt_text)
        await message.answer(prompt_text, reply_markup=kb)
        return

    # Удаление и правка
    if intent in {"delete_transaction", "correct_any_record"} or is_delete_or_edit_command:
        query = parsed.get("search_query", "") or text
        deleted = delete_record_by_keyword("Transactions", query)
        if deleted:
            res = f"Удалила покупку: {deleted.get('category')} на {_format_currency(deleted.get('amount'))} тг ({deleted.get('user_comment')})."
        else:
            res = reply or "Не нашла такую запись в таблице для удаления."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # Сводка за месяц
    if intent == "get_summary":
        now = now_astana()
        start = now.replace(day=1).strftime("%Y-%m-%d")
        end = now.replace(month=now.month + 1, day=1).strftime("%Y-%m-%d") if now.month < 12 else now.replace(year=now.year + 1, month=1, day=1).strftime("%Y-%m-%d")

        transactions = get_transactions_for_period(start, end)
        total_income = sum(parse_amount(t.get("amt", 0)) for t in transactions if str(t.get("type")) == TYPE_INCOME)
        total_expense = sum(parse_amount(t.get("amt", 0)) for t in transactions if str(t.get("type")) != TYPE_INCOME)
        by_category = {}
        for t in transactions:
            if str(t.get("type")) != TYPE_INCOME:
                cat = str(t.get("cat") or "Прочее")
                by_category[cat] = by_category.get(cat, 0.0) + parse_amount(t.get("amt", 0))

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
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # Обычная транзакция
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
        _, valid_sub = validate_transaction_category_subcategory(cat, tx.get("subcategory"))
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
        duplicate = find_recent_duplicate_transaction(amount, text)
        if duplicate:
            dup_warning = f"⚠️ _Записала, но похоже на недавний дубль ({_format_currency(amount)} тг в {str(duplicate.get('date', ''))[-8:]})._"
            reply = f"{reply}\n\n{dup_warning}" if reply else dup_warning

        append_transaction(tx)
        report = _format_confirmation_report(tx, reply)
        add_chat_message(chat_id, "Ада", report)
        await safe_answer(message, report)
        return

    # Рассрочки
    if intent == "add_installment":
        data = parsed.get("installment", {})
        if data:
            add_installment(data)
        res = reply or "Записала рассрочку."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "close_installment":
        query = parsed.get("search_query", "")
        if query:
            close_installment(query)
        res = reply or "Закрыла рассрочку."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "get_installments":
        items = get_installments()
        if items:
            lines = ["📋 **Активные рассрочки:**"]
            for item in items:
                lines.append(f"- {item.get('description')} ({item.get('bank')}): {_format_currency(item.get('total_amount'))} тг")
            res = "\n".join(lines)
        else:
            res = "Нет активных рассрочек."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # Подписки
    if intent == "add_subscription":
        sub = parsed.get("subscription") or {}
        name = sub.get("name") or parsed.get("subscription_name", "")
        amount = parse_amount(sub.get("amount", parsed.get("amount", 0)))
        bank = sub.get("bank") or parsed.get("bank", "Не указан")
        day = int(parse_amount(sub.get("day_of_month", parsed.get("day_of_month", now_astana().day))) or now_astana().day)
        if name and amount > 0:
            add_or_update_subscription(name, amount, bank, day)
            res = f"Записала подписку {name}: {_format_currency(amount)} тг/мес."
        else:
            res = "Не хватает названия или суммы подписки."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "cancel_subscription":
        name = parsed.get("subscription_name", "")
        if name:
            deactivate_subscription(name)
        res = reply or "Отменила подписку."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "get_subscriptions":
        items = get_active_subscriptions()
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

    # Список покупок
    if intent == "add_shopping":
        items = parsed.get("shopping_items", [])
        if items:
            add_shopping_items(items, user_name)
        res = reply or "Добавила в список покупок."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "clear_shopping":
        items = parsed.get("shopping_items", [])
        if items:
            mark_shopping_items_done(items)
        res = reply or "Убрала из списка."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "get_shopping":
        items = get_shopping_items()
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

    # Поездки
    if intent == "add_trip":
        destination = parsed.get("destination", "")
        dates = parsed.get("dates", "")
        budget = _to_number_or_blank(parsed.get("budget", 0))
        notes = parsed.get("notes", "")
        if destination:
            add_trip_plan(destination, dates, budget, notes)
        res = reply or "Записала поездку."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "get_trips":
        items = get_planned_trips()
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

    # Лимиты
    if intent == "get_limits":
        current_limits = get_category_limits()
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
            logging.exception(f"[Лимиты Error]: {error}")
            res = "Не удалось сгенерировать лимиты."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # Доходы
    if intent == "get_income":
        now = now_astana()
        start = now.replace(day=1).strftime("%Y-%m-%d")
        end = now.replace(month=now.month + 1, day=1).strftime("%Y-%m-%d") if now.month < 12 else now.replace(year=now.year + 1, month=1, day=1).strftime("%Y-%m-%d")
        transactions = get_transactions_for_period(start, end)
        incomes = [t for t in transactions if str(t.get("type")) == TYPE_INCOME]
        total = sum(_to_number_or_blank(t.get("amt")) for t in incomes)
        lines = [f"💰 **Доходы за {month_label(now)}:** {_format_currency(total)} тг"]
        for t in incomes:
            lines.append(f"  - {t.get('cat')}: {_format_currency(t.get('amt'))} тг ({t.get('comm')})")
        res = "\n".join(lines)
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # Обычный разговор
    res = reply or "Чем могу помочь?"
    add_chat_message(chat_id, "Ада", res)
    await safe_answer(message, res)
