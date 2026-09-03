"""Обработка текстовых сообщений от пользователя."""

import asyncio
import datetime
import json
import re

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram import types

from services.categories import (
    TYPE_EXPENSE, TYPE_INCOME,
    EXPENSE_CATEGORIES, INCOME_CATEGORIES,
    FALLBACK_EXPENSE_CATEGORY, FALLBACK_INCOME_CATEGORY,
    normalize_category, normalize_subcategory,
    validate_transaction_category_subcategory,
    get_time_context_hint,
)
from services.money import parse_amount, to_clean_number
from services.banks import normalize_bank_source
from services.deepseek_service import parse_and_analyze
from services.sheets import (
    append_transaction, get_last_200_transactions, get_category_limits,
    get_pending_reminders, add_reminder, mark_reminder_done,
    add_shopping_items, get_shopping_items, mark_shopping_items_done,
    add_trip_plan, get_planned_trips,
    add_or_update_subscription, get_active_subscriptions, deactivate_subscription,
    add_installment, get_installments, close_installment,
    split_last_transaction_by_amount, find_and_update_record, delete_record_by_keyword,
    get_transactions_for_period, find_recent_duplicate_transaction,
    debug_transactions_snapshot,
)
from services.charts import generate_expense_chart
from services.limits_ai import generate_limits_from_history
from services.weather import get_weather_forecast
from services.telegram_safe import safe_answer, safe_send_message
from services.pending_receipts import set_pending, has_pending, pop_pending
from services.pending_clarifications import (
    set_clarification, has_clarification, pop_clarification, sweep_expired_clarifications,
)
from services.memory import get_chat_history, add_chat_message


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


def _is_debug_command(text: str) -> bool:
    return text.strip().lower() in {"debug", "/debug"}


def _is_chart_command(text: str) -> bool:
    return text.strip().lower() in {"график", "/chart", "chart"}


def _is_split_command(text: str) -> bool:
    return "раздели" in text.lower() and "транзакцию" in text.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# ИНТЕРАКТИВНОЕ УТОЧНЕНИЕ КАТЕГОРИИ (InlineKeyboard)
# ═══════════════════════════════════════════════════════════════════════════════

def _build_category_clarification_keyboard(alternatives: list[str], tx_data: dict) -> InlineKeyboardMarkup:
    """Создать клавиатуру с вариантами категорий."""
    buttons = []
    for alt in alternatives:
        # callback_data ограничен 64 байтами
        callback = f"clarify_cat:{alt[:20]}"
        buttons.append([InlineKeyboardButton(text=alt, callback_data=callback)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _send_category_clarification(message: Message, tx: dict, alternatives: list[str], reply_text: str):
    """Отправить сообщение с кнопками для уточнения категории."""
    kb = _build_category_clarification_keyboard(alternatives, tx)
    sent = await message.answer(reply_text, reply_markup=kb)
    set_clarification(message.chat.id, tx, alternatives, sent.message_id)


async def handle_category_clarification_callback(callback: types.CallbackQuery):
    """Обработчик нажатия кнопки уточнения категории."""
    chat_id = callback.message.chat.id
    data = callback.data

    if not data.startswith("clarify_cat:"):
        return

    chosen_category = data.replace("clarify_cat:", "")
    clarification = pop_clarification(chat_id)

    if not clarification:
        await callback.answer("Уточнение устарело. Запиши заново.", show_alert=True)
        return

    tx = clarification["transaction"]
    tx["category"] = chosen_category

    # Валидация подкатегории для новой категории
    _, valid_sub = validate_transaction_category_subcategory(chosen_category, tx.get("subcategory"))
    tx["subcategory"] = valid_sub or normalize_subcategory(None, chosen_category, "")

    # Обновляем necessity
    from services.sheets import normalize_necessity
    tx["necessity"] = normalize_necessity(tx.get("necessity"), chosen_category)

    # Записываем
    append_transaction(tx)

    await callback.message.edit_text(
        f"✅ Записала: {_format_currency(tx.get('amount', 0))} тг → {chosen_category}\n"
        f"_{tx.get('user_comment', '')}_"
    )
    await callback.answer("Записано!")


# ═══════════════════════════════════════════════════════════════════════════════
# ОСНОВНОЙ ОБРАБОТЧИК
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_text(message: Message):
    text = message.text or ""
    user_name = message.from_user.first_name or "Пользователь"
    user_id = message.from_user.id
    chat_id = message.chat.id

    # Сохраняем сообщение в историю чата
    add_chat_message(chat_id, user_name, text)

    # ── Обработка ожидающего чека ──
    if has_pending(chat_id):
        pending = pop_pending(chat_id)
        if pending:
            transactions, receipt_user = pending
            for tx in transactions:
                tx["user_comment"] = text
                tx["user"] = receipt_user
                append_transaction(tx)
            await safe_answer(message, f"Записала {len(transactions)} покупок с комментарием. Спасибо!")
            return

    # ── Голосовые ──
    if message.voice or message.audio:
        await safe_answer(message, "Голосовые пока не поддерживаются. Пришли текстом.")
        return

    # ── /debug ──
    if _is_debug_command(text):
        debug_text = debug_transactions_snapshot()
        await safe_answer(message, f"```\n{debug_text}\n```")
        return

    # ── /chart ──
    if _is_chart_command(text):
        try:
            image_bytes = await asyncio.to_thread(generate_expense_chart)
            if image_bytes:
                await message.answer_photo(photo=image_bytes, caption="Вот твоя диаграмма расходов за текущий месяц.")
            else:
                await safe_answer(message, "Нет данных для построения графика.")
        except Exception as error:
            print(f"[График] Ошибка: {error}")
            await safe_answer(message, "Не удалось построить график.")
        return

    # ── Разделить транзакцию ──
    if _is_split_command(text):
        match = re.search(
            r"раздели\s+транзакцию\s+(\d+)[\s:]*(.+?)[\s]*\|\s*(.+?)",
            text, re.IGNORECASE
        )
        if match:
            target_amount = float(match.group(1))
            part1_desc = match.group(2).strip()
            part2_desc = match.group(3).strip()
            p1_match = re.match(r"(.+?)\s*[-—]\s*(\d+)", part1_desc)
            p2_match = re.match(r"(.+?)\s*[-—]\s*(\d+)", part2_desc)
            if p1_match and p2_match:
                part1_cat = p1_match.group(1).strip()
                part1_amt = float(p1_match.group(2))
                part2_cat = p2_match.group(1).strip()
                part2_amt = float(p2_match.group(2))
                part1_cat = normalize_category(part1_cat, EXPENSE_CATEGORIES, FALLBACK_EXPENSE_CATEGORY)
                part2_cat = normalize_category(part2_cat, EXPENSE_CATEGORIES, FALLBACK_EXPENSE_CATEGORY)
                _, part1_sub = validate_transaction_category_subcategory(part1_cat, "")
                _, part2_sub = validate_transaction_category_subcategory(part2_cat, "")
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

    # ── Запрос к DeepSeek ──
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

    # ── ТРАНЗАКЦИЯ ──
    if intent == "transaction":
        tx = parsed.get("transaction", {})
        if not tx:
            await safe_answer(message, reply or "Не поняла, что записывать.")
            return

        # Проверяем confidence — нужно ли уточнение?
        confidence = float(tx.get("confidence", 1.0))
        alternatives = tx.get("alternatives", [])

        if confidence < 0.8 and alternatives:
            # Показываем интерактивные кнопки
            await _send_category_clarification(message, tx, alternatives, 
                                               reply or "Не уверена в категории. Выбери:")
            return

        # Определяем тип
        tx_type = str(tx.get("type") or TYPE_EXPENSE).strip().upper()
        valid_categories = INCOME_CATEGORIES if tx_type == TYPE_INCOME else EXPENSE_CATEGORIES
        fallback_cat = FALLBACK_INCOME_CATEGORY if tx_type == TYPE_INCOME else FALLBACK_EXPENSE_CATEGORY

        # Нормализация
        raw_cat = tx.get("category")
        cat = normalize_category(raw_cat, valid_categories, fallback_cat)
        tx["category"] = cat
        raw_sub = tx.get("subcategory")
        _, valid_sub = validate_transaction_category_subcategory(cat, raw_sub)
        tx["subcategory"] = valid_sub or normalize_subcategory(None, cat, "")
        from services.sheets import normalize_necessity
        tx["necessity"] = normalize_necessity(tx.get("necessity"), cat)
        tx["source"] = normalize_bank_source(tx.get("bank"), tx.get("source"))
        if not tx.get("bank"):
            tx["bank"] = "Не указан"
        if not tx.get("currency"):
            tx["currency"] = "KZT"
        if not tx.get("funds_type"):
            tx["funds_type"] = "Собственные"
        if not tx.get("resource"):
            tx["resource"] = "Карта"
        if not tx.get("user"):
            tx["user"] = user_name
        if not tx.get("merchant"):
            tx["merchant"] = ""
        if not tx.get("user_comment"):
            tx["user_comment"] = ""
        if not tx.get("ai_comment"):
            tx["ai_comment"] = reply or ""

        # Проверка на дубли
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
        await safe_answer(message, reply or "Записала.")
        return

    # ── ИСПРАВЛЕНИЕ ЗАПИСИ ──
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
                    new_value = normalize_category(new_value, EXPENSE_CATEGORIES + INCOME_CATEGORIES, 
                                                   FALLBACK_EXPENSE_CATEGORY)
                if column == "subcategory":
                    ws_data = get_last_200_transactions()
                    for r in ws_data:
                        if search_query.lower() in str(r).lower():
                            cat = r.get("cat", "")
                            _, new_value = validate_transaction_category_subcategory(cat, new_value)
                            break
                if column == "necessity":
                    from services.sheets import normalize_necessity
                    new_value = normalize_necessity(new_value)
                updated = find_and_update_record(worksheet, search_query, column, new_value)
                results.append("обновлено" if updated else "не найдено")
        await safe_answer(message, reply or f"Результат: {', '.join(results)}.")
        return

    # ── РАССРОЧКИ ──
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
            lines = ["📋 Активные рассрочки:"]
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

    # ── ПОДПИСКИ ──
    if intent == "cancel_subscription":
        name = parsed.get("subscription_name", "")
        if name:
            deactivate_subscription(name)
        await safe_answer(message, reply or "Отменила подписку.")
        return

    if intent == "get_subscriptions":
        items = get_active_subscriptions()
        if items:
            lines = ["📋 Активные подписки:"]
            for item in items:
                lines.append(
                    f"- {item.get('name')}: {_format_currency(item.get('amount'))} тг/мес "
                    f"({item.get('bank')})"
                )
            await safe_answer(message, "\n".join(lines))
        else:
            await safe_answer(message, "Нет активных подписок.")
        return

    # ── НАПОМИНАНИЯ ──
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
            lines = ["📋 Активные напоминания:"]
            for item in items:
                lines.append(
                    f"- {item.get('target_user')}: {item.get('text')} "
                    f"({item.get('remind_at')})"
                )
            await safe_answer(message, "\n".join(lines))
        else:
            await safe_answer(message, "Нет активных напоминаний.")
        return

    # ── СПИСОК ПОКУПОК ──
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
            lines = ["🛒 Список покупок:"]
            for item in items:
                lines.append(f"- {item.get('item')}")
            await safe_answer(message, "\n".join(lines))
        else:
            await safe_answer(message, "Список покупок пуст.")
        return

    # ── ПОЕЗДКИ ──
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
            lines = ["✈️ Запланированные поездки:"]
            for item in items:
                lines.append(
                    f"- {item.get('destination')} ({item.get('dates')}): "
                    f"{_format_currency(item.get('budget'))} тг"
                )
            await safe_answer(message, "\n".join(lines))
        else:
            await safe_answer(message, "Нет запланированных поездок.")
        return

    # ── ЛИМИТЫ ──
    if intent == "get_limits":
        current_limits = get_category_limits()
        if current_limits:
            lines = ["📊 Текущие лимиты:"]
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
                lines = ["📊 Сгенерированные лимиты:"]
                for cat, limit in new_limits.items():
                    lines.append(f"- {cat}: {_format_currency(limit)} тг")
                await safe_answer(message, "\n".join(lines))
            else:
                await safe_answer(message, "Недостаточно данных для генерации лимитов.")
        except Exception as error:
            print(f"[Лимиты] Ошибка генерации: {error}")
            await safe_answer(message, "Не удалось сгенерировать лимиты.")
        return

    # ── СВОДКА И ДОХОДЫ ──
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
        lines = [f"📊 Сводка за {now.strftime('%B %Y')}:"]
        lines.append(f"💰 Доходы: {_format_currency(total_income)} тг")
        lines.append(f"💸 Расходы: {_format_currency(total_expense)} тг")
        lines.append(f"📈 Баланс: {_format_currency(total_income - total_expense)} тг")
        lines.append("")
        lines.append("📉 По категориям:")
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
        lines = [f"💰 Доходы за {now.strftime('%B %Y')}: {_format_currency(total)} тг"]
        for t in incomes:
            lines.append(f"  - {t.get('cat')}: {_format_currency(t.get('amt'))} тг ({t.get('comm')})")
        await safe_answer(message, "\n".join(lines))
        return

    # ── ПОГОДА ──
    if intent == "get_weather":
        forecast = await get_weather_forecast()
        if forecast:
            await safe_answer(message, forecast)
        else:
            await safe_answer(message, "Не удалось получить прогноз погоды.")
        return

    # ── УДАЛЕНИЕ ТРАНЗАКЦИИ ──
    if intent == "delete_transaction":
        query = parsed.get("search_query", "")
        if query:
            delete_record_by_keyword("Transactions", query)
        await safe_answer(message, reply or "Удалила запись.")
        return

    # ── ОБЫЧНЫЙ ЧАТ ──
    await safe_answer(message, reply or "Чем могу помочь?")
