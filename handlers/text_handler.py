"""Обработка текстовых сообщений от пользователя (все интенты)."""

import asyncio
import datetime
import re

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram import types

from services.categories import (
    TYPE_EXPENSE, TYPE_INCOME,
    EXPENSE_CATEGORIES, INCOME_CATEGORIES,
    FALLBACK_EXPENSE_CATEGORY, FALLBACK_INCOME_CATEGORY,
    normalize_category, normalize_subcategory,
    validate_transaction_category_subcategory,
)
from services.money import parse_amount
from services.banks import normalize_bank_source
from services.deepseek_service import parse_and_analyze
from services.sheets import (
    append_transaction, get_last_200_transactions, get_category_limits,
    get_pending_reminders, add_reminder,
    add_shopping_items, get_shopping_items, mark_shopping_items_done,
    add_trip_plan, get_planned_trips,
    get_active_subscriptions, deactivate_subscription,
    add_installment, get_installments, close_installment,
    split_last_transaction_by_amount, find_and_update_record, delete_record_by_keyword,
    get_transactions_for_period, find_recent_duplicate_transaction,
    debug_transactions_snapshot, normalize_necessity,
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


def _format_confirmation_report(tx: dict, ai_comment: str = "") -> str:
    """Форматирует строгий отчёт о записи (как на Скриншоте 2)."""
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
    if ai_comment:
        lines.append(f"\n💬 {ai_comment}")
    return "\n".join(lines)


def _build_clarification_keyboard(options: list[dict]) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру с кнопками (2-4 штуки) с коротким надежным индексом."""
    buttons = []
    for idx, opt in enumerate(options):
        buttons.append([InlineKeyboardButton(text=opt["label"], callback_data=f"clarify_opt:{idx}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def handle_category_clarification_callback(callback: types.CallbackQuery):
    """Обработчик нажатия на кнопки выбора категории/подкатегории."""
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
    tx["necessity"] = normalize_necessity(tx.get("necessity"), tx["category"])

    append_transaction(tx)
    report = _format_confirmation_report(tx, f"Категория уточнена: {chosen['label']}.")
    await callback.message.edit_text(report)
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
        await safe_answer(message, "Не удалось скачать аудиосообщение.")
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
    user_name = message.from_user.first_name or "Пользователь"
    chat_id = message.chat.id
    add_chat_message(chat_id, user_name, text)

    # 1. Чек ожидает комментария
    if has_pending(chat_id):
        pending = pop_pending(chat_id)
        if pending:
            transactions, receipt_user = pending
            lines = ["📸 **Записано по чеку:**"]
            for tx in transactions:
                tx["user_comment"] = text
                tx["user"] = receipt_user
                append_transaction(tx)
                lines.append(
                    f"• {_format_currency(tx.get('amount'))} {tx.get('currency')} | "
                    f"{tx.get('bank')} | {tx.get('category')} ({text})"
                )
            await safe_answer(message, "\n".join(lines))
            return

    # 2. Перехват текстового ответа на вопрос об уточнении («на работу», «домой»)
    clarification = get_clarification(chat_id)
    if clarification:
        text_lower = text.lower()
        matched_opt = None
        for opt in clarification.get("options", []):
            words = re.findall(r'\w+', opt["label"].lower())
            if any(w in text_lower for w in words if len(w) > 3):
                matched_opt = opt
                break
        if "работ" in text_lower or "кафе" in text_lower or "собой" in text_lower or "перекус" in text_lower:
            matched_opt = clarification["options"][0]
        elif "дом" in text_lower or "продукт" in text_lower:
            matched_opt = clarification["options"][-1]

        if matched_opt:
            pop_clarification(chat_id)
            tx = clarification["transaction"]
            tx["category"] = matched_opt["category"]
            tx["subcategory"] = matched_opt.get("subcategory", "")
            tx["user_comment"] = f"{tx.get('user_comment', '')} ({text})".strip()
            tx["necessity"] = normalize_necessity(tx.get("necessity"), tx["category"])
            append_transaction(tx)
            report = _format_confirmation_report(tx, f"Поняла, это {matched_opt['label']}!")
            await safe_answer(message, report)
            return

    # 3. /debug
    if text.strip().lower() in {"debug", "/debug"}:
        debug_text = debug_transactions_snapshot()
        await safe_answer(message, f"```\n{debug_text}\n```")
        return

    # 4. /chart (полноценный дашборд с BufferedInputFile)
    if text.strip().lower() in {"график", "/chart", "chart"}:
        try:
            image_bytes = await asyncio.to_thread(generate_expense_chart)
            if image_bytes:
                photo_file = BufferedInputFile(image_bytes, filename="chart.png")
                await message.answer_photo(photo=photo_file, caption="📊 Вот твой финансовый дашборд.")
            else:
                await safe_answer(message, "Нет данных для построения графика.")
        except Exception as error:
            print(f"[График] Ошибка: {error}")
            await safe_answer(message, "Не удалось построить график.")
        return

    # 5. Разделение транзакции
    if "раздели" in text.lower() and "транзакцию" in text.lower():
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
                if success:
                    await safe_answer(message, f"Разделила: {part1_cat} — {part1_amt} тг, {part2_cat} — {part2_amt} тг.")
                else:
                    await safe_answer(message, "Не нашла такую транзакцию.")
                return
        await safe_answer(message, "Формат: раздели транзакцию <сумма>: <категория> — <сумма> | <категория> — <сумма>")
        return

    # 6. Обработка через нейросеть
    history = get_last_200_transactions()
    limits = get_category_limits()
    reminders = get_pending_reminders()
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

    # ИНТЕРАКТИВНОЕ УТОЧНЕНИЕ КАТЕГОРИИ
    if intent == "need_clarification":
        tx = parsed.get("transaction", {})
        options = parsed.get("clarification_options", [])
        if tx and options:
            tx["user"] = user_name
            tx["source"] = normalize_bank_source(tx.get("bank"), tx.get("source"))
            kb = _build_clarification_keyboard(options)
            set_clarification(chat_id, tx, options)
            await message.answer(reply or "Уточни, куда отнести эту покупку:", reply_markup=kb)
            return

    # ТРАНЗАКЦИЯ
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
        if not tx.get("user"): tx["user"] = user_name
        if not tx.get("merchant"): tx["merchant"] = ""
        if not tx.get("user_comment"): tx["user_comment"] = text
        if not tx.get("ai_comment"): tx["ai_comment"] = reply or ""

        amount = parse_amount(tx.get("amount", 0))
        duplicate = find_recent_duplicate_transaction(amount)
        if duplicate:
            await safe_answer(
                message,
                f"⚠️ Похожая операция на {_format_currency(amount)} тг уже была записана "
                f"({duplicate.get('category')} — {duplicate.get('date')}). Записать ещё раз?",
            )
            return

        append_transaction(tx)
        report = _format_confirmation_report(tx, reply)
        await safe_answer(message, report)
        return

    # ИСПРАВЛЕНИЕ ЗАПИСЕЙ
    if intent == "correct_any_record":
        updates = parsed.get("updates", [])
        if not updates:
            await safe_answer(message, reply or "Не поняла, что исправлять.")
            return
        results = []
        for upd in updates:
            worksheet = upd.get("worksheet", "Transactions")
            search_query = upd.get("search_query", "")
            column = upd.get("column_to_update", "")
            new_value = upd.get("new_value", "")
            action = upd.get("action", "update")
            if action == "delete":
                deleted = delete_record_by_keyword(worksheet, search_query)
                results.append("удалено" if deleted else "не найдено")
            else:
                if column == "category":
                    new_value = normalize_category(new_value, EXPENSE_CATEGORIES + INCOME_CATEGORIES, FALLBACK_EXPENSE_CATEGORY)
                if column == "subcategory":
                    ws_data = get_last_200_transactions()
                    for r in ws_data:
                        if search_query.lower() in str(r).lower():
                            cat = r.get("cat", "")
                            _, new_value = validate_transaction_category_subcategory(cat, new_value)
                            break
                if column == "necessity":
                    new_value = normalize_necessity(new_value)
                updated = find_and_update_record(worksheet, search_query, column, new_value)
                results.append("обновлено" if updated else "не найдено")
        await safe_answer(message, reply or f"Результат: {', '.join(results)}.")
        return

    # РАССРОЧКИ
    if intent == "add_installment":
        data = parsed.get("installment", {})
        if data:
            add_installment(data)
        await safe_answer(message, reply or "Записала рассрочку.")
        return

    if intent == "close_installment":
        query = parsed.get("search_query", "")
        if query:
            close_installment(query)
        await safe_answer(message, reply or "Закрыла рассрочку.")
        return

    if intent == "get_installments":
        items = get_installments()
        if items:
            lines = ["📋 **Активные рассрочки:**"]
            for item in items:
                lines.append(
                    f"- {item.get('description')} ({item.get('bank')}): "
                    f"{_format_currency(item.get('total_amount'))} тг, "
                    f"{_format_currency(item.get('monthly_payment'))} тг/мес"
                )
            await safe_answer(message, "\n".join(lines))
        else:
            await safe_answer(message, "Нет активных рассрочек.")
        return

    # ПОДПИСКИ
    if intent == "cancel_subscription":
        name = parsed.get("subscription_name", "")
        if name:
            deactivate_subscription(name)
        await safe_answer(message, reply or "Отменила подписку.")
        return

    if intent == "get_subscriptions":
        items = get_active_subscriptions()
        if items:
            lines = ["📋 **Активные подписки:**"]
            for item in items:
                lines.append(
                    f"- {item.get('name')}: {_format_currency(item.get('amount'))} тг/мес "
                    f"({item.get('bank')})"
                )
            await safe_answer(message, "\n".join(lines))
        else:
            await safe_answer(message, "Нет активных подписок.")
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
        for time_str in times:
            add_reminder(target, time_str, text_rem, recurrence)
        await safe_answer(message, reply or "Поставила напоминание.")
        return

    if intent == "delete_reminder":
        query = parsed.get("search_query", "")
        if query:
            delete_record_by_keyword("Reminders", query)
        await safe_answer(message, reply or "Удалила напоминание.")
        return

    if intent == "get_reminders":
        items = get_pending_reminders()
        if items:
            lines = ["📋 **Активные напоминания:**"]
            for item in items:
                lines.append(f"- {item.get('target_user')}: {item.get('text')} ({item.get('remind_at')})")
            await safe_answer(message, "\n".join(lines))
        else:
            await safe_answer(message, "Нет активных напоминаний.")
        return

    # СПИСОК ПОКУПОК
    if intent == "add_shopping":
        items = parsed.get("shopping_items", [])
        if items:
            add_shopping_items(items, user_name)
        await safe_answer(message, reply or "Добавила в список покупок.")
        return

    if intent == "clear_shopping":
        items = parsed.get("shopping_items", [])
        if items:
            mark_shopping_items_done(items)
        await safe_answer(message, reply or "Убрала из списка.")
        return

    if intent == "get_shopping":
        items = get_shopping_items()
        if items:
            lines = ["🛒 **Список покупок:**"]
            for item in items:
                lines.append(f"- {item.get('item')}")
            await safe_answer(message, "\n".join(lines))
        else:
            await safe_answer(message, "Список покупок пуст.")
        return

    # ПОЕЗДКИ
    if intent == "add_trip":
        destination = parsed.get("destination", "")
        dates = parsed.get("dates", "")
        budget = _to_number_or_blank(parsed.get("budget", 0))
        notes = parsed.get("notes", "")
        if destination:
            add_trip_plan(destination, dates, budget, notes)
        await safe_answer(message, reply or "Записала поездку.")
        return

    if intent == "get_trips":
        items = get_planned_trips()
        if items:
            lines = ["✈️ **Запланированные поездки:**"]
            for item in items:
                lines.append(
                    f"- {item.get('destination')} ({item.get('dates')}): "
                    f"{_format_currency(item.get('budget'))} тг"
                )
            await safe_answer(message, "\n".join(lines))
        else:
            await safe_answer(message, "Нет запланированных поездок.")
        return

    # ЛИМИТЫ
    if intent == "get_limits":
        current_limits = get_category_limits()
        if current_limits:
            lines = ["📊 **Текущие лимиты:**"]
            for cat, limit in current_limits.items():
                lines.append(f"- {cat}: {_format_currency(limit)} тг")
            await safe_answer(message, "\n".join(lines))
        else:
            await safe_answer(message, "Лимиты не заданы.")
        return

    if intent == "generate_limits":
        try:
            new_limits = await generate_limits_from_history()
            if new_limits:
                lines = ["📊 **Сгенерированные лимиты:**"]
                for cat, limit in new_limits.items():
                    lines.append(f"- {cat}: {_format_currency(limit)} тг")
                await safe_answer(message, "\n".join(lines))
            else:
                await safe_answer(message, "Недостаточно данных для генерации лимитов.")
        except Exception as error:
            print(f"[Лимиты] Ошибка генерации: {error}")
            await safe_answer(message, "Не удалось сгенерировать лимиты.")
        return

    # СВОДКА И ДОХОДЫ
    if intent == "get_summary":
        now = datetime.datetime.now()
        start = now.replace(day=1).strftime("%Y-%m-%d")
        end = (now.replace(day=1) + datetime.timedelta(days=32)).replace(day=1).strftime("%Y-%m-%d")
        transactions = get_transactions_for_period(start, end)
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
        for cat, amt in sorted(by_category.items(), key=lambda x: -x[1]):
            lines.append(f"  - {cat}: {_format_currency(amt)} тг")
        await safe_answer(message, "\n".join(lines))
        return

    if intent == "get_income":
        now = datetime.datetime.now()
        start = now.replace(day=1).strftime("%Y-%m-%d")
        end = (now.replace(day=1) + datetime.timedelta(days=32)).replace(day=1).strftime("%Y-%m-%d")
        transactions = get_transactions_for_period(start, end)
        incomes = [t for t in transactions if str(t.get("type")) == TYPE_INCOME]
        total = sum(_to_number_or_blank(t.get("amt")) for t in incomes)
        lines = [f"💰 **Доходы за {now.strftime('%B %Y')}:** {_format_currency(total)} тг"]
        for t in incomes:
            lines.append(f"  - {t.get('cat')}: {_format_currency(t.get('amt'))} тг ({t.get('comm')})")
        await safe_answer(message, "\n".join(lines))
        return

    # ПОГОДА
    if intent == "get_weather":
        forecast = await get_weather_forecast()
        if forecast:
            await safe_answer(message, forecast)
        else:
            await safe_answer(message, "Не удалось получить прогноз погоды.")
        return

    # РАЗДЕЛЕНИЕ
    if intent == "split_transaction":
        await safe_answer(
            message,
            reply or "Чтобы разделить операцию, напиши: раздели транзакцию <сумма>: <категория> — <сумма> | <категория> — <сумма>"
        )
        return

    # УДАЛЕНИЕ
    if intent == "delete_transaction":
        query = parsed.get("search_query", "")
        if query:
            delete_record_by_keyword("Transactions", query)
        await safe_answer(message, reply or "Удалила запись.")
        return

    # ЧАТ
    await safe_answer(message, reply or "Чем могу помочь?")
