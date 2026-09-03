"""Обработка текстовых сообщений и уточнений."""

import asyncio
import datetime
import json
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
    add_or_update_subscription, get_active_subscriptions, deactivate_subscription,
    add_installment, get_installments, close_installment,
    split_last_transaction_by_amount, find_and_update_record, delete_record_by_keyword,
    get_transactions_for_period, find_recent_duplicate_transaction,
    debug_transactions_snapshot, normalize_necessity,
)
from services.charts import generate_expense_chart
from services.limits_ai import generate_limits_from_history
from services.weather import get_weather_forecast
from services.telegram_safe import safe_answer, safe_send_message
from services.pending_receipts import has_pending, pop_pending
from services.pending_clarifications import (
    set_clarification, get_clarification, pop_clarification,
)
from services.memory import get_chat_history, add_chat_message
from services.voice import transcribe_voice


def _format_currency(value):
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (ValueError, TypeError):
        return str(value)


def _format_confirmation_report(tx: dict, ai_comment: str = "") -> str:
    """Формирует аккуратную отчётность о записи, как на Скриншоте 2."""
    amt = _format_currency(tx.get("amount", 0))
    curr = tx.get("currency", "KZT")
    bank = tx.get("bank", "Не указан")
    if tx.get("resource") == "Наличные":
        bank_source = "Наличные"
    else:
        bank_source = tx.get("source") or bank

    cat = tx.get("category", "")
    comm = tx.get("user_comment", "").strip()
    comm_str = f" ({comm})" if comm else ""

    lines = [
        "✍️ **Записано:**",
        f"• {amt} {curr} | {bank_source} | {cat}{comm_str}",
    ]
    if ai_comment:
        lines.append(f"\n💬 _{ai_comment}_")
    return "\n".join(lines)


def _build_clarification_keyboard(options: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for idx, opt in enumerate(options):
        # Используем индекс, чтобы callback_data гарантированно была короткой и надёжной
        buttons.append([InlineKeyboardButton(text=opt["label"], callback_data=f"clarify_idx:{idx}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def handle_category_clarification_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    data = callback.data

    if not data.startswith("clarify_idx:"):
        return

    idx = int(data.split(":")[1])
    clarification = pop_clarification(chat_id)

    if not clarification:
        await callback.answer("Уточнение уже не актуально.", show_alert=True)
        return

    options = clarification.get("options", [])
    if idx >= len(options):
        await callback.answer("Ошибка выбора.")
        return

    selected = options[idx]
    tx = clarification["transaction"]
    tx["category"] = selected["category"]
    tx["subcategory"] = selected.get("subcategory", "")
    tx["necessity"] = normalize_necessity(tx.get("necessity"), tx["category"])

    append_transaction(tx)
    report = _format_confirmation_report(tx, "Уточнили категорию и сохранили.")
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
        await safe_answer(message, "Не удалось загрузить аудио.")
        return

    text = await transcribe_voice(file_bytes)
    if not text:
        await safe_answer(message, "Не удалось распознать голос.")
        return
    await safe_answer(message, f"🎤 «{text}»")
    await _process_text_message(message, text)


async def _process_text_message(message: Message, text: str):
    user_name = message.from_user.first_name or "Пользователь"
    chat_id = message.chat.id
    add_chat_message(chat_id, user_name, text)

    # 1. Если ждём текстовый комментарий к чеку
    if has_pending(chat_id):
        pending = pop_pending(chat_id)
        if pending:
            transactions, receipt_user = pending
            lines = ["📸 **Записано по чеку:**"]
            for tx in transactions:
                tx["user_comment"] = text
                tx["user"] = receipt_user
                append_transaction(tx)
                lines.append(f"• {_format_currency(tx.get('amount'))} {tx.get('currency')} | {tx.get('bank')} | {tx.get('category')} ({text})")
            await safe_answer(message, "\n".join(lines))
            return

    # 2. Если висит ожидание уточнения (Влад ответил текстом «на работу», «домой» и т.п.)
    clarification = get_clarification(chat_id)
    if clarification:
        text_lower = text.lower()
        matched_opt = None
        for opt in clarification.get("options", []):
            label_words = re.findall(r'\w+', opt["label"].lower())
            if any(w in text_lower for w in label_words if len(w) > 3):
                matched_opt = opt
                break
        if "работ" in text_lower or "кафе" in text_lower or "собой" in text_lower:
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
            report = _format_confirmation_report(tx, f"Поняла, записала: {matched_opt['label']}.")
            await safe_answer(message, report)
            return

    # 3. График (/chart)
    if text.strip().lower() in {"/chart", "график", "чарт"}:
        try:
            image_bytes = await asyncio.to_thread(generate_expense_chart)
            if image_bytes:
                photo_file = BufferedInputFile(image_bytes, filename="chart.png")
                await message.answer_photo(photo=photo_file, caption="📊 Финансовый дашборд за текущий месяц.")
            else:
                await safe_answer(message, "Нет данных о тратах за этот месяц.")
        except Exception as error:
            print(f"[График] Ошибка: {error}")
            await safe_answer(message, "Не удалось построить график.")
        return

    # 4. Анализ через DeepSeek
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

    # УТОЧНЕНИЕ С КНОПКАМИ
    if intent == "need_clarification":
        tx = parsed.get("transaction", {})
        options = parsed.get("clarification_options", [])
        if tx and options:
            tx["user"] = user_name
            tx["source"] = normalize_bank_source(tx.get("bank"), tx.get("source"))
            kb = _build_clarification_keyboard(options)
            set_clarification(chat_id, tx, options)
            await message.answer(reply or "Уточни, куда отнести покупку:", reply_markup=kb)
            return

    # ОБЫЧНАЯ ТРАНЗАКЦИЯ
    if intent == "transaction":
        tx = parsed.get("transaction", {})
        if not tx:
            await safe_answer(message, reply or "Не удалось распознать трату.")
            return

        tx_type = str(tx.get("type") or TYPE_EXPENSE).strip().upper()
        valid_cats = INCOME_CATEGORIES if tx_type == TYPE_INCOME else EXPENSE_CATEGORIES
        fallback_cat = FALLBACK_INCOME_CATEGORY if tx_type == TYPE_INCOME else FALLBACK_EXPENSE_CATEGORY

        cat = normalize_category(tx.get("category"), valid_cats, fallback_cat)
        tx["category"] = cat
        _, valid_sub = validate_transaction_category_subcategory(cat, tx.get("subcategory"))
        tx["subcategory"] = valid_sub or normalize_subcategory(None, cat, "")
        tx["necessity"] = normalize_necessity(tx.get("necessity"), cat)
        tx["source"] = normalize_bank_source(tx.get("bank"), tx.get("source"))
        tx["user"] = user_name
        if not tx.get("currency"): tx["currency"] = "KZT"
        if not tx.get("bank"): tx["bank"] = "Не указан"
        if not tx.get("funds_type"): tx["funds_type"] = "Собственные"
        if not tx.get("resource"): tx["resource"] = "Карта"

        append_transaction(tx)
        report = _format_confirmation_report(tx, reply)
        await safe_answer(message, report)
        return

    # ПОГОДА
    if intent == "get_weather":
        forecast = await get_weather_forecast()
        await safe_answer(message, forecast or "Не удалось связаться с погодной станцией.")
        return

    # РАССРОЧКИ
    if intent == "get_installments":
        items = get_installments()
        if items:
            lines = ["📋 **Активные рассрочки:**"]
            for item in items:
                lines.append(f"- {item.get('description')} ({item.get('bank')}): {_format_currency(item.get('total_amount'))} тг")
            await safe_answer(message, "\n".join(lines))
        else:
            await safe_answer(message, "Активных рассрочек нет.")
        return

    # ОБЩИЙ ЧАТ
    await safe_answer(message, reply or "На связи!")