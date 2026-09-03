"""Обработка фото, PDF и скриншотов чеков."""

from aiogram.types import Message
from services.vision import parse_receipt
from services.sheets import append_transaction
from services.telegram_safe import safe_answer
from services.pending_receipts import set_pending
from services.pending_clarifications import set_clarification
from services.categories import (
    TYPE_EXPENSE, TYPE_INCOME,
    EXPENSE_CATEGORIES, INCOME_CATEGORIES,
    FALLBACK_EXPENSE_CATEGORY, FALLBACK_INCOME_CATEGORY,
    normalize_category, normalize_subcategory,
    validate_transaction_category_subcategory,
)
from services.sheets import normalize_necessity
from services.banks import normalize_bank_source


async def handle_media(message: Message):
    user_name = message.from_user.first_name or "Пользователь"
    chat_id = message.chat.id
    caption = message.caption or ""

    if message.photo:
        photo = message.photo[-1]
        file = await message.bot.get_file(photo.file_id)
        file_bytes = await message.bot.download_file(file.file_path)
        file_bytes = file_bytes.read()
        filename = f"{photo.file_id}.jpg"
    elif message.document:
        doc = message.document
        file = await message.bot.get_file(doc.file_id)
        file_bytes = await message.bot.download_file(file.file_path)
        file_bytes = file_bytes.read()
        filename = doc.file_name or f"{doc.file_id}.pdf"
    else:
        await safe_answer(message, "Не распознал формат файла. Пришли фото или PDF.")
        return

    result = await parse_receipt(file_bytes, filename, caption, user_name)
    transactions = result.get("transactions", [])
    reply = result.get("reply", "")

    if not transactions:
        await safe_answer(message, reply or "Не удалось распознать чек.")
        return

    # Проверяем, есть ли транзакции с низкой confidence
    low_confidence_txs = [tx for tx in transactions if float(tx.get("confidence", 1.0)) < 0.8 and tx.get("alternatives")]

    if low_confidence_txs and not caption:
        # Если есть неоднозначные и нет подписи — показываем кнопки для первой
        tx = low_confidence_txs[0]
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        buttons = [[InlineKeyboardButton(text=alt, callback_data=f"clarify_cat:{alt[:20]}")]
                   for alt in tx.get("alternatives", [])]
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        # Валидируем базовые поля
        tx_type = str(tx.get("type") or TYPE_EXPENSE).strip().upper()
        valid_categories = INCOME_CATEGORIES if tx_type == TYPE_INCOME else EXPENSE_CATEGORIES
        fallback_cat = FALLBACK_INCOME_CATEGORY if tx_type == TYPE_INCOME else FALLBACK_EXPENSE_CATEGORY
        tx["category"] = normalize_category(tx.get("category"), valid_categories, fallback_cat)
        tx["source"] = normalize_bank_source(tx.get("bank"), tx.get("source"))
        if not tx.get("bank"): tx["bank"] = "Не указан"
        if not tx.get("currency"): tx["currency"] = "KZT"
        if not tx.get("funds_type"): tx["funds_type"] = "Собственные"
        if not tx.get("resource"): tx["resource"] = "Карта"
        if not tx.get("user"): tx["user"] = user_name
        if not tx.get("merchant"): tx["merchant"] = ""
        if not tx.get("user_comment"): tx["user_comment"] = caption or ""
        if not tx.get("ai_comment"): tx["ai_comment"] = reply or ""

        set_clarification(chat_id, tx, tx.get("alternatives", []))
        sent = await message.answer(reply or "Не уверена в категории. Выбери:", reply_markup=kb)
        return

    # Валидация и заполнение ВСЕХ полей
    validated_transactions = []
    for tx in transactions:
        tx_type = str(tx.get("type") or TYPE_EXPENSE).strip().upper()
        valid_categories = INCOME_CATEGORIES if tx_type == TYPE_INCOME else EXPENSE_CATEGORIES
        fallback_cat = FALLBACK_INCOME_CATEGORY if tx_type == TYPE_INCOME else FALLBACK_EXPENSE_CATEGORY

        raw_cat = tx.get("category")
        cat = normalize_category(raw_cat, valid_categories, fallback_cat)
        tx["category"] = cat
        raw_sub = tx.get("subcategory")
        _, valid_sub = validate_transaction_category_subcategory(cat, raw_sub)
        tx["subcategory"] = valid_sub or normalize_subcategory(None, cat, "")
        tx["necessity"] = normalize_necessity(tx.get("necessity"), cat)
        tx["source"] = normalize_bank_source(tx.get("bank"), tx.get("source"))
        if not tx.get("bank"): tx["bank"] = "Не указан"
        if not tx.get("currency"): tx["currency"] = "KZT"
        if not tx.get("funds_type"): tx["funds_type"] = "Собственные"
        if not tx.get("resource"): tx["resource"] = "Карта"
        if not tx.get("user"): tx["user"] = user_name
        if not tx.get("merchant"): tx["merchant"] = ""
        if not tx.get("user_comment"): tx["user_comment"] = caption or ""
        if not tx.get("ai_comment"): tx["ai_comment"] = reply or ""

        validated_transactions.append(tx)

    if caption:
        for tx in validated_transactions:
            tx["user_comment"] = caption
            tx["user"] = user_name
            append_transaction(tx)
        await safe_answer(message, reply or f"Записала {len(validated_transactions)} покупок.")
    else:
        set_pending(chat_id, validated_transactions, user_name)
        await safe_answer(
            message,
            reply or f"Распознала {len(validated_transactions)} покупок. Напиши комментарий к чеку, и я запишу."
        )
